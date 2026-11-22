import json
from collections import OrderedDict
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from inkdaddy_hub.api import dashboards, home_assistant
from inkdaddy_hub.database import Base, get_db
from inkdaddy_hub.models import Dashboard, Device, Photo, Setting
from inkdaddy_hub.services import dashboard_calendar as calendar
from inkdaddy_hub.services.dashboard_content import SITE_SETTING, resolve_photo_overlay, resolve_widget_text

NOW = datetime(2026, 11, 22, 18, tzinfo=timezone.utc)


def agenda(show_titles=False):
    return {
        "id": "calendar", "type": "agenda", "x": 0, "y": 1, "w": 2, "h": 1,
        "source": {"type": "calendar", "entity_id": "calendar.shared"},
        "options": {"show_event_titles": show_titles},
    }


DATE = {
    "id": "date", "type": "entity_state", "x": 3, "y": 0, "w": 1, "h": 1,
    "source": {"type": "date"}, "options": {},
}


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)
    config = {"name": "Photo cards", "widgets": [DATE]}
    with session.begin() as db:
        db.add_all([
            Device(device_uid="test-panel", name="Test panel", provisioned=True),
            Dashboard(slug="cards", name=config["name"], current_yaml=json.dumps(config)),
            Dashboard(slug="empty", name="Empty", current_yaml='{"name":"Empty","widgets":[]}'),
            Photo(id=1, original_filename="photo.png", stored_filename="photo.png",
                  original_path=str(tmp_path / "photo.png"), processed_path=str(tmp_path / "processed.png"),
                  processed_status="ready"),
            Setting(key=SITE_SETTING, value_json='{"time_zone":"America/New_York"}'),
        ])
    monkeypatch.setattr(calendar, "_cache", OrderedDict())
    monkeypatch.setattr(calendar, "_next_fetch", {})
    monkeypatch.setenv("INKDADDY_HA_MODE", "supervisor")
    monkeypatch.setenv("SUPERVISOR_TOKEN", "synthetic-calendar-token")
    try:
        yield session
    finally:
        engine.dispose()


def test_calendar_is_opt_in_read_only_and_cache_keeps_only_render_fields(catalog, monkeypatch):
    calls = []
    ticks = [100]
    monkeypatch.setattr(calendar, "monotonic", lambda: ticks[0])

    def open_calendar(url, token, path, timeout):
        calls.append((url, path, timeout))
        assert token == "synthetic-calendar-token"
        if path == "/api/calendars":
            payload = [
                {"name": "Shared", "entity_id": "calendar.shared", "summary": "hidden"},
                {"name": "Invalid", "entity_id": "calendar.more/ignored"},
            ]
        else:
            payload = [
                {"start": {"dateTime": "2026-11-22T23:00:00+00:00"},
                 "end": {"dateTime": "2026-11-22T23:30:00+00:00"},
                 "summary": "Dinner", "description": "fixture-private", "location": "fixture-private"},
                {"start": {"date": "2026-11-23"}, "end": {"date": "2026-11-24"}, "summary": "Day off"},
                {"start": {"dateTime": "2026-11-22T23:00:00"}, "end": {"date": "2026-11-24"}},
            ]
        return BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(calendar, "_open_api", open_calendar)
    with catalog() as db:
        choices = home_assistant.list_calendars(db)
        assert choices == {"calendars": [{"name": "Shared", "entity_id": "calendar.shared"}], "error": None}
        config = {"widgets": [agenda()]}
        assert resolve_widget_text(db, config, NOW, None)[0].value == "Calendar unavailable"
        assert [urlsplit(call[1]).path for call in calls] == ["/api/calendars"]
        connection, scope = calendar.calendar_connection(db)
        calendar.refresh_calendar_cache(connection, scope, calendar.calendar_requests(config), NOW)
        calendar.refresh_calendar_cache(connection, scope, calendar.calendar_requests(config), NOW)
        assert len(calls) == 2
        parsed = urlsplit(calls[-1][1])
        assert parsed.path == "/api/calendars/calendar.shared"
        assert parse_qs(parsed.query)["start"] == [NOW.isoformat()]
        widget = resolve_widget_text(db, config, NOW, None)[0]
        assert (widget.value, widget.detail) == ("18:00 · Busy", "Tomorrow · All day · Busy")
        assert "fixture-private" not in repr(calendar._cache)
        config = {"widgets": [agenda(True)]}
        assert resolve_widget_text(db, config, NOW, None)[0].value == "Calendar unavailable"
        calendar.refresh_calendar_cache(connection, scope, calendar.calendar_requests(config), NOW)
        assert resolve_widget_text(db, config, NOW, None)[0].value == "18:00 · Dinner"
        monkeypatch.setenv("SUPERVISOR_TOKEN", "synthetic-second-token")
        assert resolve_widget_text(db, config, NOW, None)[0].value == "Calendar unavailable"
        monkeypatch.setenv("SUPERVISOR_TOKEN", "synthetic-calendar-token")
        ticks[0] += 46 * 60
        assert resolve_widget_text(db, config, NOW, None)[0].value == "Calendar unavailable"

        calendar.refresh_calendar_cache(connection, scope, calendar.calendar_requests(config), NOW)
        assert resolve_widget_text(db, config, NOW, None)[0].value == "18:00 · Dinner"
        ticks[0] += 26 * 60

        def deny_calendar(_url, _token, _path, _timeout):
            raise HTTPError(None, 403, "synthetic denial", None, None)

        monkeypatch.setattr(calendar, "_open_api", deny_calendar)
        calendar.refresh_calendar_cache(connection, scope, calendar.calendar_requests(config), NOW)
        assert resolve_widget_text(db, config, NOW, None)[0].value == "Calendar unavailable"


def test_photo_overlay_assignment_preview_and_removal(catalog, monkeypatch, tmp_path):
    base_photo = Image.new("RGB", (800, 480), (10, 40, 90))
    monkeypatch.setattr(dashboards, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(dashboards, "load_color_managed_photo", lambda *_: base_photo)
    app = FastAPI()
    app.include_router(dashboards.router)

    def make_session():
        with catalog() as db:
            yield db

    app.dependency_overrides[get_db] = make_session
    route = "/api/dashboards/photo-overlays"
    with TestClient(app) as client:
        assert client.get(route).json() == {"overlays": [{"device_uid": "test-panel", "dashboard_id": None}]}
        assert client.put(f"{route}/test-panel", json={"dashboard_id": "missing"}).status_code == 404
        assert client.put(f"{route}/test-panel", json={"dashboard_id": "empty"}).status_code == 422
        assert client.put(f"{route}/missing-panel", json={"dashboard_id": "cards"}).status_code == 404
        assert client.put(f"{route}/test-panel", json={"dashboard_id": "cards"}).json() == {
            "device_uid": "test-panel", "dashboard_id": "cards",
        }
        assert client.delete("/api/dashboards/cards").status_code == 409
        preview = client.post("/api/dashboards/preview", json={
            "yaml": json.dumps({"name": "Draft", "widgets": [DATE]}), "device_uid": "test-panel", "photo_id": "1",
        })
        assert preview.status_code == 200
        assert preview.headers["cache-control"] == "no-store"
        image = Image.open(BytesIO(preview.content)).convert("RGB")
        assert image.size == (800, 480)
        assert image.getpixel((10, 100)) == base_photo.getpixel((10, 100))
        assert image.getpixel((650, 85)) != base_photo.getpixel((650, 85))
        assert client.post("/api/dashboards/preview", json={"photo_id": 2}).status_code == 404
        assert client.put(f"{route}/test-panel", json={"dashboard_id": None}).json()["dashboard_id"] is None
        assert client.delete("/api/dashboards/cards").status_code == 200


def test_date_overlay_snapshot_tracks_rendered_text_and_preserves_photo(catalog):
    with catalog() as db:
        render = resolve_photo_overlay(db, "cards", NOW, None)
        later = resolve_photo_overlay(db, "cards", NOW.replace(day=23), None)
        assert render is not None and later is not None
        assert render.widgets[0].value == "Sun, Nov 22"
        assert render.snapshot != later.snapshot
        assert resolve_photo_overlay(db, None, NOW, None) is None
        assert resolve_photo_overlay(db, "empty", NOW, None) is None
        photo = Image.new("RGB", (800, 480), (10, 40, 90))
        rendered = render.render(photo)
        assert photo.getpixel((650, 85)) == (10, 40, 90)
        assert rendered.getpixel((650, 85)) != (10, 40, 90)
        assert rendered.getpixel((100, 300)) == (10, 40, 90)
