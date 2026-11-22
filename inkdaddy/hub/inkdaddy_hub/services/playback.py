from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from PIL import Image
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Album,
    AlbumItem,
    Device,
    DeviceStatusHistory,
    Frame,
    MatterFrameTransfer,
    MatterPlaybackIntent,
    Photo,
    Playback,
    PlaybackOffer,
    PlaybackSlot,
)
from .asset_storage import asset_stat, read_asset, remove_asset, write_asset
from .dashboard_content import (
    dashboard_config,
    get_dashboard_record,
    render_dashboard,
    resolve_widget_text,
)
from .palette import PHOTO_DITHER_VERSION, WAVESHARE_7COLOR_6, raw_frame_from_image, raw_photo_frame
from .photos import load_processed_photo
from .rendering import build_frame_manifest

UPCOMING_SLOTS = 5
MAX_RAW_FRAME_BYTES = 800 * 480 // 2
ACTION_NAMES = {"next_content", "previous_content", "refresh_current", "pause", "play"}
SUCCESS_RESULTS = {"displayed_hub_frame", "ok", "refresh:ok"}
OFFER_RETENTION = timedelta(hours=1)
MATTER_FRAME_FAILED = "matter_frame_failed"


@dataclass(frozen=True)
class Source:
    type: str
    id: str


def begin_playback_write(db: Session) -> None:
    """Take the SQLite writer lock before recalculating shared playback state."""
    connection = db.connection()
    raw = connection.connection.driver_connection
    if isinstance(raw, sqlite3.Connection) and not raw.in_transaction:
        connection.execute(text("BEGIN IMMEDIATE"))


def _hour(now: datetime) -> int:
    return int(now.timestamp()) // 3600


def _hour_time(hour: int) -> datetime:
    return datetime.fromtimestamp(hour * 3600, timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _get_playback(db: Session, device: Device, hour: int) -> Playback:
    playback = db.get(Playback, device.id)
    if playback is None:
        playback = Playback(
            device_id=device.id,
            anchor_hour=hour,
            anchor_position=0,
            paused=False,
            playlist_json="[]",
        )
        db.add(playback)
        db.flush()
    return playback


def _playlist(playback: Playback | None) -> list[Source]:
    if not playback:
        return []
    return [Source(**row) for row in json.loads(playback.playlist_json)]


def playlist_uses_dashboard(db: Session, dashboard_id: str) -> bool:
    return any(
        Source("dashboard", dashboard_id) in _playlist(playback)
        for playback in db.scalars(select(Playback))
    )


def _ready_photo(db: Session, photo_id: str) -> Photo | None:
    row = db.get(Photo, int(photo_id)) if photo_id.isdigit() else None
    if not row or row.processed_status != "ready" or not row.processed_path:
        return None
    return row if asset_stat(get_settings().data_dir, "processed", row.processed_path) else None


def _expand_playback(db: Session, device: Device, playback: Playback) -> list[Source]:
    explicit = _playlist(playback)
    if explicit:
        selected = explicit
    elif device.selected_source_type == "photo":
        rows = db.scalars(
            select(Photo.id).where(Photo.processed_status == "ready").order_by(Photo.id)
        ).all()
        if (
            device.selected_source_id
            and device.selected_source_id.isdigit()
            and int(device.selected_source_id) in rows
        ):
            index = rows.index(int(device.selected_source_id))
            rows = rows[index:] + rows[:index]
        selected = [Source("photo", str(photo_id)) for photo_id in rows]
    else:
        selected = [Source(device.selected_source_type, device.selected_source_id or "sample")]

    result: list[Source] = []
    for source in selected:
        if source.type == "album" and source.id.isdigit():
            photo_ids = db.scalars(
                select(AlbumItem.photo_id)
                .where(AlbumItem.album_id == int(source.id))
                .order_by(AlbumItem.sort_order, AlbumItem.id)
            )
            result.extend(
                Source("photo", str(photo_id))
                for photo_id in photo_ids
                if _ready_photo(db, str(photo_id))
            )
        elif source.type == "photo" and _ready_photo(db, source.id):
            result.append(source)
        elif source.type == "dashboard" and (
            source.id == "sample" or get_dashboard_record(db, source.id)
        ):
            result.append(source)
    return result or [Source("dashboard", "sample")]


def update_playlist(
    db: Session, device: Device, items: object, now: datetime | None = None
) -> None:
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError("Enter a playlist with up to 100 items.")
    sources: list[Source] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("Each playlist item must have a type and an ID.")
        source = Source(str(item.get("type")), item["id"])
        if source.type == "dashboard":
            exists = source.id == "sample" or get_dashboard_record(db, source.id) is not None
        elif source.type == "photo":
            exists = _ready_photo(db, source.id) is not None
        elif source.type == "album":
            exists = source.id.isdigit() and db.get(Album, int(source.id)) is not None
        else:
            exists = False
        if not exists:
            raise ValueError("A selected playlist item no longer exists.")
        sources.append(source)
    hour = _hour(now or _utc_now())
    playback = _get_playback(db, device, hour)
    playback.playlist_json = json.dumps([asdict(source) for source in sources])
    playback.anchor_hour = hour
    playback.anchor_position = 0
    _invalidate_slot(db, device, hour)
    db.flush()


def _position(playback: Playback, hour: int) -> int:
    return playback.anchor_position + (
        0 if playback.paused else max(0, hour - playback.anchor_hour)
    )


def apply_playback_action(
    db: Session, device: Device, action: str, now: datetime | None = None
) -> None:
    hour = _hour(now or _utc_now())
    playback = _get_playback(db, device, hour)
    if action not in ACTION_NAMES:
        raise ValueError("Unsupported display action.")
    if action == "refresh_current":
        _invalidate_slot(db, device, playback.anchor_hour if playback.paused else hour)
        invalidate_matter_transfer(db, device.id)
        return
    if (action == "pause" and playback.paused) or (action == "play" and not playback.paused):
        return
    offset = {"next_content": 1, "previous_content": -1}.get(action, 0)
    playback.anchor_position = _position(playback, hour) + offset
    playback.anchor_hour = hour
    if action in {"pause", "play"}:
        playback.paused = action == "pause"
    if offset:
        _invalidate_slot(db, device, hour)


def reset_selection(db: Session, device: Device, now: datetime | None = None) -> None:
    playback = db.get(Playback, device.id)
    if playback and not _playlist(playback):
        playback.anchor_hour = _hour(now or _utc_now())
        playback.anchor_position = 0
        _invalidate_slot(db, device, playback.anchor_hour)


def _invalidate_slot(db: Session, device: Device, hour: int) -> None:
    db.execute(
        delete(PlaybackSlot).where(PlaybackSlot.device_id == device.id, PlaybackSlot.hour == hour)
    )


def invalidate_matter_transfer(db: Session, device_id: int) -> None:
    db.execute(delete(MatterFrameTransfer).where(MatterFrameTransfer.device_id == device_id))


def refresh_playback_slots(
    db: Session, device: Device, *, now: datetime | None = None, upcoming: bool = True
) -> PlaybackSlot:
    """Prepare the due frame and up to five future frames. Call inside a write transaction."""
    hour = _hour(now or _utc_now())
    playback = _get_playback(db, device, hour)
    sources = _expand_playback(db, device, playback)
    position = _position(playback, hour)
    current_hour = playback.anchor_hour if playback.paused else hour
    current = db.get(PlaybackSlot, (device.id, current_hour))
    if not current or not _slot_already_offered(db, current):
        current = _prepare_slot(db, device, current_hour, sources[position % len(sources)], observed_at=now)
    if upcoming:
        for offset in range(1, UPCOMING_SLOTS + 1):
            next_hour = hour + offset
            _prepare_slot(db, device, next_hour, sources[(position + offset) % len(sources)], observed_at=now)
        preserve_current = PlaybackSlot.hour != current_hour
        skipped_hour = PlaybackSlot.hour <= hour
        skipped_future = PlaybackSlot.hour > hour + UPCOMING_SLOTS
        db.execute(
            delete(PlaybackSlot).where(
                PlaybackSlot.device_id == device.id, preserve_current, skipped_hour | skipped_future
            )
        )
    return current


def _slot_already_offered(db: Session, slot: PlaybackSlot) -> bool:
    offered = db.scalar(
        select(PlaybackOffer.id)
        .where(
            PlaybackOffer.device_id == slot.device_id,
            PlaybackOffer.hour == slot.hour,
            PlaybackOffer.frame_id == slot.frame_id,
        )
        .limit(1)
    )
    frame = db.scalar(select(Frame).where(Frame.frame_id == slot.frame_id)) if offered else None
    return frame is not None and (
        asset_stat(get_settings().data_dir, "frames", frame.path) is not None
        # Delivery can repair the asset using an active Matter offer's exact hash.
        or _active_transfer_for_slot(db, slot) is not None
    )


def repair_playback_frame(db: Session, device: Device, slot: PlaybackSlot, frame: Frame) -> None:
    """Restore a generated cache entry only if the original render is reproducible."""
    if (
        frame.frame_id != slot.frame_id
        or frame.source_type != "playback"
        or Path(frame.path) != _generated_frame_path(frame.frame_id)
    ):
        raise ValueError("The frame is outside the generated playback cache.")
    _prepare_slot(
        db, device, slot.hour, Source(slot.source_type, slot.source_id), repair_frame=frame
    )


def _prepare_slot(
    db: Session,
    device: Device,
    hour: int,
    source: Source,
    *,
    repair_frame: Frame | None = None,
    observed_at: datetime | None = None,
) -> PlaybackSlot:
    slot = db.get(PlaybackSlot, (device.id, hour))
    photo = _ready_photo(db, source.id) if source.type == "photo" else None
    data_dir = get_settings().data_dir
    photo_metadata = asset_stat(data_dir, "processed", photo.processed_path) if photo else None
    use_photo_dither = False
    version: tuple[object, ...]
    if photo and photo_metadata:
        title = photo.original_filename
        version = (source.type, source.id, title, photo.checksum, photo_metadata.st_mtime_ns)
        # An existing offer can only be repaired from the algorithm that created it.
        legacy_offer = (
            repair_frame is not None and slot is not None and slot.render_key == _render_key(version)
        )
        use_photo_dither = not legacy_offer
        if use_photo_dither:
            version = (*version, PHOTO_DITHER_VERSION)
    else:
        source = Source("dashboard", source.id if source.type == "dashboard" else "sample")
        dashboard = get_dashboard_record(db, source.id)
        config = dashboard_config(dashboard)
        timestamp = _hour_time(hour)
        widgets = resolve_widget_text(db, config, timestamp, device, observed_at=observed_at)
        title = dashboard.name if dashboard else str(config.get("name", "Home Overview"))
        version = (source.type, source.id, title, config, [asdict(widget) for widget in widgets])
        if repair_frame is not None and slot is not None and slot.render_key != _render_key(version):
            legacy_widgets = resolve_widget_text(db, config, timestamp, device, legacy=True)
            legacy_version = (*version[:-1], [asdict(widget) for widget in legacy_widgets])
            if slot.render_key == _render_key(legacy_version):
                widgets = legacy_widgets
                version = legacy_version

    render_key = _render_key(version)
    if repair_frame is not None:
        if slot is None or slot.render_key != render_key:
            raise ValueError("The original frame can no longer be regenerated from its source.")
    elif slot and slot.render_key == render_key:
        frame = db.scalar(select(Frame).where(Frame.frame_id == slot.frame_id))
        if frame and asset_stat(data_dir, "frames", frame.path):
            return slot
    if photo and photo_metadata:
        image = load_processed_photo(data_dir, photo.processed_path)
        if image is None:
            raise ValueError("Could not read the selected processed photo.")
        if image.size != (800, 480):
            image = image.resize((800, 480), Image.Resampling.LANCZOS)
    else:
        image = render_dashboard(config, widgets)
    if use_photo_dither:
        raw, checksum = raw_photo_frame(image)
    else:
        raw, checksum = raw_frame_from_image(image, dither=True)
    if repair_frame is not None:
        if checksum != repair_frame.sha256 or checksum[:24] != repair_frame.frame_id:
            raise ValueError("The regenerated frame does not match the original frame.")
        assert slot is not None
        _store_frame(db, raw, image, source)
        return slot
    frame = _store_frame(db, raw, image, source)
    if not slot:
        slot = PlaybackSlot(device_id=device.id, hour=hour)
        db.add(slot)
    slot.source_type = source.type
    slot.source_id = source.id
    slot.title = title
    slot.frame_id = frame.frame_id
    slot.render_key = render_key
    db.flush()
    return slot


def _render_key(version: tuple[object, ...]) -> str:
    return hashlib.sha256(json.dumps(version, sort_keys=True, default=str).encode()).hexdigest()


def _generated_frame_path(frame_id: str) -> Path:
    return get_settings().data_dir / "frames" / "playback" / f"{frame_id}.raw"


def _store_frame(db: Session, raw: bytes, image: Image.Image, source: Source) -> Frame:
    manifest = build_frame_manifest(
        raw, width=image.width, height=image.height, source_id=source.id, download_url=""
    )
    frame = db.scalar(select(Frame).where(Frame.frame_id == manifest.frame_id))
    cache_path = _generated_frame_path(manifest.frame_id)
    if not frame:
        fields = manifest.as_dict()
        for field in ("created_at", "download_url", "source_id"):
            fields.pop(field)
        frame = Frame(**fields, path=str(cache_path), source_type="playback", source_id=source.id)
        db.add(frame)
    data_dir = get_settings().data_dir
    path = Path(frame.path)
    missing = asset_stat(data_dir, "frames", path) is None
    if missing or (frame.source_type == "playback" and path == cache_path):
        if frame.sha256 != manifest.sha256 or frame.size_bytes != len(raw):
            raise ValueError("The generated frame does not match its stored metadata.")
        if missing or read_asset(data_dir, "frames", path, len(raw)) != raw:
            write_asset(data_dir, "frames", path, raw)
    db.flush()
    return frame


def offer_slot(db: Session, slot: PlaybackSlot, *, now: datetime | None = None) -> Frame:
    offered_at = (now or _utc_now()).replace(tzinfo=None)
    latest = db.scalar(
        select(PlaybackOffer)
        .where(PlaybackOffer.device_id == slot.device_id)
        .order_by(PlaybackOffer.id.desc())
        .limit(1)
    )
    if not latest or (latest.hour, latest.source_type, latest.source_id, latest.frame_id) != (
        slot.hour,
        slot.source_type,
        slot.source_id,
        slot.frame_id,
    ):
        db.add(
            PlaybackOffer(
                device_id=slot.device_id,
                hour=slot.hour,
                source_type=slot.source_type,
                source_id=slot.source_id,
                title=slot.title,
                frame_id=slot.frame_id,
                offered_at=offered_at,
            )
        )
    frame = db.scalar(select(Frame).where(Frame.frame_id == slot.frame_id))
    assert frame is not None
    return frame


def acknowledge_frame(db: Session, device: Device, frame_id: str | None, result: str) -> bool:
    if not frame_id or result not in SUCCESS_RESULTS:
        return False
    playback = db.get(Playback, device.id)
    if not playback:
        return False
    offer = db.scalar(
        select(PlaybackOffer)
        .where(PlaybackOffer.device_id == device.id, PlaybackOffer.frame_id == frame_id)
        .order_by(PlaybackOffer.id.desc())
        .limit(1)
    )
    if not offer or (playback.current_offer_id and playback.current_offer_id >= offer.id):
        return False
    playback.current_offer_id = offer.id
    device.last_frame_id = frame_id
    return True


def _active_transfer_for_slot(db: Session, slot: PlaybackSlot) -> MatterFrameTransfer | None:
    return db.scalar(
        select(MatterFrameTransfer)
        .join(PlaybackOffer, PlaybackOffer.id == MatterFrameTransfer.playback_offer_id)
        .where(
            MatterFrameTransfer.device_id == slot.device_id,
            MatterFrameTransfer.render_key == slot.render_key,
            PlaybackOffer.device_id == slot.device_id,
            PlaybackOffer.hour == slot.hour,
            PlaybackOffer.frame_id == slot.frame_id,
        )
    )


def matter_transfer_failed(db: Session, device_id: int, transfer_id: str) -> bool:
    detail = db.scalar(
        select(DeviceStatusHistory.detail_json)
        .where(
            DeviceStatusHistory.device_id == device_id,
            DeviceStatusHistory.status == MATTER_FRAME_FAILED,
        )
        .order_by(DeviceStatusHistory.id.desc())
        .limit(1)
    )
    # Only the newest exact transfer can record a failure. Older entries
    # cannot describe the current transfer once it has been superseded.
    try:
        details = json.loads(detail) if detail else {}
    except ValueError:
        return False
    return isinstance(details, dict) and details.get("transfer_id") == transfer_id


def record_matter_transfer_failure(
    db: Session, device: Device, transfer_id: str, *, now: datetime | None = None
) -> bool:
    """Record terminal failure for the current unconfirmed transfer in a write transaction."""
    hour = _hour(now or _utc_now())
    playback = db.get(Playback, device.id)
    due = playback.anchor_hour if playback and playback.paused else hour
    slot = db.get(PlaybackSlot, (device.id, due))
    if slot is None:
        return False
    active = _active_transfer_for_slot(db, slot)
    if active is None or active.transfer_id != transfer_id or active.acknowledged_at is not None:
        return False
    if not matter_transfer_failed(db, device.id, transfer_id):
        device.last_refresh_result = MATTER_FRAME_FAILED
        db.add(
            DeviceStatusHistory(
                device_id=device.id,
                status=MATTER_FRAME_FAILED,
                detail_json=json.dumps({"transfer_id": transfer_id, "frame_id": slot.frame_id}),
            )
        )
    return True


def playback_status(db: Session, device: Device, now: datetime | None = None) -> dict[str, object]:
    hour = _hour(now or _utc_now())
    playback = db.get(Playback, device.id)
    current = (
        db.get(PlaybackOffer, playback.current_offer_id)
        if playback and playback.current_offer_id
        else None
    )
    due = playback.anchor_hour if playback and playback.paused else hour
    pending = db.get(PlaybackSlot, (device.id, due))
    active = _active_transfer_for_slot(db, pending) if pending else None
    awaiting = active if active and active.acknowledged_at is None else None
    pending_status = None
    if pending and (awaiting or not current or pending.frame_id != current.frame_id):
        pending_status = (
            "failed"
            if awaiting and matter_transfer_failed(db, device.id, awaiting.transfer_id)
            else "waiting"
        )
    upcoming = db.scalars(
        select(PlaybackSlot)
        .where(
            PlaybackSlot.device_id == device.id,
            PlaybackSlot.hour > hour,
            PlaybackSlot.hour <= hour + UPCOMING_SLOTS,
        )
        .order_by(PlaybackSlot.hour)
    )
    return {
        "paused": playback.paused if playback else False,
        "syncing_controls": db.get(MatterPlaybackIntent, device.id) is not None,
        "playlist": [asdict(source) for source in _playlist(playback)],
        "current": _slot_payload(device, current) if current else None,
        "pending": _slot_payload(device, pending) if pending and pending_status else None,
        "pending_status": pending_status,
        "upcoming": [_slot_payload(device, slot) for slot in upcoming],
    }


def _slot_payload(device: Device, slot: PlaybackSlot | PlaybackOffer) -> dict[str, str]:
    return {
        "scheduled_at": _hour_time(slot.hour).isoformat(),
        "source_type": slot.source_type,
        "source_id": slot.source_id,
        "title": slot.title,
        "frame_id": slot.frame_id,
        "preview_url": f"/api/devices/{quote(device.device_uid, safe='')}/playback/preview/{slot.frame_id}",
    }


def playback_frame(db: Session, device: Device, frame_id: str) -> Frame | None:
    slot = db.scalar(
        select(PlaybackSlot.device_id)
        .where(PlaybackSlot.device_id == device.id, PlaybackSlot.frame_id == frame_id)
        .limit(1)
    )
    offer = db.scalar(
        select(PlaybackOffer.id)
        .where(PlaybackOffer.device_id == device.id, PlaybackOffer.frame_id == frame_id)
        .limit(1)
    )
    if slot is None and offer is None:
        return None
    return db.scalar(select(Frame).where(Frame.frame_id == frame_id))


def read_frame_bytes(frame: Frame) -> bytes | None:
    if not 0 < frame.size_bytes <= MAX_RAW_FRAME_BYTES:
        return None
    raw = read_asset(get_settings().data_dir, "frames", frame.path, frame.size_bytes)
    return raw if raw is not None and len(raw) == frame.size_bytes else None


def preview_image(frame: Frame) -> Image.Image:
    raw = read_frame_bytes(frame)
    pixels = frame.width * frame.height
    if raw is None or frame.width <= 0 or frame.height <= 0 or (pixels + 1) // 2 != len(raw):
        raise ValueError("Prepared frame is missing or invalid.")
    palette = WAVESHARE_7COLOR_6
    indices = bytes(value for byte in raw for value in (byte >> 4, byte & 15))[
        : frame.width * frame.height
    ]
    image = Image.frombytes("P", (frame.width, frame.height), indices)
    colors = [0] * 48
    for value, color in zip(palette.packed_values, palette.colors, strict=True):
        colors[value * 3 : value * 3 + 3] = color
    image.putpalette(colors)
    return image


def prune_playback(db: Session, now: datetime | None = None) -> None:
    expired = (now or _utc_now()).replace(tzinfo=None) - OFFER_RETENTION
    db.execute(
        delete(PlaybackOffer).where(
            PlaybackOffer.offered_at < expired,
            PlaybackOffer.id.not_in(
                select(Playback.current_offer_id).where(Playback.current_offer_id.is_not(None))
            ),
            PlaybackOffer.id.not_in(select(MatterFrameTransfer.playback_offer_id)),
        )
    )
    orphans = db.scalars(
        select(Frame).where(
            Frame.source_type == "playback",
            Frame.frame_id.not_in(select(PlaybackSlot.frame_id)),
            Frame.frame_id.not_in(select(PlaybackOffer.frame_id)),
            Frame.frame_id.not_in(
                select(Device.last_frame_id).where(Device.last_frame_id.is_not(None))
            ),
        )
    ).all()
    cache_dir = get_settings().data_dir / "frames" / "playback"
    for frame in orphans:
        cache_path = cache_dir / f"{frame.frame_id}.raw"
        if Path(frame.path) == cache_path:
            remove_asset(get_settings().data_dir, "frames", cache_path)
        db.delete(frame)
