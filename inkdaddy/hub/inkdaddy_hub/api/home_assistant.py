from __future__ import annotations

import logging
from datetime import datetime, timezone
from itertools import islice
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import HAEntityCache, HomeAssistantConfig
from ..services.dashboard_calendar import fetch_calendar_choices
from ..services.dashboard_sensors import DASHBOARD_DOMAINS, battery_source, dashboard_eligible, entity_attributes
from ..services.home_assistant import (
    HA_CONFIG_ID,
    ENTITY_CACHE_DOMAINS,
    MANUAL_CONFIG_LOCK,
    cache_entity_attributes,
    cache_entity_state,
    fetch_site,
    fetch_states,
    get_connection,
    home_assistant_origin,
    normalize_state_response,
    profile_token_url,
    replace_entity_cache,
    supervisor_connection,
    validate_home_assistant_token,
)
from ..services.secrets import delete_secret, get_secret, retain_secret, secret_preview, set_secret

router = APIRouter(prefix="/api/home-assistant", tags=["home-assistant"])

HA_TOKEN_REF = "home_assistant.long_lived_access_token"
DEFAULT_BASE_URL = "http://homeassistant.local:8123"
LOGGER = logging.getLogger(__name__)


def _get_config(db: Session) -> HomeAssistantConfig | None:
    return db.get(HomeAssistantConfig, HA_CONFIG_ID, populate_existing=True)


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
    entity_count = db.scalar(select(func.count()).select_from(HAEntityCache)) or 0
    site = {"location_name": None, "time_zone": None}
    if managed is not None:
        if managed.token:
            try:
                site = fetch_site(managed)
            except (OSError, ValueError):
                pass
        return {
            "mode": managed.mode,
            "base_url": None,
            "configured": bool(managed.token),
            "enabled": bool(managed.token),
            "last_validated_at": None,
            "token_preview": None,
            "token_page_url": None,
            "entity_count": entity_count,
            **site,
        }
    base_url = config.base_url if config else DEFAULT_BASE_URL
    preview = secret_preview(config.token_secret_ref) if config else None
    return {
        "mode": "manual",
        "base_url": base_url,
        "configured": bool(preview),
        "enabled": bool(config.enabled) if config else False,
        "last_validated_at": config.last_validated_at.isoformat() if config and config.last_validated_at else None,
        "token_preview": preview,
        "token_page_url": profile_token_url(base_url),
        "entity_count": entity_count,
        **site,
    }


def _entity_payload(entity: HAEntityCache) -> dict[str, object]:
    if entity.domain == "zone":
        # Pre-upgrade zone.home rows may still have old report metadata.
        return {"entity_id": entity.entity_id, "domain": "zone", "name": entity.name,
                "state": None, "attributes": {}, "device_id": None, "source_reported_at": None, "updated_at": None}
    return {
        "entity_id": entity.entity_id,
        "domain": entity.domain,
        "name": entity.name,
        "device_id": entity.device_id,
        "state": cache_entity_state(entity.domain, entity.state),
        "attributes": cache_entity_attributes(entity_attributes(entity)),
        "source_reported_at": (
            entity.source_reported_at.replace(tzinfo=timezone.utc).isoformat()
            if entity.source_reported_at else None
        ),
        "updated_at": entity.updated_at.isoformat() if entity.updated_at else None,
    }


@router.get("/config")
def get_config(db: Session = Depends(get_db)) -> dict[str, object]:
    if supervisor_connection() is not None:
        return _config_payload(db, None)
    with MANUAL_CONFIG_LOCK:
        return _config_payload(db, _get_config(db))


@router.put("/config")
def save_config(payload: dict[str, object], db: Session = Depends(get_db)) -> dict[str, object]:
    if supervisor_connection() is not None:
        raise HTTPException(status_code=409, detail="Home Assistant Supervisor manages this connection.")
    # The hub serves a single app process. Serialize standalone configuration saves
    # across its request threads until the corresponding secret cleanup finishes.
    with MANUAL_CONFIG_LOCK:
        return _save_manual_config(db, payload)


def _save_manual_config(db: Session, payload: dict[str, object]) -> dict[str, object]:
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
    replacement = f"{HA_TOKEN_REF}.{uuid4().hex}" if token else None
    try:
        if replacement:
            # Keep the current credential usable until the settings commit succeeds.
            set_secret(replacement, token)
            config.token_secret_ref = replacement
            config.last_validated_at = datetime.now(timezone.utc).replace(tzinfo=None) if validate else None
        db.commit()
    except Exception:
        db.rollback()
        try:
            delete_secret(replacement)
        except OSError:
            LOGGER.warning("Could not remove the uncommitted Home Assistant credential from the private store.")
        raise
    db.refresh(config)
    try:
        retain_secret(HA_TOKEN_REF, config.token_secret_ref)
    except OSError:
        LOGGER.warning("Could not remove superseded Home Assistant credentials from the private store.")
    return _config_payload(db, config)


@router.post("/test")
def test_config(payload: dict[str, object], db: Session = Depends(get_db)) -> dict[str, object]:
    managed = supervisor_connection()
    if managed is not None:
        if not managed.token:
            return {"ok": False, "detail": "Home Assistant Supervisor did not supply an app token."}
        return validate_home_assistant_token(managed.base_url, managed.token)
    supplied_token = str(payload.get("token") or "")
    with MANUAL_CONFIG_LOCK:
        config = _get_config(db)
        base_url, origin = _manual_endpoint(payload, config)
        token = supplied_token or _saved_token(config, origin)
        validated_ref = (
            config.token_secret_ref
            if not supplied_token and config and config.base_url == base_url else None
        )
    if not token:
        return {"ok": False, "detail": "Paste a Home Assistant long-lived access token."}
    result = validate_home_assistant_token(base_url, token)
    if result.get("ok") and validated_ref:
        db.execute(
            update(HomeAssistantConfig)
            .where(
                HomeAssistantConfig.id == HA_CONFIG_ID,
                HomeAssistantConfig.base_url == base_url,
                HomeAssistantConfig.token_secret_ref == validated_ref,
            )
            .values(last_validated_at=datetime.now(timezone.utc).replace(tzinfo=None))
        )
        db.commit()
    return result


@router.get("/entities")
def entities(
    search: str | None = None,
    domain: str | None = None,
    limit: int = Query(250, ge=1, le=1000),
    db: Session = Depends(get_db),
    purpose: Literal["dashboard", "location", "battery"] | None = None,
) -> dict[str, object]:
    domains = {"dashboard": DASHBOARD_DOMAINS, "battery": ("sensor",), "location": ("zone",)}
    query = select(HAEntityCache).where(HAEntityCache.domain.in_(domains.get(purpose, ENTITY_CACHE_DOMAINS)))
    if domain:
        query = query.where(HAEntityCache.domain == domain)
    if search:
        like = f"%{search.lower()}%"
        query = query.where(
            func.lower(HAEntityCache.entity_id).like(like) | func.lower(HAEntityCache.name).like(like)
        )
    query = query.order_by(HAEntityCache.domain, HAEntityCache.entity_id)
    if purpose in ("dashboard", "battery"):
        query = query.execution_options(yield_per=100)
        now = datetime.now(timezone.utc)
        candidates = db.scalars(query)
        choices = filter(battery_source, candidates) if purpose == "battery" else (
            entity for entity in candidates if dashboard_eligible(entity, now)
        )
        rows = islice(choices, limit)
    else:
        rows = db.scalars(query.limit(limit))
    return {
        "entities": [_entity_payload(entity) for entity in rows],
        "cached": True,
        "error": None,
    }


@router.post("/entities/refresh")
def refresh_entities(db: Session = Depends(get_db)) -> dict[str, object]:
    connection = get_connection(db)
    if not connection:
        raise HTTPException(status_code=409, detail="Home Assistant connection is unavailable.")
    try:
        states = fetch_states(connection.base_url, connection.token)
    except (OSError, ValueError):
        return {**entities(db=db, limit=250), "error": "Home Assistant could not refresh the entity cache."}
    replace_entity_cache(db, states)
    db.commit()
    return {**entities(db=db, limit=250), "cached": False}


@router.get("/devices")
def ha_devices(db: Session = Depends(get_db)) -> dict[str, object]:
    rows = db.execute(
        select(HAEntityCache.domain, func.count())
        .where(HAEntityCache.domain.in_(ENTITY_CACHE_DOMAINS))
        .group_by(HAEntityCache.domain)
    ).all()
    return {
        "devices": [],
        "domains": [{"domain": domain, "entity_count": count} for domain, count in rows],
        "detail": "Home Assistant device-registry import is reserved; entity metadata is cached now.",
    }


@router.get("/calendars")
def list_calendars(db: Session = Depends(get_db)) -> dict[str, object]:
    connection = get_connection(db)
    if connection is None:
        return {"calendars": [], "error": "Connect Home Assistant to choose a calendar."}
    try:
        choices = fetch_calendar_choices(connection)
    except (OSError, ValueError):
        return {"calendars": [], "error": "Home Assistant calendars are unavailable."}
    return {"calendars": choices, "error": None}


@router.post("/mock/entities")
def load_mock_entities(db: Session = Depends(get_db)) -> dict[str, object]:
    if supervisor_connection() is not None:
        raise HTTPException(status_code=409, detail="This app uses the live Home Assistant entity registry.")
    mock_entities = normalize_state_response(
        [
            {
                "entity_id": "sensor.living_room_temperature",
                "state": "72",
                "last_reported": datetime.now(timezone.utc).isoformat(),
                "attributes": {"friendly_name": "Living Room Temperature", "unit_of_measurement": "°F"},
            },
            {"entity_id": "binary_sensor.front_door", "state": "off", "attributes": {"friendly_name": "Front Door"}},
        ]
    )
    replace_entity_cache(db, mock_entities)
    db.commit()
    return {"entities": len(mock_entities), "mock": True}
