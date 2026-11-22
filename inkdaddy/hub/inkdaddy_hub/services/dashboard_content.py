from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Dashboard, Device, HAEntityCache, Setting
from .dashboard import SAMPLE_DASHBOARD, load_dashboard_yaml, validate_dashboard_config
from .dashboard_calendar import calendar_id, calendar_scope, upcoming_agenda
from .dashboard_sensors import UNAVAILABLE_STATES, dashboard_eligible, entity_attributes
from .rendering import _font

SITE_SETTING = "home_assistant.site"
DashboardTimeKind = Literal["scheduled", "manual", "preview"]
DashboardRenderVersion = Literal["current", "previous", "legacy"]


@dataclass(frozen=True)
class WidgetText:
    label: str
    value: str
    detail: str = ""


class AgendaText(WidgetText):
    """Text laid out as one event per line."""


@dataclass(frozen=True)
class PhotoOverlay:
    config: dict[str, Any]
    widgets: list[WidgetText]

    @property
    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {"x": tile["x"], "y": tile["y"], "w": tile["w"],
             "agenda": isinstance(widget, AgendaText), "text": asdict(widget)}
            for tile, widget in zip(self.config["widgets"], self.widgets, strict=True)
        ]

    @classmethod
    def from_snapshot(cls, snapshot: list[dict[str, Any]]) -> PhotoOverlay:
        return cls(
            {"widgets": snapshot},
            [(AgendaText if tile["agenda"] else WidgetText)(**tile["text"]) for tile in snapshot],
        )

    def render(self, image: Image.Image) -> Image.Image:
        overlay = Image.new("RGBA", (800, 480))
        draw = ImageDraw.Draw(overlay)
        for tile, widget in zip(self.config["widgets"], self.widgets, strict=True):
            x = 17 + tile["x"] * 194
            w = tile["w"] * 194 - 10
            agenda = isinstance(widget, AgendaText)
            lines = _agenda_lines(widget) if agenda else ()
            h = 54 + len(lines) * 29 if agenda else 114
            y = 462 - h if tile["y"] else 18
            draw.rounded_rectangle((x, y, x + w, y + h), radius=12, fill=(255, 255, 255, 238),
                                   outline=(0, 0, 0, 255), width=2)
            _fit_text(draw, widget.label, (x + 13, y + 10), w - 26, 17, "black")
            if agenda:
                for i, line in enumerate(lines):
                    _fit_text(draw, line, (x + 13, y + 40 + i * 29), w - 26, 22, "black")
            else:
                _fit_text(draw, widget.value, (x + 13, y + 39), w - 26, 33, "black")
                if widget.detail:
                    _fit_text(draw, widget.detail, (x + 13, y + 83), w - 26, 16, "black")
        return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


def get_dashboard_record(db: Session, slug: str | None) -> Dashboard | None:
    return db.scalar(select(Dashboard).where(Dashboard.slug == (slug or "sample")))


def dashboard_config(record: Dashboard | None) -> dict[str, Any]:
    return deepcopy(_parsed_dashboard(record.current_yaml if record else SAMPLE_DASHBOARD))


@lru_cache(maxsize=128)
def _parsed_dashboard(source: str) -> dict[str, Any]:
    return validate_dashboard_config(load_dashboard_yaml(source))


def resolve_photo_overlay(
    db: Session,
    overlay_id: str | None,
    timestamp: datetime,
    device: Device | None,
    *,
    observed_at: datetime | None = None,
    time_kind: DashboardTimeKind = "scheduled",
) -> PhotoOverlay | None:
    if not overlay_id:
        return None
    record = get_dashboard_record(db, overlay_id)
    if record is None and overlay_id != "sample":
        return None
    config = dashboard_config(record)
    if not config["widgets"]:
        return None
    return PhotoOverlay(config, resolve_widget_text(db, config, timestamp, device, observed_at=observed_at,
                                                    time_kind=time_kind))


def resolve_widget_text(
    db: Session,
    config: dict[str, Any],
    timestamp: datetime,
    device: Device | None,
    *,
    observed_at: datetime | None = None,
    time_kind: DashboardTimeKind = "scheduled",
    render_version: DashboardRenderVersion = "current",
) -> list[WidgetText]:
    site_setting = db.get(Setting, SITE_SETTING)
    site = json.loads(site_setting.value_json) if site_setting else {}
    try:
        local = timestamp.astimezone(ZoneInfo(site.get("time_zone") or "UTC"))
    except (ValueError, ZoneInfoNotFoundError):
        local = timestamp.astimezone(ZoneInfo("UTC"))
    observed_at = observed_at or datetime.now(timezone.utc)
    widgets: list[WidgetText] = []
    show_agenda = render_version == "current" and any(w["source"].get("type") == "calendar" for w in config["widgets"])
    agenda_scope = calendar_scope(db) if show_agenda else None
    for widget in config["widgets"]:
        options = widget["options"]
        source = widget["source"]
        source_type = source.get("type")
        label = str(options.get("label") or widget.get("title") or "")
        if source_type == "clock":
            if render_version == "legacy":
                clock_label, detail = "Local time", local.strftime("%A, %B %d")
            elif render_version == "previous":
                clock_label, detail = "Time at refresh", local.strftime("For %a, %b %d")
            elif time_kind == "manual":
                clock_label, detail = "Refresh request", local.strftime("Requested %a, %b %d")
            elif time_kind == "preview":
                clock_label, detail = "Preview time", local.strftime("Preview %a, %b %d")
            else:
                clock_label, detail = "Scheduled time", local.strftime("For %a, %b %d")
            widgets.append(WidgetText(label or clock_label, local.strftime("%H:%M"), detail))
        elif source_type == "date" and render_version == "current":
            widgets.append(WidgetText(label or "Date", local.strftime("%a, %b %d")))
        elif source_type == "calendar" and render_version == "current":
            rows = upcoming_agenda(
                agenda_scope, calendar_id(source.get("entity_id")), local,
                show_titles=options.get("show_event_titles") is True, limit=4 if widget["h"] > 1 else 3,
            )
            if rows:
                widgets.append(AgendaText(label or "Upcoming events", rows[0], "\n".join(rows[1:])))
            else:
                value = "Calendar unavailable" if rows is None else "No events in the next 24 hours"
                widgets.append(AgendaText(label or "Upcoming events", value))
        elif source_type == "location":
            zone = db.get(HAEntityCache, "zone.home")
            widgets.append(
                WidgetText(
                    label or "Location",
                    site.get("location_name") or (zone and zone.name) or "Home",
                    local.tzname() or "UTC",
                )
            )
        elif source_type == "device_battery":
            sample = device.battery_percent if device else None
            widgets.append(
                WidgetText(
                    label or "Display battery",
                    f"{sample:.0f}%" if sample is not None else "Unavailable",
                    "Last reported" if sample is not None and render_version == "current" else "",
                )
            )
        elif source.get("entity_id") or source_type == "weather":
            widgets.append(_entity_widget_text(db, widget, label, observed_at, legacy=render_version == "legacy"))
        else:
            widgets.append(WidgetText(label, str(options.get("text", "Unavailable"))))
    return widgets


def _entity_widget_text(
    db: Session, widget: dict[str, Any], label: str, observed_at: datetime, *, legacy: bool
) -> WidgetText:
    source = widget["source"]
    entity_id = source.get("entity_id")
    if entity_id:
        entity = db.get(HAEntityCache, str(entity_id))
    else:
        providers = db.scalars(
            select(HAEntityCache)
            .where(
                HAEntityCache.domain == "weather",
                HAEntityCache.state.is_not(None),
                HAEntityCache.state.not_in(UNAVAILABLE_STATES),
            )
            .order_by(HAEntityCache.entity_id)
        )
        entity = next((provider for provider in providers if legacy or dashboard_eligible(provider, observed_at)), None)
    if not entity or not entity.state or entity.state in UNAVAILABLE_STATES:
        return WidgetText(label or str(entity_id or "Weather"), "Unavailable")
    if not legacy and not dashboard_eligible(entity, observed_at):
        return WidgetText(label or entity.name or entity.entity_id, "Unavailable")
    attrs = entity_attributes(entity)
    unit = widget["options"].get("unit") or attrs.get("unit_of_measurement") or ""
    value = entity.state
    detail = ""
    if entity.domain == "weather":
        if value == "partlycloudy":
            value = "partly cloudy"
        value = value.replace("-", " ").replace("_", " ").capitalize()
        if attrs.get("temperature") is not None:
            detail = f"{attrs['temperature']} {attrs.get('temperature_unit', '')}".strip()
    elif unit:
        value = f"{value} {unit}"
    elif widget["type"] in ("on_off_state", "status_pill"):
        value = value.replace("_", " ").capitalize()
    return WidgetText(label or entity.name or entity.entity_id, value, detail)


def render_dashboard(config: dict[str, Any], widgets: list[WidgetText]) -> Image.Image:
    image = Image.new("RGB", (800, 480), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 799, 59), fill=(0, 0, 0))
    _fit_text(draw, str(config.get("name", "Home Overview")), (28, 12), 744, 30, "white")
    for tile, widget in zip(config["widgets"], widgets, strict=True):
        x = 18 + tile["x"] * 194
        y = 77 + tile["y"] * 197
        w = tile["w"] * 194 - 10
        h = tile["h"] * 197 - 10
        draw.rounded_rectangle((x, y, x + w, y + h), radius=12, outline=(0, 0, 0), width=2)
        draw.rectangle((x + 14, y + 17, x + 24, y + 27), fill=(255, 0, 0))
        _fit_text(draw, widget.label, (x + 34, y + 13), w - 49, 17, "black")
        if isinstance(widget, AgendaText):
            for i, line in enumerate(_agenda_lines(widget)):
                _fit_text(draw, line, (x + 14, y + 52 + i * 34), w - 28, 24, "black")
            continue
        _fit_text(draw, widget.value, (x + 14, y + 61), w - 28, 38, "black")
        if widget.detail:
            _fit_text(draw, widget.detail, (x + 15, y + 121), w - 30, 20, "black")
    return image


def _agenda_lines(widget: WidgetText) -> tuple[str, ...]:
    return (widget.value, *widget.detail.splitlines())


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    position: tuple[int, int],
    width: int,
    size: int,
    color: str,
) -> None:
    text = " ".join(text.split())[:256]
    font = _font(size)
    while size > 12 and draw.textlength(text, font=font) > width:
        size -= 2
        font = _font(size)
    if draw.textlength(text, font=font) > width:
        while text and draw.textlength(text + "…", font=font) > width:
            text = text[:-1]
        text += "…"
    draw.text(position, text, font=font, fill=color)
