"""Regression tests for the issues found in the second review of the audiobook/timeout pull request.

Numbers refer to the review items. Fakes follow the slskd v0.26 response shapes (transfer
records carry "id", "filename", "size", "state", "bytesTransferred"; folder listings are a
list of {"name", "fileCount", "files"} with basenames).
"""

import configparser
import os
import sys
import threading

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rsoul import download as download_module
from rsoul import postprocess, workflow
from rsoul.backends.base import DownloadStatus, DownloadTarget, DownloadTask
from rsoul.backends.slskd_backend import SlskdBackend
from rsoul.config import Context
from rsoul.match import audiobook_folder_match, is_disc_folder
from rsoul.orchestrator import DownloadOrchestrator
from rsoul.state import StateManager

MB = 1024 * 1024
T0 = 1_000_000.0
AUTHOR = "Brandon Sanderson"
TITLE = "Mistborn: The Final Empire"


def make_config(slskd=None, search=None, download=None):
    config = configparser.ConfigParser(interpolation=None)
    config["Readarr"] = {"api_key": "x", "host_url": "http://chaptarr"}
    config["Slskd"] = {"api_key": "x", "host_url": "http://slskd", "download_dir": "/nonexistent", **(slskd or {})}
    config["Search Settings"] = {"minimum_filename_match_ratio": "0.7", **(search or {})}
    config["Download Settings"] = {**(download or {})}
    config["Backends"] = {"slskd_enabled": "False"}
    return config


def target(title=TITLE, author=AUTHOR, formats=("mp3",)):
    return DownloadTarget(
        book_id=7,
        book_title=title,
        author_name=author,
        series_title="",
        allowed_filetypes=list(formats),
        readarr_book={"title": title, "id": 7},
        readarr_author={"authorName": author},
        media_type="audiobook",
    )


def chapters(directory, count=20, ext="mp3", size=40 * MB):
    return [{"filename": f"{directory}\\{i:02d}.{ext}", "size": size} for i in range(1, count + 1)]


def match(directory, title=TITLE, author=AUTHOR, count=5):
    t = {"book": {"title": title}, "author": {"authorName": author}}
    return audiobook_folder_match(t, chapters(directory, count), "peer", ["mp3"], [], 0.7)


class Users:
    """Folder browsing: listings keyed by (username, directory); a missing key raises."""

    def __init__(self, listings=None):
        self.listings = listings or {}
        self.calls = []

    def directory(self, username, directory):
        self.calls.append((username, directory))
        files = self.listings[(username, directory)]
        return [{"name": directory, "fileCount": len(files), "files": [{"filename": f["filename"].rsplit("\\", 1)[1], "size": f["size"]} for f in files]}]


class Client:
    def __init__(self, users=None, transfers=None):
        self.users = users or Users()
        self.transfers = transfers


def backend(client=None, **config):
    return SlskdBackend(Context(config=make_config(**config), slskd=client or Client(), readarr=None))


def target_dict(t):
    return {"book": t.readarr_book, "author": t.readarr_author}


# ---------------------------------------------------------------------------
# B1. A folder that can't be listed in full is not downloaded
# ---------------------------------------------------------------------------


def test_unbrowsable_folder_is_skipped():
    d = "@@s\\Audiobooks\\Brandon Sanderson\\Mistborn - The Final Empire"
    b = backend(Client(users=Users({})))  # every browse raises KeyError
    results = b._match_audiobook_results(target(), target_dict(target()), [{"username": "peer", "files": chapters(d)[:6]}])
    assert results == []


def test_next_candidate_used_when_first_cannot_be_browsed():
    d = "@@s\\Audiobooks\\Brandon Sanderson\\Mistborn - The Final Empire"
    users = Users({("peer2", d): chapters(d)})
    b = backend(Client(users=users))
    responses = [{"username": "peer1", "files": chapters(d)}, {"username": "peer2", "files": chapters(d)}]
    results = b._match_audiobook_results(target(), target_dict(target()), responses)
    assert [r.username for r in results] == ["peer2"]
    assert len(results[0].extra["files"]) == 20


# ---------------------------------------------------------------------------
# B2. Disc/part subfolders are never picked
# ---------------------------------------------------------------------------


def test_disc_folder_detection():
    for name in ["CD1", "cd 02", "Disc 3", "Disk-1", "Part 2", "Teil 4", "Vol. 1", "Mistborn - CD1", "Book [Disc 2]"]:
        assert is_disc_folder(f"@@s\\Audiobooks\\{name}"), name
    for name in ["Mistborn - The Final Empire", "Part of the Deal", "The CD Collector", "Discworld"]:
        assert not is_disc_folder(f"@@s\\Audiobooks\\{name}"), name


def test_disc_subfolder_is_not_matched():
    assert match("@@s\\Audiobooks\\Brandon Sanderson - The Final Empire\\CD1", title="The Final Empire") == []


# ---------------------------------------------------------------------------
# B3. The author must be in the folder path
# ---------------------------------------------------------------------------


def test_plain_title_by_other_author_is_rejected():
    assert match("@@s\\Audiobooks\\Someone Else\\The Final Empire", title="The Final Empire") == []


def test_plain_title_without_author_is_rejected():
    assert match("@@s\\Audiobooks\\The Final Empire", title="The Final Empire") == []


def test_author_in_parent_folder_is_enough():
    assert match("@@s\\Audiobooks\\Brandon Sanderson\\The Final Empire", title="The Final Empire")


def test_author_suffix_is_not_taken_as_surname():
    assert match("@@s\\Audiobooks\\Martin Luther King\\Strength to Love", title="Strength to Love", author="Martin Luther King Jr.")


# ---------------------------------------------------------------------------
# B4. Resume follows the saved transfer IDs, not whichever record has the same name
# ---------------------------------------------------------------------------

D = "@@s\\Mistborn"


class ListTransfers:
    def __init__(self, records):
        self.records = records  # list of transfer records for user "peer" in directory D

    def get_all_downloads(self):
        return [{"username": "peer", "directories": [{"directory": D, "files": self.records}]}]

    def get_downloads(self, username):
        return {"directories": [{"directory": D, "files": self.records}]}


def rec(id_, state, name="01.mp3", done=0):
    return {"id": id_, "filename": f"{D}\\{name}", "size": 10, "state": state, "bytesTransferred": done}


def test_audiobook_resume_uses_saved_ids():
    records = [rec("new", "InProgress", done=5), rec("old", "Completed, Errored")]
    b = backend(Client(transfers=ListTransfers(records)))
    task = b.reconcile_task(
        {
            "task_id": "t",
            "book_title": "b",
            "author_name": "a",
            "book_id": 7,
            "filename": "01.mp3",
            "local_dir": "Mistborn",
            "extra": {"username": "peer", "file_dir": D, "media_type": "audiobook", "expected_files": ["01.mp3"], "files": [{"id": "new"}]},
        }
    )
    assert task.status == DownloadStatus.DOWNLOADING
    assert task.extra["files"][0]["id"] == "new"


def test_ebook_resume_uses_saved_id():
    records = [rec("old", "Completed, Errored", name="book.epub"), rec("new", "InProgress", name="book.epub", done=5)]
    b = backend(Client(transfers=ListTransfers(records)))
    task = b.reconcile_task(
        {
            "task_id": "t",
            "book_title": "b",
            "author_name": "a",
            "book_id": 7,
            "filename": "book.epub",
            "local_dir": "Mistborn",
            "extra": {"username": "peer", "file_dir": D, "files": [{"id": "new", "filename": f"{D}\\book.epub"}]},
        }
    )
    assert task.status == DownloadStatus.DOWNLOADING
    assert task.extra["slskd_id"] == "new"


def test_old_state_without_ids_prefers_active_record():
    records = [rec("new", "InProgress", done=5), rec("old", "Completed, Errored")]
    b = backend(Client(transfers=ListTransfers(records)))
    task = b.reconcile_task(
        {
            "task_id": "t",
            "book_title": "b",
            "author_name": "a",
            "book_id": 7,
            "filename": "01.mp3",
            "local_dir": "Mistborn",
            "extra": {"username": "peer", "file_dir": D, "media_type": "audiobook", "expected_files": ["01.mp3"]},
        }
    )
    assert task.status == DownloadStatus.DOWNLOADING


# ---------------------------------------------------------------------------
# B5. No download into a local folder that already has, or will get, files of the same name
# ---------------------------------------------------------------------------


class EnqueueTransfers(ListTransfers):
    def __init__(self, records=None):
        super().__init__(records or [])
        self.enqueued = []

    def enqueue(self, username, files):
        self.enqueued = files
        return True


def audiobook_result(directory=D, count=3):
    from rsoul.backends.base import SearchResult

    files = chapters(directory, count)
    return SearchResult(
        title=TITLE,
        author=AUTHOR,
        filename="01.mp3",
        size_bytes=0,
        extension="mp3",
        backend_name="slskd",
        source_id="x",
        username="peer",
        extra={"username": "peer", "file_dir": directory, "files": files, "media_type": "audiobook"},
    )


def test_download_refused_when_local_folder_has_same_names(tmp_path):
    (tmp_path / "Mistborn").mkdir()
    (tmp_path / "Mistborn" / "02.mp3").write_bytes(b"old")
    transfers = EnqueueTransfers()
    b = backend(Client(transfers=transfers), slskd={"download_dir": str(tmp_path)})
    assert b.download(target(), audiobook_result()) is None
    assert transfers.enqueued == []


def test_download_refused_when_another_peer_is_downloading_into_same_folder(tmp_path):
    other = [{"id": "x", "filename": "@@other\\Mistborn\\01.mp3", "size": 10, "state": "InProgress"}]

    class Transfers(EnqueueTransfers):
        def get_all_downloads(self):
            return [{"username": "someone", "directories": [{"directory": "@@other\\Mistborn", "files": other}]}]

    transfers = Transfers()
    b = backend(Client(transfers=transfers), slskd={"download_dir": str(tmp_path)})
    assert b.download(target(), audiobook_result()) is None
    assert transfers.enqueued == []


def test_failed_download_leftovers_are_moved_aside(tmp_path):
    folder = tmp_path / "Mistborn"
    folder.mkdir()
    (folder / "01.mp3").write_bytes(b"done")
    (folder / "unrelated.epub").write_bytes(b"other book")
    b = backend(slskd={"download_dir": str(tmp_path)})
    task = DownloadTask("t", "slskd", DownloadStatus.FAILED, TITLE, AUTHOR, 7, "01.mp3", local_dir="Mistborn")
    task.extra = {"expected_files": ["01.mp3", "02.mp3"]}

    b.discard(task)

    assert (tmp_path / "failed_downloads" / "Mistborn" / "01.mp3").exists()
    assert (folder / "unrelated.epub").exists()


def test_workflow_discards_on_failure(tmp_path):
    calls = []

    class Backend:
        name = "slskd"

        def discard(self, task):
            calls.append(task.task_id)

    class Orch:
        def get_backend(self, name):
            return Backend()

    ctx = Context(config=make_config(), slskd=None, readarr=None, config_dir=str(tmp_path), orchestrator=Orch())
    results = workflow._RunResults(ctx, [])
    results.on_complete(DownloadTask("t", "slskd", DownloadStatus.FAILED, TITLE, AUTHOR, 7, "01.mp3"))
    results.wait_for_imports()
    assert calls == ["t"]


# ---------------------------------------------------------------------------
# B6. A failed import moves only this book's files
# ---------------------------------------------------------------------------


def test_failed_audiobook_import_leaves_other_files(tmp_path):
    shared = tmp_path / "MP3"
    shared.mkdir()
    (shared / "01.mp3").write_bytes(b"x")
    (shared / "other-book.mp3").write_bytes(b"y")
    config = make_config(slskd={"download_dir": str(tmp_path)})
    ctx = Context(config=config, slskd=None, readarr=None)

    result = postprocess.process_imports(
        ctx,
        [{"author_name": "A", "title": "T", "bookId": 1, "dir": "MP3", "filename": "01.mp3", "expected_files": ["01.mp3", "02.mp3"], "backend_name": "slskd", "media_type": "audiobook"}],
    )

    assert result == {1: False}
    assert (tmp_path / "failed_imports" / "MP3" / "01.mp3").exists()
    assert (shared / "other-book.mp3").exists()


# ---------------------------------------------------------------------------
# B7. Progress reported only in percent (Stacks) still counts as progress
# ---------------------------------------------------------------------------


def test_percent_progress_is_not_a_stall():
    orch = DownloadOrchestrator([], Context(config=make_config(), slskd=None, readarr=None))
    task = DownloadTask("s", "stacks", DownloadStatus.DOWNLOADING, "b", "a", 1, "f")
    for minute in range(0, 90):
        task.progress_percent = minute * 1.0
        assert orch.check_timeouts(task, now=T0 + minute * 60) is None


# ---------------------------------------------------------------------------
# B8. Failed imports are counted as failed
# ---------------------------------------------------------------------------


def test_missing_chapter_counts_as_failed_import(tmp_path):
    (tmp_path / "Mistborn").mkdir()
    ctx = Context(config=make_config(slskd={"download_dir": str(tmp_path)}), slskd=None, readarr=None, config_dir=str(tmp_path))
    results = workflow._RunResults(ctx, [])
    done = DownloadTask("t", "slskd", DownloadStatus.COMPLETED, TITLE, AUTHOR, 7, "01.mp3", local_dir="Mistborn")
    done.extra = {"media_type": "audiobook", "expected_files": ["01.mp3"]}

    results.on_complete(done)
    results.wait_for_imports()

    assert results.completed_tasks == []
    assert results.failed_imports == [(AUTHOR, TITLE)]


def test_unsuccessful_import_command_counts_as_failed(tmp_path):
    local = tmp_path / "dl"
    (local / "Mistborn").mkdir(parents=True)
    (local / "Mistborn" / "01.mp3").write_bytes(b"x")

    class Readarr:
        def post_command(self, name, **kwargs):
            return {"id": 1, "body": {"path": kwargs["path"]}}

        def get_command(self, id_):
            # What Readarr/Chaptarr report when nothing could be imported
            return {"id": 1, "status": "completed", "result": "unsuccessful", "message": "", "body": {"path": "/elsewhere"}}

    ctx = Context(config=make_config(slskd={"download_dir": str(local)}), slskd=None, readarr=Readarr())
    item = {"author_name": AUTHOR, "title": TITLE, "bookId": 7, "dir": "Mistborn", "filename": "01.mp3", "expected_files": ["01.mp3"], "backend_name": "slskd", "media_type": "audiobook"}
    assert postprocess.process_imports(ctx, [item]) == {7: False}


def test_import_command_timeout(monkeypatch):
    class Readarr:
        def get_command(self, id_):
            return {"id": id_, "status": "started"}

    monkeypatch.setattr(postprocess.time, "sleep", lambda s: None)
    # Still running when the wait ends: left for the next run, not counted as failed
    assert postprocess.monitor_imports(Readarr(), [{"id": 1}], timeout=0) == {1: None}


# ---------------------------------------------------------------------------
# 9. max_active_downloads caps new downloads
# ---------------------------------------------------------------------------


class NoEditions:
    def get_edition(self, book_id):
        return []


class CountingOrchestrator:
    def __init__(self):
        self.started = []

    def resume_tasks(self, persisted):
        return [DownloadTask(p["task_id"], "slskd", DownloadStatus.DOWNLOADING, "b", "a", p["book_id"], "f") for p in persisted]

    def start_targets(self, targets, on_complete=None):
        self.started = [t.book_id for t in targets]
        return []

    def monitor_until(self, tasks, on_complete=None, deadline=None):
        return [], tasks

    def get_backend(self, name):
        return None


def test_max_active_downloads(tmp_path):
    config = make_config(download={"max_active_downloads": "3", "monitor_window": "60"})
    state = StateManager(str(tmp_path))
    state.add_task(DownloadTask("r1", "slskd", DownloadStatus.DOWNLOADING, "b", "a", 100, "f"))
    orch = CountingOrchestrator()
    ctx = Context(config=config, slskd=None, readarr=NoEditions(), config_dir=str(tmp_path), state=state, orchestrator=orch)
    wanted = [{"book": {"id": i, "title": f"B{i}"}, "author": {"authorName": "A"}} for i in range(1, 6)]

    workflow.run_workflow(ctx, wanted)

    assert orch.started == [1, 2]  # 3 allowed, 1 already running


# ---------------------------------------------------------------------------
# 10. Imports run alongside monitoring instead of freezing it
# ---------------------------------------------------------------------------


def test_slow_import_does_not_block_monitoring(monkeypatch, tmp_path):
    second_polled = threading.Event()

    def slow_import(ctx, items):
        # Would deadlock (and time out) if monitoring waited for this import
        assert second_polled.wait(timeout=5), "monitoring was blocked by the import"
        return {items[0]["bookId"]: True}

    monkeypatch.setattr(workflow.postprocess, "process_imports", slow_import)

    class Backend:
        name = "fake"

        def __init__(self):
            self.polls = {"a": 0, "b": 0}

        def get_status(self, task):
            self.polls[task.task_id] += 1
            if task.task_id == "a":
                task.status = DownloadStatus.COMPLETED
            else:
                if self.polls["b"] >= 2:
                    second_polled.set()
                task.status = DownloadStatus.COMPLETED if self.polls["b"] >= 3 else DownloadStatus.DOWNLOADING
                task.bytes_transferred = self.polls["b"]
            return task

        def cancel(self, task):
            pass

    ctx = Context(config=make_config(), slskd=None, readarr=None, config_dir=str(tmp_path))
    orch = DownloadOrchestrator([Backend()], ctx)
    orch.poll_interval = 0.01
    ctx.orchestrator = orch
    results = workflow._RunResults(ctx, [])
    tasks = [DownloadTask("a", "fake", DownloadStatus.QUEUED, "A", "x", 1, "a"), DownloadTask("b", "fake", DownloadStatus.QUEUED, "B", "x", 2, "b")]

    orch.monitor_until(tasks, on_complete=results.on_complete)
    results.wait_for_imports()

    assert sorted(t.book_id for t in results.completed_tasks) == [1, 2]


# ---------------------------------------------------------------------------
# 11. Timers start when the download is queued
# ---------------------------------------------------------------------------


def test_timers_start_at_enqueue(monkeypatch):
    class Backend:
        name = "fake"

        def search(self, target):
            return ["result"]

        def download(self, target, result):
            return DownloadTask("t", "fake", DownloadStatus.QUEUED, "b", "a", 1, "f")

    orch = DownloadOrchestrator([], Context(config=make_config(), slskd=None, readarr=None))
    monkeypatch.setattr("rsoul.orchestrator.time.time", lambda: 12345.0)
    task = orch._start_backend_download(Backend(), target())
    assert task.extra["monitor"]["started_at"] == 12345.0


# ---------------------------------------------------------------------------
# 12. No queueing without a snapshot of existing transfers
# ---------------------------------------------------------------------------


def test_enqueue_aborts_without_snapshot():
    class Transfers:
        enqueued = False

        def get_downloads(self, username):
            raise RuntimeError("slskd down")

        def enqueue(self, username, files):
            Transfers.enqueued = True
            return True

    class C:
        transfers = Transfers()

    assert download_module.slskd_do_enqueue(C(), "peer", [{"filename": "a\\b.mp3", "size": 1}], "a") is None
    assert Transfers.enqueued is False


# ---------------------------------------------------------------------------
# 13. Match quality beats format order
# ---------------------------------------------------------------------------


def test_better_match_beats_preferred_format():
    base = "@@s\\Audiobooks\\Brandon Sanderson"
    t = {"book": {"title": TITLE}, "author": {"authorName": AUTHOR}}
    files = chapters(f"{base}\\Mistborn - The Final Empire", 10, ext="mp3") + [
        {"filename": f"{base}\\Mistborn Final Empire Graphic Audio Dramatized Full Cast Edition\\book.m4b", "size": 600 * MB}
    ]
    matches = audiobook_folder_match(t, files, "peer", ["m4b", "mp3"], [], 0.5)
    assert matches[0]["extension"] == "mp3"


# ---------------------------------------------------------------------------
# 16. A file of unknown size is never assumed finished
# ---------------------------------------------------------------------------


def test_unknown_size_is_not_finished(tmp_path):
    (tmp_path / "B").mkdir()
    (tmp_path / "B" / "book.epub").write_bytes(b"x")
    b = backend(slskd={"download_dir": str(tmp_path)})
    task = DownloadTask("t", "slskd", DownloadStatus.DOWNLOADING, "b", "a", 1, "f", local_dir="B")
    assert b._finished_on_disk(task, {"filename": "x\\book.epub", "size": 0}) is False
    assert b._finished_on_disk(task, {"filename": "x\\book.epub", "size": 1}) is True


# ---------------------------------------------------------------------------
# 17. One status request per user per poll; finished files aren't polled again
# ---------------------------------------------------------------------------


def test_status_uses_one_request_per_user():
    class Transfers:
        list_calls = 0
        single_calls = 0

        def get_downloads(self, username):
            Transfers.list_calls += 1
            files = [{"id": i, "state": "Completed, Succeeded" if i < 50 else "InProgress", "bytesTransferred": 1} for i in range(60)]
            return {"directories": [{"directory": "d", "files": files}]}

        def get_download(self, username, id):
            Transfers.single_calls += 1
            raise AssertionError("per-file request used")

    b = backend(Client(transfers=Transfers()))
    task = DownloadTask("t", "slskd", DownloadStatus.DOWNLOADING, "b", "a", 1, "f")
    task.extra = {"files": [{"filename": f"d\\{i}.mp3", "id": i, "size": 1, "username": "u"} for i in range(60)]}

    b.get_status(task)
    b.get_status(task)

    assert Transfers.list_calls == 2
    assert Transfers.single_calls == 0


def test_status_falls_back_to_per_file_requests():
    class Transfers:
        def get_downloads(self, username):
            raise RuntimeError("list unavailable")

        def get_download(self, username, id):
            return {"state": "InProgress", "bytesTransferred": 3}

    b = backend(Client(transfers=Transfers()))
    task = DownloadTask("t", "slskd", DownloadStatus.DOWNLOADING, "b", "a", 1, "f")
    task.extra = {"files": [{"filename": "d\\1.mp3", "id": 1, "size": 10, "username": "u"}]}
    assert b.get_status(task).status == DownloadStatus.DOWNLOADING


# ---------------------------------------------------------------------------
# 18. Browsing stops at the first usable folder
# ---------------------------------------------------------------------------


def test_browsing_stops_at_first_usable_folder():
    d = "@@s\\Audiobooks\\Brandon Sanderson\\Mistborn - The Final Empire"
    users = Users({(f"peer{i}", d): chapters(d) for i in range(5)})
    b = backend(Client(users=users))
    responses = [{"username": f"peer{i}", "files": chapters(d)} for i in range(5)]
    results = b._match_audiobook_results(target(), target_dict(target()), responses)
    assert len(results) == 1
    assert len(users.calls) == 1


# ---------------------------------------------------------------------------
# State is safe to update from the import thread and the monitoring thread at once
# ---------------------------------------------------------------------------


def test_state_concurrent_updates(tmp_path):
    state = StateManager(str(tmp_path))
    for i in range(200):
        state.add_task(DownloadTask(f"t{i}", "slskd", DownloadStatus.QUEUED, "b", "a", i, "f"))

    errors = []

    def remove(ids):
        try:
            for i in ids:
                state.remove_task(f"t{i}")
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=remove, args=(range(k, 200, 4),)) for k in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert errors == []
    assert state.get_items() == []
    assert StateManager(str(tmp_path)).get_items() == []


# ---------------------------------------------------------------------------
# Re-review: a transient API error while waiting for an import is retried
# ---------------------------------------------------------------------------


def test_import_monitor_retries_transient_errors(monkeypatch):
    calls = {"n": 0}

    class Readarr:
        def get_command(self, id_):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("blip")
            return {"id": id_, "status": "completed", "result": "successful", "message": "Imported 1 book", "body": {}}

    monkeypatch.setattr(postprocess.time, "sleep", lambda s: None)
    assert postprocess.monitor_imports(Readarr(), [{"id": 1}]) == {1: True}


def test_import_thread_error_does_not_abort_the_run(monkeypatch, tmp_path):
    def broken_import(ctx, items):
        return {items[0]["bookId"]: True}

    monkeypatch.setattr(workflow.postprocess, "process_imports", broken_import)

    class BrokenState:
        def remove_task(self, task_id):
            raise OSError("disk full")

    ctx = Context(config=make_config(), slskd=None, readarr=None, config_dir=str(tmp_path), state=BrokenState())
    results = workflow._RunResults(ctx, [])
    results.on_complete(DownloadTask("t", "fake", DownloadStatus.COMPLETED, "b", "a", 1, "f"))
    results.wait_for_imports()  # must not raise
    assert len(results.completed_tasks) == 1
