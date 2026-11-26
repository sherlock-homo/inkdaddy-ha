from __future__ import annotations

import asyncio
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

try:
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.staticfiles import StaticFiles
except ModuleNotFoundError as exc:  # pragma: no cover - dependency bootstrap path
    raise RuntimeError(
        "FastAPI is required to run the hub. Install dependencies with "
        '`pip install -e ".[dev]"` from the repository root.'
    ) from exc

from .api import dashboards, devices, diagnostics, health, home_assistant, integration_requests, integrations, matter_frames, photo_places, photos, setup, settings, simulator, updates
from .config import get_settings
from .database import init_db
from .services.home_assistant import supervisor_connection
from .services.integration_requests import relay_config, run_integration_requests
from .services.matter_controller import get_controller
from .services.photo_places import run_photo_places
from .services.udp_frame_transfer import start_udp_frame_transfer_listener
from .services.udp_heartbeat import start_udp_heartbeat_listener


@asynccontextmanager
async def lifespan(_app: FastAPI):
    controller = get_controller()
    managed = supervisor_connection() is not None
    relay_enabled = relay_config()[1] is not None

    stop_event = threading.Event()
    async def run_workers() -> None:
        workers = [asyncio.to_thread(run_photo_places, stop_event)]
        if managed:
            from .worker import refresh_home_assistant, run

            # Load the site and entity snapshot before the first frame can be offered.
            await asyncio.to_thread(refresh_home_assistant)
            workers.append(asyncio.to_thread(run, stop_event, refresh_at_start=False))
        if controller is not None:
            from .services.matter_delivery import run_matter_delivery

            workers.append(asyncio.to_thread(run_matter_delivery, stop_event, controller))
        if relay_enabled:
            workers.append(asyncio.to_thread(run_integration_requests, stop_event))
        await asyncio.gather(*workers)

    worker_task = asyncio.create_task(run_workers())
    try:
        yield
    finally:
        stop_event.set()
        await worker_task


def create_app() -> FastAPI:
    hub_settings = get_settings()
    managed = os.environ.get("INKDADDY_HA_MODE") == "supervisor"
    app = FastAPI(
        title="inkDaddy Hub", version=hub_settings.version, lifespan=lifespan,
        openapi_url=None if managed else "/openapi.json",
    )
    init_db()
    start_udp_heartbeat_listener(hub_settings)
    start_udp_frame_transfer_listener(hub_settings)

    if not managed:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(health.router)
    app.include_router(setup.router)
    app.include_router(settings.router)
    app.include_router(integrations.router)
    app.include_router(integration_requests.router)
    app.include_router(home_assistant.router)
    app.include_router(matter_frames.router)
    app.include_router(dashboards.router)
    app.include_router(photo_places.router)
    app.include_router(photos.router)
    app.include_router(devices.router)
    app.include_router(simulator.router)
    app.include_router(updates.router)
    app.include_router(diagnostics.router)

    dist = Path(hub_settings.frontend_dist)
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app


app = create_app()
