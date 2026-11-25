from __future__ import annotations

import http.client
from hashlib import sha256
import importlib.util
import json
import socket
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from inkdaddy_hub.api.integration_requests import router
from inkdaddy_hub.database import Base, get_db
from inkdaddy_hub.services import integration_request_protocol as wire
from inkdaddy_hub.services import integration_requests as delivery

URL = "https://relay.example.test/v1/integration-requests"
INGEST = "i" * 48
REVIEW = "r" * 48


@pytest.fixture
def queue_client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'hub.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(delivery, "SessionLocal", sessions)
    monkeypatch.delenv(delivery.URL_ENV, raising=False)
    monkeypatch.delenv(delivery.TOKEN_ENV, raising=False)
    app = FastAPI()
    app.include_router(router)

    def db():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = db
    with TestClient(app) as client:
        yield client, sessions
    engine.dispose()


def test_request_stays_local_without_configuration_and_deduplicates(queue_client, monkeypatch):
    client, sessions = queue_client
    monkeypatch.setattr(delivery, "MAX_REQUESTS_PER_DAY", 1)
    endpoint = "/api/integration-requests"
    created = client.post(endpoint, json={"integration_name": "  Home  Connect  "})
    assert created.status_code == 200
    row = created.json()["request"]
    assert row["integration_name"] == "Home Connect"
    assert row["status"] == "queued"
    duplicate = client.post(endpoint, json={"integration_name": "home connect"}).json()
    assert duplicate == {"request": row, "duplicate": True}
    assert client.post(endpoint, json={"integration_name": "Other integration"}).status_code == 429
    assert client.post(endpoint, json={"integration_name": "Test\nHome"}).status_code == 422
    assert client.post(endpoint, json={"integration_name": "Invalid", "ha_token": "fixture"}).status_code == 422
    assert client.get(endpoint).json() == {"relay": "unconfigured", "requests": [row]}
    with sessions() as db:
        assert db.get(delivery.IntegrationRequest, row["integration_id"]).status == "queued"


def test_request_form_status_only_returns_the_requested_receipt(queue_client):
    client, _ = queue_client
    base = "/api/integration-requests"
    client.post(base, json={"integration_name": "Existing Request"})
    receipt = client.post(base, json={"integration_name": "New Request"}).json()["request"]
    assert client.get(f"{base}/status").json() == {"relay": "unconfigured", "request": None}
    assert client.get(f"{base}/status", params={"integration_id": receipt["integration_id"]}).json() == {
        "relay": "unconfigured", "request": receipt,
    }
    for missing in ("bad-id", "0" * 32):
        assert client.get(f"{base}/status", params={"integration_id": missing}).status_code == 404
    assert len(client.get(base).json()["requests"]) == 2


def test_delivery_retries_and_sends_only_the_two_public_fields(queue_client, monkeypatch):
    client, _sessions = queue_client
    monkeypatch.setenv(delivery.URL_ENV, URL)
    monkeypatch.setenv(delivery.TOKEN_ENV, INGEST)
    clock = [datetime(2026, 11, 22, 20)]
    monkeypatch.setattr(delivery, "_now", lambda: clock[0])
    statuses = iter((503, 201))
    transmitted = []

    def relay_request(method, relay, body):
        assert method == "POST"
        assert relay.token == INGEST
        transmitted.append(json.loads(body))
        status = next(statuses)
        accepted = wire.accepted_payload(wire.parse_request(transmitted[-1]))
        return status, json.dumps(accepted).encode() if status == 201 else b""

    monkeypatch.setattr(delivery, "relay_request", relay_request)
    path = "/api/integration-requests"
    queued = client.post(path, json={"integration_name": "MQTT"}).json()["request"]
    relay = delivery.relay_config()[1]
    assert delivery.deliver_next(relay)
    assert client.get(path).json()["requests"][0]["status"] == "retrying"
    assert not delivery.deliver_next(relay)
    clock[0] += timedelta(seconds=30)
    assert delivery.deliver_next(relay)
    assert client.get(path).json()["requests"][0]["status"] == "delivered"
    assert not delivery.deliver_next(relay)
    assert transmitted == [
        {"integration_id": queued["integration_id"], "integration_name": "MQTT"},
        {"integration_id": queued["integration_id"], "integration_name": "MQTT"},
    ]
    assert URL not in client.get(path).text and INGEST not in client.get(path).text


def test_terminal_failure_and_explicit_retry_have_visible_state(queue_client, monkeypatch):
    client, _sessions = queue_client
    monkeypatch.setenv(delivery.URL_ENV, URL)
    monkeypatch.setenv(delivery.TOKEN_ENV, INGEST)
    clock = [datetime(2026, 11, 22, 20)]
    monkeypatch.setattr(delivery, "_now", lambda: clock[0])
    monkeypatch.setattr(delivery, "relay_request", lambda *_: (302, b""))
    base = "/api/integration-requests"
    row = client.post(base, json={"integration_name": "Matter"}).json()["request"]
    config = delivery.relay_config()[1]
    assert delivery.deliver_next(config)
    assert client.get(base).json()["requests"][0]["status"] == "failed"
    path = f"{base}/{row['integration_id']}/retry"
    assert client.post(path).status_code == 429
    clock[0] += delivery.RETRY_COOLDOWN
    assert client.post(path).json()["status"] == "queued"


def test_expired_claim_recovers_without_resetting_attempts(queue_client, monkeypatch):
    client, sessions = queue_client
    clock = datetime(2026, 11, 22, 20)
    monkeypatch.setattr(delivery, "_now", lambda: clock)
    client.post("/api/integration-requests", json={"integration_name": "Matter"})
    with sessions() as db:
        first = delivery._claim(db, clock)
    with sessions() as db:
        assert delivery._claim(db, clock + timedelta(seconds=1)) is None
    with sessions() as db:
        recovered = delivery._claim(db, clock + delivery.LEASE_TIME)
    assert recovered.request == first.request and recovered.attempts == first.attempts + 1
    assert recovered.lease != first.lease


@pytest.mark.parametrize("url", [
    "http://relay.example.test/v1/integration-requests",
    "https://relay.example.test/",
    "https://relay.example.test:444/v1/requests",
    "https://relay.local/v1/requests",
    "https://local/v1/requests",
    "https://relay.example.test/v1/../requests",
    "https://relay.example.test/v1/requests?anything=else",
    "https://name@relay.example.test/v1/requests",
])
def test_relay_url_rejects_ambiguous_or_nonpublic_configuration(url):
    with pytest.raises(ValueError):
        wire.configured_relay(url, INGEST)


@pytest.mark.parametrize("private", ["127.0.0.1", "192.168.0.1", "::1", "fd00::1", "64:ff9b::c0a8:1"])
def test_relay_rejects_any_nonpublic_dns_result(monkeypatch, private):
    monkeypatch.setattr(wire.socket, "getaddrinfo", lambda *_a, **_kw: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443)),
        (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (private, 443, 0, 0)),
    ])
    with pytest.raises(ValueError, match="addresses must be public"):
        wire.relay_request("POST", wire.configured_relay(URL, INGEST), b"{}")


def test_sender_pins_ip_and_does_not_read_or_follow_redirect(monkeypatch):
    monkeypatch.setattr(wire, "_relay_addresses", lambda hostname: ["1.1.1.1"])
    connections = []

    class Redirect:
        status = 302

        def read(self, *_args):
            raise AssertionError("The sender does not read a redirect response body")

    class Connection:
        def __init__(self, host, ip):
            connections.append((host, ip))

        def request(self, method, path, body, headers):
            assert (method, path, body) == ("POST", "/v1/integration-requests", b"{}")
            assert headers["Authorization"] == f"Bearer {INGEST}"

        def getresponse(self):
            return Redirect()

        def close(self):
            pass

    monkeypatch.setattr(wire, "_PinnedHTTPSConnection", Connection)
    assert wire.relay_request("POST", wire.configured_relay(URL, INGEST), b"{}") == (302, b"")
    assert connections == [("relay.example.test", "1.1.1.1")]


@pytest.fixture
def bridge():
    tool = Path(__file__).resolve().parents[3] / "tools" / "integration_request_bridge.py"
    spec = importlib.util.spec_from_file_location("integration_request_bridge", tool)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_relay_separates_ingress_and_private_pickup_and_requires_owner_approval(tmp_path, bridge, monkeypatch):
    remote = tmp_path / "relay.sqlite"
    local = tmp_path / "local.sqlite"
    handoff = tmp_path / "handoff.json"
    request = wire.request_data("Philips Hue")
    with bridge.RelayService(0, remote, INGEST, REVIEW) as server:
        worker = Thread(target=server.serve_forever)
        worker.start()

        def via_http(method, token, body=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                connection.request(method, wire.REQUEST_PATH, body, headers={
                    "Authorization": f"Bearer {token}", "Content-Type": "application/json",
                })
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()

        try:
            assert via_http("POST", REVIEW, wire.request_body(request))[0] == 401
            assert via_http("GET", INGEST)[0] == 401
            assert via_http("POST", INGEST, b'{"integration_name":"fixture","log":"fixture"}')[0] == 422
            status, receipt = via_http("POST", INGEST, wire.request_body(request))
            assert status == 201 and json.loads(receipt) == wire.accepted_payload(request)
            status, receipt = via_http("POST", INGEST, wire.request_body(request))
            assert status == 200 and json.loads(receipt) == wire.accepted_payload(request)
            assert json.loads(via_http("GET", REVIEW)[1]) == {"requests": [request.payload()]}

            monkeypatch.setattr(bridge, "relay_request", lambda method, relay: via_http(method, relay.token))
            assert bridge.pickup(URL, REVIEW, local) == 1
            assert bridge.pickup(URL, REVIEW, local) == 0
            assert local.stat().st_mode & 0o077 == 0
            with pytest.raises(ValueError, match="Approve"):
                bridge.prepare_handoff(local, request.integration_id, handoff)
            bridge.decide(local, request.integration_id, "approved")
            bridge.prepare_handoff(local, request.integration_id, handoff)
            assert handoff.stat().st_mode & 0o077 == 0
            assert json.loads(handoff.read_text()) == {
                "version": 1, "task": "assess_integration_compatibility", "integration": request.payload(),
            }
        finally:
            server.shutdown()
            worker.join()


def test_pickup_validates_full_queue_before_saving(tmp_path, bridge, monkeypatch):
    local = tmp_path / "local.sqlite"
    records = [wire.request_data("Philips Hue").payload(), {"other": "fixture"}]
    monkeypatch.setattr(bridge, "relay_request", lambda *_: (200, json.dumps({"requests": records}).encode()))
    with pytest.raises(ValueError, match="invalid queue"):
        bridge.pickup(URL, REVIEW, local)
    assert not local.exists()


def test_private_queue_rejects_public_or_linked_directory(tmp_path, bridge):
    exposed = tmp_path / "exposed"
    exposed.mkdir(mode=0o755)
    exposed.chmod(0o755)  # Exercise a public directory even under an owner-only process umask.
    with pytest.raises(ValueError, match="queue directory"):
        bridge.add_requests(exposed / "review.sqlite", [wire.request_data("Matter")], rate_limit=False)
    assert not (exposed / "review.sqlite").exists()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(private, target_is_directory=True)
    with pytest.raises(ValueError, match="queue directory"):
        bridge.add_requests(linked / "review.sqlite", [wire.request_data("Matter")], rate_limit=False)
    assert not (private / "review.sqlite").exists()


def test_watch_retries_with_private_token_and_sanitized_status(bridge, tmp_path, monkeypatch, capsys):
    tokenfile = tmp_path / "review.token"
    tokenfile.touch(mode=0o600)
    tokenfile.write_text(REVIEW + "\n")
    reads, waits = [], []

    def pickup(url, token, path):
        assert (url, token, path) == (URL, REVIEW, tmp_path / "queue.sqlite")
        reads.append(True)
        if len(reads) == 1:
            raise OSError("private-looking network detail")
        return 1

    def sleep(interval):
        waits.append(interval)
        if len(waits) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(bridge, "pickup", pickup)
    monkeypatch.setattr(bridge, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        bridge.watch(URL, tmp_path / "queue.sqlite", tokenfile, 60)
    assert len(reads) == 2 and waits == [60, 60]
    output = capsys.readouterr()
    assert output.out == "Saved 1 new requests.\n"
    assert output.err == "Pickup failed. The next check will retry.\n"


def test_development_and_review_use_approved_data_and_separate_sandboxes(tmp_path, bridge, monkeypatch):
    tool = Path(__file__).resolve().parents[3] / "tools" / "integration_request_review.py"
    spec = importlib.util.spec_from_file_location("integration_request_review", tool)
    review = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review)
    queue = tmp_path / "review.sqlite"
    manifest_path = tmp_path / "handoff.json"
    request = wire.request_data("Synthetic Integration Name")
    bridge.add_requests(queue, [request], rate_limit=False)
    bridge.decide(queue, request.integration_id, "approved")
    bridge.prepare_handoff(queue, request.integration_id, manifest_path)
    manifest = bridge.read_approved_handoff(queue, manifest_path)
    manifest_path.write_text(json.dumps({**manifest, "endpoint": "additional field"}))
    with pytest.raises(ValueError, match="valid integration request handoff"):
        bridge.read_approved_handoff(queue, manifest_path)

    source = tmp_path / "source"
    source.mkdir()
    old_source = source / "example.py"
    old_source.write_text("print('unchanged')\n")
    subprocess.run(["git", "-C", str(source), "init", "--quiet"], check=True)
    subprocess.run(["git", "-C", str(source), "add", "example.py"], check=True)
    tree = subprocess.check_output(["git", "-C", str(source), "write-tree"], text=True).strip()
    with pytest.raises(ValueError, match="must identify a commit"):
        review.develop_and_review(manifest, source, tree, tmp_path / "no-tree")
    revision = subprocess.check_output(
        ["git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit-tree", tree, "-m", "Fixture"], text=True,
    ).strip()
    old_source.write_text("print('replacement')\n")
    subprocess.run(["git", "-C", str(source), "add", "example.py"], check=True)
    replacement_tree = subprocess.check_output(["git", "-C", str(source), "write-tree"], text=True).strip()
    replacement = subprocess.check_output(
        ["git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit-tree", replacement_tree, "-m", "Replacement"], text=True,
    ).strip()
    subprocess.run(["git", "-C", str(source), "replace", revision, replacement], check=True)
    old_source.write_text("print('unchanged')\n")
    fake = tmp_path / "fake-codex"
    fake.write_text(f"#!{sys.executable}\n" + """import json
from hashlib import sha256
import os
import sys
from pathlib import Path

args = sys.argv
mode = args[args.index("--sandbox") + 1]
workspace = Path(args[args.index("-C") + 1])
report = Path(args[args.index("-o") + 1])
assert os.environ.get("INKDADDY_INTEGRATION_RELAY_TOKEN") is None
assert "Synthetic Integration Name" not in " ".join(args)
assert "--ephemeral" in args and "--ignore-user-config" in args
assert 'approval_policy="never"' in args and 'web_search="disabled"' in args
assert "sandbox_workspace_write.network_access=false" in args
assert "sandbox_workspace_write.exclude_tmpdir_env_var=true" in args
assert "sandbox_workspace_write.exclude_slash_tmp=true" in args
assert "TMPDIR" not in os.environ
manifest = json.loads((workspace / "integration-request.json").read_text())
assert manifest["integration"]["integration_name"] == "Synthetic Integration Name"
if mode == "workspace-write":
    (workspace / "example.py").write_text("print('proposed')\\n")
    (workspace / "new.py").write_text("enabled = True\\n")
    (workspace / "integration-request.json").write_text("operator data is intentionally excluded from the patch")
else:
    assert mode == "read-only"
    assert (workspace / "example.py").read_text() == "print('unchanged')\\n"
    patch = (workspace / "proposed.patch").read_text()
    assert "print('proposed')" in patch and "enabled = True" in patch
    assert "integration-request.json" not in patch and "proposed.patch" not in patch
digest = sha256((workspace / "proposed.patch").read_bytes()).hexdigest() if mode == "read-only" else ""
report.write_text(mode + "\\n" + (digest + "\\n" if digest else ""))
""")
    fake.chmod(0o700)
    monkeypatch.setenv(delivery.TOKEN_ENV, INGEST)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(review.shutil, "which", lambda _: str(fake))
    output = tmp_path / "run"
    review.develop_and_review(manifest, source, revision, output)
    assert old_source.read_text() == "print('unchanged')\n"
    assert (output / "development.txt").read_text() == "workspace-write\n"
    patch_path = output / "review" / "proposed.patch"
    proposed = patch_path.read_bytes()
    digest = sha256(proposed).hexdigest()
    report = output / "review.txt"
    assert report.read_text() == f"read-only\n{digest}\n"
    for contents in ("Missing patch.", "Read file.\n" + "0" * 64):
        report.write_text(contents)
        with pytest.raises(ValueError, match="exact proposed patch"):
            review.verify_review_report(report, patch_path, proposed)
    report.write_text(f"**Patch SHA256**\n`{digest}`\n")
    with pytest.raises(ValueError, match="written assessment"):
        review.verify_review_report(report, patch_path, proposed)
    report.write_text(f"**Patch SHA256**\n`{digest}`\nThe change updates example.py.")
    review.verify_review_report(report, patch_path, proposed)
    report.write_text(f"**Patch digest**: `{digest}`\nThe change updates example.py.")
    review.verify_review_report(report, patch_path, proposed)
    patch_path.write_bytes(b"")
    with pytest.raises(ValueError, match="changed during review"):
        review.verify_review_report(report, patch_path, proposed)
    report.write_text(f"**Patch digest**: `{sha256(b'').hexdigest()}`\nThe integration is already present.")
    review.verify_review_report(report, patch_path, b"")

    bridge.decide(queue, request.integration_id, "declined")
    saved = tmp_path / "saved-handoff.json"
    saved.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Approve"):
        bridge.read_approved_handoff(queue, saved)


@pytest.mark.parametrize("receipt", [
    b"an unrelated HTTP success page",
    b'{"status":"accepted"}',
    b'{"status":"accepted","integration_id":"00000000000000000000000000000000"}',
    b" " * (wire.MAX_ACK_BYTES + 1),
])
def test_delivery_requires_a_bounded_exact_relay_ack(queue_client, monkeypatch, receipt):
    client, _sessions = queue_client
    monkeypatch.setenv(delivery.URL_ENV, URL)
    monkeypatch.setenv(delivery.TOKEN_ENV, INGEST)
    monkeypatch.setattr(delivery, "relay_request", lambda *_: (200, receipt))
    path = "/api/integration-requests"
    client.post(path, json={"integration_name": "MQTT"})
    assert delivery.deliver_next(delivery.relay_config()[1])
    assert client.get(path).json()["requests"][0]["status"] == "failed"
