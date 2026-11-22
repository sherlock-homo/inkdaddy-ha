from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Dashboard, Device
from ..services.dashboard import (
    DashboardValidationError,
    SAMPLE_DASHBOARD,
    load_dashboard_yaml,
    validate_dashboard_config,
)
from ..services.dashboard_content import (
    dashboard_config,
    get_dashboard_record,
    render_dashboard,
    resolve_widget_text,
)
from ..services.playback import begin_playback_write, playlist_uses_dashboard
from ..services.rendering import render_preview_png_bytes

router = APIRouter(prefix="/api/dashboards", tags=["dashboards"])


def _parse(raw_yaml: str) -> dict[str, Any]:
    try:
        return validate_dashboard_config(load_dashboard_yaml(raw_yaml))
    except (DashboardValidationError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _payload(record: Dashboard | None) -> dict[str, object]:
    return {
        "id": record.slug if record else "sample",
        "name": record.name if record else "Home Overview",
        "yaml": record.current_yaml if record else SAMPLE_DASHBOARD,
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
def preview_unsaved(payload: dict[str, object], db: Session = Depends(get_db)) -> Response:
    config = _parse(str(payload.get("yaml") or SAMPLE_DASHBOARD))
    return _preview(db, config, str(payload["device_uid"]) if payload.get("device_uid") else None)


@router.get("/{dashboard_id}")
def get_dashboard(dashboard_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    return _payload(_find(db, dashboard_id))


@router.put("/{dashboard_id}")
def update_dashboard(
    dashboard_id: str, payload: dict[str, object], db: Session = Depends(get_db)
) -> dict[str, object]:
    begin_playback_write(db)
    record = _find(db, dashboard_id)
    raw_yaml = str(payload.get("yaml") or (record.current_yaml if record else SAMPLE_DASHBOARD))
    config = _parse(raw_yaml)
    name = str(payload.get("name") or config.get("name") or dashboard_id).strip()[:255]
    if record is None:
        record = Dashboard(slug=dashboard_id, name=name, current_yaml=raw_yaml)
        db.add(record)
    else:
        record.name = name
        record.current_yaml = raw_yaml
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
    if dashboard_id == "sample" or device_uses or playlist_uses_dashboard(db, dashboard_id):
        raise HTTPException(status_code=409, detail="This dashboard is in use.")
    record = _find(db, dashboard_id)
    db.delete(record)
    db.commit()
    return {"deleted": True}


@router.get("/{dashboard_id}/preview")
@router.post("/{dashboard_id}/preview")
def preview_dashboard(
    dashboard_id: str, device_uid: str | None = None, db: Session = Depends(get_db)
) -> Response:
    config = dashboard_config(_find(db, dashboard_id))
    return _preview(db, config, device_uid)


def _preview(db: Session, config: dict[str, Any], device_uid: str | None) -> Response:
    device = (
        db.scalar(select(Device).where(Device.device_uid == device_uid)) if device_uid else None
    )
    widgets = resolve_widget_text(db, config, datetime.now(timezone.utc), device, time_kind="preview")
    return Response(
        content=render_preview_png_bytes(render_dashboard(config, widgets)), media_type="image/png"
    )


@router.get("/{dashboard_id}/export")
def export_dashboard(dashboard_id: str, db: Session = Depends(get_db)) -> Response:
    record = _payload(_find(db, dashboard_id))
    return Response(content=str(record["yaml"]), media_type="application/x-yaml")
