from __future__ import annotations

from datetime import timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..models import Device, DeviceStatusHistory, HAEntityCache, MatterFrameBinding, Photo
from ..services.home_assistant import supervisor_connection
from ..services.matter_controller import MatterControllerError, get_controller

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


@router.get("")
def diagnostics(db: Session = Depends(get_db)) -> dict[str, object]:
    settings = get_settings()
    controller = get_controller()
    controller_status = "not_configured"
    if controller:
        try:
            controller_status = "ready" if controller.health().get("ok") is True else "unavailable"
        except MatterControllerError:
            controller_status = "unavailable"
    last_displayed_at = db.scalar(
        select(func.max(DeviceStatusHistory.created_at)).where(DeviceStatusHistory.status == "displayed_hub_frame")
    )
    return {
        "app": settings.app_name,
        "version": settings.version,
        "mode": "supervisor" if supervisor_connection() else "standalone",
        "matter_controller": controller_status,
        "registered_displays": db.scalar(select(func.count()).select_from(Device)),
        "matter_links": db.scalar(select(func.count()).select_from(MatterFrameBinding)),
        "ready_photos": db.scalar(select(func.count()).select_from(Photo).where(Photo.processed_status == "ready")),
        "cached_entities": db.scalar(select(func.count()).select_from(HAEntityCache)),
        "last_displayed_at": last_displayed_at.replace(tzinfo=timezone.utc).isoformat() if last_displayed_at else None,
    }
