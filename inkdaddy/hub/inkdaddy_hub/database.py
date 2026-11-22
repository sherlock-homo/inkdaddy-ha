from __future__ import annotations

from collections.abc import Generator

from .config import get_settings

try:
    from sqlalchemy import create_engine, inspect
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
    from . import models  # noqa: F401

    settings.ensure_data_dirs()
    with engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        Base.metadata.create_all(bind=connection)
        columns = {column["name"] for column in inspect(connection).get_columns("ha_entity_cache")}
        if "source_reported_at" not in columns:
            connection.exec_driver_sql("ALTER TABLE ha_entity_cache ADD COLUMN source_reported_at TIMESTAMP")


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
