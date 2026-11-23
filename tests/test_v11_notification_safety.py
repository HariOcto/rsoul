"""Bounded webhook delivery and durable unresolved-submission notification regressions."""

import configparser
import copy
import json
import logging
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from rsoul import import_transaction, notify, postprocess
from rsoul.persistence import StatePersistenceError
from rsoul.state import StateManager
from test_v11_imports import run, saved_pending, setup, writer


class Response:
    def __init__(self, status=204):
        self.status_code = status
        self.closed = False

    @property
    def content(self):
        raise AssertionError("A notification must never buffer the response body")

    def close(self):
        self.closed = True


def configuration(url="https://hooks.example/FAKE_SECRET", events="needs_review,submission_unknown,persistence_error"):
    config = configparser.ConfigParser(interpolation=None)
    config["Notifications"] = {"webhook_url": url, "events": events}
    return config


@pytest.fixture
def deliveries(monkeypatch):
    calls = []

    def transport(url, **kwargs):
        response = Response()
        calls.append((url, kwargs, response))
        return response

    dispatcher = notify._Dispatcher(transport=transport)
    monkeypatch.setattr(notify, "_dispatcher", dispatcher)
    notify.configure(configuration())
    yield calls
    notify.configure(None)
    assert dispatcher.shutdown(1)


def test_hung_delivery_does_not_block_checkpointing_and_queue_stays_bounded(monkeypatch):
    started, release = threading.Event(), threading.Event()
    requests = []

    def blocked(url, **kwargs):
        requests.append((threading.current_thread(), url))
        started.set()
        release.wait(5)
        return Response()

    dispatcher = notify._Dispatcher(transport=blocked, capacity=2)
    monkeypatch.setattr(notify, "_dispatcher", dispatcher)
    notify.configure(configuration())
    saved = []
    record = {"phase": "staged", "book_id": 42, "intended_identity": {"title": "Book", "author_name": "Author"}}
    try:
        began = time.monotonic()
        import_transaction._retain(record, "needs_review", "Review this import", persist=lambda item: saved.append(copy.deepcopy(item)))
        assert time.monotonic() - began < 0.5
        assert started.wait(1)
        assert saved[-1]["phase"] == "needs_review"
        assert not release.is_set()
        first_worker = requests[0][0]
        assert first_worker.daemon
        assert notify.send("needs_review", "Queued 1", "message")
        assert notify.send("needs_review", "Queued 2", "message")
        assert not notify.send("needs_review", "Queue full", "message")
        assert len(dispatcher._queue) == 2

        # Reconfiguration discards pending notices and reuses the same blocked worker.
        notify.configure(None)
        notify.configure(configuration("https://hooks.example/NEW_FAKE_SECRET"))
        assert notify.send("needs_review", "New configuration", "message")
        assert dispatcher._thread is first_worker
        assert len(requests) == 1
        began = time.monotonic()
        assert not notify.shutdown(0.02)
        assert time.monotonic() - began < 0.5
        assert not dispatcher._queue
        assert not notify.send("needs_review", "After shutdown", "message")
    finally:
        release.set()
        assert dispatcher.shutdown(1)
        notify.configure(None)
    assert len(requests) == 1  # no delayed call against either configuration


def test_request_and_payload_are_bounded_and_response_is_closed(deliveries):
    assert notify.send("needs_review", "T" * 10000, "\U0001f642" * 10000)
    assert notify.flush(1)
    url, request, response = deliveries[0]
    assert request["timeout"] == 3
    assert request["stream"] is True and request["allow_redirects"] is False
    assert response.closed
    payload = request["json"]
    assert len(payload["content"]) <= 1900
    assert len(payload["body"]) == notify.MAX_MESSAGE_LENGTH
    assert len(payload["title"]) == notify.MAX_TITLE_LENGTH + len("R:soul: ")
    assert len(json.dumps(payload).encode()) < 128 * 1024
    notify.configure(configuration("https://hooks.example/" + "x" * notify.MAX_URL_LENGTH))
    assert not notify.send("needs_review", "too long", "message")
    assert len(deliveries) == 1


def test_stuck_worker_cannot_hold_the_python_process_open():
    # A process boundary catches executors/threads that silently join at interpreter exit.
    # The transport never returns and never opens a connection.
    script = """
import configparser
import threading
from rsoul import notify
started = threading.Event()
def stuck(*args, **kwargs):
    started.set()
    threading.Event().wait()
notify._dispatcher = notify._Dispatcher(transport=stuck)
config = configparser.ConfigParser()
config['Notifications'] = {'webhook_url': 'https://hooks.example/FAKE_SECRET'}
notify.configure(config)
assert notify.send('needs_review', 'Book', 'Review')
assert started.wait(1)
assert notify.shutdown(0.02) is False
print('bounded shutdown reached')
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=3, check=True)
    assert result.stdout.strip() == "bounded shutdown reached"


@pytest.mark.parametrize("failure", [ConnectionError("FAKE_SECRET in a connection error"), 500, 302])
def test_enqueue_success_is_separate_from_delivery_failure(monkeypatch, caplog, failure):
    responses = []

    def transport(url, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        response = Response(failure)
        responses.append(response)
        return response

    dispatcher = notify._Dispatcher(transport=transport)
    monkeypatch.setattr(notify, "_dispatcher", dispatcher)
    notify.configure(configuration())
    try:
        assert notify.send("needs_review", "Book", "Review") is True
        assert notify.flush(1)
        assert "FAKE_SECRET" not in caplog.text
        assert "could not be delivered" in caplog.text or "was refused" in caplog.text
        assert all(response.closed for response in responses)
    finally:
        notify.configure(None)
        assert dispatcher.shutdown(1)


@pytest.mark.parametrize("status", [200, 302])
def test_streamed_http_ignores_a_stalled_body_and_keeps_webhook_secret_out_of_debug_logs(monkeypatch, caplog, status):
    body_release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(status)
            self.send_header("Content-Length", "10000")
            if status == 302:
                self.send_header("Location", "/never-follow-this")
            self.end_headers()
            self.wfile.flush()
            # Deliberately never send a body before the assertion below. stream=False would
            # block the delivery waiting for it, even though the useful headers arrived.
            body_release.wait(5)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    server_thread.start()
    dispatcher = notify._Dispatcher()
    monkeypatch.setattr(notify, "_dispatcher", dispatcher)
    secret = "FAKE_WEBHOOK_BEARER_SECRET"
    try:
        with caplog.at_level(logging.DEBUG):
            notify.configure(configuration(f"http://127.0.0.1:{server.server_port}/api/webhooks/123/{secret}"))
            assert notify.send("needs_review", "Book", "Review")
            assert notify.flush(1)
            assert not body_release.is_set()
            logging.getLogger("rsoul.import_transaction").debug("Ordinary application debugging remains visible")
            logging.getLogger("urllib3.connectionpool").debug("Unrelated HTTP debugging remains visible")
        assert secret not in caplog.text
        assert ("was delivered (HTTP 200)" if status == 200 else "was refused by the webhook (HTTP 302)") in caplog.text
        assert "Ordinary application debugging remains visible" in caplog.text
        assert "Unrelated HTTP debugging remains visible" in caplog.text
    finally:
        notify.configure(None)
        body_release.set()
        assert dispatcher.shutdown(1)
        server.shutdown()
        server.server_close()
        server_thread.join(1)


@pytest.mark.parametrize("receipt", ["lost", None, {}, {"id": ""}, {"id": False}, {"id": 0}, {"id": []}])
def test_missing_receipt_is_announced_once_after_durable_post_intent_and_never_resubmitted(tmp_path, deliveries, receipt):
    ctx, item, server, _ = setup(tmp_path)
    server.action = "none"
    original_post = server.post_command

    def before_post(name, payload):
        disk = json.loads(ctx.state.filepath.read_text())[0]["extra"]["import_pending"]
        assert disk["phase"] == "submission_unknown"
        assert disk["before_ids"] == []
        assert disk["attempts"][0]["payload"]["name"] == name
        assert "submission_unknown_notice_recorded_at" not in disk["attempts"][0]
        assert not deliveries

    def uncertain_post(**payload):
        original_post(**payload)
        if receipt == "lost":
            raise TimeoutError("accepted but response lost")
        return receipt

    server.before_post = before_post
    server.post_command = uncertain_post
    pending = run(ctx, item)
    assert pending["phase"] == "submission_unknown"
    assert notify.flush(1)
    assert len(deliveries) == 1
    assert deliveries[0][1]["json"]["event"] == "submission_unknown"
    assert saved_pending(ctx)["attempts"][0]["submission_unknown_notice_recorded_at"]

    # Load the durable marker as a fresh process would, then revisit the unresolved attempt.
    ctx.state = StateManager(str(ctx.state.filepath.parent))
    pending = saved_pending(ctx)
    for _ in range(3):
        pending = postprocess.check_pending_import(server, pending, config=ctx.config, persist=writer(ctx))
    assert notify.flush(1)
    assert len(deliveries) == 1
    assert len(server.posts) == 1


def test_valid_receipt_does_not_announce_the_pre_post_safety_phase(tmp_path, deliveries):
    ctx, item, server, _ = setup(tmp_path)
    assert run(ctx, item) is True
    assert notify.flush(1)
    assert not deliveries
    assert len(server.posts) == 1
    assert "submission_unknown_notice_recorded_at" not in saved_pending(ctx)["attempts"][0]


def test_notification_marker_must_be_durable_before_an_unknown_notice(tmp_path, deliveries, monkeypatch):
    ctx, item, server, _ = setup(tmp_path)
    server.action, server.lose_response = "none", True
    original_save = ctx.state.set_extra

    def fail_notification_checkpoint(task_id, extra):
        attempts = extra["import_pending"].get("attempts") or []
        if attempts and attempts[-1].get("submission_unknown_notice_recorded_at"):
            raise StatePersistenceError("cannot checkpoint the unresolved response")
        return original_save(task_id, extra)

    monkeypatch.setattr(ctx.state, "set_extra", fail_notification_checkpoint)
    with pytest.raises(StatePersistenceError):
        run(ctx, item)
    assert notify.flush(1)
    assert not deliveries
    assert len(server.posts) == 1
    disk = saved_pending(ctx)
    assert disk["phase"] == "submission_unknown"
    assert "submission_unknown_notice_recorded_at" not in disk["attempts"][0]


def test_reconciliation_announces_an_unresolved_intent_after_a_crash(tmp_path, deliveries):
    ctx, item, server, _ = setup(tmp_path)
    server.action = "none"

    def crash_after_intent(*args):
        raise KeyboardInterrupt("process stopped after intent checkpoint")

    server.before_post = crash_after_intent
    with pytest.raises(KeyboardInterrupt):
        run(ctx, item)
    assert not deliveries and not server.posts
    ctx.state = StateManager(str(ctx.state.filepath.parent))
    outcome = postprocess.check_pending_import(server, saved_pending(ctx), config=ctx.config, persist=writer(ctx))
    assert outcome["phase"] == "submission_unknown"
    assert notify.flush(1)
    assert len(deliveries) == 1 and not server.posts


def test_disabled_sync_still_retains_files_without_submission_or_unknown_notice(tmp_path, deliveries):
    ctx, item, server, _ = setup(tmp_path)
    ctx.config.set("Readarr", "disable_sync", "True")
    outcome = run(ctx, item)
    assert outcome["phase"] == "validating"
    assert all((server.root / "peer-folder" / name).is_file() for name in item["expected_files"])
    assert not server.posts
    assert notify.flush(1) and not deliveries


@pytest.mark.parametrize("entry_point", ["check_pending", "recover_staging", "submit_prepared"])
def test_operator_discard_marker_blocks_normal_import_reconciliation(entry_point):
    class NoClientCalls:
        def __getattr__(self, name):
            raise AssertionError("Operator discard must not call the import API")

    pending = {"phase": "staging", "operator_discard": "discard-operation", "manifest": []}
    def no_checkpoint(value):
        raise AssertionError("The discard journal owns this operation")
    if entry_point == "recover_staging":
        result = import_transaction.recover_staging(pending, persist=no_checkpoint)
    else:
        result = getattr(import_transaction, entry_point)(NoClientCalls(), pending, persist=no_checkpoint)
    assert result["phase"] == "needs_review" and "--discard" in result["reason"]
    if entry_point == "check_pending":
        assert pending["phase"] == "staging"  # read-back preserves the caller's snapshot
