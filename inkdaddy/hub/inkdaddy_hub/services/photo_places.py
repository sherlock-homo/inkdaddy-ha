"""Explicit, persistent reverse-geocoding jobs for photo originals."""

from __future__ import annotations

import hashlib
import logging
import math
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import SessionLocal
from ..models import Photo, Setting
from .geocoding import ProviderUnavailable, reverse_place
from .photo_metadata import capture_details

AUTO = "photo_places_auto"
NEXT_REQUEST = "photo_places_provider_after"
PENDING = ("batch", "auto")
LOGGER = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def auto_enabled(db: Session) -> bool:
    row = db.get(Setting, AUTO)
    return bool(row and row.value_json == "true")


def status(db: Session) -> dict[str, object]:
    counts = dict(db.execute(select(Photo.place_lookup, func.count()).group_by(Photo.place_lookup)).all())
    return {"enabled": auto_enabled(db), "pending": sum(counts.get(k, 0) for k in PENDING),
            "resolved": counts.get("found", 0), "unavailable": counts.get("missing", 0) + counts.get("failed", 0)}


def _setting(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row:
        row.value_json = value
    else:
        db.add(Setting(key=key, value_json=value))


def set_automatic(db: Session, enabled: bool) -> None:
    from .playback import begin_playback_write

    begin_playback_write(db)
    _setting(db, AUTO, "true" if enabled else "false")
    if not enabled:
        db.execute(update(Photo).where(Photo.place_lookup == "auto").values(
            place_lookup=None, place_retry_at=None, place_attempts=None))
    db.commit()


def enqueue_existing(db: Session) -> None:
    from .playback import begin_playback_write

    begin_playback_write(db)
    db.execute(update(Photo).where(Photo.original_path != "", or_(
        Photo.place_lookup.is_(None), Photo.place_lookup == "failed",
        (Photo.place_lookup == "missing") & (Photo.place_retry_at <= _now()),
    )).values(place_lookup="batch", place_retry_at=None, place_attempts=None))
    db.execute(update(Photo).where(Photo.place_lookup == "auto").values(place_lookup="batch"))
    db.commit()


def _next_job(db: Session, now: datetime):
    return db.scalar(select(Photo).where(Photo.place_lookup.in_(PENDING),
        or_(Photo.place_retry_at.is_(None), Photo.place_retry_at <= now))
        .order_by(Photo.id).limit(1))


def _current_job(db: Session, photo_id: int, path: str, lease: datetime) -> Photo | None:
    row = db.get(Photo, photo_id, populate_existing=True)
    if row and row.original_path == path and row.place_lookup in PENDING and row.place_retry_at == lease:
        return row
    return None


def _claim(db: Session, now: datetime):
    from .playback import begin_playback_write

    if _next_job(db, now) is None:
        return None
    db.rollback()
    begin_playback_write(db)
    row = _next_job(db, now)
    if row is None:
        return None
    lease = now + timedelta(minutes=2)
    row.place_retry_at = lease
    job = row.id, row.original_path, lease
    db.commit()
    return job


def process_one(sessions=SessionLocal) -> bool:
    from .playback import begin_playback_write

    with sessions() as db:
        job = _claim(db, _now())
    if job is None:
        return False
    _, path, _ = job
    details = capture_details(get_settings().data_dir, path, want_date=False, want_location=True)
    with sessions() as db:
        begin_playback_write(db)
        row = _current_job(db, *job)
        if row is None:
            return True
        if details.coordinates is None:  # Embedded place name, no GPS or unreadable original.
            row.place_lookup, row.place_retry_at = "skipped", None
            db.commit()
            return True
        lat, lon = details.coordinates
        coarse = all(math.isclose(n, round(n, 2), rel_tol=0, abs_tol=1e-9) for n in (lat, lon))
        precise = not coarse and (details.accuracy_m is None or 0 <= details.accuracy_m <= 25)
        key = hashlib.sha256(f"{lat:.6f},{lon:.6f},{precise}".encode()).hexdigest()
        row.place_key = key
        now = _now()
        cached = db.scalar(select(Photo).where(Photo.place_key == key, or_(Photo.place_lookup == "found",
            (Photo.place_lookup == "missing") & (Photo.place_retry_at > now))).limit(1))
        if cached:
            row.place_label, row.place_lookup, row.place_retry_at = cached.place_label, cached.place_lookup, cached.place_retry_at
            db.commit()
            return True
        throttle = db.get(Setting, NEXT_REQUEST)
        if throttle and (available := datetime.fromisoformat(throttle.value_json)) > now:
            row.place_retry_at = available
            db.commit()
            return True
        # A lease keeps other processes from repeating an in-flight point lookup.
        provider_lease = (now + timedelta(seconds=30)).isoformat()
        _setting(db, NEXT_REQUEST, provider_lease)
        db.commit()
    # All file and network I/O happens outside the SQLite writer transaction.
    try:
        label = reverse_place(lat, lon, precise=precise)
        delay = 0
    except ProviderUnavailable as error:
        label = ""
        delay = min(error.retry_seconds, 7 * 86400)
    with sessions() as db:
        begin_playback_write(db)
        retry = _now() + timedelta(seconds=delay)
        throttle = db.get(Setting, NEXT_REQUEST)
        if throttle and throttle.value_json == provider_lease:
            throttle.value_json = (retry if delay else now + timedelta(seconds=2)).isoformat()
        row = _current_job(db, *job)
        if row is not None:
            row.place_attempts = (row.place_attempts or 0) + 1
            if delay:
                if row.place_attempts >= 3:
                    row.place_lookup = "failed"
                row.place_retry_at = retry
            else:
                row.place_lookup = "found" if label else "missing"
                row.place_label = label or None
                row.place_retry_at = None if label else _now() + timedelta(days=7)
        db.commit()
    return True


def run_photo_places(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            worked = process_one()
        except (SQLAlchemyError, OSError):
            LOGGER.warning("Photo place lookup temporarily unavailable.")
            worked = False
        stop.wait(0.2 if worked else 5)
