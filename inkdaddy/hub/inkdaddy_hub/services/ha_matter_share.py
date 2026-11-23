from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from websockets.exceptions import WebSocketException
from websockets.sync.client import ClientConnection, connect

from .home_assistant import supervisor_connection

_CORE_WEBSOCKET = "ws://supervisor/core/websocket"
_REGISTRY_ID = re.compile(r"[0-9a-f]{32}")
_DISPLAY_UID = re.compile(r"xiao-mg24-[0-9a-f]{8}")
_NODE_IDENTIFIER = re.compile(r"deviceid_[0-9A-Fa-f]{16}-[0-9A-Fa-f]{16}-MatterNodeDevice")
_QR_CODE = re.compile(r"MT:[0-9A-Z.\-]{10,512}")
_MANUAL_CODE = re.compile(r"(?:[0-9]{11}|[0-9]{21})")
# Debug logging on this connection would expose the API token and temporary pairing code.
_WIRE_LOGGER = logging.Logger(__name__, level=logging.CRITICAL + 1)


class HAMatterShareError(RuntimeError):
    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.status = status


def list_inkdaddy_devices() -> list[dict[str, str]]:
    """Read HA's registered Matter displays without performing device operations."""
    with _authenticated_core() as ws:
        return _list_candidates(ws)


def open_inkdaddy_share(device_id: str) -> tuple[str, str]:
    """Open HA's enhanced commissioning window for an explicitly selected inkDaddy."""
    if not isinstance(device_id, str) or not _REGISTRY_ID.fullmatch(device_id):
        raise HAMatterShareError("Select an inkDaddy display registered in Home Assistant.", 422)
    with _authenticated_core() as ws:
        selected = next(
            (device for device in _list_candidates(ws) if device["device_id"] == device_id), None
        )
        if selected is None:
            raise HAMatterShareError(
                "Home Assistant no longer lists this inkDaddy. Refresh the device list.", 404
            )
        result = _command(ws, 2, "matter/open_commissioning_window", 35, device_id=device_id)
    return _pairing_code(result), selected["device_uid"]


def _list_candidates(ws: ClientConnection) -> list[dict[str, str]]:
    rows = _command(ws, 1, "config/device_registry/list", 10)
    if not isinstance(rows, list):
        raise HAMatterShareError("Home Assistant returned an invalid device list.", 502)
    candidates: list[dict[str, str]] = []
    for row in rows:
        if candidate := _candidate(row):
            candidates.append(candidate)
    serial_counts = Counter(candidate["device_uid"] for candidate in candidates)
    return sorted(
        (candidate for candidate in candidates if serial_counts[candidate["device_uid"]] == 1),
        key=lambda candidate: (candidate["name"].casefold(), candidate["device_uid"]),
    )


def _candidate(row: object) -> dict[str, str] | None:
    if not isinstance(row, dict) or row.get("via_device_id") or row.get("disabled_by"):
        return None
    model, device_id = row.get("model"), row.get("id")
    if not isinstance(model, str) or model.strip().casefold() != "inkdaddy":
        return None
    if not isinstance(device_id, str) or not _REGISTRY_ID.fullmatch(device_id):
        return None
    identifiers = row.get("identifiers")
    if not isinstance(identifiers, list):
        return None
    matter_ids = {
        item[1]
        for item in identifiers
        if isinstance(item, list)
        and len(item) == 2
        and item[0] == "matter"
        and isinstance(item[1], str)
    }
    roots = {item for item in matter_ids if _NODE_IDENTIFIER.fullmatch(item)}
    serials = {
        serial
        for item in matter_ids
        if item.startswith("serial_")
        and _DISPLAY_UID.fullmatch(serial := item.removeprefix("serial_"))
    }
    if len(roots) != 1 or len(serials) != 1:
        return None
    serial = next(iter(serials))
    reported_serial = row.get("serial_number")
    if reported_serial is not None and reported_serial != serial:
        return None
    name = row.get("name_by_user") or row.get("name") or "inkDaddy"
    candidate = {
        "device_id": device_id,
        "name": name[:128] if isinstance(name, str) else "inkDaddy",
        "device_uid": serial,
    }
    version = row.get("sw_version")
    if isinstance(version, str) and version.strip():
        candidate["firmware_version"] = version.strip()[:128]
    return candidate


@contextmanager
def _authenticated_core() -> Iterator[ClientConnection]:
    connection = supervisor_connection()
    if connection is None or not connection.token:
        raise HAMatterShareError("Home Assistant sharing is available through the inkDaddy app.")
    try:
        with connect(
            _CORE_WEBSOCKET,
            open_timeout=5,
            close_timeout=2,
            max_size=8 * 1024 * 1024,
            proxy=None,
            logger=_WIRE_LOGGER,
        ) as ws:
            if _receive(ws, 5).get("type") != "auth_required":
                raise HAMatterShareError(
                    "Home Assistant did not start WebSocket authentication.", 502
                )
            ws.send(json.dumps({"type": "auth", "access_token": connection.token}))
            if _receive(ws, 5).get("type") != "auth_ok":
                raise HAMatterShareError("Home Assistant declined the app's authorization.", 403)
            yield ws
    except (OSError, WebSocketException):
        raise HAMatterShareError("Home Assistant sharing is unavailable or timed out.") from None


def _receive(ws: ClientConnection, timeout: int) -> dict[str, Any]:
    try:
        result = json.loads(ws.recv(timeout=timeout))
    except ValueError:
        raise HAMatterShareError("Home Assistant returned an invalid response.", 502) from None
    if not isinstance(result, dict):
        raise HAMatterShareError("Home Assistant returned an invalid response.", 502)
    return result


def _command(
    ws: ClientConnection, message_id: int, command: str, timeout: int, **fields: str
) -> object:
    ws.send(json.dumps({"id": message_id, "type": command, **fields}))
    response = _receive(ws, timeout)
    if response.get("type") != "result" or response.get("id") != message_id:
        raise HAMatterShareError("Home Assistant returned an unexpected response.", 502)
    if response.get("success") is not True:
        if command == "matter/open_commissioning_window":
            raise HAMatterShareError(
                "Home Assistant could not share this display. Confirm it is paired and awake.", 409
            )
        raise HAMatterShareError("Home Assistant could not list its registered devices.", 502)
    return response.get("result")


def _pairing_code(result: object) -> str:
    if isinstance(result, dict):
        qr = result.get("setup_qr_code")
        if isinstance(qr, str) and _QR_CODE.fullmatch(qr):
            return qr
        manual = result.get("setup_manual_code")
        if isinstance(manual, str) and _MANUAL_CODE.fullmatch(manual):
            return manual
    raise HAMatterShareError("Home Assistant did not return a valid sharing code.", 502)
