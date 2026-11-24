from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class DashboardValidationError(ValueError):
    """Raised when a dashboard config cannot be rendered safely."""


GRID_COLS = 4
GRID_ROWS = 2
MAX_YAML_BYTES = 64 * 1024
MAX_YAML_EVENTS = 4096
MAX_YAML_DEPTH = 32
PHOTO_METADATA_FIELDS = ("date_taken", "years_ago", "location")

WIDGET_FOOTPRINTS: dict[str, set[tuple[int, int]]] = {
    "big_number": {(1, 1), (2, 1)},
    "on_off_state": {(1, 1)},
    "status_pill": {(1, 1)},
    "entity_state": {(1, 1), (2, 1)},
    "line_chart": {(2, 1), (2, 2), (4, 1)},
    "bar_chart": {(2, 1), (2, 2), (4, 1)},
    "donut_chart": {(1, 1), (2, 2)},
    "agenda": {(2, 1), (2, 2), (4, 1), (4, 2)},
    "weather_summary": {(2, 1), (2, 2), (4, 1)},
    "text_note": {(1, 1), (2, 1), (2, 2), (4, 1), (4, 2)},
    "photo_tile": {(1, 1), (2, 1), (2, 2), (4, 2)},
    "vertical_status_stack": {(1, 2)},
    "full_dashboard": {(4, 2)},
}


@dataclass(frozen=True)
class OccupiedTile:
    widget_id: str
    x: int
    y: int


def load_dashboard_yaml(raw_yaml: str) -> dict[str, Any]:
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional dev env
        raise RuntimeError("PyYAML is required to load dashboard YAML.") from exc

    try:
        size = len(raw_yaml.encode("utf-8"))
    except UnicodeError as exc:
        raise DashboardValidationError("Dashboard YAML must be UTF-8 text.") from exc
    if size > MAX_YAML_BYTES:
        raise DashboardValidationError("Dashboard YAML exceeds the 64 KiB limit.")

    depth = 0
    has_aliases = False
    try:
        for count, event in enumerate(yaml.parse(raw_yaml), start=1):
            if isinstance(event, yaml.AliasEvent):
                has_aliases = True
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
            if isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
            if count > MAX_YAML_EVENTS or depth > MAX_YAML_DEPTH:
                raise DashboardValidationError("Dashboard YAML is too complex.")
        if has_aliases:
            nodes = [(yaml.compose(raw_yaml, Loader=yaml.SafeLoader), 0)]
            expanded_count = 0
            while nodes:
                node, depth = nodes.pop()
                expanded_count += 1
                if expanded_count > MAX_YAML_EVENTS or depth > MAX_YAML_DEPTH:
                    raise DashboardValidationError("Dashboard YAML aliases are too complex.")
                if isinstance(node, yaml.MappingNode):
                    nodes.extend((part, depth + 1) for pair in node.value for part in pair)
                elif isinstance(node, yaml.SequenceNode):
                    nodes.extend((part, depth + 1) for part in node.value)
        loaded = yaml.safe_load(raw_yaml)
    except DashboardValidationError:
        raise
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        raise DashboardValidationError("Dashboard YAML could not be parsed.") from exc
    if not isinstance(loaded, dict):
        raise DashboardValidationError("Dashboard YAML must produce a mapping.")
    try:
        json.dumps(loaded, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise DashboardValidationError("Use JSON-compatible values in dashboard YAML.") from exc
    return loaded


def validate_dashboard_config(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise DashboardValidationError("Dashboard config must be a mapping.")

    grid = config.get("grid", {"cols": GRID_COLS, "rows": GRID_ROWS})
    if not isinstance(grid, dict):
        raise DashboardValidationError("grid must be a mapping.")
    cols = _int_field(grid, "cols", GRID_COLS)
    rows = _int_field(grid, "rows", GRID_ROWS)
    if (cols, rows) != (GRID_COLS, GRID_ROWS):
        raise DashboardValidationError("inkDaddy v1 supports only a 4x2 tile grid.")

    widgets = config.get("widgets", [])
    if not isinstance(widgets, list):
        raise DashboardValidationError("widgets must be a list.")

    occupied: dict[tuple[int, int], str] = {}
    normalized_widgets: list[dict[str, Any]] = []
    for index, widget in enumerate(widgets):
        if not isinstance(widget, dict):
            raise DashboardValidationError(f"Widget at index {index} must be a mapping.")
        normalized = validate_widget(widget, index=index, cols=cols, rows=rows)
        for tile in iter_widget_tiles(normalized):
            previous = occupied.get((tile.x, tile.y))
            if previous is not None:
                raise DashboardValidationError(
                    f"Widget {normalized['id']} overlaps {previous} at tile {tile.x},{tile.y}."
                )
            occupied[(tile.x, tile.y)] = normalized["id"]
        normalized_widgets.append(normalized)

    normalized_config = dict(config)
    normalized_config["grid"] = {"cols": cols, "rows": rows}
    normalized_config["widgets"] = normalized_widgets
    normalized_config.setdefault("version", 1)
    normalized_config.setdefault("target", {"width": 800, "height": 480, "palette": "waveshare_7color_6"})
    return normalized_config


def validate_widget(widget: dict[str, Any], *, index: int, cols: int, rows: int) -> dict[str, Any]:
    widget_id = str(widget.get("id") or f"widget_{index + 1}")
    widget_type = str(widget.get("type") or "")
    if widget_type not in WIDGET_FOOTPRINTS:
        raise DashboardValidationError(f"Widget {widget_id} has unsupported type {widget_type!r}.")

    x = _int_field(widget, "x", None)
    y = _int_field(widget, "y", None)
    w = _int_field(widget, "w", None)
    h = _int_field(widget, "h", None)
    if x is None or y is None or w is None or h is None:
        raise DashboardValidationError(f"Widget {widget_id} must include x, y, w, and h.")
    if (w, h) not in WIDGET_FOOTPRINTS[widget_type]:
        allowed = ", ".join(f"{aw}x{ah}" for aw, ah in sorted(WIDGET_FOOTPRINTS[widget_type]))
        raise DashboardValidationError(f"Widget {widget_id} footprint {w}x{h} is invalid; allowed: {allowed}.")
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise DashboardValidationError(f"Widget {widget_id} has invalid tile coordinates.")
    if x + w > cols or y + h > rows:
        raise DashboardValidationError(f"Widget {widget_id} extends outside the {cols}x{rows} grid.")

    normalized = dict(widget)
    normalized.update({"id": widget_id, "type": widget_type, "x": x, "y": y, "w": w, "h": h})
    for field in ("source", "options"):
        normalized.setdefault(field, {})
        if not isinstance(normalized[field], dict):
            raise DashboardValidationError(f"Widget {widget_id} {field} must be a mapping.")
    source = normalized["source"]
    if source.get("type") == "photo_metadata" and source.get("field") not in PHOTO_METADATA_FIELDS:
        raise DashboardValidationError("Choose a photo date, age, or location for the Photo details widget.")
    return normalized


def iter_widget_tiles(widget: dict[str, Any]) -> list[OccupiedTile]:
    return [
        OccupiedTile(widget_id=widget["id"], x=x, y=y)
        for y in range(widget["y"], widget["y"] + widget["h"])
        for x in range(widget["x"], widget["x"] + widget["w"])
    ]


def detect_layout_collisions(widgets: list[dict[str, Any]], cols: int = GRID_COLS, rows: int = GRID_ROWS) -> list[str]:
    errors: list[str] = []
    occupied: dict[tuple[int, int], str] = {}
    for index, widget in enumerate(widgets):
        try:
            normalized = validate_widget(widget, index=index, cols=cols, rows=rows)
        except DashboardValidationError as exc:
            errors.append(str(exc))
            continue
        for tile in iter_widget_tiles(normalized):
            previous = occupied.get((tile.x, tile.y))
            if previous:
                errors.append(f"{normalized['id']} overlaps {previous} at {tile.x},{tile.y}")
            occupied[(tile.x, tile.y)] = normalized["id"]
    return errors


def _int_field(mapping: dict[str, Any], key: str, default: int | None) -> int | None:
    value = mapping.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool):
        raise DashboardValidationError(f"{key} must be an integer.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DashboardValidationError(f"{key} must be an integer.") from exc


SAMPLE_DASHBOARD = """version: 1
name: Home Overview
target:
  width: 800
  height: 480
  palette: waveshare_7color_6
grid:
  cols: 4
  rows: 2
widgets:
  - id: clock
    type: big_number
    x: 0
    y: 0
    w: 2
    h: 1
    source:
      type: clock
    options:
      label: Local time
  - id: weather
    type: weather_summary
    x: 2
    y: 0
    w: 2
    h: 1
    source:
      type: weather
    options:
      label: Weather
  - id: location
    type: entity_state
    x: 0
    y: 1
    w: 2
    h: 1
    source:
      type: location
    options:
      label: Location
  - id: battery
    type: big_number
    x: 2
    y: 1
    w: 2
    h: 1
    options:
      label: Display battery
    source:
      type: device_battery
"""
