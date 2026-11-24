"""Cross-module recovery regressions from the independent safety review.

External APIs are simulated; queues and recovery journals use real disk writes.
"""
import configparser
import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import requests

from rsoul.backends import DownloadTask, DownloadStatus
from rsoul.config import Context
from rsoul.import_transaction import SCHEMA, JOURNAL_DIR
from rsoul.library import CatalogUnavailable, require_author_catalog, sibling_titles
from rsoul.search import SearchError
from rsoul.state import StateManager
from rsoul.wanted_queue import WantedQueue
from rsoul.workflow import _RunResults


REPO = Path(__file__).resolve().parents[1]


def _config(mode="audiobook", limit=6):
    config = configparser.ConfigParser(interpolation=None)
    config.read_dict({
        "Readarr": {"disable_sync": "false"},
        "Slskd": {"download_dir": "/downloads"},
        "Search Settings": {
            "media_mode": mode, "wanted_scan_pages": "1",
            "wanted_backlog_limit": str(limit), "remove_wanted_on_failure": "false",
        },
    })
    return config


class _WantedAPI:
    def __init__(self):
        # Plain Readarr has no per-record mediaType; the configured instance mode
        # supplies the type just as it does when targets are constructed.
        self.books = {n: {"id": n, "authorId": 1, "title": f"Book {n}",
                          "monitored": True, "hasFiles": False} for n in range(1, 7)}
        self.page_error = None
        self.refresh_error = None
        self.calls = []

    def get_missing(self, page=1, page_size=2, **kwargs):
        self.calls.append(page)
        if self.page_error:
            raise self.page_error
        rows = [b for b in self.books.values() if not b["hasFiles"]]
        return {"totalRecords": len(rows), "records": copy.deepcopy(rows[(page - 1) * page_size:page * page_size])}

    get_cutoff = get_missing

    def get_book(self, book_id):
        if self.refresh_error:
            raise self.refresh_error
        return copy.deepcopy(self.books[book_id])


@pytest.mark.parametrize("mode", ["ebook", "audiobook"])
def test_full_wanted_backlog_retires_satisfied_plain_readarr_books(tmp_path, mode):
    api = _WantedAPI()
    ctx = Context(_config(mode, limit=2), None, api, str(tmp_path))
    queue = WantedQueue(str(tmp_path))
    assert [b["id"] for b in queue.fetch(ctx, "missing", "incrementing_page", 2)] == [1, 2]
    api.books[1]["hasFiles"] = api.books[2]["hasFiles"] = True
    assert queue.fetch(ctx, "missing", "incrementing_page", 2) == []
    assert queue.data["lists"][f"missing:{mode}"]["candidates"] == []
    assert api.calls == [1]  # The full backlog was refreshed without another page.


def test_unknown_media_in_both_mode_is_not_assumed_satisfied(tmp_path):
    api = _WantedAPI()
    ctx = Context(_config("both", limit=2), None, api, str(tmp_path))
    queue = WantedQueue(str(tmp_path))
    queue.fetch(ctx, "missing", "incrementing_page", 2)
    api.books[1]["hasFiles"] = True
    assert {b["id"] for b in queue.fetch(ctx, "missing", "incrementing_page", 2)} == {1, 2}


def test_wanted_page_outage_uses_refreshed_backlog_without_advancing_cursor(tmp_path):
    api = _WantedAPI()
    ctx = Context(_config(), None, api, str(tmp_path))
    queue = WantedQueue(str(tmp_path))
    queue.fetch(ctx, "missing", "incrementing_page", 2)
    before = queue.path.read_bytes()
    api.page_error = requests.ConnectionError("wanted endpoint offline")
    ready = queue.fetch(ctx, "missing", "incrementing_page", 2)
    assert {b["id"] for b in ready} == {1, 2}
    assert queue.path.read_bytes() == before
    assert ctx.wanted_scan_errors == {"missing": "wanted endpoint offline"}
    api.page_error = None
    queue.fetch(ctx, "missing", "incrementing_page", 2)
    assert ctx.wanted_scan_errors == {}


def test_wanted_outage_does_not_search_unrefreshable_snapshots(tmp_path):
    api = _WantedAPI()
    ctx = Context(_config(), None, api, str(tmp_path))
    queue = WantedQueue(str(tmp_path))
    queue.fetch(ctx, "missing", "incrementing_page", 2)
    before = queue.path.read_bytes()
    api.page_error = requests.ConnectionError("wanted endpoint offline")
    api.refresh_error = requests.ConnectionError("book endpoint offline")
    with pytest.raises(requests.ConnectionError, match="wanted endpoint"):
        queue.fetch(ctx, "missing", "incrementing_page", 2)
    assert queue.path.read_bytes() == before


def test_malformed_wanted_page_does_not_partially_advance_or_search_its_rows(tmp_path):
    api = _WantedAPI()
    ctx = Context(_config(), None, api, str(tmp_path))
    queue = WantedQueue(str(tmp_path))
    queue.fetch(ctx, "missing", "incrementing_page", 2)
    before = queue.path.read_bytes()
    api.get_missing = lambda **kwargs: {"totalRecords": 6, "records": [api.books[3], {"title": "missing ID"}]}
    assert {b["id"] for b in queue.fetch(ctx, "missing", "incrementing_page", 2)} == {1, 2}
    assert queue.path.read_bytes() == before
    assert "missing" in ctx.wanted_scan_errors


def test_catalog_outage_is_distinct_from_empty_and_retries_same_run():
    calls = []
    def get(resource, ver_uri=None, params=None):
        calls.append(params)
        if len(calls) == 1:
            raise requests.ConnectionError("bibliography endpoint offline")
        return [{"id": 8, "authorId": 1, "title": "Dune Messiah"}]
    # A small context without stats exercises compatibility with independent
    # import/preview callers as well as the production Context dataclass.
    ctx = SimpleNamespace(readarr=SimpleNamespace(_get=get, ver_uri="/api/v1"))
    book = {"id": 7, "authorId": 1, "title": "Dune"}
    with pytest.raises(CatalogUnavailable, match="offline"):
        require_author_catalog(ctx, book)
    assert require_author_catalog(ctx, book)["titles"] == ["Dune Messiah"]
    assert sibling_titles(ctx, book) == ["Dune Messiah"]
    assert len(calls) == 2


def test_valid_empty_catalog_is_cached_as_success():
    calls = []
    def get(*args, **kwargs):
        calls.append(1)
        return []
    ctx = SimpleNamespace(readarr=SimpleNamespace(_get=get, ver_uri="/api/v1"))
    for _ in range(2):
        assert require_author_catalog(ctx, {"authorId": 1}) == {"titles": [], "series": [], "records": []}
    assert len(calls) == 1


@pytest.mark.parametrize("response", [{"records": []}, [None], [{"id": 7, "authorId": 1}]])
def test_malformed_catalog_is_never_cached_as_empty(response):
    ctx = SimpleNamespace(readarr=SimpleNamespace(_get=lambda *args, **kwargs: response, ver_uri="/api/v1"))
    with pytest.raises(CatalogUnavailable):
        require_author_catalog(ctx, {"authorId": 1})
    assert ctx.stats["author_titles"] == {}


def test_local_discard_failure_preserves_failed_task_for_recovery(tmp_path):
    state = StateManager(str(tmp_path))
    task = DownloadTask("task-discard", "slskd", DownloadStatus.FAILED,
                        "Dune", "Frank Herbert", 7, "01.mp3", local_dir="Dune",
                        extra={"files": [{"filename": "01.mp3", "size": 10}], "media_type": "audiobook"})
    task.error_message = "File(s) failed: Completed, Errored"
    state.add_task(task)
    def failed_discard(_task):
        raise OSError("quarantine directory is read-only")
    backend = SimpleNamespace(cleanup=lambda _task: True, discard=failed_discard)
    ctx = Context(_config(), None, object(), str(tmp_path), state=state)
    ctx.orchestrator = SimpleNamespace(get_backend=lambda _name: backend)
    results = _RunResults(ctx, [])
    try:
        results.on_complete(task)
    finally:
        results.wait_for_imports()
    recovered = StateManager(str(tmp_path)).get_items()
    assert len(recovered) == 1
    assert recovered[0]["extra"]["terminal_failure"] == task.error_message


def _main_fixture(tmp_path, monkeypatch, **search):
    spec = importlib.util.spec_from_file_location("rsoul_main_review", REPO / "rsoul.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = configparser.ConfigParser(interpolation=None)
    config.read(REPO / "config.ini")
    config["Readarr"].update(api_key="test", host_url="http://unused.invalid", disable_sync="true")
    config["Slskd"].update(api_key="test", download_dir=str(tmp_path / "downloads"))
    config["Backends"]["slskd_enabled"] = "false"
    config["Search Settings"].update(media_mode="audiobook", **search)
    config["Download Settings"]["monitor_window"] = "30"
    (tmp_path / "downloads").mkdir()
    with (tmp_path / "config.ini").open("w") as handle:
        config.write(handle)
    for key in list(__import__("os").environ):
        if key.upper().startswith("RSOUL__") or key == "IN_DOCKER":
            monkeypatch.delenv(key)
    monkeypatch.setattr(sys, "argv", ["rsoul.py", "-c", str(tmp_path)])
    monkeypatch.setattr(module, "setup_logging", lambda config: None)
    monkeypatch.setattr(module, "report_versions", lambda ctx: None)
    client = SimpleNamespace(session=requests.Session(), get_author=lambda _id: {"authorName": "Author"})
    monkeypatch.setattr(module, "ReadarrAPI", lambda *_: client)
    backend = SimpleNamespace(name="slskd", download_dir=str(tmp_path / "downloads"))
    monkeypatch.setattr(module, "create_backends_from_config", lambda ctx: [backend])
    orch = SimpleNamespace(backends={"slskd": backend}, unchecked_task_ids=set(),
                           get_backend=lambda _name: backend,
                           monitor_until=lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(module, "DownloadOrchestrator", lambda *_: orch)
    return module


def test_cli_keeps_healthy_wanted_source_when_another_source_fails(tmp_path, monkeypatch):
    module = _main_fixture(tmp_path, monkeypatch, search_source="all")
    wanted = {"id": 7, "authorId": 1, "title": "Book", "mediaType": "audiobook"}
    def get_books(ctx, source, *args, **kwargs):
        if source == "cutoff_unmet":
            raise SearchError("cutoff endpoint unavailable")
        return [wanted]
    monkeypatch.setattr(module, "get_books", get_books)
    targets = []
    monkeypatch.setattr(module, "run_workflow", lambda ctx, values: targets.extend(values) or {})
    assert module.main() == module.EXIT_PARTIAL
    assert [t["book"]["id"] for t in targets] == [7]


def test_cli_adopts_sidecar_only_work_even_when_no_books_are_wanted(tmp_path, monkeypatch):
    module = _main_fixture(tmp_path, monkeypatch)
    root = tmp_path / "downloads"
    journal_dir = root / JOURNAL_DIR
    journal_dir.mkdir()
    pending = {"schema": SCHEMA, "operation_id": "sidecar-operation", "phase": "submission_unknown",
               "download_root": str(root), "book_id": 7, "media_type": "audiobook",
               "local_dir": str(root / "staged-book"), "readarr_path": "/downloads/staged-book",
               "files": [], "source_task": {
                   "task_id": "sidecar-task", "book_id": 7, "book_title": "Dune", "author_name": "Frank Herbert",
                   "backend_name": "slskd", "filename": "Dune.mp3", "local_dir": "Dune", "extra": {"media_type": "audiobook"},
               }}
    (journal_dir / "sidecar-operation.json").write_text(json.dumps(pending))
    monkeypatch.setattr(module, "get_books", lambda *args, **kwargs: [])
    assert module.main() == module.EXIT_OK
    recovered = StateManager(str(tmp_path)).get_items()
    assert len(recovered) == 1
    assert recovered[0]["task_id"] == "sidecar-task"
    assert recovered[0]["extra"]["import_pending"]["phase"] == "submission_unknown"


@pytest.mark.parametrize("repeat", [False, True])
def test_repeat_runs_keep_the_startup_report_out_of_the_info_log(tmp_path, monkeypatch, caplog, capsys, repeat):
    # run.sh sets RSOUL_REPEAT_RUN after the container's first run: the banner and the config
    # lines are the same every 5 minutes, so later runs log them at debug level only
    import logging
    module = _main_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("RSOUL__SEARCH_SETTINGS__NUMBER_OF_BOOKS_TO_GRAB", "10")
    if repeat:
        monkeypatch.setenv("RSOUL_REPEAT_RUN", "1")
    else:
        monkeypatch.delenv("RSOUL_REPEAT_RUN", raising=False)
    monkeypatch.setattr(module, "get_books", lambda *args, **kwargs: [])
    with caplog.at_level(logging.INFO):
        module.main()
    info = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    shown = any(m.startswith("Environment override:") for m in info) and any(m.startswith("Media mode:") for m in info)
    assert shown is (not repeat)
    assert ("READARR SOUL" in capsys.readouterr().out) is (not repeat)
