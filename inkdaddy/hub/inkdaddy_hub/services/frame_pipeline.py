from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AlbumItem, Device, Frame, Photo
from .asset_storage import asset_stat, write_asset
from .dashboard_content import dashboard_config, get_dashboard_record, render_dashboard, resolve_photo_overlay, resolve_widget_text
from .matter_onboarding import default_matter_onboarding_codes
from .palette import WAVESHARE_7COLOR_6, raw_frame_from_image
from .photos import load_color_managed_photo
from .provisioning import DisplayContentKind, ProvisioningState, next_display_content_kind
from .rendering import FrameManifest, build_frame_manifest, render_matter_join_screen


@dataclass(frozen=True)
class PreparedDeviceFrame:
    manifest: FrameManifest
    content_kind: DisplayContentKind
    source_id: str
    raw_path: Path


def prepare_device_frame(db: Session, device: Device, *, refresh_count: int = 0) -> PreparedDeviceFrame:
    image, content_kind, source_id = render_device_image(db, device, refresh_count=refresh_count)
    _sync_resolved_source(device, source_id)
    raw, _checksum = raw_frame_from_image(image, WAVESHARE_7COLOR_6)
    manifest = build_frame_manifest(
        raw,
        width=image.width,
        height=image.height,
        source_id=source_id,
        download_url=f"/api/devices/{device.device_uid}/frame",
    )
    frame_path = _store_frame_bytes(db, device, manifest, raw)
    device.last_frame_id = manifest.frame_id
    db.commit()
    return PreparedDeviceFrame(
        manifest=manifest,
        content_kind=content_kind,
        source_id=source_id,
        raw_path=frame_path,
    )


def render_device_image(db: Session, device: Device, *, refresh_count: int) -> tuple[Image.Image, DisplayContentKind, str]:
    content_count = cycleable_content_count(db, device)
    state = ProvisioningState(
        commissioned=device.provisioned,
        mesh_connected=device.mesh_connected,
        cycleable_content_count=content_count,
        refresh_count=refresh_count,
    )
    content_kind = next_display_content_kind(state)
    if content_kind == DisplayContentKind.MATTER_JOIN_SCREEN:
        codes = default_matter_onboarding_codes()
        return (
            render_matter_join_screen(
                qr_payload=codes.qr_payload,
                setup_pin=f"{codes.setup_pin:08d}",
                manual_code=codes.manual_code,
                discriminator=str(codes.discriminator),
            ),
            content_kind,
            "matter-join",
        )
    photo = None
    if device.selected_source_type == "photo":
        photo = selected_photo(db, device)
    elif device.selected_source_type == "album":
        photo = selected_album_photo(db, device)
    if photo:
        image = load_color_managed_photo(get_settings().data_dir, photo.processed_path, photo.original_path)
        if image is not None:
            overlay = resolve_photo_overlay(db, device.overlay_dashboard_id, datetime.now(timezone.utc), device,
                                            time_kind="manual", photo=photo)
            return overlay.render(image) if overlay else image, content_kind, f"photo-{photo.id}"
    dashboard = get_dashboard_record(db, device.selected_source_id) if device.selected_source_type == "dashboard" else None
    config = dashboard_config(dashboard)
    widgets = resolve_widget_text(db, config, datetime.now(timezone.utc), device)
    return render_dashboard(config, widgets), content_kind, dashboard.slug if dashboard else "dashboard-sample"


def cycleable_content_count(db: Session, device: Device) -> int:
    if device.selected_source_type == "photo":
        return 1 if selected_photo(db, device) else 0
    if device.selected_source_type == "album" and device.selected_source_id and device.selected_source_id.isdigit():
        return int(
            db.scalar(select(func.count()).select_from(AlbumItem).where(AlbumItem.album_id == int(device.selected_source_id)))
            or 0
        )
    return 1 if device.selected_source_type == "dashboard" else 0


def selected_photo(db: Session, device: Device) -> Photo | None:
    if device.selected_source_id and device.selected_source_id.isdigit():
        photo = db.get(Photo, int(device.selected_source_id))
        if photo and photo.processed_status == "ready":
            return photo
    return db.execute(
        select(Photo)
        .where(Photo.processed_status == "ready")
        .order_by(Photo.created_at.desc(), Photo.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def selected_album_photo(db: Session, device: Device) -> Photo | None:
    if not device.selected_source_id or not device.selected_source_id.isdigit():
        return None
    return db.execute(
        select(Photo)
        .join(AlbumItem, AlbumItem.photo_id == Photo.id)
        .where(AlbumItem.album_id == int(device.selected_source_id), Photo.processed_status == "ready")
        .order_by(AlbumItem.sort_order)
        .limit(1)
    ).scalar_one_or_none()


def _store_frame_bytes(db: Session, device: Device, manifest: FrameManifest, raw: bytes) -> Path:
    settings = get_settings()
    frame_path = settings.data_dir / "frames" / f"device-{device.id}-{manifest.frame_id}.raw"
    frame_record = db.scalar(select(Frame).where(Frame.frame_id == manifest.frame_id))
    if frame_record:
        frame_path = Path(frame_record.path)
        if asset_stat(settings.data_dir, "frames", frame_path) is None:
            if frame_record.sha256 != manifest.sha256 or frame_record.size_bytes != len(raw):
                raise ValueError("The generated frame does not match its stored metadata.")
            write_asset(settings.data_dir, "frames", frame_path, raw)
    else:
        write_asset(settings.data_dir, "frames", frame_path, raw)
        db.add(
            Frame(
                frame_id=manifest.frame_id,
                source_type="device",
                source_id=device.device_uid,
                width=manifest.width,
                height=manifest.height,
                palette=manifest.palette,
                bits_per_pixel=manifest.bits_per_pixel,
                packing=manifest.packing,
                byte_order=manifest.byte_order,
                size_bytes=manifest.size_bytes,
                sha256=manifest.sha256,
                content_type=manifest.content_type,
                path=str(frame_path),
            )
        )
    return frame_path


def _sync_resolved_source(device: Device, source_id: str) -> None:
    if device.selected_source_type == "photo" and source_id.startswith("photo-"):
        resolved_id = source_id.removeprefix("photo-")
        if resolved_id.isdigit() and device.selected_source_id != resolved_id:
            device.selected_source_id = resolved_id
