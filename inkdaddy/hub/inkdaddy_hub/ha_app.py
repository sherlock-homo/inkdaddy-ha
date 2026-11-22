from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import socket
import subprocess
import threading
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

SUPERVISOR_IP = "172.30.32.2"
MINIMUM_INGRESS_SUPERVISOR = (2026, 11, 0)
MATTER_EXECUTABLE = Path("/opt/inkdaddy/matter_controller/src/server.mjs")
READ_METHODS = {"GET", "HEAD", "OPTIONS"}
REQUEST_MARKER = (b"x-inkdaddy-request", b"1")
MIB = 1024 * 1024


async def ingress_only(
    app: Callable[..., Awaitable[None]],
    scope: dict[str, Any],
    receive: Callable[..., Awaitable[dict[str, Any]]],
    send: Callable[..., Awaitable[None]],
) -> None:
    # Uvicorn runs with proxy headers disabled so this is the actual peer address.
    client = scope.get("client")
    if scope["type"] == "websocket" and (not client or client[0] != SUPERVISOR_IP):
        await send({"type": "websocket.close", "code": 1008})
        return
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    from starlette.datastructures import Headers, MutableHeaders
    from starlette.exceptions import HTTPException
    from starlette.responses import JSONResponse

    if not client or client[0] != SUPERVISOR_IP:
        await JSONResponse({"detail": "Access through Home Assistant Ingress."}, 403)(scope, receive, send)
        return

    path = scope.get("path", "/").removeprefix(scope.get("root_path", ""))

    async def secure_send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            headers = MutableHeaders(scope=message)
            headers["x-content-type-options"] = "nosniff"
            headers["referrer-policy"] = "no-referrer"
            headers["content-security-policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' blob: data:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
            )
            if path.startswith("/api/"):
                headers["cache-control"] = "no-store"
        await send(message)

    if scope.get("method", "GET") in READ_METHODS:
        await app(scope, receive, secure_send)
        return

    headers = Headers(scope=scope)
    # A cross-origin form cannot set this header. The managed app permits no CORS origins.
    if REQUEST_MARKER not in headers.raw:
        await JSONResponse({"detail": "Open inkDaddy in Home Assistant to make changes."}, 403)(
            scope, receive, secure_send
        )
        return

    from .services.photos import MAX_BATCH_BYTES, MAX_UPLOAD_BYTES

    limit = {
        "/api/photos": MAX_UPLOAD_BYTES + MIB,
        "/api/photos/batch": MAX_BATCH_BYTES + MIB,
    }.get(path, MIB)
    detail = "The request exceeds the upload limit. Split large photo batches and try again."
    length = headers.get("content-length")
    if length and (not length.isascii() or not length.isdecimal() or len(length) > 10 or int(length) > limit):
        await JSONResponse({"detail": detail}, 413)(scope, receive, secure_send)
        return

    received = 0

    async def bounded_receive() -> dict[str, Any]:
        nonlocal received
        message = await receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > limit:
                raise HTTPException(status_code=413, detail=detail)
        return message

    await app(scope, bounded_receive, secure_send)


def _supervisor_info(token: str, path: str) -> dict[str, Any]:
    connection = http.client.HTTPConnection("supervisor", timeout=10)
    try:
        connection.request("GET", path, headers={"Authorization": f"Bearer {token}"})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"Supervisor info returned HTTP {response.status}.")
        payload = json.load(response)
    except (OSError, http.client.HTTPException, ValueError):
        raise RuntimeError("Cannot read authenticated Home Assistant Supervisor info.") from None
    finally:
        connection.close()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("Supervisor did not return valid system info.")
    return data


def require_ingress_authorization(token: str) -> None:
    # Supervisor 2026.11.0 first enforces panel_admin for the proxied request.
    # /info is available with the app token without extra Supervisor privileges.
    version = _supervisor_info(token, "/info").get("supervisor")
    match = re.fullmatch(r"([0-9]{4})\.([0-9]{1,2})\.([0-9]{1,3})", version) if isinstance(version, str) else None
    if not match or tuple(map(int, match.groups())) < MINIMUM_INGRESS_SUPERVISOR:
        raise RuntimeError("Update Home Assistant Supervisor to 2026.11.0 or later for administrator-only Ingress.")


def ingress_port(token: str) -> int:
    port = _supervisor_info(token, "/addons/self/info").get("ingress_port")
    if type(port) is not int or not 62000 <= port <= 65500:
        raise RuntimeError("Supervisor did not assign an Ingress port.")
    return port


def ingress_host() -> str:
    # Supervisor uses the Docker network gateway to reach host-network apps.
    # Connecting a UDP socket selects the local interface without sending data.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((SUPERVISOR_IP, 9))
        return route.getsockname()[0]


def _maintain_matter_server(environment: dict[str, str], stop: threading.Event) -> None:
    socket_path = Path(environment["INKDADDY_MATTER_SOCKET"])
    while not stop.is_set():
        try:
            # The directory is private and the prior child has exited.
            socket_path.unlink(missing_ok=True)
            process = subprocess.Popen(
                ["node", str(MATTER_EXECUTABLE)], env=environment,
                cwd=MATTER_EXECUTABLE.parents[1], umask=0o077,
            )
        except OSError:
            stop.wait(5)
            continue
        while process.poll() is None and not stop.wait(1):
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        stop.wait(5)


@contextmanager
def managed_matter():
    if not MATTER_EXECUTABLE.is_file():
        yield
        return
    data = Path(os.getenv("INKDADDY_DATA_DIR", "/data/inkdaddy")) / "matter"
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="inkdaddy-matter-") as runtime:
        controller = {
            "INKDADDY_MATTER_SOCKET": str(Path(runtime) / "controller.sock"),
            "INKDADDY_MATTER_TOKEN": secrets.token_urlsafe(32),
        }
        previous = {key: os.environ.get(key) for key in controller}
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "NODE_ENV": "production",
            "INKDADDY_MATTER_NETWORK_PORT": os.getenv("INKDADDY_MATTER_NETWORK_PORT", "8792"),
            "INKDADDY_MATTER_STORE": str(data),
            **controller,
        }
        stop = threading.Event()
        supervisor = threading.Thread(target=_maintain_matter_server, args=(environment, stop), daemon=True)
        os.environ.update(controller)
        try:
            supervisor.start()
            try:
                yield
            finally:
                stop.set()
                supervisor.join(timeout=8)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def main() -> None:
    from functools import partial

    import uvicorn

    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("Supervisor did not supply an app token.")
    os.umask(0o077)
    require_ingress_authorization(token)
    port = ingress_port(token)

    with managed_matter():
        from .main import app

        uvicorn.run(
            partial(ingress_only, app),
            host=ingress_host(),
            port=port,
            interface="asgi3",
            lifespan="on",
            proxy_headers=False,
        )


if __name__ == "__main__":
    main()
