"""Tests for the twelfth round: what Chaptarr's own log showed about imports.

A folder scan lets Chaptarr decide which book the files are. On the first real runs it filed
"Trafalgar Gambit (Ark Royal 3).mp3" under the book "Ark Royal", and created new records for
six other books while the wanted records stayed wanted.

1. import_method = targeted names the book for the import
2. When that can't be prepared, files remain pending; no dangerous scan fallback
3. An import that lands on another record is not falsely marked successful
"""

import logging
import os
import sys
import copy
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_download_cleanup_and_retries import make_config

from rsoul import postprocess
from rsoul.config import Context, validate_config
from rsoul.history import HistoryManager

NUTTALL = "Christopher G. Nuttall"
QUALITY = {"quality": {"id": 10, "name": "MP3"}, "revision": {"version": 1, "real": 0}}


class Chaptarr:
    """Enough of Readarr/Chaptarr for both import methods."""

    ver_uri = "/api/v1"

    def __init__(self, local_root, editions=None, preview=None, has_files=True, takes_files=True, preview_error=None):
        self.local_root = str(local_root)
        self.editions = [{"id": 21610, "foreignEditionId": "az:B00KS8G4ZO", "monitored": True, "readingFormatId": 2}] if editions is None else editions
        self.preview = preview
        self.preview_error = preview_error
        self.has_files = has_files
        self.takes_files = takes_files
        self.commands = []
        self.previews = []
        self.bookfiles = []
        self.events = []

    def get_book(self, book_id):
        return {"id": book_id, "title": "The Trafalgar Gambit", "authorId": 37, "hasFiles": self.has_files}

    def get_edition(self, book_id):
        return self.editions

    def get_author(self, author_id):
        return {"id": 37, "authorName": NUTTALL}

    def _get(self, path, ver_uri="", params=None):
        if path == "bookfile":
            return list(self.bookfiles)
        if path == "history":
            return {"records": list(self.events), "totalRecords": len(self.events)}
        assert path == "manualimport"
        self.previews.append(params)
        if self.preview_error:
            raise self.preview_error
        if self.preview is not None:
            preview = copy.deepcopy(self.preview)
            if isinstance(preview, list):
                for row in preview:
                    if isinstance(row, dict) and isinstance(row.get("path"), str) and row["path"].startswith(STAGED + "/"):
                        row["path"] = params["folder"] + row["path"][len(STAGED):]
            return preview
        folder = params["folder"]
        local = folder.replace("/chaptarr-side", self.local_root)
        return [{"path": f"{folder}/{name}", "quality": QUALITY, "indexerFlags": 0} for name in sorted(os.listdir(local))]

    def post_command(self, name, **kwargs):
        self.commands.append((name, kwargs))
        if self.takes_files:
            entries = kwargs["files"] if name == "ManualImport" else [
                {"path": kwargs["path"] + "/" + f, "bookId": 4020, "editionId": 21610}
                for f in os.listdir(kwargs["path"].replace("/chaptarr-side", self.local_root))]
            for entry in entries:
                source = entry["path"].replace("/chaptarr-side", self.local_root)
                file_id = len(self.bookfiles) + 100
                output = f"/server-library/{file_id}.mp3"
                if self.has_files:
                    self.bookfiles.append({"id": file_id, "bookId": entry["bookId"], "editionId": entry["editionId"],
                                           "path": output, "size": os.path.getsize(source)})
                    self.events.append({"id": file_id, "bookId": entry["bookId"], "date": datetime.now(timezone.utc).isoformat(),
                                        "eventType": "bookFileImported", "data": {"FileId": str(file_id), "DroppedPath": entry["path"], "ImportedPath": output}})
                os.remove(source)
        return {"id": len(self.commands), "body": {"path": kwargs.get("path", "")}}

    def get_command(self, id_):
        return {"id": id_, "status": "completed", "result": "successful", "message": "", "body": {"path": ""}}


def run_import(tmp_path, chaptarr, method="targeted", files=("Trafalgar Gambit (Ark Royal 3).mp3",)):
    local = tmp_path / "dl"
    (local / "Christopher G Nuttall").mkdir(parents=True, exist_ok=True)
    for name in files:
        (local / "Christopher G Nuttall" / name).write_bytes((Path(__file__).parent / "fixtures" / "silence.mp3").read_bytes())
    config = make_config(slskd={"download_dir": str(local), "readarr_download_dir": "/chaptarr-side"})
    config["Readarr"]["import_method"] = method
    config["Readarr"]["import_poll_timeout"] = "0"
    config["Postprocessing"] = {"audiobook_tag_check": "off"}
    ctx = Context(config=config, slskd=None, readarr=chaptarr, history=HistoryManager(str(tmp_path)))
    item = {"author_name": NUTTALL, "title": "The Trafalgar Gambit", "bookId": 4020, "dir": "Christopher G Nuttall", "filename": files[0], "expected_files": list(files), "backend_name": "slskd", "media_type": "audiobook", "source_id": "peer"}
    return postprocess.process_imports(ctx, [item]), ctx


STAGED = "/chaptarr-side/rsoul_audiobooks/Christopher G Nuttall/The Trafalgar Gambit"


# ---------------------------------------------------------------------------
# 1. Targeted import
# ---------------------------------------------------------------------------


def test_targeted_import_names_author_book_and_edition(tmp_path):
    chaptarr = Chaptarr(tmp_path / "dl")
    result, _ = run_import(tmp_path, chaptarr)

    assert result == {4020: True}
    staged = chaptarr.previews[0]["folder"]
    assert staged.startswith(STAGED + " (") and staged.endswith(")")
    assert chaptarr.previews == [{"folder": staged, "filterExistingFiles": "false"}]
    assert len(chaptarr.commands) == 1
    name, body = chaptarr.commands[0]
    assert name == "ManualImport"
    assert body["importMode"] == "move" and body["replaceExistingFiles"] is False
    assert body["files"] == [
        {
            "path": f"{staged}/Trafalgar Gambit (Ark Royal 3).mp3",
            "authorId": 37,
            "bookId": 4020,  # the record that was wanted, not whatever the tags suggest
            "editionId": 21610,
            "foreignEditionId": "az:B00KS8G4ZO",
            "quality": QUALITY,
            "releaseGroup": None,
            "indexerFlags": 0,
            "disableReleaseSwitching": True,
        }
    ]


def test_targeted_import_covers_every_chapter_and_nothing_else(tmp_path):
    names = ("01.mp3", "02.mp3", "03.mp3")
    chaptarr = Chaptarr(tmp_path / "dl")
    stray = [{"path": f"{STAGED}/{n}", "quality": QUALITY} for n in names] + [{"path": f"{STAGED}/cover.jpg", "quality": QUALITY}, {"path": f"{STAGED}/old.mp3", "quality": QUALITY}]
    chaptarr.preview = stray
    result, _ = run_import(tmp_path, chaptarr, files=names)

    assert result == {4020: True}
    assert [os.path.basename(f["path"]) for f in chaptarr.commands[0][1]["files"]] == list(names)


def test_monitored_edition_is_preferred(tmp_path):
    editions = [{"id": 1, "foreignEditionId": "a", "monitored": False, "readingFormatId": 2}, {"id": 2, "foreignEditionId": "b", "monitored": True, "readingFormatId": 2}]
    chaptarr = Chaptarr(tmp_path / "dl", editions=editions)
    run_import(tmp_path, chaptarr)
    assert chaptarr.commands[0][1]["files"][0]["editionId"] == 2


def test_explicit_scan_is_supported_with_complete_correlation(tmp_path):
    chaptarr = Chaptarr(tmp_path / "dl")
    result, _ = run_import(tmp_path, chaptarr, method="scan")
    assert result == {4020: True}
    assert chaptarr.previews == []
    assert len(chaptarr.commands) == 1 and chaptarr.commands[0][0] == "DownloadedBooksScan"
    assert chaptarr.commands[0][1]["path"].startswith(STAGED + " (")


# ---------------------------------------------------------------------------
# 2. Fail closed instead of falling back to a scan
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"editions": []},  # the book has no edition to import for
        {"preview": []},  # Chaptarr lists nothing importable
        {"preview": [{"path": STAGED + "/Trafalgar Gambit (Ark Royal 3).mp3"}]},  # no quality given
        {"preview": {"message": "unexpected"}},
        {"preview_error": RuntimeError("404 Not Found: no such endpoint")},  # a server without manual import
    ],
)
def test_import_that_cannot_be_targeted_stays_pending(tmp_path, kwargs):
    chaptarr = Chaptarr(tmp_path / "dl", **kwargs)
    result, _ = run_import(tmp_path, chaptarr)
    assert isinstance(result[4020], dict) and result[4020]["method"] == "targeted"
    assert chaptarr.commands == []
    assert Path(result[4020]["local_dir"]).is_dir()


def test_failed_targeted_import_is_retained_without_peer_penalty(tmp_path):
    # Chaptarr accepts the command but takes no file: unverified, never silently scanned.
    chaptarr = Chaptarr(tmp_path / "dl", takes_files=False)
    result, ctx = run_import(tmp_path, chaptarr)
    assert result[4020]["phase"] == "needs_review"
    assert [name for name, _ in chaptarr.commands] == ["ManualImport"]
    assert Path(result[4020]["files"][0]).exists()
    assert not ctx.history.is_failed("peer", "The Trafalgar Gambit")


def test_folder_with_several_books_is_not_scanned_in_targeted_mode(tmp_path):
    chaptarr = Chaptarr(tmp_path / "dl")
    commands = postprocess.trigger_imports(chaptarr, "/chaptarr-side", ["Author"], "targeted", {"Author": [1, 2]}, {"Author": ["/x/a.epub", "/x/b.epub"]})
    assert chaptarr.commands == [] and commands == []


def test_import_method_is_validated():
    config = make_config()
    config["Readarr"]["import_method"] = "manual"
    with pytest.raises(ValueError, match="import_method"):
        validate_config(config)
    config["Readarr"]["import_method"] = "Targeted"
    validate_config(config)
    assert postprocess.import_method(config) == "targeted"


# ---------------------------------------------------------------------------
# 3. Imported, but for another record
# ---------------------------------------------------------------------------


def test_import_that_lands_on_another_record_is_reported(tmp_path, caplog):
    chaptarr = Chaptarr(tmp_path / "dl", has_files=False)  # the wanted record stays empty
    with caplog.at_level(logging.WARNING):
        result, _ = run_import(tmp_path, chaptarr, method="scan")
    assert result[4020]["phase"] == "needs_review"  # cannot be recorded as imported
    assert result[4020]["verification"]["status"] == "unverified"
    assert "Legacy folder scan" in caplog.text


def test_no_report_when_the_wanted_record_has_the_files(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        run_import(tmp_path, Chaptarr(tmp_path / "dl"), method="scan")
    assert "not for the record that was wanted" not in caplog.text


@pytest.mark.parametrize("book, empty", [({"hasFiles": False}, True), ({"hasFiles": True}, False), ({"statistics": {"bookFileCount": 0}}, True), ({"statistics": {"bookFileCount": 2}}, False), ({}, False), (None, False)])
def test_record_is_only_called_empty_when_the_server_says_so(book, empty):
    class Client:
        def get_book(self, book_id):
            return book

    assert postprocess.wanted_record_still_empty(Client(), 1) is empty


def test_check_survives_an_unreachable_server():
    class Client:
        def get_book(self, book_id):
            raise ConnectionError("down")

    assert postprocess.wanted_record_still_empty(Client(), 1) is False
