from __future__ import annotations

import logging
import secrets
import threading
from dataclasses import dataclass, replace
from time import monotonic

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import Device, MatterFrameBinding, MatterPlaybackIntent, Playback
from .matter_controller import MatterController, MatterControllerError, event_number
from .matter_frame_broker import (
    MatterFrameError,
    load_matter_frame_payload,
    prepare_matter_frame,
    record_matter_frame_receipt,
)
from .playback import (
    ACTION_NAMES,
    apply_playback_action,
    begin_playback_write,
    invalidate_matter_transfer,
    matter_transfer_failed,
    record_matter_transfer_failure,
)

LOGGER = logging.getLogger(__name__)
# The panel can remain busy for two minutes. Completion events normally
# arrive much sooner and keep receipt checks inside the controller process.
MISSING_RECEIPT_RECHECK_SECONDS = 120


def _time(now: float | None) -> float:
    return monotonic() if now is None else now


@dataclass
class DeliveryAttempt:
    transfer_id: str
    submitted: bool = False
    next_attempt: float = 0
    retry_seconds: float = 5
    panel_failures: int = 0
    renew_transfer: bool = False
    next_radio_check: float = 0

    def retry_later(self, now: float) -> None:
        self.next_attempt = now + self.retry_seconds
        self.retry_seconds = min(self.retry_seconds * 2, 60)


@dataclass
class PlaybackAttempt:
    revision: str
    next_attempt: float = 0
    retry_seconds: float = 5


def queue_playback_mirror(db: Session, device_id: int, playing: bool) -> None:
    intent = db.get(MatterPlaybackIntent, device_id)
    if intent is not None and intent.playing == playing:
        return
    if intent is None:
        intent = MatterPlaybackIntent(device_id=device_id)
        db.add(intent)
    intent.revision = secrets.token_hex(16)
    intent.playing = playing


def mirror_playback(
    controller: MatterController,
    device_id: int,
    target: str,
    attempt: PlaybackAttempt | None,
    *,
    now: float | None = None,
) -> PlaybackAttempt | None:
    with SessionLocal() as db:
        binding = db.get(MatterFrameBinding, device_id)
        intent = db.get(MatterPlaybackIntent, device_id)
        if not binding or binding.node_id != target or not intent:
            return None
        revision, playing = intent.revision, intent.playing
    if attempt is None or attempt.revision != revision:
        attempt = PlaybackAttempt(revision)
    if attempt.next_attempt > _time(now):
        return attempt
    try:
        controller.set_playback(target, playing)
    except MatterControllerError:
        attempt.next_attempt = _time(now) + attempt.retry_seconds
        attempt.retry_seconds = min(attempt.retry_seconds * 2, 60)
        return attempt
    with SessionLocal() as db:
        begin_playback_write(db)
        binding = db.get(MatterFrameBinding, device_id)
        intent = db.get(MatterPlaybackIntent, device_id)
        if binding and binding.node_id == target and intent and intent.revision == revision:
            db.delete(intent)
            db.commit()
    return None


def sync_actions(controller: MatterController, device_id: int, target: str) -> None:
    with SessionLocal() as db:
        binding = db.get(MatterFrameBinding, device_id)
        if binding is None or binding.node_id != target:
            return
        after = binding.last_action_number
    try:
        reported = controller.actions(target, after)
        events = [
            (event_number(entry.get("event_number")), entry.get("action")) for entry in reported
        ]
        events.sort(key=lambda event: event[0])
    except (MatterControllerError, ValueError, TypeError):
        return
    if not events:
        return
    with SessionLocal() as db:
        begin_playback_write(db)
        binding = db.get(MatterFrameBinding, device_id)
        device = db.get(Device, device_id)
        if binding is None or binding.node_id != target or device is None:
            return
        pending_mirror = db.get(MatterPlaybackIntent, device_id) is not None
        last = event_number(binding.last_action_number)
        for number, action in events:
            if number > last and isinstance(action, str) and action in ACTION_NAMES:
                if not (pending_mirror and action in {"play", "pause"}):
                    apply_playback_action(db, device, action)
                last = number
        binding.last_action_number = str(last)
        db.commit()


def deliver_frame(
    controller: MatterController,
    device_id: int,
    target: str,
    attempt: DeliveryAttempt | None,
    *,
    now: float | None = None,
) -> DeliveryAttempt | None:
    with SessionLocal() as db:
        begin_playback_write(db)
        device = _bound_device(db, device_id, target)
        if device is None:
            return None
        uid = device.device_uid
        offer = prepare_matter_frame(db, uid, include_payload=False)
        if (
            attempt is not None and attempt.transfer_id == offer.transfer_id
            and attempt.renew_transfer and _time(now) >= attempt.next_attempt
        ):
            invalidate_matter_transfer(db, device_id)
            offer = prepare_matter_frame(db, uid, include_payload=False)
            attempt = replace(attempt, transfer_id=offer.transfer_id, submitted=False, renew_transfer=False)
        terminal_failure = (
            not offer.acknowledged
            and (attempt is None or attempt.transfer_id != offer.transfer_id)
            and matter_transfer_failed(db, device_id, offer.transfer_id)
        )
        if offer.acknowledged:
            db.commit()
            return None
        if terminal_failure:
            db.commit()
            return DeliveryAttempt(offer.transfer_id, next_attempt=float("inf"))
        if attempt is None or attempt.transfer_id != offer.transfer_id:
            attempt = DeliveryAttempt(offer.transfer_id)
        if attempt.next_attempt > _time(now):
            db.commit()
            return attempt
        payload = None if attempt.submitted else load_matter_frame_payload(db, device, offer)
        db.commit()

    try:
        if payload is None:
            response = _delivery_receipt(controller, target, attempt, now)
        else:
            response = controller.send(target, offer.transfer_id, payload, offer.sha256)
        state = response.get("state")
        if response.get("transfer_id") != offer.transfer_id:
            raise MatterControllerError("The Matter receipt did not match the pending transfer.")
    except MatterControllerError:
        attempt.retry_later(_time(now))
        return attempt

    if state == "displayed":
        with SessionLocal() as db:
            begin_playback_write(db)
            device = _bound_device(db, device_id, target)
            if device is not None:
                percent = response.get("battery_percent")
                if "battery_percent" in response and (percent is None or (
                    type(percent) in {int, float} and 0 <= percent <= 100
                )):
                    device.battery_percent = percent
                if record_matter_frame_receipt(
                    db, uid, transfer_id=offer.transfer_id, frame_id=offer.frame_id,
                    result="displayed_hub_frame",
                ):
                    device.mesh_connected = True
                    db.commit()
        return None
    completed = _time(now)
    if state == "queued":
        if not attempt.submitted:
            attempt.next_radio_check = completed + MISSING_RECEIPT_RECHECK_SECONDS
        attempt.submitted = True
        attempt.next_attempt = completed + 5
        attempt.retry_seconds = 5
    elif state == "failed":
        attempt.panel_failures += 1
        attempt.renew_transfer = True
        attempt.next_attempt = completed + 15 * attempt.panel_failures
        if attempt.panel_failures >= 3:
            attempt.next_attempt = float("inf")
            with SessionLocal() as db:
                begin_playback_write(db)
                device = _bound_device(db, device_id, target)
                if device is not None and record_matter_transfer_failure(db, device, offer.transfer_id):
                    db.commit()
    else:
        attempt.submitted = False
        attempt.retry_later(completed)
    return attempt


def _delivery_receipt(
    controller: MatterController, target: str, attempt: DeliveryAttempt, now: float | None
) -> dict[str, object]:
    response = controller.cached_receipt(target, attempt.transfer_id)
    if response.get("transfer_id") != attempt.transfer_id:
        raise MatterControllerError("The cached Matter receipt did not match the pending transfer.")
    if response.get("state") in ("displayed", "failed"):
        return response
    if _time(now) >= attempt.next_radio_check:
        try:
            return controller.receipt(target, attempt.transfer_id)
        finally:
            attempt.next_radio_check = _time(now) + MISSING_RECEIPT_RECHECK_SECONDS
    return {"transfer_id": attempt.transfer_id, "state": "queued"}


def run_matter_delivery(stop_event: threading.Event, controller: MatterController) -> None:
    attempts: dict[tuple[int, str], DeliveryAttempt] = {}
    playback_attempts: dict[tuple[int, str], PlaybackAttempt] = {}
    while not stop_event.is_set():
        try:
            with SessionLocal() as db:
                bindings = {
                    (device_id, target): current_offer_id
                    for device_id, target, current_offer_id in db.execute(
                        select(MatterFrameBinding.device_id, MatterFrameBinding.node_id, Playback.current_offer_id)
                        .outerjoin(Playback, Playback.device_id == MatterFrameBinding.device_id)
                    ).tuples()
                }
            attempts = {binding: attempts[binding] for binding in bindings if binding in attempts}
            playback_attempts = {
                binding: playback_attempts[binding] for binding in bindings if binding in playback_attempts
            }
            for binding, current_offer_id in bindings.items():
                if stop_event.is_set():
                    break
                sync_actions(controller, *binding)
                if current_offer_id is None:
                    _deliver_binding_frame(controller, binding, attempts)
                playback_attempt = mirror_playback(
                    controller, *binding, playback_attempts.get(binding)
                )
                if playback_attempt is None:
                    playback_attempts.pop(binding, None)
                else:
                    playback_attempts[binding] = playback_attempt
                if current_offer_id is not None:
                    _deliver_binding_frame(controller, binding, attempts)
        except SQLAlchemyError:
            LOGGER.exception("Could not update the Matter frame queue.")
        stop_event.wait(3)


def _deliver_binding_frame(
    controller: MatterController,
    binding: tuple[int, str],
    attempts: dict[tuple[int, str], DeliveryAttempt],
) -> None:
    try:
        attempt = deliver_frame(controller, *binding, attempts.get(binding))
    except MatterFrameError as exc:
        LOGGER.warning("Could not prepare a frame for Matter node %s. %s", binding[1], exc)
        return
    if attempt is None:
        attempts.pop(binding, None)
    else:
        attempts[binding] = attempt


def _bound_device(db: Session, device_id: int, target: str) -> Device | None:
    bound = db.get(MatterFrameBinding, device_id)
    if bound is not None and bound.node_id == target:
        return db.get(Device, device_id)
    return None
