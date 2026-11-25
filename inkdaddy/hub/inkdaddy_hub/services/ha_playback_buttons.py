"""Install Home Assistant Template buttons over a bound frame's Matter endpoints."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import count

from .ha_matter_share import HAMatterShareError, _NODE_IDENTIFIER, _authenticated_core, _candidate, _command
from .home_assistant import _NoRedirect, auth_headers, supervisor_connection

_NAMES = {1: "Next", 2: "Previous", 3: "Play/Pause", 4: "Refresh"}
_INSTALL_LOCK = threading.Lock()
_REGISTRY_ID = re.compile(r"[0-9a-f]{32}")
_FLOW_ID = re.compile(r"[A-Za-z0-9-]{16,64}")


def _flow_api(path: str, data: dict | None, *, method: str = "POST") -> dict:
    core = supervisor_connection()
    if not core or not core.token:
        raise HAMatterShareError("Open inkDaddy through Home Assistant to set up playback buttons.")
    request = urllib.request.Request(
        core.base_url + "/api/config/config_entries/" + path,
        data=None if data is None else json.dumps(data).encode(),
        headers=auth_headers(core.token), method=method,
    )
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=8) as response:
            body = response.read(256 * 1024 + 1)
        result = json.loads(body) if len(body) <= 256 * 1024 else None
        if isinstance(result, dict):
            return result
    except (OSError, ValueError) as exc:
        raise HAMatterShareError("Home Assistant could not configure a Template button. Retry setup.") from exc
    raise HAMatterShareError("Home Assistant returned an unexpected button setup response.", 502)


@contextmanager
def _flow(kind: str, handler: str) -> Iterator[tuple[str, dict]]:
    form = _flow_api(kind, {"handler": handler})
    flow_id = form.get("flow_id")
    if not isinstance(flow_id, str) or not _FLOW_ID.fullmatch(flow_id):
        raise HAMatterShareError("Home Assistant did not start the button configuration flow.", 502)
    path = f"{kind}/{flow_id}"
    try:
        yield path, form
    finally:
        # A completed create flow is already closed. Cancel only unfinished flows.
        if form.get("type") != "create_entry":
            try:
                _flow_api(path, None, method="DELETE")
            except HAMatterShareError:
                pass


def _button_action(device_id: str, switch: dict) -> dict:
    # Device actions resolve the registry ID even after an owner renames the entity.
    return {"type": "toggle", "device_id": device_id, "entity_id": switch["id"], "domain": "switch"}


def _create_button(device_id: str, switch: dict, name: str) -> str:
    with _flow("flow", "template") as (path, state):
        if state.get("type") != "menu" or "button" not in state.get("menu_options", []):
            raise HAMatterShareError("This Home Assistant version does not offer Template buttons.", 409)
        fields = _flow_api(path, {"next_step_id": "button"})
        if fields.get("type") != "form" or fields.get("step_id") != "button":
            raise HAMatterShareError("Home Assistant could not open its Template button editor.", 409)
        state.update(_flow_api(path, {"name": name, "device_id": device_id, "press": [_button_action(device_id, switch)]}))
        entry = state.get("result") or {}
        if state.get("type") == "create_entry" and isinstance(entry, dict) and entry.get("domain") == "template" and entry.get("entry_id"):
            return entry["entry_id"]
        raise HAMatterShareError("Home Assistant declined the playback button configuration.", 409)


def _target_switch(entry_id: str, device_id: str) -> str | None:
    """Inspect saved actions using HA's options form; never submit changes to it."""
    with _flow("options/flow", entry_id) as (_, form):
        if form.get("step_id") != "button":
            return None
        values = {}
        for field in form.get("data_schema", []):
            hint = field.get("description") or {}
            values[field["name"]] = hint.get("suggested_value", field.get("default"))
        actions = values.get("press")
        if values.get("device_id") == device_id and isinstance(actions, list) and len(actions) == 1:
            action = actions[0]
            if isinstance(action, dict) and isinstance(target := action.get("entity_id"), str):
                if _REGISTRY_ID.fullmatch(target) and action == _button_action(device_id, {"id": target}):
                    return target
    return None


def _switches(rows: list, device_id: str, root: str) -> dict[int, dict]:
    found = {}
    for row in rows:
        if row.get("device_id") != device_id or row.get("platform") != "matter" or not row["entity_id"].startswith("switch."):
            continue
        match = re.fullmatch(re.escape(root) + r"-([1-4])-MatterPlug-6-0", row.get("unique_id") or "")
        if match:
            endpoint = int(match[1])
            if endpoint in found or not _REGISTRY_ID.fullmatch(row.get("id") or "") or row.get("disabled_by"):
                raise HAMatterShareError("A playback switch is disabled or ambiguous. Check the Matter device in Home Assistant.", 409)
            found[endpoint] = row
    if found.keys() != _NAMES.keys():
        raise HAMatterShareError("This display does not advertise all four supported playback switches.", 409)
    return found


def _helpers(rows: list, device_id: str) -> list[dict]:
    return [row for row in rows if row.get("device_id") == device_id and row.get("platform") == "template"
            and row["entity_id"].startswith("button.") and row.get("config_entry_id")
            and row.get("unique_id") == row["config_entry_id"]]


def install_ha_playback_buttons(uid: str) -> dict[str, int]:
    """Operate on a single verified device and hide switches only after all four helpers load."""
    if not _INSTALL_LOCK.acquire(blocking=False):
        raise HAMatterShareError("Playback button setup is already running. Retry when it finishes.", 409)
    try:
        with _authenticated_core() as ws:
            ids = count(1)

            def registry(name: str) -> list:
                result = _command(ws, next(ids), f"config/{name}_registry/list", 10)
                if not isinstance(result, list):
                    raise HAMatterShareError("Home Assistant could not read its device controls.", 502)
                return result

            matching = [r for r in registry("device") if (candidate := _candidate(r)) and candidate["device_uid"] == uid]
            if len(matching) != 1:
                raise HAMatterShareError("Select a paired inkDaddy display with a unique identity.", 409)
            device = matching[0]
            device_id = device["id"]
            root = next(item[1].removeprefix("deviceid_") for item in device["identifiers"]
                        if isinstance(item, list) and len(item) == 2 and item[0] == "matter"
                        and isinstance(item[1], str) and _NODE_IDENTIFIER.fullmatch(item[1]))
            rows = registry("entity")
            switches = _switches(rows, device_id, root)
            entries = {}
            for helper in _helpers(rows, device_id):
                target = _target_switch(helper["config_entry_id"], device_id)
                if target:
                    entries[target] = helper["config_entry_id"]
            created = 0
            for endpoint, switch in switches.items():
                if switch["id"] not in entries:
                    entries[switch["id"]] = _create_button(device_id, switch, _NAMES[endpoint])
                    created += 1
            expected = {entries[s["id"]] for s in switches.values()}
            for attempt in range(12):
                rows = registry("entity")
                ready = {h["config_entry_id"] for h in _helpers(rows, device_id) if not h.get("disabled_by") and not h.get("hidden_by")}
                if expected <= ready:
                    break
                if attempt == 11:
                    raise HAMatterShareError("Waiting for all four playback buttons to be enabled and visible. Check the helpers and retry.", 409)
                time.sleep(0.25)
            current = _switches(rows, device_id, root)
            if any(row["id"] != switches[ep]["id"] for ep, row in current.items()):
                raise HAMatterShareError("The device controls changed during setup. Retry.", 409)
            for row in current.values():
                if row.get("hidden_by") is None:
                    _command(ws, next(ids), "config/entity_registry/update", 10, entity_id=row["entity_id"], hidden_by="user")
            return {"buttons": len(expected), "created": created}
    finally:
        _INSTALL_LOCK.release()
