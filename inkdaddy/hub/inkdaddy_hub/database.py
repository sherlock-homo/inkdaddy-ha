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
            "photos": {"framing_json": "TEXT"},
            "devices": {"overlay_dashboard_id": "VARCHAR(128)"},
            "ha_entity_cache": {"source_reported_at": "TIMESTAMP"},
            "playback": {
                "manual_requested_at": "TIMESTAMP",
                "first_new_interval_minute": "INTEGER",
            },
            "playback_slots": {
                "retained_offer_id": "INTEGER",
                "manual_observed_at": "TIMESTAMP",
                "photo_render_json": "TEXT",
            },
        }
        for table, additions in migrations.items():
            columns = {column["name"] for column in schema.get_columns(table)}
            for name, type_sql in additions.items():
                if name not in columns:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {type_sql}")
        minute_columns = {
            "playback": {"anchor_hour": "anchor_minute", "manual_request_hour": "manual_request_minute"},
            "playback_slots": {"hour": "scheduled_minute"},
            "playback_offers": {"hour": "scheduled_minute"},
        }
        hourly_upgrade = "anchor_hour" in {column["name"] for column in schema.get_columns("playback")}
        for table, renames in minute_columns.items():
            columns = {column["name"] for column in schema.get_columns(table)}
            for old, new in renames.items():
                if old in columns:
                    connection.exec_driver_sql(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
                    # Negate first to avoid transient collisions in a composite primary key.
                    renamed_table = sql_table(table, column(new))
                    minutes = renamed_table.c[new]
                    connection.execute(renamed_table.update().values({minutes: minutes * -60}))
                    connection.execute(renamed_table.update().values({minutes: -minutes}))
                elif new not in columns:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {new} INTEGER")
        if hourly_upgrade:
            now = int(datetime.now(timezone.utc).timestamp()) // 60
            previous_due = now - now % 60
            # The old scheduler ran hourly even when a standalone device had
            # saved another interval. Start that cadence at a future slot.
            rows = connection.execute(
                select(Playback.device_id, Playback.anchor_minute, Playback.anchor_position,
                       Playback.paused, Device.refresh_interval_minutes)
                .select_from(Playback)
                .join(Device, Device.id == Playback.device_id)
                .where(Device.refresh_interval_minutes != 60)
            ).all()
            for device_id, anchor, position, paused, interval in rows:
                if not paused:
                    position += max(0, previous_due - anchor) // 60
                    anchor = previous_due
                first = previous_due + ((now - previous_due) // interval + 1) * interval
                connection.execute(update(Playback).where(Playback.device_id == device_id).values(
                    anchor_minute=anchor, anchor_position=position, first_new_interval_minute=first,
                ))
                connection.execute(delete(PlaybackSlot).where(
                    PlaybackSlot.device_id == device_id, PlaybackSlot.scheduled_minute > previous_due,
                ))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
