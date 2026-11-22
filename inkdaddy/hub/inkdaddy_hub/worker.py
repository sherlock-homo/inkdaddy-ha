from __future__ import annotations

import json
import logging
import signal
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from .config import get_settings
from .database import SessionLocal, init_db
from .models import Device, Setting
from .services.dashboard_content import SITE_SETTING
from .services import home_assistant
from .services.playback import begin_playback_write, prune_playback, refresh_playback_slots
from .services.updates import run_auto_update_cycle

LOGGER = logging.getLogger(__name__)
HA_REFRESH_SECONDS = 1800


def refresh_home_assistant() -> None:
    with SessionLocal() as db:
        connection = home_assistant.get_connection(db)
    if not connection:
        return
    try:
        states = home_assistant.fetch_states(connection.base_url, connection.token)
    except (OSError, ValueError):
        LOGGER.warning("Home Assistant states unavailable. Using the last cached readings.")
        states = None
    try:
        site_data = home_assistant.fetch_site(connection)
    except (OSError, ValueError):
        site_data = None
    if states is None and not site_data:
        return
    with SessionLocal() as db:
        begin_playback_write(db)
        if states is not None:
            home_assistant.replace_entity_cache(db, states)
        if site_data:
            site_row = db.get(Setting, SITE_SETTING)
            if site_row:
                site_row.value_json = json.dumps(site_data)
            else:
                db.add(Setting(key=SITE_SETTING, value_json=json.dumps(site_data)))
        db.commit()


def run_playback_cycle(now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    with SessionLocal() as db:
        device_ids = db.scalars(select(Device.id).where(Device.provisioned.is_(True))).all()
    for device_id in device_ids:
        with SessionLocal() as db:
            begin_playback_write(db)
            device = db.get(Device, device_id)
            if device and device.provisioned:
                try:
                    refresh_playback_slots(db, device, now=now)
                    db.commit()
                except (OSError, ValueError):
                    LOGGER.exception(
                        "Could not prepare the hourly queue for display %s.", device_id
                    )
    with SessionLocal() as db:
        begin_playback_write(db)
        prune_playback(db, now)
        db.commit()


def run(stop_event: threading.Event, *, refresh_at_start: bool = True) -> None:
    settings = get_settings()
    interval = max(300, settings.update_check_interval_seconds)
    last_update_check = float("-inf")
    last_ha_refresh = float("-inf") if refresh_at_start else time.monotonic()
    init_db()

    while not stop_event.is_set():
        now = time.monotonic()
        if now - last_ha_refresh >= HA_REFRESH_SECONDS:
            last_ha_refresh = now
            refresh_home_assistant()
        run_playback_cycle()
        if now - last_update_check >= interval:
            last_update_check = now
            result = run_auto_update_cycle(settings)
            if result.get("status") not in {"disabled", "current"}:
                print(
                    f"[inkDaddy worker] auto-update: {json.dumps(result, sort_keys=True)}",
                    flush=True,
                )
        stop_event.wait(min(30, interval))


def main() -> None:
    stop_event = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    run(stop_event)


if __name__ == "__main__":
    main()
