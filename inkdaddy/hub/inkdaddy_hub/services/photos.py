from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from io import BytesIO
from os import fstat
from pathlib import Path
from struct import iter_unpack
from typing import BinaryIO

from PIL import Image, ImageCms, ImageOps, PngImagePlugin
from sqlalchemy import delete, func, select, union_all
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import HubSettings, get_settings
from ..models import Album, AlbumItem, Frame, Photo, PhotoRevision
from .asset_storage import asset_names, open_asset, read_asset, remove_asset, write_asset
from .palette import WAVESHARE_7COLOR_6, raw_photo_frame
from .photo_framing import PhotoFraming, frame_photo

LOGGER = logging.getLogger(__name__)
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
ALLOWED_RASTER_FORMATS = ("JPEG", "PNG", "WEBP", "BMP")
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_BATCH_BYTES = 128 * 1024 * 1024
MAX_IMAGE_DIMENSION = 16_384
MAX_IMAGE_PIXELS = 50_000_000
MAX_ICC_PROFILE_BYTES = 1024 * 1024
_RGB_PHOTO_MODES = {"RGB", "RGBA", "RGBX", "P", "PA"}
_RGB_ICC_CLASSES = {b"scnr", b"mntr", b"prtr", b"spac"}
_PHOTO_DERIVATIVE_STEM = re.compile(r"photo-[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}")
_ORPHAN_DERIVATIVE_AGE = timedelta(days=1)


class PhotoProcessingError(ValueError):
    pass


class PhotoEditConflict(PhotoProcessingError):
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
    revision = photo_revision(photo)
    framing = json.loads(photo.framing_json) if photo.framing_json else None
    return {
        "id": str(photo.id),
        "original_filename": photo.original_filename,
        "stored_filename": photo.stored_filename,
        "processed_status": photo.processed_status,
        "width": photo.width,
        "height": photo.height,
        "palette": photo.palette,
        "checksum": photo.checksum,
        "preview_url": f"/api/photos/{photo.id}/preview?v={revision}" if photo.preview_path else None,
        "frame_url": f"/api/photos/{photo.id}/frame?v={revision}" if photo.frame_path else None,
        "framing": framing,
        "revision": revision,
        "can_edit_framing": bool(revision and photo.original_path and photo.processed_status == "ready"),
        "uploaded_at": photo.created_at.isoformat() if photo.created_at else None,
        "updated_at": photo.updated_at.isoformat() if photo.updated_at else None,
    }


def photo_revision(photo: Photo | PhotoRevision) -> str | None:
    return hashlib.sha256(photo.processed_path.encode()).hexdigest()[:24] if photo.processed_path else None


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
        framing = PhotoFraming("fill" if mode == "crop" else mode)
        processed = process_photo_file(stem, original_path, framing=framing, settings=settings)
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
            framing_json=json.dumps(asdict(framing)),
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
    source: Path | BinaryIO,
    *,
    mode: str = "fit",
    framing: PhotoFraming | None = None,
    settings: HubSettings | None = None,
) -> ProcessedPhotoFiles:
    settings = settings or get_settings()
    framing = framing or PhotoFraming("fill" if mode == "crop" else mode)
    processed, _focus = _render_framed_photo(source, framing, settings)
    processed_path, preview_path, frame_path = _processed_paths(settings, photo_id)
    png = _photo_png(processed)
    raw, checksum = raw_photo_frame(processed)
    write_asset(settings.data_dir, "processed", processed_path, png)
    write_asset(settings.data_dir, "previews", preview_path, png)
    write_asset(settings.data_dir, "frames", frame_path, raw)
    return ProcessedPhotoFiles(
        processed_path=processed_path,
        preview_path=preview_path,
        frame_path=frame_path,
        width=processed.width,
        height=processed.height,
        checksum=checksum,
        palette=WAVESHARE_7COLOR_6.name,
    )


def _render_framed_photo(
    source: Path | BinaryIO, framing: PhotoFraming, settings: HubSettings
) -> tuple[Image.Image, tuple[float, float]]:
    image = _decode_photo(source, native_mode=True)
    profile = _embedded_rgb_profile(image)
    if image.mode != "RGB":
        image = image.convert("RGB")
    width, height = settings.default_width, settings.default_height
    size = (width, height)
    framed, focus = frame_photo(image, size, framing)
    processed = _normalize_rgb(framed, profile)
    if processed.size != size:
        # Match ImageOps.pad's center placement, adding the white border in sRGB.
        canvas = Image.new("RGB", size, "white")
        offset = (round((width - processed.width) / 2), round((height - processed.height) / 2))
        canvas.paste(processed, offset)
        processed = canvas
    return processed, focus


def _photo_png(processed: Image.Image) -> bytes:
    # The standard PNG marker also distinguishes new files from legacy padded
    # PNGs whose source profile was lost and can be recovered from the original.
    png_info = PngImagePlugin.PngInfo()
    png_info.add(b"sRGB", bytes([ImageCms.Intent.RELATIVE_COLORIMETRIC]))
    with BytesIO() as output:
        processed.save(output, format="PNG", pnginfo=png_info)
        return output.getvalue()


def photo_framing_preview(photo: Photo, framing: PhotoFraming) -> tuple[bytes, tuple[float, float]]:
    settings = get_settings()
    with open_asset(settings.data_dir, "originals", photo.original_path) as source:
        if source is None or not 0 < fstat(source.fileno()).st_size <= MAX_UPLOAD_BYTES:
            raise PhotoProcessingError("The original photo is unavailable.")
        image, focus = _render_framed_photo(source, framing, settings)
    return _photo_png(image), focus


def update_photo_framing(
    db: Session, photo: Photo, framing: PhotoFraming, *, revision: str
) -> Photo:
    from .playback import begin_playback_write

    if photo_revision(photo) != revision:
        raise PhotoEditConflict("This photo changed. Reopen the editor to load its current framing.")
    if not photo.original_path or photo.processed_status != "ready":
        raise PhotoProcessingError("The original photo is unavailable.")

    settings = get_settings()
    stem = uuid.uuid4().hex
    original_path = photo.original_path
    photo_id = photo.id
    try:
        with open_asset(settings.data_dir, "originals", original_path) as source:
            if source is None or not 0 < fstat(source.fileno()).st_size <= MAX_UPLOAD_BYTES:
                raise PhotoProcessingError("The original photo is unavailable.")
            processed = process_photo_file(stem, source, framing=framing, settings=settings)
        begin_playback_write(db)
        current = db.get(Photo, photo_id, populate_existing=True)
        if current is None or photo_revision(current) != revision or current.original_path != original_path:
            raise PhotoEditConflict("This photo changed. Reopen the editor to load its current framing.")
        db.add(PhotoRevision(
            photo_id=current.id,
            processed_path=current.processed_path,
            preview_path=current.preview_path,
            frame_path=current.frame_path,
            checksum=current.checksum,
            framing_json=current.framing_json,
        ))
        current.processed_path = str(processed.processed_path)
        current.preview_path = str(processed.preview_path)
        current.frame_path = str(processed.frame_path)
        current.width, current.height = processed.width, processed.height
        current.palette, current.checksum = processed.palette, processed.checksum
        current.framing_json = json.dumps(asdict(framing))
        db.commit()
    except Exception as error:
        try:
            db.rollback()
        finally:
            paths = _processed_paths(settings, stem)
            for directory, path in zip(("processed", "previews", "frames"), paths, strict=True):
                try:
                    remove_asset(settings.data_dir, directory, path)
                except OSError:
                    LOGGER.warning("Could not remove an asset from a failed framing edit.")
        if isinstance(error, (OSError, SQLAlchemyError)):
            raise PhotoProcessingError("Could not save this photo framing.") from error
        raise
    return current


def _decode_photo(source: Path | BinaryIO, *, native_mode: bool = False) -> Image.Image:
    try:
        with Image.open(source, formats=ALLOWED_RASTER_FORMATS) as opened:
            source_width, source_height = opened.size
            if (
                not 0 < source_width <= MAX_IMAGE_DIMENSION
                or not 0 < source_height <= MAX_IMAGE_DIMENSION
                or source_width * source_height > MAX_IMAGE_PIXELS
            ):
                raise PhotoProcessingError("Uploaded image dimensions exceed the supported limit.")
            image = ImageOps.exif_transpose(opened)
            return image if native_mode else image.convert("RGB")
    except PhotoProcessingError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise PhotoProcessingError("Uploaded image dimensions exceed the supported limit.") from exc
    except (OSError, SyntaxError, ValueError) as exc:
        raise PhotoProcessingError("Uploaded file is not a supported image.") from exc


def _embedded_rgb_profile(image: Image.Image) -> bytes | None:
    profile = image.info.get("icc_profile")
    if (
        image.mode in _RGB_PHOTO_MODES
        and isinstance(profile, bytes)
        and 0 < len(profile) <= MAX_ICC_PROFILE_BYTES
    ):
        return profile
    return None


@lru_cache(maxsize=1)
def _srgb_profile() -> ImageCms.ImageCmsProfile:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))


def _valid_rgb_icc(profile: bytes) -> bool:
    # ICC profiles use a 128-byte header followed by a tag count and tag table.
    length = len(profile)
    if (
        not 132 <= length <= MAX_ICC_PROFILE_BYTES
        or int.from_bytes(profile[:4], "big") != length
        or profile[12:16] not in _RGB_ICC_CLASSES
        or profile[16:20] != b"RGB "
        or profile[20:24] not in (b"XYZ ", b"Lab ")
        or profile[36:40] != b"acsp"
    ):
        return False
    count = int.from_bytes(profile[128:132], "big")
    table_end = 132 + count * 12
    if count == 0 or table_end > length:
        return False
    tags = iter_unpack(">4sII", memoryview(profile)[132:table_end])
    return all(
        offset % 4 == 0 and offset >= table_end and size >= 8 and offset + size <= length
        for _name, offset, size in tags
    )


def _normalize_rgb(image: Image.Image, profile: bytes | None) -> Image.Image:
    if profile and min(image.size) > 0 and _valid_rgb_icc(profile):
        try:
            source = ImageCms.getOpenProfile(BytesIO(profile))
            if source.profile.xcolor_space.strip() == "RGB":
                normalized = ImageCms.profileToProfile(
                    image,
                    source,
                    _srgb_profile(),
                    renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
                    outputMode="RGB",
                    flags=ImageCms.Flags.BLACKPOINTCOMPENSATION,
                )
                if normalized is not None:
                    image = normalized
        except (ImageCms.PyCMSError, ImportError):
            # Keep the RGB samples if the embedded profile is invalid or unavailable.
            pass
    # Derivatives keep pixels only. The original retains its original metadata.
    image.info.clear()
    return image


def _original_rgb_profile(data_dir: Path, value: str | None) -> bytes | None:
    try:
        with open_asset(data_dir, "originals", value) as asset:
            if asset is None or not 0 < fstat(asset.fileno()).st_size <= MAX_UPLOAD_BYTES:
                return None
            with Image.open(asset, formats=ALLOWED_RASTER_FORMATS) as original:
                return _embedded_rgb_profile(original)
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None


def load_processed_photo(data_dir: Path, value: str | None) -> Image.Image | None:
    raw = read_asset(data_dir, "processed", value, MAX_UPLOAD_BYTES)
    if not raw:
        return None
    try:
        return _decode_photo(BytesIO(raw))
    except PhotoProcessingError:
        return None


def load_color_managed_photo(
    data_dir: Path, processed_path: str | None, original_path: str | None
) -> Image.Image | None:
    """Normalize a saved raster while retaining its existing framing."""
    image = load_processed_photo(data_dir, processed_path)
    if image is None:
        return None
    profile = None
    if "srgb" not in image.info:
        if "icc_profile" in image.info:
            profile = _embedded_rgb_profile(image)
        else:
            profile = _original_rgb_profile(data_dir, original_path)
    return _normalize_rgb(image, profile)


def read_photo_asset(value: str | None, directory: str, *, limit: int = MAX_UPLOAD_BYTES) -> bytes | None:
    return read_asset(get_settings().data_dir, directory, value, limit)


def sweep_untracked_photo_derivatives(db: Session, data_dir: Path, *, now: datetime) -> None:
    """Collect aged generated derivatives during the hourly playback writer transaction."""
    candidates = []
    for directory, extension in (("processed", ".png"), ("previews", ".png"), ("frames", ".raw")):
        try:
            names = asset_names(data_dir, directory)
        except OSError:
            LOGGER.warning("Could not list generated photo assets for cleanup.")
            continue
        candidates.extend(
            (directory, name) for name in names
            if name.endswith(extension) and _PHOTO_DERIVATIVE_STEM.fullmatch(name[:-len(extension)])
        )
    if not candidates:
        return
    paths = (
        Photo.processed_path, Photo.preview_path, Photo.frame_path, Photo.original_path,
        PhotoRevision.processed_path, PhotoRevision.preview_path, PhotoRevision.frame_path, Frame.path,
    )
    # Protect a referenced name even when an old row spells the data path differently.
    referenced = {
        Path(path).name for path in db.scalars(union_all(*(select(column) for column in paths))) if path
    }
    cutoff = (now - _ORPHAN_DERIVATIVE_AGE).timestamp()
    for directory, name in candidates:
        if name in referenced:
            continue
        try:
            remove_asset(data_dir, directory, data_dir / directory / name, older_than=cutoff)
        except OSError:
            LOGGER.warning("Could not remove an untracked generated photo derivative.")


def delete_photo_record(db: Session, photo: Photo) -> None:
    data_dir = get_settings().data_dir
    revisions = db.scalars(select(PhotoRevision).where(PhotoRevision.photo_id == photo.id)).all()
    assets = [("originals", photo.original_path)]
    for item in (photo, *revisions):
        assets.extend((
            ("processed", item.processed_path),
            ("previews", item.preview_path),
            ("frames", item.frame_path),
        ))
    db.execute(delete(PhotoRevision).where(PhotoRevision.photo_id == photo.id))
    db.execute(delete(AlbumItem).where(AlbumItem.photo_id == photo.id))
    db.delete(photo)
    db.commit()
    for directory, value in assets:
        try:
            remove_asset(data_dir, directory, value)
        except OSError:
            LOGGER.warning("Could not remove a deleted photo asset.")


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
