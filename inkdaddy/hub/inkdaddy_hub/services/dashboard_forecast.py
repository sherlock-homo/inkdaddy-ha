from __future__ import annotations

import json
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from http.client import HTTPException, HTTPSConnection
from threading import Lock
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..models import ForecastCache

_REFRESH = timedelta(hours=3)
_MAX_AGE = timedelta(hours=24)
_RETRY = timedelta(minutes=10)
_CACHE_LIMIT = 64
_MAX_RESPONSE = 64 * 1024
_refresh_lock = Lock()
_FETCH_ERRORS = (OSError, HTTPException, ValueError, RecursionError)
_CONDITIONS = {
    0: "sunny", 1: "sunny", 2: "partlycloudy", 3: "cloudy", 45: "fog", 48: "fog",
    51: "rainy", 53: "rainy", 55: "rainy", 56: "snowy-rainy", 57: "snowy-rainy",
    61: "rainy", 63: "rainy", 65: "pouring", 66: "snowy-rainy", 67: "snowy-rainy",
    71: "snowy", 73: "snowy", 75: "snowy", 77: "snowy", 80: "rainy", 81: "rainy",
    82: "pouring", 85: "snowy", 86: "snowy", 95: "lightning-rainy", 96: "hail", 99: "hail",
}


@dataclass(frozen=True)
class ForecastDay:
    date: str
    high: int
    low: int
    condition: str


@dataclass(frozen=True, order=True)
class ForecastPlace:
    location: str
    latitude: float
    longitude: float
    timezone: str
    unit: str = "F"

    @property
    def key(self) -> str:
        return f"{self.latitude:.3f},{self.longitude:.3f},{self.timezone},{self.unit}"


def forecast_place(source: dict[str, Any]) -> ForecastPlace:
    name = source.get("location")
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or not name.isprintable():
        raise ValueError("Choose a forecast city.")
    for field, limit in (("latitude", 90), ("longitude", 180)):
        value = source.get(field)
        if type(value) not in (float, int) or not -limit <= value <= limit:
            raise ValueError("Choose a forecast city with valid coordinates.")
    zone = source.get("timezone")
    if not isinstance(zone, str) or not 0 < len(zone) <= 80:
        raise ValueError("Choose a forecast city with a time zone.")
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("Choose a forecast city with a valid time zone.") from None
    unit = source.get("unit", "F")
    if unit not in ("C", "F"):
        raise ValueError("Choose Fahrenheit or Celsius.")
    return ForecastPlace(name.strip(), round(source["latitude"], 3), round(source["longitude"], 3), zone, unit)


def _read_provider(host: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
    # Both callers use fixed HTTPS hosts and paths. Only the query is configurable.
    connection = HTTPSConnection(host, timeout=4)
    try:
        connection.request("GET", path + "?" + urlencode(params), headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("Forecast provider unavailable.")
        raw = response.read(_MAX_RESPONSE + 1)
        if len(raw) > _MAX_RESPONSE:
            raise ValueError("Forecast response is too large.")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Invalid forecast response.")
        return result
    finally:
        connection.close()


def location_choices(query: str) -> list[dict[str, Any]]:
    query = query.strip()
    if not 2 <= len(query) <= 80 or not query.isprintable():
        raise ValueError("Enter a city name between 2 and 80 characters.")
    result = _read_provider("geocoding-api.open-meteo.com", "/v1/search",
                            {"name": query, "count": 8, "language": "en", "format": "json"})
    choices = []
    rows = result.get("results", [])
    if not isinstance(rows, list):
        raise ValueError("Invalid city search response.")
    for row in rows[:8]:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            continue
        names = [row.get(k) for k in ("name", "admin1", "country_code")]
        name = ", ".join(dict.fromkeys(n for n in names if isinstance(n, str) and n))
        try:
            location = forecast_place({**row, "location": name})
        except ValueError:
            continue
        choices.append({k: v for k, v in asdict(location).items() if k != "unit"})
    return choices


def forecast_requests(config: dict[str, Any]) -> set[ForecastPlace]:
    return {forecast_place(w["source"]) for w in config["widgets"] if w["source"].get("type") == "forecast"}


def _forecast_rows(place: ForecastPlace, now: datetime) -> list[dict[str, Any]]:
    result = _read_provider("api.open-meteo.com", "/v1/forecast", {
        "latitude": place.latitude, "longitude": place.longitude, "timezone": place.timezone,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min", "forecast_days": 9,
        "temperature_unit": "fahrenheit" if place.unit == "F" else "celsius",
    })
    daily = result.get("daily")
    units = result.get("daily_units")
    fields = ("time", "temperature_2m_max", "temperature_2m_min", "weather_code")
    if not isinstance(daily, dict) or not isinstance(units, dict) or not all(
        isinstance(daily.get(k), list) for k in fields
    ) or any(units.get(k) != "°" + place.unit for k in fields[1:3]):
        raise ValueError("Invalid daily forecast.")
    rows = []
    today = now.astimezone(ZoneInfo(place.timezone)).date()
    for when, high, low, code in zip(*(daily[k][:9] for k in fields), strict=True):
        if not isinstance(when, str) or len(when) != 10 or type(code) is not int or code not in _CONDITIONS:
            continue
        if any(type(t) not in (int, float) or not -260 <= t <= 260 for t in (high, low)) or high < low:
            continue
        day = date.fromisoformat(when)
        if today <= day <= today + timedelta(days=8):
            rows.append(asdict(ForecastDay(day.isoformat(), round(high), round(low), _CONDITIONS[code])))
    required = {(today + timedelta(days=i)).isoformat() for i in range(7)}
    if not required.issubset(row["date"] for row in rows):
        raise ValueError("Incomplete seven-day forecast.")
    return rows


def _refresh_place(engine: Engine, place: ForecastPlace, now: datetime) -> None:
    # Reserve a retry window before each fetch. Never hold a DB write lock across the network.
    utc = now.astimezone(timezone.utc).replace(tzinfo=None)
    from .playback import begin_playback_write

    with _refresh_lock, Session(engine) as db:
        row = db.get(ForecastCache, place.key)
        if row and utc < row.retry_at <= utc + _REFRESH:
            return
        # The worker may also be running in another process against this store.
        begin_playback_write(db)
        row = db.get(ForecastCache, place.key, populate_existing=True)
        if row and utc < row.retry_at <= utc + _REFRESH:
            return
        if not row:
            overflow = select(ForecastCache.key).order_by(ForecastCache.retry_at.desc()).offset(_CACHE_LIMIT - 1)
            db.execute(delete(ForecastCache).where(ForecastCache.key.in_(overflow)))
            row = ForecastCache(key=place.key)
            db.add(row)
        row.retry_at = utc + _RETRY
        db.commit()
    try:
        days = _forecast_rows(place, now)
    except _FETCH_ERRORS:
        return
    with _refresh_lock, Session(engine) as db:
        row = db.get(ForecastCache, place.key)
        if row and row.retry_at == utc + _RETRY:
            row.fetched_at = utc
            row.retry_at = utc + _REFRESH
            row.days_json = json.dumps(days, separators=(",", ":"))
            db.commit()


def refresh_forecast_cache(engine: Engine, places: Iterable[ForecastPlace], now: datetime) -> None:
    jobs = sorted(set(places))[:_CACHE_LIMIT]
    if len(jobs) == 1:
        _refresh_place(engine, jobs[0], now)
    elif jobs:
        with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
            list(pool.map(lambda place: _refresh_place(engine, place, now), jobs))


def forecast_days(db: Session, place: ForecastPlace, when: datetime, observed_at: datetime) -> tuple[ForecastDay, ...]:
    row = db.get(ForecastCache, place.key, populate_existing=True)
    if not row or not row.fetched_at or not row.days_json:
        return ()
    utc = observed_at.astimezone(timezone.utc).replace(tzinfo=None)
    if not -timedelta(minutes=1) <= utc - row.fetched_at <= _MAX_AGE:
        return ()
    start = when.astimezone(ZoneInfo(place.timezone)).date()
    # Require the seven requested local dates in order; never relabel a missing day.
    by_date = {day["date"]: day for day in json.loads(row.days_json)}
    days = [by_date.get((start + timedelta(days=i)).isoformat()) for i in range(7)]
    return tuple(ForecastDay(**day) for day in days) if all(days) else ()
