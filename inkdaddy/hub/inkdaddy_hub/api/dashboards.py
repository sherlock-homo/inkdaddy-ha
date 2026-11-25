from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from PIL import Image
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..models import Dashboard, DashboardVersion, Device, Photo
from ..services.dashboard import (
    DashboardValidationError,
    HISTORICAL_SAMPLE_DASHBOARD,
    SAMPLE_DASHBOARD,
    load_dashboard_yaml,
    validate_dashboard_config,
)
from ..services.dashboard_content import (
    PhotoOverlay,
    dashboard_config,
    editable_dashboard_yaml,
    get_dashboard_record,
    render_dashboard,
    resolve_widget_text,
)
from ..services.dashboard_calendar import calendar_connection, calendar_requests, refresh_calendar_cache
from ..services.photos import load_color_managed_photo
from ..services.playback import begin_playback_write, playlist_uses_dashboard
from ..services.rendering import render_preview_png_bytes

router = APIRouter(prefix="/api/dashboards", tags=["dashboards"])


class PhotoOverlayAssignment(BaseModel):
    dashboard_id: str | None = Field(max_length=128)


class DashboardPreviewRequest(BaseModel):
    yaml: str | None = None
    device_uid: str | None = Field(default=None, max_length=128)
    photo_id: int | None = Field(default=None, gt=0, le=2**63 - 1)


def _parse(raw_yaml: str) -> dict[str, Any]:
    try:
        return validate_dashboard_config(load_dashboard_yaml(raw_yaml))
    except (DashboardValidationError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _payload(record: Dashboard | None) -> dict[str, object]:
    return {
        "id": record.slug if record else "sample",
        "name": record.name if record else "Home Overview",
        "yaml": editable_dashboard_yaml(record.current_yaml) if record else SAMPLE_DASHBOARD,
        "config": dashboard_config(record),
        "active": (record is None or record.slug == "sample"),
    }


def _find(db: Session, dashboard_id: str) -> Dashboard | None:
    record = get_dashboard_record(db, dashboard_id)
    if record is None and dashboard_id != "sample":
        raise HTTPException(status_code=404, detail="Dashboard not found.")
    return record


@router.get("")
def list_dashboards(db: Session = Depends(get_db)) -> dict[str, object]:
    rows = db.scalars(select(Dashboard).order_by(Dashboard.name, Dashboard.slug)).all()
    dashboards = [_payload(row) for row in rows]
    if not any(row.slug == "sample" for row in rows):
        dashboards.insert(0, _payload(None))
    return {"dashboards": dashboards}


@router.post("")
def create_dashboard(
    payload: dict[str, object], db: Session = Depends(get_db)
) -> dict[str, object]:
    raw_yaml = str(payload.get("yaml") or SAMPLE_DASHBOARD)
    config = _parse(raw_yaml)
    name = str(config.get("name") or "Dashboard").strip()[:255]
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(payload.get("id") or name).lower()).strip("-")[:128]
    if not slug:
        raise HTTPException(status_code=422, detail="A dashboard ID is required.")
    begin_playback_write(db)
    if slug == "sample" or get_dashboard_record(db, slug):
        raise HTTPException(status_code=409, detail="A dashboard with this ID already exists.")
    record = Dashboard(name=name, slug=slug, current_yaml=raw_yaml)
    db.add(record)
    db.commit()
    return _payload(record)


@router.post("/import")
def import_dashboard(
    payload: dict[str, object], db: Session = Depends(get_db)
) -> dict[str, object]:
    return create_dashboard(payload, db)


@router.post("/preview")
def preview_unsaved(payload: DashboardPreviewRequest, db: Session = Depends(get_db)) -> Response:
    config = _parse(payload.yaml or SAMPLE_DASHBOARD)
    return _preview(db, config, payload.device_uid, payload.photo_id)


@router.get("/photo-overlays")
def list_photo_overlays(db: Session = Depends(get_db)) -> dict[str, object]:
    devices = db.scalars(select(Device).order_by(Device.name, Device.device_uid))
    return {"overlays": [
        {"device_uid": device.device_uid, "dashboard_id": device.overlay_dashboard_id} for device in devices
    ]}


@router.put("/photo-overlays/{device_uid}")
def set_photo_overlay(
    device_uid: str, payload: PhotoOverlayAssignment, db: Session = Depends(get_db),
) -> dict[str, str | None]:
    begin_playback_write(db)
    device = db.scalar(select(Device).where(Device.device_uid == device_uid))
    if device is None:
        raise HTTPException(status_code=404, detail="Display not found.")
    if payload.dashboard_id is not None:
        config = dashboard_config(_find(db, payload.dashboard_id))
        if not config["widgets"]:
            raise HTTPException(status_code=422, detail="Add a widget to this dashboard before using it over photos.")
    device.overlay_dashboard_id = payload.dashboard_id
    db.commit()
    return {"device_uid": device.device_uid, "dashboard_id": device.overlay_dashboard_id}


@router.get("/{dashboard_id}")
def get_dashboard(dashboard_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    return _payload(_find(db, dashboard_id))


@router.put("/{dashboard_id}")
def update_dashboard(
    dashboard_id: str, payload: dict[str, object], db: Session = Depends(get_db)
) -> dict[str, object]:
    begin_playback_write(db)
    record = _find(db, dashboard_id)
    raw_yaml = str(payload.get("yaml") or (editable_dashboard_yaml(record.current_yaml) if record else SAMPLE_DASHBOARD))
    config = _parse(raw_yaml)
    name = str(payload.get("name") or config.get("name") or dashboard_id).strip()[:255]
    previous = record.current_yaml if record else HISTORICAL_SAMPLE_DASHBOARD
    if record is None:
        record = Dashboard(slug=dashboard_id, name=name, current_yaml=raw_yaml)
        db.add(record)
    else:
        record.name = name
        record.current_yaml = raw_yaml
    # Keep the pre-removal layout for exact repair of a previously offered image.
    # New layouts cannot contain a clock, so this archives at most one such copy.
    if editable_dashboard_yaml(previous) != previous:
        record.versions.append(DashboardVersion(version_number=0, yaml=previous))
    db.commit()
    return _payload(record)


@router.delete("/{dashboard_id}")
def delete_dashboard(dashboard_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    begin_playback_write(db)
    device_uses = db.scalar(
        select(Device.id)
        .where(
            Device.selected_source_type == "dashboard", Device.selected_source_id == dashboard_id
        )
        .limit(1)
    )
    overlay_uses = db.scalar(select(Device.id).where(
        (Device.overlay_dashboard_id == dashboard_id) | (Device.interlace_dashboard_id == dashboard_id)
    ).limit(1))
    if dashboard_id == "sample" or device_uses or overlay_uses or playlist_uses_dashboard(db, dashboard_id):
        raise HTTPException(status_code=409, detail="This dashboard is in use.")
    record = _find(db, dashboard_id)
    db.execute(delete(DashboardVersion).where(DashboardVersion.dashboard_id == record.id))
    db.delete(record)
    db.commit()
    return {"deleted": True}


@router.get("/{dashboard_id}/preview")
@router.post("/{dashboard_id}/preview")
def preview_dashboard(
    dashboard_id: str, device_uid: str | None = None, photo_id: int | None = Query(default=None, gt=0, le=2**63 - 1),
    db: Session = Depends(get_db),
) -> Response:
    config = dashboard_config(_find(db, dashboard_id))
    return _preview(db, config, device_uid, photo_id)


def _preview(db: Session, config: dict[str, Any], device_uid: str | None, photo_id: int | None = None) -> Response:
    device = (
        db.scalar(select(Device).where(Device.device_uid == device_uid)) if device_uid else None
    )
    image = None
    photo = None
    if photo_id is not None:
        photo = db.get(Photo, photo_id)
        image = (
            load_color_managed_photo(get_settings().data_dir, photo.processed_path, photo.original_path)
            if photo and photo.processed_status == "ready" else None
        )
        if image is None:
            raise HTTPException(status_code=404, detail="Photo preview unavailable.")
        if image.size != (800, 480):
            image = image.resize((800, 480), Image.Resampling.LANCZOS)
    now = datetime.now(timezone.utc)
    if requests := calendar_requests(config):
        connection, scope = calendar_connection(db)
        refresh_calendar_cache(connection, scope, requests, now)
    widgets = resolve_widget_text(db, config, now, device, time_kind="preview", photo=photo)
    image = PhotoOverlay(config, widgets).render(image) if image else render_dashboard(config, widgets)
    return Response(
        content=render_preview_png_bytes(image), media_type="image/png", headers={"Cache-Control": "no-store"},
    )


@router.get("/{dashboard_id}/export")
def export_dashboard(dashboard_id: str, db: Session = Depends(get_db)) -> Response:
    record = _payload(_find(db, dashboard_id))
    return Response(content=str(record["yaml"]), media_type="application/x-yaml")
