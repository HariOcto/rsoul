"""Regression tests for the third review round.

1. A single-file audiobook in a folder full of other books downloads only that file
2. A peer whose files Readarr/Chaptarr rejects is recorded, and the rejected files are moved away
3. A saved download that can't be resumed is cleared out of slskd and the download folder
4. A failed download's transfers are cancelled before its leftovers are moved
5. The next search result is tried when the best one can't be started
6. Timeouts may be decimals, as the validation allows
7. Placeholder paths from the sample config are caught
8. Each wanted list keeps its own page counter; damaged or stale counters recover
9. Transfers queued but not tracked are cancelled again
10. Unknown "Completed, ..." states count as failures
11. A search that never leaves "InProgress" doesn't hang the run
12. ignored_users tolerates spaces
13. A state file with the wrong shape is treated as damaged
"""

import configparser
import json
import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rsoul import download as download_module
from rsoul import postprocess, workflow
from rsoul.backends import slskd_backend
from rsoul.backends.base import DownloadStatus, DownloadTarget, DownloadTask, SearchResult
from rsoul.backends.slskd_backend import SlskdBackend
from rsoul.config import Context, validate_config
from rsoul.history import HistoryManager
from rsoul.match import audiobook_folder_match
from rsoul.orchestrator import DownloadOrchestrator
from rsoul.search import get_books
from rsoul.state import StateManager
from rsoul.utils import get_current_page

MB = 1024 * 1024
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTHOR = "Brandon Sanderson"
TITLE = "Mistborn: The Final Empire"
LIBRARY = "@@share\\Audiobooks"
BOOK_FILE = f"{LIBRARY}\\Brandon Sanderson - Mistborn - The Final Empire.m4b"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(slskd_backend.time, "sleep", lambda s: None)
    monkeypatch.setattr(download_module.time, "sleep", lambda s: None)


def make_config(slskd=None, search=None, download=None):
    config = configparser.ConfigParser(interpolation=None)
    config["Readarr"] = {"api_key": "x", "host_url": "http://chaptarr"}
    config["Slskd"] = {"api_key": "x", "host_url": "http://slskd", "download_dir": "/nonexistent", **(slskd or {})}
    config["Search Settings"] = {"minimum_filename_match_ratio": "0.7", **(search or {})}
    config["Download Settings"] = {**(download or {})}
    config["Backends"] = {"slskd_enabled": "False"}
    return config


def target(formats=("m4b", "mp3")):
    return DownloadTarget(
        book_id=7,
        book_title=TITLE,
        author_name=AUTHOR,
        series_title="",
        allowed_filetypes=list(formats),
        readarr_book={"title": TITLE, "id": 7},
        readarr_author={"authorName": AUTHOR},
        media_type="audiobook",
    )


class Searches:
    def __init__(self, responses, state="Completed"):
        self.responses = responses
        self._state = state
        self.state_calls = 0

    def search_text(self, **kwargs):
        return {"id": "s1"}

    def state(self, search_id, include):
        self.state_calls += 1
        return {"state": self._state}

    def search_responses(self, search_id):
        return self.responses

    def delete(self, search_id):
        pass


class Users:
    def __init__(self, listings=None):
        self.listings = listings or {}
        self.browsed = []

    def directory(self, username, directory):
        self.browsed.append((username, directory))
        files = self.listings[(username, directory)]
        return [{"name": directory, "fileCount": len(files), "files": files}]


class Transfers:
    """A small slskd transfer list: enqueue registers records (optionally only some)."""

    def __init__(self, register=None, late=()):
        self.records = {}  # id -> record
        self.register = register
        self.late = list(late)  # records that only show up after the enqueue polling is over
        self.cancelled = []
        self.list_calls = 0

    def enqueue(self, username, files):
        shown = files if self.register is None else files[: self.register]
        for f in shown:
            self.records[f"new-{len(self.records)}"] = {"username": username, "filename": f["filename"], "size": f["size"], "state": "Queued, Remotely", "bytesTransferred": 0}
        return True

    def _dirs(self, username):
        dirs = {}
        for id_, r in self.records.items():
            if r["username"] == username:
                dirs.setdefault(r["filename"].rsplit("\\", 1)[0], []).append(dict(r, id=id_))
        return [{"directory": d, "files": fs} for d, fs in dirs.items()]

    def get_downloads(self, username):
        self.list_calls += 1
        return {"username": username, "directories": self._dirs(username)}

    def get_all_downloads(self):
        return [{"username": u, "directories": self._dirs(u)} for u in {r["username"] for r in self.records.values()}]

    def cancel_download(self, username, id, remove=False):
        self.cancelled.append(id)
        self.records.pop(id, None)
        return True


class Client:
    def __init__(self, responses=None, listings=None, transfers=None, search_state="Completed"):
        self.searches = Searches(responses or [], search_state)
        self.users = Users(listings)
        self.transfers = transfers or Transfers()


def backend(client=None, history=None, **config):
    ctx = Context(config=make_config(**config), slskd=client or Client(), readarr=None, history=history)
    return SlskdBackend(ctx)


# ---------------------------------------------------------------------------
# 1. Single-file audiobook in a shared folder
# ---------------------------------------------------------------------------


def library_client():
    """A peer's "Audiobooks" folder with 30 books in it; the search finds the one wanted."""
    others = [{"filename": f"Some Author - Other Book {i}.m4b", "size": 400 * MB} for i in range(30)]
    listing = [{"filename": BOOK_FILE.rsplit("\\", 1)[1], "size": 500 * MB}] + others
    return Client(responses=[{"username": "peer", "files": [{"filename": BOOK_FILE, "size": 500 * MB}]}], listings={("peer", LIBRARY): listing})


def test_single_file_match_is_flagged():
    t = {"book": {"title": TITLE}, "author": {"authorName": AUTHOR}}
    matches = audiobook_folder_match(t, [{"filename": BOOK_FILE, "size": 500 * MB}], "peer", ["m4b"], [], 0.7)
    assert [m["single_file"] for m in matches] == [True]


def test_folder_named_after_the_book_is_not_single_file():
    t = {"book": {"title": TITLE}, "author": {"authorName": AUTHOR}}
    directory = "@@share\\Brandon Sanderson\\Mistborn - The Final Empire"
    matches = audiobook_folder_match(t, [{"filename": f"{directory}\\book.m4b", "size": 500 * MB}], "peer", ["m4b"], [], 0.7)
    assert [m["single_file"] for m in matches] == [False]


def test_single_file_audiobook_does_not_take_the_whole_folder():
    client = library_client()
    results = backend(client).search(target())

    assert len(results) == 1
    assert [f["filename"] for f in results[0].extra["files"]] == [BOOK_FILE]
    assert results[0].size_bytes == 500 * MB
    assert client.users.browsed == []  # nothing to learn from the folder listing


def test_single_file_audiobooks_from_one_folder_get_separate_tasks():
    client = library_client()
    b = backend(client)
    result = b.search(target())[0]
    task = b.download(target(), result)

    assert task.task_id == f"peer|{BOOK_FILE}"
    assert task.extra["expected_files"] == ["Brandon Sanderson - Mistborn - The Final Empire.m4b"]
    assert task.local_dir == "Audiobooks"
    assert len(client.transfers.records) == 1


# ---------------------------------------------------------------------------
# 2. Rejected imports
# ---------------------------------------------------------------------------


class RejectingReadarr:
    """Readarr/Chaptarr finishing the scan without importing anything."""

    def __init__(self, result="unsuccessful"):
        self.result = result

    def post_command(self, name, **kwargs):
        return {"id": 1, "body": {"path": kwargs["path"]}}

    def get_command(self, id_):
        return {"id": 1, "status": "completed", "result": self.result, "message": "", "body": {"path": "/elsewhere"}}


def audiobook_item(local):
    (local / "Mistborn").mkdir(parents=True)
    (local / "Mistborn" / "01.mp3").write_bytes(b"x")
    return {"author_name": AUTHOR, "title": TITLE, "bookId": 7, "dir": "Mistborn", "filename": "01.mp3", "expected_files": ["01.mp3"], "backend_name": "slskd", "media_type": "audiobook", "source_id": "peer", "username": "peer"}


def test_rejected_import_records_the_peer(tmp_path):
    local = tmp_path / "dl"
    history = HistoryManager(str(tmp_path))
    ctx = Context(config=make_config(slskd={"download_dir": str(local)}), slskd=None, readarr=RejectingReadarr(), history=history)

    assert postprocess.process_imports(ctx, [audiobook_item(local)]) == {7: False}
    # Otherwise the next run finds, downloads and fails on the same folder again
    assert history.is_failed("peer", TITLE)


def test_files_left_after_a_reported_import_are_moved_away(tmp_path):
    local = tmp_path / "dl"
    history = HistoryManager(str(tmp_path))
    ctx = Context(config=make_config(slskd={"download_dir": str(local)}), slskd=None, readarr=RejectingReadarr(result="successful"), history=history)

    assert postprocess.process_imports(ctx, [audiobook_item(local)]) == {7: False}
    staged = local / postprocess.AUDIOBOOK_STAGING_DIR / "Brandon Sanderson" / "Mistborn The Final Empire"
    assert not (staged / "01.mp3").exists()
    assert (local / "failed_imports" / "Mistborn The Final Empire" / "01.mp3").exists()
    assert history.is_failed("peer", TITLE)


def test_pending_import_that_fails_records_the_peer(tmp_path, monkeypatch):
    history = HistoryManager(str(tmp_path))
    state = StateManager(str(tmp_path))
    state.items = [{"task_id": "peer|d", "backend_name": "slskd", "book_title": TITLE, "author_name": AUTHOR, "book_id": 7, "filename": "01.mp3", "extra": {"username": "peer", "import_pending": {"local_dir": "/x"}}}]
    ctx = Context(config=make_config(), slskd=None, readarr=None, config_dir=str(tmp_path), history=history, state=state)
    monkeypatch.setattr(postprocess, "check_pending_import", lambda readarr, pending: False)

    results = workflow._RunResults(ctx, [])
    assert workflow._follow_up_pending_imports(ctx, results) == 0
    assert history.is_failed("peer", TITLE)
    assert results.failed_imports == [(AUTHOR, TITLE)]
    assert state.get_items() == []


# ---------------------------------------------------------------------------
# 3. Dropped downloads are cleared away
# ---------------------------------------------------------------------------


def test_dropped_download_is_cancelled_and_its_files_moved(tmp_path):
    folder = tmp_path / "Mistborn"
    folder.mkdir()
    (folder / "01.mp3").write_bytes(b"chapter")  # finished before the rest was lost
    transfers = Transfers()
    transfers.records = {"t2": {"username": "peer", "filename": "@@s\\Mistborn\\02.mp3", "size": 10, "state": "Queued, Remotely", "bytesTransferred": 0}}
    config = make_config(slskd={"download_dir": str(tmp_path)})
    ctx = Context(config=config, slskd=Client(transfers=transfers), readarr=None, config_dir=str(tmp_path))
    ctx.orchestrator = DownloadOrchestrator([SlskdBackend(ctx)], ctx)
    ctx.state = StateManager(str(tmp_path))
    ctx.state.items = [
        {
            "task_id": "peer|@@s\\Mistborn",
            "backend_name": "slskd",
            "book_title": TITLE,
            "author_name": AUTHOR,
            "book_id": 7,
            "filename": "01.mp3",
            "local_dir": "Mistborn",
            # 03.mp3 is neither queued nor on disk, so the book can't be completed
            "extra": {"username": "peer", "file_dir": "@@s\\Mistborn", "media_type": "audiobook", "expected_files": ["01.mp3", "02.mp3", "03.mp3"], "files": [{"id": "t2", "filename": "@@s\\Mistborn\\02.mp3", "username": "peer"}]},
        }
    ]

    assert workflow._resume_persisted(ctx) == []
    assert ctx.state.get_items() == []
    assert transfers.cancelled == ["t2"]  # nothing keeps downloading unattended
    assert (tmp_path / "failed_downloads" / "Mistborn" / "01.mp3").exists()
    assert not folder.exists()  # a retry starts from a clean folder


# ---------------------------------------------------------------------------
# 4. Order of clean-up after a failed download
# ---------------------------------------------------------------------------


def test_transfers_are_cancelled_before_leftovers_are_moved(tmp_path):
    calls = []

    class Backend:
        name = "slskd"

        def cleanup(self, task):
            calls.append("cancel transfers")

        def discard(self, task):
            calls.append("move leftovers")

    class Orch:
        def get_backend(self, name):
            return Backend()

    ctx = Context(config=make_config(), slskd=None, readarr=None, config_dir=str(tmp_path), orchestrator=Orch())
    results = workflow._RunResults(ctx, [])
    results.on_complete(DownloadTask("t", "slskd", DownloadStatus.FAILED, TITLE, AUTHOR, 7, "01.mp3"))
    results.wait_for_imports()
    assert calls == ["cancel transfers", "move leftovers"]


def test_failed_task_leaves_state_even_if_the_failure_list_cannot_be_written(tmp_path):
    class Readarr:
        def get_book(self, book_id):
            return {"id": book_id, "title": TITLE, "monitored": True}

        def get_edition(self, book_id):
            return []

        def upd_book(self, book, editions):
            pass

    class Orch:
        def get_backend(self, name):
            return None

    config = make_config(search={"remove_wanted_on_failure": "True"})
    state = StateManager(str(tmp_path))
    ctx = Context(config=config, slskd=None, readarr=Readarr(), config_dir=str(tmp_path / "missing-folder"), orchestrator=Orch(), state=state)
    task = DownloadTask("t", "slskd", DownloadStatus.FAILED, TITLE, AUTHOR, 7, "01.mp3")
    state.add_task(task)

    results = workflow._RunResults(ctx, [])
    results.on_complete(task)
    results.wait_for_imports()
    assert state.get_items() == []
    assert results.failed_books == [(AUTHOR, TITLE)]


# ---------------------------------------------------------------------------
# 5. Next result when the best one can't be started
# ---------------------------------------------------------------------------


def test_next_result_is_tried_when_the_first_cannot_start():
    def result(name):
        return SearchResult(title=TITLE, author=AUTHOR, filename=name, size_bytes=1, extension="epub", backend_name="fake", source_id=name)

    class Backend:
        name = "fake"
        tried = []

        def search(self, t):
            return [result("a.epub"), result("b.epub"), result("c.epub")]

        def download(self, t, r):
            Backend.tried.append(r.filename)
            if r.filename == "a.epub":
                return None
            return DownloadTask(r.filename, "fake", DownloadStatus.QUEUED, TITLE, AUTHOR, 7, r.filename)

    ctx = Context(config=make_config(), slskd=None, readarr=None)
    orch = DownloadOrchestrator([Backend()], ctx)
    task = orch._start_backend_download(Backend(), target())
    assert task.filename == "b.epub"
    assert Backend.tried == ["a.epub", "b.epub"]


# ---------------------------------------------------------------------------
# 6. Decimal timeouts
# ---------------------------------------------------------------------------


def test_decimal_timeouts_are_accepted_everywhere():
    config = configparser.ConfigParser(interpolation=None)
    config.read(os.path.join(REPO, "config.ini"))
    config["Readarr"]["api_key"] = "a"
    config["Slskd"]["api_key"] = "b"
    config["Slskd"]["download_dir"] = "/downloads"
    config["Download Settings"].update({"stall_timeout": "1800.0", "queue_timeout": "90.5", "max_download_time": "0"})
    validate_config(config)

    orch = DownloadOrchestrator([], Context(config=config, slskd=None, readarr=None))
    assert (orch.stall_timeout, orch.queue_timeout, orch.max_download_time) == (1800.0, 90.5, 0.0)


# ---------------------------------------------------------------------------
# 7. Placeholder paths
# ---------------------------------------------------------------------------


def sample_config():
    config = configparser.ConfigParser(interpolation=None)
    config.read(os.path.join(REPO, "config.ini"))
    config["Readarr"]["api_key"] = "a"
    config["Slskd"]["api_key"] = "b"
    return config


def test_placeholder_download_dir_is_rejected():
    with pytest.raises(ValueError, match="download_dir"):
        validate_config(sample_config())


def test_placeholder_readarr_download_dir_falls_back_to_download_dir():
    config = sample_config()
    config["Slskd"]["download_dir"] = "/downloads"
    validate_config(config)
    ctx = Context(config=config, slskd=None, readarr=None)
    assert SlskdBackend(ctx).readarr_download_dir == "/downloads"


def test_zero_books_to_grab_is_rejected():
    config = sample_config()
    config["Slskd"]["download_dir"] = "/downloads"
    config["Search Settings"]["number_of_books_to_grab"] = "0"
    with pytest.raises(ValueError, match="number_of_books_to_grab"):
        validate_config(config)


def test_invalid_log_level_falls_back():
    import logging

    from rsoul.config import setup_logging

    root = logging.getLogger()
    before_level, before_handlers = root.level, list(root.handlers)
    try:
        setup_logging({"Logging": {"level": "verbose"}})
        assert root.level == logging.INFO
    finally:
        root.setLevel(before_level)
        root.handlers[:] = before_handlers


# ---------------------------------------------------------------------------
# 8. Page counters
# ---------------------------------------------------------------------------


class PagedReadarr:
    def __init__(self, missing_total=50, cutoff_total=50):
        self.totals = {"missing": missing_total, "cutoff": cutoff_total}
        self.pages = {"missing": [], "cutoff": []}

    def _get(self, kind, page=None, page_size=10, **kwargs):
        if page is not None:
            self.pages[kind].append(page)
        total = self.totals[kind]
        start = ((page or 1) - 1) * page_size
        return {"totalRecords": total, "records": [{"id": i} for i in range(start, min(total, start + page_size))]}

    def get_missing(self, **kwargs):
        return self._get("missing", **kwargs)

    def get_cutoff(self, **kwargs):
        return self._get("cutoff", **kwargs)


def paged_ctx(tmp_path, readarr):
    config = configparser.ConfigParser()
    config["Search Settings"] = {}
    return Context(config=config, slskd=None, readarr=readarr, config_dir=str(tmp_path))


def test_each_wanted_list_keeps_its_own_page(tmp_path):
    readarr = PagedReadarr()
    ctx = paged_ctx(tmp_path, readarr)
    for _ in range(3):  # three runs with search_source = all
        get_books(ctx, "missing", "incrementing_page", 10)
        get_books(ctx, "cutoff_unmet", "incrementing_page", 10)

    # With one shared counter each list saw only every other page: 1, 3, 5 and 2, 4, 1
    assert readarr.pages == {"missing": [1, 2, 3], "cutoff": [1, 2, 3]}


def test_page_beyond_the_end_restarts_at_page_one(tmp_path):
    (tmp_path / ".current_page.txt").write_text("9")  # the wanted list has shrunk to two pages
    readarr = PagedReadarr(missing_total=20)
    books = get_books(paged_ctx(tmp_path, readarr), "missing", "incrementing_page", 10)

    assert readarr.pages["missing"] == [1]
    assert len(books) == 10
    assert (tmp_path / ".current_page.txt").read_text() == "2"


@pytest.mark.parametrize("content", ["", "abc", "-3", "0"])
def test_damaged_page_file_starts_again(tmp_path, content):
    path = tmp_path / ".current_page.txt"
    path.write_text(content)
    assert get_current_page(str(path)) == 1
    assert path.read_text() == "1"


# ---------------------------------------------------------------------------
# 9. Untracked transfers
# ---------------------------------------------------------------------------


def chapter_files(count):
    return [{"filename": f"@@s\\Mistborn\\{i:02d}.mp3", "size": 10} for i in range(1, count + 1)]


def test_partly_registered_audiobook_is_cancelled_completely():
    transfers = Transfers(register=2)
    client = Client(transfers=transfers)

    assert download_module.slskd_do_enqueue(client, "peer", chapter_files(4), "@@s\\Mistborn", require_all=True) is None
    assert sorted(transfers.cancelled) == ["new-0", "new-1"]
    assert transfers.records == {}


def test_transfer_that_never_showed_up_is_cancelled_when_it_appears_late():
    class LateTransfers(Transfers):
        def get_downloads(self, username):
            # Nothing is listed while R:soul polls (first call is the snapshot, then 4 polls);
            # the record only appears afterwards
            listing = super().get_downloads(username)
            return listing if self.list_calls > 5 else {"username": username, "directories": []}

    transfers = LateTransfers()
    client = Client(transfers=transfers)

    assert download_module.slskd_do_enqueue(client, "peer", chapter_files(1), "@@s\\Mistborn") is None
    assert transfers.cancelled == ["new-0"]


def test_older_transfers_of_the_same_file_are_left_alone():
    transfers = Transfers(register=0)
    transfers.records = {"old": {"username": "peer", "filename": "@@s\\Mistborn\\01.mp3", "size": 10, "state": "Completed, Errored", "bytesTransferred": 0}}
    client = Client(transfers=transfers)

    assert download_module.slskd_do_enqueue(client, "peer", chapter_files(1), "@@s\\Mistborn") is None
    assert transfers.cancelled == []


# ---------------------------------------------------------------------------
# 10. Unknown finished states
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["Completed, Errored", "Completed, Cancelled", "Completed, SomethingNew"])
def test_any_finished_state_other_than_success_is_a_failure(state):
    files = [{"status": {"state": "Completed, Succeeded"}}, {"status": {"state": state}}]
    assert download_module.downloads_all_done(files) == (False, True)


def test_unfinished_states_are_not_failures():
    files = [{"status": {"state": "Completed, Succeeded"}}, {"status": {"state": "Queued, Remotely"}}, {"status": None}]
    assert download_module.downloads_all_done(files) == (False, False)


# ---------------------------------------------------------------------------
# 11. Search that never finishes
# ---------------------------------------------------------------------------


def test_search_stuck_in_progress_does_not_hang(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(slskd_backend.time, "time", lambda: clock["now"])
    monkeypatch.setattr(slskd_backend.time, "sleep", lambda s: clock.__setitem__("now", clock["now"] + s))

    client = Client(responses=[{"username": "peer", "files": []}], search_state="InProgress")
    ctx = Context(config=make_config(search={"search_timeout": "5000"}), slskd=client, readarr=None)

    results, search_id = slskd_backend._execute_search(ctx, "Brandon Sanderson - Mistborn", "main")
    assert search_id == "s1"
    assert len(results) == 1
    assert client.searches.state_calls < 100


# ---------------------------------------------------------------------------
# 12. ignored_users
# ---------------------------------------------------------------------------


def test_ignored_users_may_have_spaces():
    b = backend(search={"ignored_users": "someone, peer ,"})
    assert b._match_thresholds()["ignored_users"] == ["someone", "peer"]


# ---------------------------------------------------------------------------
# 13. State file with the wrong shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("content", ['{"task_id": "x"}', '"text"', "[1, 2]"])
def test_state_file_with_wrong_shape_is_not_used(tmp_path, content):
    (tmp_path / "grab_list_state.json").write_text(content)
    state = StateManager(str(tmp_path))
    assert state.get_items() == []
    assert state.get_tasks_for_orchestrator() == []


def test_state_file_roundtrip_still_works(tmp_path):
    (tmp_path / "grab_list_state.json").write_text(json.dumps([{"task_id": "a", "book_id": 1}]))
    assert StateManager(str(tmp_path)).get_tasks_for_orchestrator() == [{"task_id": "a", "book_id": 1}]
