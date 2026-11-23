import importlib
import os
import plistlib
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

from inkdaddy_hub.services import integration_request_protocol


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    tools = Path(__file__).resolve().parents[3] / "tools"
    monkeypatch.syspath_prepend(str(tools))
    module = importlib.import_module("install_request_inbox")
    sources = {
        str(module.BRIDGE): (tools / module.BRIDGE.name).read_bytes(),
        str(module.PROTOCOL): Path(integration_request_protocol.__file__).read_bytes(),
    }

    def git(_source, *args):
        if args[:2] == ("rev-parse", "--verify"):
            return args[2].removesuffix("^{commit}").encode()
        assert args[:2] == ("cat-file", "blob")
        return sources[args[2].split(":", 1)[1]]

    monkeypatch.setattr(module, "git", git)
    return module


def test_inbox_install_preserves_private_state_and_only_stages_reviewed_code(inbox, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    commit = "a" * 40
    state = inbox.install(tmp_path, home, commit, activate=False)
    agents = home / "Library/LaunchAgents"
    review = (state / "review.token").read_bytes()
    ingest = (state / "ingest.token").read_bytes()
    assert review != ingest
    for name in ("ingest.token", "review.token"):
        assert stat.S_IMODE((state / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    runtime = state / "releases" / commit
    assert {p.relative_to(runtime) for p in runtime.rglob("*") if p.is_file()} == {inbox.BRIDGE, inbox.PROTOCOL}
    assert all(stat.S_IMODE(p.stat().st_mode) == (0o500 if p.is_dir() else 0o400)
               for p in [runtime, *runtime.rglob("*")])
    relay = plistlib.loads((agents / f"{inbox.RELAY}.plist").read_bytes())
    notify = plistlib.loads((agents / f"{inbox.NOTIFY}.plist").read_bytes())
    assert relay["KeepAlive"] and notify["StartInterval"] == 300
    assert "serve" in relay["ProgramArguments"]
    assert notify["ProgramArguments"][-3:] == ["--db", str(state / "relay.sqlite"), "notify"]
    assert not any(token.strip() in plistlib.dumps(job) for token in (review, ingest) for job in (relay, notify))
    subprocess.run(notify["ProgramArguments"], check=True)
    with sqlite3.connect(state / "relay.sqlite") as db:
        db.execute("INSERT INTO integration_requests VALUES (?, ?, ?, ?)", ("1"*32, "Test", 1, "approved"))

    before = (agents / f"{inbox.RELAY}.plist").stat()
    inbox.install(tmp_path, home, commit, activate=False)
    assert before.st_ino == (agents / f"{inbox.RELAY}.plist").stat().st_ino
    inbox.install(tmp_path, home, "b"*40, activate=False)
    assert (state / "review.token").read_bytes() == review and (state / "ingest.token").read_bytes() == ingest
    with sqlite3.connect(state / "relay.sqlite") as db:
        assert db.execute("SELECT decision FROM integration_requests").fetchone() == ("approved",)


def test_inbox_refuses_unsafe_or_altered_install(inbox, tmp_path):
    state = inbox.install(tmp_path, tmp_path, "a"*40, activate=False)
    token = state / "review.token"
    token.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        inbox.install(tmp_path, tmp_path, "a"*40, activate=False)
    token.chmod(0o600)
    bridge = state / "releases" / ("a"*40) / inbox.BRIDGE
    bridge.chmod(0o600)
    bridge.write_text("changed")
    with pytest.raises(ValueError, match="reviewed"):
        inbox.install(tmp_path, tmp_path, "a"*40, activate=False)


@pytest.mark.parametrize("failure", ["bootstrap", "listener"])
def test_failed_upgrade_restores_both_running_agents(inbox, tmp_path, monkeypatch, failure):
    import types

    state = inbox.install(tmp_path, tmp_path, "a"*40, activate=False)
    directory = tmp_path / "Library/LaunchAgents"
    old = {p.stem: p.read_bytes() for p in directory.iterdir()}
    new = inbox.job_definitions(state, state / "releases" / ("b"*40) / inbox.BRIDGE)
    active = set(old)

    def launchctl(argv, **kwargs):
        assert argv[0] == inbox.LAUNCHCTL
        if argv[1] == "print":
            label = argv[2].rsplit("/",1)[-1]
            ok = label in active or label == str(os.getuid())
            return types.SimpleNamespace(returncode=0 if ok else 113)
        if argv[1] == "bootout":
            active.remove(argv[2].rsplit("/",1)[-1])
        else:
            path = Path(argv[3])
            if failure == "bootstrap" and path.stem == inbox.NOTIFY and path.read_bytes() == new[inbox.NOTIFY]:
                raise subprocess.CalledProcessError(5, argv)
            active.add(path.stem)
        return types.SimpleNamespace(returncode=0)

    def failed_health(_domain):
        raise ValueError("The relay failed to bind its loopback port.")

    monkeypatch.setattr(inbox.subprocess, "run", launchctl)
    monkeypatch.setattr(inbox, "verify_listener", failed_health)
    with pytest.raises((subprocess.CalledProcessError, ValueError)):
        inbox.install_jobs(directory, new, activate=True)
    assert active == set(old)
    assert {p.stem: p.read_bytes() for p in directory.iterdir()} == old
