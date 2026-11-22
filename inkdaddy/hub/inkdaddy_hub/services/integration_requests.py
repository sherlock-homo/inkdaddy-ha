from __future__ import annotations

import http.client
import json
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, Integer, String, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.exc import SQLAlchemyError

from ..database import Base, SessionLocal
from .integration_request_protocol import (
    MAX_ACK_BYTES, Relay, RequestData, accepted_payload, configured_relay, relay_request, request_body,
)
from .playback import begin_playback_write

MAX_REQUESTS_PER_DAY = 5
MAX_SAVED_REQUESTS = 128
MAX_ATTEMPTS = 6
LEASE_TIME = timedelta(minutes=2)
RETRY_COOLDOWN = timedelta(minutes=1)
URL_ENV = "INKDADDY_INTEGRATION_RELAY_URL"
TOKEN_ENV = "INKDADDY_INTEGRATION_RELAY_TOKEN"


class IntegrationRequest(Base):
    __tablename__ = "integration_requests"

    integration_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    integration_name: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease: Mapped[str | None] = mapped_column(String(32))


@dataclass(frozen=True)
class Claim:
    request: RequestData
    attempts: int
    lease: str


class RequestLimit(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def relay_config() -> tuple[str, Relay | None]:
    url, token = os.environ.get(URL_ENV, ""), os.environ.get(TOKEN_ENV, "")
    if not url and not token:
        return "unconfigured", None
    try:
        return "ready", configured_relay(url, token)
    except ValueError:
        return "invalid", None


def request_payload(row: IntegrationRequest) -> dict[str, str]:
    return {
        "integration_id": row.integration_id,
        "integration_name": row.integration_name,
        "status": row.status,
        "created_at": row.created_at.replace(tzinfo=timezone.utc).isoformat(),
        "updated_at": row.updated_at.replace(tzinfo=timezone.utc).isoformat(),
    }


def enqueue_request(db: Session, request: RequestData) -> tuple[IntegrationRequest, bool]:
    begin_playback_write(db)
    existing = db.get(IntegrationRequest, request.integration_id)
    if existing is not None:
        return existing, True
    now = _now()
    count = select(func.count()).select_from(IntegrationRequest)
    if (db.scalar(count.where(IntegrationRequest.created_at > now - timedelta(days=1))) or 0) >= MAX_REQUESTS_PER_DAY:
        raise RequestLimit("The daily limit for new integration requests was reached.")
    if (db.scalar(count) or 0) >= MAX_SAVED_REQUESTS:
        raise RequestLimit("The integration request queue has reached its capacity.")
    row = IntegrationRequest(
        integration_id=request.integration_id, integration_name=request.integration_name,
        status="queued", created_at=now, updated_at=now, next_attempt_at=now,
    )
    db.add(row)
    db.commit()
    return row, False


def retry_request(db: Session, integration_id: str) -> IntegrationRequest | None:
    begin_playback_write(db)
    row = db.get(IntegrationRequest, integration_id)
    if row is None or row.status != "failed":
        return row
    now = _now()
    if now - row.updated_at < RETRY_COOLDOWN:
        raise RequestLimit("Wait one minute before retrying delivery.")
    row.status = "queued"
    row.next_attempt_at = now
    row.updated_at = now
    row.attempts = 0
    row.lease = None
    db.commit()
    return row


def _claim(db: Session, now: datetime) -> Claim | None:
    begin_playback_write(db)
    row = db.scalar(
        select(IntegrationRequest).where(
            IntegrationRequest.status.in_(("queued", "retrying", "sending")),
            IntegrationRequest.next_attempt_at <= now,
        ).order_by(IntegrationRequest.next_attempt_at, IntegrationRequest.created_at).limit(1)
    )
    if row is None:
        return None
    if row.attempts >= MAX_ATTEMPTS:
        row.status = "failed"
        row.lease = None
        row.updated_at = now
        db.commit()
        return None
    row.attempts += 1
    row.lease = secrets.token_hex(16)
    row.status = "sending"
    row.updated_at = now
    row.next_attempt_at = now + LEASE_TIME
    claim = Claim(RequestData(row.integration_id, row.integration_name), row.attempts, row.lease)
    db.commit()
    return claim


def _send(relay: Relay, request: RequestData) -> str:
    try:
        status, receipt = relay_request("POST", relay, request_body(request))
    except ValueError:
        return "failed"
    except (OSError, http.client.HTTPException):
        return "retrying"
    if status in (200, 201):
        try:
            if len(receipt) <= MAX_ACK_BYTES and json.loads(receipt.decode("utf-8")) == accepted_payload(request):
                return "delivered"
        except (ValueError, RecursionError):
            pass
        return "failed"
    if status in (408, 425, 429) or status >= 500:
        return "retrying"
    return "failed"


def deliver_next(relay: Relay) -> bool:
    with SessionLocal() as db:
        claim = _claim(db, _now())
    if claim is None:
        return False
    status = _send(relay, claim.request)
    if status == "retrying" and claim.attempts >= MAX_ATTEMPTS:
        status = "failed"

    # Relay writes deduplicate the same ID if a crash happened after acceptance.
    with SessionLocal() as db:
        begin_playback_write(db)
        row = db.get(IntegrationRequest, claim.request.integration_id)
        if row is not None and row.lease == claim.lease:
            now = _now()
            row.status = status
            row.updated_at = now
            row.next_attempt_at = now + timedelta(seconds=30 * 2 ** (claim.attempts - 1))
            row.lease = None
            db.commit()
    return True


def run_integration_requests(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        _, relay = relay_config()
        try:
            if relay is not None:
                deliver_next(relay)
        except SQLAlchemyError:
            # A temporary database lock can retry during the next sweep.
            pass
        stop_event.wait(5)
