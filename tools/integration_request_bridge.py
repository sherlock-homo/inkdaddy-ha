#!/usr/bin/env python3
"""Owner relay, private pickup, and explicit review for inkDaddy integration requests."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import sqlite3
import stat
import subprocess
import sys
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from time import sleep, time

# Only the standard-library wire contract is shared with the packaged app.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "inkdaddy" / "hub"))

from inkdaddy_hub.services.integration_request_protocol import (
    INTEGRATION_ID_PATTERN,
    MAX_REQUEST_BYTES,
    REQUEST_PATH,
    TOKEN_PATTERN,
    RequestData,
    accepted_payload,
    configured_relay,
    parse_request,
    relay_request,
)

MAX_QUEUE = 512
MAX_NEW_REQUESTS_PER_DAY = 100


class QueueLimit(ValueError):
    pass


def token_file(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ValueError("Token files must be private regular files owned by the current user.")
        raw = source.read(259)
    try:
        token = raw.decode("ascii").rstrip("\r\n")
    except UnicodeDecodeError:
        raise ValueError("The relay token format is invalid.") from None
    if len(raw) > 258 or TOKEN_PATTERN.fullmatch(token) is None:
        raise ValueError("The relay token format is invalid.")
    return token


@contextmanager
def open_queue(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.stat(follow_symlinks=False)
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise ValueError("The owner queue directory must be private and owned by the current user.")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ValueError("The owner queue must be a private regular file owned by the current user.")
    finally:
        os.close(fd)
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS integration_requests (
                    integration_id TEXT PRIMARY KEY,
                    integration_name TEXT NOT NULL,
                    received_at INTEGER NOT NULL,
                    decision TEXT NOT NULL DEFAULT 'pending'
                        CHECK (decision IN ('pending', 'approved', 'declined'))
                )
            """)
            yield connection
    finally:
        connection.close()


def add_requests(path: Path, requests: list[RequestData], *, rate_limit: bool) -> int:
    with open_queue(path) as db:
        db.execute("BEGIN IMMEDIATE")
        existing = {row[0] for row in db.execute("SELECT integration_id FROM integration_requests")}
        pending = {}
        for request in requests:
            if request.integration_id not in existing:
                pending.setdefault(request.integration_id, request.integration_name)
        if not pending:
            return 0
        if len(existing) + len(pending) > MAX_QUEUE:
            raise QueueLimit("The owner queue has reached its capacity.")
        now = int(time())
        if rate_limit:
            daily, = db.execute(
                "SELECT count(*) FROM integration_requests WHERE received_at > ?", (now - 86400,),
            ).fetchone()
            if daily + len(pending) > MAX_NEW_REQUESTS_PER_DAY:
                raise QueueLimit("The owner queue reached its daily limit.")
        db.executemany(
            "INSERT INTO integration_requests (integration_id, integration_name, received_at) VALUES (?, ?, ?)",
            [(identifier, name, now) for identifier, name in pending.items()],
        )
        return len(pending)


def queue_payload(path: Path) -> list[dict[str, str]]:
    with open_queue(path) as db:
        return [
            dict(row) for row in db.execute(
                "SELECT integration_id, integration_name FROM integration_requests ORDER BY received_at, integration_id"
            )
        ]


class RelayService(HTTPServer):
    request_queue_size = 16

    def __init__(self, port: int, queue: Path, ingest_token: str, review_token: str):
        if secrets.compare_digest(ingest_token, review_token):
            raise ValueError("Ingest and review tokens must be different.")
        self.queue = queue
        self.ingest_token = ingest_token
        self.review_token = review_token
        super().__init__(("127.0.0.1", port), RelayHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(6)
        return connection, address

    def handle_error(self, _request, _client_address):
        # The default socketserver error handler includes a sender address.
        print("A relay connection ended before completion.", file=sys.stderr)


class RelayHandler(BaseHTTPRequestHandler):
    server: RelayService
    server_version = "inkDaddy-relay"
    sys_version = ""

    def log_message(self, *_args):
        # Relay logs omit sender addresses, request fields, and credentials.
        pass

    def _reply(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorize(self, token: str) -> bool:
        if self.path != REQUEST_PATH:
            self._reply(404, {"status": "not_found"})
            return False
        supplied = self.headers.get_all("Authorization", [])
        if len(supplied) != 1 or not supplied[0].isascii() or not secrets.compare_digest(supplied[0], f"Bearer {token}"):
            self._reply(401, {"status": "unauthorized"})
            return False
        return True

    def do_POST(self) -> None:
        if not self._authorize(self.server.ingest_token):
            return
        lengths = self.headers.get_all("Content-Length", [])
        if (
            len(lengths) != 1 or len(lengths[0]) > 4 or not lengths[0].isascii() or not lengths[0].isdecimal()
            or not 0 < int(lengths[0]) <= MAX_REQUEST_BYTES or self.headers.get("Transfer-Encoding") is not None
        ):
            self._reply(413, {"status": "invalid_size"})
            return
        if self.headers.get_content_type() != "application/json":
            self._reply(415, {"status": "invalid_type"})
            return
        try:
            size = int(lengths[0])
            contents = self.rfile.read(size)
            if len(contents) != size:
                raise ValueError("Incomplete request.")
            request = parse_request(json.loads(contents.decode("utf-8")))
            inserted = add_requests(self.server.queue, [request], rate_limit=True)
        except QueueLimit:
            self._reply(429, {"status": "rate_limited"})
        except (ValueError, RecursionError):
            self._reply(422, {"status": "invalid_request"})
        except (sqlite3.Error, OSError):
            self._reply(503, {"status": "temporarily_unavailable"})
        else:
            self._reply(201 if inserted else 200, accepted_payload(request))

    def do_GET(self) -> None:
        if not self._authorize(self.server.review_token):
            return
        try:
            payload = queue_payload(self.server.queue)
        except (sqlite3.Error, OSError):
            self._reply(503, {"status": "temporarily_unavailable"})
        else:
            self._reply(200, {"requests": payload})


def pickup(url: str, token: str, path: Path) -> int:
    relay = configured_relay(url, token)
    status, contents = relay_request("GET", relay)
    if status != 200:
        raise ValueError("The owner relay did not accept the pickup request.")
    try:
        payload = json.loads(contents.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"requests"}:
            raise ValueError
        records = payload["requests"]
        if not isinstance(records, list) or len(records) > MAX_QUEUE:
            raise ValueError
        # Validate the complete batch before saving any records.
        requests = [parse_request(item) for item in records]
    except (ValueError, RecursionError):
        raise ValueError("The owner relay returned an invalid queue.") from None
    return add_requests(path, requests, rate_limit=False)


def decide(path: Path, integration_id: str, decision: str) -> None:
    if INTEGRATION_ID_PATTERN.fullmatch(integration_id) is None:
        raise ValueError("Invalid integration identifier.")
    with open_queue(path) as db:
        updated = db.execute(
            "UPDATE integration_requests SET decision = ? WHERE integration_id = ?",
            (decision, integration_id),
        )
        if updated.rowcount != 1:
            raise ValueError("Integration request not found.")


def _approved_request(path: Path, integration_id: str) -> RequestData:
    if INTEGRATION_ID_PATTERN.fullmatch(integration_id) is None:
        raise ValueError("Invalid integration identifier.")
    with open_queue(path) as db:
        row = db.execute(
            "SELECT integration_id, integration_name, decision FROM integration_requests WHERE integration_id = ?",
            (integration_id,),
        ).fetchone()
    if row is None or row["decision"] != "approved":
        raise ValueError("Approve the integration request before preparing a handoff.")
    return parse_request({"integration_id": row["integration_id"], "integration_name": row["integration_name"]})


def _manifest(request: RequestData) -> dict:
    return {"version": 1, "task": "assess_integration_compatibility", "integration": request.payload()}


def prepare_handoff(path: Path, integration_id: str, destination: Path) -> RequestData:
    request = _approved_request(path, integration_id)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(_manifest(request), output, ensure_ascii=True, indent=2)
        output.write("\n")
    return request


def read_approved_handoff(queue: Path, handoff: Path) -> dict:
    fd = os.open(handoff, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("Choose a regular handoff file.")
        contents = source.read(2049)
    try:
        if len(contents) > 2048:
            raise ValueError
        payload = json.loads(contents.decode("utf-8"))
        request = parse_request(payload["integration"])
        if type(payload.get("version")) is not int or payload != _manifest(request):
            raise ValueError
    except (KeyError, TypeError, ValueError, RecursionError):
        raise ValueError("Choose a valid integration request handoff.") from None
    approved = _approved_request(queue, request.integration_id)
    if request != approved:
        raise ValueError("The handoff does not match the approved request.")
    return _manifest(approved)


def watch(url: str, path: Path, review_token_file: Path, interval: int) -> None:
    if not 60 <= interval <= 3600:
        raise ValueError("Choose a pickup interval from 60 to 3600 seconds.")
    # A typo in endpoint or initial credentials fails before starting the periodic loop.
    configured_relay(url, token_file(review_token_file))
    while True:
        try:
            count = pickup(url, token_file(review_token_file), path)
            if count:
                print(f"Saved {count} new requests.", flush=True)
        except (sqlite3.Error, OSError, ValueError, http.client.HTTPException):
            print("Pickup failed. The next check will retry.", file=sys.stderr, flush=True)
        sleep(interval)


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="Owner-controlled private SQLite file.")
    commands = parser.add_subparsers(dest="command", required=True)

    server = commands.add_parser("serve", help="Start the loopback server behind the owner’s TLS proxy.")
    server.add_argument("--port", default=8779, type=int)
    server.add_argument("--ingest-token-file", required=True, type=Path)
    server.add_argument("--review-token-file", required=True, type=Path)

    fetch = commands.add_parser("pickup", help="Fetch a private review queue over HTTPS.")
    fetch.add_argument("--url", required=True)
    fetch.add_argument("--review-token-file", required=True, type=Path)
    watcher = commands.add_parser("watch", help="Periodically pick up requests into the private queue.")
    watcher.add_argument("--url", required=True)
    watcher.add_argument("--review-token-file", required=True, type=Path)
    watcher.add_argument("--seconds", type=int, default=300)

    commands.add_parser("list", help="List saved requests and owner review decisions.")
    decision = commands.add_parser("decide", help="Record an explicit owner review decision.")
    decision.add_argument("integration_id")
    decision.add_argument("decision", choices=("approved", "declined"))

    handoff = commands.add_parser("handoff", help="Write a data-only handoff for one approved request.")
    handoff.add_argument("integration_id")
    handoff.add_argument("--output", required=True, type=Path)

    develop = commands.add_parser("develop", help="Launch local development and read-only review for an approved handoff.")
    develop.add_argument("--handoff", required=True, type=Path)
    develop.add_argument("--source", required=True, type=Path)
    develop.add_argument("--revision", required=True)
    develop.add_argument("--output", required=True, type=Path)

    args = parser.parse_args()
    try:
        if args.command == "serve":
            with RelayService(
                args.port, args.db, token_file(args.ingest_token_file), token_file(args.review_token_file),
            ) as service:
                service.serve_forever()
        elif args.command == "pickup":
            print(f"Saved {pickup(args.url, token_file(args.review_token_file), args.db)} new requests.")
        elif args.command == "watch":
            watch(args.url, args.db, args.review_token_file, args.seconds)
        elif args.command == "list":
            with open_queue(args.db) as db:
                print(json.dumps([dict(row) for row in db.execute(
                    "SELECT integration_id, integration_name, decision FROM integration_requests ORDER BY received_at"
                )], ensure_ascii=True, indent=2))
        elif args.command == "decide":
            decide(args.db, args.integration_id, args.decision)
        elif args.command == "handoff":
            prepare_handoff(args.db, args.integration_id, args.output)
        else:
            from integration_request_review import develop_and_review

            request = read_approved_handoff(args.db, args.handoff)
            develop_and_review(request, args.source, args.revision, args.output)
            print("Development and independent review reports are ready in the selected output directory.")
    except KeyboardInterrupt:
        return 0
    except ValueError as exc:
        # Data decoder errors may contain the relay's raw content.
        print(str(exc) if type(exc) in (ValueError, QueueLimit) else "Invalid relay response.", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError:
        print("The local development or review step failed. Review the staged output logs.", file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError, http.client.HTTPException):
        print("The owner queue, private token file, or relay could not be accessed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
