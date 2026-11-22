from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from inkdaddy_hub.api import matter_frames
from inkdaddy_hub.config import HubSettings
from inkdaddy_hub.database import Base, SessionLocal
from inkdaddy_hub.main import app
from inkdaddy_hub.models import (
    Device, DeviceStatusHistory, Frame, MatterFrameBinding, MatterPlaybackIntent, MatterFrameTransfer,
    Playback, PlaybackOffer,
)
from inkdaddy_hub.services import ha_matter_share, matter_delivery, matter_frame_broker, playback
from inkdaddy_hub.services.matter_controller import MatterControllerError
from inkdaddy_hub.services.matter_delivery import (
    MISSING_RECEIPT_RECHECK_SECONDS,
    deliver_frame,
    mirror_playback,
    sync_actions,
)
from inkdaddy_hub.services.udp_frame_transfer import FrameTransferRequest, handle_frame_transfer_request
from inkdaddy_hub.services.udp_heartbeat import ParsedHeartbeat, record_udp_heartbeat


class ControllerStub:
    def __init__(self, identity):
        self.identity = identity
        self.sent = []
        self.state = "queued"
        self.wrong_id = False
        self.mirrored = []
        self.radio_receipts = []

    def commission(self, code, *, expected_uid=None):
        assert code == "12345678901"
        return self.identity

    def send(self, node_id, transfer_id, payload, digest):
        assert len(payload) == 192000
        assert hashlib.sha256(payload).hexdigest() == digest
        assert digest[:24] != transfer_id
        self.sent.append((node_id, transfer_id))
        return {"transfer_id": transfer_id, "state": "queued"}

    def receipt(self, node_id, transfer_id):
        self.radio_receipts.append((node_id, transfer_id))
        return self.cached_receipt(node_id, transfer_id)

    def cached_receipt(self, node_id, transfer_id):
        assert self.sent[-1] == (node_id, transfer_id)
        return {
            "transfer_id": "0" * 24 if self.wrong_id else transfer_id,
            "state": self.state,
            "battery_percent": 74,
        }

    def set_playback(self, target, playing):
        self.mirrored.append((target, playing))


def test_binding_uses_identity_from_the_authenticated_controller(monkeypatch):
    uid = f"xiao-mg24-{uuid4().hex[:8]}"
    node = str(uuid4().int >> 96)
    controller = ControllerStub({"node_id": node, "device_uid": None})
    monkeypatch.setattr(matter_frames, "get_controller", lambda: controller)

    with TestClient(app) as client:
        bad = client.post("/api/matter-frames/pair", json={"code": "unexpected-secret"})
        assert bad.status_code == 422
        assert "unexpected-secret" not in bad.text
        pending = client.post("/api/matter-frames/pair", json={"code": "1234-567-8901", "device_uid": uid})
        assert pending.json() == {"node_id": node, "device_uid": None, "linked": False}
        controller.identity["device_uid"] = uid
        linked = client.post("/api/matter-frames/pair", json={"code": "1234-567-8901"})
        assert linked.json() == {"node_id": node, "device_uid": uid, "linked": True}
        assert client.get(f"/api/devices/{uid}").json()["provisioned"]


@pytest.mark.parametrize("returned_uid", ["xiao-mg24-abcdef01", None])
def test_native_share_matches_the_selected_uid_and_serializes_requests(monkeypatch, returned_uid):
    controller = ControllerStub({"node_id": "12345", "device_uid": returned_uid})
    commissioned = []

    def commission(code, *, expected_uid=None):
        commissioned.append(expected_uid)
        assert expected_uid == "xiao-mg24-abcdef02"
        return controller.identity

    monkeypatch.setattr(controller, "commission", commission)
    monkeypatch.setattr(matter_frames, "get_controller", lambda: controller)
    monkeypatch.setattr(matter_frames, "supervisor_connection", lambda: type("Managed", (), {"token": "supervisor"})())
    device_id = "0" * 32
    opened = []

    def share(requested_id):
        assert requested_id == device_id
        opened.append(requested_id)
        return ("12345678901", "xiao-mg24-abcdef02")

    monkeypatch.setattr(ha_matter_share, "open_inkdaddy_share", share)
    with TestClient(app) as client:
        with matter_frames._pairing():
            busy = client.post("/api/matter-frames/pair-home-assistant", json={"device_id": device_id})
            assert busy.status_code == 409 and not opened
        wrong = client.post("/api/matter-frames/pair-home-assistant", json={"device_id": device_id})
        assert wrong.status_code == 409 and opened == [device_id]
        assert commissioned == ["xiao-mg24-abcdef02"]
        assert "12345678901" not in wrong.text


@pytest.fixture
def linked_display():
    uid = f"xiao-mg24-{uuid4().hex[:8]}"
    node = str(uuid4().int >> 96)
    identity = {"node_id": node, "device_uid": uid}
    with SessionLocal() as db:
        assert matter_frames._bind(db, identity)["linked"]
        device = db.scalar(select(Device).where(Device.device_uid == uid))
        assert device is not None
        return device.id, ControllerStub(identity)


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("initial", ["actions", "send", "mirror"]),
        ("prepare_error", ["actions", "prepare", "mirror"]),
        ("confirmed", ["actions", "mirror", "send"]),
        ("paused", ["actions", "send", "mirror"]),
    ],
)
def test_delivery_worker_prioritizes_the_first_frame(tmp_path, monkeypatch, delivery_clock, case, expected):
    engine = create_engine(f"sqlite:///{tmp_path / 'delivery.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(matter_delivery, "SessionLocal", sessions)
    uid, node = "xiao-mg24-abcdef01", "123"
    controller = ControllerStub({"device_uid": uid, "node_id": node})

    with sessions() as db:
        assert matter_frames._bind(db, controller.identity)["linked"]
        device_id = db.scalar(select(Device.id).where(Device.device_uid == uid))
        assert device_id is not None
        if case == "paused":
            playback.apply_playback_action(db, db.get(Device, device_id), "pause")
            matter_delivery.queue_playback_mirror(db, device_id, False)
            db.commit()

    if case == "confirmed":
        offer = deliver_frame(controller, device_id, node, None, now=0)
        controller.state = "displayed"
        assert deliver_frame(controller, device_id, node, offer, now=5) is None
        with sessions() as db:
            playback.begin_playback_write(db)
            playback.apply_playback_action(db, db.get(Device, device_id), "refresh_current")
            db.commit()

    calls = []
    original_send = controller.send

    def actions(target, after):
        assert target == node and after == "0"
        calls.append("actions")
        return [{"event_number": "1", "action": "next_content"}]

    def send(target, transfer_id, payload, digest):
        calls.append("send")
        with sessions() as db:
            assert db.get(MatterFrameBinding, device_id).last_action_number == "1"
            schedule = db.get(Playback, device_id)
            assert schedule.anchor_position == 1
            assert schedule.paused is (case == "paused")
        return original_send(target, transfer_id, payload, digest)

    def mirror(target, playing):
        assert target == node and playing is (case != "paused")
        calls.append("mirror")
        if case == "initial":
            raise MatterControllerError("The playback switch timed out.")

    def broken_frame(_db, _uid, **_options):
        calls.append("prepare")
        raise matter_frame_broker.MatterFrameError("The prepared frame is unavailable.")

    monkeypatch.setattr(controller, "actions", actions, raising=False)
    monkeypatch.setattr(controller, "send", send)
    monkeypatch.setattr(controller, "set_playback", mirror)
    if case == "prepare_error":
        monkeypatch.setattr(matter_delivery, "prepare_matter_frame", broken_frame)

    stop = threading.Event()
    monkeypatch.setattr(stop, "wait", lambda _timeout: stop.set())
    matter_delivery.run_matter_delivery(stop, controller)
    assert calls == expected
    with sessions() as db:
        assert (db.get(MatterPlaybackIntent, device_id) is not None) is (case == "initial")


def test_current_and_battery_follow_only_the_exact_display_receipt(linked_display):
    device_id, controller = linked_display
    node = controller.identity["node_id"]
    attempt = deliver_frame(controller, device_id, node, None, now=0)
    assert attempt is not None and attempt.submitted
    controller.state = "displayed"
    controller.wrong_id = True
    attempt = deliver_frame(controller, device_id, node, attempt, now=6)
    with SessionLocal() as db:
        assert db.get(Device, device_id).last_frame_id is None
    controller.wrong_id = False
    assert deliver_frame(controller, device_id, node, attempt, now=20) is None
    with SessionLocal() as db:
        device = db.get(Device, device_id)
        assert device.last_frame_id and device.mesh_connected
        assert device.battery_percent == 74
    assert deliver_frame(controller, device_id, node, None, now=30) is None
    assert len(controller.sent) == 1
    assert controller.radio_receipts == []


@pytest.mark.parametrize("operation", ["frame", "playback"])
def test_transport_backoff_starts_after_the_exchange_finishes(linked_display, monkeypatch, operation):
    device_id, controller = linked_display
    node = controller.identity["node_id"]
    clock = [0.0]
    calls = []

    def slow_timeout(*_args):
        calls.append(clock[0])
        clock[0] += 24
        raise MatterControllerError("Synthetic timeout.")

    monkeypatch.setattr(matter_delivery, "monotonic", lambda: clock[0])
    if operation == "frame":
        run = deliver_frame
        monkeypatch.setattr(controller, "send", slow_timeout)
    else:
        run = mirror_playback
        monkeypatch.setattr(controller, "set_playback", slow_timeout)
        with SessionLocal() as db:
            matter_delivery.queue_playback_mirror(db, device_id, False)
            db.commit()

    attempt = run(controller, device_id, node, None)
    assert attempt.next_attempt == 29
    clock[0] = 28
    assert run(controller, device_id, node, attempt) is attempt
    clock[0] = 29
    attempt = run(controller, device_id, node, attempt)
    assert calls == [0, 29]
    assert attempt.next_attempt == 63


def test_missing_event_uses_a_bounded_radio_receipt_check(linked_display):
    device_id, controller = linked_display
    node = controller.identity["node_id"]
    deadline = MISSING_RECEIPT_RECHECK_SECONDS
    attempt = deliver_frame(controller, device_id, node, None, now=0)
    assert attempt is not None
    for now in (5, 50, deadline - 5, deadline):
        attempt = deliver_frame(controller, device_id, node, attempt, now=now)
    assert attempt is not None and attempt.submitted
    assert controller.radio_receipts == [(node, attempt.transfer_id)]
    assert len(controller.sent) == 1

    controller.state = "displayed"
    assert deliver_frame(controller, device_id, node, attempt, now=deadline + 5) is None
    assert len(controller.radio_receipts) == 1


@pytest.mark.parametrize("repairable", [True, False])
def test_only_frame_sends_read_and_verify_payload(delivery_clock, monkeypatch, tmp_path, repairable):
    engine = create_engine(f"sqlite:///{tmp_path / 'delivery.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(matter_delivery, "SessionLocal", sessions)
    monkeypatch.setattr(playback, "get_settings", lambda: HubSettings(data_dir=tmp_path))
    uid, node = "xiao-mg24-abcdef01", "123"
    controller = ControllerStub({"device_uid": uid, "node_id": node})
    with sessions() as db:
        assert matter_frames._bind(db, controller.identity)["linked"]
        device_id = db.scalar(select(Device.id).where(Device.device_uid == uid))
        assert device_id is not None

    reads = []
    read = matter_frame_broker.read_frame_bytes
    send = controller.send

    def read_asset(frame):
        reads.append(frame.frame_id)
        return read(frame)

    def timeout_first_send(*args):
        result = send(*args)
        if len(controller.sent) == 1:
            raise MatterControllerError("Synthetic timeout.")
        return result

    monkeypatch.setattr(matter_frame_broker, "read_frame_bytes", read_asset)
    monkeypatch.setattr(controller, "send", timeout_first_send)
    attempt = deliver_frame(controller, device_id, node, None, now=0)
    assert attempt is not None and not attempt.submitted
    assert len(reads) == 1
    with sessions() as db:
        transfer = db.get(MatterFrameTransfer, device_id)
        offer = db.get(PlaybackOffer, transfer.playback_offer_id)
        frame = db.scalar(select(Frame).where(Frame.frame_id == offer.frame_id))
        path = Path(frame.path)
        prior_payload = path.read_bytes()
        if not repairable:
            # A file with the wrong full SHA cannot be sent or regenerated at a foreign path.
            wrong_path = tmp_path / "unmanaged.raw"
            wrong_path.write_bytes(bytes([prior_payload[0] ^ 1]) + prior_payload[1:])
            frame.path = str(wrong_path)
            db.commit()
    path.unlink()

    assert deliver_frame(controller, device_id, node, attempt, now=3) is attempt
    assert len(reads) == 1
    if not repairable:
        with pytest.raises(matter_frame_broker.MatterFrameError, match="repair"):
            deliver_frame(controller, device_id, node, attempt, now=5)
        assert controller.sent == [(node, attempt.transfer_id)]
        assert not path.exists()
        return

    attempt = deliver_frame(controller, device_id, node, attempt, now=5)
    assert attempt is not None and attempt.submitted
    assert controller.sent == [(node, attempt.transfer_id)] * 2
    assert path.read_bytes() == prior_payload
    assert len(reads) == 3  # One read per send plus verification after repair.
    path.unlink()
    attempt = deliver_frame(controller, device_id, node, attempt, now=10)
    assert attempt is not None and attempt.submitted and len(reads) == 3
    controller.state = "displayed"
    assert deliver_frame(controller, device_id, node, attempt, now=15) is None
    assert deliver_frame(controller, device_id, node, None, now=18) is None
    assert len(reads) == 3
    assert not path.exists()


def test_a_cached_unknown_does_not_resend_until_the_device_confirms_unknown(linked_display):
    device_id, controller = linked_display
    node = controller.identity["node_id"]
    deadline = MISSING_RECEIPT_RECHECK_SECONDS
    attempt = deliver_frame(controller, device_id, node, None, now=0)
    assert attempt is not None
    transfer_id = attempt.transfer_id
    controller.state = "unknown"

    attempt = deliver_frame(controller, device_id, node, attempt, now=deadline - 5)
    assert attempt is not None and attempt.submitted
    assert controller.sent == [(node, transfer_id)] and controller.radio_receipts == []

    attempt = deliver_frame(controller, device_id, node, attempt, now=deadline)
    assert attempt is not None and not attempt.submitted
    assert controller.radio_receipts == [(node, transfer_id)]
    attempt = deliver_frame(controller, device_id, node, attempt, now=deadline + 5)
    assert attempt is not None and attempt.submitted
    assert controller.sent == [(node, transfer_id), (node, transfer_id)]


def test_a_failed_panel_exchange_retries_with_a_new_transfer(linked_display):
    device_id, controller = linked_display
    node = controller.identity["node_id"]
    attempt = deliver_frame(controller, device_id, node, None, now=0)
    controller.state = "failed"
    attempt = deliver_frame(controller, device_id, node, attempt, now=5)
    attempt = deliver_frame(controller, device_id, node, attempt, now=19)
    assert len(controller.sent) == 1
    attempt = deliver_frame(controller, device_id, node, attempt, now=20)
    assert controller.sent[0][1] != controller.sent[1][1]
    assert attempt is not None and attempt.submitted


@pytest.fixture
def delivery_clock(monkeypatch):
    clock = [datetime(2026, 11, 20, 19, 5, tzinfo=timezone.utc)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(clock[0].timestamp(), tz)

    monkeypatch.setattr(matter_frame_broker, "datetime", Clock)
    monkeypatch.setattr(playback, "_utc_now", lambda: clock[0])
    return clock


def _submit_third_attempt(controller, device_id, node, start):
    attempt = deliver_frame(controller, device_id, node, None, now=start)
    controller.state = "failed"
    for step in (5, 20, 25, 55):
        attempt = deliver_frame(controller, device_id, node, attempt, now=start + step)
    assert attempt is not None and attempt.submitted and attempt.panel_failures == 2
    return attempt


@pytest.mark.parametrize("recovery", ["refresh", "next_hour", "late_success"])
def test_terminal_failure_is_exact_and_survives_worker_restart(linked_display, delivery_clock, recovery):
    device_id, controller = linked_display
    node = controller.identity["node_id"]
    device_url = f"/api/devices/{controller.identity['device_uid']}"
    with SessionLocal() as db:
        db.get(Device, device_id).battery_percent = 74
        db.commit()
    with TestClient(app) as client:
        attempt = deliver_frame(controller, device_id, node, None, now=0)
        controller.state = "displayed"
        assert deliver_frame(controller, device_id, node, attempt, now=5) is None
        confirmed = client.get(f"{device_url}/playback").json()
        assert confirmed["pending_status"] is None and confirmed["pending"] is None

        assert client.post(f"{device_url}/manual-refresh?action=refresh_current").status_code == 200
        attempt = _submit_third_attempt(controller, device_id, node, 10)
        pending = client.get(f"{device_url}/playback").json()
        assert pending["pending_status"] == "waiting"
        assert pending["pending"]["frame_id"] == confirmed["current"]["frame_id"]

        failed = deliver_frame(controller, device_id, node, attempt, now=70)
        assert failed is not None and failed.next_attempt == float("inf")
        state = client.get(f"{device_url}/playback").json()
        assert state["pending_status"] == "failed"
        assert state["pending"] == pending["pending"]
        assert state["current"] == confirmed["current"]

        with SessionLocal() as db:
            failures = db.scalars(
                select(DeviceStatusHistory)
                .where(
                    DeviceStatusHistory.device_id == device_id,
                    DeviceStatusHistory.status == playback.MATTER_FRAME_FAILED,
                )
            ).all()
            assert len(failures) == 1
            assert json.loads(failures[0].detail_json)["transfer_id"] == failed.transfer_id

        restarted = ControllerStub(controller.identity)
        stopped = deliver_frame(restarted, device_id, node, None, now=300)
        assert stopped is not None and stopped.next_attempt == float("inf")
        assert restarted.sent == [] and restarted.radio_receipts == []
        assert client.get(f"{device_url}/playback").json()["pending_status"] == "failed"

        if recovery == "late_success":
            with SessionLocal() as db:
                assert matter_frame_broker.record_matter_frame_receipt(
                    db, controller.identity["device_uid"], transfer_id=failed.transfer_id,
                    frame_id=state["pending"]["frame_id"], result="displayed_hub_frame",
                )
                db.commit()
            confirmed_again = client.get(f"{device_url}/playback").json()
            assert confirmed_again["pending"] is None and confirmed_again["pending_status"] is None
            return

        if recovery == "refresh":
            assert client.post(f"{device_url}/manual-refresh?action=refresh_current").status_code == 200
        else:
            delivery_clock[0] += timedelta(hours=1)

        replacement = deliver_frame(restarted, device_id, node, stopped, now=305)
        assert replacement is not None and replacement.submitted
        assert replacement.transfer_id != failed.transfer_id
        assert client.get(f"{device_url}/playback").json()["pending_status"] == "waiting"


@pytest.mark.parametrize("supersession", ["refresh", "next_hour"])
def test_late_failure_cannot_mark_a_replaced_offer(linked_display, delivery_clock, monkeypatch, supersession):
    device_id, controller = linked_display
    node = controller.identity["node_id"]
    device_url = f"/api/devices/{controller.identity['device_uid']}"
    with TestClient(app) as client:
        attempt = _submit_third_attempt(controller, device_id, node, 0)
        original_receipt = controller.cached_receipt

        def receipt_after_supersession(target, transfer_id):
            if supersession == "refresh":
                assert client.post(f"{device_url}/manual-refresh?action=refresh_current").status_code == 200
            else:
                delivery_clock[0] += timedelta(hours=1)
            return original_receipt(target, transfer_id)

        monkeypatch.setattr(controller, "cached_receipt", receipt_after_supersession)
        stale = deliver_frame(controller, device_id, node, attempt, now=60)
        with SessionLocal() as db:
            assert not db.scalar(select(DeviceStatusHistory.id).where(
                DeviceStatusHistory.device_id == device_id,
                DeviceStatusHistory.status == playback.MATTER_FRAME_FAILED,
            ))
        fresh = deliver_frame(controller, device_id, node, stale, now=65)
        assert fresh is not None and fresh.submitted and fresh.transfer_id != attempt.transfer_id
        assert client.get(f"{device_url}/playback").json()["pending_status"] == "waiting"


def test_secure_link_excludes_legacy_panel_writes(linked_display):
    device_id, controller = linked_display
    uid = controller.identity["device_uid"]
    for kind in ("manifest_request", "chunk_request", "refresh_result"):
        with SessionLocal() as db:
            response = handle_frame_transfer_request(
                db, FrameTransferRequest(request_type=kind, device_uid=uid), HubSettings(),
            )
            assert b"error=matter_transport_required" in response
    record_udp_heartbeat(ParsedHeartbeat(device_uid=uid, battery_percent=5))
    with SessionLocal() as db:
        assert db.get(Device, device_id).battery_percent is None


def test_matter_switch_replay_does_not_reapply_the_same_action(linked_display, monkeypatch):
    device_id, controller = linked_display
    node = controller.identity["node_id"]
    events = [
        {"event_number": "18446744073709551615", "action": "pause"},
        {"event_number": "18446744073709551614", "action": "play"},
    ]
    requested = []

    def actions(target, after):
        assert target == node
        requested.append(after)
        return events

    monkeypatch.setattr(controller, "actions", actions, raising=False)
    mirror_playback(controller, device_id, node, None, now=0)
    sync_actions(controller, device_id, node)
    sync_actions(controller, device_id, node)
    with SessionLocal() as db:
        assert db.get(MatterFrameBinding, device_id).last_action_number == "18446744073709551615"
        assert db.get(Playback, device_id).paused
    assert requested == ["0", "18446744073709551615"]


def test_app_playback_mirror_keeps_newer_intent_during_an_inflight_write(linked_display, monkeypatch):
    device_id, controller = linked_display
    node, uid = controller.identity["node_id"], controller.identity["device_uid"]
    target = f"/api/devices/{uid}/manual-refresh"
    with TestClient(app) as client:
        def superseded_write(target_node, playing):
            assert target_node == node and playing is True
            response = client.post(target + "?action=pause")
            assert response.json()["playback"]["syncing_controls"] is True

        with monkeypatch.context() as patch:
            patch.setattr(controller, "set_playback", superseded_write)
            mirror_playback(controller, device_id, node, None, now=0)

        with SessionLocal() as db:
            assert db.get(MatterPlaybackIntent, device_id).playing is False
            assert db.get(Playback, device_id).paused

        controller.actions = lambda *_: [{"event_number": "1", "action": "play"}]
        sync_actions(controller, device_id, node)
        with SessionLocal() as db:
            assert db.get(Playback, device_id).paused
            assert db.get(MatterFrameBinding, device_id).last_action_number == "1"
        mirror_playback(controller, device_id, node, None, now=5)
        assert controller.mirrored == [(node, False)]

        controller.actions = lambda *_: [{"event_number": "2", "action": "pause"}]
        sync_actions(controller, device_id, node)
        state = client.get(f"/api/devices/{uid}/playback").json()
        assert state["paused"] is True and state["syncing_controls"] is False
        assert mirror_playback(controller, device_id, node, None, now=10) is None
        assert controller.mirrored == [(node, False)]
