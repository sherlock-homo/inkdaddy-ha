from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.photo_places import enqueue_existing, set_automatic, status

router = APIRouter(prefix="/api/photo-places", tags=["photos"])


class PhotoPlaceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


@router.get("")
def place_settings(db: Session = Depends(get_db)) -> dict[str, object]:
    return status(db)


@router.put("")
def update_place_settings(payload: PhotoPlaceSettings, db: Session = Depends(get_db)) -> dict[str, object]:
    set_automatic(db, payload.enabled)
    return status(db)


@router.post("/lookup")
def lookup_existing(db: Session = Depends(get_db)) -> dict[str, object]:
    enqueue_existing(db)
    return status(db)
