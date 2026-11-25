import json
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from inkdaddy_hub.api import dashboards, home_assistant
from inkdaddy_hub.database import Base, get_db
from inkdaddy_hub.models import Dashboard, Device, Photo, Setting
from inkdaddy_hub.services import dashboard_calendar as calendar
from inkdaddy_hub.services.dashboard_content import SITE_SETTING, dashboard_config, resolve_photo_overlay, resolve_widget_text

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
        assert client.put(f"{route}/test-panel", json={"dashboard_id": "@photo-details"}).is_success
        assert client.get(route).json()["overlays"][0]["dashboard_id"] == "@photo-details"
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


@pytest.mark.parametrize(("has_date", "location", "expected"), [
    (True, "Example city", ("Nov 22, 2025 · 1 year ago", "Example city")),
    (True, "", ("Nov 22, 2025 · 1 year ago", "")),
    (False, "Example city", ("Example city", "")),
    (False, "", None),
])
def test_photo_details_caption_uses_only_available_fields(catalog, monkeypatch, has_date, location, expected):
    from inkdaddy_hub.services import dashboard_content

    captured = NOW.date().replace(year=2025) if has_date else None
    monkeypatch.setattr(dashboard_content, "capture_details",
                        lambda *a, **kw: dashboard_content.CaptureDetails(captured, location))
    with catalog() as db:
        photo = db.get(Photo, 1)
        overlay = resolve_photo_overlay(db, "@photo-details", NOW, None, photo=photo)
        if expected is None:
            assert overlay is None
            return
        widget, = overlay.widgets
        assert (widget.value, widget.detail) == expected
        base = Image.new("RGB", (800, 480), "black")
        painted = overlay.render(base)
        left, top, right, bottom = painted.getbbox()
        assert 15 <= left < right <= 400 and 395 <= top < bottom <= 464
        restored = dashboard_content.PhotoOverlay.from_snapshot(overlay.snapshot)
        assert restored.render(base).tobytes() == painted.tobytes()


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


def test_existing_clock_tiles_disappear_from_editor_and_saved_layout(catalog):
    clock = {**DATE, "id": "old-clock", "source": {"type": "clock"}}
    mixed = {"name": "Mixed", "widgets": [clock, {**DATE, "x": 1}], "custom": {"version": 7}}
    original = json.dumps(mixed)
    with catalog.begin() as db:
        db.add(Dashboard(slug="mixed", name="Mixed", current_yaml=original))
        db.add(Dashboard(slug="only-clock", name="Only clock",
                         current_yaml=json.dumps({"name": "Only clock", "widgets": [clock]})))
    with catalog() as db:
        saved = dashboards.get_dashboard("mixed", db)
        raw = json.loads(saved["yaml"])
        assert raw == {**mixed, "widgets": [{**DATE, "x": 1}]}
        assert saved["config"]["widgets"] == [{**DATE, "x": 1}]
        assert db.scalar(select(Dashboard.current_yaml).where(Dashboard.slug == "mixed")) == original
        empty = dashboards.get_dashboard("only-clock", db)
        assert empty["config"]["widgets"] == [] and json.loads(empty["yaml"])["widgets"] == []
        assert dashboards.update_dashboard("only-clock", {"yaml": empty["yaml"]}, db)["config"]["widgets"] == []
        assert dashboards.get_dashboard("only-clock", db)["config"]["widgets"] == []
        archived = db.scalar(select(Dashboard).where(Dashboard.slug == "only-clock"))
        assert dashboard_config(archived, historical=True)["widgets"][0]["source"]["type"] == "clock"
        assert dashboards.delete_dashboard("only-clock", db) == {"deleted": True}
        for operation in (lambda: dashboards.create_dashboard({"yaml": original}, db),
                          lambda: dashboards.update_dashboard("mixed", {"yaml": original}, db)):
            with pytest.raises(HTTPException) as invalid:
                operation()
            assert invalid.value.status_code == 422


def test_selected_zone_and_battery_use_the_same_safe_cache_as_pickers(catalog):
    from inkdaddy_hub.models import HAEntityCache
    from inkdaddy_hub.services.home_assistant import normalize_state_response, replace_entity_cache

    now = datetime.now(timezone.utc)
    def row(entity_id, state, **attrs):
        return {"entity_id": entity_id, "state": state, "attributes": attrs, "last_reported": now.isoformat()}

    with catalog() as db:
        replace_entity_cache(db, normalize_state_response([
            row("zone.studio", "7", friendly_name="Studio", latitude=42, longitude=42, persons=["sensitive"]),
            row("sensor.remote", "0", device_class="battery", unit_of_measurement="%"),
            row("sensor.volts", "4.1", device_class="voltage", unit_of_measurement="V"),
            row("sensor.wrong", "400", device_class="battery", unit_of_measurement="%"),
            row("sensor.unknown", "unknown", device_class="battery", unit_of_measurement="%"),
        ]))
        db.commit()
        zones = home_assistant.entities(purpose="location", limit=40, db=db)["entities"]
        assert [(z["name"], z["state"], z["attributes"], z["source_reported_at"]) for z in zones] == [
            ("Studio", None, {}, None)
        ]
        assert [b["entity_id"] for b in home_assistant.entities(purpose="battery", limit=10, db=db)["entities"]] == [
            "sensor.remote", "sensor.unknown", "sensor.wrong"
        ]
        zone = db.get(HAEntityCache, "zone.studio")
        assert zone.state is None and zone.attributes_json == "{}" and zone.source_reported_at is None
        config = {"name": "Selected", "widgets": [
            {**DATE, "x": 0, "source": {"type": "location", "entity_id": "zone.studio"}},
            {**DATE, "x": 1, "source": {"type": "device_battery", "entity_id": "sensor.remote"}},
            {**DATE, "x": 2, "source": {"type": "device_battery", "entity_id": "sensor.volts"}},
        ]}
        registered = dashboards.create_dashboard({"yaml": json.dumps(config)}, db)
        reloaded = dashboards.get_dashboard(registered["id"], db)["config"]
        assert [w.value for w in resolve_widget_text(db, reloaded, now, None, observed_at=now)] == [
            "Studio", "0%", "Unavailable"
        ]
        late = now + timedelta(hours=2)
        later = resolve_widget_text(db, reloaded, late, None, observed_at=late)
        assert [w.value for w in later] == ["Studio", "0%", "Unavailable"]
        assert later[1].detail.startswith("Reported ") and str(now.year) in later[1].detail
        db.delete(zone)
        db.flush()
        assert resolve_widget_text(db, reloaded, now, None, observed_at=now)[0].value == "Unavailable"


def test_resized_photo_overlays_restore_both_new_and_historical_geometry():
    from inkdaddy_hub.services.dashboard_content import PhotoOverlay, WidgetText

    config = {"widgets": [{**DATE, "x": 0, "y": 0, "w": 1, "h": 2}]}
    overlay = PhotoOverlay(config, [WidgetText("Fixture", "123")])
    original = Image.new("RGB", (800, 480), "red")
    expanded = overlay.render(original)
    assert expanded.getpixel((100, 350)) != original.getpixel((100, 350))
    assert PhotoOverlay.from_snapshot(overlay.snapshot).render(original).tobytes() == expanded.tobytes()
    legacy = [{k: v for k, v in overlay.snapshot[0].items() if k != "h"}]
    repaired = PhotoOverlay.from_snapshot(legacy).render(original)
    assert repaired.getpixel((100, 350)) == original.getpixel((100, 350))
    assert repaired.getpixel((100, 100)) != original.getpixel((100, 100))


def test_retired_starter_clock_becomes_date_only_when_still_present(catalog):
    from inkdaddy_hub.services.dashboard import HISTORICAL_SAMPLE_DASHBOARD, load_dashboard_yaml

    saved = load_dashboard_yaml(HISTORICAL_SAMPLE_DASHBOARD)
    saved["name"] = "My saved dashboard"
    saved["widgets"][1]["source"]["entity_id"] = "weather.selected"
    saved["widgets"][3]["source"] = {"type": "home_assistant", "entity_id": "sensor.den_battery"}
    with catalog.begin() as db:
        record = Dashboard(slug="saved", name=saved["name"], current_yaml=json.dumps(saved))
        db.add(record)
    with catalog() as db:
        current = dashboards.get_dashboard("saved", db)
        starter_date, = [w for w in dashboard_config(None)["widgets"] if w["source"] == {"type": "date"}]
        assert (starter_date["x"], starter_date["y"], starter_date["w"], starter_date["h"]) == (0, 0, 2, 1)
        assert current["config"]["widgets"] == [*saved["widgets"][1:], starter_date]
        assert json.loads(current["yaml"])["widgets"] == current["config"]["widgets"]
        dashboards.update_dashboard("saved", {"yaml": current["yaml"]}, db)
        record = db.scalar(select(Dashboard).where(Dashboard.slug == "saved"))
        assert dashboard_config(record, historical=True)["widgets"] == saved["widgets"]
        # A deliberate removal, including when dev24 had already saved the
        # blank corner, must survive a reload.
        empty_corner = {**saved, "widgets": saved["widgets"][1:]}
        dashboards.update_dashboard("saved", {"yaml": json.dumps(empty_corner)}, db)
        assert dashboards.get_dashboard("saved", db)["config"]["widgets"] == saved["widgets"][1:]
        existing_date = {**DATE, "x": 3, "y": 1}
        record.current_yaml = json.dumps({**saved, "widgets": [saved["widgets"][0], existing_date]})
        assert dashboard_config(record)["widgets"] == [existing_date]


def test_date_on_dashboard_and_preview_rolls_over_at_home_assistant_midnight(catalog, monkeypatch):
    from inkdaddy_hub.services.dashboard_content import WidgetText, render_dashboard
    from inkdaddy_hub.services.rendering import render_preview_png_bytes

    class PreviewClock(datetime):
        @classmethod
        def now(cls, tz):
            return datetime(2026, 11, 25, 4, 59, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(dashboards, "datetime", PreviewClock)
    before = PreviewClock.now(timezone.utc)
    after = before + timedelta(minutes=1)
    with catalog() as db:
        config = dashboards.get_dashboard("cards", db)["config"]
        tuesday = [WidgetText("Date", "Tue, Nov 24")]
        wednesday = [WidgetText("Date", "Wed, Nov 25")]
        assert resolve_widget_text(db, config, before, None, observed_at=after) == tuesday
        assert resolve_widget_text(db, config, after, None, observed_at=before) == wednesday
        preview = dashboards.preview_dashboard("cards", device_uid=None, photo_id=None, db=db)
        assert preview.body == render_preview_png_bytes(render_dashboard(config, tuesday))


def test_pre_palette_theme_metadata_can_be_opened_and_replaced(catalog):
    with catalog() as db:
        config = {"name": "Archive", "widgets": [DATE], "theme": "classic"}
        db.add(Dashboard(slug="archive", name="Archive", current_yaml=json.dumps(config)))
        db.flush()
        result = dashboards.get_dashboard("archive", db)
        assert result["config"]["theme"] == {}
        assert json.loads(result["yaml"])["theme"] == {}
        config["theme"] = {"primary": "red", "accent": "white"}
        saved = dashboards.update_dashboard("archive", {"yaml": json.dumps(config)}, db)
        assert saved["config"]["theme"] == config["theme"]
