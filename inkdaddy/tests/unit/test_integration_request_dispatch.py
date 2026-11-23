import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
from threading import Barrier

import pytest


REVISION = "a" * 40


@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "tools"))
    import integration_request_dispatch as dispatcher
    from inkdaddy_hub.services.integration_request_protocol import request_data

    queue = tmp_path / "private" / "review.sqlite"
    source = tmp_path / "source"
    source.mkdir()
    request = request_data("Indoor Air Quality")
    dispatcher.bridge.add_requests(queue, [request], rate_limit=False)
    # The CLI runs dispatch() in its own OS session. Use a synthetic reviewer
    # in the test process so no model service or production credential is needed.
    monkeypatch.setattr(dispatcher.os, "getsid", lambda _: os.getpid())
    monkeypatch.setattr(dispatcher.os, "getpgrp", os.getpid)
    real_group_exists = dispatcher.group_exists
    monkeypatch.setattr(dispatcher, "group_exists", lambda group: group == os.getpid() or real_group_exists(group))
    return dispatcher, queue, source, request


def write_reports(manifest, source, revision, output):
    assert manifest["task"] == "assess_integration_compatibility"
    output.mkdir()
    (output / "development.txt").write_text("Fixture development report")
    (output / "review.txt").write_text("Fixture review report")
    (output / "review").mkdir()
    (output / "review" / "proposed.patch").write_text("")


def test_only_approved_items_are_dispatched_once_at_pinned_revision(owner, monkeypatch):
    dispatcher, queue, source, request = owner
    monkeypatch.setattr(dispatcher, "develop_and_review", write_reports)
    dispatcher.dispatch(queue, source, REVISION, None)
    dispatcher.bridge.decide(queue, request.integration_id, "declined")
    dispatcher.dispatch(queue, source, REVISION, None)
    assert dispatcher.status(queue, REVISION) == []
    dispatcher.bridge.decide(queue, request.integration_id, "approved")
    dispatcher.dispatch(queue, source, REVISION, None)
    dispatcher.dispatch(queue, source, REVISION, None)
    job, = dispatcher.status(queue, REVISION)
    assert (job["integration_id"], job["attempt"], job["status"]) == (request.integration_id, 1, "reviewed")
    assert (queue.parent / job["directory"] / "reports" / "review.txt").is_file()
    dispatcher.dispatch(queue, source, "b" * 40, None)
    assert dispatcher.status(queue, "b" * 40) == []
    with pytest.raises(ValueError, match="Retry requires"):
        dispatcher.dispatch(queue, source, REVISION, request.integration_id)


def test_concurrent_ticks_cannot_claim_the_same_request(owner):
    dispatcher, queue, _source, request = owner
    dispatcher.bridge.decide(queue, request.integration_id, "approved")
    barrier = Barrier(2)

    def tick():
        barrier.wait()
        return dispatcher.claim(queue, REVISION, None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [executor.submit(tick) for _ in range(2)]
        assert sum(job.result() is not None for job in jobs) == 1


def test_retry_keeps_previous_attempt_and_rechecks_owner_decision(owner, monkeypatch):
    dispatcher, queue, source, request = owner
    dispatcher.bridge.decide(queue, request.integration_id, "approved")

    def fail(*args):
        raise RuntimeError("Fixture review failed")

    monkeypatch.setattr(dispatcher, "develop_and_review", fail)
    with pytest.raises(RuntimeError, match="Fixture"):
        dispatcher.dispatch(queue, source, REVISION, None)
    dispatcher.dispatch(queue, source, REVISION, None)
    assert [row["status"] for row in dispatcher.status(queue, REVISION)] == ["failed"]
    dispatcher.bridge.decide(queue, request.integration_id, "declined")
    with pytest.raises(ValueError, match="Retry requires"):
        dispatcher.dispatch(queue, source, REVISION, request.integration_id)
    dispatcher.bridge.decide(queue, request.integration_id, "approved")
    monkeypatch.setattr(dispatcher, "develop_and_review", write_reports)
    dispatcher.dispatch(queue, source, REVISION, request.integration_id)
    first, second = dispatcher.status(queue, REVISION)
    assert (first["status"], second["status"], second["attempt"]) == ("failed", "reviewed", 2)
    assert first["directory"] != second["directory"]
    assert (queue.parent / first["directory"] / "integration-request.json").is_file()


@pytest.mark.parametrize("original_status", ["running", "failed"])
def test_interrupted_worker_waits_for_agents_and_requires_explicit_retry(owner, monkeypatch, original_status):
    dispatcher, queue, source, request = owner
    dispatcher.bridge.decide(queue, request.integration_id, "approved")
    job = dispatcher.claim(queue, REVISION, None)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        with dispatcher.bridge.open_queue(queue) as db:
            db.execute("UPDATE integration_dispatch SET process_group=?, status=? WHERE rowid=?",
                       (child.pid, original_status, job["rowid"]))
        assert dispatcher.claim(queue, REVISION, request.integration_id) is None
        assert dispatcher.claim(queue, "b" * 40, None) is None
        assert dispatcher.status(queue, REVISION)[0]["status"] == original_status
    finally:
        child.terminate()
        child.wait(timeout=5)
    expected_status = "interrupted" if original_status == "running" else "failed"
    assert dispatcher.status(queue, REVISION)[0]["status"] == expected_status
    assert dispatcher.claim(queue, "b" * 40, None) is None
    monkeypatch.setattr(dispatcher, "develop_and_review", write_reports)
    dispatcher.dispatch(queue, source, "b" * 40, request.integration_id)
    assert [row["status"] for row in dispatcher.status(queue, REVISION)] == [expected_status]
    assert [row["status"] for row in dispatcher.status(queue, "b" * 40)] == ["reviewed"]


def test_dispatch_output_must_be_outside_the_approved_source(owner):
    dispatcher, queue, _source, _request = owner
    with pytest.raises(ValueError, match="outside"):
        dispatcher.dispatch(queue, queue.parent, REVISION, None)


def test_reboot_releases_a_stale_process_group_without_retrying(owner, monkeypatch):
    dispatcher, queue, source, request = owner
    dispatcher.bridge.decide(queue, request.integration_id, "approved")
    monkeypatch.setattr(dispatcher, "boot_id", lambda: "first-boot")
    assert dispatcher.claim(queue, REVISION, None)
    monkeypatch.setattr(dispatcher, "boot_id", lambda: "next-boot")
    assert dispatcher.status(queue, REVISION)[0]["status"] == "interrupted"
    assert dispatcher.claim(queue, REVISION, None) is None
    monkeypatch.setattr(dispatcher, "develop_and_review", write_reports)
    dispatcher.dispatch(queue, source, REVISION, request.integration_id)
    assert [row["status"] for row in dispatcher.status(queue, REVISION)] == ["interrupted", "reviewed"]


def test_missing_model_report_does_not_complete_review(owner, monkeypatch):
    dispatcher, queue, source, request = owner
    dispatcher.bridge.decide(queue, request.integration_id, "approved")
    monkeypatch.setattr(dispatcher, "develop_and_review", lambda *_: None)
    with pytest.raises(ValueError, match="both development and review reports"):
        dispatcher.dispatch(queue, source, REVISION, None)
    assert dispatcher.status(queue, REVISION)[0]["status"] == "failed"


def test_unattended_mode_runs_one_shot_without_retrying_failed_work(owner, monkeypatch):
    dispatcher, queue, source, _request = owner
    args = ["--db", str(queue), "--source", str(source), "--revision", REVISION]
    monkeypatch.setattr(sys, "argv", ["dispatch.py", *args, "--watch-seconds", "300"])
    calls = []

    def run(command, *, start_new_session):
        calls.append(command)
        assert start_new_session
        return 1

    def wait(seconds):
        assert seconds == 300
        raise KeyboardInterrupt

    monkeypatch.setattr(dispatcher.subprocess, "call", run)
    monkeypatch.setattr(dispatcher, "sleep", wait)
    old_umask = os.umask(0o077)
    try:
        assert dispatcher.main() == 130
    finally:
        os.umask(old_umask)
    assert [command[2:] for command in calls] == [args]
