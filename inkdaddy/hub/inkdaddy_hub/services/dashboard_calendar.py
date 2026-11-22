from __future__ import annotations

import json
import re
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from threading import Lock
from time import monotonic
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode

from sqlalchemy.orm import Session

from ..models import HomeAssistantConfig
from .home_assistant import HA_CONFIG_ID, MANUAL_CONFIG_LOCK, HAConnection, _open_api, get_connection, supervisor_connection

_CALENDAR_ID = re.compile(r"calendar\.[a-z0-9_]{1,240}\Z")
_CACHE_SECONDS = 45 * 60
_REFRESH_SECONDS = 25 * 60
_RETRY_SECONDS = 5 * 60
_CACHE_LIMIT = 24
_RESPONSE_LIMIT = 512 * 1024
_AGENDA_DURATION = timedelta(hours=24)
_CACHE_HORIZON = timedelta(hours=31)
_cache_lock = Lock()


@dataclass(frozen=True)
class CalendarEvent:
    start: date | datetime
    end: date | datetime
    title: str = field(repr=False)


_cache: OrderedDict[tuple[str, str, bool], tuple[float, tuple[CalendarEvent, ...]]] = OrderedDict()
_next_fetch: dict[tuple[str, str, bool], float] = {}


def calendar_id(value: object) -> str | None:
    return value if isinstance(value, str) and _CALENDAR_ID.fullmatch(value) else None


def calendar_requests(config: dict[str, Any]) -> set[tuple[str, bool]]:
    requests: set[tuple[str, bool]] = set()
    for widget in config["widgets"]:
        source = widget["source"]
        if source.get("type") == "calendar" and (entity_id := calendar_id(source.get("entity_id"))):
            requests.add((entity_id, widget["options"].get("show_event_titles") is True))
    return requests


def calendar_scope(db: Session) -> str | None:
    managed = supervisor_connection()
    if managed is not None:
        return "supervisor:" + sha256(managed.token.encode()).hexdigest() if managed.token else None
    config = db.get(HomeAssistantConfig, HA_CONFIG_ID, populate_existing=True)
    if config and config.enabled and config.token_secret_ref:
        return f"manual:{config.base_url}:{config.token_secret_ref}"
    return None


def calendar_connection(db: Session) -> tuple[HAConnection | None, str | None]:
    # Capture one settings generation before doing network I/O. The renderer
    # compares its current generation without opening the manual secret store.
    with MANUAL_CONFIG_LOCK:
        return get_connection(db), calendar_scope(db)


def _read_calendar(connection: HAConnection, path: str) -> list[Any]:
    with _open_api(connection.base_url, connection.token, path, 4) as response:
        content = response.read(_RESPONSE_LIMIT + 1)
    if len(content) > _RESPONSE_LIMIT:
        raise ValueError("Calendar response exceeded the size limit.")
    result = json.loads(content)
    if not isinstance(result, list):
        raise ValueError("Calendar response must be a list.")
    return result


def fetch_calendar_choices(connection: HAConnection) -> list[dict[str, str]]:
    choices = []
    for item in _read_calendar(connection, "/api/calendars"):
        if not isinstance(item, dict) or not (entity_id := calendar_id(item.get("entity_id"))):
            continue
        name = item.get("name")
        choices.append({"entity_id": entity_id, "name": name[:120] if isinstance(name, str) and name else entity_id})
        if len(choices) == 250:
            break
    return choices


def _read_event_time(value: object) -> date | datetime:
    if not isinstance(value, dict):
        raise ValueError("Invalid calendar event time.")
    if isinstance(whole_day := value.get("date"), str) and len(whole_day) == 10:
        return date.fromisoformat(whole_day)
    if isinstance(instant := value.get("dateTime"), str):
        timestamp = datetime.fromisoformat(instant)
        if timestamp.tzinfo is not None:
            return timestamp
    raise ValueError("Invalid calendar event time.")


def _event(value: object, *, show_titles: bool) -> CalendarEvent | None:
    if not isinstance(value, dict):
        return None
    try:
        start = _read_event_time(value.get("start"))
        end = _read_event_time(value.get("end"))
        if type(start) is not type(end) or start >= end:
            return None
    except (OverflowError, ValueError):
        return None
    title = value.get("summary") if show_titles else None
    cleaned = " ".join(title[:256].split())[:100] if isinstance(title, str) else ""
    return CalendarEvent(start, end, cleaned or "Busy")


def _entries(
    connection: HAConnection, entity_id: str, now: datetime, *, show_titles: bool
) -> tuple[CalendarEvent, ...]:
    query = urlencode({"start": now.isoformat(), "end": (now + _CACHE_HORIZON).isoformat()})
    path = f"/api/calendars/{quote(entity_id, safe='')}?{query}"
    return tuple(
        event for item in _read_calendar(connection, path)[:250]
        if (event := _event(item, show_titles=show_titles)) is not None
    )


def refresh_calendar_cache(
    connection: HAConnection | None, scope: str | None, requests: set[tuple[str, bool]], now: datetime
) -> None:
    # Background refresh and explicit previews share a short-lived memory cache.
    # Never forward or persist raw calendar payloads.
    tick = monotonic()
    with _cache_lock:
        for key in [key for key, value in _cache.items() if value[0] <= tick]:
            del _cache[key]
        for key in [key for key, after in _next_fetch.items() if after <= tick]:
            del _next_fetch[key]
        if connection is None or scope is None:
            return
        jobs = [
            (scope, entity_id, show_titles) for entity_id, show_titles in sorted(requests)[:_CACHE_LIMIT]
            if (scope, entity_id, show_titles) not in _next_fetch
        ]
        for key in jobs:
            _next_fetch[key] = tick + _RETRY_SECONDS
        while len(_next_fetch) > 2 * _CACHE_LIMIT:
            del _next_fetch[next(iter(_next_fetch))]
    if not jobs:
        return

    def load(key: tuple[str, str, bool]) -> None:
        context, entity_id, show_titles = key
        try:
            entries = _entries(connection, entity_id, now, show_titles=show_titles)
        except HTTPError as exc:
            with exc:
                if exc.code in (400, 401, 403, 404):
                    with _cache_lock:
                        _cache.pop((context, entity_id, False), None)
                        _cache.pop((context, entity_id, True), None)
            return
        except (OSError, ValueError):
            return
        with _cache_lock:
            if key in _next_fetch:
                _next_fetch[key] = monotonic() + _REFRESH_SECONDS
            _cache[key] = (monotonic() + _CACHE_SECONDS, entries)
            _cache.move_to_end(key)
            while len(_cache) > _CACHE_LIMIT:
                _cache.popitem(last=False)

    with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as workers:
        list(workers.map(load, jobs))


def _as_local(value: date | datetime, local: datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(local.tzinfo)
    return datetime.combine(value, time.min, local.tzinfo)


def upcoming_agenda(
    scope: str | None, entity_id: str | None, local: datetime, *, show_titles: bool, limit: int
) -> tuple[str, ...] | None:
    if scope is None or entity_id is None:
        return None
    with _cache_lock:
        stored = _cache.get((scope, entity_id, show_titles))
    if not stored or stored[0] <= monotonic():
        return None
    bound = local + _AGENDA_DURATION
    visible = []
    for event in stored[1]:
        try:
            start = _as_local(event.start, local)
            if start >= bound or _as_local(event.end, local) <= local:
                continue
        except (OverflowError, ValueError):
            continue
        if start.date() > local.date():
            when = f"Tomorrow {start:%H:%M}" if isinstance(event.start, datetime) else "Tomorrow · All day"
        else:
            when = start.strftime("%H:%M") if isinstance(event.start, datetime) else "All day"
        visible.append((start, f"{when} · {event.title}"))
    return tuple(line for _, line in sorted(visible)[:limit])
