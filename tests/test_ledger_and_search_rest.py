"""Regression tests for the eleventh round: findings from a day of real runs on TrueNAS.

1. A book R:soul imported is not downloaded again when it turns up as wanted once more
2. A book nobody shares is not searched for again on every run
3. A peer that refuses a download is left alone for that book for a day
4. A name that passed the title and author checks is not rejected by the fuzzy score
5. A rate-limited slskd search is repeated once
6. The run summary tells failed downloads from books that were not found
"""

import logging
import os
import sys
from datetime import datetime, timedelta

import pytest
from rsoul.outcomes import SearchIncomplete

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_download_cleanup_and_retries import MB, Client, make_config
from test_duplicates_and_shared_folders import THRESHOLDS, Chaptarr, wanted

from rsoul import display, workflow
from rsoul import download as download_module
from rsoul.backends import slskd_backend
from rsoul.backends.base import DownloadStatus, DownloadTarget, DownloadTask
from rsoul.backends.slskd_backend import SlskdBackend
from rsoul.config import Context, validate_config
from rsoul.history import HistoryManager
from rsoul.ledger import BookLedger
from rsoul.match import audiobook_folder_match
from rsoul.state import StateManager

NUTTALL = "Christopher G. Nuttall"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(slskd_backend.time, "sleep", lambda s: None)
    monkeypatch.setattr(download_module.time, "sleep", lambda s: None)


def ctx_for(tmp_path, records=(), **search):
    config = make_config(search={"media_mode": "audiobook", **search})
    ctx = Context(config=config, slskd=None, readarr=Chaptarr(records), config_dir=str(tmp_path), state=StateManager(str(tmp_path)))
    ctx.ledger = BookLedger(str(tmp_path))
    return ctx


def built(ctx, books, **kwargs):
    return [t.book_id for t in workflow.build_targets(ctx, books, **kwargs)]


# ---------------------------------------------------------------------------
# 1. Imported, and wanted again
# ---------------------------------------------------------------------------


def test_imported_book_is_not_downloaded_again(tmp_path, caplog):
    ctx = ctx_for(tmp_path)
    ctx.ledger.note_imported(4020, NUTTALL, "The Trafalgar Gambit", "audiobook")

    with caplog.at_level(logging.WARNING):
        assert built(ctx, [wanted(4020, "The Trafalgar Gambit", author=NUTTALL), wanted(4030, "Warspite", author=NUTTALL)]) == [4030]
    assert "Skipping Christopher G. Nuttall - The Trafalgar Gambit (book 4020): R:soul downloaded this title and it was imported on" in caplog.text
    assert "book_ledger.json" in caplog.text


def test_another_record_of_an_imported_title_is_not_downloaded_either(tmp_path):
    # Two records, one title: after the import for 2730, record 4728 must not start the same download
    ctx = ctx_for(tmp_path)
    ctx.ledger.note_imported(2730, "Kevin J. Anderson", "Scattered Suns: The Saga of the Seven Suns Book 4", "audiobook")
    assert built(ctx, [wanted(4728, "Scattered Suns: The Saga of the Seven Suns Book 4", author="Kevin J. Anderson")]) == []
    # ...but the ebook of the same title is another matter
    assert built(ctx_for_mode(tmp_path, "ebook"), [wanted(9, "Scattered Suns: The Saga of the Seven Suns Book 4", "ebook", author="Kevin J. Anderson")]) == [9]


def ctx_for_mode(tmp_path, mode):
    ctx = ctx_for(tmp_path, media_mode=mode)
    return ctx


def test_guard_ends_after_redownload_after_days(tmp_path):
    ctx = ctx_for(tmp_path, redownload_after_days="30")
    ctx.ledger.note_imported(1, NUTTALL, "Warspite", "audiobook")
    ctx.ledger.books["1"]["imported_at"] = (datetime.now() - timedelta(days=31)).isoformat()
    assert built(ctx, [wanted(1, "Warspite", author=NUTTALL)]) == [1]


def test_guard_can_be_switched_off(tmp_path):
    ctx = ctx_for(tmp_path, redownload_after_days="0")
    ctx.ledger.note_imported(1, NUTTALL, "Warspite", "audiobook")
    assert built(ctx, [wanted(1, "Warspite", author=NUTTALL)]) == [1]


def test_book_wanted_for_better_quality_is_not_held_back(tmp_path):
    # It has files and is on the cutoff-unmet list: an upgrade, not a failed import
    ctx = ctx_for(tmp_path)
    ctx.ledger.note_imported(1, NUTTALL, "Warspite", "audiobook")
    upgrade = wanted(1, "Warspite", author=NUTTALL)
    upgrade["book"]["hasFiles"] = True
    assert built(ctx, [upgrade]) == [1]
    readarr_style = wanted(1, "Warspite", author=NUTTALL)
    readarr_style["book"]["statistics"] = {"bookFileCount": 1}
    assert built(ctx, [readarr_style]) == [1]


class Orch:
    def get_backend(self, name):
        return None


def test_successful_import_is_written_to_the_ledger(tmp_path, monkeypatch):
    ctx = ctx_for(tmp_path)
    ctx.orchestrator = Orch()
    monkeypatch.setattr(workflow.postprocess, "process_imports", lambda ctx, items, **kwargs: {4020: True})
    results = workflow._RunResults(ctx, [])
    task = DownloadTask("t", "slskd", DownloadStatus.COMPLETED, "The Trafalgar Gambit", NUTTALL, 4020, "a.mp3", extra={"media_type": "audiobook"})
    results.on_complete(task)
    results.wait_for_imports()

    assert BookLedger(str(tmp_path)).imported_recently(4020, NUTTALL, "The Trafalgar Gambit", "audiobook", 30) is not None  # saved to disk
    assert built(ctx, [wanted(4020, "The Trafalgar Gambit", author=NUTTALL)]) == []


def test_failed_import_is_not_in_the_ledger(tmp_path, monkeypatch):
    ctx = ctx_for(tmp_path)
    ctx.orchestrator = Orch()
    monkeypatch.setattr(workflow.postprocess, "process_imports", lambda ctx, items, **kwargs: {7: False})
    results = workflow._RunResults(ctx, [])
    results.on_complete(DownloadTask("t", "slskd", DownloadStatus.COMPLETED, "Robot Dreams", "Isaac Asimov", 7, "a.mp3", extra={"media_type": "audiobook"}))
    results.wait_for_imports()
    assert built(ctx, [wanted(7, "Robot Dreams")]) == [7]


# ---------------------------------------------------------------------------
# 2. Not found
# ---------------------------------------------------------------------------


def failed_start(book_id, title, reason):
    return DownloadTask(f"not_found_{book_id}", "none", DownloadStatus.FAILED, title, NUTTALL, book_id, "", extra={"start_failure": reason})


def test_book_nobody_shares_rests_for_a_day(tmp_path, caplog):
    ctx = ctx_for(tmp_path)
    ctx.orchestrator = Orch()
    results = workflow._RunResults(ctx, [])
    results.on_complete(failed_start(4133, "The Prince's Alliance", "not_found"))

    books = [wanted(4133, "The Prince's Alliance", author=NUTTALL), wanted(4030, "Warspite", author=NUTTALL)]
    with caplog.at_level(logging.INFO):
        assert built(ctx, books) == [4030]
    assert "Not searching again yet for 1 book(s)" in caplog.text and "The Prince's Alliance" in caplog.text

    ctx.ledger.books["4133"]["not_found_at"] = (datetime.now() - timedelta(days=1, minutes=1)).isoformat()
    assert built(ctx, books) == [4133, 4030]


@pytest.mark.parametrize("reason", ["not_searched", "not_started"])
def test_book_that_was_not_really_searched_does_not_rest(tmp_path, reason):
    # slskd was down, or the match could not be used right now: try again on the next run
    ctx = ctx_for(tmp_path)
    ctx.orchestrator = Orch()
    workflow._RunResults(ctx, []).on_complete(failed_start(1, "Warspite", reason))
    assert built(ctx, [wanted(1, "Warspite", author=NUTTALL)]) == [1]


def test_rest_can_be_switched_off_and_the_dry_run_ignores_it(tmp_path):
    ctx = ctx_for(tmp_path, not_found_retry_days="0")
    ctx.ledger.note_not_found(1, NUTTALL, "Warspite", "audiobook")
    assert built(ctx, [wanted(1, "Warspite", author=NUTTALL)]) == [1]

    ctx = ctx_for(tmp_path)
    ctx.ledger.note_imported(2, NUTTALL, "We Lead", "audiobook")
    books = [wanted(1, "Warspite", author=NUTTALL), wanted(2, "We Lead", author=NUTTALL)]
    assert built(ctx, books) == []
    assert built(ctx, books, use_ledger=False) == [1, 2]  # a dry run shows every wanted book


def test_started_download_ends_the_rest(tmp_path):
    ledger = BookLedger(str(tmp_path))
    ledger.note_not_found(1, NUTTALL, "Warspite", "audiobook")
    ledger.note_found(1)
    assert not ledger.not_found_recently(1, 1)


def test_read_only_ledger_writes_nothing_and_a_damaged_one_is_survived(tmp_path):
    ledger = BookLedger(str(tmp_path), read_only=True)
    ledger.note_imported(1, NUTTALL, "Warspite", "audiobook")
    assert list(tmp_path.iterdir()) == []

    (tmp_path / "book_ledger.json").write_text("{not json")
    assert BookLedger(str(tmp_path)).books == {}
    (tmp_path / "book_ledger.json").write_text('["a list"]')
    assert BookLedger(str(tmp_path)).books == {}


def test_new_settings_are_validated():
    config = make_config(search={"redownload_after_days": "-1"})
    with pytest.raises(ValueError, match="redownload_after_days"):
        validate_config(config)
    config = make_config(search={"not_found_retry_days": "often"})
    with pytest.raises(ValueError, match="not_found_retry_days"):
        validate_config(config)


# ---------------------------------------------------------------------------
# 3. A peer that refuses
# ---------------------------------------------------------------------------


def failed_download(error):
    return DownloadTask("Adarack|d", "slskd", DownloadStatus.FAILED, "Winter's Heart", "Robert Jordan", 3618, "9 Winter's Heart.m4b", extra={"username": "Adarack", "media_type": "audiobook"}, error_message=error)


def test_refusing_peer_is_left_alone_for_a_day(tmp_path):
    ctx = ctx_for(tmp_path)
    ctx.orchestrator = Orch()
    ctx.history = HistoryManager(str(tmp_path))
    results = workflow._RunResults(ctx, [])
    results.on_complete(failed_download("File(s) failed: Completed, Rejected"))

    assert ctx.history.is_failed("Adarack", "Winter's Heart")  # the next search goes to another peer
    entry = ctx.history.history[0]
    assert (datetime.fromisoformat(entry["retry_after"]) - datetime.fromisoformat(entry["timestamp"])).days == 1
    assert not ctx.history.is_failed("Adarack", "The Eye of the World")  # only for this book
    assert results.failed_downloads == [("Robert Jordan", "Winter's Heart", "File(s) failed: Completed, Rejected")]
    assert results.failed_books == []
    assert built(ctx, [wanted(3618, "Winter's Heart", author="Robert Jordan")]) == [3618]  # the book itself doesn't rest


@pytest.mark.parametrize("error", ["No data received for 1800s", "Waited more than 7200s in a queue without starting", "Exceeded max_download_time (86400s)"])
def test_peer_that_only_stalled_is_tried_again(tmp_path, error):
    ctx = ctx_for(tmp_path)
    ctx.orchestrator = Orch()
    ctx.history = HistoryManager(str(tmp_path))
    workflow._RunResults(ctx, []).on_complete(failed_download(error))
    assert not ctx.history.is_failed("Adarack", "Winter's Heart")


# ---------------------------------------------------------------------------
# 4. Title and author established: the fuzzy score only ranks
# ---------------------------------------------------------------------------


def candidates(title, author, path, size=180 * MB):
    t = {"book": {"title": title}, "author": {"authorName": author}}
    return audiobook_folder_match(t, [{"filename": path, "size": size}], "peer", ["mp3", "m4b"], **THRESHOLDS)


@pytest.mark.parametrize(
    "path",
    [
        "audiobooks\\Marko Kloos\\Frontlines\\Frontlines 7.5 - Corvus\\01.mp3",
        "audiobooks\\Marko Kloos\\Frontlines 7.5 - Corvus (Unabridged) [Luke Daniels]\\Corvus.m4b",
        "Audiobooks\\Marko Kloos - Frontlines Series\\07.5 - Corvus - A Frontlines Novella [64k]\\01.mp3",
        "Audio\\Kloos, Marko\\Corvus.mp3",
    ],
)
def test_one_word_title_behind_a_series_prefix_matches(path):
    found = candidates("Corvus", "Marko Kloos", path)
    if "Novella" in path:
        assert found == []  # "A Frontlines Novella" behind the title: not provably this book
    else:
        assert len(found) == 1 and found[0]["score"] >= THRESHOLDS["minimum_match_ratio"]


def test_low_scoring_match_ranks_behind_a_clean_one():
    t = {"book": {"title": "Corvus"}, "author": {"authorName": "Marko Kloos"}}
    odd = audiobook_folder_match(t, [{"filename": "a\\Marko Kloos\\Frontlines\\Frontlines 7.5 - Corvus\\01.mp3", "size": MB}], "p1", ["mp3"], **THRESHOLDS)[0]
    clean = audiobook_folder_match(t, [{"filename": "a\\Marko Kloos - Corvus\\01.mp3", "size": MB}], "p2", ["mp3"], **THRESHOLDS)[0]
    assert clean["score"] > odd["score"] == THRESHOLDS["minimum_match_ratio"]


@pytest.mark.parametrize(
    "title, author, path",
    [
        ("Corvus", "Marko Kloos", "a\\Marko Kloos\\Frontlines 2 - Lines of Departure\\01.mp3"),  # another title
        ("Corvus", "Marko Kloos", "a\\Someone Else\\Corvus\\01.mp3"),  # another author
        ("Frontlines", "Marko Kloos", "a\\Marko Kloos\\Frontlines 7.5 - Corvus\\01.mp3"),  # the series is not a book
        ("Corvus", "Marko Kloos", "a\\Marko Kloos\\Corvus 2\\01.mp3"),
    ],
)
def test_the_strict_checks_still_decide(title, author, path):
    assert candidates(title, author, path) == []


# ---------------------------------------------------------------------------
# 5. slskd rate limit
# ---------------------------------------------------------------------------


def test_rate_limited_search_is_repeated_once(monkeypatch):
    calls = []

    def flaky(ctx, query, label):
        calls.append(query)
        if len(calls) == 1:
            import requests
            response = requests.Response()
            response.status_code = 429
            response.headers["Retry-After"] = "0"
            raise requests.HTTPError("429 Client Error: Too Many Requests", response=response)
        return [], "s1"

    monkeypatch.setattr(slskd_backend, "_execute_search", flaky)
    ctx = Context(config=make_config(search={"max_search_fallbacks": "0"}), slskd=Client(), readarr=None)
    target = DownloadTarget(book_id=7, book_title="Endeavour", author_name=NUTTALL, series_title="", allowed_filetypes=["mp3"], media_type="audiobook")

    assert SlskdBackend(ctx).search(target) == []  # answered, not "could not be searched"
    assert calls[0] == calls[1]


def test_other_search_errors_are_not_repeated(monkeypatch):
    calls = []

    def broken(ctx, query, label):
        calls.append(query)
        raise RuntimeError("500 Server Error")

    monkeypatch.setattr(slskd_backend, "_execute_search", broken)
    ctx = Context(config=make_config(search={"max_search_fallbacks": "0"}), slskd=Client(), readarr=None)
    target = DownloadTarget(book_id=7, book_title="Endeavour", author_name=NUTTALL, series_title="", allowed_filetypes=["mp3"], media_type="audiobook")
    with pytest.raises(SearchIncomplete, match="Search coverage incomplete"):
        SlskdBackend(ctx).search(target)
    assert len(calls) == len(set(calls))


# ---------------------------------------------------------------------------
# 6. Run summary
# ---------------------------------------------------------------------------


def test_summary_tells_failed_downloads_from_books_not_found(caplog):
    with caplog.at_level(logging.INFO):
        display.print_run_summary(1, 3, [("Marko Kloos", "Corvus")], [("Isaac Asimov", "Robots and Empire")], [("Robert Jordan", "Winter's Heart", "File(s) failed: Completed, Rejected")])
    text = caplog.text
    assert "Not found (1):" in text and "Download failed (1):" in text and "Import failed (1):" in text
    assert "Robert Jordan — Winter's Heart (File(s) failed: Completed, Rejected)" in text
