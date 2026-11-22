from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .security import redact_secrets

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

SUPERVISOR_CORE_URL = "http://supervisor/core"
HA_CONFIG_ID = 1


@dataclass(frozen=True)
class HAConnection:
    base_url: str
    token: str = field(repr=False)
    mode: str = "manual"


def supervisor_connection() -> HAConnection | None:
    if os.environ.get("INKDADDY_HA_MODE") != "supervisor":
        return None
    return HAConnection(SUPERVISOR_CORE_URL, os.environ.get("SUPERVISOR_TOKEN", ""), "supervisor")


def get_connection(db: Session) -> HAConnection | None:
    managed = supervisor_connection()
    if managed is not None:
        return managed if managed.token else None

    from ..models import HomeAssistantConfig
    from .secrets import get_secret

    config = db.get(HomeAssistantConfig, HA_CONFIG_ID)
    if not config or not config.enabled:
        return None
    token = get_secret(config.token_secret_ref)
    return HAConnection(config.base_url, token) if token else None


@dataclass(frozen=True)
class HAEntity:
    entity_id: str
    domain: str
    name: str | None
    state: str | None
    attributes: dict[str, Any]


def home_assistant_origin(base_url: str) -> tuple[str, str, int]:
    invalid_url = "Enter a valid HTTP or HTTPS Home Assistant URL."
    if any(character in base_url for character in "\\?#") or any(
        ord(character) <= 32 or ord(character) == 127 for character in base_url
    ):
        raise ValueError(invalid_url)
    try:
        endpoint = urlsplit(base_url)
        hostname = endpoint.hostname
        port = endpoint.port
    except ValueError:
        raise ValueError(invalid_url) from None
    if (
        endpoint.scheme not in {"http", "https"}
        or not hostname
        or endpoint.username is not None
        or endpoint.password is not None
        or "%" in endpoint.netloc
        or port == 0
    ):
        raise ValueError(invalid_url)

    # Require an unambiguous authority before comparing it with the origin
    # to which an existing credential is bound.
    host = f"[{hostname}]" if ":" in hostname else hostname
    if endpoint.netloc.lower() != (host if port is None else f"{host}:{port}"):
        raise ValueError(invalid_url)
    return endpoint.scheme, hostname, port if port is not None else (443 if endpoint.scheme == "https" else 80)


def profile_token_url(base_url: str) -> str | None:
    try:
        home_assistant_origin(base_url)
    except ValueError:
        return None
    return base_url.rstrip("/") + "/profile"


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_api(base_url: str, token: str, path: str, timeout: float):
    home_assistant_origin(base_url)
    request = urllib.request.Request(base_url.rstrip("/") + path, headers=auth_headers(token), method="GET")
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=timeout)


_site_cache: tuple[HAConnection, float, dict[str, str | None]] | None = None


def fetch_site(connection: HAConnection) -> dict[str, str | None]:
    global _site_cache
    cached = _site_cache
    if cached is not None and cached[0] == connection and cached[1] > time.monotonic():
        return cached[2].copy()
    with _open_api(connection.base_url, connection.token, "/api/config", 4.0) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("Home Assistant /api/config response must be an object.")
    site = {key: value if isinstance(value := payload.get(key), str) else None for key in ("location_name", "time_zone")}
    _site_cache = (connection, time.monotonic() + 300, site)
    return site.copy()


def normalize_state_response(payload: list[dict[str, Any]]) -> list[HAEntity]:
    entities: list[HAEntity] = []
    for item in payload:
        entity_id = str(item.get("entity_id", ""))
        if "." not in entity_id:
            continue
        attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        name = attributes.get("friendly_name") or entity_id
        entities.append(
            HAEntity(
                entity_id=entity_id,
                domain=entity_id.split(".", 1)[0],
                name=str(name) if name else None,
                state=str(item.get("state")) if item.get("state") is not None else None,
                attributes=attributes,
            )
        )
    return entities


def validate_home_assistant_token(base_url: str, token: str, timeout: float = 8.0) -> dict[str, Any]:
    try:
        with _open_api(base_url, token, "/api/", timeout) as response:
            body = response.read(200).decode("utf-8", errors="replace")
            return {"ok": 200 <= response.status < 300, "status": response.status, "body": body}
    except urllib.error.HTTPError as exc:
        with exc:
            return {"ok": False, "status": exc.code, "body": exc.read(200).decode("utf-8", errors="replace")}
    except urllib.error.URLError as exc:
        return {"ok": False, "status": None, "body": str(exc.reason)}
    except ValueError:
        return {"ok": False, "status": None, "body": "The Home Assistant URL or credentials are invalid."}


def fetch_states(base_url: str, token: str, timeout: float = 12.0) -> list[HAEntity]:
    with _open_api(base_url, token, "/api/states", timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Home Assistant /api/states response must be a list.")
    return normalize_state_response(payload)


def safe_ha_log_payload(payload: Any) -> Any:
    return redact_secrets(payload)
