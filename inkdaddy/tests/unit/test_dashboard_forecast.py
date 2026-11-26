import json
from datetime import datetime, timedelta, timezone

import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from inkdaddy_hub.api import dashboards
from inkdaddy_hub.database import Base
from inkdaddy_hub.services import dashboard_forecast as forecast
from inkdaddy_hub.services.dashboard import DashboardValidationError, validate_dashboard_config
from inkdaddy_hub.services.dashboard_content import ForecastText, PhotoOverlay, resolve_widget_text

NOW = datetime(2026, 11, 25, 4, 40, tzinfo=timezone.utc)  # Nov 24 in Atlanta
SOURCE = {"type": "forecast", "location": "Atlanta, GA", "latitude": 33.749, "longitude": -84.388,
          "timezone": "America/New_York"}


def layout(**changes):
    return {"widgets": [{"id": "outlook", "type": "weather_summary", "source": {**SOURCE, **changes},
                         "x": 0, "y": 1, "w": 2, "h": 1}]}


def response(now=NOW):
    dates = [now.date() + timedelta(days=i) for i in range(-1, 8)]
    conditions = ["tsrain", "pcloudy", "cloudy", "clear", "lightrain", "ts", "rainsnow", "humid", "rain"]
    return {"product": "civillight", "dataseries": [
        {"date": int(day.strftime("%Y%m%d")), "temp2m": {"max": 13 + i, "min": 4 + i}, "weather": conditions[i]}
        for i, day in enumerate(dates)
    ]}


@pytest.mark.parametrize("change", [{"latitude": 91}, {"longitude": False}, {"timezone": "../tmp"},
                                    {"unit": "X"}, {"latitude": float("nan")}, {"location": "\nBad"}])
def test_invalid_forecast_widgets_are_rejected(change):
    with pytest.raises(DashboardValidationError):
        validate_dashboard_config(layout(**change))


def test_cache_survives_restart_midnight_outage_and_incomplete_response(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "forecast.sqlite"))
    Base.metadata.create_all(engine)
    config = validate_dashboard_config(layout())
    place, = forecast.forecast_requests(config)
    calls = []
    def provider(host, path, params):
        calls.append((host, path, params))
        return response()
    monkeypatch.setattr(forecast, "_read_provider", provider)
    forecast.refresh_forecast_cache(engine, [place, place], NOW)
    engine.dispose()
    forecast.refresh_forecast_cache(engine, [place], NOW + timedelta(minutes=10))
    assert len(calls) == 1
    host, path, params = calls[0]
    assert (host, path) == ("www.7timer.info", "/bin/api.pl")
    assert params == {"lat": 33.749, "lon": -84.388, "product": "civillight", "output": "json", "unit": "metric"}
    after_midnight = NOW + timedelta(hours=1)
    with Session(engine) as db:
        widget, = resolve_widget_text(db, config, NOW, None, observed_at=NOW)
        assert isinstance(widget, ForecastText) and widget.value == "Atlanta, GA" and widget.unit == "F"
        dates = [(NOW.date() + timedelta(days=i)).isoformat() for i in range(-1, 8)]
        assert [d.date for d in widget.days] == dates[:7]
        assert widget.days[0] == forecast.ForecastDay("2026-11-24", 55, 39, "lightning-rainy")
        later, = resolve_widget_text(db, config, after_midnight, None, observed_at=after_midnight)
        assert [d.date for d in later.days] == dates[1:8]
        overlay = PhotoOverlay(config, [later])
        stored = json.loads(json.dumps(overlay.snapshot))
        restored = PhotoOverlay.from_snapshot(stored)
        assert restored.widgets == [later]
        canvas = Image.new("RGB", (800, 480), "white")
        assert restored.render(canvas).tobytes() == overlay.render(canvas).tobytes()
        assert overlay.render(canvas).tobytes() != canvas.tobytes()
    partial = response()
    partial["dataseries"][2]["temp2m"]["max"] = None
    monkeypatch.setattr(forecast, "_read_provider", lambda *args: partial)
    forecast.refresh_forecast_cache(engine, [place], NOW + timedelta(hours=4))
    with Session(engine) as db:
        later, = resolve_widget_text(db, config, after_midnight, None, observed_at=NOW + timedelta(hours=5))
        assert len(later.days) == 7
        expired, = resolve_widget_text(db, config, after_midnight, None, observed_at=NOW + timedelta(hours=25))
        assert expired.days == () and expired.detail == "Forecast unavailable"


def test_city_choice_endpoint_and_unit_are_scoped(monkeypatch):
    app = FastAPI()
    app.include_router(dashboards.router)
    client = TestClient(app)
    assert client.get("/api/dashboards/forecast/locations?query=x").status_code == 422
    assert client.get("/api/dashboards/forecast/locations?query=Berlin").status_code == 503
    def geocoder(host, path, params):
        assert (host, path) == ("geocoding-api.open-meteo.com", "/v1/search")
        assert params["name"] == "Berlin"
        return {"results": [None, {"name": "Invalid"}, {"location": "user input", "name": "Berlin", "admin1": "Berlin",
                            "country_code": "DE", "latitude": 52.52001, "longitude": 13.405, "timezone": "Europe/Berlin"}]}
    monkeypatch.setattr(forecast, "_read_provider", geocoder)
    locations = client.get("/api/dashboards/forecast/locations?query=Berlin").json()["locations"]
    assert locations == [{"location": "Berlin, DE", "latitude": 52.52, "longitude": 13.405, "timezone": "Europe/Berlin"}]
    place = forecast.forecast_place({**locations[0], "unit": "C"})
    def other_unit(host, path, params):
        assert params["unit"] == "metric" and params["lat"] == 52.52
        return response()
    monkeypatch.setattr(forecast, "_read_provider", other_unit)
    days = forecast._forecast_rows(place, NOW)
    assert days[0]["high"] == 14 and days[0]["low"] == 5


def test_concurrent_requests_share_a_single_forecast_and_retry_after_failure(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "forecast.sqlite"))
    Base.metadata.create_all(engine)
    place = forecast.forecast_place(SOURCE)
    started = Event()
    finish = Event()
    calls = []
    def provider(*args):
        calls.append(args[1])
        started.set()
        assert finish.wait(5)
        raise OSError("Temporary outage.")
    monkeypatch.setattr(forecast, "_read_provider", provider)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(forecast.refresh_forecast_cache, engine, [place], NOW)
        assert started.wait(5)
        forecast.refresh_forecast_cache(engine, [place], NOW + timedelta(seconds=1))
        finish.set()
        first.result(5)
    assert calls == ["/bin/api.pl"]
    forecast.refresh_forecast_cache(engine, [place], NOW + timedelta(minutes=5))
    assert len(calls) == 1
    monkeypatch.setattr(forecast, "_read_provider", lambda *args: response())
    forecast.refresh_forecast_cache(engine, [place], NOW + timedelta(minutes=11))
    with Session(engine) as db:
        assert len(forecast.forecast_days(db, place, NOW, NOW + timedelta(minutes=11))) == 7


def test_save_assign_and_immediate_refresh_use_forecast_and_preserve_dashboard_slug(tmp_path, monkeypatch):

    from inkdaddy_hub.api import devices
    from inkdaddy_hub.config import get_settings
    from inkdaddy_hub.database import get_db
    from inkdaddy_hub.models import Device, ForecastCache, PlaybackSlot, Setting
    from sqlalchemy import delete, select

    monkeypatch.setenv("INKDADDY_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    engine = create_engine("sqlite:///" + str(tmp_path / "forecast.sqlite"), connect_args={"timeout": 1})
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(dashboards.router)
    app.include_router(devices.router)

    def sessions():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db] = sessions
    client = TestClient(app)
    calls = []

    def provider(host, path, params):
        calls.append(path)
        # Normal playback writes can still commit while the provider is answering.
        with Session(engine) as other:
            other.add(Setting(key=f"independent-{len(calls)}", value_json="true"))
            other.commit()
        return response(datetime.now(timezone.utc))

    monkeypatch.setattr(forecast, "_read_provider", provider)
    try:
        with Session(engine) as db:
            db.add(Device(device_uid="frame", name="Panel", provisioned=True, mesh_connected=True))
            db.commit()
        slug = "forecast-locations"
        save = client.post("/api/dashboards", json={"id": slug, "yaml": json.dumps(layout())})
        assert save.status_code == 200 and len(calls) == 1
        assert client.get("/api/dashboards/" + slug).json()["id"] == slug
        # Simulate a saved layout first assigned after its forecast cache was purged.
        with Session(engine) as db:
            db.execute(delete(ForecastCache))
            db.commit()
        selected = client.put("/api/devices/frame", json={"selected_source_type": "dashboard", "selected_source_id": slug})
        assert selected.status_code == 200 and len(calls) == 2
        refresh = client.post("/api/devices/frame/manual-refresh?action=refresh_current")
        assert refresh.status_code == 200 and len(calls) == 2
        with Session(engine) as db:
            frame_id = refresh.json()["frame"]["frame_id"]
            slot = db.scalar(select(PlaybackSlot).where(PlaybackSlot.frame_id == frame_id))
            widget, = json.loads(slot.dashboard_render_json)["widgets"]
            assert len(widget["days"]) == 7
        # Replacing a city's source also warms that saved layout before responding.
        changed = client.put("/api/dashboards/" + slug, json={"yaml": json.dumps(
            layout(location="Berlin", latitude=52.52, longitude=13.405, timezone="Europe/Berlin"))})
        assert changed.status_code == 200 and len(calls) == 3
        with Session(engine) as db:
            db.execute(delete(ForecastCache))
            db.commit()
        assert client.get("/api/dashboards/" + slug + "/preview?device_uid=frame").status_code == 200
        assert len(calls) == 4
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_three_hour_forecast_extends_the_week_across_dst(monkeypatch):
    issued = datetime(2026, 10, 31, 0, tzinfo=timezone.utc)
    # A fall-back day in New York spans 25 hours. Place every sample on its actual local date.
    from datetime import date
    place = forecast.forecast_place(SOURCE)
    target = date(2026, 11, 1)
    feed = {"product": "civil", "init": issued.strftime("%Y%m%d%H"), "dataseries": [
        {"timepoint": hour, "temp2m": hour - 30, "weather": "clearday" if hour == 42 else "rainnight"}
        for hour in range(3, 75, 3)
    ]}
    monkeypatch.setattr(forecast, "_read_provider", lambda *args: feed)
    assert forecast._boundary_days(place, {target}) == [forecast.ForecastDay("2026-11-01", 70, 32, "sunny")]
    feed["dataseries"] = [row for row in feed["dataseries"] if row["timepoint"] != 36]
    assert forecast._boundary_days(place, {target}) == []


def test_seven_real_dates_when_model_is_already_on_tomorrow(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "rollover.sqlite"))
    Base.metadata.create_all(engine)
    place = forecast.forecast_place(SOURCE)
    data = response()
    data["dataseries"] = data["dataseries"][1:8]
    def provider(host, path, params):
        if params["product"] == "civil":
            raise OSError("Unavailable")
        return data
    monkeypatch.setattr(forecast, "_read_provider", provider)
    forecast.refresh_forecast_cache(engine, [place], NOW)
    with Session(engine) as db:
        result = forecast.forecast_days(db, place, NOW, NOW)
        assert len(result) == 7
        assert result[0].date == "2026-11-25" and result[-1].date == "2026-12-01"
