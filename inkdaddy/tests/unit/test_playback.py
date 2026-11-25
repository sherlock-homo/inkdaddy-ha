import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from inkdaddy_hub.api import devices as devices_api
from inkdaddy_hub.config import HubSettings
from inkdaddy_hub.database import Base
from inkdaddy_hub.models import (
    Dashboard,
    Device,
    DeviceStatusHistory,
    Frame,
    HAEntityCache,
    Photo,
    PhotoRevision,
    Playback,
    PlaybackSlot,
)
from inkdaddy_hub.services import playback
from inkdaddy_hub.services.dashboard_content import dashboard_config, resolve_widget_text
from inkdaddy_hub.services.matter_frame_broker import (
    MatterFrameError,
    prepare_matter_frame,
    record_matter_frame_receipt,
)
from inkdaddy_hub.services.palette import raw_frame_from_image, raw_photo_frame
from inkdaddy_hub.services.playback import (
    acknowledge_frame,
    apply_playback_action,
    begin_playback_write,
    offer_slot,
    playback_status,
    preview_image,
    prune_playback,
    refresh_playback_slots,
    update_playlist,
    update_refresh_interval,
)
from inkdaddy_hub.services.udp_frame_transfer import (
    FrameTransferRequest,
    handle_frame_transfer_request,
)

NOW = datetime(2026, 11, 18, 16, 45, tzinfo=timezone.utc)


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        "inkdaddy_hub.services.playback.get_settings", lambda: HubSettings(data_dir=tmp_path)
    )
    photo_path = tmp_path / "processed" / "photo.png"
    photo_path.parent.mkdir()
    Image.new("RGB", (800, 480), (127, 102, 102)).save(photo_path)
    with Session(engine, expire_on_commit=False, autoflush=False) as db:
        device = Device(
            device_uid="test-screen", name="Test screen", provisioned=True, mesh_connected=True
        )
        photo = Photo(
            original_filename="Holiday.png",
            stored_filename="photo.png",
            original_path=str(photo_path),
            processed_path=str(photo_path),
            processed_status="ready",
        )
        db.add_all([device, photo])
        db.commit()
        yield db, device, photo


@pytest.mark.parametrize("where", ["outside", "symlink", "too_large"])
def test_frame_http_and_matter_reject_unmanaged_or_oversized_payloads(catalog, tmp_path, where):
    db, device, _photo = catalog
    prepared = prepare_matter_frame(db, device.device_uid, now=NOW)
    frame = db.scalar(select(Frame).where(Frame.frame_id == prepared.frame_id))
    assert frame is not None
    assert devices_api.frame(device.device_uid, frame.frame_id, db=db).body == prepared.payload
    assert devices_api.playback_preview(device.device_uid, frame.frame_id, db=db).status_code == 200

    if where == "outside":
        invalid = tmp_path / "external-fixture.raw"
        invalid.write_bytes(prepared.payload)
    elif where == "symlink":
        invalid = tmp_path / "frames" / "linked.raw"
        invalid.symlink_to(frame.path)
    else:
        invalid = Path(frame.path)
        invalid.write_bytes(prepared.payload + b"\x00")
    frame.path = str(invalid)
    db.flush()

    with pytest.raises(HTTPException) as raw_error:
        devices_api.frame(device.device_uid, frame.frame_id, db=db)
    assert raw_error.value.status_code == 404
    with pytest.raises(HTTPException) as preview_error:
        devices_api.playback_preview(device.device_uid, frame.frame_id, db=db)
    assert preview_error.value.status_code == 404
    with pytest.raises(MatterFrameError):
        prepare_matter_frame(db, device.device_uid, now=NOW)


def test_change_refresh_interval_preserves_current_offer_and_round_trips_matter(catalog):
    from inkdaddy_hub.services.matter_frame_broker import load_matter_frame_payload

    db, device, photo = catalog
    update_playlist(db, device, [{"type": "dashboard", "id": "sample"},
                                 {"type": "photo", "id": str(photo.id)}], NOW)
    initial = prepare_matter_frame(db, device.device_uid, now=NOW)
    assert record_matter_frame_receipt(
        db, device.device_uid, transfer_id=initial.transfer_id, frame_id=initial.frame_id, result="ok", now=NOW,
    )
    refresh_playback_slots(db, device, now=NOW)
    update_refresh_interval(db, device, 90, NOW)
    refresh_playback_slots(db, device, now=NOW)
    assert prepare_matter_frame(db, device.device_uid, now=NOW).transfer_id == initial.transfer_id
    status = playback_status(db, device, NOW)
    assert status["current"]["frame_id"] == initial.frame_id
    assert [item["scheduled_at"][11:16] for item in status["upcoming"]] == [
        "17:30", "19:00", "20:30", "22:00", "23:30",
    ]

    next_tick = NOW.replace(hour=17, minute=30)
    next_frame = prepare_matter_frame(db, device.device_uid, now=next_tick, include_payload=False)
    assert next_frame.scheduled_at == next_tick
    assert next_frame.source_id == str(photo.id)
    assert load_matter_frame_payload(db, device, next_frame)

    apply_playback_action(db, device, "pause", next_tick)
    update_refresh_interval(db, device, 20, next_tick)
    later = next_tick + timedelta(minutes=60)
    paused = prepare_matter_frame(db, device.device_uid, now=later)
    assert (paused.source_id, paused.transfer_id, paused.scheduled_at) == (
        next_frame.source_id, next_frame.transfer_id, next_tick,
    )
    apply_playback_action(db, device, "play", later)
    following = prepare_matter_frame(db, device.device_uid, now=later + timedelta(minutes=20))
    assert following.source_type == "dashboard"


@pytest.mark.parametrize("overlay_id", ["sample", "agenda"])
def test_photo_overlay_repairs_exact_offered_bytes_after_photo_and_layout_edits(
    catalog, tmp_path, monkeypatch, overlay_id,
):
    from inkdaddy_hub.services import dashboard_content

    db, device, photo = catalog
    db.add(Dashboard(slug="agenda", name="Calendar", current_yaml=json.dumps({
        "name": "Calendar",
        "widgets": [{
            "id": "event_list", "type": "agenda", "x": 0, "y": 1, "w": 2, "h": 1,
            "source": {"type": "calendar", "entity_id": "calendar.fixture"},
            "options": {"show_event_titles": True},
        }],
    })))
    db.flush()
    monkeypatch.setattr(dashboard_content, "upcoming_agenda", lambda *args, **kwargs: ("17:15 · Fixture",))
    device.selected_source_type = "photo"
    plain = refresh_playback_slots(db, device, now=NOW)
    plain_id = plain.frame_id
    device.overlay_dashboard_id = overlay_id
    original_offer = prepare_matter_frame(db, device.device_uid, now=NOW)
    assert original_offer.frame_id != plain_id
    due = db.get(PlaybackSlot, (device.id, playback.epoch_second(NOW.replace(minute=0))))
    if overlay_id == "agenda":
        assert '"agenda": true' in due.photo_render_json
        assert "calendar.fixture" not in due.photo_render_json
    original_frame = db.scalar(select(Frame).where(Frame.frame_id == original_offer.frame_id))
    frame_path = Path(original_frame.path)
    db.add(PhotoRevision(photo_id=photo.id, processed_path=photo.processed_path))
    reframed = tmp_path / "processed" / "new-framing.png"
    Image.new("RGB", (800, 480), "red").save(reframed)
    photo.processed_path = str(reframed)
    device.overlay_dashboard_id = None
    db.commit()
    frame_path.unlink()

    restored = prepare_matter_frame(db, device.device_uid, now=NOW, repair_cache=True)
    assert restored == original_offer
    assert frame_path.read_bytes() == original_offer.payload
    next_frame = prepare_matter_frame(db, device.device_uid, now=NOW + timedelta(hours=1))
    assert next_frame.frame_id not in {plain_id, original_offer.frame_id}


def test_shorter_interval_keeps_in_flight_offer_and_advances_once_at_next_tick(catalog):
    db, device, photo = catalog
    update_playlist(db, device, [{"type": "dashboard", "id": "sample"},
                                 {"type": "photo", "id": str(photo.id)}], NOW)
    pending = prepare_matter_frame(db, device.device_uid, now=NOW)
    update_refresh_interval(db, device, 25, NOW)
    refresh_playback_slots(db, device, now=NOW)
    assert prepare_matter_frame(db, device.device_uid, now=NOW + timedelta(minutes=1)) == pending
    assert record_matter_frame_receipt(
        db, device.device_uid, transfer_id=pending.transfer_id, frame_id=pending.frame_id, result="ok", now=NOW,
    )
    assert playback_status(db, device, NOW)["upcoming"][0]["scheduled_at"][11:16] == "16:50"
    due = prepare_matter_frame(db, device.device_uid, now=NOW + timedelta(minutes=5))
    assert (due.scheduled_at.minute, due.source_type) == (50, "photo")


def test_regeneration_does_not_write_to_a_foreign_persisted_path(catalog, tmp_path):
    db, device, _photo = catalog
    prepared = prepare_matter_frame(db, device.device_uid, now=NOW)
    frame = db.scalar(select(Frame).where(Frame.frame_id == prepared.frame_id))
    assert frame is not None
    foreign = tmp_path / "unmanaged.raw"
    frame.path = str(foreign)
    db.flush()

    with pytest.raises(MatterFrameError, match="prepare"):
        prepare_matter_frame(db, device.device_uid, now=NOW, repair_cache=True)
    assert not foreign.exists()


def test_five_upcoming_and_only_offered_success_counts_as_displayed(catalog):
    db, device, photo = catalog
    begin_playback_write(db)
    update_playlist(
        db,
        device,
        [{"type": "dashboard", "id": "sample"}, {"type": "photo", "id": str(photo.id)}],
        NOW,
    )
    due = refresh_playback_slots(db, device, now=NOW)
    db.commit()
    status = playback_status(db, device, NOW)
    assert status["current"] is None
    assert status["pending"]["source_type"] == "dashboard"
    assert [slot["source_type"] for slot in status["upcoming"]] == [
        "photo",
        "dashboard",
        "photo",
        "dashboard",
        "photo",
    ]
    at = [datetime.fromisoformat(slot["scheduled_at"]) for slot in status["upcoming"]]
    assert at == [NOW.replace(minute=0) + timedelta(hours=hour) for hour in range(1, 6)]
    for slot in status["upcoming"]:
        path = db.scalar(select(Frame.path).where(Frame.frame_id == slot["frame_id"]))
        assert Path(path).stat().st_size == 192_000
    photo_id = status["upcoming"][0]["frame_id"]
    photo_frame = db.scalar(select(Frame).where(Frame.frame_id == photo_id))
    assert len(set(preview_image(photo_frame).tobytes())) > 1  # mid-tone photo is actually dithered
    assert not acknowledge_frame(db, device, photo_id, "displayed_hub_frame")
    offered = offer_slot(db, due, now=NOW)
    db.flush()
    assert not acknowledge_frame(db, device, offered.frame_id, "display_refresh_failed")
    assert acknowledge_frame(db, device, offered.frame_id, "displayed_hub_frame")
    assert not acknowledge_frame(db, device, offered.frame_id, "displayed_hub_frame")
    db.commit()
    with Session(db.bind) as reopened:
        result = playback_status(reopened, reopened.get(Device, device.id), NOW)
        assert result["current"]["frame_id"] == offered.frame_id
        assert result["pending"] is None


def test_pause_retains_frame_and_resume_preserves_position(catalog):
    db, device, photo = catalog
    update_playlist(
        db,
        device,
        [{"type": "dashboard", "id": "sample"}, {"type": "photo", "id": str(photo.id)}],
        NOW,
    )
    first = refresh_playback_slots(db, device, now=NOW)
    offer_slot(db, first, now=NOW)
    db.flush()
    acknowledge_frame(db, device, first.frame_id, "ok")
    apply_playback_action(db, device, "pause", NOW)
    later = NOW + timedelta(hours=4)
    assert refresh_playback_slots(db, device, now=later).frame_id == first.frame_id
    apply_playback_action(db, device, "play", later)
    assert refresh_playback_slots(db, device, now=later).source_id == "sample"
    next_slot = refresh_playback_slots(db, device, now=later + timedelta(hours=1))
    assert next_slot.source_id == str(photo.id)
    new_offer = offer_slot(db, next_slot, now=later)
    db.flush()
    assert acknowledge_frame(db, device, new_offer.frame_id, "ok")
    assert not acknowledge_frame(db, device, first.frame_id, "ok")


def test_existing_hour_stays_stable_after_ha_state_changes(catalog):
    db, device, _photo = catalog
    weather = HAEntityCache(
        entity_id="weather.forecast_home",
        domain="weather",
        state="sunny",
        source_reported_at=NOW.replace(tzinfo=None),
        attributes_json='{"temperature": 71, "temperature_unit": "°F"}',
    )
    db.add(weather)
    db.flush()
    frame = refresh_playback_slots(db, device, now=NOW)
    record = offer_slot(db, frame, now=NOW)
    db.flush()
    weather.state = "partlycloudy"
    db.flush()
    assert refresh_playback_slots(db, device, now=NOW).frame_id == record.frame_id
    assert (
        refresh_playback_slots(db, device, now=NOW + timedelta(hours=1)).frame_id != record.frame_id
    )
    device.battery_percent = 0
    widgets = resolve_widget_text(db, dashboard_config(None), NOW, device, observed_at=NOW)
    assert widgets[0].value == "Partly cloudy"
    assert widgets[0].detail == "71 °F"
    assert widgets[2].value == "0%"


def test_sample_weather_uses_first_available_provider(catalog):
    db, device, _photo = catalog
    db.add_all([
        HAEntityCache(entity_id="weather.a_unavailable", domain="weather", state="unavailable"),
        HAEntityCache(entity_id="weather.b_unknown", domain="weather", state="unknown"),
        HAEntityCache(entity_id="weather.c_null", domain="weather", state=None),
        HAEntityCache(
            entity_id="weather.d_local",
            domain="weather",
            state="partlycloudy",
            source_reported_at=NOW.replace(tzinfo=None),
            attributes_json='{"temperature": 71, "temperature_unit": "°F"}',
        ),
        HAEntityCache(entity_id="weather.e_other", domain="weather", state="sunny"),
    ])
    db.flush()
    config = dashboard_config(None)
    weather, *_ = resolve_widget_text(db, config, NOW + timedelta(hours=4), device, observed_at=NOW)
    assert (weather.value, weather.detail) == ("Partly cloudy", "71 °F")
    assert resolve_widget_text(db, config, NOW, device, observed_at=NOW + timedelta(minutes=61))[0].value == "Unavailable"

    config["widgets"][0]["source"]["entity_id"] = "weather.a_unavailable"
    assert resolve_widget_text(db, config, NOW, device)[0].value == "Unavailable"


@pytest.mark.parametrize("action", ["next_content", "previous_content", "refresh_current"])
def test_manual_dashboard_time_survives_worker_delay_without_shifting_slots(catalog, monkeypatch, action):
    db, device, _photo = catalog
    device_id = device.id
    apply_playback_action(db, device, action, NOW)
    db.commit()
    rendered = []
    def capture(db, config, timestamp, device, **options):
        rendered.append((timestamp, options["time_kind"]))
        return resolve_widget_text(db, config, timestamp, device, **options)

    monkeypatch.setattr(playback, "resolve_widget_text", capture)
    later = NOW + timedelta(minutes=6)
    with Session(db.bind, autoflush=False) as worker:
        slot = refresh_playback_slots(worker, worker.get(Device, device_id), now=later)
        status = playback_status(worker, worker.get(Device, device_id), later)
        assert slot.scheduled_second == playback.epoch_second(NOW.replace(minute=0))
        assert status["pending"]["scheduled_at"] == "2026-11-18T16:00:00+00:00"
        assert [item["scheduled_at"][11:16] for item in status["upcoming"]] == [
            "17:00", "18:00", "19:00", "20:00", "21:00",
        ]
        assert rendered[0] == (NOW, "manual")
        assert rendered[1:] == [(NOW.replace(minute=0) + timedelta(hours=n), "scheduled") for n in range(1, 6)]


def test_paused_manual_dashboard_refresh_uses_new_date_and_repairs_exact_bitmap(catalog):
    db, device, _photo = catalog
    apply_playback_action(db, device, "pause", NOW)
    anchor = db.get(Playback, device.id).anchor_second
    requested = NOW + timedelta(days=1, hours=2, minutes=3)
    db.add(HAEntityCache(
        entity_id="weather.home", domain="weather", state="sunny",
        source_reported_at=requested.replace(tzinfo=None) - timedelta(minutes=55),
    ))
    device.battery_percent = 84
    apply_playback_action(db, device, "refresh_current", requested)
    db.commit()
    uid = device.device_uid
    rendered_at = requested + timedelta(minutes=2)
    offer = prepare_matter_frame(db, uid, now=rendered_at)
    assert offer.scheduled_at.isoformat() == "2026-11-18T16:00:00+00:00"
    assert db.get(Playback, device.id).anchor_second == anchor
    config = dashboard_config(None)
    widgets = resolve_widget_text(db, config, requested, device, observed_at=rendered_at, time_kind="manual")
    assert widgets[0].value == "Sunny"
    assert (widgets[2].value, widgets[2].detail) == ("84%", "Last reported")
    exact, _ = raw_frame_from_image(playback.render_dashboard(config, widgets), dither=True)
    assert offer.payload == exact
    frame_path = Path(db.scalar(select(Frame.path).where(Frame.frame_id == offer.frame_id)))
    db.commit()
    frame_path.unlink()
    with Session(db.bind, autoflush=False) as worker:
        recovered = prepare_matter_frame(worker, uid, now=rendered_at + timedelta(hours=4), repair_cache=True)
        assert recovered == offer
        assert frame_path.read_bytes() == exact


@pytest.mark.parametrize("render_version", ["dev23", "dev20", "previous", "legacy"])
def test_existing_dashboard_offer_keeps_its_original_bitmap_after_clock_update(catalog, render_version):
    db, device, _photo = catalog
    device.battery_percent = 84
    hour = playback.epoch_second(NOW.replace(minute=0))
    timestamp = NOW.replace(minute=0)
    config = dashboard_config(None, historical=True)
    widgets = resolve_widget_text(db, config, timestamp, device, render_version=render_version)
    if render_version == "dev20":
        assert widgets[0].detail == "For Wed, Nov 18"
        assert widgets[3].detail == "Last reported"
    image = playback.render_dashboard(config, widgets)
    exact, sha = raw_frame_from_image(image, dither=True)
    frame = playback._store_frame(db, exact, image, playback.Source("dashboard", "sample"))
    slot = PlaybackSlot(
        device_id=device.id, scheduled_second=hour, source_type="dashboard", source_id="sample",
        title=config["name"], frame_id=frame.frame_id,
        render_key=playback._render_key(("dashboard", "sample", config["name"], config, [asdict(widget) for widget in widgets])),
    )
    db.add(slot)
    db.flush()
    offer_slot(db, slot, now=NOW)
    db.flush()
    offer = prepare_matter_frame(db, device.device_uid, now=NOW)
    assert offer.sha256 == sha
    # Saving and renaming the formerly implicit sample removes its clock in
    # current renders. An existing receipt must still reproduce the old bytes.
    from inkdaddy_hub.api import dashboards
    dashboards.update_dashboard("sample", {"name": "New layout", "yaml": '{"name":"Edited","widgets":[]}'}, db)
    cache_path = Path(frame.path)
    cache_path.unlink()
    recovered = prepare_matter_frame(db, device.device_uid, now=NOW, repair_cache=True)
    assert recovered == offer
    assert cache_path.read_bytes() == exact


def test_manual_action_selects_configured_dashboard(catalog):
    db, device, _photo = catalog
    db.add(Dashboard(slug="other", name="Other", current_yaml="name: Other\nwidgets: []"))
    db.flush()
    update_playlist(
        db,
        device,
        [{"type": "dashboard", "id": "sample"}, {"type": "dashboard", "id": "other"}],
        NOW,
    )
    assert refresh_playback_slots(db, device, now=NOW).source_id == "sample"
    apply_playback_action(db, device, "next_content", NOW)
    assert refresh_playback_slots(db, device, now=NOW).source_id == "other"
    apply_playback_action(db, device, "previous_content", NOW)
    assert refresh_playback_slots(db, device, now=NOW).source_id == "sample"


def test_thread_manifest_offers_new_hour_even_if_device_reports_previous_frame(
    catalog, monkeypatch, tmp_path
):
    db, device, photo = catalog
    monkeypatch.setattr("inkdaddy_hub.services.playback._utc_now", lambda: NOW)
    settings = HubSettings(data_dir=tmp_path, udp_frame_chunk_size=1024)
    update_playlist(
        db,
        device,
        [{"type": "dashboard", "id": "sample"}, {"type": "photo", "id": str(photo.id)}],
        NOW,
    )
    slot = refresh_playback_slots(db, device, now=NOW)
    previous_frame_id = slot.frame_id
    db.commit()

    def exchange(request_type, **fields):
        return handle_frame_transfer_request(
            db, FrameTransferRequest(request_type, device.device_uid, **fields), settings
        )

    assert f"frame_id={previous_frame_id}".encode() in exchange("manifest_request")
    exchange("refresh_result", frame_id=previous_frame_id, result="displayed_hub_frame")
    assert b"status=no_change" in exchange("manifest_request", last_frame_id=previous_frame_id)
    next_hour = NOW + timedelta(hours=1)
    monkeypatch.setattr("inkdaddy_hub.services.playback._utc_now", lambda: next_hour)

    response = exchange("manifest_request", last_frame_id=previous_frame_id)
    assert b"status=changed" in response
    selected = playback_status(db, device, next_hour)
    assert selected["current"]["frame_id"] == previous_frame_id
    next_id = selected["pending"]["frame_id"]
    assert f"frame_id={next_id}".encode() in response
    _, chunk = exchange("chunk_request", frame_id=next_id, chunk_index=0).split(b"\n", 1)
    assert len(chunk) == 1024
    exchange("refresh_result", frame_id=next_id, result="displayed_hub_frame")
    assert playback_status(db, device, next_hour)["current"]["frame_id"] == next_id


def test_matter_broker_retry_keeps_verified_transfer_and_checks_exact_receipt(catalog):
    db, device, _photo = catalog
    first = prepare_matter_frame(db, device.device_uid, now=NOW)
    db.commit()
    assert first.size_bytes == 192_000
    assert re.fullmatch("[a-f0-9]{24}", first.transfer_id)
    assert first.frame_id != first.transfer_id
    assert hashlib.sha256(first.payload).hexdigest() == first.sha256
    assert playback_status(db, device, NOW)["current"] is None
    with Session(db.bind, autoflush=False) as reopened:
        retry = prepare_matter_frame(reopened, device.device_uid, now=NOW)
        reopened.commit()
        assert retry == first
        assert not record_matter_frame_receipt(
            reopened,
            device.device_uid,
            transfer_id=retry.transfer_id,
            frame_id=retry.frame_id,
            result="display_refresh_failed",
        )
        assert not record_matter_frame_receipt(
            reopened,
            device.device_uid,
            transfer_id=retry.transfer_id,
            frame_id="b" * 24,
            result="ok",
        )
        assert record_matter_frame_receipt(
            reopened,
            device.device_uid,
            transfer_id=retry.transfer_id,
            frame_id=retry.frame_id,
            result="ok",
        )
        reopened.commit()
        assert record_matter_frame_receipt(
            reopened,
            device.device_uid,
            transfer_id=retry.transfer_id,
            frame_id=retry.frame_id,
            result="ok",
        )
        history = reopened.scalars(select(DeviceStatusHistory)).all()
        assert len(history) == 1
        proven = prepare_matter_frame(reopened, device.device_uid, now=NOW)
        assert proven.acknowledged
        assert proven.transfer_id == retry.transfer_id


def test_matter_broker_rejects_superseded_transfer_even_for_same_pixels(catalog):
    db, device, photo = catalog
    update_playlist(
        db,
        device,
        [{"type": "photo", "id": str(photo.id)}, {"type": "dashboard", "id": "sample"}],
        NOW,
    )
    first = prepare_matter_frame(db, device.device_uid, now=NOW)
    apply_playback_action(db, device, "next_content", NOW)
    middle = prepare_matter_frame(db, device.device_uid, now=NOW)
    apply_playback_action(db, device, "previous_content", NOW)
    final = prepare_matter_frame(db, device.device_uid, now=NOW)
    assert first.frame_id == final.frame_id
    assert len({first.transfer_id, middle.transfer_id, final.transfer_id}) == 3
    for prior in (first, middle):
        assert not record_matter_frame_receipt(
            db,
            device.device_uid,
            transfer_id=prior.transfer_id,
            frame_id=prior.frame_id,
            result="ok",
        )
    assert record_matter_frame_receipt(
        db,
        device.device_uid,
        transfer_id=final.transfer_id,
        frame_id=final.frame_id,
        result="ok",
    )


def test_matter_broker_rejects_cross_device_and_corrupt_frame(catalog):
    db, device, _photo = catalog
    second = Device(device_uid="different-screen", name="Other")
    db.add(second)
    db.flush()
    offer = prepare_matter_frame(db, device.device_uid, now=NOW)
    assert not record_matter_frame_receipt(
        db,
        second.device_uid,
        transfer_id=offer.transfer_id,
        frame_id=offer.frame_id,
        result="ok",
    )
    path = db.scalar(select(Frame.path).where(Frame.frame_id == offer.frame_id))
    Path(path).write_bytes(bytes([offer.payload[0] ^ 1]) + offer.payload[1:])
    with pytest.raises(MatterFrameError, match="integrity"):
        prepare_matter_frame(db, device.device_uid, now=NOW)


def test_matter_broker_pending_offer_survives_stale_cache_cleanup(catalog):
    db, device, _photo = catalog
    offer = prepare_matter_frame(db, device.device_uid, now=NOW)
    later = NOW + timedelta(hours=2)
    refresh_playback_slots(db, device, now=later)
    prune_playback(db, now=later)
    db.commit()
    assert record_matter_frame_receipt(
        db,
        device.device_uid,
        transfer_id=offer.transfer_id,
        frame_id=offer.frame_id,
        result="ok",
    )


def test_concurrent_matter_preparations_share_one_durable_offer(catalog):
    db, device, _photo = catalog
    device_uid = device.device_uid
    engine = db.bind

    def prepare():
        with Session(engine, autoflush=False) as session:
            offer = prepare_matter_frame(session, device_uid, now=NOW)
            session.commit()
            return offer.transfer_id

    with ThreadPoolExecutor(max_workers=4) as pool:
        transfer_ids = list(pool.map(lambda _: prepare(), range(4)))
    assert len(set(transfer_ids)) == 1


def test_manual_refresh_replaces_confirmed_transfer_with_same_bitmap(catalog):
    db, device, photo = catalog
    update_playlist(db, device, [{"type": "photo", "id": str(photo.id)}], NOW)
    initial = prepare_matter_frame(db, device.device_uid, now=NOW)
    expected = {"frame_id": initial.frame_id, "transfer_id": initial.transfer_id, "result": "ok"}
    assert record_matter_frame_receipt(db, device.device_uid, **expected)
    db.commit()
    apply_playback_action(db, device, "refresh_current", NOW)
    db.commit()
    assert playback_status(db, device, NOW)["current"]["frame_id"] == initial.frame_id
    assert not record_matter_frame_receipt(db, device.device_uid, **expected)
    refresh = prepare_matter_frame(db, device.device_uid, now=NOW)
    assert refresh.payload == initial.payload
    assert refresh.transfer_id != initial.transfer_id
    assert not refresh.acknowledged


def test_scheduled_repeat_keeps_exact_receipt_and_manual_refresh_still_sends(catalog):
    from inkdaddy_hub.models import PlaybackOffer
    from inkdaddy_hub.services.matter_frame_broker import load_matter_frame_payload

    db, device, photo = catalog
    update_playlist(db, device, [{"type": "photo", "id": str(photo.id)}], NOW)
    initial = prepare_matter_frame(db, device.device_uid, now=NOW)
    assert record_matter_frame_receipt(
        db, device.device_uid, transfer_id=initial.transfer_id, frame_id=initial.frame_id, result="ok", now=NOW,
    )
    first_confirmed = playback_status(db, device, NOW)["current"]
    db.commit()

    later = NOW + timedelta(hours=24)
    with Session(db.bind, autoflush=False) as reopened:
        screen = reopened.get(Device, device.id)
        refresh_playback_slots(reopened, screen, now=later)
        kept = prepare_matter_frame(reopened, screen.device_uid, now=later, include_payload=False)
        assert kept.acknowledged and kept.payload is None
        assert (kept.transfer_id, kept.scheduled_at) == (initial.transfer_id, initial.scheduled_at)
        prune_playback(reopened, later)
        reopened.commit()

    with Session(db.bind, autoflush=False) as restarted:
        screen = restarted.get(Device, device.id)
        assert prepare_matter_frame(restarted, screen.device_uid, now=later, include_payload=False) == kept
        assert load_matter_frame_payload(restarted, screen, kept) == initial.payload
        assert len(restarted.scalars(select(PlaybackOffer)).all()) == 1
        assert len(restarted.scalars(select(DeviceStatusHistory)).all()) == 1
        assert screen.last_seen_at == NOW.replace(tzinfo=None)
        status = playback_status(restarted, screen, later)
        assert status["current"] == first_confirmed and status["pending"] is None
        assert len(status["upcoming"]) == 5
        assert status["upcoming"][0]["scheduled_at"] == (later.replace(minute=0) + timedelta(hours=1)).isoformat()
        apply_playback_action(restarted, screen, "refresh_current", later)
        fresh = prepare_matter_frame(restarted, screen.device_uid, now=later)
        assert not fresh.acknowledged and fresh.transfer_id != initial.transfer_id
        assert fresh.payload == initial.payload


def test_retained_slot_freezes_until_due_and_keeps_pending_transfers_unconfirmed(catalog):
    db, device, photo = catalog
    update_playlist(db, device, [{"type": "photo", "id": str(photo.id)}], NOW)
    initial = prepare_matter_frame(db, device.device_uid, now=NOW)
    later = NOW + timedelta(hours=1)
    pending = prepare_matter_frame(db, device.device_uid, now=later)
    assert pending.sha256 == initial.sha256 and pending.transfer_id != initial.transfer_id
    assert not pending.acknowledged
    assert not record_matter_frame_receipt(
        db, device.device_uid, transfer_id=initial.transfer_id, frame_id=initial.frame_id, result="ok",
    )
    assert record_matter_frame_receipt(
        db, device.device_uid, transfer_id=pending.transfer_id, frame_id=pending.frame_id, result="ok", now=later,
    )
    retained_at = later + timedelta(hours=1)
    assert prepare_matter_frame(db, device.device_uid, now=retained_at).transfer_id == pending.transfer_id
    Image.new("RGB", (800, 480), "blue").save(photo.processed_path)
    refresh_playback_slots(db, device, now=retained_at)
    assert prepare_matter_frame(db, device.device_uid, now=retained_at).transfer_id == pending.transfer_id
    replacement = prepare_matter_frame(db, device.device_uid, now=retained_at + timedelta(hours=1))
    assert replacement.sha256 != pending.sha256 and not replacement.acknowledged


@pytest.mark.parametrize("damage", ["truncated", "wrong_byte", "extra_bytes"])
def test_matter_delivery_repairs_generated_file_without_changing_offer(catalog, damage):
    db, device, _photo = catalog
    initial = prepare_matter_frame(db, device.device_uid, now=NOW)
    db.commit()
    path = Path(db.scalar(select(Frame.path).where(Frame.frame_id == initial.frame_id)))
    damaged = {
        "truncated": initial.payload[:64],
        "wrong_byte": bytes([initial.payload[0] ^ 1]) + initial.payload[1:],
        "extra_bytes": initial.payload + b"\x00",
    }
    path.write_bytes(damaged[damage])

    recovered = prepare_matter_frame(db, device.device_uid, now=NOW, repair_cache=True)

    assert recovered == initial
    assert path.read_bytes() == initial.payload
    assert record_matter_frame_receipt(
        db, device.device_uid, transfer_id=initial.transfer_id, frame_id=initial.frame_id, result="ok"
    )


def test_preupgrade_photo_offer_repairs_and_only_unoffered_slots_get_new_dither(catalog):
    db, device, photo = catalog
    source = playback.Source("photo", str(photo.id))
    update_playlist(db, device, [{"type": source.type, "id": source.id}], NOW)
    photo_path = Path(photo.processed_path)
    with Image.open(photo_path) as image:
        old_raw, old_digest = raw_frame_from_image(image, dither=True)
        new_raw, new_digest = raw_photo_frame(image)
        assert new_digest != old_digest
        frame = playback._store_frame(db, old_raw, image, source)
    legacy_version = (
        source.type, source.id, photo.original_filename, photo.checksum, photo_path.stat().st_mtime_ns
    )
    legacy_key = hashlib.sha256(
        json.dumps(legacy_version, sort_keys=True, default=str).encode()
    ).hexdigest()
    hour = playback.epoch_second(NOW.replace(minute=0))
    for scheduled_hour in (hour, hour + 3600):
        db.add(PlaybackSlot(
            device_id=device.id,
            scheduled_second=scheduled_hour,
            source_type=source.type,
            source_id=source.id,
            title=photo.original_filename,
            frame_id=frame.frame_id,
            render_key=legacy_key,
        ))
    db.flush()
    offer_slot(db, db.get(PlaybackSlot, (device.id, hour)), now=NOW)
    db.flush()
    initial = prepare_matter_frame(db, device.device_uid, now=NOW)
    assert initial.sha256 == old_digest
    db.commit()
    path = Path(frame.path)
    path.unlink()
    # A restore may preserve the processed image bytes and replace its filesystem metadata.
    restored = photo_path.with_suffix(".restored")
    restored.write_bytes(photo_path.read_bytes())
    changed_mtime = photo_path.stat().st_mtime_ns + 10_000_000_000
    os.utime(restored, ns=(changed_mtime, changed_mtime))
    restored.replace(photo_path)

    scheduled = refresh_playback_slots(db, device, now=NOW)
    assert scheduled.frame_id == initial.frame_id
    recovered = prepare_matter_frame(db, device.device_uid, now=NOW, repair_cache=True)
    assert recovered == initial
    assert path.read_bytes() == old_raw
    upcoming = db.get(PlaybackSlot, (device.id, hour + 3600))
    assert upcoming.render_key != legacy_key
    assert upcoming.frame_id == new_digest[:24]
    upgraded_frame = db.scalar(select(Frame).where(Frame.frame_id == upcoming.frame_id))
    assert upgraded_frame is not None
    assert playback.read_frame_bytes(upgraded_frame) == new_raw


def test_color_upgrade_keeps_old_offer_and_rebuilds_future_photo(catalog, monkeypatch):
    db, device, photo = catalog
    load_legacy = playback.load_processed_photo
    monkeypatch.setattr(
        playback, "load_color_managed_photo",
        lambda data_dir, processed_path, _original_path: load_legacy(data_dir, processed_path),
    )
    update_playlist(db, device, [{"type": "photo", "id": str(photo.id)}], now=NOW)
    original_offer = prepare_matter_frame(db, device.device_uid, now=NOW)
    frame = db.scalar(select(Frame).where(Frame.frame_id == original_offer.frame_id))
    Path(frame.path).unlink()

    corrected = Image.new("RGB", (800, 480), (190, 88, 77))
    monkeypatch.setattr(playback, "load_color_managed_photo", lambda *_args: corrected)
    refresh_playback_slots(db, device, now=NOW)
    future = playback_status(db, device, NOW)["upcoming"]
    assert {slot["frame_id"] for slot in future} == {raw_photo_frame(corrected)[1][:24]}
    assert future[0]["frame_id"] != original_offer.frame_id

    recovered = prepare_matter_frame(db, device.device_uid, now=NOW, repair_cache=True)
    assert recovered == original_offer
    assert Path(frame.path).read_bytes() == original_offer.payload


def test_matter_delivery_keeps_confirmed_offer_when_source_no_longer_matches(catalog):
    db, device, _photo = catalog
    initial = prepare_matter_frame(db, device.device_uid, now=NOW)
    assert record_matter_frame_receipt(
        db, device.device_uid, transfer_id=initial.transfer_id, frame_id=initial.frame_id, result="ok"
    )
    db.add(HAEntityCache(
        entity_id="weather.home", domain="weather", state="sunny",
        source_reported_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))
    db.commit()
    current = playback_status(db, device, NOW)["current"]
    path = Path(db.scalar(select(Frame.path).where(Frame.frame_id == initial.frame_id)))
    path.write_bytes(initial.payload[:64])

    with pytest.raises(MatterFrameError, match="original frame can no longer be regenerated"):
        prepare_matter_frame(db, device.device_uid, now=NOW, repair_cache=True)

    assert playback_status(db, device, NOW)["current"] == current
    assert path.read_bytes() == initial.payload[:64]


def test_manual_refresh_restores_a_damaged_generated_file(catalog):
    db, device, _photo = catalog
    apply_playback_action(db, device, "refresh_current", NOW)
    initial = prepare_matter_frame(db, device.device_uid, now=NOW)
    db.commit()
    path = Path(db.scalar(select(Frame.path).where(Frame.frame_id == initial.frame_id)))
    path.write_bytes(initial.payload[:64])

    apply_playback_action(db, device, "refresh_current", NOW)
    recovered = prepare_matter_frame(db, device.device_uid, now=NOW)

    assert recovered.frame_id == initial.frame_id
    assert recovered.transfer_id != initial.transfer_id
    assert recovered.payload == initial.payload
    assert path.read_bytes() == initial.payload


def test_prune_reconciles_rolled_back_generated_frames_once_per_hour(catalog, tmp_path, monkeypatch):
    db, device, _photo = catalog
    old = NOW - timedelta(hours=2)
    confirmed = prepare_matter_frame(db, device.device_uid, now=old)
    assert record_matter_frame_receipt(
        db, device.device_uid, transfer_id=confirmed.transfer_id,
        frame_id=confirmed.frame_id, result="ok", now=old,
    )
    db.commit()
    cache = tmp_path / "frames" / "playback"
    displayed = cache / f"{confirmed.frame_id}.raw"

    device.battery_percent = 54
    refresh_playback_slots(db, device, now=NOW)
    untracked = set(cache.glob("*.raw")) - {displayed}
    assert untracked
    db.rollback()

    unrelated = cache / "notes.raw"
    unrelated.write_bytes(b"fixture kept outside the generated naming convention")
    external = tmp_path / "outside.raw"
    external.write_bytes(b"an external fixture")
    symlink = cache / ("f" * 24 + ".raw")
    symlink.symlink_to(external)
    list_names = playback.asset_names
    sweeps = []

    def count_sweep(*args):
        sweeps.append(args)
        return list_names(*args)

    monkeypatch.setattr(playback, "asset_names", count_sweep)
    prune_playback(db, now=NOW)
    db.commit()
    assert not any(path.exists() for path in untracked)
    assert displayed.read_bytes() == confirmed.payload
    assert unrelated.exists() and symlink.is_symlink()
    assert external.read_bytes() == b"an external fixture"

    later_orphan = cache / ("a" * 24 + ".raw")
    later_orphan.write_bytes(b"an interrupted managed cache write")
    prune_playback(db, now=NOW + timedelta(seconds=30))
    db.commit()
    assert len(sweeps) == 1 and later_orphan.exists()
    prune_playback(db, now=NOW + timedelta(hours=1))
    db.commit()
    assert len(sweeps) == 2 and not later_orphan.exists()
    assert displayed.read_bytes() == confirmed.payload


def test_invalid_future_photo_falls_back_without_changing_playlist_or_healthy_slots(catalog, tmp_path, monkeypatch):
    db, device, broken = catalog
    healthy_path = tmp_path / "processed" / "healthy.png"
    Image.new("RGB", (800, 480), (220, 80, 30)).save(healthy_path)
    healthy = Photo(
        original_filename="Healthy.png", stored_filename=healthy_path.name,
        original_path=str(healthy_path), processed_path=str(healthy_path), processed_status="ready",
    )
    db.add(healthy)
    db.flush()
    playlist = [
        {"type": "dashboard", "id": "sample"},
        {"type": "photo", "id": str(broken.id)},
        {"type": "photo", "id": str(healthy.id)},
    ]
    update_playlist(db, device, playlist, now=NOW)
    db.commit()
    broken_path = Path(broken.processed_path)
    broken_path.write_bytes(b"invalid synthetic image")
    load = playback.load_color_managed_photo
    broken_reads = []

    def count_broken_reads(data, path, original):
        if path == str(broken_path):
            broken_reads.append(path)
        return load(data, path, original)

    monkeypatch.setattr(playback, "load_color_managed_photo", count_broken_reads)
    current = refresh_playback_slots(db, device, now=NOW)
    assert current.source_type == "dashboard"
    initial = prepare_matter_frame(db, device.device_uid, now=NOW)
    assert record_matter_frame_receipt(
        db, device.device_uid, transfer_id=initial.transfer_id,
        frame_id=initial.frame_id, result="ok", now=NOW,
    )
    db.commit()
    queued = playback_status(db, device, NOW)
    assert len(queued["upcoming"]) == 5
    assert queued["upcoming"][0]["source_type"] == "dashboard"
    assert queued["upcoming"][1]["source_id"] == str(healthy.id)
    assert queued["playlist"] == playlist

    for seconds in (10, 20):
        refresh_playback_slots(db, device, now=NOW + timedelta(seconds=seconds))
        db.commit()
        assert playback_status(db, device, NOW + timedelta(seconds=seconds)) == queued
    assert len(broken_reads) == 1
    assert db.get(Photo, broken.id).processed_status == "ready"

    Image.new("RGB", (800, 480), (20, 130, 220)).save(broken_path)
    refresh_playback_slots(db, device, now=NOW + timedelta(seconds=30))
    db.commit()
    recovered = playback_status(db, device, NOW + timedelta(seconds=30))
    assert len(recovered["upcoming"]) == 5
    assert recovered["current"] == queued["current"]
    assert recovered["playlist"] == playlist
    assert recovered["upcoming"][0]["source_id"] == str(broken.id)
    assert recovered["upcoming"][1] == queued["upcoming"][1]


def test_repair_does_not_substitute_for_an_already_offered_damaged_photo(catalog):
    db, device, photo = catalog
    update_playlist(db, device, [{"type": "photo", "id": str(photo.id)}], now=NOW)
    offer = prepare_matter_frame(db, device.device_uid, now=NOW)
    assert record_matter_frame_receipt(
        db, device.device_uid, transfer_id=offer.transfer_id,
        frame_id=offer.frame_id, result="ok", now=NOW,
    )
    db.commit()
    frame = db.scalar(select(Frame).where(Frame.frame_id == offer.frame_id))
    damage = offer.payload[:32]
    Path(photo.processed_path).write_bytes(b"invalid synthetic image")
    Path(frame.path).write_bytes(damage)

    with pytest.raises(MatterFrameError, match="Could not repair"):
        prepare_matter_frame(db, device.device_uid, now=NOW, repair_cache=True)
    assert playback_status(db, device, NOW)["current"]["frame_id"] == offer.frame_id
    assert Path(frame.path).read_bytes() == damage


def test_repeated_photo_reuses_prepared_bytes_and_delivery_verifies_them(catalog, monkeypatch):
    db, device, photo = catalog
    conversion = playback.raw_photo_frame
    conversions = []

    def count_conversion(image):
        conversions.append(image.size)
        return conversion(image)

    monkeypatch.setattr(playback, "raw_photo_frame", count_conversion)
    update_playlist(db, device, [{"type": "photo", "id": str(photo.id)}], now=NOW)
    first = refresh_playback_slots(db, device, now=NOW)
    initial = prepare_matter_frame(db, device.device_uid, now=NOW)
    assert len(conversions) == 1
    assert {slot.frame_id for slot in db.scalars(select(PlaybackSlot))} == {first.frame_id}
    db.commit()
    frame = db.scalar(select(Frame).where(Frame.frame_id == initial.frame_id))
    path = Path(frame.path)

    # A wrong length is rejected even for warm reuse. One rebuild serves all six slots.
    path.write_bytes(initial.payload[:32])
    later = NOW + timedelta(hours=1)
    current = refresh_playback_slots(db, device, now=later)
    assert current.frame_id == initial.frame_id
    assert len(conversions) == 2
    assert path.read_bytes() == initial.payload

    # An altered same-length file is checked against the offered SHA before sending.
    damaged = bytes([initial.payload[0] ^ 1]) + initial.payload[1:]
    path.write_bytes(damaged)
    with pytest.raises(ValueError, match="Prepared frame is missing or invalid"):
        preview_image(frame)
    sent = prepare_matter_frame(db, device.device_uid, now=later, repair_cache=True)
    assert sent.sha256 == initial.sha256 and sent.payload == initial.payload
    assert path.read_bytes() == initial.payload
    assert len(conversions) == 3

    original = Path(photo.processed_path)
    before = original.stat()
    replacement = original.with_name("replacement.png")
    Image.new("RGB", (800, 480), (30, 120, 220)).save(replacement)
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(original)
    assert original.stat().st_mtime_ns == before.st_mtime_ns
    refresh_playback_slots(db, device, now=later)
    assert len(conversions) == 4
    assert initial.frame_id not in {slot["frame_id"] for slot in playback_status(db, device, later)["upcoming"]}


def test_scheduled_photo_age_uses_each_original_and_slot_local_date(catalog, tmp_path, monkeypatch):
    from inkdaddy_hub.models import Setting
    from inkdaddy_hub.services import dashboard_content

    db, device, first = catalog
    monkeypatch.setattr(dashboard_content, "get_settings", lambda: HubSettings(data_dir=tmp_path))
    (tmp_path / "originals").mkdir()
    second = Photo(original_filename="Second.jpg", stored_filename="two.jpg", processed_path=first.processed_path,
                   processed_status="ready")
    for photo, year in ((first, 2000), (second, 2010)):
        path = tmp_path / "originals" / f"{year}.jpg"
        photo.original_path = str(path)
        tags = Image.Exif()
        tags[34665] = {36867: f"{year}:11:18 20:01:02"}
        tags[315] = "Excluded private field"
        Image.new("RGB", (16, 10), "white").save(path, exif=tags)
    config = {"name": "Photo age", "widgets": [{
        "id": "ago", "type": "entity_state", "x": 0, "y": 0, "w": 1, "h": 1,
        "source": {"type": "photo_metadata", "field": "years_ago"},
    }]}
    db.add_all([second, Setting(key=dashboard_content.SITE_SETTING, value_json='{"time_zone":"America/New_York"}'),
                Dashboard(slug="age", name="Photo age", current_yaml=json.dumps(config))])
    device.overlay_dashboard_id = "age"
    db.flush()
    when = NOW.replace(hour=4, minute=0)
    update_playlist(db, device, [{"type": "photo", "id": str(p.id)} for p in (first, second)], when)
    offer = prepare_matter_frame(db, device.device_uid, now=when)
    refresh_playback_slots(db, device, now=when)
    slots = db.scalars(select(PlaybackSlot).where(PlaybackSlot.device_id == device.id)
                       .order_by(PlaybackSlot.scheduled_second)).all()
    assert [json.loads(s.photo_render_json)["overlay"][0]["text"]["value"] for s in slots[:3]] == [
        "25 years ago", "16 years ago", "26 years ago",
    ]
    assert all("Excluded private field" not in s.photo_render_json for s in slots)
    # Repair an already offered photo after its original or settings disappear.
    Path(first.original_path).unlink()
    device.overlay_dashboard_id = None
    frame = db.scalar(select(Frame).where(Frame.frame_id == offer.frame_id))
    Path(frame.path).unlink()
    db.commit()
    assert prepare_matter_frame(db, device.device_uid, now=when, repair_cache=True) == offer


def test_alternating_mode_advances_album_photos_on_exact_half_intervals(catalog, tmp_path, monkeypatch):
    from inkdaddy_hub.api import dashboards
    from inkdaddy_hub.models import Album, AlbumItem
    from inkdaddy_hub.services.matter_frame_broker import load_matter_frame_payload

    db, device, photo = catalog
    start = NOW.replace(minute=0)
    monkeypatch.setattr(playback, "_utc_now", lambda: start)
    second_path = tmp_path / "processed" / "second.png"
    Image.new("RGB", (800, 480), "red").save(second_path)
    second = Photo(original_filename="Second.png", stored_filename="second.png", original_path=str(second_path),
                   processed_path=str(second_path), processed_status="ready")
    dashboard = Dashboard(slug="morning", name="Morning", current_yaml='{"name":"Morning","widgets":[]}')
    album = Album(name="Selected")
    db.add_all([second, dashboard, album])
    db.flush()
    db.add(AlbumItem(album_id=album.id, photo_id=second.id, sort_order=0))
    db.flush()
    playlist = [{"type": "photo", "id": str(photo.id)}, {"type": "dashboard", "id": "sample"},
                {"type": "album", "id": str(album.id)}]
    update_playlist(db, device, playlist, start)
    device.overlay_dashboard_id = "sample"
    old_offer = prepare_matter_frame(db, device.device_uid, now=start)
    uid = device.device_uid
    response = devices_api.update_device(uid, {"interlace_dashboard_id": "morning", "refresh_interval_minutes": 11}, db)
    assert response["interlace_dashboard_id"] == "morning" and response["refresh_interval_warning"]
    assert prepare_matter_frame(db, uid, now=start + timedelta(seconds=329)) == old_offer
    assert record_matter_frame_receipt(db, uid, transfer_id=old_offer.transfer_id,
                                      frame_id=old_offer.frame_id, result="ok", now=start)
    assert db.get(PlaybackSlot, (device.id, int(start.timestamp()))).photo_render_json is not None
    status = playback_status(db, device, start)
    assert status["playlist"] == playlist
    assert [(item["source_type"], item["source_id"]) for item in status["upcoming"]] == [
        ("dashboard", "morning"), ("photo", str(second.id)), ("dashboard", "morning"),
        ("photo", str(photo.id)), ("dashboard", "morning"),
    ]
    assert [datetime.fromisoformat(item["scheduled_at"]) for item in status["upcoming"]] == [
        start + timedelta(seconds=330 * n) for n in range(1, 6)
    ]
    with pytest.raises(HTTPException) as used:
        dashboards.delete_dashboard("morning", db)
    assert used.value.status_code == 409
    second_id = second.id
    device_id = device.id
    db.commit()
    with Session(db.bind, autoflush=False) as restarted:
        display = restarted.get(Device, device_id)
        boundary = start + timedelta(seconds=330)
        offered = prepare_matter_frame(restarted, uid, now=boundary)
        assert (offered.scheduled_at, offered.source_id) == (boundary, "morning")
        assert load_matter_frame_payload(restarted, display, offered) == offered.payload
        apply_playback_action(restarted, display, "pause", boundary)
        later = start + timedelta(hours=2)
        assert prepare_matter_frame(restarted, uid, now=later) == replace(offered, playing=False)
        apply_playback_action(restarted, display, "next_content", later)
        assert prepare_matter_frame(restarted, uid, now=later).source_id == str(second_id)
        apply_playback_action(restarted, display, "previous_content", later)
        assert prepare_matter_frame(restarted, uid, now=later).source_id == "morning"
        apply_playback_action(restarted, display, "play", later)
        playback.update_playback_schedule(restarted, display, 60, None, later)
        refresh_playback_slots(restarted, display, now=later)
        status = playback_status(restarted, display, later)
        assert status["playlist"] == playlist
        assert {item["source_id"] for item in status["upcoming"]} == {"sample", str(photo.id), str(second_id)}
        assert len(status["upcoming"]) == 5


def test_alternating_library_fallback_and_dashboard_validation(catalog, monkeypatch):
    db, device, photo = catalog
    monkeypatch.setattr(playback, "_utc_now", lambda: NOW)
    for invalid in ([], 42, "", "missing"):
        with pytest.raises(HTTPException) as rejected:
            devices_api.update_device(device.device_uid, {"interlace_dashboard_id": invalid}, db)
        assert rejected.value.status_code == 422
    assert device.interlace_dashboard_id is None
    result = devices_api.update_device(device.device_uid, {"interlace_dashboard_id": "sample"}, db)
    assert result["interlace_dashboard_id"] == "sample" and not result["refresh_interval_warning"]
    status = playback_status(db, device, NOW)
    assert status["pending"]["source_id"] == str(photo.id)
    assert [slot["source_type"] for slot in status["upcoming"]] == ["dashboard", "photo", "dashboard", "photo", "dashboard"]
    photo.processed_status = "failed"
    db.flush()
    refresh_playback_slots(db, device, now=NOW)
    assert {slot["source_id"] for slot in playback_status(db, device, NOW)["upcoming"]} == {"sample"}
    assert devices_api.update_device(device.device_uid, {"interlace_dashboard_id": None}, db)["interlace_dashboard_id"] is None


def test_unreadable_alternating_photo_uses_configured_dashboard(catalog):
    db, device, photo = catalog
    db.add(Dashboard(slug="chosen", name="Chosen", current_yaml='{"name":"Chosen","widgets":[]}'))
    db.flush()
    Path(photo.processed_path).write_bytes(b"unreadable fixture")
    playback.update_playback_schedule(db, device, 60, "chosen", NOW)
    assert refresh_playback_slots(db, device, now=NOW).source_id == "chosen"
    assert {slot["source_id"] for slot in playback_status(db, device, NOW)["upcoming"]} == {"chosen"}
