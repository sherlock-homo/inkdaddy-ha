from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timezone

from .config import get_settings

try:
    from sqlalchemy import column, create_engine, delete, inspect, select, table as sql_table, update
    from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
except ModuleNotFoundError as exc:  # pragma: no cover - exercised before deps are installed
    raise RuntimeError(
        "SQLAlchemy is required for the hub database. Install backend dependencies with "
        '`pip install -e ".[dev]"`.'
    ) from exc


class Base(DeclarativeBase):
    pass


settings = get_settings()
settings.ensure_data_dirs()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db() -> None:
    from .models import Device, Playback, PlaybackSlot

    settings.ensure_data_dirs()
    with engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        Base.metadata.create_all(bind=connection)
        schema = inspect(connection)
        migrations = {
            "photos": {"framing_json": "TEXT", "place_lookup": "VARCHAR(16)",
                       "place_key": "VARCHAR(64)", "place_label": "VARCHAR(180)", "place_retry_at": "TIMESTAMP",
                       "place_attempts": "INTEGER"},
            "devices": {"overlay_dashboard_id": "VARCHAR(128)", "interlace_dashboard_id": "VARCHAR(128)"},
            "ha_entity_cache": {"source_reported_at": "TIMESTAMP"},
            "playback": {
                "manual_requested_at": "TIMESTAMP",
            },
            "playback_slots": {
                "retained_offer_id": "INTEGER",
                "manual_observed_at": "TIMESTAMP",
                "photo_render_json": "TEXT",
                "dashboard_render_json": "TEXT",
            },
        }
        for table, additions in migrations.items():
            columns = {column["name"] for column in schema.get_columns(table)}
            for name, type_sql in additions.items():
                if name not in columns:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {type_sql}")
        # A slot key denotes an absolute wall time. Convert every reference together,
        # including in-flight offers and paused/manual anchors from either older schema.
        clock_columns = {
            "playback": {
                "anchor_second": ("anchor_minute", "anchor_hour"),
                "manual_request_second": ("manual_request_minute", "manual_request_hour"),
                "first_new_interval_second": ("first_new_interval_minute", None),
            },
            "playback_slots": {"scheduled_second": ("scheduled_minute", "hour")},
            "playback_offers": {"scheduled_second": ("scheduled_minute", "hour")},
        }
        hourly_upgrade = "anchor_hour" in {col["name"] for col in schema.get_columns("playback")}
        for table, clocks in clock_columns.items():
            columns = {col["name"] for col in schema.get_columns(table)}
            for name, (minutes, hours) in clocks.items():
                if name in columns:
                    continue
                old = minutes if minutes in columns else hours
                if old not in columns:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} BIGINT")
                    continue
                connection.exec_driver_sql(f"ALTER TABLE {table} RENAME COLUMN {old} TO {name}")
                if connection.dialect.name == "postgresql":
                    connection.exec_driver_sql(f"ALTER TABLE {table} ALTER COLUMN {name} TYPE BIGINT")
                renamed = sql_table(table, column(name))
                clock = renamed.c[name]
                multiplier = 60 if old == minutes else 3600
                # Negate first to avoid transient collisions in the composite slot key.
                connection.execute(renamed.update().values({clock: -clock * multiplier}))
                connection.execute(renamed.update().values({clock: -clock}))
        if hourly_upgrade:
            now = int(datetime.now(timezone.utc).timestamp())
            previous_due = now - now % 3600
            # The old scheduler ran hourly even when a standalone device had
            # saved another interval. Start that cadence at a future slot.
            rows = connection.execute(
                select(Playback.device_id, Playback.anchor_second, Playback.anchor_position,
                       Playback.paused, Device.refresh_interval_minutes)
                .select_from(Playback)
                .join(Device, Device.id == Playback.device_id)
                .where(Device.refresh_interval_minutes != 60)
            ).all()
            for device_id, anchor, position, paused, interval in rows:
                if not paused:
                    position += max(0, previous_due - anchor) // 3600
                    anchor = previous_due
                interval *= 60
                first = previous_due + ((now - previous_due) // interval + 1) * interval
                connection.execute(update(Playback).where(Playback.device_id == device_id).values(
                    anchor_second=anchor, anchor_position=position, first_new_interval_second=first,
                ))
                connection.execute(delete(PlaybackSlot).where(
                    PlaybackSlot.device_id == device_id, PlaybackSlot.scheduled_second > previous_due,
                ))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
