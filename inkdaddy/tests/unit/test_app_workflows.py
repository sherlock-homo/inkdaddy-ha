from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import BytesIO
from threading import Event

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from inkdaddy_hub.api import devices, diagnostics, health, matter_frames, photos, settings, updates
from inkdaddy_hub.config import HubSettings
from inkdaddy_hub.database import Base, get_db
from inkdaddy_hub.models import Device, DeviceStatusHistory, MatterFrameBinding, Photo


@pytest.fixture
def workflow_client(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    app = FastAPI()
    for module in (settings, diagnostics, health, matter_frames, photos, updates, devices):
        app.include_router(module.router)

    def session():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = session
    with TestClient(app) as client:
        yield client, sessions
    engine.dispose()


def test_photo_framing_persists_and_uploads_use_it(workflow_client, tmp_path, monkeypatch):
    import inkdaddy_hub.services.photos as photo_service

    client, _ = workflow_client
    monkeypatch.setattr(photo_service, "get_settings", lambda: HubSettings(
        data_dir=tmp_path, default_width=8, default_height=8,
    ))
    assert client.get("/api/settings").json()["photo_upload_mode"] == "fit"
    saved = client.put("/api/settings", json={"photo_upload_mode": "fill"})
    assert saved.status_code == 200
    assert client.get("/api/settings").json()["photo_upload_mode"] == "fill"
    assert client.put("/api/settings", json={"default_refresh_minutes": 15}).status_code == 422

    encoded = BytesIO()
    Image.new("RGB", (12, 4), (255, 0, 0)).save(encoded, format="PNG")

    photo = client.post("/api/photos", files={"file": ("sample.png", encoded.getvalue(), "image/png")})
    assert photo.status_code == 200
    batch = client.post("/api/photos/batch", data={"mode": "fit"},
                        files={"files": ("sample.png", encoded.getvalue(), "image/png")})
    assert batch.status_code == 200
    fallback = batch.json()["photos"][0]

    filled = Image.open(BytesIO(client.get(photo.json()["preview_url"]).content)).convert("RGB")
    fitted = Image.open(BytesIO(client.get(fallback["preview_url"]).content)).convert("RGB")
    assert filled.getpixel((0, 0)) != (255, 255, 255)
    assert fitted.getpixel((0, 0)) == (255, 255, 255)


def test_album_order_persists_and_invalid_changes_leave_saved_album_intact(workflow_client):
    client, sessions = workflow_client
    with sessions.begin() as db:
        for name in ("First", "Second"):
            db.add(Photo(original_filename=name, stored_filename="", original_path=""))

    assert client.post("/api/albums", json={"name": "Incomplete", "photo_ids": ["missing"]}).status_code == 422
    assert client.get("/api/albums").json()["albums"] == []

    album = client.post("/api/albums", json={"name": "Travel", "photo_ids": ["1", "2", "2"]})
    assert album.status_code == 200
    assert [photo["id"] for photo in album.json()["photos"]] == ["1", "2"]
    path = f"/api/albums/{album.json()['id']}"
    assert client.put(path, json={"name": "Uncommitted", "photo_ids": ["2", "3"]}).status_code == 409
    assert client.get(path).json()["name"] == "Travel"

    saved = client.put(path, json={"name": "Road trip", "photo_ids": ["2", "1"]})
    assert saved.status_code == 200
    assert [photo["id"] for photo in client.get(path).json()["photos"]] == ["2", "1"]
    assert client.get(path).json()["name"] == "Road trip"


def test_durable_link_and_diagnostics_use_saved_state_without_exposing_details(workflow_client, monkeypatch):
    client, sessions = workflow_client

    class MatterController:
        def nodes(self, *, refresh=False):
            assert refresh is False
            return [{"node_id": "2", "device_uid": None}]

        def health(self):
            return {"ok": True, "internal_detail": "fixture-only"}

    monkeypatch.setattr(matter_frames, "get_controller", MatterController)
    monkeypatch.setattr(diagnostics, "get_controller", MatterController)
    monkeypatch.setenv("INKDADDY_HA_MODE", "supervisor")
    monkeypatch.setenv("SUPERVISOR_TOKEN", "fixture-only-token")
    with sessions.begin() as db:
        device = Device(device_uid="xiao-mg24-aabbccdd", name="Fixture Display", provisioned=True)
        db.add(device)
        db.flush()
        db.add(MatterFrameBinding(device_id=device.id, node_id="1"))
        db.add(DeviceStatusHistory(
            device_id=device.id, status="displayed_hub_frame", created_at=datetime(2026, 1, 1),
        ))

    status = client.get("/api/matter-frames").json()
    assert status["available"] is True
    assert status["nodes"] == [
        {"node_id": "1", "device_uid": "xiao-mg24-aabbccdd", "linked": True},
        {"node_id": "2", "device_uid": None, "linked": False},
    ]
    report = client.get("/api/diagnostics")
    assert report.json()["matter_controller"] == "ready"
    assert report.json()["registered_displays"] == report.json()["matter_links"] == 1
    assert report.json()["last_displayed_at"] == "2026-01-01T00:00:00+00:00"
    assert "fixture-only" not in report.text
    assert "data_dir" not in report.json()


def test_managed_updates_report_actual_manager_and_decline_unavailable_operations(workflow_client, monkeypatch):
    client, _ = workflow_client
    monkeypatch.setenv("INKDADDY_HA_MODE", "supervisor")

    def unexpected(*args, **kwargs):
        raise AssertionError("Managed release status is provided by Supervisor or Matter")

    monkeypatch.setattr(updates, "get_hub_update_info", unexpected)
    monkeypatch.setattr(updates, "get_firmware_update_info", unexpected)
    assert client.get("/api/updates/hub/check").json()["status"] == "managed_by_home_assistant"
    assert client.post("/api/updates/hub/apply", json={}).status_code == 409
    assert "command" not in client.get("/api/updates/hub/auto").json()
    assert client.put("/api/updates/hub/auto", json={}).status_code == 409
    assert client.get("/api/updates/firmware/check").json() == {"status": "managed_by_matter"}
    assert client.post("/api/updates/firmware/check").json() == {"status": "managed_by_matter"}
    assert client.get("/api/updates/firmware/manifest").json() == {"status": "managed_by_matter"}
    assert client.post("/api/updates/firmware/approve", json={}).status_code == 501


def test_standalone_firmware_checks_keep_release_metadata(workflow_client, monkeypatch):
    client, _ = workflow_client
    monkeypatch.delenv("INKDADDY_HA_MODE", raising=False)
    get_info = updates.get_firmware_update_info
    monkeypatch.setattr(updates, "get_firmware_update_info", lambda **kwargs: get_info(
        settings=HubSettings(github_repo=None), **kwargs,
    ))
    for endpoint in ("check", "manifest"):
        payload = client.get(f"/api/updates/firmware/{endpoint}?current_version=1.0").json()
        assert payload["status"] == "not_configured"
        assert payload["board"] == "xiao_mg24"
        assert payload["current_version"] == "1.0"


@pytest.mark.parametrize(("endpoint", "field"), [("/api/photos", "file"), ("/api/photos/batch", "files")])
def test_upload_processing_does_not_block_hub_health(workflow_client, monkeypatch, endpoint, field):
    client, _ = workflow_client
    processing = Event()
    release = Event()
    create = photos.create_photo_from_bytes

    def delayed_processor(*args, **kwargs):
        processing.set()
        assert release.wait(timeout=5), "The health request did not complete during photo processing"
        return create(*args, **kwargs)

    monkeypatch.setattr(photos, "create_photo_from_bytes", delayed_processor)
    encoded = BytesIO()
    Image.new("RGB", (16, 16), "red").save(encoded, format="PNG")
    with ThreadPoolExecutor(max_workers=2) as executor:
        uploading = executor.submit(
            client.post, endpoint, files={field: ("fixture.png", encoded.getvalue(), "image/png")},
        )
        try:
            assert processing.wait(timeout=5)
            health_request = executor.submit(client.get, "/api/health")
            assert health_request.result(timeout=2).json()["ok"]
            assert not uploading.done()
        finally:
            release.set()
        assert uploading.result(timeout=5).status_code == 200
