"""v10.2 regression tests: never turn a targeted import into a folder scan;
verify the actual destination of new files; handle recovery, cleanup and ledger IDs safely.
"""
import configparser
import os
import time

import pytest

from rsoul import import_identity, postprocess, workflow
from rsoul.backends.base import DownloadStatus, DownloadTask
from rsoul.config import Context
from rsoul.ledger import BookLedger
from rsoul.state import StateManager
from test_review_round12 import Chaptarr, NUTTALL, QUALITY, STAGED, run_import


def test_edition_type_is_structured_and_untyped_is_not_audio():
    assert import_identity.edition_type({"readingFormatId": 1}) == "physical"
    assert import_identity.edition_type({"readingFormatId": "2"}) == "audiobook"
    assert import_identity.edition_type({"readingFormatId": 3}) == "ebook"
    assert import_identity.edition_type({"isEbook": True}) == "ebook"
    assert import_identity.edition_type({"isEbook": False, "format": "Audible Audio"}) is None
    assert import_identity.edition_type({"readingFormatId": 99}) == "unknown"


def test_audio_edition_is_chosen_over_monitored_ebook_and_physical():
    book = {"id": 7, "mediaType": "audiobook"}
    editions = [
        {"id": 10, "readingFormatId": 3, "monitored": True},
        {"id": 11, "readingFormatId": 1, "monitored": True},
        {"id": 12, "readingFormatId": 2, "monitored": False},
    ]
    assert import_identity.select_edition(book, editions, "audiobook")["id"] == 12
    assert import_identity.select_edition(book, editions, "ebook") is None
    assert import_identity.select_edition({"id": 7}, editions, "audiobook")["id"] == 12


def test_edition_is_ambiguous_when_two_audio_editions_have_same_monitor_state():
    editions = [
        {"id": 10, "readingFormatId": 2, "monitored": True},
        {"id": 11, "readingFormatId": 2, "monitored": True},
    ]
    assert import_identity.select_edition({"id": 7, "mediaType": "audiobook"}, editions, "audiobook") is None
    assert import_identity.select_edition({"id": 7, "mediaType": "audiobook"}, [{"id": 10, "isEbook": False}], "audiobook") is None
    assert import_identity.select_edition({"id": 7}, [{"id": 10}], "ebook")["id"] == 10  # plain Readarr


def test_targeted_import_selects_audiobook_edition_in_live_workflow(tmp_path):
    editions = [
        {"id": 1, "readingFormatId": 3, "monitored": True},
        {"id": 2, "readingFormatId": 2, "monitored": False},
    ]
    chaptarr = Chaptarr(tmp_path / "dl", editions=editions)
    result, _ = run_import(tmp_path, chaptarr)
    assert result == {4020: True}
    assert chaptarr.commands[0][1]["files"][0]["editionId"] == 2


def test_targeted_import_rejects_untyped_edition_without_scan(tmp_path):
    chaptarr = Chaptarr(tmp_path / "dl", editions=[{"id": 1, "monitored": True, "isEbook": False}])
    result, _ = run_import(tmp_path, chaptarr)
    assert result[4020]["method"] == "targeted"
    assert chaptarr.commands == []


class WrongEdition(Chaptarr):
    def post_command(self, name, **kwargs):
        reply = super().post_command(name, **kwargs)
        if self.bookfiles:
            self.bookfiles[-1]["editionId"] = 999  # Chaptarr wrongly attached to a different edition
        return reply


class WrongBook(Chaptarr):
    def post_command(self, name, **kwargs):
        reply = super().post_command(name, **kwargs)
        if self.bookfiles:
            self.bookfiles[-1]["bookId"] = 77  # server ignored requested bookId
        return reply


@pytest.mark.parametrize("klass", [WrongEdition, WrongBook])
def test_targeted_import_misassignment_is_rejected(tmp_path, klass):
    chaptarr = klass(tmp_path / "dl")
    result, ctx = run_import(tmp_path, chaptarr)
    assert result == {4020: False}
    assert ctx.history.is_failed("peer", "The Trafalgar Gambit")
    assert len(chaptarr.commands) == 1


class NothingVerified(Chaptarr):
    def post_command(self, name, **kwargs):
        reply = super().post_command(name, **kwargs)
        self.bookfiles.clear()  # files moved, but no proof they landed on the wanted record
        return reply


def test_disappeared_files_without_bookfile_readback_remain_pending_then_fail(tmp_path):
    client = NothingVerified(tmp_path / "dl")
    result, _ = run_import(tmp_path, client)
    pending = result[4020]
    assert pending["method"] == "targeted" and pending["command_id"] == 1
    assert pending["edition_id"] == 21610 and pending["before_ids"] == []
    assert postprocess.check_pending_import(client, pending) is None
    assert postprocess.check_pending_import(client, pending, now=pending["since"] + postprocess.PENDING_IMPORT_MAX_AGE + 1) is False
    assert [c[0] for c in client.commands] == ["ManualImport"]


def test_targeted_verification_ignores_existing_files():
    class Bookfiles:
        ver_uri = "/api/v1"

        def _get(self, path, v, params=None):
            return [{"id": 5, "bookId": 10, "editionId": 20}]

    assert import_identity.verify_new_bookfiles(Bookfiles(), 10, 20, [5]) is None
    assert import_identity.verify_new_bookfiles(Bookfiles(), 10, 20, []) is True
    assert import_identity.verify_new_bookfiles(Bookfiles(), 10, 21, []) is False


def test_unsupported_bookfile_readback_keeps_files_staged(tmp_path):
    class NoBookfile(Chaptarr):
        def _get(self, path, ver_uri="", params=None):
            if path == "bookfile":
                raise RuntimeError("bookfile unsupported")
            return super()._get(path, ver_uri, params)

    client = NoBookfile(tmp_path / "dl")
    result, _ = run_import(tmp_path, client)
    assert result[4020]["method"] == "targeted" and result[4020]["command_id"] is None
    assert client.commands == []
    assert os.path.exists(result[4020]["files"][0])


def test_failed_manual_import_without_command_path_moves_staged_files(tmp_path):
    class Failed(Chaptarr):
        def get_command(self, id_):
            return {"id": id_, "status": "completed", "result": "unsuccessful", "body": {}, "message": "Failed to import"}

    client = Failed(tmp_path / "dl", takes_files=False)
    result, ctx = run_import(tmp_path, client)
    assert result == {4020: False}
    assert (tmp_path / "dl" / "failed_imports" / "The Trafalgar Gambit" / "Trafalgar Gambit (Ark Royal 3).mp3").exists()
    assert not (tmp_path / "dl" / "rsoul_audiobooks" / "Christopher G Nuttall" / "The Trafalgar Gambit").exists()


def test_pending_targeted_retry_never_calls_downloaded_books_scan(tmp_path):
    local = tmp_path / "dl"
    staged = local / "rsoul_audiobooks" / "Christopher G Nuttall" / "The Trafalgar Gambit"
    staged.mkdir(parents=True)
    f = staged / "Trafalgar Gambit (Ark Royal 3).mp3"
    f.write_bytes(b"audio")
    p = postprocess.pending_import_record(
        "rsoul_audiobooks/Christopher G Nuttall/The Trafalgar Gambit", "/chaptarr-side", str(local), [str(f)],
        51, method="targeted", book_id=4020, media_type="audiobook", edition_id=21610, before_ids=[],
    )
    class Forgot(Chaptarr):
        def get_command(self, id_):
            import requests
            response = requests.Response()
            response.status_code = 404
            raise requests.HTTPError("gone", response=response)
    client = Forgot(local, takes_files=False)
    updated = postprocess.check_pending_import(client, p)
    assert updated["command_id"] == 1 and updated["submits"] == 2
    assert updated["edition_id"] == 21610 and updated["method"] == "targeted"
    assert [n for n, _ in client.commands] == ["ManualImport"]


def test_ledger_never_suppresses_other_media_or_reused_book_ids(tmp_path):
    ledger = BookLedger(str(tmp_path))
    ledger.note_imported(4, "Author", "A Book", "ebook")
    assert ledger.imported_recently(4, "Author", "A Book", "audiobook", 30) is None
    assert ledger.imported_recently(4, "Author", "Another Book", "ebook", 30) is None
    assert ledger.imported_recently(5, "Author", "A Book", "ebook", 30) is not None
    ledger.note_not_found(10, "Author", "A Book", "ebook")
    assert ledger.not_found_recently(10, 1, "Author", "A Book", "ebook")
    assert not ledger.not_found_recently(10, 1, "Author", "A Book", "audiobook")
    assert not ledger.not_found_recently(10, 1, "Another Author", "A Book", "ebook")
    ledger.note_imported(4, "Author", "Another Book", "audiobook")
    assert ledger.imported_recently(4, "Author", "A Book", "ebook", 30) is None
    assert ledger.imported_recently(4, "Author", "Another Book", "audiobook", 30) is not None


def test_sync_disabled_does_not_write_imported_ledger(tmp_path, monkeypatch):
    cfg = configparser.ConfigParser()
    cfg["Readarr"] = {"disable_sync": "True"}
    cfg["Slskd"] = {"download_dir": str(tmp_path)}
    ledger = BookLedger(str(tmp_path))
    ctx = Context(config=cfg, slskd=None, readarr=None, config_dir=str(tmp_path), ledger=ledger)
    monkeypatch.setattr(workflow.postprocess, "process_imports", lambda *a, **kw: {7: True})
    result = workflow._RunResults(ctx, [])
    result._import(DownloadTask("t", "slskd", DownloadStatus.COMPLETED, "Book", "Author", 7, "book.epub", extra={"media_type": "ebook"}))
    assert ledger.books == {}


def test_disappeared_targeted_files_do_not_masquerade_as_success_after_command_404(tmp_path):
    class Lost:
        def get_command(self, id_):
            import requests
            response = requests.Response()
            response.status_code = 404
            raise requests.HTTPError("not found", response=response)

        ver_uri = "/api/v1"
        def _get(self, path, ver_uri, params=None):
            return []

        def post_command(self, **kw):
            raise AssertionError("No staging files: must not resubmit import")

    p = postprocess.pending_import_record("staged", "/chaptarr", str(tmp_path), [str(tmp_path / "gone.mp3")], 1,
                                          method="targeted", book_id=7, media_type="audiobook", edition_id=20, before_ids=[])
    assert postprocess.check_pending_import(Lost(), p) is None


def test_run_workflow_does_not_redownload_a_book_awaiting_targeted_verification(tmp_path, monkeypatch):
    from test_review_round11 import ctx_for
    from test_review_round10 import wanted

    ctx = ctx_for(tmp_path)
    task = DownloadTask("t", "slskd", DownloadStatus.COMPLETED, "The Trafalgar Gambit", NUTTALL, 4020, "book.mp3")
    task.extra["import_pending"] = postprocess.pending_import_record(
        "staged", "/remote", str(tmp_path), [str(tmp_path / "gone.mp3")], 1,
        method="targeted", book_id=4020, media_type="audiobook", edition_id=21610, before_ids=[],
    )
    ctx.state.add_task(task)
    monkeypatch.setattr(postprocess, "check_pending_import", lambda *args, **kwargs: None)

    class Orch:
        unchecked_task_ids = set()

        def start_targets(self, targets, on_complete=None):
            raise AssertionError("Cannot download a book whose import is still pending")

        def monitor_until(self, tasks, on_complete=None, deadline=None):
            return [], []

    ctx.orchestrator = Orch()
    workflow.run_workflow(ctx, [wanted(4020, "The Trafalgar Gambit", author=NUTTALL)])
    assert ctx.state.has_pending_state()


# ---------------------------------------------------------------------------
# Verification against what Chaptarr really returns
# (BookFileResource in Chaptarr v0.9.1000: no editionId at all, and bookId is 0 when the
# file's edition isn't loaded with it). The read-back must not require either field.
# ---------------------------------------------------------------------------


class BookFileServer:
    ver_uri = "/api/v1"

    def __init__(self, rows):
        self.rows = rows

    def _get(self, path, ver, params=None):
        assert path == "bookfile"
        return self.rows


def chaptarr_row(file_id, book_id=4020):
    return {"id": file_id, "authorId": 37, "bookId": book_id, "path": f"/audiobooks/x/{file_id}.mp3", "size": 1, "quality": {}}


@pytest.mark.parametrize("reported_book", [4020, 0, None])
def test_new_file_on_the_wanted_book_verifies_with_chaptarrs_fields(reported_book):
    row = chaptarr_row(9001, reported_book)
    if reported_book is None:
        del row["bookId"]
    assert import_identity.verify_new_bookfiles(BookFileServer([chaptarr_row(1), row]), 4020, 21610, ["1"]) is True


def test_contradicting_fields_still_fail_where_a_server_reports_them():
    assert import_identity.verify_new_bookfiles(BookFileServer([chaptarr_row(9001, 77)]), 4020, 21610, []) is False
    wrong_edition = dict(chaptarr_row(9001), editionId=999)
    assert import_identity.verify_new_bookfiles(BookFileServer([wrong_edition]), 4020, 21610, []) is False
    right_edition = dict(chaptarr_row(9001), editionId=21610)
    assert import_identity.verify_new_bookfiles(BookFileServer([right_edition]), 4020, 21610, []) is True


def test_nothing_new_on_the_wanted_book_is_not_verified():
    assert import_identity.verify_new_bookfiles(BookFileServer([chaptarr_row(1)]), 4020, 21610, ["1"]) is None
    assert import_identity.verify_new_bookfiles(BookFileServer([]), 4020, 21610, None) is None
