#!/usr/bin/env python3
"""Install inkDaddy's owner inbox into the current macOS login session."""

from __future__ import annotations

import argparse
import fcntl
import os
import plistlib
import re
import secrets
import stat
import subprocess
import sys
from dataclasses import dataclass
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

BRIDGE = Path("tools/integration_request_bridge.py")
PROTOCOL = Path("inkdaddy/hub/inkdaddy_hub/services/integration_request_protocol.py")
RELAY = "com.inkdaddy.requests.relay"
NOTIFY = "com.inkdaddy.requests.notify"
LAUNCHCTL = "/bin/launchctl"


def owned_directory(path: Path, *, private: bool) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    forbidden = 0o077 if private else 0o022
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & forbidden:
        raise ValueError(f"Check ownership and permissions of {path}.")


@contextmanager
def install_lock(state: Path):
    fd = os.open(state / "install.lock", os.O_NOFOLLOW | os.O_CREAT | os.O_RDWR | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Check ownership and permissions of the installation lock.")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def private_file(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError(f"Check ownership and permissions of {path}.")
        return source.read()


def write_atomic(path: Path, content: bytes, *, replace: bool = True, mode: int = 0o600) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        name = Path(tmp.name)
        try:
            tmp.write(content)
            tmp.flush()
            os.fchmod(tmp.fileno(), mode)
            os.fsync(tmp.fileno())
            if replace:
                os.replace(name, path)
            else:
                os.link(name, path)
        finally:
            name.unlink(missing_ok=True)


def credential(path: Path) -> bytes:
    try:
        value = private_file(path)
    except FileNotFoundError:
        value = secrets.token_urlsafe(48).encode() + b"\n"
        write_atomic(path, value, replace=False)
    value = value.rstrip(b"\r\n")
    if not re.fullmatch(rb"[A-Za-z0-9._~+/=-]{32,256}", value):
        raise ValueError(f"Invalid credential format in {path}.")
    return value


def git(source: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "--no-replace-objects", "-C", str(source), *args])


def prepare_runtime(source: Path, releases: Path, revision: str | None) -> Path:
    if revision is not None and not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", revision):
        raise ValueError("Use a full Git commit ID for --revision.")
    commit = git(source, "rev-parse", "--verify", (revision or "HEAD") + "^{commit}").decode().strip()
    if revision is not None and commit != revision:
        raise ValueError("The requested revision must identify a commit.")
    snapshot = {name: git(source, "cat-file", "blob", f"{commit}:{name}") for name in (BRIDGE, PROTOCOL)}
    release = releases / commit
    if release.exists() or release.is_symlink():
        if release.is_symlink() or not release.is_dir():
            raise ValueError("Invalid installed runtime directory.")
        expected_paths = set(snapshot)
        for name in snapshot:
            expected_paths.update(name.parents)
        expected_paths.discard(Path("."))
        if {p.relative_to(release) for p in release.rglob("*")} != expected_paths:
            raise ValueError("Installed runtime contains unexpected files.")
        for name, expected in snapshot.items():
            if (release / name).resolve() != release / name or private_file(release / name) != expected:
                raise ValueError("Installed runtime differs from its reviewed revision.")
    else:
        with tempfile.TemporaryDirectory(dir=releases) as staging:
            tree = Path(staging) / "runtime"
            owned_directory(tree, private=True)
            for name, data in snapshot.items():
                directory = tree
                for component in name.parts[:-1]:
                    directory = directory / component
                    owned_directory(directory, private=True)
                write_atomic(tree / name, data, replace=False, mode=0o400)
            # Invoke the same isolated interpreter/entry point the agents will use.
            subprocess.run([sys.executable, "-I", "-S", "-B", str(tree / BRIDGE),
                            "--db", os.devnull, "notify", "--help"], check=True, stdout=subprocess.DEVNULL)
            for directory in tree.rglob("*"):
                if directory.is_dir():
                    directory.chmod(0o500)
            tree.rename(release)
            release.chmod(0o500)
    return release / BRIDGE


def job_definitions(state: Path, script: Path) -> dict[str, bytes]:
    common = [sys.executable, "-I", "-S", "-B", str(script), "--db", str(state / "relay.sqlite")]
    jobs = {
        RELAY: {"KeepAlive": True, "ThrottleInterval": 10, "ProgramArguments": [
            *common, "serve", "--ingest-token-file", str(state / "ingest.token"),
            "--review-token-file", str(state / "review.token")]},
        NOTIFY: {"StartInterval": 300, "ProgramArguments": [*common, "notify"]},
    }
    return {label: plistlib.dumps({"Label": label, "RunAtLoad": True, "Umask": 0o077, **spec})
            for label, spec in jobs.items()}


def loaded(domain: str, label: str) -> bool:
    result = subprocess.run([LAUNCHCTL, "print", f"{domain}/{label}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def verify_listener(domain: str) -> None:
    """Confirm the relay job itself owns the local socket without sending a credential."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = subprocess.run([LAUNCHCTL, "print", f"{domain}/{RELAY}"], capture_output=True, text=True)
        pid = re.search(r"^\s*pid = ([1-9][0-9]*)$", result.stdout, re.MULTILINE)
        if result.returncode == 0 and pid:
            listener = subprocess.run(
                ["/usr/sbin/lsof", "-nP", "-a", "-p", pid[1], "-iTCP:8779", "-sTCP:LISTEN", "-Fn"],
                capture_output=True, text=True,
            )
            if listener.returncode == 0 and [row for row in listener.stdout.splitlines()
                                              if row.startswith("n")] == ["n127.0.0.1:8779"]:
                return
        time.sleep(0.25)
    raise ValueError("The inkDaddy relay did not start listening on 127.0.0.1:8779.")


@dataclass
class JobChange:
    label: str
    path: Path
    previous: bytes | None
    content: bytes
    was_loaded: bool


def install_jobs(directory: Path, jobs: dict[str, bytes], activate: bool) -> None:
    domain = f"gui/{os.getuid()}"
    if activate:
        subprocess.run([LAUNCHCTL, "print", domain], check=True, stdout=subprocess.DEVNULL)
    changes = []
    for label, content in jobs.items():
        path = directory / f"{label}.plist"
        try:
            previous = private_file(path)
        except FileNotFoundError:
            previous = None
        if previous is not None:
            old, new = plistlib.loads(previous), plistlib.loads(content)
            old_args, new_args = old.get("ProgramArguments"), new["ProgramArguments"]
            # Recognize our own revision-specific installation before replacing a job.
            releases = Path(new_args[4]).parents[2]
            script_pattern = re.escape(str(releases)) + r"/(?:[0-9a-f]{40}|[0-9a-f]{64})/" + re.escape(str(BRIDGE))
            if (old.get("Label") != label or not isinstance(old_args, list)
                    or len(old_args) != len(new_args) or old_args[1:4] != new_args[1:4]
                    or old_args[5:] != new_args[5:] or not isinstance(old_args[4], str)
                    or not re.fullmatch(script_pattern, old_args[4])):
                raise ValueError(f"A different LaunchAgent already occupies {path}.")
        running = activate and loaded(domain, label)
        if running and previous is None:
            raise ValueError(f"A different LaunchAgent already uses {label}.")
        if previous != content or (activate and not running):
            changes.append(JobChange(label, path, previous, content, running))

    applied = []
    try:
        for change in changes:
            if change.was_loaded:
                subprocess.run([LAUNCHCTL, "bootout", f"{domain}/{change.label}"], check=True)
            applied.append(change)
            write_atomic(change.path, change.content)
            if activate:
                subprocess.run([LAUNCHCTL, "bootstrap", domain, str(change.path)], check=True)
        if activate:
            verify_listener(domain)
    except (OSError, ValueError, subprocess.CalledProcessError):
        for change in reversed(applied):
            if activate and loaded(domain, change.label):
                subprocess.run([LAUNCHCTL, "bootout", f"{domain}/{change.label}"], check=True)
            if change.previous is None:
                change.path.unlink(missing_ok=True)
            else:
                write_atomic(change.path, change.previous)
                if change.was_loaded:
                    subprocess.run([LAUNCHCTL, "bootstrap", domain, str(change.path)], check=True)
        raise


def install(source: Path, home: Path, revision: str | None, *, activate: bool) -> Path:
    home = home.resolve(strict=True)
    for directory in (home, home / ".local", home / ".local/share",
                      home / "Library", home / "Library/LaunchAgents"):
        owned_directory(directory, private=False)
    state = home / ".local/share/inkdaddy-requests"
    owned_directory(state, private=True)
    with install_lock(state):
        releases = state / "releases"
        owned_directory(releases, private=True)
        script = prepare_runtime(source, releases, revision)
        if secrets.compare_digest(credential(state / "ingest.token"), credential(state / "review.token")):
            raise ValueError("The ingest and review credentials must differ.")
        install_jobs(home / "Library/LaunchAgents", job_definitions(state, script), activate)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", help="Reviewed Git commit ID. Defaults to the current commit.")
    parser.add_argument("--stage-home", type=Path, help="Prepare a separate test home without starting services.")
    args = parser.parse_args()
    activate = args.stage_home is None
    if activate and sys.platform != "darwin":
        parser.error("Activation requires macOS.")
    home = Path.home() if activate else args.stage_home
    if not activate and home.resolve() == Path.home().resolve():
        parser.error("Use a separate directory for --stage-home.")
    try:
        state = install(Path(__file__).resolve().parents[1], home, args.revision, activate=activate)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Installation stopped: {exc}", file=sys.stderr)
        return 1
    print(f"{'Installed' if activate else 'Staged'} the owner inbox at {state}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
