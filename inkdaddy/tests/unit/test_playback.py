import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
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
    assert widgets[1].value == "Partly cloudy"
    assert widgets[1].detail == "71 °F"
    assert widgets[3].value == "0%"


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
    clock, weather, *_ = resolve_widget_text(db, config, NOW + timedelta(hours=4), device, observed_at=NOW)
    assert (weather.value, weather.detail) == ("Partly cloudy", "71 °F")
    assert clock.detail.startswith("For ")
    assert clock.value == "20:45"
    assert resolve_widget_text(db, config, NOW, device, observed_at=NOW + timedelta(minutes=61))[1].value == "Unavailable"

    config["widgets"][1]["source"]["entity_id"] = "weather.a_unavailable"
    assert resolve_widget_text(db, config, NOW, device)[1].value == "Unavailable"


@pytest.mark.parametrize("action", ["next_content", "previous_content", "refresh_current"])
def test_manual_dashboard_time_survives_worker_delay_without_shifting_slots(catalog, monkeypatch, action):
    db, device, _photo = catalog
    device_id = device.id
    apply_playback_action(db, device, action, NOW)
    db.commit()
    rendered = []
    render_dashboard = playback.render_dashboard

    def capture(config, widgets):
        rendered.append(widgets[0])
        return render_dashboard(config, widgets)

    monkeypatch.setattr(playback, "render_dashboard", capture)
    later = NOW + timedelta(minutes=6)
    with Session(db.bind, autoflush=False) as worker:
        slot = refresh_playback_slots(worker, worker.get(Device, device_id), now=later)
        status = playback_status(worker, worker.get(Device, device_id), later)
        assert slot.hour == int(NOW.timestamp()) // 3600
        assert status["pending"]["scheduled_at"] == "2026-11-18T16:00:00+00:00"
        assert [item["scheduled_at"][11:16] for item in status["upcoming"]] == [
            "17:00", "18:00", "19:00", "20:00", "21:00",
        ]
        assert rendered[0].value == "16:45"
        assert rendered[0].detail == "Requested Wed, Nov 18"
        assert [widget.value for widget in rendered[1:]] == ["17:00", "18:00", "19:00", "20:00", "21:00"]


def test_paused_manual_dashboard_refresh_uses_new_date_and_repairs_exact_bitmap(catalog):
    db, device, _photo = catalog
    apply_playback_action(db, device, "pause", NOW)
    anchor = db.get(Playback, device.id).anchor_hour
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
    assert db.get(Playback, device.id).anchor_hour == anchor
    config = dashboard_config(None)
    widgets = resolve_widget_text(db, config, requested, device, observed_at=rendered_at, time_kind="manual")
    assert (widgets[0].value, widgets[0].detail) == ("18:48", "Requested Thu, Nov 19")
    assert widgets[1].value == "Sunny"
    assert (widgets[3].value, widgets[3].detail) == ("84%", "Last reported")
    exact, _ = raw_frame_from_image(playback.render_dashboard(config, widgets), dither=True)
    assert offer.payload == exact
    frame_path = Path(db.scalar(select(Frame.path).where(Frame.frame_id == offer.frame_id)))
    db.commit()
    frame_path.unlink()
    with Session(db.bind, autoflush=False) as worker:
        recovered = prepare_matter_frame(worker, uid, now=rendered_at + timedelta(hours=4), repair_cache=True)
        assert recovered == offer
        assert frame_path.read_bytes() == exact


@pytest.mark.parametrize("render_version", ["previous", "legacy"])
def test_existing_dashboard_offer_keeps_its_original_bitmap_after_clock_update(catalog, render_version):
    db, device, _photo = catalog
    device.battery_percent = 84
    hour = int(NOW.timestamp()) // 3600
    timestamp = NOW.replace(minute=0)
    config = dashboard_config(None)
    widgets = resolve_widget_text(db, config, timestamp, device, render_version=render_version)
    image = playback.render_dashboard(config, widgets)
    exact, sha = raw_frame_from_image(image, dither=True)
    frame = playback._store_frame(db, exact, image, playback.Source("dashboard", "sample"))
    slot = PlaybackSlot(
        device_id=device.id, hour=hour, source_type="dashboard", source_id="sample",
        title=config["name"], frame_id=frame.frame_id,
        render_key=playback._render_key(("dashboard", "sample", config["name"], config, [asdict(widget) for widget in widgets])),
    )
    db.add(slot)
    db.flush()
    offer_slot(db, slot, now=NOW)
    db.flush()
    offer = prepare_matter_frame(db, device.device_uid, now=NOW)
    assert offer.sha256 == sha
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
    hour = int(NOW.timestamp()) // 3600
    for scheduled_hour in (hour, hour + 1):
        db.add(PlaybackSlot(
            device_id=device.id,
            hour=scheduled_hour,
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
    upcoming = db.get(PlaybackSlot, (device.id, hour + 1))
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
