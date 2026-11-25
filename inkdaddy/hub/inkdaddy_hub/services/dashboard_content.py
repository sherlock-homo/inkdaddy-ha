from __future__ import annotations

import json
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, tzinfo
from functools import lru_cache
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Dashboard, Device, HAEntityCache, Photo, Setting
from .dashboard import (
    DEV25_SAMPLE_DASHBOARD,
    PANEL_COLOR_NAMES,
    HISTORICAL_SAMPLE_DASHBOARD,
    SAMPLE_DASHBOARD,
    DashboardValidationError,
    load_dashboard_yaml,
    validate_dashboard_config,
)
from .dashboard_calendar import calendar_id, calendar_scope, upcoming_agenda
from .dashboard_sensors import UNAVAILABLE_STATES, battery_level, dashboard_eligible, entity_attributes, report_is_recent
from .dashboard_forecast import ForecastDay, forecast_days, forecast_place
from .photo_metadata import CaptureDetails, capture_details, photo_age
from .rendering import _font

SITE_SETTING = "home_assistant.site"
# Dashboard slugs only contain letters, digits, underscores and hyphens.
PHOTO_DETAILS_OVERLAY = "@photo-details"
DashboardTimeKind = Literal["scheduled", "manual", "preview"]
DashboardRenderVersion = Literal["current", "dev25", "dev24", "dev23", "dev20", "previous", "legacy"]


@dataclass(frozen=True)
class WidgetText:
    label: str
    value: str
    detail: str = ""


class AgendaText(WidgetText):
    """Text laid out as one event per line."""


@dataclass(frozen=True)
class ForecastText(WidgetText):
    unit: str = "F"
    days: tuple[ForecastDay, ...] = ()


class PhotoCaption(WidgetText):
    """Compact one- or two-line caption on a photo."""


@dataclass(frozen=True)
class PhotoOverlay:
    config: dict[str, Any]
    widgets: list[WidgetText]

    @property
    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {"x": tile["x"], "y": tile["y"], "w": tile["w"],
             "agenda": isinstance(widget, AgendaText), "text": asdict(widget),
             **({"caption": True} if isinstance(widget, PhotoCaption) else {}),
             **({"forecast": True, "accent": tile.get("accent", self.config.get("theme", {}).get("accent", "yellow"))}
                if isinstance(widget, ForecastText) else {}),
             **({"h": 2} if tile.get("h") == 2 else {})}
            for tile, widget in zip(self.config["widgets"], self.widgets, strict=True)
        ]

    @classmethod
    def from_snapshot(cls, snapshot: list[dict[str, Any]]) -> PhotoOverlay:
        widgets: list[WidgetText] = []
        for tile in snapshot:
            if tile.get("forecast"):
                fields = tile["text"]
                days = tuple(ForecastDay(**item) for item in fields["days"])
                widgets.append(ForecastText(**{**fields, "days": days}))
            elif tile.get("caption"):
                widgets.append(PhotoCaption(**tile["text"]))
            elif tile["agenda"]:
                widgets.append(AgendaText(**tile["text"]))
            else:
                widgets.append(WidgetText(**tile["text"]))
        return cls({"widgets": snapshot}, widgets)

    def render(self, image: Image.Image) -> Image.Image:
        overlay = Image.new("RGBA", (800, 480))
        draw = ImageDraw.Draw(overlay)
        for tile, widget in zip(self.config["widgets"], self.widgets, strict=True):
            x = 17 + tile["x"] * 194
            w = tile["w"] * 194 - 10
            if isinstance(widget, PhotoCaption):
                width = min(w, 26 + round(max(draw.textlength(widget.value, font=_font(22)),
                                               draw.textlength(widget.detail, font=_font(18)))))
                y = 397 if widget.detail else 421
                draw.rounded_rectangle((x, y, x + width, 462), radius=9, fill=(255, 255, 255, 238),
                                       outline=(0, 0, 0, 255), width=2)
                _fit_text(draw, widget.value, (x + 13, y + 7), width - 26, 22, "black")
                if widget.detail:
                    _fit_text(draw, widget.detail, (x + 13, y + 35), width - 26, 18, "black")
                continue
            if isinstance(widget, ForecastText):
                from .dashboard_art import render_forecast_tile

                height = 444 if tile.get("h") == 2 else 114
                y = 462 - height if tile["y"] else 18
                accent = tile.get("accent", self.config.get("theme", {}).get("accent", "yellow"))
                overlay.paste(render_forecast_tile((w, height), widget, accent), (x, y))
                continue
            agenda = isinstance(widget, AgendaText)
            lines = _agenda_lines(widget) if agenda else ()
            h = 54 + len(lines) * 29 if agenda else 114
            if tile.get("h") == 2:
                h = 444
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


@lru_cache(maxsize=128)
def editable_dashboard_yaml(source: str, *, restore_starter_date: bool = True) -> str:
    config = load_dashboard_yaml(source)
    # Before palette selection, imported dashboards could contain arbitrary theme metadata.
    # Leave the saved YAML intact and present the supported inks to the live editor.
    if "theme" in config:
        theme = config["theme"]
        cleaned = {k: v for k, v in theme.items() if k not in ("primary", "accent")
                   or isinstance(v, str) and v in PANEL_COLOR_NAMES} if isinstance(theme, dict) else {}
        if theme != cleaned:
            config["theme"] = cleaned
            source = json.dumps(config, indent=2, ensure_ascii=False)
    widgets = config.get("widgets")
    if isinstance(widgets, list):
        kept = [item for item in widgets if not (
            isinstance(item, dict) and isinstance(item.get("source"), dict)
            and item["source"].get("type") == "clock"
        )]
        if len(kept) != len(widgets):
            # Only the starter clock has a known replacement. Never fill a
            # deliberately empty tile or introduce a second calendar date.
            if restore_starter_date and not any(
                isinstance(item, dict) and isinstance(item.get("source"), dict)
                and item["source"].get("type") == "date" for item in kept
            ):
                starter_clock = next(w for w in load_dashboard_yaml(HISTORICAL_SAMPLE_DASHBOARD)["widgets"]
                                     if w["source"]["type"] == "clock")
                if starter_clock in widgets:
                    date = next(w for w in load_dashboard_yaml(SAMPLE_DASHBOARD)["widgets"]
                                if w["source"]["type"] == "date")
                    if any(isinstance(item, dict) and item.get("id") == date["id"] for item in kept):
                        date["id"] = starter_clock["id"]
                    kept.append(date)
            return json.dumps({**config, "widgets": kept}, indent=2, ensure_ascii=False)
    return source


def _historical_yaml(record: Dashboard | None) -> str:
    if record is None:
        return HISTORICAL_SAMPLE_DASHBOARD
    return next((old.yaml for old in record.versions if old.version_number == 0), record.current_yaml)


def dashboard_config(record: Dashboard | None, *, historical: bool = False) -> dict[str, Any]:
    source = record.current_yaml if record else SAMPLE_DASHBOARD
    if historical:
        source = _historical_yaml(record)
    return deepcopy(_parsed_dashboard(source, historical))


def dev25_dashboard_configs(record: Dashboard | None) -> Iterator[dict[str, Any]]:
    sources = [record.current_yaml] if record else [DEV25_SAMPLE_DASHBOARD]
    if record:
        sources.extend(v.yaml for v in record.versions if v.version_number == 25)
    for source in sources:
        config = deepcopy(_parsed_dashboard(source, False))
        original = load_dashboard_yaml(source)
        if "theme" in original:
            config["theme"] = original["theme"]
        yield config


def dev24_dashboard_configs(record: Dashboard | None) -> Iterator[dict[str, Any]]:
    # An edit may have archived the original clock layout. Dev24 removed every
    # clock before normalizing widget IDs and calculating the offer hash.
    sources = [_historical_yaml(record)]
    if record and record.current_yaml != sources[0]:
        sources.append(record.current_yaml)
    for source in sources:
        filtered = editable_dashboard_yaml(source, restore_starter_date=False)
        try:
            config = validate_dashboard_config(load_dashboard_yaml(filtered))
        except DashboardValidationError:
            # Earlier releases accepted some source bindings dev24 rejected.
            continue
        yield config


@lru_cache(maxsize=128)
def _parsed_dashboard(source: str, historical: bool) -> dict[str, Any]:
    editable = source if historical else editable_dashboard_yaml(source)
    return validate_dashboard_config(load_dashboard_yaml(editable), historical=historical)


def resolve_photo_overlay(
    db: Session,
    overlay_id: str | None,
    timestamp: datetime,
    device: Device | None,
    *,
    photo: Photo | None = None,
    observed_at: datetime | None = None,
    time_kind: DashboardTimeKind = "scheduled",
) -> PhotoOverlay | None:
    if not overlay_id:
        return None
    if overlay_id == PHOTO_DETAILS_OVERLAY:
        if photo is None:
            return None
        details = capture_details(get_settings().data_dir, photo.original_path, want_date=True, want_location=True)
        if not details.taken and not details.location:
            return None
        if details.taken:
            _, local = _site_context(db, timestamp)
            age = photo_age(details.taken, local.date())
            caption = PhotoCaption("", f"{details.taken:%b %d, %Y} · {age}", details.location)
        else:
            caption = PhotoCaption("", details.location)
        return PhotoOverlay({"widgets": [{"x": 0, "y": 1, "w": 2}]}, [caption])
    record = get_dashboard_record(db, overlay_id)
    if record is None and overlay_id != "sample":
        return None
    config = dashboard_config(record)
    if not config["widgets"]:
        return None
    return PhotoOverlay(config, resolve_widget_text(db, config, timestamp, device, observed_at=observed_at,
                                                    time_kind=time_kind, photo=photo))


def _site_context(db: Session, timestamp: datetime) -> tuple[dict[str, Any], datetime]:
    site_setting = db.get(Setting, SITE_SETTING)
    site = json.loads(site_setting.value_json) if site_setting else {}
    try:
        local = timestamp.astimezone(ZoneInfo(site.get("time_zone") or "UTC"))
    except (ValueError, ZoneInfoNotFoundError):
        local = timestamp.astimezone(ZoneInfo("UTC"))
    return site, local


def resolve_widget_text(
    db: Session,
    config: dict[str, Any],
    timestamp: datetime,
    device: Device | None,
    *,
    photo: Photo | None = None,
    observed_at: datetime | None = None,
    time_kind: DashboardTimeKind = "scheduled",
    render_version: DashboardRenderVersion = "current",
) -> list[WidgetText]:
    site, local = _site_context(db, timestamp)
    observed_at = observed_at or datetime.now(timezone.utc)
    widgets: list[WidgetText] = []
    supports_overlay_widgets = render_version in ("current", "dev25", "dev24", "dev23", "dev20")
    show_agenda = supports_overlay_widgets and any(w["source"].get("type") == "calendar" for w in config["widgets"])
    agenda_scope = calendar_scope(db) if show_agenda else None
    details = CaptureDetails()
    if photo is not None:
        fields = [w["source"].get("field") for w in config["widgets"] if w["source"].get("type") == "photo_metadata"]
        if fields:
            details = capture_details(get_settings().data_dir, photo.original_path,
                                      want_date="date_taken" in fields or "years_ago" in fields,
                                      want_location="location" in fields)
    for widget in config["widgets"]:
        options = widget["options"]
        source = widget["source"]
        source_type = source.get("type")
        label = str(options.get("label") or widget.get("title") or "")
        if source_type == "clock" and render_version not in ("current", "dev25", "dev24"):
            if render_version == "legacy":
                clock_label, detail = "Local time", local.strftime("%A, %B %d")
            elif render_version == "previous":
                clock_label, detail = "Time at refresh", local.strftime("For %a, %b %d")
            elif time_kind == "manual":
                clock_label, detail = "Refresh request", local.strftime("Requested %a, %b %d")
            elif time_kind == "preview":
                clock_label, detail = "Preview time", local.strftime("Preview %a, %b %d")
            elif render_version == "dev20":
                clock_label, detail = "Scheduled time", local.strftime("For %a, %b %d")
            else:
                clock_label, detail = "Scheduled time", local.strftime("Scheduled %a, %b %d")
            widgets.append(WidgetText(label or clock_label, local.strftime("%H:%M"), detail))
        elif source_type == "date" and supports_overlay_widgets:
            widgets.append(WidgetText(label or "Date", local.strftime("%a, %b %d")))
        elif source_type == "forecast" and render_version == "current":
            place = forecast_place(source)
            days = forecast_days(db, place, timestamp, observed_at)
            widgets.append(ForecastText(label or "7-day forecast", place.location, "" if days else "Forecast unavailable",
                                        place.unit, days))
        elif source_type == "calendar" and supports_overlay_widgets:
            rows = upcoming_agenda(
                agenda_scope, calendar_id(source.get("entity_id")), local,
                show_titles=options.get("show_event_titles") is True, limit=4 if widget["h"] > 1 else 3,
            )
            if rows:
                widgets.append(AgendaText(label or "Upcoming events", rows[0], "\n".join(rows[1:])))
            else:
                value = "Calendar unavailable" if rows is None else "No events in the next 24 hours"
                widgets.append(AgendaText(label or "Upcoming events", value))
        elif source_type == "photo_metadata" and render_version in ("current", "dev25", "dev24", "dev23"):
            field = source["field"]
            if field == "location":
                text = WidgetText(label or "Photo location", details.location or "Location unavailable")
            elif field == "years_ago":
                text = WidgetText(label or "Photo age", photo_age(details.taken, local.date()) if details.taken else "Date unavailable")
            else:
                text = WidgetText(label or "Date taken", details.taken.strftime("%b %d, %Y") if details.taken else "Date unavailable")
            widgets.append(text)
        elif source_type == "location":
            entity_id = source.get("entity_id") if render_version in ("current", "dev25", "dev24") else None
            zone = db.get(HAEntityCache, entity_id or "zone.home")
            if entity_id:
                value = (zone.name or zone.entity_id) if zone and zone.domain == "zone" else "Unavailable"
                widgets.append(WidgetText(label or "Location", value))
                continue
            widgets.append(
                WidgetText(
                    label or "Location",
                    site.get("location_name") or (zone and zone.name) or "Home",
                    (local.tzname() or "UTC") if render_version != "current" else "",
                )
            )
        elif source_type == "device_battery":
            entity_id = source.get("entity_id") if render_version in ("current", "dev25", "dev24") else None
            if entity_id:
                battery = db.get(HAEntityCache, entity_id)
                level = battery_level(battery)
                detail = ""
                if level is not None:
                    reported = battery.source_reported_at
                    detail = "Report time unknown"
                    if reported is not None:
                        when = reported.replace(tzinfo=timezone.utc).astimezone(local.tzinfo)
                        if report_is_recent(reported, observed_at):
                            detail = "" if render_version == "current" else "Last reported"
                        else:
                            detail = when.strftime("Reported %b %d, %Y %H:%M")
                widgets.append(WidgetText(label or (battery.name if battery else None) or "Battery",
                                          f"{level}%" if level is not None else "Unavailable", detail))
                continue
            sample = device.battery_percent if device else None
            widgets.append(
                WidgetText(
                    label or "Display battery",
                    f"{sample:.0f}%" if sample is not None else "Unavailable",
                    "Last reported" if sample is not None and supports_overlay_widgets and render_version != "current" else "",
                )
            )
        elif source.get("entity_id") or source_type == "weather":
            widgets.append(_entity_widget_text(db, widget, label, observed_at, local.tzinfo, render_version))
        else:
            widgets.append(WidgetText(label, str(options.get("text", "Unavailable"))))
    return widgets


def _entity_widget_text(
    db: Session, widget: dict[str, Any], label: str, observed_at: datetime,
    display_zone: tzinfo | None, render_version: DashboardRenderVersion,
) -> WidgetText:
    def eligible(entity: HAEntityCache) -> bool:
        if render_version == "legacy":
            return True
        if render_version not in ("current", "dev25") and entity.domain == "weather":
            return report_is_recent(entity.source_reported_at, observed_at)
        return dashboard_eligible(entity, observed_at)

    source = widget["source"]
    entity_id = source.get("entity_id")
    if entity_id:
        entity = db.get(HAEntityCache, str(entity_id))
    else:
        query = select(HAEntityCache).where(
            HAEntityCache.domain == "weather",
            HAEntityCache.state.is_not(None),
            HAEntityCache.state.not_in(UNAVAILABLE_STATES),
        )
        if render_version in ("current", "dev25"):
            query = query.order_by(HAEntityCache.source_reported_at.desc())
        providers = db.scalars(query.order_by(HAEntityCache.entity_id))
        entity = next((provider for provider in providers if eligible(provider)), None)
    if not entity or not entity.state or entity.state in UNAVAILABLE_STATES:
        return WidgetText(label or str(entity_id or "Weather"), "Unavailable")
    if not eligible(entity):
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
        if render_version in ("current", "dev25") and not report_is_recent(entity.source_reported_at, observed_at):
            when = entity.source_reported_at.replace(tzinfo=timezone.utc).astimezone(display_zone)
            detail = " · ".join(filter(None, (detail, when.strftime("Reported %b %d %H:%M"))))
    elif unit:
        value = f"{value} {unit}"
    elif widget["type"] in ("on_off_state", "status_pill"):
        value = value.replace("_", " ").capitalize()
    return WidgetText(label or entity.name or entity.entity_id, value, detail)


def render_dashboard(config: dict[str, Any], widgets: list[WidgetText]) -> Image.Image:
    from .dashboard_art import render_modern_dashboard

    return render_modern_dashboard(config, widgets)


def render_legacy_dashboard(config: dict[str, Any], widgets: list[WidgetText]) -> Image.Image:
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
