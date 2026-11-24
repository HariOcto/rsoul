"""Exercise submission/poll coordination through real completion callbacks and state."""

import configparser
import copy
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from rsoul import postprocess
from rsoul.backends import DownloadTask, DownloadStatus
from rsoul.persistence import StatePersistenceError
from rsoul.state import StateManager
from rsoul.workflow import _RunResults, _follow_up_pending_imports, _resume_persisted
from test_import_transaction import audio


class ImportServer:
    ver_uri = "/api/v1"

    def __init__(self, root, state):
        self.root, self.state = root, state
        self.lock = threading.RLock()
        self.posts, self.inputs, self.statuses = [], {}, {}
        self.bookfiles, self.events = {1: [], 2: []}, {1: [], 2: []}
        self.checked = {1: threading.Event(), 2: threading.Event()}
        self.check_counts = {1: 0, 2: 0}
        self.submitted = {1: threading.Event(), 2: threading.Event()}
        self.auto_complete = {2}
        self.lose_response = set()
        self.threads = set()

    def get_book(self, book_id):
        return {"id": book_id, "authorId": 7, "title": f"Book {book_id}", "mediaType": "audiobook"}

    def get_author(self, author_id):
        return {"id": 7, "authorName": "Correct Author"}

    def get_edition(self, book_id):
        return [{"id": book_id * 10, "readingFormatId": 2, "monitored": True}]

    def local(self, path):
        return self.root / path.removeprefix("/remote/")

    def _get(self, resource, ver_uri=None, params=None):
        with self.lock:
            if resource == "manualimport":
                return [{"path": params["folder"] + "/" + path.name,
                         "quality": {"quality": {"id": 1, "name": "Audio"}}}
                        for path in self.local(params["folder"]).iterdir() if path.is_file()]
            book_id = params["bookId"]
            if resource == "bookfile":
                return copy.deepcopy(self.bookfiles[book_id])
            if resource == "history":
                return {"records": copy.deepcopy(self.events[book_id]), "totalRecords": len(self.events[book_id])}
        raise AssertionError(resource)

    def post_command(self, name, **payload):
        with self.lock:
            self.threads.add(threading.get_ident())
            assert name == "ManualImport"
            book_id = payload["files"][0]["bookId"]
            saved = next(item for item in self.state.get_items() if item["book_id"] == book_id)
            assert saved["extra"]["import_pending"]["attempts"]  # intent predates the POST
            self.posts.append(book_id)
            self.inputs[book_id] = [item["path"] for item in payload["files"]]
            self.statuses[book_id] = "started"
            if book_id in self.auto_complete:
                self.complete(book_id)
            self.submitted[book_id].set()
            if book_id in self.lose_response:
                raise TimeoutError("accepted, but receipt lost")
            return {"id": 1000 + book_id, "name": name}

    def get_command(self, command_id):
        with self.lock:
            self.threads.add(threading.get_ident())
            book_id = command_id - 1000
            self.check_counts[book_id] += 1
            self.checked[book_id].set()
            return {"id": command_id, "status": self.statuses[book_id], "result": "successful"}

    def complete(self, book_id):
        with self.lock:
            if book_id not in self.inputs or self.bookfiles[book_id]:
                return
            for source in self.inputs[book_id]:
                size = self.local(source).stat().st_size
                file_id = book_id * 100 + len(self.bookfiles[book_id])
                output = f"/server-only/library/{file_id}.m4b"
                self.bookfiles[book_id].append({"id": file_id, "bookId": book_id, "authorId": 7,
                    "editionId": book_id * 10, "mediaType": "audiobook", "path": output, "size": size})
                self.events[book_id].append({"bookId": book_id, "authorId": 7,
                    "date": datetime.now(timezone.utc).isoformat(), "eventType": "bookFileImported",
                    "data": {"ImportedPath": output, "DroppedPath": source, "FileId": str(file_id), "Size": str(size)}})
                self.local(source).unlink()
            self.statuses[book_id] = "completed"


def environment(tmp_path, timeout=0.2):
    root = tmp_path / "downloads"
    root.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state = StateManager(str(state_dir))
    config = configparser.ConfigParser()
    config.read_dict({"Slskd": {"download_dir": str(root), "readarr_download_dir": "/remote"},
                      "Readarr": {"import_poll_timeout": str(timeout)},
                      "Postprocessing": {"audiobook_tag_check": "off"}})
    tasks = []
    for book_id in (1, 2):
        peer = root / f"peer-{book_id}"
        peer.mkdir()
        filename = f"chapter{book_id}.wav"
        audio(peer / filename, book_id)
        task = DownloadTask(f"task-{book_id}", "slskd", DownloadStatus.COMPLETED, f"Book {book_id}",
                            "Correct Author", book_id, filename, local_dir=peer.name,
                            extra={"media_type": "audiobook", "expected_files": [filename]})
        state.add_task(task)
        tasks.append(task)
    server = ImportServer(root, state)
    ctx = SimpleNamespace(config=config, config_dir=str(state_dir), state=state, readarr=server, orchestrator=None)
    return ctx, tasks, server


def test_later_completed_book_submits_while_first_command_is_still_active(tmp_path):
    ctx, tasks, server = environment(tmp_path)
    result = _RunResults(ctx, [])
    try:
        result.on_complete(tasks[0])
        assert server.checked[1].wait(2), "first command was not checked"
        assert server.statuses[1] == "started"
        result.on_complete(tasks[1])
        assert server.submitted[2].wait(2), "second submission was blocked by the first poll"
        assert server.statuses[1] == "started"
        assert server.posts == [1, 2]
        server.complete(1)
    finally:
        result.wait_for_imports()
    assert {task.book_id for task in result.completed_tasks} == {1, 2}
    assert len(server.threads) == 1  # submission and checks use the same worker
    assert not ctx.state.get_items()


def test_each_active_command_keeps_its_original_timeout_and_journal(tmp_path):
    ctx, tasks, server = environment(tmp_path, timeout=0.05)
    server.auto_complete.clear()
    result = _RunResults(ctx, [])
    started = time.monotonic()
    for task in tasks:
        result.on_complete(task)
    result.wait_for_imports()
    assert time.monotonic() - started < 2
    assert server.posts == [1, 2]
    assert not result.completed_tasks
    saved = StateManager(str(ctx.state.filepath.parent)).get_items()
    assert len(saved) == 2
    assert all(item["extra"]["import_pending"]["phase"] == "importing" for item in saved)
    assert all(Path(path).exists() for item in saved for path in item["extra"]["import_pending"]["files"])
    assert len(result.pending_imports) == 2


def test_zero_timeout_still_checks_accepted_commands_once(tmp_path):
    ctx, tasks, server = environment(tmp_path, timeout=0)
    server.auto_complete = {1, 2}
    result = _RunResults(ctx, [])
    for task in tasks:
        result.on_complete(task)
    result.wait_for_imports()
    assert {task.book_id for task in result.completed_tasks} == {1, 2}
    assert all(event.is_set() for event in server.checked.values())


def test_resumed_active_imports_receive_one_nonwaiting_check_at_startup(tmp_path):
    ctx, tasks, server = environment(tmp_path, timeout=0)
    server.auto_complete.clear()
    first_run = _RunResults(ctx, [])
    for task in tasks:
        first_run.on_complete(task)
    first_run.wait_for_imports()
    before = dict(server.check_counts)
    ctx.config["Readarr"]["import_poll_timeout"] = "600"
    resumed = _RunResults(ctx, [])
    started = time.monotonic()
    try:
        assert _follow_up_pending_imports(ctx, resumed) == 2
    finally:
        resumed.wait_for_imports()
    assert time.monotonic() - started < 1
    assert server.check_counts == {book_id: count + 1 for book_id, count in before.items()}
    assert server.posts == [1, 2]
    assert len(ctx.state.get_items()) == 2


def test_disable_sync_preserves_downloads_and_never_submits_or_polls(tmp_path):
    ctx, tasks, server = environment(tmp_path)
    ctx.config["Readarr"]["disable_sync"] = "true"
    result = _RunResults(ctx, [])
    for task in tasks:
        result.on_complete(task)
    result.wait_for_imports()
    assert server.posts == [] and not any(event.is_set() for event in server.checked.values())
    saved = ctx.state.get_items()
    assert all(item["extra"]["import_pending"]["phase"] == "validating" for item in saved)
    assert all(Path(entry["source_path"]).exists() for item in saved for entry in item["extra"]["import_pending"]["manifest"])


def test_persistence_failure_stops_queued_work_and_retains_the_active_import(tmp_path, monkeypatch):
    ctx, tasks, server = environment(tmp_path)
    result = _RunResults(ctx, [])
    result.on_complete(tasks[0])
    assert server.checked[1].wait(2)
    original = ctx.state.set_extra
    def fail_second(task_id, extra):
        if task_id == "task-2":
            raise StatePersistenceError("second import checkpoint failed")
        return original(task_id, extra)
    monkeypatch.setattr(ctx.state, "set_extra", fail_second)
    result.on_complete(tasks[1])
    with pytest.raises(StatePersistenceError, match="second import checkpoint failed"):
        result.wait_for_imports()
    assert isinstance(ctx.fatal_state_error, StatePersistenceError)
    assert server.posts == [1]
    saved = {item["book_id"]: item for item in StateManager(str(ctx.state.filepath.parent)).get_items()}
    assert saved[1]["extra"]["import_pending"]["phase"] == "importing"
    assert saved[2]["extra"]["transfer_complete"] is True
    assert (server.root / tasks[1].local_dir / tasks[1].filename).exists()


def test_unexpected_poll_error_does_not_drop_journal_or_block_other_books(tmp_path, monkeypatch):
    ctx, tasks, server = environment(tmp_path)
    check = postprocess.check_pending_import
    def fail_first(client, pending, **kwargs):
        if pending["book_id"] == 1:
            raise RuntimeError("unexpected response shape")
        return check(client, pending, **kwargs)
    monkeypatch.setattr(postprocess, "check_pending_import", fail_first)
    result = _RunResults(ctx, [])
    for task in tasks:
        result.on_complete(task)
    result.wait_for_imports()
    assert server.posts == [1, 2]
    assert [task.book_id for task in result.completed_tasks] == [2]
    saved = ctx.state.get_items()
    assert len(saved) == 1 and saved[0]["book_id"] == 1
    assert saved[0]["extra"]["import_pending"]["command_id"] == 1001
    assert Path(saved[0]["extra"]["import_pending"]["files"][0]).exists()


def test_lost_receipt_does_not_resubmit_or_delay_other_books(tmp_path):
    ctx, tasks, server = environment(tmp_path)
    server.lose_response.add(1)
    result = _RunResults(ctx, [])
    for task in tasks:
        result.on_complete(task)
    result.on_complete(tasks[0])  # duplicate completion callback
    result.wait_for_imports()
    assert server.posts == [1, 2]
    assert [task.book_id for task in result.completed_tasks] == [2]
    saved = ctx.state.get_items()
    assert len(saved) == 1
    assert saved[0]["extra"]["import_pending"]["phase"] == "submission_unknown"
    assert len(saved[0]["extra"]["import_pending"]["attempts"]) == 1


def test_operator_discard_blocks_callbacks_followup_and_download_resume(tmp_path):
    ctx, tasks, server = environment(tmp_path)
    ctx.state.remove_task(tasks[1].task_id)
    tasks[0].extra.update(operator_discard={"phase": "incomplete", "files": []},
        import_pending={"phase": "needs_review", "local_dir": str(server.root / "peer-1"),
                        "operator_discard": "operation"})
    ctx.state.update_task(tasks[0])
    saved_bytes = ctx.state.filepath.read_bytes()
    def unexpected(*args, **kwargs):
        pytest.fail("operator discard must be resumed explicitly")
    ctx.orchestrator = SimpleNamespace(unchecked_task_ids=set(), resume_tasks=unexpected, get_backend=unexpected)
    result = _RunResults(ctx, [])
    result.on_complete(tasks[0])
    assert _follow_up_pending_imports(ctx, result) == 1
    assert _resume_persisted(ctx, result) == []
    result.wait_for_imports()
    assert server.posts == [] and ctx.state.filepath.read_bytes() == saved_bytes
