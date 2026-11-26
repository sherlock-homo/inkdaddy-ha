"""Resolve a photo point against Photon's OpenStreetMap reverse index."""

from __future__ import annotations

import http.client
import json
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

HOST = "photon.komoot.io"
MAX_RESPONSE = 128 * 1024
VENUE_KEYS = {"amenity", "shop", "tourism", "leisure", "craft", "historic", "office", "aeroway", "railway"}


class ProviderUnavailable(Exception):
    """Contains only the retry delay; provider messages may include coordinates."""

    def __init__(self, retry_seconds: int = 900):
        self.retry_seconds = retry_seconds


def _text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c if c.isprintable() else " " for c in value[:160]).split())[:90]


def _joined(*parts: str) -> str:
    return ", ".join(dict.fromkeys(p for p in parts if p))[:180]


def _distance(lat: float, lon: float, point: object) -> float:
    if not isinstance(point, list) or len(point) != 2:
        return math.inf
    lng, latitude = point
    if any(type(n) not in (float, int) for n in point) or not (-180 <= lng <= 180 and -90 <= latitude <= 90):
        return math.inf
    delta_lon = math.radians(lng - lon)
    delta_lat = math.radians(latitude - lat)
    hav = math.sin(delta_lat / 2) ** 2 + math.cos(math.radians(lat)) * math.cos(math.radians(latitude)) * math.sin(delta_lon / 2) ** 2
    return 12_742_000 * math.asin(min(1, math.sqrt(hav)))


def place_label(data: object, lat: float, lon: float, *, precise: bool) -> str:
    if not isinstance(data, dict) or not isinstance(data.get("features"), list):
        raise ProviderUnavailable()
    nearby = []
    for feature in data["features"][:24]:
        if not isinstance(feature, dict):
            continue
        geom, props = feature.get("geometry"), feature.get("properties")
        if not isinstance(geom, dict) or geom.get("type") != "Point" or not isinstance(props, dict):
            continue
        distance = _distance(lat, lon, geom.get("coordinates"))
        if distance <= 1000:
            nearby.append((distance, props))
    nearby.sort(key=lambda item: item[0])
    localities = []
    venues: dict[str, str] = {}
    addresses: dict[str, str] = {}
    other_landmarks = set()
    for distance, item in nearby:
        name = _text(item.get("name"))
        city = _text(item.get("city") or item.get("town") or item.get("village"))
        if not city and item.get("type") in ("city", "town", "village", "locality"):
            city = name
        area = _joined(city or _text(item.get("county")), _text(item.get("state")))
        localities.append(area or _text(item.get("country")))
        key = _text(item.get("osm_key"))
        if precise and distance <= 30 and name:
            if key in VENUE_KEYS:
                venues.setdefault(name.casefold(), _joined("Near " + name, area))
            elif key in ("building", "man_made", "landuse", "natural"):
                other_landmarks.add(name.casefold())
        street, number = _text(item.get("street")), _text(item.get("housenumber"))
        if precise and distance <= 30 and street and number:
            address = f"{number} {street}"
            addresses.setdefault(address.casefold(), _joined("Near " + address, area))
    if len(venues) == 1 and len(addresses) <= 1 and not other_landmarks.difference(venues):
        return next(iter(venues.values()))
    if len(addresses) == 1:
        return next(iter(addresses.values()))
    return next((area for area in localities if area), "")


def _retry_delay(value: str) -> int:
    if value.isascii() and value.isdigit() and len(value) <= 7:
        return max(900, int(value))
    try:
        deadline = parsedate_to_datetime(value)
        return max(900, math.ceil((deadline - datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return 900


def reverse_place(lat: float, lon: float, *, precise: bool) -> str:
    query = urlencode({"lat": f"{lat:.7f}", "lon": f"{lon:.7f}", "limit": 12, "radius": 1, "lang": "en"})
    connection = http.client.HTTPSConnection(HOST, timeout=6)
    try:
        connection.request("GET", "/reverse?" + query, headers={
            "Accept": "application/json", "User-Agent": "inkDaddy/1.0 (+https://github.com/sherlock-homo/inkdaddy-ha)",
        })
        response = connection.getresponse()
        if response.status != 200:
            retry = response.getheader("Retry-After", "")
            raise ProviderUnavailable(_retry_delay(retry))
        payload = response.read(MAX_RESPONSE + 1)
        if len(payload) > MAX_RESPONSE:
            raise ProviderUnavailable()
        return place_label(json.loads(payload), lat, lon, precise=precise)
    except (OSError, http.client.HTTPException, ValueError, RecursionError) as error:
        raise ProviderUnavailable() from error
    finally:
        connection.close()
