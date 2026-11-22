from functools import partial
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from inkdaddy_hub import ha_app
from inkdaddy_hub.api import devices, matter_frames, simulator
from inkdaddy_hub.database import Base, get_db
from inkdaddy_hub.models import Device, MatterFrameBinding

UID = "xiao-mg24-01234567"
DEVICE_ROUTE = f"/api/devices/{UID}"
MARKED = {"X-InkDaddy-Request": "1"}


@pytest.fixture
def managed_app(tmp_path, monkeypatch):
    monkeypatch.setenv("INKDADDY_HA_MODE", "supervisor")
    engine = create_engine(f"sqlite:///{tmp_path / 'guards.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory.begin() as db:
        display = Device(device_uid=UID, name="Bench", battery_percent=83, firmware_version="6")
        db.add(display)
        db.flush()
        db.add(MatterFrameBinding(device_id=display.id, node_id="1"))

    def isolated_db():
        with factory() as db:
            yield db

    app = FastAPI()
    for module in (devices, matter_frames, simulator):
        app.include_router(module.router)
    app.dependency_overrides[get_db] = isolated_db
    yield TestClient(partial(ha_app.ingress_only, app), client=(ha_app.SUPERVISOR_IP, 4000))
    engine.dispose()


def test_managed_device_settings_cannot_replace_matter_telemetry(managed_app):
    response = managed_app.put(DEVICE_ROUTE, json={"name": "Kitchen", "battery_percent": 25}, headers=MARKED)
    assert response.status_code == 422
    assert managed_app.post("/api/devices", json={"device_uid": UID}, headers=MARKED).status_code == 409
    assert managed_app.post("/api/simulator/devices", json={"device_uid": UID}, headers=MARKED).status_code == 409
    assert managed_app.post(f"{DEVICE_ROUTE}/heartbeat", json={"battery_percent": 25}, headers=MARKED).status_code == 409
    assert managed_app.post(f"{DEVICE_ROUTE}/refresh-result", json={"result": "ok"}, headers=MARKED).status_code == 409

    rename = managed_app.put(DEVICE_ROUTE, json={"name": " Kitchen "}, headers=MARKED)
    assert rename.status_code == 200
    assert rename.json()["name"] == "Kitchen"
    assert rename.json()["battery_percent"] == 83
    assert rename.json()["firmware_version"] == "6"


def test_managed_frame_manifest_does_not_prepare_on_get(managed_app, monkeypatch):
    prepare = Mock()
    monkeypatch.setattr(devices, "refresh_playback_slots", prepare)
    response = managed_app.get(f"{DEVICE_ROUTE}/frame-manifest")
    assert response.status_code == 409
    prepare.assert_not_called()


def test_active_matter_check_requires_marked_post_in_managed_mode(managed_app, monkeypatch):
    controller = Mock()
    controller.nodes.return_value = [{"node_id": "1", "device_uid": UID}]
    monkeypatch.setattr(matter_frames, "get_controller", lambda: controller)

    cached = managed_app.get("/api/matter-frames")
    assert cached.json()["nodes"] == [{"node_id": "1", "device_uid": UID, "linked": True}]
    controller.nodes.assert_called_once_with(refresh=False)
    assert managed_app.get("/api/matter-frames?refresh=true").status_code == 409
    assert managed_app.post("/api/matter-frames/check").status_code == 403
    controller.nodes.assert_called_once()
    assert managed_app.post("/api/matter-frames/check", headers=MARKED).status_code == 200
    controller.nodes.assert_called_with(refresh=True)

    monkeypatch.delenv("INKDADDY_HA_MODE")
    assert managed_app.get("/api/matter-frames?refresh=true").status_code == 200
    controller.nodes.assert_called_with(refresh=True)


@pytest.mark.parametrize("version", ["2026.11.0", "2027.1.0"])
def test_ingress_authorization_requires_authenticated_system_version(version):
    with patch("inkdaddy_hub.ha_app.http.client.HTTPConnection") as make_connection:
        connection = make_connection.return_value
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = f'{{"data":{{"supervisor":"{version}"}}}}'.encode()
        ha_app.require_ingress_authorization("synthetic-app-token")
        connection.request.assert_called_once_with("GET", "/info", headers={"Authorization": "Bearer synthetic-app-token"})
        connection.close.assert_called_once()


@pytest.mark.parametrize("version", ["2026.10.2", "2026.11.0b1", "other", None])
def test_unverified_supervisor_does_not_open_ingress_or_start_controller(version, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "synthetic-app-token")
    monkeypatch.setattr(ha_app, "_supervisor_info", lambda *_args: {"supervisor": version})
    ingress_port, start_controller = Mock(), Mock()
    monkeypatch.setattr(ha_app.os, "umask", Mock())
    monkeypatch.setattr(ha_app, "ingress_port", ingress_port)
    monkeypatch.setattr(ha_app, "managed_matter", start_controller)
    with pytest.raises(RuntimeError, match="2026.11.0 or later"):
        ha_app.main()
    ingress_port.assert_not_called()
    start_controller.assert_not_called()
