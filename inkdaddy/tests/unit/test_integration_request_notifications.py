import importlib.util
import os
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from inkdaddy_hub.services.integration_request_protocol import request_data


@pytest.fixture
def bridge():
    path = Path(__file__).resolve().parents[3] / "tools" / "integration_request_bridge.py"
    spec = importlib.util.spec_from_file_location("integration_request_bridge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_notify_batches_only_pending_requests_and_remembers_delivery(bridge, tmp_path, monkeypatch):
    queue = tmp_path / "inbox.sqlite"
    items = [request_data(name) for name in ("Fixture alpha", "Fixture beta", "Fixture gamma")]
    bridge.add_requests(queue, items, rate_limit=False)
    bridge.decide(queue, items[2].integration_id, "declined")
    native = Mock()
    monkeypatch.setenv("INKDADDY_INTEGRATION_RELAY_TOKEN", "fixture-local-secret")
    monkeypatch.setattr(bridge.subprocess, "run", native)
    assert bridge.notify_pending(queue) == 2
    assert bridge.notify_pending(queue) == 0
    native.assert_called_once()
    args, kwargs = native.call_args
    assert args[0][:2] == ["/usr/bin/osascript", "-e"]
    assert kwargs["check"] and kwargs["timeout"] > 0
    assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    assert not any(item.integration_name in str(native.call_args) for item in items)
    assert not any(item.integration_id in str(native.call_args) for item in items)
    assert "fixture-local-secret" not in str(native.call_args)
    assert bridge.add_requests(queue, items, rate_limit=False) == 0
    bridge.decide(queue, items[0].integration_id, "approved")
    assert bridge.notify_pending(queue) == 0
    stored = sorted(bridge.queue_payload(queue), key=lambda row: row["integration_name"])
    assert stored == [item.payload() for item in items]


def test_failed_native_post_leaves_request_pending_for_a_new_invocation(bridge, tmp_path, monkeypatch, capsys):
    queue = tmp_path / "inbox.sqlite"
    item = request_data("Fixture private name")
    native = Mock(side_effect=[subprocess.CalledProcessError(1, "private token fixture"), None])
    monkeypatch.setattr(bridge.subprocess, "run", native)
    monkeypatch.setattr(bridge.sys, "argv", ["bridge", "--db", str(queue), "notify"])
    assert bridge.notify_pending(queue) == 0
    bridge.add_requests(queue, [item], rate_limit=False)
    original_umask = os.umask(0o077)
    try:
        assert bridge.main() == 1
        assert capsys.readouterr().err == "macOS could not post the notification. The next check will retry.\n"
        assert bridge.main() == 0
        assert bridge.main() == 0
    finally:
        os.umask(original_umask)
    assert native.call_count == 2
    assert capsys.readouterr().out == ""
    with sqlite3.connect(queue) as db:
        assert db.execute("SELECT decision FROM integration_requests").fetchone() == ("pending",)


def test_arrivals_during_notification_are_picked_up_on_next_check(bridge, tmp_path, monkeypatch):
    queue = tmp_path / "inbox.sqlite"
    first = request_data("First request")
    second = request_data("Later request")
    bridge.add_requests(queue, [first], rate_limit=False)

    def post(*_args, **_kwargs):
        assert bridge.notify_pending(queue) == 0
        assert bridge.add_requests(queue, [second], rate_limit=False) == 1

    monkeypatch.setattr(bridge.subprocess, "run", post)
    assert bridge.notify_pending(queue) == 1
    monkeypatch.setattr(bridge.subprocess, "run", Mock())
    assert bridge.notify_pending(queue) == 1
    assert bridge.notify_pending(queue) == 0
    assert queue.with_name(queue.name + ".notify.lock").stat().st_mode & 0o077 == 0
