from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import HAEntityCache

DASHBOARD_DOMAINS = ("sensor", "weather")
DASHBOARD_MAX_AGE = timedelta(hours=1)
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


def report_is_recent(report: datetime | None, at: datetime) -> bool:
    if report is None:
        return False
    now = at.astimezone(timezone.utc).replace(tzinfo=None) if at.tzinfo else at
    return now - DASHBOARD_MAX_AGE <= report <= now + timedelta(minutes=1)


def dashboard_eligible(entity: HAEntityCache, at: datetime) -> bool:
    state = entity.state
    if not state or state in UNAVAILABLE_STATES or not report_is_recent(entity.source_reported_at, at):
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
