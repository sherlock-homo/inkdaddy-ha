from __future__ import annotations

import json
import math
import os
import socket
import threading
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import HubSettings
from ..database import SessionLocal
from ..models import Device, DeviceStatusHistory, Frame, MatterFrameBinding, PlaybackOffer
from .asset_storage import open_asset
from .playback import MAX_RAW_FRAME_BYTES, acknowledge_frame, begin_playback_write, offer_slot, refresh_playback_slots

PROTOCOL = "inkdaddy-frame:v1"
HEADER_SEPARATOR = b"\n"
MAX_REQUEST_BYTES = 1024
MAX_CHUNK_SIZE = 1024
MIN_CHUNK_SIZE = 64


@dataclass(frozen=True)
class FrameTransferRequest:
    request_type: str
    device_uid: str
    frame_id: str | None = None
    last_frame_id: str | None = None
    chunk_index: int = 0
    chunk_size: int | None = None
    refresh_count: int = 0
    result: str | None = None
    sequence: str | None = None


def start_udp_frame_transfer_listener(settings: HubSettings) -> threading.Thread | None:
    if not settings.udp_frame_transfer_enabled:
        return None

    thread = threading.Thread(
        target=_serve_udp_frame_transfer,
        args=(settings,),
        name="inkdaddy-udp-frame-transfer",
        daemon=True,
    )
    thread.start()
    return thread


def parse_frame_transfer_request(payload: bytes) -> FrameTransferRequest:
    if not payload or len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("invalid frame-transfer request length")
    try:
        text = payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("frame-transfer request must be utf-8 text") from exc

    raw = _parse_key_value_payload(text)
    if raw.get("protocol") != PROTOCOL:
        raise ValueError("unsupported frame-transfer protocol")

    request_type = _optional_string(raw.get("type"), max_length=64)
    if not request_type:
        raise ValueError("frame-transfer request missing type")
    device_uid = _optional_string(raw.get("device_uid") or raw.get("uid"), max_length=128)
    if not device_uid:
        raise ValueError("frame-transfer request missing device_uid")

    return FrameTransferRequest(
        request_type=request_type,
        device_uid=device_uid,
        frame_id=_optional_string(raw.get("frame_id"), max_length=128),
        last_frame_id=_optional_string(raw.get("last_frame_id"), max_length=128),
        chunk_index=_optional_int(raw.get("chunk_index"), default=0),
        chunk_size=_optional_int(raw.get("chunk_size"), default=None),
        refresh_count=_optional_int(raw.get("refresh_count"), default=0),
        result=_optional_string(raw.get("result"), max_length=64),
        sequence=_optional_string(raw.get("sequence") or raw.get("seq"), max_length=64),
    )


def handle_frame_transfer_request(
    db: Session, request: FrameTransferRequest, settings: HubSettings, peer_addr: object | None = None
) -> bytes:
    begin_playback_write(db)
    secure = db.scalar(select(MatterFrameBinding.device_id).join(Device).where(
        Device.device_uid == request.device_uid
    ))
    if secure is not None:
        return build_error_response("matter_transport_required", request.sequence)
    if request.request_type == "manifest_request":
        return _handle_manifest_request(db, request, settings, peer_addr)
    if request.request_type == "chunk_request":
        return _handle_chunk_request(db, request, settings)
    if request.request_type == "refresh_result":
        return _handle_refresh_result(db, request)
    return build_error_response("unsupported_request_type", request.sequence)


def build_manifest_response(
    *,
    status: str,
    frame: Frame | None,
    content_kind: str | None,
    source_id: str | None,
    chunk_size: int,
    sequence: str | None,
) -> bytes:
    chunk_count = math.ceil(frame.size_bytes / chunk_size) if frame else 0
    fields: dict[str, object] = {
        "type": "manifest_response",
        "status": status,
        "chunk_size": chunk_size,
        "chunk_count": chunk_count,
    }
    if frame:
        fields.update(
            {
                "frame_id": frame.frame_id,
                "width": frame.width,
                "height": frame.height,
                "palette": frame.palette,
                "bits_per_pixel": frame.bits_per_pixel,
                "packing": frame.packing,
                "byte_order": frame.byte_order,
                "size_bytes": frame.size_bytes,
                "sha256": frame.sha256,
                "content_type": frame.content_type,
                "source_id": source_id or frame.source_id,
            }
        )
    if content_kind:
        fields["content_kind"] = content_kind
    return build_text_response(fields, sequence)


def build_chunk_response(
    *,
    frame_id: str,
    chunk_index: int,
    offset: int,
    payload: bytes,
    sequence: str | None,
) -> bytes:
    header = build_text_response(
        {
            "type": "chunk_response",
            "status": "ok",
            "frame_id": frame_id,
            "chunk_index": chunk_index,
            "offset": offset,
            "payload_bytes": len(payload),
        },
        sequence,
    )
    return header + HEADER_SEPARATOR + payload


def build_text_response(fields: dict[str, object], sequence: str | None = None) -> bytes:
    parts = [PROTOCOL]
    for key, value in fields.items():
        parts.append(f"{key}={_wire_value(value)}")
    if sequence:
        parts.append(f"seq={_wire_value(sequence)}")
    return ";".join(parts).encode("utf-8")


def build_error_response(error: str, sequence: str | None = None) -> bytes:
    return build_text_response({"type": "error", "status": "error", "error": error}, sequence)


def _serve_udp_frame_transfer(settings: HubSettings) -> None:
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, int(settings.udp_ipv6_only))
        sock.bind((settings.udp_frame_transfer_host, settings.udp_frame_transfer_port))
    except OSError:
        return

    while True:
        try:
            payload, addr = sock.recvfrom(MAX_REQUEST_BYTES)
            request = parse_frame_transfer_request(payload)
            with SessionLocal() as db:
                response = handle_frame_transfer_request(db, request, settings, addr)
            sock.sendto(response, addr)
        except Exception:
            continue


def _handle_manifest_request(
    db: Session, request: FrameTransferRequest, settings: HubSettings, peer_addr: object | None
) -> bytes:
    begin_playback_write(db)
    device = _get_or_create_device(db, request.device_uid)
    device.last_seen_at = datetime.utcnow()
    device.mesh_connected = True
    device.provisioned = True
    db.flush()

    chunk_size = _normalized_chunk_size(request.chunk_size, settings.udp_frame_chunk_size)
    _record_manifest_debug(db, device, request, chunk_size, peer_addr)
    slot = refresh_playback_slots(db, device, upcoming=False)
    frame = offer_slot(db, slot)
    changed = request.last_frame_id != frame.frame_id
    source_id = f"photo-{slot.source_id}" if slot.source_type == "photo" else slot.source_id
    db.commit()
    return build_manifest_response(
        status="changed" if changed else "no_change",
        frame=frame,
        content_kind="normal_frame" if changed else None,
        source_id=source_id,
        chunk_size=chunk_size,
        sequence=request.sequence,
    )


def _record_manifest_debug(
    db: Session,
    device: Device,
    request: FrameTransferRequest,
    chunk_size: int,
    peer_addr: object | None,
) -> None:
    peer_host: str | None = None
    peer_port: int | None = None
    if isinstance(peer_addr, tuple) and len(peer_addr) >= 2:
        peer_host = str(peer_addr[0])
        try:
            peer_port = int(peer_addr[1])
        except (TypeError, ValueError):
            peer_port = None
    db.add(
        DeviceStatusHistory(
            device_id=device.id,
            battery_percent=device.battery_percent,
            battery_voltage=device.battery_voltage,
            status="debug:manifest_request",
            detail_json=json.dumps(
                {
                    "peer_host": peer_host,
                    "peer_port": peer_port,
                    "chunk_size": chunk_size,
                    "refresh_count": request.refresh_count,
                    "sequence": request.sequence,
                    "last_frame_id": request.last_frame_id,
                }
            ),
        )
    )


def _handle_chunk_request(db: Session, request: FrameTransferRequest, settings: HubSettings) -> bytes:
    if not request.frame_id:
        return build_error_response("missing_frame_id", request.sequence)
    chunk_size = _normalized_chunk_size(request.chunk_size, settings.udp_frame_chunk_size)
    frame = db.scalar(select(Frame).where(Frame.frame_id == request.frame_id))
    if not frame:
        return build_error_response("frame_not_found", request.sequence)
    if frame.source_type == "playback":
        offered = db.scalar(select(PlaybackOffer.id).join(Device, PlaybackOffer.device_id == Device.id).where(
            PlaybackOffer.frame_id == frame.frame_id, Device.device_uid == request.device_uid
        ).limit(1))
        if offered is None:
            return build_error_response("frame_not_offered", request.sequence)
    elif frame.source_type != "device" or frame.source_id != request.device_uid:
        return build_error_response("frame_not_offered", request.sequence)
    offset = request.chunk_index * chunk_size
    if not 0 <= offset < frame.size_bytes <= MAX_RAW_FRAME_BYTES:
        return build_error_response("chunk_out_of_range", request.sequence)
    try:
        with open_asset(settings.data_dir, "frames", frame.path) as handle:
            if handle is None or os.fstat(handle.fileno()).st_size != frame.size_bytes:
                return build_error_response("frame_file_missing", request.sequence)
            handle.seek(offset)
            chunk = handle.read(min(chunk_size, frame.size_bytes - offset))
    except OSError:
        return build_error_response("frame_file_missing", request.sequence)
    return build_chunk_response(
        frame_id=frame.frame_id,
        chunk_index=request.chunk_index,
        offset=offset,
        payload=chunk,
        sequence=request.sequence,
    )


def _handle_refresh_result(db: Session, request: FrameTransferRequest) -> bytes:
    begin_playback_write(db)
    device = _get_or_create_device(db, request.device_uid)
    result = request.result or "unknown"
    device.last_seen_at = datetime.utcnow()
    device.mesh_connected = True
    device.provisioned = True
    device.last_refresh_result = result
    acknowledge_frame(db, device, request.frame_id, result)
    detail_json = json.dumps({"frame_id": request.frame_id, "sequence": request.sequence})
    db.add(
        DeviceStatusHistory(
            device_id=device.id,
            battery_percent=device.battery_percent,
            battery_voltage=device.battery_voltage,
            status=f"refresh:{result}",
            detail_json=detail_json,
        )
    )
    db.commit()
    return build_text_response(
        {"type": "refresh_result_ack", "status": "ok", "result": result},
        request.sequence,
    )


def _get_or_create_device(db: Session, device_uid: str) -> Device:
    device = db.scalar(select(Device).where(Device.device_uid == device_uid))
    if device:
        return device
    device = Device(
        device_uid=device_uid,
        name="inkDaddy Display",
        hardware_target="xiao_mg24",
        selected_source_type="dashboard",
        selected_source_id="sample",
    )
    db.add(device)
    db.flush()
    return device


def _parse_key_value_payload(text: str) -> dict[str, object]:
    raw: dict[str, object] = {}
    for part in text.split(";"):
        item = part.strip()
        if not item:
            continue
        if item == PROTOCOL:
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


def _optional_int(value: object, *, default: int | None) -> int:
    if value is None or value == "":
        if default is None:
            return 0
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        if default is None:
            return 0
        return default


def _normalized_chunk_size(requested: int | None, fallback: int) -> int:
    chunk_size = requested or fallback
    return max(MIN_CHUNK_SIZE, min(MAX_CHUNK_SIZE, chunk_size))


def _wire_value(value: object) -> str:
    return str(value).replace(";", "_").replace("\n", "_").replace("\r", "_")
