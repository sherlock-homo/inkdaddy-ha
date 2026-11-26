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
    "clear": "sunny", "pcloudy": "partlycloudy", "mcloudy": "cloudy", "cloudy": "cloudy",
    "humid": "fog", "fog": "fog", "windy": "windy", "lightrain": "rainy", "oshower": "rainy",
    "ishower": "rainy", "rain": "pouring", "lightsnow": "snowy", "snow": "snowy",
    "rainsnow": "snowy-rainy", "ts": "lightning", "tsrain": "lightning-rainy",
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
        return f"7timer:{self.latitude:.3f},{self.longitude:.3f},{self.timezone},{self.unit}"


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


def _temperature(celsius: float, unit: str) -> int:
    return round(celsius * 9 / 5 + 32 if unit == "F" else celsius)


def _forecast_feed(place: ForecastPlace, product: str) -> dict[str, Any]:
    data = _read_provider("www.7timer.info", "/bin/api.pl", {
        "lat": place.latitude, "lon": place.longitude, "product": product, "output": "json", "unit": "metric",
    })
    if data.get("product") != product or not isinstance(data.get("dataseries"), list):
        raise ValueError("Invalid forecast response.")
    return data


def _boundary_days(place: ForecastPlace, wanted: set[date]) -> list[ForecastDay]:
    # CIVIL Light publishes seven dates. Its three-hour companion spans eight
    # days, so slots just across local midnight can still show a complete week.
    feed = _forecast_feed(place, "civil")
    stamp = feed.get("init")
    if not isinstance(stamp, str) or len(stamp) != 10:
        raise ValueError("Invalid forecast issue time.")
    issued = datetime.strptime(stamp, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    zone = ZoneInfo(place.timezone)
    by_day: dict[date, dict[int, tuple[float, str]]] = {}
    for row in feed["dataseries"][:64]:
        if not isinstance(row, dict):
            continue
        hour, temp, code = row.get("timepoint"), row.get("temp2m"), row.get("weather")
        if (type(hour) is not int or not 0 < hour <= 192 or hour % 3 or type(temp) not in (int, float)
                or not -90 <= temp <= 65 or not isinstance(code, str)):
            continue
        local = (issued + timedelta(hours=hour)).astimezone(zone)
        condition = _CONDITIONS.get(code.removesuffix("night").removesuffix("day"))
        if local.date() in wanted and condition:
            by_day.setdefault(local.date(), {})[hour] = (temp, condition)
    days = []
    for day, samples in by_day.items():
        # With a DST transition a complete local day has 7–9 samples.
        midnight = datetime.combine(day, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
        end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
        start_hour = int((midnight - issued).total_seconds() // 3600)
        expected = {h for h in range(start_hour, start_hour + 27)
                    if h % 3 == 0 and midnight <= issued + timedelta(hours=h) < end}
        if not expected or expected != samples.keys():
            continue
        midday = min(samples, key=lambda h: abs((issued + timedelta(hours=h)).astimezone(zone).hour - 12))
        temps = [t for t, _ in samples.values()]
        days.append(ForecastDay(day.isoformat(), _temperature(max(temps), place.unit),
                                _temperature(min(temps), place.unit), samples[midday][1]))
    return days


def _forecast_rows(place: ForecastPlace, now: datetime) -> list[dict[str, Any]]:
    local_now = now.astimezone(ZoneInfo(place.timezone))
    today = local_now.date()
    requested = {today + timedelta(days=i) for i in range(7)}
    tomorrow_week_end = today + timedelta(days=7)
    rows: dict[date, ForecastDay] = {}
    for row in _forecast_feed(place, "civillight")["dataseries"][:9]:
        if not isinstance(row, dict):
            continue
        stamp, temps, code = row.get("date"), row.get("temp2m"), row.get("weather")
        if type(stamp) is not int or len(str(stamp)) != 8 or not isinstance(temps, dict) or not isinstance(code, str):
            continue
        high, low = temps.get("max"), temps.get("min")
        condition = _CONDITIONS.get(code)
        if not condition or any(type(t) not in (int, float) or not -90 <= t <= 65 for t in (low, high)) or high < low:
            continue
        day = datetime.strptime(str(stamp), "%Y%m%d").date()
        if today <= day <= tomorrow_week_end:
            rows[day] = ForecastDay(day.isoformat(), _temperature(high, place.unit),
                                    _temperature(low, place.unit), condition)
    missing = (requested | {tomorrow_week_end}) - rows.keys()
    if rows and (requested - rows.keys() or (missing and local_now.hour >= 20)):
        try:
            rows.update((date.fromisoformat(day.date), day) for day in _boundary_days(place, missing))
        except _FETCH_ERRORS:
            pass  # A complete daily feed is usable while its companion is down.
    if not (requested.issubset(rows) or {day + timedelta(days=1) for day in requested}.issubset(rows)):
        raise ValueError("Incomplete seven-day forecast.")
    return [asdict(rows[day]) for day in sorted(rows)]


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
            midnight = datetime.combine(now.astimezone(ZoneInfo(place.timezone)).date() + timedelta(days=1),
                                        datetime.min.time(), tzinfo=ZoneInfo(place.timezone))
            row.retry_at = min(utc + _REFRESH, midnight.astimezone(timezone.utc).replace(tzinfo=None))
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
    # Some model runs already start tomorrow evening local time. Use their seven
    # actual dates only when the first date is exactly one day ahead.
    if by_date and min(by_date) == (start + timedelta(days=1)).isoformat():
        start += timedelta(days=1)
    days = [by_date.get((start + timedelta(days=i)).isoformat()) for i in range(7)]
    return tuple(ForecastDay(**day) for day in days) if all(days) else ()
