from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import HAEntityCache, HomeAssistantConfig
from ..services.home_assistant import (
    HA_CONFIG_ID,
    fetch_site,
    fetch_states,
    get_connection,
    home_assistant_origin,
    normalize_state_response,
    profile_token_url,
    supervisor_connection,
    validate_home_assistant_token,
)
from ..services.secrets import get_secret, secret_preview, set_secret

router = APIRouter(prefix="/api/home-assistant", tags=["home-assistant"])

HA_TOKEN_REF = "home_assistant.long_lived_access_token"
DEFAULT_BASE_URL = "http://homeassistant.local:8123"


def _get_config(db: Session) -> HomeAssistantConfig | None:
    return db.get(HomeAssistantConfig, HA_CONFIG_ID)


def _manual_endpoint(
    payload: dict[str, object], config: HomeAssistantConfig | None
) -> tuple[str, tuple[str, str, int]]:
    base_url = str(payload.get("base_url") or (config.base_url if config else DEFAULT_BASE_URL)).strip().rstrip("/")
    try:
        return base_url, home_assistant_origin(base_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _saved_token(config: HomeAssistantConfig | None, origin: tuple[str, str, int]) -> str | None:
    if config is None or not (token := get_secret(config.token_secret_ref)):
        return None
    try:
        same_origin = home_assistant_origin(config.base_url) == origin
    except ValueError:
        same_origin = False
    if not same_origin:
        raise HTTPException(status_code=422, detail="Enter a new access token when changing the Home Assistant origin.")
    return token


def _config_payload(db: Session, config: HomeAssistantConfig | None) -> dict[str, object]:
    managed = supervisor_connection()
    base_url = config.base_url if config else DEFAULT_BASE_URL
    entity_count = db.scalar(select(func.count()).select_from(HAEntityCache)) or 0
    connection = get_connection(db)
    site = {"location_name": None, "time_zone": None}
    if connection and managed is not None:
        try:
            site = fetch_site(connection)
        except (OSError, ValueError):
            pass
    if managed is not None:
        return {
            "mode": managed.mode,
            "base_url": None,
            "configured": bool(connection),
            "enabled": bool(connection),
            "last_validated_at": None,
            "token_preview": None,
            "token_page_url": None,
            "entity_count": entity_count,
            **site,
        }
    return {
        "mode": "manual",
        "base_url": base_url,
        "configured": bool(config and get_secret(config.token_secret_ref)),
        "enabled": bool(config.enabled) if config else False,
        "last_validated_at": config.last_validated_at.isoformat() if config and config.last_validated_at else None,
        "token_preview": secret_preview(config.token_secret_ref) if config else None,
        "token_page_url": profile_token_url(base_url),
        "entity_count": entity_count,
        **site,
    }


def _entity_payload(entity: HAEntityCache) -> dict[str, object]:
    try:
        attributes = json.loads(entity.attributes_json)
    except json.JSONDecodeError:
        attributes = {}
    return {
        "entity_id": entity.entity_id,
        "domain": entity.domain,
        "name": entity.name,
        "device_id": entity.device_id,
        "state": entity.state,
        "attributes": attributes,
        "updated_at": entity.updated_at.isoformat() if entity.updated_at else None,
    }


@router.get("/config")
def get_config(db: Session = Depends(get_db)) -> dict[str, object]:
    return _config_payload(db, _get_config(db))


@router.put("/config")
def save_config(payload: dict[str, object], db: Session = Depends(get_db)) -> dict[str, object]:
    if supervisor_connection() is not None:
        raise HTTPException(status_code=409, detail="Home Assistant Supervisor manages this connection.")
    config = _get_config(db)
    base_url, origin = _manual_endpoint(payload, config)
    token = str(payload.get("token") or "")
    enabled = bool(payload.get("enabled", True))
    validate = bool(payload.get("validate", bool(token)))
    if not token:
        _saved_token(config, origin)

    if token and validate:
        result = validate_home_assistant_token(base_url, token)
        if not result.get("ok"):
            raise HTTPException(status_code=422, detail={"message": "Home Assistant token validation failed.", "result": result})

    if not config:
        config = HomeAssistantConfig(
            id=HA_CONFIG_ID,
            base_url=base_url,
            token_secret_ref="",
            enabled=enabled,
        )
        db.add(config)
    else:
        config.base_url = base_url
        config.enabled = enabled
    if token:
        # Never overwrite another origin's credential while its settings row
        # is still visible to other requests or if this transaction fails.
        origin_digest = sha256(json.dumps(origin).encode("utf-8")).hexdigest()
        config.token_secret_ref = set_secret(f"{HA_TOKEN_REF}.{origin_digest}", token)
        config.last_validated_at = datetime.utcnow() if validate else config.last_validated_at
    db.commit()
    db.refresh(config)
    return _config_payload(db, config)


@router.post("/test")
def test_config(payload: dict[str, object], db: Session = Depends(get_db)) -> dict[str, object]:
    managed = supervisor_connection()
    if managed is not None:
        if not managed.token:
            return {"ok": False, "detail": "Home Assistant Supervisor did not supply an app token."}
        return validate_home_assistant_token(managed.base_url, managed.token)
    config = _get_config(db)
    base_url, origin = _manual_endpoint(payload, config)
    token = str(payload.get("token") or "")
    if not token:
        token = _saved_token(config, origin) or ""
    if not token:
        return {"ok": False, "detail": "Paste a Home Assistant long-lived access token."}
    result = validate_home_assistant_token(base_url, token)
    if result.get("ok") and config:
        config.last_validated_at = datetime.utcnow()
        db.commit()
    return result


@router.get("/entities")
def entities(
    search: str | None = None,
    domain: str | None = None,
    refresh: bool = False,
    limit: int = Query(250, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    if supervisor_connection() and not refresh:
        refresh = db.scalar(select(HAEntityCache.entity_id).limit(1)) is None
    error: str | None = None
    if refresh:
        connection = get_connection(db)
        if not connection:
            raise HTTPException(status_code=409, detail="Home Assistant connection is unavailable.")
        try:
            states = fetch_states(connection.base_url, connection.token)
        except (OSError, ValueError) as exc:
            error = str(exc)
        else:
            db.execute(delete(HAEntityCache))
            for item in states:
                db.add(
                    HAEntityCache(
                        entity_id=item.entity_id,
                        domain=item.domain,
                        name=item.name,
                        state=item.state,
                        attributes_json=json.dumps(item.attributes, sort_keys=True),
                    )
                )
            db.commit()

    query = select(HAEntityCache)
    if domain:
        query = query.where(HAEntityCache.domain == domain)
    if search:
        like = f"%{search.lower()}%"
        query = query.where(
            func.lower(HAEntityCache.entity_id).like(like) | func.lower(HAEntityCache.name).like(like)
        )
    query = query.order_by(HAEntityCache.domain, HAEntityCache.entity_id).limit(limit)
    rows = db.scalars(query).all()
    return {
        "entities": [_entity_payload(entity) for entity in rows],
        "cached": not refresh or bool(error),
        "error": error,
    }


@router.post("/entities/refresh")
def refresh_entities(db: Session = Depends(get_db)) -> dict[str, object]:
    return entities(refresh=True, limit=250, db=db)


@router.get("/devices")
def ha_devices(db: Session = Depends(get_db)) -> dict[str, object]:
    rows = db.execute(select(HAEntityCache.domain, func.count()).group_by(HAEntityCache.domain)).all()
    return {
        "devices": [],
        "domains": [{"domain": domain, "entity_count": count} for domain, count in rows],
        "detail": "Home Assistant device-registry import is reserved; entity metadata is cached now.",
    }


@router.post("/mock/entities")
def load_mock_entities(db: Session = Depends(get_db)) -> dict[str, object]:
    if supervisor_connection() is not None:
        raise HTTPException(status_code=409, detail="This app uses the live Home Assistant entity registry.")
    mock_entities = normalize_state_response(
        [
            {
                "entity_id": "sensor.living_room_temperature",
                "state": "72",
                "attributes": {"friendly_name": "Living Room Temperature", "unit_of_measurement": "F"},
            },
            {"entity_id": "binary_sensor.front_door", "state": "off", "attributes": {"friendly_name": "Front Door"}},
        ]
    )
    db.execute(delete(HAEntityCache))
    for item in mock_entities:
        db.add(
            HAEntityCache(
                entity_id=item.entity_id,
                domain=item.domain,
                name=item.name,
                state=item.state,
                attributes_json=json.dumps(item.attributes, sort_keys=True),
            )
        )
    db.commit()
    return {"entities": len(mock_entities), "mock": True}
