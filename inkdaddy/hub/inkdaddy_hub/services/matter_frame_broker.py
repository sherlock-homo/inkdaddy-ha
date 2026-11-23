from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Device,
    DeviceStatusHistory,
    Frame,
    MatterFrameTransfer,
    Playback,
    PlaybackOffer,
    PlaybackSlot,
)
from .palette import WAVESHARE_7COLOR_6
from .playback import (
    SUCCESS_RESULTS,
    begin_playback_write,
    epoch_minute,
    read_frame_bytes,
    refresh_playback_slots,
    repair_playback_frame,
    scheduled_time,
)

FRAME_WIDTH = 800
FRAME_HEIGHT = 480
FRAME_SIZE = FRAME_WIDTH * FRAME_HEIGHT // 2


class MatterFrameError(ValueError):
    """The configured display or its prepared panel frame is unavailable."""


class _FramePayloadError(MatterFrameError):
    """The frame metadata is valid but its backing file could not be verified."""


@dataclass(frozen=True)
class MatterFrameOffer:
    transfer_id: str
    frame_id: str
    sha256: str
    payload: bytes | None
    width: int
    height: int
    playing: bool
    acknowledged: bool
    source_type: str
    source_id: str
    scheduled_at: datetime

    @property
    def size_bytes(self) -> int:
        return FRAME_SIZE


def prepare_matter_frame(
    db: Session,
    device_uid: str,
    *,
    now: datetime | None = None,
    repair_cache: bool = False,
    include_payload: bool = True,
) -> MatterFrameOffer:
    """Prepare an exact offer for a known device after the caller verifies its Matter binding.

    The delivery worker can defer the asset read until it needs to send bytes.
    Commit this transaction before sending the offer or its bytes to the controller.
    """
    begin_playback_write(db)
    timestamp = now or datetime.now(timezone.utc)
    device = _resolve_device(db, device_uid)
    try:
        slot = refresh_playback_slots(db, device, now=timestamp, upcoming=False)
    except (OSError, ValueError) as exc:
        raise MatterFrameError("Could not prepare the scheduled frame.") from exc
    frame = db.scalar(select(Frame).where(Frame.frame_id == slot.frame_id))
    if frame is None:
        raise MatterFrameError("The scheduled frame was not prepared.")
    _validate_frame_metadata(frame)
    data = _verified_payload(db, device, slot, frame, repair_cache=repair_cache) if include_payload else None
    active = db.get(MatterFrameTransfer, device.id)
    previous = db.get(PlaybackOffer, active.playback_offer_id) if active else None
    latest = _latest_offer_id(db, device.id)
    playback = db.get(Playback, device.id)
    assert playback is not None
    same_artwork = (
        active is not None and previous is not None and previous.id == latest
        and previous.frame_id == slot.frame_id and previous.title == slot.title
        and previous.source_type == slot.source_type and previous.source_id == slot.source_id
    )
    if (
        same_artwork and previous.scheduled_minute < slot.scheduled_minute
        and active.acknowledged_at is not None and previous.id == playback.current_offer_id
        and device.last_frame_id == slot.frame_id and playback.manual_request_minute != slot.scheduled_minute
    ):
        # Keep the authenticated receipt and its timestamp. No panel refresh occurred.
        slot.retained_offer_id = previous.id
    elif (
        not same_artwork or previous.scheduled_minute != slot.scheduled_minute
        or active.render_key != slot.render_key
    ):
        slot.retained_offer_id = None
        previous = PlaybackOffer(
            device_id=device.id,
            scheduled_minute=slot.scheduled_minute,
            source_type=slot.source_type,
            source_id=slot.source_id,
            title=slot.title,
            frame_id=slot.frame_id,
            offered_at=timestamp.astimezone(timezone.utc).replace(tzinfo=None),
        )
        db.add(previous)
        db.flush()
        transfer_id = secrets.token_hex(12)
        while transfer_id == frame.frame_id:
            transfer_id = secrets.token_hex(12)
        if active is None:
            active = MatterFrameTransfer(device_id=device.id)
            db.add(active)
        active.transfer_id = transfer_id
        active.playback_offer_id = previous.id
        active.render_key = slot.render_key
        active.acknowledged_at = None
        db.flush()
    return MatterFrameOffer(
        transfer_id=active.transfer_id,
        frame_id=frame.frame_id,
        sha256=frame.sha256,
        payload=data,
        width=frame.width,
        height=frame.height,
        playing=not playback.paused,
        acknowledged=active.acknowledged_at is not None,
        source_type=slot.source_type,
        source_id=slot.source_id,
        scheduled_at=scheduled_time(previous.scheduled_minute),
    )


def load_matter_frame_payload(db: Session, device: Device, offer: MatterFrameOffer) -> bytes:
    """Read the exact offered asset in the same write transaction that prepared it."""
    scheduled_minute = epoch_minute(offer.scheduled_at)
    slot = db.get(PlaybackSlot, (device.id, scheduled_minute))
    if slot is None and offer.acknowledged:
        # The queue can prune the original slot while the display keeps its confirmed image.
        slot = db.scalar(
            select(PlaybackSlot)
            .join(MatterFrameTransfer, MatterFrameTransfer.playback_offer_id == PlaybackSlot.retained_offer_id)
            .where(PlaybackSlot.device_id == device.id, MatterFrameTransfer.device_id == device.id,
                   MatterFrameTransfer.transfer_id == offer.transfer_id)
            .order_by(PlaybackSlot.scheduled_minute.desc()).limit(1)
        )
    frame = db.scalar(select(Frame).where(Frame.frame_id == offer.frame_id))
    if slot is None or slot.frame_id != offer.frame_id or frame is None or frame.sha256 != offer.sha256:
        raise MatterFrameError("The prepared frame changed before delivery.")
    _validate_frame_metadata(frame)
    return _verified_payload(db, device, slot, frame, repair_cache=True)


def _verified_payload(
    db: Session, device: Device, slot: PlaybackSlot, frame: Frame, *, repair_cache: bool
) -> bytes:
    try:
        return _read_verified_bytes(frame)
    except _FramePayloadError:
        if not repair_cache:
            raise
    try:
        repair_playback_frame(db, device, slot, frame)
    except (OSError, ValueError) as exc:
        raise MatterFrameError(f"Could not repair the prepared frame: {exc}") from exc
    return _read_verified_bytes(frame)


def record_matter_frame_receipt(
    db: Session,
    device_uid: str,
    *,
    transfer_id: str,
    frame_id: str,
    result: str,
    now: datetime | None = None,
) -> bool:
    """Accept only the association's latest exact successful offer, including duplicate retries."""
    if result not in SUCCESS_RESULTS:
        return False
    begin_playback_write(db)
    device = _resolve_device(db, device_uid)
    active = db.get(MatterFrameTransfer, device.id)
    if active is None or active.transfer_id != transfer_id:
        return False
    offer = db.get(PlaybackOffer, active.playback_offer_id)
    if offer is None or offer.frame_id != frame_id or offer.id != _latest_offer_id(db, device.id):
        return False
    playback = db.get(Playback, device.id)
    if playback is None or (
        playback.current_offer_id is not None and playback.current_offer_id > offer.id
    ):
        return False
    if active.acknowledged_at is not None:
        return True

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
    active.acknowledged_at = now_utc
    playback.current_offer_id = offer.id
    device.last_frame_id = offer.frame_id
    device.last_refresh_result = "displayed_hub_frame"
    device.last_seen_at = now_utc
    db.add(
        DeviceStatusHistory(
            device_id=device.id,
            battery_percent=device.battery_percent,
            battery_voltage=device.battery_voltage,
            status="displayed_hub_frame",
            detail_json=json.dumps(
                {"frame_id": frame_id, "transfer_id": transfer_id, "transport": "matter"}
            ),
        )
    )
    db.flush()
    return True


def _resolve_device(db: Session, device_uid: str) -> Device:
    device = db.scalar(select(Device).where(Device.device_uid == device_uid))
    if device is None:
        raise MatterFrameError("This Matter association has no registered display.")
    return device


def _latest_offer_id(db: Session, device_id: int) -> int | None:
    return db.scalar(
        select(PlaybackOffer.id)
        .where(PlaybackOffer.device_id == device_id)
        .order_by(PlaybackOffer.id.desc())
        .limit(1)
    )


def _validate_frame_metadata(frame: Frame) -> None:
    if (
        frame.width != FRAME_WIDTH
        or frame.height != FRAME_HEIGHT
        or frame.size_bytes != FRAME_SIZE
        or frame.palette != WAVESHARE_7COLOR_6.name
        or frame.packing != WAVESHARE_7COLOR_6.packing
        or frame.bits_per_pixel != WAVESHARE_7COLOR_6.bits_per_pixel
        or frame.byte_order != "row_major"
    ):
        raise MatterFrameError("The prepared frame does not match the Spectra 6 panel format.")


def _read_verified_bytes(frame: Frame) -> bytes:
    data = read_frame_bytes(frame)
    if data is None:
        raise _FramePayloadError("Could not read the prepared frame.")
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != FRAME_SIZE or digest != frame.sha256 or digest[:24] != frame.frame_id:
        raise _FramePayloadError("The prepared frame failed its integrity check.")
    return data
