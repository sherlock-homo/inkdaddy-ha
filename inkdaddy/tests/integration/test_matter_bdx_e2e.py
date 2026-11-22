"""Run after `npm ci --prefix matter_controller` with INKDADDY_RUN_BDX_TEST=1.

Set INKDADDY_TEST_NODE to an explicit Node executable if it is not on PATH.
Only an isolated SQLite catalog and loopback Matter fabrics take part.
"""

import hashlib
import io
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from inkdaddy_hub import worker
from inkdaddy_hub.api import dashboards, devices, matter_frames, photos
from inkdaddy_hub.config import HubSettings
from inkdaddy_hub.database import Base, get_db
from inkdaddy_hub.models import (
    Device,
    DeviceStatusHistory,
    Frame,
    HAEntityCache,
    MatterFrameBinding,
    MatterPlaybackIntent,
)
from inkdaddy_hub.services import matter_delivery, playback
from inkdaddy_hub.services.dashboard_content import (
    dashboard_config,
    get_dashboard_record,
    render_dashboard,
    resolve_widget_text,
)
from inkdaddy_hub.services.matter_controller import MatterController
from inkdaddy_hub.services.matter_frame_broker import (
    prepare_matter_frame,
    record_matter_frame_receipt,
)
from inkdaddy_hub.services.palette import WAVESHARE_7COLOR_6, raw_frame_from_image

pytestmark = pytest.mark.skipif(
    os.getenv("INKDADDY_RUN_BDX_TEST") != "1",
    reason="Set INKDADDY_RUN_BDX_TEST=1 to run the real loopback CASE and BDX peer.",
)
NOW = datetime(2026, 11, 18, 16, 30, tzinfo=timezone.utc)
PEER = Path(__file__).resolve().parents[2] / "matter_controller" / "fixtures" / "playback_peer.mjs"
PREFIX = "INKDADDY_E2E "


class SoftwarePeer:
    def __init__(self, process, log):
        self.process = process
        self.log = log
        self.messages = queue.Queue()
        self.reader = threading.Thread(target=self.read_stdout, daemon=True)
        self.reader.start()

    def read_stdout(self):
        for line in self.process.stdout:
            if line.startswith(PREFIX):
                self.messages.put(json.loads(line.removeprefix(PREFIX)))
        self.messages.put(None)

    def receive(self):
        try:
            message = self.messages.get(timeout=35)
        except queue.Empty:
            pytest.fail(f"Software Matter peer timed out.\n{self.log.read_text()[-3000:]}")
        if message is None:
            pytest.fail(f"Software Matter peer exited.\n{self.log.read_text()[-3000:]}")
        assert "error" not in message, message.get("error")
        return message

    def call(self, action, **fields):
        self.process.stdin.write(json.dumps({"action": action, **fields}) + "\n")
        self.process.stdin.flush()
        return self.receive()


@pytest.fixture
def peer(tmp_path, request):
    node = os.getenv("INKDADDY_TEST_NODE") or shutil.which("node")
    if not node:
        pytest.fail("Select Node 22.13 or newer with INKDADDY_TEST_NODE.")
    if not PEER.is_file():
        pytest.fail("The Matter software peer fixture is missing.")
    store = tmp_path / "matter"
    store.mkdir()
    log = tmp_path / "matter-peer.log"
    with log.open("w") as errors:
        process = subprocess.Popen(
            [node, str(PEER), str(store), str(getattr(request, "param", 2))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            encoding="utf-8",
        )
        fixture = SoftwarePeer(process, log)
        try:
            yield fixture, fixture.receive()
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=4)
            fixture.reader.join(timeout=2)
            process.stdout.close()


@pytest.fixture
def app_catalog(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'acceptance.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    settings = HubSettings(data_dir=tmp_path / "hub")
    clock = SimpleNamespace(now=NOW)
    monkeypatch.setattr("inkdaddy_hub.services.photos.get_settings", lambda: settings)
    monkeypatch.setattr(playback, "get_settings", lambda: settings)
    monkeypatch.setattr(playback, "_utc_now", lambda: clock.now)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(matter_delivery, "SessionLocal", sessions)
    monkeypatch.setattr(
        matter_delivery,
        "prepare_matter_frame",
        lambda db, uid, **options: prepare_matter_frame(db, uid, now=clock.now, **options),
    )
    app = FastAPI()
    for module in (dashboards, devices, matter_frames, photos):
        app.include_router(module.router)

    def isolated_db():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = isolated_db
    with TestClient(app) as client:
        yield client, sessions, clock
    engine.dispose()


@pytest.mark.parametrize(
    ("peer", "packing"), [(2, None), (3, 1)], indirect=["peer"],
    ids=["revision-2-raw-share-code", "revision-3-packbits-native-share"],
)
def test_first_frame_for_new_link_uses_local_home_overview(peer, packing, app_catalog, monkeypatch):
    from inkdaddy_hub.main import lifespan
    from inkdaddy_hub.services import ha_matter_share

    software, ready = peer
    client, sessions, clock = app_catalog
    controller = MatterController(ready["port"], ready["token"])
    monkeypatch.setenv("INKDADDY_MATTER_PORT", str(ready["port"]))
    monkeypatch.setenv("INKDADDY_MATTER_TOKEN", ready["token"])
    monkeypatch.setenv("INKDADDY_HA_MODE", "supervisor")
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-supervisor-token")

    if packing is None:
        link = response_json(client.post("/api/matter-frames/pair", json={"code": ready["share_code"]}))
    else:
        home_assistant_id = "a" * 32

        def open_share(device_id):
            assert device_id == home_assistant_id
            return ready["share_code"], ready["device_uid"]

        monkeypatch.setattr(ha_matter_share, "open_inkdaddy_share", open_share)
        link = response_json(
            client.post("/api/matter-frames/pair-home-assistant", json={"device_id": home_assistant_id})
        )
    uid, target = link["device_uid"], link["node_id"]
    assert link["linked"] and uid == ready["device_uid"]
    route = f"/api/devices/{uid}/playback"
    with sessions() as db:
        device_id = db.scalar(select(Device.id).where(Device.device_uid == uid))
    assert response_json(client.get(route))["playlist"] == []

    metadata_requested = threading.Event()
    metadata_ready = threading.Event()
    started_delivery = threading.Event()
    first_offer = threading.Event()
    attempts = []
    run_delivery = matter_delivery.run_matter_delivery
    deliver_frame = matter_delivery.deliver_frame

    def fetch_states(_url, _token):
        metadata_requested.set()
        assert metadata_ready.wait(10)
        return [
            worker.home_assistant.HAEntity(
                "weather.archived_forecast", "weather", "Archived Forecast", "unavailable", {},
            ),
            worker.home_assistant.HAEntity(
                "weather.test_house", "weather", "Forecast", "partlycloudy",
                {"temperature": 16, "temperature_unit": "°C"},
                reported_at=clock.now.replace(tzinfo=None),
            )
        ]

    def run_worker(_stop, *, refresh_at_start):
        assert not refresh_at_start
        worker.run_playback_cycle(now=clock.now)

    def observe_worker(stop, active_controller):
        started_delivery.set()
        run_delivery(stop, active_controller)

    def observe_offer(*args, **kwargs):
        attempt = deliver_frame(*args, **kwargs)
        if attempt is not None and attempt.submitted and not attempts:
            attempts.append(attempt)
            first_offer.set()
        return attempt

    monkeypatch.setattr(worker.home_assistant, "fetch_states", fetch_states)
    monkeypatch.setattr(
        worker.home_assistant, "fetch_site",
        lambda _connection: {"time_zone": "America/New_York", "location_name": "Test House"},
    )
    monkeypatch.setattr(worker, "run", run_worker)
    monkeypatch.setattr(matter_delivery, "run_matter_delivery", observe_worker)
    monkeypatch.setattr(matter_delivery, "deliver_frame", observe_offer)

    with TestClient(FastAPI(lifespan=lifespan)):
        try:
            assert metadata_requested.wait(5), "Managed startup did not request Home Assistant metadata"
            assert not started_delivery.wait(0.15), "Frame delivery started against an empty HA cache"
        finally:
            metadata_ready.set()
        assert started_delivery.wait(5)
        assert first_offer.wait(12), "The delivery worker did not queue the first dashboard"

        (attempt,) = attempts
        state = response_json(client.get(route))
        assert state["current"] is None
        assert state["playlist"] == []
        assert state["pending"]["source_id"] == "sample"
        assert state["pending"]["title"] == "Home Overview"
        assert [slot["source_id"] for slot in state["upcoming"]] == ["sample"] * 5
        with sessions() as db:
            config = dashboard_config(None)
            widgets = resolve_widget_text(
                db, config, NOW.replace(minute=0), db.get(Device, device_id), observed_at=clock.now
            )
            assert [widget.value for widget in widgets[:3]] == ["11:00", "Partly cloudy", "Test House"]
            assert widgets[1].detail == "16 °C"
            expected, _digest = raw_frame_from_image(render_dashboard(config, widgets), dither=True)
        expect_download(software, attempt.transfer_id, expected, packing=packing)
        software.call("display", transfer_id=attempt.transfer_id)
        wait_for_cached_receipt(controller, target, attempt.transfer_id, "displayed")

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            confirmed = response_json(client.get(route))["current"]
            if confirmed is not None:
                break
            time.sleep(0.05)
        else:
            pytest.fail("The delivery worker did not confirm the first dashboard")
        assert confirmed["frame_id"] == state["pending"]["frame_id"]
        assert software.call("playback_state") == {"playing": True}
        assert software.call("counts")["receipt_reads"] == 0


def test_rendered_queue_to_case_bdx_and_exact_display_receipt(peer, app_catalog, monkeypatch):
    software, ready = peer
    client, sessions, clock = app_catalog
    controller = MatterController(ready["port"], ready["token"])
    monkeypatch.setenv("INKDADDY_MATTER_PORT", str(ready["port"]))
    monkeypatch.setenv("INKDADDY_MATTER_TOKEN", ready["token"])

    link = response_json(client.post("/api/matter-frames/pair", json={"code": ready["share_code"]}))
    uid, target = link["device_uid"], link["node_id"]
    assert link["linked"] and uid == ready["device_uid"]
    route = f"/api/devices/{uid}"
    with sessions() as db:
        device_id = db.scalar(select(Device.id).where(Device.device_uid == uid))
        assert db.get(MatterFrameBinding, device_id).node_id == target
        assert db.get(MatterPlaybackIntent, device_id).playing is True
        db.add(
            HAEntityCache(
                entity_id="sensor.room",
                domain="sensor",
                name="Room temperature",
                state="21",
                source_reported_at=clock.now.replace(tzinfo=None),
                attributes_json='{"unit_of_measurement": "°C"}',
            )
        )
        db.commit()
    assert software.call("playback_state") == {"playing": False}
    assert matter_delivery.mirror_playback(controller, device_id, target, None, now=900) is None
    assert software.call("playback_state") == {"playing": True}
    with sessions() as db:
        assert db.get(MatterPlaybackIntent, device_id) is None

    artwork = io.BytesIO()
    Image.new("RGB", (800, 480), (255, 0, 0)).save(artwork, "PNG")
    photo = response_json(
        client.post("/api/photos", files={"file": ("Red.png", artwork.getvalue(), "image/png")})
    )
    dashboard = response_json(
        client.post(
            "/api/dashboards",
            json={
                "id": "panel-battery",
                "yaml": """
name: Panel battery
widgets:
  - id: panel_power
    type: big_number
    x: 0
    y: 0
    w: 2
    h: 1
    source:
      type: device_battery
    options:
      label: Battery
""",
            },
        )
    )
    hourly = response_json(
        client.post(
            "/api/dashboards",
            json={
                "id": "hourly",
                "yaml": """
name: Hourly
widgets:
  - {id: time, type: big_number, x: 0, y: 0, w: 2, h: 1, source: {type: clock}}
  - {id: room, type: big_number, x: 2, y: 0, w: 2, h: 1, source: {type: home_assistant, entity_id: sensor.room}}
""",
            },
        )
    )
    queue_state = response_json(
        client.put(
            f"{route}/playback",
            json={
                "playlist": [
                    {"type": "photo", "id": photo["id"]},
                    {"type": "dashboard", "id": dashboard["id"]},
                    {"type": "dashboard", "id": hourly["id"]},
                ]
            },
        )
    )
    assert queue_state["current"] is None
    pending_photo = queue_state["pending"]
    assert pending_photo["source_id"] == photo["id"]
    assert [slot["source_id"] for slot in queue_state["upcoming"]] == [
        dashboard["id"],
        hourly["id"],
        photo["id"],
        dashboard["id"],
        hourly["id"],
    ]
    assert [datetime.fromisoformat(slot["scheduled_at"]) for slot in queue_state["upcoming"]] == [
        NOW.replace(minute=0) + timedelta(hours=hour) for hour in range(1, 6)
    ]
    for slot in queue_state["upcoming"]:
        response = client.get(f"{route}/frame", params={"frame_id": slot["frame_id"]})
        assert response.status_code == 200
        assert len(response.content) == 192_000

    first = matter_delivery.deliver_frame(controller, device_id, target, None, now=1_000)
    assert first is not None and first.submitted
    assert first.transfer_id != pending_photo["frame_id"]
    expected_artwork = bytes([0x33]) * 192_000
    digest = expect_download(software, first.transfer_id, expected_artwork)
    assert response_json(client.get(f"{route}/playback"))["current"] is None
    wrong_id = "f" * 24
    software.call("emit_receipt", transfer_id=wrong_id, state=2, battery=27)
    software.call(
        "emit_receipt", transfer_id=first.transfer_id, state=2, battery=31, other_fabric=True
    )
    first = matter_delivery.deliver_frame(controller, device_id, target, first, now=1_005)
    assert first is not None and first.submitted
    assert controller.cached_receipt(target, wrong_id)["state"] == "unknown"
    assert controller.cached_receipt(target, first.transfer_id)["state"] == "queued"
    assert response_json(client.get(f"{route}/playback"))["current"] is None
    assert software.call("counts")["receipt_reads"] == 0
    finish_display(software, controller, device_id, target, first, now=1_010)

    shown = response_json(client.get(f"{route}/playback"))["current"]
    assert shown == pending_photo
    png = client.get(shown["preview_url"])
    assert png.status_code == 200
    with Image.open(io.BytesIO(png.content)) as image:
        assert image.size == (800, 480)
        assert image.convert("RGB").getpixel((400, 240)) == (255, 0, 0)
    assert response_json(client.get(route))["battery_percent"] == 81.5
    assert (
        controller.send(target, first.transfer_id, expected_artwork, digest)["state"] == "displayed"
    )
    assert software.call("counts")["acceptance_count"] == 1

    software.call("emit_next")
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        matter_delivery.sync_actions(controller, device_id, target)
        with sessions() as db:
            event_number = db.get(MatterFrameBinding, device_id).last_action_number
        if event_number != "0":
            break
        time.sleep(0.1)
    assert event_number != "0", "The real Matter subscription did not deliver Next."
    matter_delivery.sync_actions(controller, device_id, target)
    second = matter_delivery.deliver_frame(controller, device_id, target, None, now=1_015)
    assert second is not None and second.transfer_id != first.transfer_id
    state = response_json(client.get(f"{route}/playback"))
    assert state["current"] == shown
    pending_dashboard = state["pending"]
    assert pending_dashboard["source_id"] == dashboard["id"]
    # This dashboard has only battery text. The original upcoming bitmap was
    # prepared before the first CASE receipt supplied the battery measurement.
    assert pending_dashboard["frame_id"] != queue_state["upcoming"][0]["frame_id"]
    with sessions() as db:
        device = db.get(Device, device_id)
        config = dashboard_config(get_dashboard_record(db, dashboard["id"]))
        widgets = resolve_widget_text(db, config, NOW, device)
        assert widgets[0].value == "82%"
        expected_dashboard, _digest = raw_frame_from_image(
            render_dashboard(config, widgets), dither=True
        )
        data = db.scalar(select(Frame.path).where(Frame.frame_id == pending_dashboard["frame_id"]))
        assert Path(data).read_bytes() == expected_dashboard
    battery_waveform_sha = expect_download(software, second.transfer_id, expected_dashboard)
    assert battery_waveform_sha[:24] == pending_dashboard["frame_id"]
    software.call("emit_receipt", transfer_id=first.transfer_id, state=3, battery=12)
    second = matter_delivery.deliver_frame(controller, device_id, target, second, now=1_020)
    assert second is not None and second.submitted
    assert controller.cached_receipt(target, first.transfer_id)["state"] == "displayed"
    assert response_json(client.get(f"{route}/playback"))["current"] == shown
    assert response_json(client.get(route))["battery_percent"] == 81.5
    finish_display(software, controller, device_id, target, second, now=1_025)
    assert controller.cached_receipt(target, first.transfer_id)["state"] == "displayed"
    assert response_json(client.get(f"{route}/playback"))["current"] == pending_dashboard

    worker.run_playback_cycle(now=clock.now)
    warmed = response_json(client.get(f"{route}/playback"))["upcoming"][0]
    assert warmed["source_id"] == hourly["id"]
    clock.now = NOW.replace(minute=0) + timedelta(hours=1)
    assert datetime.fromisoformat(warmed["scheduled_at"]) == clock.now
    with sessions() as db:
        room = db.get(HAEntityCache, "sensor.room")
        room.state = "24"
        room.source_reported_at = clock.now.replace(tzinfo=None)
        db.commit()
    worker.run_playback_cycle(now=clock.now)
    advanced = response_json(client.get(f"{route}/playback"))
    assert advanced["current"] == pending_dashboard
    assert len(advanced["upcoming"]) == 5
    due = advanced["pending"]
    assert due["source_id"] == hourly["id"]
    assert due["scheduled_at"] == warmed["scheduled_at"]
    assert due["frame_id"] != warmed["frame_id"]
    third = matter_delivery.deliver_frame(controller, device_id, target, None, now=2_800)
    assert third is not None and third.transfer_id not in {first.transfer_id, second.transfer_id}
    with sessions() as db:
        config = dashboard_config(get_dashboard_record(db, hourly["id"]))
        widgets = resolve_widget_text(
            db, config, clock.now, db.get(Device, device_id), observed_at=clock.now
        )
        assert [widget.value for widget in widgets] == ["17:00", "24 °C"]
        expected_hourly, _digest = raw_frame_from_image(
            render_dashboard(config, widgets), dither=True
        )
    assert expect_download(software, third.transfer_id, expected_hourly)[:24] == due["frame_id"]
    assert response_json(client.get(f"{route}/playback"))["current"] == pending_dashboard
    finish_display(software, controller, device_id, target, third, now=2_805)
    assert response_json(client.get(f"{route}/playback"))["current"] == due

    for action, playing in (("pause", False), ("play", True)):
        ui = response_json(client.post(f"{route}/manual-refresh", params={"action": action}))
        assert ui["playback"]["paused"] is (not playing)
        with sessions() as db:
            assert db.get(MatterPlaybackIntent, device_id).playing is playing
        assert (
            matter_delivery.mirror_playback(controller, device_id, target, None, now=2_810) is None
        )
        assert software.call("playback_state") == {"playing": playing}
        with sessions() as db:
            assert db.get(MatterPlaybackIntent, device_id) is None
    assert response_json(client.get(f"{route}/playback"))["current"] == due

    with sessions() as db:
        assert not record_matter_frame_receipt(
            db,
            uid,
            transfer_id=first.transfer_id,
            frame_id=shown["frame_id"],
            result="displayed_hub_frame",
        )
        assert record_matter_frame_receipt(
            db,
            uid,
            transfer_id=third.transfer_id,
            frame_id=due["frame_id"],
            result="displayed_hub_frame",
        )
        receipts = db.scalars(
            select(DeviceStatusHistory).where(
                DeviceStatusHistory.device_id == device_id,
                DeviceStatusHistory.status == "displayed_hub_frame",
            )
        ).all()
        assert [json.loads(row.detail_json)["transfer_id"] for row in receipts] == [
            first.transfer_id,
            second.transfer_id,
            third.transfer_id,
        ]
        assert {json.loads(row.detail_json)["transport"] for row in receipts} == {"matter"}
    assert software.call("counts") == {"acceptance_count": 3, "receipt_reads": 0}

    clock.now += timedelta(minutes=8)
    response_json(client.post(f"{route}/manual-refresh", params={"action": "refresh_current"}))
    rejected = matter_delivery.deliver_frame(controller, device_id, target, None, now=2_815)
    assert rejected is not None and rejected.transfer_id != third.transfer_id
    with sessions() as db:
        config = dashboard_config(get_dashboard_record(db, hourly["id"]))
        widgets = resolve_widget_text(
            db, config, clock.now, db.get(Device, device_id), observed_at=clock.now, time_kind="manual",
        )
        assert [widget.value for widget in widgets] == ["17:08", "24 °C"]
        assert widgets[0].detail.startswith("Requested ")
        expected_manual, _digest = raw_frame_from_image(render_dashboard(config, widgets), dither=True)
    assert expected_manual != expected_hourly
    expect_download(software, rejected.transfer_id, expected_manual)
    software.call("emit_receipt", transfer_id=rejected.transfer_id, state=3, battery=12)
    wait_for_cached_receipt(controller, target, rejected.transfer_id, "failed")
    retry = matter_delivery.deliver_frame(controller, device_id, target, rejected, now=2_820)
    assert retry is not None and retry.renew_transfer and retry.panel_failures == 1
    assert response_json(client.get(f"{route}/playback"))["current"] == due
    assert response_json(client.get(route))["battery_percent"] == 81.5
    assert software.call("counts") == {"acceptance_count": 4, "receipt_reads": 0}


@pytest.mark.parametrize("peer", [3], indirect=True, ids=["revision-3-palette"])
def test_palette_photo_to_case_bdx_and_display_receipt(peer, app_catalog, monkeypatch):
    software, ready = peer
    client, sessions, _clock = app_catalog
    controller = MatterController(ready["port"], ready["token"])
    monkeypatch.setenv("INKDADDY_MATTER_PORT", str(ready["port"]))
    monkeypatch.setenv("INKDADDY_MATTER_TOKEN", ready["token"])

    link = response_json(client.post("/api/matter-frames/pair", json={"code": ready["share_code"]}))
    uid, target = link["device_uid"], link["node_id"]
    assert link["linked"] and uid == ready["device_uid"]
    route = f"/api/devices/{uid}/playback"
    with sessions() as db:
        device_id = db.scalar(select(Device.id).where(Device.device_uid == uid))

    panel_values = (0, 1, 2, 3, 5, 6)
    colors = dict(zip(WAVESHARE_7COLOR_6.packed_values, WAVESHARE_7COLOR_6.colors))
    rgb_block = bytes(channel for value in panel_values for channel in colors[value])
    image = Image.frombytes("RGB", (800, 480), rgb_block * (800 * 480 // len(panel_values)))
    artwork = io.BytesIO()
    image.save(artwork, "PNG")
    photo = response_json(
        client.post(
            "/api/photos", files={"file": ("Panel colors.png", artwork.getvalue(), "image/png")}
        )
    )
    state = response_json(
        client.put(route, json={"playlist": [{"type": "photo", "id": photo["id"]}]})
    )
    assert state["current"] is None
    pending = state["pending"]
    assert pending["source_id"] == photo["id"]

    attempt = matter_delivery.deliver_frame(controller, device_id, target, None, now=1_000)
    assert attempt is not None and attempt.submitted
    # The alternating raw bytes have no repeated runs for PackBits to exploit.
    expected = bytes.fromhex("012356") * (192_000 // 3)
    checksum = expect_download(software, attempt.transfer_id, expected, packing=2)
    assert checksum[:24] == pending["frame_id"]
    assert response_json(client.get(route))["current"] is None
    finish_display(software, controller, device_id, target, attempt, now=1_005)
    assert response_json(client.get(route))["current"] == pending


def response_json(response):
    assert response.is_success, response.text
    return response.json()


def expect_download(software, transfer_id, raw, *, packing=None):
    result = software.call("download", transfer_id=transfer_id)
    checksum = hashlib.sha256(raw).hexdigest()
    assert result["byte_count"] == len(raw) == 192_000
    assert result["sha256"] == checksum
    assert result["transport"] == "CASE/BDX"
    assert result["displayed"] is False
    assert result["packing"] == packing
    if packing is None:
        assert result["designator_suffix"] == "raw"
        assert result["offered_encoded_length"] is None
        assert result["wire_byte_count"] == 192_000
    else:
        assert result["designator_suffix"] == "packed"
        assert result["offered_encoded_length"] == result["wire_byte_count"]
        if packing == 1:
            assert 0 < result["wire_byte_count"] < 128_000
        else:
            assert result["wire_byte_count"] == 128_000
    return checksum


def finish_display(software, controller, device_id, target, attempt, *, now):
    software.call("display", transfer_id=attempt.transfer_id)
    report = wait_for_cached_receipt(controller, target, attempt.transfer_id, "displayed")
    assert report == {
        "transfer_id": attempt.transfer_id,
        "state": "displayed",
        "battery_percent": 81.5,
    }
    assert matter_delivery.deliver_frame(controller, device_id, target, attempt, now=now) is None
    assert software.call("counts")["receipt_reads"] == 0


def wait_for_cached_receipt(controller, target, transfer_id, expected_state):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        receipt = controller.cached_receipt(target, transfer_id)
        if receipt["state"] == expected_state:
            return receipt
        time.sleep(0.05)
    pytest.fail(f"Matter did not deliver the {expected_state} receipt for {transfer_id}.")
