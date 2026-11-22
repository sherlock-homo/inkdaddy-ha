from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from ..config import HubSettings, get_settings
from .security import redact_secret_value

_store_lock = threading.Lock()


def _load(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items()}


def _write(path: Path, payload: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    tmp = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        tmp.unlink(missing_ok=True)


def set_secret(ref: str, value: str, settings: HubSettings | None = None) -> str:
    settings = settings or get_settings()
    with _store_lock:
        payload = _load(settings.secrets_path)
        payload[ref] = value
        _write(settings.secrets_path, payload)
    return ref


def get_secret(ref: str | None, settings: HubSettings | None = None) -> str | None:
    if not ref:
        return None
    settings = settings or get_settings()
    return _load(settings.secrets_path).get(ref)


def delete_secret(ref: str | None, settings: HubSettings | None = None) -> None:
    if not ref:
        return
    settings = settings or get_settings()
    with _store_lock:
        payload = _load(settings.secrets_path)
        if ref in payload:
            payload.pop(ref)
            _write(settings.secrets_path, payload)


def retain_secret(namespace: str, active_ref: str, settings: HubSettings | None = None) -> None:
    settings = settings or get_settings()
    with _store_lock:
        payload = _load(settings.secrets_path)
        retained = {
            key: value for key, value in payload.items()
            if key == active_ref or (key != namespace and not key.startswith(f"{namespace}."))
        }
        if retained != payload:
            _write(settings.secrets_path, retained)


def secret_preview(ref: str | None, settings: HubSettings | None = None) -> str | None:
    value = get_secret(ref, settings)
    return redact_secret_value(value)
