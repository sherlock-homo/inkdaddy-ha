#!/usr/bin/env python3
"""Run one approved integration through local development and independent review."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
from time import sleep

import integration_request_bridge as bridge
from integration_request_review import develop_and_review


ATTEMPTS = """
    CREATE TABLE IF NOT EXISTS integration_dispatch (
        integration_id TEXT NOT NULL, revision TEXT NOT NULL, attempt INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('running', 'interrupted', 'failed', 'reviewed')),
        process_group INTEGER, boot_id TEXT, directory TEXT NOT NULL,
        PRIMARY KEY (integration_id, revision, attempt)
    )
"""


def group_exists(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def boot_id() -> str | None:
    try:
        if sys.platform == "darwin":
            return subprocess.check_output(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"], text=True).strip()
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def claim(queue: Path, revision: str, retry: str | None, *, session_leader: int | None = None) -> dict | None:
    boot = boot_id()
    with bridge.open_queue(queue) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(ATTEMPTS)
        # The CLI and its agent children share a dedicated process group. If the
        # dispatcher exits first, an explicit retry still waits for those children.
        tracked = db.execute("""
            SELECT rowid, process_group, boot_id, status FROM integration_dispatch WHERE process_group IS NOT NULL
        """).fetchall()
        for old in tracked:
            # A fresh session could not have acquired the id of a live group.
            same_boot = not boot or not old["boot_id"] or old["boot_id"] == boot
            if same_boot and old["process_group"] != session_leader and group_exists(old["process_group"]):
                print("A development or review process is still running.")
                return None
            state = "interrupted" if old["status"] == "running" else old["status"]
            db.execute("UPDATE integration_dispatch SET status=?, process_group=NULL WHERE rowid=?", (state, old["rowid"]))
        if retry:
            item = db.execute("""
                SELECT r.integration_id, r.integration_name, a.status, a.attempt
                FROM integration_requests r JOIN integration_dispatch a USING (integration_id)
                WHERE r.decision='approved' AND r.integration_id=?
                ORDER BY a.rowid DESC LIMIT 1
            """, (retry,)).fetchone()
            if item is None or item["status"] not in ("interrupted", "failed"):
                raise ValueError("Retry requires an approved request whose last attempt failed or was interrupted.")
            attempt = item["attempt"] + 1
        else:
            item = db.execute("""
                SELECT r.integration_id, r.integration_name FROM integration_requests r
                WHERE r.decision='approved' AND NOT EXISTS (
                    SELECT 1 FROM integration_dispatch a WHERE a.integration_id=r.integration_id
                ) ORDER BY r.received_at, r.integration_id LIMIT 1
            """).fetchone()
            if item is None:
                print("No new approved request for this revision. Use --status to inspect earlier attempts.")
                return None
            attempt = 1

        request = bridge.parse_request({key: item[key] for key in ("integration_id", "integration_name")})
        directory = f"run-{request.integration_id}-{revision[:12]}-{attempt}-{secrets.token_hex(6)}"
        cursor = db.execute("INSERT INTO integration_dispatch VALUES (?, ?, ?, 'running', ?, ?, ?)", (
            request.integration_id, revision, attempt, os.getpgrp(), boot, directory,
        ))
        return {"rowid": cursor.lastrowid, "id": request.integration_id, "directory": directory}


def status(queue: Path, revision: str) -> list[dict]:
    boot = boot_id()
    with bridge.open_queue(queue) as db:
        db.execute(ATTEMPTS)
        rows = db.execute("""
            SELECT integration_id, attempt, status, process_group, boot_id, directory
            FROM integration_dispatch WHERE revision=? ORDER BY integration_id, attempt
        """, (revision,))
        results = []
        for row in rows:
            item = dict(row)
            group = item.pop("process_group")
            prior_boot = item.pop("boot_id")
            rebooted = boot and prior_boot and boot != prior_boot
            if item["status"] == "running" and (group is None or rebooted or not group_exists(group)):
                item["status"] = "interrupted"
            results.append(item)
        return results


def dispatch(queue: Path, source: Path, revision: str, retry: str | None) -> None:
    if os.getsid(0) != os.getpid():
        raise ValueError("Dispatch requires its own process session.")
    source = source.resolve(strict=True)
    if queue.resolve().is_relative_to(source):
        raise ValueError("Keep the private queue and output outside the source checkout.")
    job = claim(queue, revision, retry, session_leader=os.getpid())
    if job is None:
        return
    destination = queue.parent / job["directory"]
    result = "failed"
    try:
        destination.mkdir(mode=0o700)
        handoff = destination / "integration-request.json"
        bridge.prepare_handoff(queue, job["id"], handoff)
        manifest = bridge.read_approved_handoff(queue, handoff)
        reports = destination / "reports"
        develop_and_review(manifest, source, revision, reports)
        for report in ("development.txt", "review.txt"):
            path = reports / report
            if not path.is_file() or not path.stat().st_size:
                raise ValueError("The agent run did not produce both development and review reports.")
        if not (reports / "review" / "proposed.patch").is_file():
            raise ValueError("The agent run did not retain the reviewed patch.")
        result = "reviewed"
    finally:
        with bridge.open_queue(queue) as db:
            db.execute("UPDATE integration_dispatch SET status=? WHERE rowid=?", (result, job["rowid"]))
    print(f"Independent review finished. Reports are in {destination / 'reports'}.")


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="Worker's private review queue.")
    parser.add_argument("--source", type=Path, help="Approved local source checkout.")
    parser.add_argument("--revision", required=True, help="Full approved Git commit ID.")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_true", help="List attempts at this revision.")
    actions.add_argument("--retry", metavar="REQUEST_ID", help="Start a fresh attempt after a failure or interruption.")
    actions.add_argument("--watch-seconds", type=int, help="Check for new owner-approved work every 60–3600 seconds.")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args.revision):
        parser.error("Pass the full approved Git commit ID.")
    if args.retry is not None and not bridge.INTEGRATION_ID_PATTERN.fullmatch(args.retry):
        parser.error("Pass a valid request identifier to retry.")
    if not args.status and args.source is None:
        parser.error("Dispatch requires --source.")
    if args.watch_seconds is not None and not 60 <= args.watch_seconds <= 3600:
        parser.error("Choose a watch interval from 60 to 3600 seconds.")
    try:
        if args.status:
            print(json.dumps(status(args.db, args.revision), indent=2))
        elif args.watch_seconds is not None:
            command = [sys.executable, str(Path(__file__).resolve()), "--db", str(args.db),
                       "--source", str(args.source), "--revision", args.revision]
            while True:
                code = subprocess.call(command, start_new_session=True)
                if code:
                    print("Dispatch ended with an error. Check --status before retrying that request.", file=sys.stderr)
                sleep(args.watch_seconds)
        elif os.getsid(0) != os.getpid():
            # Neither the owner's terminal nor other scheduled jobs belong to
            # this session. An orphaned agent remains identifiable after a crash.
            return subprocess.call([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
                                   start_new_session=True)
        else:
            dispatch(args.db, args.source, args.revision, args.retry)
    except KeyboardInterrupt:
        return 130
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError, subprocess.CalledProcessError):
        print("Dispatch failed. Inspect --status and the private attempt directory.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
