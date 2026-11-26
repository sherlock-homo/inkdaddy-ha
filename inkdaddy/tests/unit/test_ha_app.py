import asyncio
from datetime import datetime, timedelta, timezone
from functools import partial
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from inkdaddy_hub.api.integrations import list_integrations
from inkdaddy_hub.database import Base
from inkdaddy_hub.ha_app import SUPERVISOR_IP, ingress_host, ingress_only, ingress_port
from inkdaddy_hub.services.home_assistant import SUPERVISOR_CORE_URL, supervisor_connection


def test_ingress_only_accepts_connections_from_supervisor():
    async def request(peer, proxy_peer):
        messages = []

        async def send(message):
            messages.append(message)

        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        await ingress_only(downstream, {
            "type": "http",
            "client": (peer, 1111),
            "headers": [(b"x-forwarded-for", proxy_peer.encode())],
        }, None, send)
        return messages[0]["status"]

    assert asyncio.run(request(SUPERVISOR_IP, "192.0.2.15")) == 200
    assert asyncio.run(request("192.0.2.15", SUPERVISOR_IP)) == 403


def test_ingress_requires_marker_and_bounds_requests(monkeypatch):
    from inkdaddy_hub import ha_app

    monkeypatch.setattr(ha_app, "MIB", 32)
    app = FastAPI()

    @app.post("/api/test")
    async def echo(request: Request):
        return {"size": len(await request.body())}

    client = TestClient(partial(ingress_only, app), client=(SUPERVISOR_IP, 1111))
    assert client.post("/api/test", content="hello").status_code == 403
    headers = {"X-InkDaddy-Request": "1"}
    accepted = client.post("/api/test", headers=headers, content="hello")
    assert accepted.json() == {"size": 5}
    assert accepted.headers["cache-control"] == "no-store"
    assert accepted.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'self'" in accepted.headers["content-security-policy"]
    assert client.post("/api/test", headers={**headers, "Content-Length": "33"}, content="hello").status_code == 413
    assert client.post("/api/test", headers=headers, content=iter([b"a" * 17, b"b" * 17])).status_code == 413


def test_managed_app_does_not_expose_development_cors_or_openapi(monkeypatch):
    from inkdaddy_hub.main import create_app

    monkeypatch.setenv("INKDADDY_HA_MODE", "supervisor")
    app = create_app()
    client = TestClient(partial(ingress_only, app), client=(SUPERVISOR_IP, 1111))
    response = client.options("/api/photos", headers={
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "X-InkDaddy-Request",
    })
    assert "access-control-allow-origin" not in response.headers
    assert app.openapi_url is None


def test_supervisor_credentials_do_not_appear_in_repr(monkeypatch):
    monkeypatch.setenv("INKDADDY_HA_MODE", "supervisor")
    monkeypatch.setenv("SUPERVISOR_TOKEN", "private-token")
    connection = supervisor_connection()
    assert connection.base_url == SUPERVISOR_CORE_URL
    assert connection.token == "private-token"
    assert "private-token" not in repr(connection)


def test_reads_supervisor_assigned_ingress_port():
    with patch("inkdaddy_hub.ha_app.http.client.HTTPConnection") as make_connection:
        connection = make_connection.return_value
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"result":"ok","data":{"ingress_port":64112}}'
        assert ingress_port("private-token") == 64112
        connection.request.assert_called_once_with(
            "GET", "/addons/self/info", headers={"Authorization": "Bearer private-token"}
        )
        connection.close.assert_called_once()


def test_ingress_binds_to_the_supervisor_route():
    with patch("inkdaddy_hub.ha_app.socket.socket") as make_socket:
        route = make_socket.return_value.__enter__.return_value
        route.getsockname.return_value = ("192.0.2.10", 38811)
        assert ingress_host() == "192.0.2.10"
        route.connect.assert_called_once_with((SUPERVISOR_IP, 9))


def test_ingress_revalidates_entry_page_without_disabling_asset_caching():
    app = FastAPI()

    @app.get("/{path:path}")
    def placeholder(path: str):
        return {}

    client = TestClient(partial(ingress_only, app), client=(SUPERVISOR_IP, 1111))
    for path in ("/", "/index.html"):
        assert client.get(path).headers["cache-control"] == "no-cache"
    assert "cache-control" not in client.get("/assets/fixture.js").headers
    assert client.get("/api/health").headers["cache-control"] == "no-store"


def test_update_details_allowlist_only_the_apps_own_release_status(monkeypatch):
    from inkdaddy_hub import ha_app

    metadata = {
        "slug": "fixture_inkdaddy", "version_latest": "0.2.1", "update_available": True,
        "auto_update": False, "options": {"hidden": "fixture"}, "ingress_url": "/private/fixture",
    }

    def self_info(token, path):
        assert token == "fixture" and path == "/addons/self/info"
        return metadata

    monkeypatch.setattr(ha_app, "_supervisor_info", self_info)
    assert ha_app.app_update_details("fixture") == {
        "manage_path": "/config/app/fixture_inkdaddy/info", "latest_version": "0.2.1",
        "update_available": True, "auto_update": False,
    }
    metadata.update(slug="../other", auto_update="false", update_available=None, version_latest=None)
    assert ha_app.app_update_details("fixture") == {}
    assert ha_app.app_update_details("") == {}
    with patch.object(ha_app, "_supervisor_info", side_effect=RuntimeError):
        assert ha_app.app_update_details("fixture") == {}


def test_only_reported_firmware_is_exposed_for_registered_displays():
    from inkdaddy_hub.services.ha_matter_share import _candidate

    uid = "xiao-mg24-aabbccdd"
    row = {"model": "inkDaddy", "id": "a" * 32, "sw_version": "  6.0  ", "private_field": "fixture",
           "identifiers": [["matter", f"serial_{uid}"], ["matter", f"deviceid_{'1'*16}-{'2'*16}-MatterNodeDevice"]]}
    assert _candidate(row) == {"device_id": "a" * 32, "name": "inkDaddy", "device_uid": uid, "firmware_version": "6.0"}
    assert "firmware_version" not in _candidate({**row, "sw_version": None})
    assert _candidate({**row, "model": "other"}) is None


def test_thread_border_router_reports_home_assistant_management(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        monkeypatch.delenv("INKDADDY_HA_MODE", raising=False)
        standalone_router = next(
            item for item in list_integrations(db)["integrations"]
            if item["id"] == "thread_border_router"
        )
        assert standalone_router["status"] == "diagnostics_required"
        assert standalone_router["manage_url"] == "#settings/diagnostics"

        monkeypatch.setenv("INKDADDY_HA_MODE", "supervisor")
        monkeypatch.setenv("SUPERVISOR_TOKEN", "private-token")
        managed_router = next(
            item for item in list_integrations(db)["integrations"]
            if item["id"] == "thread_border_router"
        )
        assert managed_router["status"] == "managed_by_home_assistant"
        assert managed_router["configured"] is None
        assert managed_router["enabled"] is None
        assert managed_router["manage_url"] == "/config/thread"

def test_home_assistant_site_refresh_does_not_discard_cached_states(monkeypatch):
    from sqlalchemy.orm import sessionmaker

    from inkdaddy_hub import worker
    from inkdaddy_hub.models import HAEntityCache, Setting
    from inkdaddy_hub.services.dashboard_content import SITE_SETTING
    from inkdaddy_hub.services.home_assistant import HAConnection

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as db:
        db.add(HAEntityCache(entity_id="weather.home", domain="weather",
                             state="sunny", attributes_json="{}"))
    connection = HAConnection("http://supervisor/core", "test-token")
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker.home_assistant, "get_connection", lambda _db: connection)

    def unavailable(_url, _token):
        raise OSError("States unavailable")

    monkeypatch.setattr(worker.home_assistant, "fetch_states", unavailable)
    monkeypatch.setattr(worker.home_assistant, "fetch_site", lambda _connection: {
        "location_name": "Test House",
        "time_zone": "America/New_York",
    })

    worker.refresh_home_assistant()

    with sessions() as db:
        assert db.get(HAEntityCache, "weather.home").state == "sunny"
        assert "America/New_York" in db.get(Setting, SITE_SETTING).value_json
    engine.dispose()


def test_dashboard_entity_catalog_is_cache_only_and_filters_for_fresh_measurements(monkeypatch):
    from inkdaddy_hub.api import home_assistant as api
    from inkdaddy_hub.models import HAEntityCache
    from inkdaddy_hub.services.home_assistant import normalize_state_response, replace_entity_cache

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)

    def state(entity_id, value, minutes, **attributes):
        return {
            "entity_id": entity_id, "state": value, "attributes": attributes,
            "last_reported": (now - timedelta(minutes=minutes)).isoformat(),
            "last_updated": (now - timedelta(days=10)).isoformat(),
        }

    def no_network(*_args):
        raise AssertionError("Dashboard catalog must use the existing cache.")

    monkeypatch.setattr(api, "get_connection", no_network)
    with Session(engine) as db:
        replace_entity_cache(db, normalize_state_response([
            state("sensor.a_stale", "15", 65, device_class="temperature"),
            state("sensor.b_diagnostic", "15", 5, device_class="signal_strength", unit_of_measurement="W"),
            state("sensor.c_unknown", "unknown", 7, device_class="temperature"),
            state("sensor.d_unbounded", "inf", 3, device_class="temperature"),
            state("sensor.e_temperature", "18", 24, device_class="temperature", token="fixture-only"),
            state("weather.expired", "rainy", 181),
            state("weather.home", "sunny", 75, temperature=float("inf"), temperature_unit="°C"),
            state("switch.fan", "off", 5),
            state("input_text.example", "fixture-only", 4),
            state("sensor.textual", "fixture-only", 3, device_class="temperature"),
        ]))
        db.commit()
        matched = api.entities(db=db, purpose="dashboard", limit=1)
        assert [entity["entity_id"] for entity in matched["entities"]] == ["sensor.e_temperature"]
        assert matched["cached"]
        assert matched["entities"][0]["source_reported_at"] == (now - timedelta(minutes=24)).isoformat()
        assert "fixture-only" not in db.get(HAEntityCache, "sensor.e_temperature").attributes_json
        assert db.get(HAEntityCache, "input_text.example") is None
        assert db.get(HAEntityCache, "sensor.textual").state is None
        weather = api.entities(db=db, domain="weather", purpose="dashboard", limit=2)
        assert [entity["entity_id"] for entity in weather["entities"]] == ["weather.home"]
        assert weather["entities"][0]["dashboard_valid_until"] == (now + timedelta(minutes=105)).isoformat()
        assert "temperature" not in weather["entities"][0]["attributes"]
        assert api.entities(db=db, domain="switch", purpose="dashboard", limit=2)["entities"] == []
    engine.dispose()


def test_entity_cache_schema_migrates_existing_rows_idempotently(monkeypatch):
    from sqlalchemy import inspect

    from inkdaddy_hub import database

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE ha_entity_cache (entity_id VARCHAR PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO ha_entity_cache VALUES ('weather.test')")
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    database.init_db()
    assert [column["name"] for column in inspect(engine).get_columns("ha_entity_cache")].count("source_reported_at") == 1
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT * FROM ha_entity_cache").one() == ("weather.test", None)


def test_playback_clock_schema_migrates_existing_rows_idempotently(monkeypatch):
    from inkdaddy_hub import database
    from inkdaddy_hub.models import Playback, PlaybackOffer, PlaybackSlot

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE playback RENAME COLUMN anchor_second TO anchor_hour")
        connection.exec_driver_sql("ALTER TABLE playback DROP COLUMN manual_request_second")
        connection.exec_driver_sql("ALTER TABLE playback_slots RENAME COLUMN scheduled_second TO hour")
        connection.exec_driver_sql("ALTER TABLE playback_offers RENAME COLUMN scheduled_second TO hour")
        connection.exec_driver_sql("ALTER TABLE playback DROP COLUMN manual_requested_at")
        connection.exec_driver_sql("ALTER TABLE playback_slots DROP COLUMN manual_observed_at")
        connection.exec_driver_sql("ALTER TABLE playback_slots DROP COLUMN retained_offer_id")
        connection.exec_driver_sql(
            "INSERT INTO playback (device_id, playlist_json, anchor_hour, anchor_position, paused) "
            "VALUES (1, '[]', 7, 2, 1)"
        )
        connection.exec_driver_sql(
            "INSERT INTO playback_slots (device_id, hour, source_type, source_id, title, frame_id, render_key) "
            "VALUES (1, 7, 'dashboard', 'sample', 'Home Overview', 'old-frame', 'old-render')"
        )
        connection.exec_driver_sql(
            "INSERT INTO playback_offers (id, device_id, hour, source_type, source_id, title, frame_id, offered_at) "
            "VALUES (3, 1, 7, 'dashboard', 'sample', 'Home Overview', 'old-frame', '2026-11-18T07:00:05')"
        )
        connection.exec_driver_sql("UPDATE playback SET current_offer_id = 3 WHERE device_id = 1")
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    database.init_db()
    with Session(engine) as db:
        state = db.get(Playback, 1)
        slot = db.get(PlaybackSlot, (1, 25200))
        assert state.paused and (state.anchor_second, state.anchor_position) == (25200, 2)
        assert state.manual_request_second is state.manual_requested_at is None
        assert db.get(PlaybackOffer, state.current_offer_id).scheduled_second == 25200
        assert (slot.frame_id, slot.render_key) == ("old-frame", "old-render")
        assert slot.manual_observed_at is slot.retained_offer_id is None
    engine.dispose()


def test_dev23_minute_clock_preserves_pending_offer_pause_and_manual_times(monkeypatch):
    from inkdaddy_hub import database
    from inkdaddy_hub.models import Device, MatterFrameTransfer, Playback, PlaybackOffer, PlaybackSlot

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    # Use dev.23 column names and persisted minute values, including slot keys
    # that would collide halfway through an in-place multiplication.
    with Session(engine) as db:
        db.add(Device(id=1, device_uid="fixture", name="Fixture"))
        db.add(Playback(device_id=1, playlist_json="[]", anchor_second=60, anchor_position=2,
                        first_new_interval_second=120, paused=True, manual_request_second=60, current_offer_id=3))
        for due in (60, 3600):
            db.add(PlaybackSlot(device_id=1, scheduled_second=due, source_type="photo", source_id="7",
                                frame_id="image", render_key="render", title="Photo"))
        db.add(PlaybackOffer(id=3, device_id=1, scheduled_second=60, source_type="photo", source_id="7",
                             frame_id="image", title="Photo", offered_at=datetime(2026, 11, 1)))
        db.add(MatterFrameTransfer(device_id=1, transfer_id="transfer", playback_offer_id=3, render_key="render"))
        db.commit()
    with engine.begin() as connection:
        for table, column in (("playback", "anchor"), ("playback", "first_new_interval"),
                              ("playback", "manual_request"), ("playback_slots", "scheduled"),
                              ("playback_offers", "scheduled")):
            connection.exec_driver_sql(f"ALTER TABLE {table} RENAME COLUMN {column}_second TO {column}_minute")
        connection.exec_driver_sql("ALTER TABLE devices DROP COLUMN interlace_dashboard_id")
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    database.init_db()
    with Session(engine) as db:
        assert db.get(Device, 1).interlace_dashboard_id is None
        state = db.get(Playback, 1)
        assert (state.paused, state.anchor_second, state.anchor_position) == (True, 3600, 2)
        assert (state.first_new_interval_second, state.manual_request_second) == (7200, 3600)
        for second in (3600, 216000):
            assert db.get(PlaybackSlot, (1, second)).frame_id == "image"
        transfer = db.get(MatterFrameTransfer, 1)
        assert transfer.playback_offer_id == state.current_offer_id == 3
        assert db.get(PlaybackOffer, 3).scheduled_second == 3600
    engine.dispose()


def test_overview_upgrade_preserves_sources_and_later_layout_edits(monkeypatch):
    import json
    from copy import deepcopy

    from inkdaddy_hub import database
    from inkdaddy_hub.models import Dashboard
    from inkdaddy_hub.services.dashboard import SAMPLE_DASHBOARD, load_dashboard_yaml

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    old = load_dashboard_yaml(SAMPLE_DASHBOARD)
    old["name"] = "Household"
    old["theme"] = {"primary": "black", "accent": "green"}
    previous = {"forecast": (2, 1), "date": (0, 0), "device_battery": (0, 1), "weather": (2, 0)}
    for item in old["widgets"]:
        kind = item["source"]["type"]
        item.update(x=previous[kind][0], y=previous[kind][1], w=2, h=1, id=f"custom-{kind}")
        if kind == "weather":
            item["source"]["entity_id"] = "weather.test"
        if kind == "forecast":
            item["source"].update(location="Paris", latitude=48.857, longitude=2.352, timezone="Europe/Paris", unit="C")
        item["options"]["label"] = f"My {kind}"

    # Existing dashboards and frontend presets can store widgets in any order.
    old["widgets"].reverse()
    source = json.dumps(old)
    custom = deepcopy(old)
    custom["widgets"][0]["w"] = 1
    untouched = json.dumps(custom)
    with Session(engine) as db:
        db.add_all([Dashboard(id=1, name="Household", slug="mine", current_yaml=source),
                    Dashboard(id=2, name="Custom", slug="custom", current_yaml=untouched)])
        db.commit()
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    with Session(engine) as db:
        assert db.get(Dashboard, 2).current_yaml == untouched
        migrated = load_dashboard_yaml(db.get(Dashboard, 1).current_yaml)
        expected = deepcopy(old)
        rectangles = {"date": (0, 0, 1), "weather": (1, 0, 2), "device_battery": (3, 0, 1), "forecast": (0, 1, 4)}
        for item in expected["widgets"]:
            item["x"], item["y"], item["w"] = rectangles[item["source"]["type"]]
        assert migrated == expected
        db.get(Dashboard, 1).current_yaml = source
        db.commit()
    database.init_db()
    with Session(engine) as db:
        assert db.get(Dashboard, 1).current_yaml == source
    engine.dispose()
