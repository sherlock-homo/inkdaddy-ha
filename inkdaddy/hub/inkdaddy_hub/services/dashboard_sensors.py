from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import HAEntityCache

DASHBOARD_DOMAINS = ("sensor", "weather")
DASHBOARD_MAX_AGE = timedelta(hours=1)
WEATHER_MAX_AGE = timedelta(hours=3)
UNAVAILABLE_STATES = ("unavailable", "unknown")
_MEASUREMENT_CLASSES = {
    "aqi", "atmospheric_pressure", "battery", "carbon_dioxide", "carbon_monoxide",
    "current", "energy", "humidity", "illuminance", "moisture", "pm1", "pm10", "pm25",
    "power", "pressure", "temperature", "voltage", "volatile_organic_compounds",
    "volatile_organic_compounds_parts", "water", "wind_speed",
}
_UNCLASSIFIED_UNITS = {"°C", "°F", "K", "W", "kW", "Wh", "kWh", "lx", "hPa", "mbar", "ppm"}


def entity_attributes(entity: HAEntityCache) -> dict[str, Any]:
    try:
        attributes = json.loads(entity.attributes_json)
    except (TypeError, ValueError):
        return {}
    return attributes if isinstance(attributes, dict) else {}


def report_is_recent(report: datetime | None, at: datetime, max_age: timedelta = DASHBOARD_MAX_AGE) -> bool:
    if report is None:
        return False
    now = at.astimezone(timezone.utc).replace(tzinfo=None) if at.tzinfo else at
    return now - max_age <= report <= now + timedelta(minutes=1)


def dashboard_report_lifetime(domain: str) -> timedelta:
    return WEATHER_MAX_AGE if domain == "weather" else DASHBOARD_MAX_AGE


def dashboard_eligible(entity: HAEntityCache, at: datetime) -> bool:
    state = entity.state
    if not state or state in UNAVAILABLE_STATES:
        return False
    if not report_is_recent(entity.source_reported_at, at, dashboard_report_lifetime(entity.domain)):
        return False
    if entity.domain == "weather":
        return True
    if entity.domain != "sensor":
        return False
    try:
        if not math.isfinite(float(state)):
            return False
    except (ValueError, OverflowError):
        return False
    attributes = entity_attributes(entity)
    device_class = attributes.get("device_class")
    if isinstance(device_class, str) and device_class:
        return device_class in _MEASUREMENT_CLASSES
    unit = attributes.get("unit_of_measurement")
    return isinstance(unit, str) and unit in _UNCLASSIFIED_UNITS


def battery_source(entity: HAEntityCache) -> bool:
    attrs = entity_attributes(entity)
    return entity.domain == "sensor" and attrs.get("device_class") == "battery" and attrs.get("unit_of_measurement") == "%"


def battery_level(entity: HAEntityCache | None) -> str | None:
    if not entity or not battery_source(entity) or not entity.state:
        return None
    try:
        if 0 <= float(entity.state) <= 100:
            return entity.state
    except (ValueError, OverflowError):
        pass
    return None
