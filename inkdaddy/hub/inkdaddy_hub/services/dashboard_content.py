from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Dashboard, Device, HAEntityCache, Setting
from .dashboard import SAMPLE_DASHBOARD, load_dashboard_yaml, validate_dashboard_config
from .dashboard_sensors import UNAVAILABLE_STATES, dashboard_eligible, entity_attributes
from .rendering import _font

SITE_SETTING = "home_assistant.site"


@dataclass(frozen=True)
class WidgetText:
    label: str
    value: str
    detail: str = ""


def get_dashboard_record(db: Session, slug: str | None) -> Dashboard | None:
    return db.scalar(select(Dashboard).where(Dashboard.slug == (slug or "sample")))


def dashboard_config(record: Dashboard | None) -> dict[str, Any]:
    return validate_dashboard_config(
        load_dashboard_yaml(record.current_yaml if record else SAMPLE_DASHBOARD)
    )


def resolve_widget_text(
    db: Session,
    config: dict[str, Any],
    timestamp: datetime,
    device: Device | None,
    *,
    observed_at: datetime | None = None,
    legacy: bool = False,
) -> list[WidgetText]:
    site_setting = db.get(Setting, SITE_SETTING)
    site = json.loads(site_setting.value_json) if site_setting else {}
    try:
        local = timestamp.astimezone(ZoneInfo(site.get("time_zone") or "UTC"))
    except (ValueError, ZoneInfoNotFoundError):
        local = timestamp.astimezone(ZoneInfo("UTC"))
    observed_at = observed_at or datetime.now(timezone.utc)
    widgets: list[WidgetText] = []
    for widget in config["widgets"]:
        options = widget["options"]
        source = widget["source"]
        source_type = source.get("type")
        label = str(options.get("label") or widget.get("title") or "")
        if source_type == "clock":
            detail = local.strftime("%A, %B %d") if legacy else local.strftime("For %a, %b %d")
            widgets.append(
                WidgetText(label or ("Local time" if legacy else "Time at refresh"), local.strftime("%H:%M"), detail)
            )
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
                )
            )
        elif source.get("entity_id") or source_type == "weather":
            widgets.append(_entity_widget_text(db, widget, label, observed_at, legacy=legacy))
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
        _fit_text(draw, widget.value, (x + 14, y + 61), w - 28, 38, "black")
        if widget.detail:
            _fit_text(draw, widget.detail, (x + 15, y + 121), w - 30, 20, "black")
    return image


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
