from __future__ import annotations

import http.client
import json
import os
import re
import socket
from dataclasses import dataclass, field
from typing import Any


class MatterControllerError(RuntimeError):
    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.status = status


def node_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        raise ValueError("Invalid Matter node ID.")
    if int(value) > 0xFFFFFFEFFFFFFFFF:
        raise ValueError("Invalid operational Matter node ID.")
    return value


def event_number(value: object) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,19})", value):
        raise ValueError("Invalid Matter event number.")
    number = int(value)
    if number > 0xFFFFFFFFFFFFFFFF:
        raise ValueError("Invalid Matter event number.")
    return number


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: int):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self.timeout)
            connection.connect(self.path)
        except OSError:
            connection.close()
            raise
        self.sock = connection


@dataclass(frozen=True)
class MatterController:
    port: int | None
    token: str = field(repr=False)
    socket_path: str | None = None

    def request(
        self,
        method: str,
        path: str,
        *,
        timeout: int,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        connection = (
            _UnixHTTPConnection(self.socket_path, timeout) if self.socket_path
            else http.client.HTTPConnection("127.0.0.1", self.port, timeout)
        )
        try:
            connection.request(method, path, body, {
                **(headers or {}),
                "Authorization": f"Bearer {self.token}",
            })
            response = connection.getresponse()
            if response.status >= 400:
                raise MatterControllerError("The Matter controller could not complete the request.", response.status)
            if not 200 <= response.status < 300:
                raise MatterControllerError("The Matter controller returned an unexpected response.")
            data = response.read(32769)
        except (OSError, http.client.HTTPException) as exc:
            raise MatterControllerError("The Matter controller is unavailable.") from exc
        finally:
            connection.close()
        try:
            parsed = json.loads(data) if len(data) <= 32768 else None
        except ValueError as exc:
            raise MatterControllerError("The Matter controller returned an invalid response.") from exc
        if not isinstance(parsed, dict):
            raise MatterControllerError("The Matter controller returned an invalid response.")
        return parsed

    def health(self) -> dict[str, Any]:
        return self.request("GET", "/v1/health", timeout=2)

    def nodes(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        path = "/v1/nodes?refresh=1" if refresh else "/v1/nodes"
        nodes = self.request("GET", path, timeout=18 if refresh else 4).get("nodes")
        if not isinstance(nodes, list) or any(not isinstance(item, dict) for item in nodes):
            raise MatterControllerError("The Matter controller returned an invalid node list.")
        return nodes

    def commission(self, code: str, *, expected_uid: str | None = None) -> dict[str, Any]:
        payload = {"code": code}
        if expected_uid is not None:
            payload["expected_uid"] = expected_uid
        return self.request(
            "POST", "/v1/commission", timeout=125,
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )

    def send(self, target: str, transfer_id: str, payload: bytes, digest: str) -> dict[str, Any]:
        return self.request(
            "POST", self._frame_path(target, transfer_id), timeout=30, body=payload,
            headers={"Content-Type": "application/octet-stream", "X-Frame-SHA256": digest},
        )

    def receipt(self, target: str, transfer_id: str) -> dict[str, Any]:
        return self.request("GET", self._frame_path(target, transfer_id), timeout=24)

    def cached_receipt(self, target: str, transfer_id: str) -> dict[str, Any]:
        path = self._frame_path(target, transfer_id)
        return self.request("GET", f"{path}?cached=1", timeout=3)

    def actions(self, target: str, after: str) -> list[dict[str, Any]]:
        target = node_id(target)
        number = event_number(after)
        reply = self.request("GET", f"/v1/nodes/{target}/actions?after={number}", timeout=3)
        actions = reply.get("actions")
        if not isinstance(actions, list) or len(actions) > 128 or any(not isinstance(item, dict) for item in actions):
            raise MatterControllerError("The Matter controller returned invalid playback events.")
        return actions

    def set_playback(self, target: str, playing: bool) -> None:
        response = self.request(
            "POST", f"/v1/nodes/{node_id(target)}/playback", timeout=28,
            body=json.dumps({"playing": playing}).encode(),
            headers={"Content-Type": "application/json"},
        )
        if response.get("playing") is not playing:
            raise MatterControllerError("The display did not confirm its playback control state.")

    @staticmethod
    def _frame_path(target: str, transfer_id: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{24}", transfer_id):
            raise ValueError("Invalid frame transfer ID.")
        return f"/v1/nodes/{node_id(target)}/frames/{transfer_id}"


def get_controller() -> MatterController | None:
    token = os.getenv("INKDADDY_MATTER_TOKEN")
    if not token:
        return None
    if socket_path := os.getenv("INKDADDY_MATTER_SOCKET"):
        return MatterController(None, token, socket_path)
    return MatterController(int(os.getenv("INKDADDY_MATTER_PORT", "8791")), token)
