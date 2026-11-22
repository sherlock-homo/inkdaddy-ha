import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
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
from inkdaddy_hub.models import Dashboard, Device, DeviceStatusHistory, Frame, HAEntityCache, Photo
from inkdaddy_hub.services.dashboard_content import dashboard_config, resolve_widget_text
from inkdaddy_hub.services.matter_frame_broker import (
    MatterFrameError,
    prepare_matter_frame,
    record_matter_frame_receipt,
)
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
    widgets = resolve_widget_text(db, dashboard_config(None), NOW, device)
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
            attributes_json='{"temperature": 71, "temperature_unit": "°F"}',
        ),
        HAEntityCache(entity_id="weather.e_other", domain="weather", state="sunny"),
    ])
    db.flush()
    config = dashboard_config(None)
    weather = resolve_widget_text(db, config, NOW, device)[1]
    assert (weather.value, weather.detail) == ("Partly cloudy", "71 °F")

    config["widgets"][1]["source"]["entity_id"] = "weather.a_unavailable"
    assert resolve_widget_text(db, config, NOW, device)[1].value == "Unavailable"


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
        [{"type": "dashboard", "id": "sample"}, {"type": "photo", "id": str(photo.id)}],
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
    db, device, _photo = catalog
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


def test_matter_delivery_keeps_confirmed_offer_when_source_no_longer_matches(catalog):
    db, device, _photo = catalog
    initial = prepare_matter_frame(db, device.device_uid, now=NOW)
    assert record_matter_frame_receipt(
        db, device.device_uid, transfer_id=initial.transfer_id, frame_id=initial.frame_id, result="ok"
    )
    db.add(HAEntityCache(entity_id="weather.home", domain="weather", state="sunny"))
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
