import os
import socketserver
import stat
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import pytest

from inkdaddy_hub import ha_app
from inkdaddy_hub.services.matter_controller import MatterControllerError, get_controller


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        assert self.path == "/v1/health"
        if self.headers.get("Authorization") != "Bearer demo":
            self.send_error(401)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *_args):
        pass


def test_matter_client_uses_private_unix_http_and_preserves_bearer(monkeypatch):
    with TemporaryDirectory(prefix="inkdaddy-test-", dir="/tmp") as runtime:
        path = Path(runtime) / "controller.sock"
        monkeypatch.setenv("INKDADDY_MATTER_PORT", "unused")
        monkeypatch.setenv("INKDADDY_MATTER_SOCKET", str(path))
        monkeypatch.setenv("INKDADDY_MATTER_TOKEN", "demo")
        with socketserver.UnixStreamServer(str(path), _HealthHandler) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            try:
                assert get_controller().health() == {"ok": True}
                monkeypatch.setenv("INKDADDY_MATTER_TOKEN", "other")
                with pytest.raises(MatterControllerError) as caught:
                    get_controller().health()
                assert caught.value.status == 401
            finally:
                server.shutdown()
                thread.join()
        with pytest.raises(MatterControllerError, match="unavailable"):
            get_controller().health()


def test_managed_matter_uses_private_runtime_and_restores_environment(tmp_path, monkeypatch):
    script = tmp_path / "src" / "server.mjs"
    script.parent.mkdir()
    script.touch()
    monkeypatch.setattr(ha_app, "MATTER_EXECUTABLE", script)
    monkeypatch.setenv("INKDADDY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INKDADDY_MATTER_PORT", "unused")
    monkeypatch.setenv("INKDADDY_MATTER_SOCKET", "previous")
    monkeypatch.delenv("INKDADDY_MATTER_TOKEN", raising=False)
    launched = threading.Event()
    observed = {}

    def supervise(environment, stop):
        observed["environment"] = environment
        launched.set()
        stop.wait()
        observed["present_on_shutdown"] = Path(environment["INKDADDY_MATTER_SOCKET"]).parent.exists()

    monkeypatch.setattr(ha_app, "_maintain_matter_server", supervise)
    with ha_app.managed_matter():
        assert launched.wait(1)
        child_environment = observed["environment"]
        assert "INKDADDY_MATTER_PORT" not in child_environment
        assert child_environment["INKDADDY_MATTER_TOKEN"] == os.environ["INKDADDY_MATTER_TOKEN"]
        path = Path(get_controller().socket_path)
        assert str(path) == child_environment["INKDADDY_MATTER_SOCKET"]
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert observed["present_on_shutdown"]
    assert not path.parent.exists()
    assert os.environ["INKDADDY_MATTER_SOCKET"] == "previous"
    assert "INKDADDY_MATTER_TOKEN" not in os.environ


def test_matter_restart_removes_stale_endpoint_and_stops_active_child(tmp_path, monkeypatch):
    path = tmp_path / "controller.sock"
    path.touch()
    environment = {"INKDADDY_MATTER_SOCKET": str(path)}
    stop = threading.Event()
    monkeypatch.setattr(stop, "wait", lambda _seconds: stop.is_set())
    first = Mock()
    first.poll.return_value = 1
    replacement = Mock()
    replacement.poll.return_value = None
    outcomes = iter((first, replacement))

    def launch(*_args, **kwargs):
        assert kwargs["env"] == environment
        assert kwargs["umask"] == 0o077
        assert not path.exists()
        path.touch()
        child = next(outcomes)
        if child is replacement:
            stop.set()
        return child

    monkeypatch.setattr(ha_app.subprocess, "Popen", launch)
    ha_app._maintain_matter_server(environment, stop)
    first.terminate.assert_not_called()
    replacement.terminate.assert_called_once()
    replacement.wait.assert_called_once_with(timeout=6)
