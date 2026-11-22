from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from ..config import HubSettings
from ..database import SessionLocal
from ..models import Device, DeviceStatusHistory, MatterFrameBinding
from .playback import ACTION_NAMES, acknowledge_frame, apply_playback_action, begin_playback_write


@dataclass(frozen=True)
class ParsedHeartbeat:
    device_uid: str
    name: str | None = None
    hardware_target: str | None = None
    hardware_version: str | None = None
    firmware_version: str | None = None
    battery_percent: float | None = None
    battery_voltage: float | None = None
    mesh_connected: bool = True
    provisioned: bool = True
    status: str = "udp_heartbeat"
    action: str | None = None
    last_frame_id: str | None = None
    sequence: str | None = None


def parse_udp_heartbeat(payload: bytes) -> ParsedHeartbeat:
    text = payload.decode("utf-8", errors="replace").strip()
    if not text:
        raise ValueError("empty heartbeat payload")

    raw: dict[str, object]
    if text.startswith("{"):
        loaded = json.loads(text)
        if not isinstance(loaded, dict):
            raise ValueError("heartbeat JSON must be an object")
        raw = loaded
    else:
        raw = _parse_key_value_payload(text)

    device_uid = str(raw.get("device_uid") or raw.get("uid") or "").strip()
    if not device_uid:
        raise ValueError("heartbeat missing device_uid")

    return ParsedHeartbeat(
        device_uid=device_uid[:128],
        name=_optional_string(raw.get("name"), max_length=255),
        hardware_target=_optional_string(raw.get("hardware_target") or raw.get("target"), max_length=128),
        hardware_version=_optional_string(raw.get("hardware_version") or raw.get("hardware"), max_length=64),
        firmware_version=_optional_string(raw.get("firmware_version") or raw.get("firmware"), max_length=64),
        battery_percent=_optional_float(raw.get("battery_percent", raw.get("battery"))),
        battery_voltage=_optional_float(raw.get("battery_voltage", raw.get("voltage"))),
        mesh_connected=_optional_bool(raw.get("mesh_connected"), default=True),
        provisioned=_optional_bool(raw.get("provisioned"), default=True),
        status=_optional_string(raw.get("status"), max_length=64) or "udp_heartbeat",
        action=_optional_string(raw.get("action"), max_length=64),
        last_frame_id=_optional_string(raw.get("last_frame_id"), max_length=128),
        sequence=_optional_string(raw.get("sequence") or raw.get("seq"), max_length=64),
    )


def start_udp_heartbeat_listener(settings: HubSettings) -> threading.Thread | None:
    if not settings.udp_heartbeat_enabled:
        return None

    thread = threading.Thread(
        target=_serve_udp_heartbeats,
        args=(settings.udp_heartbeat_host, settings.udp_heartbeat_port, settings.udp_ipv6_only),
        name="inkdaddy-udp-heartbeat",
        daemon=True,
    )
    thread.start()
    return thread


def _serve_udp_heartbeats(host: str, port: int, ipv6_only: bool) -> None:
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, int(ipv6_only))
        sock.bind((host, port))
    except OSError:
        return

    while True:
        try:
            payload, _addr = sock.recvfrom(2048)
            heartbeat = parse_udp_heartbeat(payload)
            record_udp_heartbeat(heartbeat)
        except Exception:
            continue


def record_udp_heartbeat(heartbeat: ParsedHeartbeat) -> None:
    with SessionLocal() as db:
        begin_playback_write(db)
        device = db.scalar(select(Device).where(Device.device_uid == heartbeat.device_uid))
        if device is not None and db.get(MatterFrameBinding, device.id) is not None:
            return
        if not device:
            device = Device(
                device_uid=heartbeat.device_uid,
                name=heartbeat.name or "inkDaddy Display",
                hardware_target=heartbeat.hardware_target or "xiao_mg24",
                selected_source_type="dashboard",
                selected_source_id="sample",
            )
            db.add(device)

        if heartbeat.name:
            device.name = heartbeat.name
        if heartbeat.hardware_target:
            device.hardware_target = heartbeat.hardware_target
        if heartbeat.hardware_version:
            device.hardware_version = heartbeat.hardware_version
        if heartbeat.firmware_version:
            device.firmware_version = heartbeat.firmware_version
        if heartbeat.battery_percent is not None:
            device.battery_percent = heartbeat.battery_percent
        if heartbeat.battery_voltage is not None:
            device.battery_voltage = heartbeat.battery_voltage
        if heartbeat.status in {"displayed_hub_frame", "display_refresh_failed"} or heartbeat.status.startswith("refresh:"):
            device.last_refresh_result = heartbeat.status

        device.provisioned = heartbeat.provisioned
        device.mesh_connected = heartbeat.mesh_connected
        device.last_seen_at = datetime.utcnow()
        db.flush()
        acknowledge_frame(db, device, heartbeat.last_frame_id, heartbeat.status)

        details = {}
        if heartbeat.sequence:
            details["sequence"] = heartbeat.sequence
        if heartbeat.action:
            details["action"] = heartbeat.action
        detail_json = json.dumps(details)
        recent_duplicate = heartbeat.sequence and db.scalar(
            select(DeviceStatusHistory.id).where(
                DeviceStatusHistory.device_id == device.id,
                DeviceStatusHistory.created_at > datetime.utcnow() - timedelta(seconds=10),
                DeviceStatusHistory.status == heartbeat.status,
                DeviceStatusHistory.detail_json == detail_json,
            ).limit(1)
        )
        if not recent_duplicate:
            if heartbeat.action in ACTION_NAMES:
                apply_playback_action(db, device, heartbeat.action)
            db.add(
                DeviceStatusHistory(
                    device_id=device.id,
                    battery_percent=device.battery_percent,
                    battery_voltage=device.battery_voltage,
                    status=heartbeat.status,
                    detail_json=detail_json,
                )
            )
        db.commit()


def _parse_key_value_payload(text: str) -> dict[str, object]:
    raw: dict[str, object] = {}
    for part in text.split(";"):
        item = part.strip()
        if not item:
            continue
        if item == "inkdaddy:v1":
            raw["protocol"] = item
            continue
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        raw[key.strip()] = value.strip()
    return raw


def _optional_string(value: object, *, max_length: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:max_length] if text else None


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: object, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
