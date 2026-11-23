from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config import get_settings
from ..ha_app import app_update_details
from ..services.home_assistant import supervisor_connection
from ..services.updates import (
    firmware_manifest_response,
    get_firmware_update_info,
    get_hub_update_info,
    hub_update_response,
    start_hub_update,
    update_policy,
)

router = APIRouter(prefix="/api/updates", tags=["updates"])


@router.get("/hub/check")
@router.post("/hub/check")
def hub_update_check() -> dict[str, object]:
    if managed := supervisor_connection():
        return {
            "status": "managed_by_home_assistant",
            "current_version": get_settings().version,
            **app_update_details(managed.token),
        }
    return hub_update_response(get_hub_update_info())


@router.post("/hub/apply")
def hub_update_apply(payload: dict[str, object]) -> dict[str, object]:
    if supervisor_connection():
        raise HTTPException(status_code=409, detail="Install app updates through Home Assistant Apps.")
    info = get_hub_update_info()
    version = str(payload.get("version") or info.latest_version or "latest")
    result = start_hub_update(version=version, update_info=info)
    return {"accepted": result["accepted"], "status": result["status"], "requested_version": version}


@router.get("/hub/auto")
def hub_auto_update_status() -> dict[str, object]:
    settings = get_settings()
    if supervisor_connection():
        return {"status": "managed_by_home_assistant", "configured": False}
    policy = update_policy(settings)
    return {
        "repository": policy.repository,
        "auto_apply": policy.auto_apply,
        "apply_allowed": policy.apply_allowed,
        "check_interval_seconds": policy.check_interval_seconds,
        "configured": bool(policy.repository),
    }


@router.put("/hub/auto")
def hub_auto_update_update(_payload: dict[str, object]) -> dict[str, object]:
    raise HTTPException(status_code=409, detail="Update policy is managed by the installation.")


@router.get("/firmware/manifest")
@router.get("/firmware/check")
@router.post("/firmware/check")
def firmware_check(board: str = "xiao_mg24", current_version: str | None = None) -> dict[str, object]:
    if supervisor_connection():
        return {"status": "managed_by_matter"}
    return firmware_manifest_response(get_firmware_update_info(board=board, current_version=current_version))


@router.post("/firmware/approve")
def firmware_approve(_payload: dict[str, object]) -> dict[str, object]:
    raise HTTPException(status_code=501, detail="Use your Matter controller to install firmware.")
