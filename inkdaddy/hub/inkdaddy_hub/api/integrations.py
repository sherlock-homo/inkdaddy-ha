from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Device, HAEntityCache, HomeAssistantConfig
from ..services.home_assistant import HA_CONFIG_ID, supervisor_connection
from ..services.secrets import get_secret

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


@router.get("")
def list_integrations(db: Session = Depends(get_db)) -> dict[str, object]:
    ha_config = db.get(HomeAssistantConfig, HA_CONFIG_ID)
    managed = supervisor_connection()
    ha_entities = db.scalar(select(func.count()).select_from(HAEntityCache)) or 0
    display_count = db.scalar(select(func.count()).select_from(Device)) or 0
    ha_configured = bool(managed.token) if managed is not None else bool(ha_config and get_secret(ha_config.token_secret_ref))
    ha_enabled = ha_configured if managed is not None else bool(ha_config and ha_config.enabled)
    return {
        "integrations": [
            {
                "id": "home_assistant",
                "name": "Home Assistant",
                "status": "connected" if ha_configured and ha_enabled else "not_configured",
                "configured": ha_configured,
                "enabled": ha_enabled,
                "entity_count": ha_entities,
                "manage_url": "#home-assistant",
            },
            {
                "id": "inkdaddy_devices",
                "name": "inkDaddy Displays",
                "status": "ready" if display_count else "no_devices",
                "configured": bool(display_count),
                "enabled": True,
                "device_count": display_count,
                "manage_url": "#devices",
            },
            {
                "id": "thread_border_router",
                "name": "Thread Border Router",
                "status": "managed_by_home_assistant" if managed is not None else "diagnostics_required",
                "configured": None if managed is not None else False,
                "enabled": None if managed is not None else False,
                "manage_url": "/config/thread" if managed is not None else "#diagnostics",
            },
        ]
    }
