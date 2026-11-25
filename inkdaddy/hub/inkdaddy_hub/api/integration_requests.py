from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.integration_request_protocol import INTEGRATION_ID_PATTERN, MAX_NAME_LENGTH, request_data
from ..services.integration_requests import (
    IntegrationRequest, RequestLimit, enqueue_request, relay_config, request_payload, retry_request,
)

router = APIRouter(prefix="/api/integration-requests", tags=["integration-requests"])


class NewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    integration_name: str = Field(max_length=MAX_NAME_LENGTH)


@router.get("")
def list_requests(db: Session = Depends(get_db)) -> dict[str, object]:
    relay, _ = relay_config()
    rows = db.scalars(select(IntegrationRequest).order_by(IntegrationRequest.created_at.desc())).all()
    return {"relay": relay, "requests": [request_payload(row) for row in rows]}


@router.get("/status")
def request_status(integration_id: str | None = None, db: Session = Depends(get_db)) -> dict[str, object]:
    row = None
    if integration_id is not None:
        if INTEGRATION_ID_PATTERN.fullmatch(integration_id):
            row = db.get(IntegrationRequest, integration_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Integration request not found.")
    relay, _ = relay_config()
    return {"relay": relay, "request": request_payload(row) if row is not None else None}


@router.post("")
def create_request(payload: NewRequest, db: Session = Depends(get_db)) -> dict[str, object]:
    try:
        request = request_data(payload.integration_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        row, duplicate = enqueue_request(db, request)
    except RequestLimit as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {"request": request_payload(row), "duplicate": duplicate}


@router.post("/{integration_id}/retry")
def request_retry(integration_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    if INTEGRATION_ID_PATTERN.fullmatch(integration_id) is None:
        raise HTTPException(status_code=404, detail="Integration request not found.")
    if relay_config()[1] is None:
        raise HTTPException(status_code=409, detail="Configure the owner relay before retrying.")
    try:
        row = retry_request(db, integration_id)
    except RequestLimit as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Integration request not found.")
    return request_payload(row)
