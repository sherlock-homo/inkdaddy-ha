from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..models import Setting
from ..services.home_assistant import supervisor_connection
from ..services.playback import begin_playback_write

router = APIRouter(prefix="/api/settings", tags=["settings"])

PhotoFraming = Literal["fit", "fill", "intelligent"]
PHOTO_FRAMING_KEY = "photo_upload_mode"


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    photo_upload_mode: PhotoFraming


def photo_upload_mode(db: Session) -> PhotoFraming:
    saved = db.get(Setting, PHOTO_FRAMING_KEY)
    if saved:
        try:
            value = json.loads(saved.value_json)
        except ValueError:
            value = None
        if value in ("fill", "crop"):
            return "fill"
        if value == "intelligent":
            return "intelligent"
    return "fit"


@router.get("")
def get_hub_settings(db: Session = Depends(get_db)) -> dict[str, object]:
    settings = get_settings()
    return {
        "mode": "supervisor" if supervisor_connection() else "standalone",
        "version": settings.version,
        "photo_upload_mode": photo_upload_mode(db),
        "scheduled_refresh_minutes": 60,
        "target": {"width": settings.default_width, "height": settings.default_height},
    }


@router.put("")
def update_hub_settings(payload: SettingsUpdate, db: Session = Depends(get_db)) -> dict[str, object]:
    begin_playback_write(db)
    db.merge(Setting(key=PHOTO_FRAMING_KEY, value_json=json.dumps(payload.photo_upload_mode)))
    db.commit()
    return get_hub_settings(db)
