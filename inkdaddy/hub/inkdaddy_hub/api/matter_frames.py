from __future__ import annotations

import re
import threading
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Device, MatterFrameBinding, Playback
from ..services.home_assistant import supervisor_connection
from ..services.matter_controller import MatterController, MatterControllerError, get_controller, node_id
from ..services.matter_delivery import queue_playback_mirror
from ..services.playback import begin_playback_write

router = APIRouter(prefix="/api/matter-frames", tags=["matter-frames"])

_DISPLAY_UID = re.compile(r"xiao-mg24-[0-9a-f]{8}")
_MATTER_QR = re.compile(r"MT:[0-9A-Z.\-]{10,512}")
_pairing_lock = threading.Lock()


@contextmanager
def _pairing():
    if not _pairing_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A display is already connecting.")
    try:
        yield
    finally:
        _pairing_lock.release()


def sharing_code(value: object) -> str:
    if not isinstance(value, str) or len(value) > 515:
        raise HTTPException(status_code=422, detail="Enter the sharing code from Home Assistant.")
    value = value.strip()
    manual = re.sub(r"[ -]", "", value)
    if _MATTER_QR.fullmatch(value):
        return value
    if re.fullmatch(r"(?:[0-9]{11}|[0-9]{21})", manual):
        return manual
    raise HTTPException(status_code=422, detail="Enter the sharing code from Home Assistant.")


def _controller() -> MatterController:
    controller = get_controller()
    if controller is None:
        raise HTTPException(status_code=503, detail="Matter frame delivery is unavailable.")
    return controller


def _bind(db: Session, matter_node: dict[str, object]) -> dict[str, object]:
    try:
        target = node_id(matter_node.get("node_id"))
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="The Matter controller could not identify the node.") from exc
    uid = matter_node.get("device_uid")
    if not isinstance(uid, str) or not _DISPLAY_UID.fullmatch(uid):
        return {"node_id": target, "device_uid": None, "linked": False}

    begin_playback_write(db)
    device = db.scalar(select(Device).where(Device.device_uid == uid))
    if device is None:
        device = Device(device_uid=uid, name="inkDaddy", selected_source_id="sample")
        db.add(device)
        db.flush()
    existing = db.get(MatterFrameBinding, device.id)
    if existing and existing.node_id != target:
        raise HTTPException(status_code=409, detail="This display is already linked to another Matter node.")
    other = db.scalar(select(MatterFrameBinding).where(MatterFrameBinding.node_id == target))
    if other and other.device_id != device.id:
        raise HTTPException(status_code=409, detail="This Matter node is already linked to another display.")
    if existing is None:
        device.provisioned = True
        db.add(MatterFrameBinding(device_id=device.id, node_id=target))
        playback = db.get(Playback, device.id)
        queue_playback_mirror(db, device.id, not playback.paused if playback else True)
        db.flush()
    db.commit()
    return {"node_id": target, "device_uid": uid, "linked": True}


@router.get("")
def matter_status(refresh: bool = False, db: Session = Depends(get_db)) -> dict[str, object]:
    if refresh and supervisor_connection() is not None:
        raise HTTPException(status_code=409, detail="Use Check connections to contact Matter displays.")
    return _matter_status(db, refresh=refresh)


@router.post("/check")
def check_matter_status(db: Session = Depends(get_db)) -> dict[str, object]:
    return _matter_status(db, refresh=True)


def _matter_status(db: Session, *, refresh: bool) -> dict[str, object]:
    controller = get_controller()
    linked = dict(db.execute(select(MatterFrameBinding.node_id, Device.device_uid).join(Device)).tuples().all())
    if controller is None:
        return {"enabled": False, "available": False, "nodes": []}
    try:
        node_list = controller.nodes(refresh=refresh)
    except MatterControllerError:
        return {
            "enabled": True,
            "available": False,
            "nodes": [{"node_id": target, "device_uid": uid, "linked": True} for target, uid in linked.items()],
        }
    nodes = [
        {"node_id": target, "device_uid": uid, "linked": True}
        for target, uid in linked.items()
    ]
    nodes.extend(
        {"node_id": target, "device_uid": item.get("device_uid"), "linked": False}
        for item in node_list
        if isinstance(target := item.get("node_id"), str) and target not in linked
    )
    return {"enabled": True, "available": True, "nodes": nodes}


@router.post("/pair")
def pair(payload: dict[str, object], db: Session = Depends(get_db)) -> dict[str, object]:
    with _pairing():
        return _commission(db, sharing_code(payload.get("code")))


def _commission(db: Session, code: str, *, expected_uid: str | None = None) -> dict[str, object]:
    try:
        controller = _controller()
        matter_node = controller.commission(code) if expected_uid is None else controller.commission(code, expected_uid=expected_uid)
    except MatterControllerError as exc:
        if expected_uid is not None and exc.status == 409:
            raise HTTPException(status_code=409, detail="The selected display's Matter identity could not be verified.") from exc
        if exc.status in {400, 404, 408, 409, 422, 502}:
            raise HTTPException(
                status_code=exc.status,
                detail="Check that the display is awake and the Home Assistant sharing window is still open.",
            ) from exc
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    returned_uid = matter_node.get("device_uid")
    if expected_uid is not None and returned_uid != expected_uid:
        raise HTTPException(status_code=409, detail="The Matter identity did not match the selected display.")
    return _bind(db, matter_node)


@router.get("/home-assistant")
def discover_ha_displays() -> dict[str, object]:
    managed = supervisor_connection()
    if managed is None or not managed.token:
        return {"available": False, "devices": []}
    from ..services.ha_matter_share import HAMatterShareError, list_inkdaddy_devices

    try:
        return {"available": True, "devices": list_inkdaddy_devices()}
    except HAMatterShareError:
        return {"available": False, "devices": []}


@router.post("/pair-home-assistant")
def pair_ha_display(payload: dict[str, object], db: Session = Depends(get_db)) -> dict[str, object]:
    managed = supervisor_connection()
    if managed is None or not managed.token:
        raise HTTPException(status_code=503, detail="Home Assistant is unavailable.")
    requested = payload.get("device_id")
    if not isinstance(requested, str) or not re.fullmatch(r"[a-f0-9]{32}", requested):
        raise HTTPException(status_code=422, detail="Choose an inkDaddy display paired in Home Assistant.")
    _controller()

    from ..services.ha_matter_share import HAMatterShareError, open_inkdaddy_share

    with _pairing():
        try:
            code, device_uid = open_inkdaddy_share(requested)
        except HAMatterShareError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return _commission(db, sharing_code(code), expected_uid=device_uid)


@router.post("/link")
def link(payload: dict[str, object], db: Session = Depends(get_db)) -> dict[str, object]:
    try:
        requested = node_id(payload.get("node_id"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        nodes = _controller().nodes()
    except MatterControllerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    match = next((item for item in nodes if item.get("node_id") == requested), None)
    if match is None:
        raise HTTPException(status_code=404, detail="The Matter controller has not paired this display.")
    return _bind(db, match)


@router.post("/home-assistant/{device_uid}/playback-buttons")
def setup_ha_playback_buttons(device_uid: str, db: Session = Depends(get_db)) -> dict[str, int]:
    from ..services.ha_matter_share import HAMatterShareError
    from ..services.ha_playback_buttons import install_ha_playback_buttons

    bound = db.scalar(select(MatterFrameBinding.device_id).join(Device).where(Device.device_uid == device_uid))
    if not bound or not _DISPLAY_UID.fullmatch(device_uid):
        raise HTTPException(status_code=404, detail="Select a linked Matter display.")
    try:
        return install_ha_playback_buttons(device_uid)
    except HAMatterShareError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
