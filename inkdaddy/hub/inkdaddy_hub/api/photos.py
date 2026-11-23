from __future__ import annotations

from os import SEEK_END
from typing import BinaryIO

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..database import get_db
from ..models import Album, AlbumItem, Photo, PhotoRevision
from ..services.photo_framing import PHOTO_FRAMING_MODES, PhotoFraming
from ..services.playback import MAX_RAW_FRAME_BYTES, begin_playback_write
from ..services.photos import (
    MAX_BATCH_BYTES,
    MAX_UPLOAD_BYTES,
    PhotoEditConflict,
    PhotoProcessingError,
    album_detail,
    album_to_dict,
    create_photo_from_bytes,
    delete_photo_record,
    photo_framing_preview,
    photo_revision,
    photo_to_dict,
    read_photo_asset,
    set_album_items,
    update_photo_framing,
)
from .settings import photo_upload_mode

router = APIRouter(prefix="/api", tags=["photos"])


def _spooled_file_size(file: BinaryIO) -> int:
    position = file.tell()
    try:
        return file.seek(0, SEEK_END)
    finally:
        file.seek(position)


async def _read_image(upload: UploadFile, size: int | None = None) -> bytes:
    expected_size = upload.size if size is None else size
    if expected_size is not None and expected_size > MAX_UPLOAD_BYTES:
        raise PhotoProcessingError("Uploaded image exceeds the 32 MB limit.")
    limit = MAX_UPLOAD_BYTES if expected_size is None else expected_size
    try:
        content = await upload.read(limit + 1)
    except OSError as exc:
        raise PhotoProcessingError("Could not read the uploaded image.") from exc
    if len(content) > MAX_UPLOAD_BYTES:
        raise PhotoProcessingError("Uploaded image exceeds the 32 MB limit.")
    if expected_size is not None and len(content) != expected_size:
        raise PhotoProcessingError("Uploaded image did not match its submitted size.")
    return content


def _upload_mode(db: Session, mode: str | None) -> str:
    if mode is None:
        return photo_upload_mode(db)
    if mode not in (*PHOTO_FRAMING_MODES, "crop"):
        raise HTTPException(status_code=422, detail="Select Fit, Fill, or Intelligent Crop.")
    return mode


def _process_image(db: Session, *, filename: str, content: bytes, mode: str) -> dict[str, object]:
    return photo_to_dict(create_photo_from_bytes(db, filename=filename, content=content, mode=mode))


@router.get("/photos")
def list_photos(db: Session = Depends(get_db)) -> dict[str, object]:
    photos = db.scalars(select(Photo).order_by(Photo.created_at.desc(), Photo.id.desc())).all()
    return {"photos": [photo_to_dict(photo) for photo in photos]}


@router.post("/photos")
async def upload_photo(
    file: UploadFile = File(...),
    mode: str | None = Form(None),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    mode = await run_in_threadpool(_upload_mode, db, mode)
    try:
        return await run_in_threadpool(
            _process_image, db, filename=file.filename or "upload.jpg",
            content=await _read_image(file), mode=mode,
        )
    except PhotoProcessingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/photos/batch")
async def batch_upload(
    files: list[UploadFile] = File(default=[]),
    mode: str | None = Form(None),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    if len(files) > 50:
        raise HTTPException(status_code=422, detail="Batch uploads are limited to 50 images.")
    sizes: list[int | None] = []
    total = 0
    for upload in files:
        try:
            size = upload.size if upload.size is not None else await run_in_threadpool(_spooled_file_size, upload.file)
        except OSError:
            sizes.append(None)
            continue
        total += size
        if total > MAX_BATCH_BYTES:
            raise HTTPException(status_code=413, detail="Batch uploads are limited to 128 MB of images.")
        sizes.append(size)

    mode = await run_in_threadpool(_upload_mode, db, mode)
    created: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    for upload, size in zip(files, sizes, strict=True):
        try:
            if size is None:
                raise PhotoProcessingError("Could not read the uploaded image.")
            record = await run_in_threadpool(
                _process_image, db, filename=upload.filename or "upload.jpg",
                content=await _read_image(upload, size), mode=mode,
            )
            created.append(record)
        except PhotoProcessingError as exc:
            errors.append({"filename": upload.filename, "detail": str(exc)})
    return {"photos": created, "accepted": len(created), "errors": errors}


@router.post("/photos/placeholders")
def create_photo_placeholder(payload: dict[str, object], db: Session = Depends(get_db)) -> dict[str, object]:
    """Compatibility endpoint for API tests and simulator-only metadata creation."""
    name = str(payload.get("original_filename") or "pending-upload.jpg")
    photo = Photo(
        original_filename=name,
        stored_filename=name,
        original_path="",
        processed_status="pending",
        palette="waveshare_7color_6",
    )
    db.add(photo)
    db.commit()
    db.refresh(photo)
    return photo_to_dict(photo)


@router.get("/photos/{photo_id}")
def get_photo(photo_id: int, db: Session = Depends(get_db)) -> dict[str, object]:
    photo = db.get(Photo, photo_id)
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found.")
    return photo_to_dict(photo)


class PhotoFramingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: str
    x: float | None = None
    y: float | None = None
    zoom: float = 1.0
    revision: str = Field(pattern=r"^[0-9a-f]{24}$")


@router.get("/photos/{photo_id}/framing-preview")
def preview_photo_framing(
    photo_id: int,
    mode: str,
    x: float | None = None,
    y: float | None = None,
    zoom: float = 1.0,
    compare_saved: bool = False,
    db: Session = Depends(get_db),
) -> Response:
    photo = db.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found.")
    try:
        content, focus = photo_framing_preview(photo, PhotoFraming(mode, x, y, zoom))
    except (ValueError, OSError) as error:
        detail = str(error) if isinstance(error, ValueError) else "The original photo is unavailable."
        raise HTTPException(status_code=422, detail=detail) from error
    headers = {
        "Cache-Control": "private, no-store",
        "X-InkDaddy-Focus-X": str(focus[0]),
        "X-InkDaddy-Focus-Y": str(focus[1]),
    }
    if compare_saved:
        saved = read_photo_asset(photo.preview_path, "previews", limit=len(content))
        headers["X-InkDaddy-Preview-Changed"] = "true" if saved != content else "false"
    return Response(content=content, media_type="image/png", headers=headers)


@router.put("/photos/{photo_id}/framing")
def set_photo_framing(
    photo_id: int, payload: PhotoFramingUpdate, db: Session = Depends(get_db)
) -> dict[str, object]:
    photo = db.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found.")
    try:
        framing = PhotoFraming(payload.mode, payload.x, payload.y, payload.zoom)
        return photo_to_dict(update_photo_framing(db, photo, framing, revision=payload.revision))
    except PhotoEditConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.put("/photos/{photo_id}")
def update_photo(photo_id: int, payload: dict[str, object], db: Session = Depends(get_db)) -> dict[str, object]:
    photo = db.get(Photo, photo_id)
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found.")
    if "original_filename" in payload or "name" in payload:
        photo.original_filename = _name(payload.get("original_filename", payload.get("name")), max_length=512)
    db.commit()
    db.refresh(photo)
    return photo_to_dict(photo)


@router.delete("/photos/{photo_id}")
def delete_photo(photo_id: int, db: Session = Depends(get_db)) -> dict[str, object]:
    begin_playback_write(db)
    photo = db.get(Photo, photo_id)
    if photo:
        delete_photo_record(db, photo)
    return {"deleted": True}


def _photo_version(db: Session, photo_id: int, revision: str | None) -> Photo | PhotoRevision | None:
    photo = db.get(Photo, photo_id)
    if photo is None or revision is None or photo_revision(photo) == revision:
        return photo
    archived = db.scalars(select(PhotoRevision).where(PhotoRevision.photo_id == photo_id))
    return next((item for item in archived if photo_revision(item) == revision), None)


@router.get("/photos/{photo_id}/preview")
def photo_preview(photo_id: int, v: str | None = None, db: Session = Depends(get_db)) -> Response:
    photo = _photo_version(db, photo_id, v)
    if not photo or not photo.preview_path:
        raise HTTPException(status_code=404, detail="Photo preview not found.")
    content = read_photo_asset(photo.preview_path, "previews")
    if content is None:
        raise HTTPException(status_code=404, detail="Photo preview file not found.")
    return Response(content=content, media_type="image/png")


@router.get("/photos/{photo_id}/frame")
def photo_frame(photo_id: int, v: str | None = None, db: Session = Depends(get_db)) -> Response:
    photo = _photo_version(db, photo_id, v)
    if not photo or not photo.frame_path:
        raise HTTPException(status_code=404, detail="Photo frame not found.")
    content = read_photo_asset(photo.frame_path, "frames", limit=MAX_RAW_FRAME_BYTES)
    if content is None:
        raise HTTPException(status_code=404, detail="Photo frame file not found.")
    return Response(content=content, media_type="application/octet-stream")


@router.get("/albums")
def list_albums(db: Session = Depends(get_db)) -> dict[str, object]:
    albums = db.scalars(select(Album).order_by(Album.created_at.desc(), Album.id.desc())).all()
    return {"albums": [album_to_dict(db, album) for album in albums]}


@router.post("/albums")
def create_album(payload: dict[str, object], db: Session = Depends(get_db)) -> dict[str, object]:
    begin_playback_write(db)
    photo_ids = _album_photo_ids(db, payload["photo_ids"]) if "photo_ids" in payload else []
    mode = payload.get("mode", "ordered")
    album = Album(
        name=_name(payload.get("name", "New album")),
        mode=mode if mode in ("ordered", "shuffle", "random") else "ordered",
    )
    db.add(album)
    db.flush()
    set_album_items(db, album, photo_ids)
    return album_detail(db, album)


@router.get("/albums/{album_id}")
def get_album(album_id: int, db: Session = Depends(get_db)) -> dict[str, object]:
    album = db.get(Album, album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found.")
    return album_detail(db, album)


@router.put("/albums/{album_id}")
def update_album(album_id: int, payload: dict[str, object], db: Session = Depends(get_db)) -> dict[str, object]:
    begin_playback_write(db)
    album = db.get(Album, album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found.")
    if "name" in payload:
        album.name = _name(payload["name"])
    if "mode" in payload:
        mode = payload["mode"]
        album.mode = mode if mode in ("ordered", "shuffle", "random") else album.mode
    if "photo_ids" in payload:
        set_album_items(db, album, _album_photo_ids(db, payload["photo_ids"]))
    else:
        db.commit()
    return album_detail(db, album)


def _name(value: object, max_length: int = 255) -> str:
    if not isinstance(value, str) or not (name := value.strip()) or len(name) > max_length:
        raise HTTPException(status_code=422, detail=f"Enter a name between 1 and {max_length} characters.")
    return name


def _album_photo_ids(db: Session, values: object) -> list[int]:
    if not isinstance(values, list):
        raise HTTPException(status_code=422, detail="Choose an ordered list of photos.")
    try:
        photo_ids = list(dict.fromkeys(_photo_id(value) for value in values))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Choose valid photo IDs.") from exc
    if photo_ids:
        found = set(db.scalars(select(Photo.id).where(Photo.id.in_(photo_ids))))
        if found != set(photo_ids):
            raise HTTPException(status_code=409, detail="A selected photo is no longer available.")
    return photo_ids


def _photo_id(value: object) -> int:
    if type(value) is int or isinstance(value, str) and value.isascii() and value.isdigit():
        photo_id = int(value)
        if 0 < photo_id <= 2**63 - 1:
            return photo_id
    raise ValueError("Invalid photo ID.")


@router.delete("/albums/{album_id}")
def delete_album(album_id: int, db: Session = Depends(get_db)) -> dict[str, object]:
    album = db.get(Album, album_id)
    if album:
        db.execute(delete(AlbumItem).where(AlbumItem.album_id == album.id))
        db.delete(album)
        db.commit()
    return {"deleted": True}
