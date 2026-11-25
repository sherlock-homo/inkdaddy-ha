from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from itertools import chain
from os import stat_result
from pathlib import Path
from time import monotonic
from urllib.parse import quote

from PIL import Image
from sqlalchemy import delete, func, select, text, union
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
    PhotoRevision,
    Playback,
    PlaybackOffer,
    PlaybackSlot,
    Setting,
)
from .asset_storage import asset_names, asset_stat, read_asset, remove_asset, write_asset
from .dashboard_content import (
    AgendaText,
    ForecastDay,
    ForecastText,
    PhotoOverlay,
    WidgetText,
    dashboard_config,
    dev24_dashboard_configs,
    dev25_dashboard_configs,
    get_dashboard_record,
    render_dashboard,
    render_legacy_dashboard,
    resolve_widget_text,
    resolve_photo_overlay,
)
from .palette import PHOTO_DITHER_VERSION, WAVESHARE_7COLOR_6, raw_frame_from_image, raw_photo_frame
from .photos import load_color_managed_photo, load_processed_photo, sweep_untracked_photo_derivatives
from .rendering import build_frame_manifest

UPCOMING_SLOTS = 5
MAX_RAW_FRAME_BYTES = 800 * 480 // 2
ACTION_NAMES = {"next_content", "previous_content", "refresh_current", "pause", "play"}
SUCCESS_RESULTS = {"displayed_hub_frame", "ok", "refresh:ok"}
OFFER_RETENTION = timedelta(hours=1)
_ASSET_SWEEP_KEY = "playback.asset_sweep_hour"
_GENERATED_RAW_NAME = re.compile(r"[0-9a-f]{24}\.raw")
_UNREADABLE_PHOTOS_LIMIT = 128
_UNREADABLE_PHOTO_RETRY_SECONDS = 300
_PHOTO_COLOR_VERSION = "icc-srgb-v1"
_unreadable_photos: OrderedDict[tuple[object, ...], float] = OrderedDict()
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


def epoch_second(now: datetime) -> int:
    return int(now.timestamp())


def slot_interval_seconds(device: Device) -> int:
    minutes = device.refresh_interval_minutes or 60
    return minutes * (30 if device.interlace_dashboard_id else 60)


def _scheduled_second(device: Device, playback: Playback | None, now: datetime) -> int:
    second = epoch_second(now)
    anchor = playback.anchor_second if playback else 0
    if playback and playback.first_new_interval_second is not None:
        if second < playback.first_new_interval_second:
            return anchor
        anchor = playback.first_new_interval_second
    return second - (second - anchor) % slot_interval_seconds(device)


def scheduled_time(scheduled_second: int) -> datetime:
    return datetime.fromtimestamp(scheduled_second, timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _get_playback(db: Session, device: Device, scheduled_second: int) -> Playback:
    playback = db.get(Playback, device.id)
    if playback is None:
        playback = Playback(
            device_id=device.id,
            anchor_second=scheduled_second,
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
    selected = _playlist(playback)
    if not selected and device.selected_source_type != "photo":
        selected = [Source(device.selected_source_type, device.selected_source_id or "sample")]
    if device.interlace_dashboard_id:
        selected = [source for source in selected if source.type in ("photo", "album")]
    if not selected:
        rows = list(db.scalars(select(Photo.id).where(Photo.processed_status == "ready").order_by(Photo.id)))
        if device.selected_source_type == "photo" and device.selected_source_id and device.selected_source_id.isdigit():
            photo_id = int(device.selected_source_id)
            if photo_id in rows:
                index = rows.index(photo_id)
                rows = rows[index:] + rows[:index]
        selected = [Source("photo", str(photo_id)) for photo_id in rows]

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
    if device.interlace_dashboard_id:
        dashboard = Source("dashboard", device.interlace_dashboard_id)
        return [entry for photo in result for entry in (photo, dashboard)] or [dashboard]
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
    scheduled_second = _scheduled_second(device, db.get(Playback, device.id), now or _utc_now())
    playback = _get_playback(db, device, scheduled_second)
    playback.playlist_json = json.dumps([asdict(source) for source in sources])
    _set_anchor(playback, scheduled_second, 0)
    playback.manual_request_second = playback.manual_requested_at = None
    _invalidate_slot(db, device, scheduled_second)
    db.flush()


def _position(playback: Playback, scheduled_second: int, interval: int) -> int:
    if playback.paused or scheduled_second <= playback.anchor_second:
        return playback.anchor_position
    if playback.first_new_interval_second is not None:
        return playback.anchor_position + 1 + (scheduled_second - playback.first_new_interval_second) // interval
    return playback.anchor_position + (scheduled_second - playback.anchor_second) // interval


def _set_anchor(playback: Playback, scheduled_second: int, position: int) -> None:
    playback.anchor_second = scheduled_second
    playback.anchor_position = position
    first = playback.first_new_interval_second
    if first is not None and scheduled_second >= first:
        playback.first_new_interval_second = None


def _next_scheduled_second(playback: Playback | None, due: int, interval: int) -> int:
    first = playback.first_new_interval_second if playback else None
    return first if first is not None and first > due else due + interval


def apply_playback_action(
    db: Session, device: Device, action: str, now: datetime | None = None
) -> None:
    requested_at = now or _utc_now()
    scheduled_second = _scheduled_second(device, db.get(Playback, device.id), requested_at)
    playback = _get_playback(db, device, scheduled_second)
    if action not in ACTION_NAMES:
        raise ValueError("Unsupported display action.")
    if action == "refresh_current":
        playback.manual_request_second = playback.anchor_second if playback.paused else scheduled_second
        playback.manual_requested_at = requested_at.astimezone(timezone.utc).replace(tzinfo=None)
        _invalidate_slot(db, device, playback.manual_request_second)
        invalidate_matter_transfer(db, device.id)
        return
    if (action == "pause" and playback.paused) or (action == "play" and not playback.paused):
        return
    offset = {"next_content": 1, "previous_content": -1}.get(action, 0)
    _set_anchor(playback, scheduled_second, _position(playback, scheduled_second, slot_interval_seconds(device)) + offset)
    if action in {"pause", "play"}:
        playback.paused = action == "pause"
    if offset:
        playback.manual_request_second = scheduled_second
        playback.manual_requested_at = requested_at.astimezone(timezone.utc).replace(tzinfo=None)
        _invalidate_slot(db, device, scheduled_second)


def reset_selection(db: Session, device: Device, now: datetime | None = None) -> None:
    playback = db.get(Playback, device.id)
    if playback and not _playlist(playback):
        _set_anchor(playback, _scheduled_second(device, playback, now or _utc_now()), 0)
        playback.manual_request_second = playback.manual_requested_at = None
        _invalidate_slot(db, device, playback.anchor_second)


def update_playback_schedule(
    db: Session, device: Device, interval_minutes: int, dashboard_id: str | None, now: datetime | None = None,
) -> None:
    """Rebuild the future cadence while keeping the current offer and paused state."""
    old_dashboard = device.interlace_dashboard_id
    if interval_minutes == device.refresh_interval_minutes and old_dashboard == dashboard_id:
        return
    timestamp = now or _utc_now()
    playback = db.get(Playback, device.id) if device.id is not None else None
    if playback is None:
        device.interlace_dashboard_id = dashboard_id
        device.refresh_interval_minutes = interval_minutes
        return
    due = _scheduled_second(device, playback, timestamp)
    position = _position(playback, due, slot_interval_seconds(device))
    current = playback.anchor_second if playback.paused else due

    if bool(old_dashboard) != bool(dashboard_id):
        old_sources = _expand_playback(db, device, playback)
        index = position % len(old_sources)
        current_source = old_sources[index]
        slot = db.get(PlaybackSlot, (device.id, current))
        offered = slot is not None and _slot_already_offered(db, slot)
        if offered:
            current_source = Source(slot.source_type, slot.source_id)
        device.interlace_dashboard_id = dashboard_id
        new_sources = _expand_playback(db, device, playback)
        if dashboard_id and current_source.type == "dashboard":
            # Continue after the preceding playlist photo when an offered
            # dashboard occupies this slot. Otherwise start on a photo now.
            photo_count = sum(source.type == "photo" for source in old_sources[:index])
            position = photo_count * 2 - int(offered)
        elif current_source in new_sources:
            occurrence = old_sources[:index].count(current_source)
            matches = [i for i, source in enumerate(new_sources) if source == current_source]
            position = matches[occurrence % len(matches)]
        else:
            previous = old_sources[(index - 1) % len(old_sources)]
            position = new_sources.index(previous) if previous in new_sources else (-1 if offered else 0)
    device.interlace_dashboard_id = dashboard_id
    device.refresh_interval_minutes = interval_minutes
    interval = slot_interval_seconds(device)
    _set_anchor(playback, current, position)
    elapsed = max(0, epoch_second(timestamp) - due)
    playback.first_new_interval_second = due + (elapsed // interval + 1) * interval
    db.execute(delete(PlaybackSlot).where(
        PlaybackSlot.device_id == device.id, PlaybackSlot.scheduled_second > current,
    ))


def update_refresh_interval(db: Session, device: Device, interval: int, now: datetime | None = None) -> None:
    update_playback_schedule(db, device, interval, device.interlace_dashboard_id, now)


def _invalidate_slot(db: Session, device: Device, scheduled_second: int) -> None:
    db.execute(
        delete(PlaybackSlot).where(PlaybackSlot.device_id == device.id, PlaybackSlot.scheduled_second == scheduled_second)
    )


def invalidate_matter_transfer(db: Session, device_id: int) -> None:
    db.execute(delete(MatterFrameTransfer).where(MatterFrameTransfer.device_id == device_id))


def _manual_request_at(playback: Playback | None, scheduled_second: int) -> datetime | None:
    if playback and playback.manual_request_second == scheduled_second and playback.manual_requested_at is not None:
        return playback.manual_requested_at.replace(tzinfo=timezone.utc)
    return None


def refresh_playback_slots(
    db: Session, device: Device, *, now: datetime | None = None, upcoming: bool = True
) -> PlaybackSlot:
    """Prepare the due frame and up to five future frames. Call inside a write transaction."""
    observed_at = now or _utc_now()
    scheduled_second = _scheduled_second(device, db.get(Playback, device.id), observed_at)
    playback = _get_playback(db, device, scheduled_second)
    sources = _expand_playback(db, device, playback)
    position = _position(playback, scheduled_second, slot_interval_seconds(device))
    current_second = playback.anchor_second if playback.paused else scheduled_second
    current = db.get(PlaybackSlot, (device.id, current_second))
    if not current or not _slot_already_offered(db, current):
        current = _prepare_slot(
            db, device, current_second, sources[position % len(sources)],
            observed_at=observed_at, manual_requested_at=_manual_request_at(playback, current_second),
        )
    if upcoming:
        interval = slot_interval_seconds(device)
        next_second = _next_scheduled_second(playback, scheduled_second, interval)
        for offset in range(1, UPCOMING_SLOTS + 1):
            _prepare_slot(
                db, device, next_second + (offset - 1) * interval,
                sources[(position + offset) % len(sources)], observed_at=observed_at,
            )
        preserve_current = PlaybackSlot.scheduled_second != current_second
        skipped_past = PlaybackSlot.scheduled_second <= scheduled_second
        skipped_future = PlaybackSlot.scheduled_second > next_second + (UPCOMING_SLOTS - 1) * interval
        db.execute(
            delete(PlaybackSlot).where(
                PlaybackSlot.device_id == device.id, preserve_current, skipped_past | skipped_future
            )
        )
    return current


def _slot_already_offered(db: Session, slot: PlaybackSlot) -> bool:
    # A scheduled repeat can keep the artwork from an earlier confirmed offer.
    # Freeze that decision for this slot just as we freeze content already sent.
    if slot.retained_offer_id is not None:
        return True
    offered = db.scalar(
        select(PlaybackOffer.id)
        .where(
            PlaybackOffer.device_id == slot.device_id,
            PlaybackOffer.scheduled_second == slot.scheduled_second,
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
    if slot.source_type == "photo":
        _repair_photo_frame(db, slot, frame)
        return
    if slot.dashboard_render_json:
        # A date, forecast or battery reading may change during a transfer.
        # Recompose exactly what the offer contained, even after the owner edits the layout.
        try:
            snapshot = json.loads(slot.dashboard_render_json)
            widgets = []
            for tile, values in zip(snapshot["config"]["widgets"], snapshot["widgets"], strict=True):
                if "days" in values:
                    values["days"] = tuple(ForecastDay(**day) for day in values["days"])
                    widgets.append(ForecastText(**values))
                elif tile["source"].get("type") == "calendar":
                    widgets.append(AgendaText(**values))
                else:
                    widgets.append(WidgetText(**values))
            image = render_dashboard(snapshot["config"], widgets)
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ValueError("The saved dashboard snapshot is invalid.") from exc
        raw, digest = raw_frame_from_image(image, dither=True)
        if digest != frame.sha256 or digest[:24] != frame.frame_id:
            raise ValueError("The saved dashboard no longer matches its offer.")
        _store_frame(db, raw, image, Source(slot.source_type, slot.source_id))
        return
    manual_requested_at = _manual_request_at(db.get(Playback, device.id), slot.scheduled_second)
    _prepare_slot(
        db, device, slot.scheduled_second, Source(slot.source_type, slot.source_id),
        repair_frame=frame, manual_requested_at=manual_requested_at,
    )


def _prepare_slot(
    db: Session,
    device: Device,
    scheduled_second: int,
    source: Source,
    *,
    repair_frame: Frame | None = None,
    observed_at: datetime | None = None,
    manual_requested_at: datetime | None = None,
) -> PlaybackSlot:
    slot = db.get(PlaybackSlot, (device.id, scheduled_second))
    photo = _ready_photo(db, source.id) if source.type == "photo" else None
    data_dir = get_settings().data_dir
    photo_metadata = asset_stat(data_dir, "processed", photo.processed_path) if photo else None
    overlay = None
    overlay_snapshot = None
    dashboard_snapshot = None
    painter = render_dashboard
    version: tuple[object, ...]
    if photo and photo_metadata:
        title = photo.original_filename
        original_metadata = asset_stat(data_dir, "originals", photo.original_path)
        version = (
            source.type, source.id, title, photo.checksum, _photo_asset_signature(photo_metadata),
            PHOTO_DITHER_VERSION, _PHOTO_COLOR_VERSION,
            _photo_asset_signature(original_metadata) if original_metadata else None,
        )
        overlay = resolve_photo_overlay(
            db, device.overlay_dashboard_id, manual_requested_at or scheduled_time(scheduled_second),
            device, photo=photo, observed_at=observed_at,
            time_kind="manual" if manual_requested_at is not None else "scheduled",
        )
        if overlay is not None:
            overlay_snapshot = overlay.snapshot
            version = (*version, "photo-overlay-v1", overlay_snapshot)
    else:
        source = Source("dashboard", source.id if source.type == "dashboard" else device.interlace_dashboard_id or "sample")
        dashboard = get_dashboard_record(db, source.id)
        config = dashboard_config(dashboard)
        timestamp = manual_requested_at or scheduled_time(scheduled_second)
        if repair_frame is not None and slot is not None:
            observation = slot.manual_observed_at or db.scalar(
                select(func.min(PlaybackOffer.offered_at)).where(
                    PlaybackOffer.device_id == device.id,
                    PlaybackOffer.scheduled_second == slot.scheduled_second,
                    PlaybackOffer.frame_id == repair_frame.frame_id,
                )
            )
            if observation is not None:
                observed_at = observation.replace(tzinfo=timezone.utc)
        observed_at = observed_at or _utc_now()
        widgets = resolve_widget_text(
            db, config, timestamp, device, observed_at=observed_at,
            time_kind="manual" if manual_requested_at is not None else "scheduled",
        )
        title = dashboard.name if dashboard else str(config.get("name", "Home Overview"))
        dashboard_snapshot = {"config": config, "widgets": [asdict(widget) for widget in widgets]}
        version = ("color-dashboard-v1", source.type, source.id, title, dashboard_snapshot)
        if repair_frame is not None and slot is not None:
            if slot.render_key != _render_key(version):
                original_config = dashboard_config(dashboard, historical=True)
                candidates = chain(
                    (("dev25", layout) for layout in dev25_dashboard_configs(dashboard)),
                    (("dev24", layout) for layout in dev24_dashboard_configs(dashboard)),
                    ((revision, original_config) for revision in ("dev23", "dev20", "previous", "legacy")),
                )
                for render_version, layout in candidates:
                    historical_widgets = resolve_widget_text(
                        db, layout, timestamp, device, observed_at=observed_at, render_version=render_version,
                        time_kind="manual" if manual_requested_at is not None else "scheduled",
                    )
                    historical_version = (source.type, source.id, slot.title, layout,
                                          [asdict(widget) for widget in historical_widgets])
                    if slot.render_key == _render_key(historical_version):
                        config = layout
                        widgets = historical_widgets
                        version = historical_version
                        painter = render_legacy_dashboard
                        break

    render_key = _render_key(version)
    frame = None
    if repair_frame is not None:
        if slot is None or slot.render_key != render_key:
            raise ValueError("The original frame can no longer be regenerated from its source.")
    else:
        if slot and slot.render_key == render_key:
            existing = db.scalar(select(Frame).where(Frame.frame_id == slot.frame_id))
            if existing and (
                _photo_frame_available(data_dir, existing) if photo_metadata
                else asset_stat(data_dir, "frames", existing.path)
            ):
                return slot
        if photo_metadata:
            previous = db.scalar(
                select(Frame).join(PlaybackSlot, PlaybackSlot.frame_id == Frame.frame_id)
                .where(
                    PlaybackSlot.device_id == device.id,
                    PlaybackSlot.render_key == render_key,
                )
                .limit(1)
            )
            if previous and _photo_frame_available(data_dir, previous):
                frame = previous
    if frame is None:
        if photo and photo_metadata:
            image = _load_scheduled_photo(data_dir, photo, photo_metadata)
            if image is None:
                return _prepare_slot(
                    db, device, scheduled_second, Source("dashboard", device.interlace_dashboard_id or "sample"),
                    observed_at=observed_at, manual_requested_at=manual_requested_at,
                )
            image = _framed_photo(image, overlay)
            raw, checksum = raw_photo_frame(image)
        else:
            image = painter(config, widgets)
            raw, checksum = raw_frame_from_image(image, dither=True)
        if repair_frame is not None:
            if checksum != repair_frame.sha256 or checksum[:24] != repair_frame.frame_id:
                raise ValueError("The regenerated frame does not match the original frame.")
            assert slot is not None
            _store_frame(db, raw, image, source)
            return slot
        frame = _store_frame(db, raw, image, source)
    if not slot:
        slot = PlaybackSlot(device_id=device.id, scheduled_second=scheduled_second)
        db.add(slot)
    slot.source_type = source.type
    slot.source_id = source.id
    slot.title = title
    slot.frame_id = frame.frame_id
    slot.render_key = render_key
    slot.retained_offer_id = None
    slot.dashboard_render_json = json.dumps(dashboard_snapshot) if dashboard_snapshot is not None else None
    slot.photo_render_json = (
        json.dumps({"processed_path": photo.processed_path, "overlay": overlay_snapshot})
        if photo and photo_metadata else None
    )
    slot.manual_observed_at = (
        observed_at.astimezone(timezone.utc).replace(tzinfo=None)
        if source.type == "dashboard" and manual_requested_at is not None else None
    )
    db.flush()
    return slot


def _framed_photo(image: Image.Image, overlay: PhotoOverlay | None) -> Image.Image:
    if image.size != (800, 480):
        image = image.resize((800, 480), Image.Resampling.LANCZOS)
    return overlay.render(image) if overlay is not None else image


def _repair_photo_frame(db: Session, slot: PlaybackSlot, frame: Frame) -> None:
    photo = db.get(Photo, int(slot.source_id)) if slot.source_id.isdigit() else None
    if photo is None:
        raise ValueError("The original photo is no longer available.")
    try:
        snapshot = json.loads(slot.photo_render_json) if slot.photo_render_json else {}
        overlay = PhotoOverlay.from_snapshot(snapshot["overlay"]) if snapshot.get("overlay") else None
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError("The stored photo render could not be read.") from exc
    snapshot_path = snapshot.get("processed_path")
    data_dir = get_settings().data_dir
    revisions = db.scalars(select(PhotoRevision.processed_path).where(
        PhotoRevision.photo_id == photo.id, PhotoRevision.processed_path.is_not(None),
    ).order_by(PhotoRevision.id.desc()).execution_options(yield_per=16))
    paths = dict.fromkeys((snapshot_path, photo.processed_path))
    for path in chain(paths, (path for path in revisions if path not in paths)):
        if path is None:
            continue
        images = (
            lambda: load_color_managed_photo(data_dir, path, photo.original_path),
            lambda: load_processed_photo(data_dir, path),
        )
        for load_image in images:
            image = load_image()
            if image is None:
                continue
            image = _framed_photo(image, overlay)
            renderers = (raw_photo_frame,) if overlay is not None else (
                raw_photo_frame, lambda image: raw_frame_from_image(image, dither=True),
            )
            for render in renderers:
                raw, checksum = render(image)
                if checksum == frame.sha256 and checksum[:24] == frame.frame_id:
                    _store_frame(db, raw, image, Source("photo", slot.source_id))
                    return
    raise ValueError("The regenerated frame does not match the original frame.")


def _photo_frame_available(data_dir: Path, frame: Frame) -> bool:
    if (
        frame.source_type != "playback"
        or Path(frame.path) != _generated_frame_path(frame.frame_id)
        or frame.size_bytes != MAX_RAW_FRAME_BYTES
        or (frame.width, frame.height) != (800, 480)
    ):
        return False
    metadata = asset_stat(data_dir, "frames", frame.path)
    return metadata is not None and metadata.st_size == MAX_RAW_FRAME_BYTES


def _photo_asset_signature(metadata: stat_result) -> tuple[int, int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns


def _load_scheduled_photo(data_dir: Path, photo: Photo, metadata: stat_result) -> Image.Image | None:
    # Several slots can share the same source, so retry only after it changes or the timer expires.
    signature = (data_dir, photo.processed_path, *_photo_asset_signature(metadata))
    now = monotonic()
    if _unreadable_photos.get(signature, 0) > now:
        return None
    image = load_color_managed_photo(data_dir, photo.processed_path, photo.original_path)
    if image is None:
        _unreadable_photos[signature] = now + _UNREADABLE_PHOTO_RETRY_SECONDS
        if len(_unreadable_photos) > _UNREADABLE_PHOTOS_LIMIT:
            _unreadable_photos.popitem(last=False)
    else:
        _unreadable_photos.pop(signature, None)
    return image


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
    if not latest or (latest.scheduled_second, latest.source_type, latest.source_id, latest.frame_id) != (
        slot.scheduled_second,
        slot.source_type,
        slot.source_id,
        slot.frame_id,
    ):
        db.add(
            PlaybackOffer(
                device_id=slot.device_id,
                scheduled_second=slot.scheduled_second,
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
            PlaybackOffer.scheduled_second == slot.scheduled_second,
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
    playback = db.get(Playback, device.id)
    scheduled_second = _scheduled_second(device, playback, now or _utc_now())
    due = playback.anchor_second if playback and playback.paused else scheduled_second
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
    playback = db.get(Playback, device.id)
    scheduled_second = _scheduled_second(device, playback, now or _utc_now())
    current = (
        db.get(PlaybackOffer, playback.current_offer_id)
        if playback and playback.current_offer_id
        else None
    )
    due = playback.anchor_second if playback and playback.paused else scheduled_second
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
    interval = slot_interval_seconds(device)
    next_second = _next_scheduled_second(playback, scheduled_second, interval)
    upcoming = db.scalars(
        select(PlaybackSlot)
        .where(
            PlaybackSlot.device_id == device.id,
            PlaybackSlot.scheduled_second >= next_second,
            PlaybackSlot.scheduled_second <= next_second + (UPCOMING_SLOTS - 1) * interval,
        )
        .order_by(PlaybackSlot.scheduled_second)
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
        "scheduled_at": scheduled_time(slot.scheduled_second).isoformat(),
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
    if (
        raw is None or frame.width <= 0 or frame.height <= 0 or (pixels + 1) // 2 != len(raw)
        or hashlib.sha256(raw).hexdigest() != frame.sha256
    ):
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
    begin_playback_write(db)
    now = now or _utc_now()
    expired = now.replace(tzinfo=None) - OFFER_RETENTION
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
    data_dir = get_settings().data_dir
    cache_dir = data_dir / "frames" / "playback"
    for frame in orphans:
        cache_path = cache_dir / f"{frame.frame_id}.raw"
        if Path(frame.path) == cache_path:
            remove_asset(data_dir, "frames", cache_path)
        db.delete(frame)

    _sweep_untracked_assets(db, data_dir, cache_dir, now)


def _sweep_untracked_assets(db: Session, data_dir: Path, cache_dir: Path, now: datetime) -> None:
    hour = int(now.timestamp()) // 3600
    last_sweep = db.get(Setting, _ASSET_SWEEP_KEY)
    if last_sweep and last_sweep.value_json == str(hour):
        return
    # A rollback can leave the cache file behind without a committed Frame row.
    # The caller holds the same writer lock used when generating playback assets.
    names = asset_names(data_dir, "frames", "playback")
    if names:
        referenced = set(db.scalars(union(
            select(Frame.frame_id),
            select(PlaybackSlot.frame_id),
            select(PlaybackOffer.frame_id),
            select(Device.last_frame_id).where(Device.last_frame_id.is_not(None)),
        )))
        for name in names:
            if _GENERATED_RAW_NAME.fullmatch(name) and name[:-4] not in referenced:
                remove_asset(data_dir, "frames", cache_dir / name)
    sweep_untracked_photo_derivatives(db, data_dir, now=now)
    if last_sweep:
        last_sweep.value_json = str(hour)
    else:
        db.add(Setting(key=_ASSET_SWEEP_KEY, value_json=str(hour)))
    db.flush()
