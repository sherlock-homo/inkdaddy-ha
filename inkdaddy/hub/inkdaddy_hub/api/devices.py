from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    Device,
    DeviceStatusHistory,
    Frame,
    MatterFrameBinding,
    MatterPlaybackIntent,
    MatterFrameTransfer,
    Playback,
    PlaybackOffer,
    PlaybackSlot,
)
from ..services.frame_pipeline import prepare_device_frame, render_device_image
from ..services.matter_delivery import queue_playback_mirror
from ..services.playback import (
    ACTION_NAMES,
    acknowledge_frame,
    apply_playback_action,
    begin_playback_write,
    offer_slot,
    playback_frame,
    playback_status,
    preview_image,
    read_frame_bytes,
    refresh_playback_slots,
    reset_selection,
    update_playlist,
)
from ..services.rendering import render_preview_png_bytes

router = APIRouter(prefix="/api/devices", tags=["devices"])


def _device_payload(device: Device) -> dict[str, object]:
    warning = device.refresh_interval_minutes < 15
    return {
        "id": str(device.id),
        "device_uid": device.device_uid,
        "name": device.name,
        "hardware_target": device.hardware_target,
        "firmware_version": device.firmware_version,
        "hardware_version": device.hardware_version,
        "provisioned": device.provisioned,
        "mesh_connected": device.mesh_connected,
        "refresh_interval_minutes": device.refresh_interval_minutes,
        "refresh_interval_warning": warning,
        "selected_source_type": device.selected_source_type,
        "selected_source_id": device.selected_source_id,
        "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
        "last_refresh_result": device.last_refresh_result,
        "last_frame_id": device.last_frame_id,
        "battery_percent": device.battery_percent,
        "battery_voltage": device.battery_voltage,
        "created_at": device.created_at.isoformat() if device.created_at else None,
        "updated_at": device.updated_at.isoformat() if device.updated_at else None,
    }


def _get_device(db: Session, device_id: str) -> Device:
    device = db.scalar(select(Device).where(Device.device_uid == device_id))
    if not device and device_id.isdigit():
        device = db.get(Device, int(device_id))
    if not device:
        raise HTTPException(status_code=404, detail="Device not found.")
    return device


def create_or_update_device(db: Session, payload: dict[str, object]) -> Device:
    device_uid = str(payload.get("device_uid") or payload.get("id") or f"display-{uuid_suffix(db)}")
    device = db.scalar(select(Device).where(Device.device_uid == device_uid))
    if not device:
        device = Device(
            device_uid=device_uid,
            name=str(payload.get("name") or "inkDaddy Display"),
            hardware_target=str(payload.get("hardware_target") or "xiao_mg24"),
            selected_source_type=str(payload.get("selected_source_type") or "dashboard"),
            selected_source_id=str(payload.get("selected_source_id") or "sample"),
        )
        db.add(device)
    _apply_device_payload(device, payload)
    db.commit()
    db.refresh(device)
    return device


def uuid_suffix(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(Device)) or 0) + 1


def _apply_device_payload(device: Device, payload: dict[str, object]) -> None:
    string_fields = ("name", "hardware_target", "firmware_version", "hardware_version", "selected_source_type", "selected_source_id")
    bool_fields = ("provisioned", "mesh_connected")
    float_fields = ("battery_percent", "battery_voltage")
    for key in string_fields:
        if key in payload and payload[key] is not None:
            setattr(device, key, str(payload[key]))
    for key in bool_fields:
        if key in payload:
            setattr(device, key, bool(payload[key]))
    for key in float_fields:
        if key in payload and payload[key] is not None:
            setattr(device, key, float(payload[key]))
    if "refresh_interval_minutes" in payload and payload["refresh_interval_minutes"] is not None:
        minutes = int(payload["refresh_interval_minutes"])
        device.refresh_interval_minutes = max(10, min(3600, minutes))


@router.get("")
def list_devices(db: Session = Depends(get_db)) -> dict[str, object]:
    devices = db.scalars(select(Device).order_by(Device.created_at.desc(), Device.id.desc())).all()
    return {"devices": [_device_payload(device) for device in devices]}


@router.post("")
def create_device(payload: dict[str, object], db: Session = Depends(get_db)) -> dict[str, object]:
    return _device_payload(create_or_update_device(db, payload))


@router.get("/{device_id}")
def get_device(device_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    return _device_payload(_get_device(db, device_id))


@router.put("/{device_id}")
def update_device(device_id: str, payload: dict[str, object], db: Session = Depends(get_db)) -> dict[str, object]:
    begin_playback_write(db)
    device = _get_device(db, device_id)
    previous_selection = (device.selected_source_type, device.selected_source_id)
    _apply_device_payload(device, payload)
    if previous_selection != (device.selected_source_type, device.selected_source_id):
        reset_selection(db, device)
    db.commit()
    db.refresh(device)
    return _device_payload(device)


@router.delete("/{device_id}")
def delete_device(device_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    begin_playback_write(db)
    device = _get_device(db, device_id)
    db.execute(delete(DeviceStatusHistory).where(DeviceStatusHistory.device_id == device.id))
    db.execute(delete(MatterPlaybackIntent).where(MatterPlaybackIntent.device_id == device.id))
    db.execute(delete(MatterFrameBinding).where(MatterFrameBinding.device_id == device.id))
    db.execute(delete(MatterFrameTransfer).where(MatterFrameTransfer.device_id == device.id))
    db.execute(delete(PlaybackSlot).where(PlaybackSlot.device_id == device.id))
    db.execute(delete(PlaybackOffer).where(PlaybackOffer.device_id == device.id))
    db.execute(delete(Playback).where(Playback.device_id == device.id))
    db.delete(device)
    db.commit()
    return {"deleted": True}


@router.post("/{device_id}/heartbeat")
def heartbeat(device_id: str, payload: dict[str, object], db: Session = Depends(get_db)) -> dict[str, object]:
    begin_playback_write(db)
    device = _get_device(db, device_id)
    if db.get(MatterFrameBinding, device.id) is not None:
        raise HTTPException(status_code=409, detail="This display reports through Matter.")
    _apply_device_payload(device, payload)
    if "last_frame_id" in payload:
        device.last_frame_id = str(payload["last_frame_id"])
    device.last_seen_at = datetime.utcnow()
    db.add(
        DeviceStatusHistory(
            device_id=device.id,
            battery_percent=device.battery_percent,
            battery_voltage=device.battery_voltage,
            status=str(payload.get("status") or "heartbeat"),
            detail_json="{}",
        )
    )
    db.commit()
    db.refresh(device)
    return {"ok": True, "device": _device_payload(device)}


@router.get("/{device_id}/history")
def status_history(device_id: str, limit: int = 50, db: Session = Depends(get_db)) -> dict[str, object]:
    device = _get_device(db, device_id)
    rows = db.scalars(
        select(DeviceStatusHistory)
        .where(DeviceStatusHistory.device_id == device.id)
        .order_by(DeviceStatusHistory.created_at.desc())
        .limit(max(1, min(250, limit)))
    ).all()
    return {
        "history": [
            {
                "status": row.status,
                "battery_percent": row.battery_percent,
                "battery_voltage": row.battery_voltage,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }


@router.get("/{device_id}/frame-manifest")
def frame_manifest(device_id: str, refresh_count: int = 0, db: Session = Depends(get_db)) -> dict[str, object]:
    begin_playback_write(db)
    device = _get_device(db, device_id)
    if db.get(MatterFrameBinding, device.id) is not None:
        slot = refresh_playback_slots(db, device, upcoming=False)
        record = playback_frame(db, device, slot.frame_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Prepared frame not found.")
        db.commit()
        return _slot_manifest(device, record, slot)
    if device.provisioned and device.mesh_connected:
        slot = refresh_playback_slots(db, device, upcoming=False)
        record = offer_slot(db, slot)
        db.commit()
        return _slot_manifest(device, record, slot)
    prepared = prepare_device_frame(db, device, refresh_count=refresh_count)
    return {**prepared.manifest.as_dict(), "content_kind": prepared.content_kind.value}


def _slot_manifest(device: Device, record: Frame, slot: PlaybackSlot) -> dict[str, object]:
    fields = {
        key: getattr(record, key) for key in (
            "frame_id", "width", "height", "palette", "bits_per_pixel", "packing", "byte_order",
            "size_bytes", "sha256", "content_type"
        )
    }
    return {
        **fields,
        "source_id": f"photo-{slot.source_id}" if slot.source_type == "photo" else slot.source_id,
        "created_at": record.created_at.isoformat(),
        "download_url": f"/api/devices/{device.device_uid}/frame?frame_id={record.frame_id}",
        "content_kind": "normal_frame",
    }


@router.post("/{device_id}/manual-refresh")
def manual_refresh(device_id: str, action: str = "next_content", db: Session = Depends(get_db)) -> dict[str, object]:
    if action not in ACTION_NAMES:
        raise HTTPException(status_code=400, detail="Unsupported manual refresh action.")
    begin_playback_write(db)
    device = _get_device(db, device_id)
    apply_playback_action(db, device, action)
    if action in {"play", "pause"} and db.get(MatterFrameBinding, device.id) is not None:
        queue_playback_mirror(db, device.id, action == "play")
    slot = refresh_playback_slots(db, device)
    record = playback_frame(db, device, slot.frame_id)
    if action not in {"pause", "play"}:
        device.last_refresh_result = "manual_refresh_requested"
    db.add(
        DeviceStatusHistory(
            device_id=device.id,
            status="manual_refresh_requested",
            detail_json=json.dumps({"action": action}),
        )
    )
    db.commit()
    return {
        "ok": True,
        "action": action,
        "device": _device_payload(device),
        "frame": _slot_manifest(device, record, slot) if record else None,
        "playback": playback_status(db, device),
    }


@router.get("/{device_id}/playback")
def get_playback(device_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    return playback_status(db, _get_device(db, device_id))


@router.put("/{device_id}/playback")
def save_playback(device_id: str, payload: dict[str, object], db: Session = Depends(get_db)) -> dict[str, object]:
    begin_playback_write(db)
    device = _get_device(db, device_id)
    try:
        update_playlist(db, device, payload.get("playlist"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    refresh_playback_slots(db, device)
    db.commit()
    return playback_status(db, device)


@router.get("/{device_id}/playback/preview/{frame_id}")
def playback_preview(device_id: str, frame_id: str, db: Session = Depends(get_db)) -> Response:
    frame_record = playback_frame(db, _get_device(db, device_id), frame_id)
    if not frame_record:
        raise HTTPException(status_code=404, detail="Prepared frame not found.")
    try:
        image = preview_image(frame_record)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Prepared frame not found.") from exc
    return Response(content=render_preview_png_bytes(image), media_type="image/png")


@router.get("/{device_id}/frame-preview")
def frame_preview(device_id: str, refresh_count: int = 0, db: Session = Depends(get_db)) -> Response:
    device = _get_device(db, device_id)
    image, _content_kind, _source_id = render_device_image(db, device, refresh_count=refresh_count)
    return Response(content=render_preview_png_bytes(image), media_type="image/png")


@router.get("/{device_id}/frame")
def frame(device_id: str, frame_id: str | None = None, db: Session = Depends(get_db)) -> Response:
    device = _get_device(db, device_id)
    if frame_id:
        frame_record = playback_frame(db, device, frame_id)
    elif device.last_frame_id:
        frame_record = db.scalar(select(Frame).where(Frame.frame_id == device.last_frame_id))
    else:
        frame_record = None
    content = read_frame_bytes(frame_record) if frame_record else None
    if content is None:
        raise HTTPException(status_code=404, detail="Frame file not found.")
    return Response(content=content, media_type="application/octet-stream")


@router.get("/{device_id}/config")
def device_config(device_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    device = _get_device(db, device_id)
    return {
        "device_uid": device.device_uid,
        "refresh_interval_minutes": device.refresh_interval_minutes,
        "selected_source_type": device.selected_source_type,
        "selected_source_id": device.selected_source_id,
        "provisioning_timeout_seconds": 300,
        "startup_join_timeout_seconds": 30,
        "refresh_interval_warning": device.refresh_interval_minutes < 15,
    }


@router.put("/{device_id}/config")
def update_device_config(device_id: str, payload: dict[str, object], db: Session = Depends(get_db)) -> dict[str, object]:
    update_device(device_id, payload, db)
    return device_config(device_id, db)


@router.post("/{device_id}/refresh-result")
def refresh_result(device_id: str, payload: dict[str, object], db: Session = Depends(get_db)) -> dict[str, object]:
    begin_playback_write(db)
    device = _get_device(db, device_id)
    if db.get(MatterFrameBinding, device.id) is not None:
        raise HTTPException(status_code=409, detail="This display reports through Matter.")
    device.last_refresh_result = str(payload.get("result") or "unknown")
    frame_id = str(payload["frame_id"]) if payload.get("frame_id") else None
    acknowledge_frame(db, device, frame_id, device.last_refresh_result)
    db.add(
        DeviceStatusHistory(
            device_id=device.id,
            battery_percent=device.battery_percent,
            battery_voltage=device.battery_voltage,
            status=f"refresh:{device.last_refresh_result}",
            detail_json=json.dumps({"frame_id": payload.get("frame_id")} if payload.get("frame_id") else {}),
        )
    )
    db.commit()
    db.refresh(device)
    return {"ok": True, "device": _device_payload(device)}
