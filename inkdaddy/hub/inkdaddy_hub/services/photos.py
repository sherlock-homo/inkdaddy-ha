from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import HubSettings, get_settings
from ..models import Album, AlbumItem, Photo
from .asset_storage import read_asset, remove_asset
from .palette import WAVESHARE_7COLOR_6, raw_frame_from_image
from .rendering import render_preview_png_bytes

LOGGER = logging.getLogger(__name__)
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
ALLOWED_RASTER_FORMATS = ("JPEG", "PNG", "WEBP", "BMP")
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_BATCH_BYTES = 128 * 1024 * 1024
MAX_IMAGE_DIMENSION = 16_384
MAX_IMAGE_PIXELS = 50_000_000


class PhotoProcessingError(ValueError):
    pass


@dataclass(frozen=True)
class ProcessedPhotoFiles:
    processed_path: Path
    preview_path: Path
    frame_path: Path
    width: int
    height: int
    checksum: str
    palette: str


def sanitize_filename(filename: str) -> str:
    name = Path(filename or "upload.jpg").name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).stem).strip(".-") or "photo"
    suffix = Path(name).suffix.lower() or ".jpg"
    if suffix not in ALLOWED_EXTENSIONS:
        raise PhotoProcessingError(f"Unsupported image type {suffix!r}.")
    return f"{stem[:96]}{suffix}"


def photo_to_dict(photo: Photo) -> dict[str, object]:
    return {
        "id": str(photo.id),
        "original_filename": photo.original_filename,
        "stored_filename": photo.stored_filename,
        "processed_status": photo.processed_status,
        "width": photo.width,
        "height": photo.height,
        "palette": photo.palette,
        "checksum": photo.checksum,
        "preview_url": f"/api/photos/{photo.id}/preview" if photo.preview_path else None,
        "frame_url": f"/api/photos/{photo.id}/frame" if photo.frame_path else None,
        "uploaded_at": photo.created_at.isoformat() if photo.created_at else None,
        "updated_at": photo.updated_at.isoformat() if photo.updated_at else None,
    }


def album_to_dict(db: Session, album: Album) -> dict[str, object]:
    item_count = db.scalar(select(func.count()).select_from(AlbumItem).where(AlbumItem.album_id == album.id)) or 0
    return {
        "id": str(album.id),
        "name": album.name,
        "mode": album.mode,
        "item_count": item_count,
        "created_at": album.created_at.isoformat() if album.created_at else None,
        "updated_at": album.updated_at.isoformat() if album.updated_at else None,
    }


def create_photo_from_bytes(
    db: Session,
    *,
    filename: str,
    content: bytes,
    mode: str = "fit",
    settings: HubSettings | None = None,
) -> Photo:
    if not content:
        raise PhotoProcessingError("Uploaded image is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise PhotoProcessingError("Uploaded image exceeds the 32 MB limit.")

    settings = settings or get_settings()
    safe_name = sanitize_filename(filename)
    stem = uuid.uuid4().hex
    original_path = settings.data_dir / "originals" / f"{stem}-{safe_name}"
    asset_paths = (original_path, *_processed_paths(settings, stem))
    try:
        original_path.parent.mkdir(parents=True, exist_ok=True)
        original_path.write_bytes(content)
        processed = process_photo_file(stem, original_path, mode=mode, settings=settings)
        photo = Photo(
            original_filename=safe_name,
            stored_filename=original_path.name,
            original_path=str(original_path),
            processed_path=str(processed.processed_path),
            preview_path=str(processed.preview_path),
            frame_path=str(processed.frame_path),
            width=processed.width,
            height=processed.height,
            palette=processed.palette,
            checksum=processed.checksum,
            processed_status="ready",
        )
        db.add(photo)
        db.commit()
    except Exception as error:
        try:
            db.rollback()
        finally:
            for path in asset_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    LOGGER.warning("Could not remove an asset from a failed photo upload.")
        if isinstance(error, (OSError, SQLAlchemyError)):
            raise PhotoProcessingError("Could not store the uploaded photo.") from error
        raise
    return photo


def _processed_paths(settings: HubSettings, photo_id: int | str) -> tuple[Path, Path, Path]:
    return (
        settings.data_dir / "processed" / f"photo-{photo_id}.png",
        settings.data_dir / "previews" / f"photo-{photo_id}.png",
        settings.data_dir / "frames" / f"photo-{photo_id}.raw",
    )


def process_photo_file(
    photo_id: int | str,
    original_path: Path,
    *,
    mode: str = "fit",
    settings: HubSettings | None = None,
) -> ProcessedPhotoFiles:
    settings = settings or get_settings()
    width = settings.default_width
    height = settings.default_height
    mode = mode if mode in {"fit", "fill", "crop"} else "fit"

    image = _decode_photo(original_path)

    if mode == "fill":
        processed = ImageOps.fit(image, (width, height), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    elif mode == "crop":
        processed = ImageOps.fit(image, (width, height), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    else:
        processed = ImageOps.pad(
            image,
            (width, height),
            method=Image.Resampling.LANCZOS,
            color=(255, 255, 255),
            centering=(0.5, 0.5),
        )

    processed_path, preview_path, frame_path = _processed_paths(settings, photo_id)
    for path in (processed_path, preview_path, frame_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    processed.save(processed_path, format="PNG")
    preview_path.write_bytes(render_preview_png_bytes(processed))
    raw, checksum = raw_frame_from_image(processed, WAVESHARE_7COLOR_6)
    frame_path.write_bytes(raw)
    return ProcessedPhotoFiles(
        processed_path=processed_path,
        preview_path=preview_path,
        frame_path=frame_path,
        width=width,
        height=height,
        checksum=checksum,
        palette=WAVESHARE_7COLOR_6.name,
    )


def _decode_photo(source: Path | BinaryIO) -> Image.Image:
    try:
        with Image.open(source, formats=ALLOWED_RASTER_FORMATS) as opened:
            source_width, source_height = opened.size
            if (
                not 0 < source_width <= MAX_IMAGE_DIMENSION
                or not 0 < source_height <= MAX_IMAGE_DIMENSION
                or source_width * source_height > MAX_IMAGE_PIXELS
            ):
                raise PhotoProcessingError("Uploaded image dimensions exceed the supported limit.")
            return ImageOps.exif_transpose(opened).convert("RGB")
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise PhotoProcessingError("Uploaded image dimensions exceed the supported limit.") from exc
    except (OSError, SyntaxError) as exc:
        raise PhotoProcessingError("Uploaded file is not a supported image.") from exc


def load_processed_photo(data_dir: Path, value: str | None) -> Image.Image | None:
    raw = read_asset(data_dir, "processed", value, MAX_UPLOAD_BYTES)
    if not raw:
        return None
    try:
        return _decode_photo(BytesIO(raw))
    except PhotoProcessingError:
        return None


def read_photo_asset(value: str | None, directory: str, *, limit: int = MAX_UPLOAD_BYTES) -> bytes | None:
    return read_asset(get_settings().data_dir, directory, value, limit)


def delete_photo_record(db: Session, photo: Photo) -> None:
    data_dir = get_settings().data_dir
    for directory, value in (
        ("originals", photo.original_path),
        ("processed", photo.processed_path),
        ("previews", photo.preview_path),
        ("frames", photo.frame_path),
    ):
        remove_asset(data_dir, directory, value)
    db.execute(delete(AlbumItem).where(AlbumItem.photo_id == photo.id))
    db.delete(photo)
    db.commit()


def create_album(db: Session, *, name: str, mode: str = "ordered") -> Album:
    mode = mode if mode in {"ordered", "shuffle", "random"} else "ordered"
    album = Album(name=name.strip() or "New album", mode=mode)
    db.add(album)
    db.commit()
    db.refresh(album)
    return album


def set_album_items(db: Session, album: Album, photo_ids: list[int]) -> list[AlbumItem]:
    existing = {photo.id for photo in db.scalars(select(Photo).where(Photo.id.in_(photo_ids))).all()}
    db.execute(delete(AlbumItem).where(AlbumItem.album_id == album.id))
    items: list[AlbumItem] = []
    for index, photo_id in enumerate(photo_ids):
        if photo_id in existing:
            item = AlbumItem(album_id=album.id, photo_id=photo_id, sort_order=index)
            db.add(item)
            items.append(item)
    db.commit()
    return items


def album_detail(db: Session, album: Album) -> dict[str, object]:
    rows = db.execute(
        select(AlbumItem, Photo)
        .join(Photo, Photo.id == AlbumItem.photo_id)
        .where(AlbumItem.album_id == album.id)
        .order_by(AlbumItem.sort_order)
    ).all()
    payload = album_to_dict(db, album)
    payload["photos"] = [photo_to_dict(photo) | {"sort_order": item.sort_order} for item, photo in rows]
    return payload
