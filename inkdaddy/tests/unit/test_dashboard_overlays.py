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


def test_photo_widgets_preview_each_selected_original_and_handle_on_its_own(catalog, tmp_path, monkeypatch):
    from inkdaddy_hub.api import photos
    from inkdaddy_hub.services import dashboard_content
    from inkdaddy_hub.services.dashboard import validate_dashboard_config

    base = Image.new("RGB", (800, 480), (10, 40, 90))
    settings = SimpleNamespace(data_dir=tmp_path)
    monkeypatch.setattr(dashboard_content, "get_settings", lambda: settings)
    monkeypatch.setattr(dashboards, "get_settings", lambda: settings)
    monkeypatch.setattr(dashboards, "load_color_managed_photo", lambda *_: base)
    (tmp_path / "originals").mkdir()
    paths = [tmp_path / "originals" / f"photo{n}.jpg" for n in (1, 2)]
    for path, year in zip(paths, (2000, 2010)):
        tags = Image.Exif()
        tags[34665] = {36867: f"{year}:11:22 23:45:01"}
        tags[315] = "Artist fixture"
        Image.new("RGB", (4, 4)).save(path, exif=tags)
    widget = {**DATE, "source": {"type": "photo_metadata", "field": "date_taken"}, "options": {"label": "Captured"}}
    config = {"name": "Capture info", "widgets": [widget]}
    expected = []
    with catalog.begin() as db:
        p1 = db.get(Photo, 1)
        p1.original_path = str(paths[0])
        p2 = Photo(id=2, original_filename="photo2.jpg", stored_filename="photo2.jpg",
                   original_path=str(paths[1]), processed_path=p1.processed_path, processed_status="ready")
        db.add(p2)
        record = dashboard_content.get_dashboard_record(db, "cards")
        record.current_yaml = json.dumps(config)
        for photo, year in ((p1, 2000), (p2, 2010)):
            render = resolve_photo_overlay(db, "cards", NOW, None, photo=photo)
            assert render.widgets[0] == dashboard_content.WidgetText("Captured", f"Nov 22, {year}")
            expected.append(dashboards.render_preview_png_bytes(render.render(base)))
        unavailable = resolve_widget_text(db, validate_dashboard_config(config), NOW, None)
        assert unavailable[0].value == "Date unavailable"
        widget["source"]["field"] = "years_ago"
        age_config = validate_dashboard_config(config)
        # Local midnight falls between these UTC timestamps.
        assert resolve_widget_text(db, age_config, NOW.replace(hour=3), None, photo=p1)[0].value == "25 years ago"
        assert resolve_widget_text(db, age_config, NOW.replace(hour=6), None, photo=p1)[0].value == "26 years ago"

    def make_session():
        with catalog() as db:
            yield db

    app = FastAPI()
    app.include_router(dashboards.router)
    app.include_router(photos.router)
    app.dependency_overrides[get_db] = make_session
    with TestClient(app) as client:
        widget["source"]["field"] = "date_taken"
        for photo_id in (1, 2):
            result = client.get(f"/api/dashboards/cards/preview?photo_id={photo_id}")
            draft = client.post("/api/dashboards/preview", json={"yaml": json.dumps(config), "photo_id": photo_id})
            assert result.status_code == draft.status_code == 200
            assert result.headers["cache-control"] == "no-store"
            assert result.content == draft.content == expected[photo_id - 1]
            assert b"Artist fixture" not in result.content
        assert expected[0] != expected[1]
        assert client.get("/api/dashboards/cards/preview?photo_id=3").status_code == 404
        assert client.get("/api/dashboards/cards/preview?photo_id=-1").status_code == 422
        listing = client.get("/api/photos")
        assert listing.status_code == 200
        assert all(word not in listing.text for word in ("2000:11:22", "2010:11:22", "Artist fixture", "Nov 22"))
        widget["source"]["field"] = "Artist"
        assert client.post("/api/dashboards/preview", json={"yaml": json.dumps(config), "photo_id": 1}).status_code == 422


def test_skips_original_without_a_photo_widget(catalog, monkeypatch):
    from inkdaddy_hub.services import dashboard_content

    def unexpected(*_args, **_kwargs):
        pytest.fail("A dashboard without a photo metadata widget read an original")

    monkeypatch.setattr(dashboard_content, "capture_details", unexpected)
    with catalog() as db:
        assert resolve_photo_overlay(db, "cards", NOW, None, photo=db.get(Photo, 1)).widgets[0].label == "Date"
