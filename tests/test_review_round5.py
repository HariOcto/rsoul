"""Regression tests for the fifth review round (second outside review).

1. "Title 2" is the next book, not the wanted "Title", in file and folder names alike
2. Only evident chapter numbers group files into one book
3. A folder whose chapters are spread over several audio formats is not downloaded in part
4. Damaged values inside a saved entry are caught; a surprise while resuming drops one download
5. Failures with a possible local cause only pause a peer; wrong content excludes it for good
"""

import json
import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_review_round3 import AUTHOR, MB, Client, Transfers, backend, make_config

from rsoul import download as download_module
from rsoul import postprocess
from rsoul.backends import slskd_backend
from rsoul.backends.base import DownloadTarget
from rsoul.backends.slskd_backend import SlskdBackend
from rsoul.config import Context
from rsoul.history import HistoryManager
from rsoul.match import audiobook_folder_match, book_key, same_book_files, title_segment_match
from rsoul.orchestrator import DownloadOrchestrator
from rsoul.state import StateManager

LIB = "@@s\\Audiobooks"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(slskd_backend.time, "sleep", lambda s: None)
    monkeypatch.setattr(download_module.time, "sleep", lambda s: None)


def target(title="Dune", author="Frank Herbert", formats=("m4b", "mp3")):
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


def matches(path, title="Dune", author="Frank Herbert"):
    t = {"book": {"title": title}, "author": {"authorName": author}}
    return bool(audiobook_folder_match(t, [{"filename": path, "size": 500 * MB}], "peer", ["m4b", "mp3"], [], 0.7))


# ---------------------------------------------------------------------------
# 1. Sequels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        f"{LIB}\\Frank Herbert - Dune 2.m4b",  # the only file a peer has: still not "Dune"
        f"{LIB}\\Frank Herbert - Dune 1.m4b",  # may be part 1 of several: not provably the whole book
        f"{LIB}\\Frank Herbert - Dune_2.m4b",
        "@@s\\Frank Herbert - Dune 2\\01.mp3",  # the same in a folder name
        "@@s\\Frank Herbert\\Dune 3\\01.mp3",
        "@@s\\Frank Herbert\\Dune 2nd\\01.mp3",
    ],
)
def test_bare_number_after_the_title_is_another_book(path):
    assert not matches(path)


@pytest.mark.parametrize(
    "path",
    [
        f"{LIB}\\Frank Herbert - Dune.m4b",
        f"{LIB}\\Frank Herbert - Dune 01.mp3",  # zero-padded: a chapter
        f"{LIB}\\Frank Herbert - Dune - 02.mp3",  # set apart: a chapter
        f"{LIB}\\Frank Herbert - Dune (Part 2 of 5).m4b",
        "@@s\\Frank Herbert\\Dune Book 1\\01.mp3",
        "@@s\\Frank Herbert\\Dune (1965)\\01.mp3",
        "@@s\\Frank Herbert\\Dune 1965\\01.mp3",  # a year
        "@@s\\Frank Herbert\\1 Dune\\01.mp3",  # number in front: position in the series
        "@@s\\Frank Herbert\\Dune Chronicles 1 - Dune\\01.mp3",
    ],
)
def test_numbers_that_are_set_apart_still_match(path):
    assert matches(path)


def test_number_that_belongs_to_the_title_matches():
    assert matches(f"{LIB}\\Joseph Heller - Catch-22.m4b", title="Catch-22", author="Joseph Heller")
    assert matches("@@s\\Ray Bradbury\\Fahrenheit 451\\01.mp3", title="Fahrenheit 451", author="Ray Bradbury")
    assert not matches("@@s\\Ray Bradbury\\Fahrenheit 451 2\\01.mp3", title="Fahrenheit 451", author="Ray Bradbury")


def test_existing_series_number_cases_still_pass():
    assert title_segment_match("Lines of Departure", "Frontlines 2 - Lines of Departure", "Marko Kloos")
    assert title_segment_match("Salvation", "Salvation Sequence 1 - Salvation", "Peter F. Hamilton", "Salvation Sequence")
    assert not title_segment_match("Salvation", "Salvation Lost", "Peter F. Hamilton")


def test_sequel_is_not_downloaded_with_the_book():
    listing = [{"filename": n, "size": 500 * MB} for n in ("Frank Herbert - Dune.m4b", "Frank Herbert - Dune 2.m4b", "Frank Herbert - Dune (2).m4b", "Frank Herbert - Dune Messiah.m4b")]
    client = Client(responses=[{"username": "peer", "files": [{"filename": f"{LIB}\\Frank Herbert - Dune.m4b", "size": 500 * MB}]}], listings={("peer", LIB): listing})
    results = backend(client).search(target())

    assert [f["filename"] for f in results[0].extra["files"]] == [f"{LIB}\\Frank Herbert - Dune.m4b"]


def test_peer_with_only_the_sequel_gives_no_result():
    listing = [{"filename": "Frank Herbert - Dune 2.m4b", "size": 500 * MB}]
    client = Client(responses=[{"username": "peer", "files": [{"filename": f"{LIB}\\Frank Herbert - Dune 2.m4b", "size": 500 * MB}]}], listings={("peer", LIB): listing})
    assert backend(client).search(target()) == []


# ---------------------------------------------------------------------------
# 2. Which numbers make files parts of one book
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a, b, same",
    [
        ("Title - 1.mp3", "Title - 2.mp3", True),  # set apart by a separator
        ("Title 01.mp3", "Title 02.mp3", True),  # zero-padded
        ("Title Part 1.m4b", "Title Part 2.m4b", True),  # named as parts
        ("Title_01.mp3", "Title_02.mp3", True),
        ("Title_1.mp3", "Title_2.mp3", False),  # "_" often stands for a space: as ambiguous as "Title 2"
        ("Title 1.m4b", "Title 2.m4b", False),  # bare numbers: could be two books
        ("Title.m4b", "Title 2.m4b", False),
        ("Catch-22.m4b", "Catch-23.m4b", False),  # the number is part of the name
    ],
)
def test_only_evident_part_numbers_share_a_key(a, b, same):
    assert (book_key(a) == book_key(b)) is same


def test_unpadded_numbers_continue_a_padded_series():
    names = [f"Title {i:02d}.mp3" for i in range(1, 13)] + ["Other 01.mp3"]
    assert same_book_files(["Title 01.mp3"], names) == [f"Title {i:02d}.mp3" for i in range(1, 13)]  # 10-12 included
    names = ["T 001.mp3", "T 099.mp3", "T 100.mp3", "T 2.mp3"]
    assert same_book_files(["T 001.mp3"], names) == ["T 001.mp3", "T 099.mp3", "T 100.mp3"]


def test_numbered_copy_is_not_a_part_of_an_unnumbered_book():
    assert same_book_files(["Dune.m4b"], ["Dune.m4b", "Dune (2).m4b", "Dune - 02.m4b"]) == ["Dune.m4b"]


def test_chapters_ten_and_up_are_downloaded_with_the_rest():
    name = lambda i: f"Frank Herbert - Dune {i:02d}.mp3"
    listing = [{"filename": name(i), "size": 40 * MB} for i in range(1, 13)]
    client = Client(responses=[{"username": "peer", "files": [{"filename": f"{LIB}\\{name(3)}", "size": 40 * MB}]}], listings={("peer", LIB): listing})
    results = backend(client).search(target())

    assert len(results[0].extra["files"]) == 12


# ---------------------------------------------------------------------------
# 3. Chapters spread over several formats
# ---------------------------------------------------------------------------

FOLDER = "@@s\\Brandon Sanderson\\The Final Empire"


def folder_search(names, formats):
    listing = [{"filename": n, "size": 40 * MB} for n in names]
    responses = [{"username": "peer", "files": [{"filename": f"{FOLDER}\\{n}", "size": 40 * MB} for n in names]}]
    client = Client(responses=responses, listings={("peer", FOLDER): listing})
    results = backend(client).search(target("The Final Empire", AUTHOR, formats))
    return [f["filename"].rsplit("\\", 1)[1] for f in results[0].extra["files"]] if results else []


def test_folder_with_chapters_in_two_formats_is_skipped():
    assert folder_search(["01.mp3", "02.m4a"], ("mp3", "m4a")) == []
    assert folder_search(["01.mp3", "02.mp3", "03.m4a", "04.m4a"], ("mp3", "m4a")) == []


def test_split_is_noticed_even_if_the_other_format_is_not_wanted():
    assert folder_search(["01.mp3", "02.wma"], ("mp3",)) == []


def test_second_copy_in_another_format_is_fine():
    # A whole-book .m4b next to the mp3 chapters
    assert folder_search(["01.mp3", "02.mp3", "The Final Empire.m4b"], ("mp3", "m4b")) == ["01.mp3", "02.mp3"]
    # The same chapters twice
    assert folder_search(["01.mp3", "02.mp3", "01.m4a", "02.m4a"], ("mp3", "m4a")) == ["01.mp3", "02.mp3"]
    # A two-part .m4b next to twelve mp3 chapters: the preferred format is complete
    names = [f"{i:02d}.mp3" for i in range(1, 13)] + ["Part 1.m4b", "Part 2.m4b"]
    assert folder_search(names, ("m4b", "mp3")) == ["Part 1.m4b", "Part 2.m4b"]


def test_cover_and_text_files_do_not_count_as_another_format():
    assert folder_search(["01.mp3", "02.mp3", "cover.jpg", "info.nfo"], ("mp3",)) == ["01.mp3", "02.mp3"]


# ---------------------------------------------------------------------------
# 4. Damaged values inside saved entries
# ---------------------------------------------------------------------------

GOOD = {
    "task_id": "peer|a",
    "backend_name": "slskd",
    "book_title": "A",
    "author_name": "X",
    "book_id": 1,
    "filename": "01.mp3",
    "local_dir": "A",
    "extra": {
        "username": "peer",
        "file_dir": "@@s\\A",
        "media_type": "audiobook",
        "expected_files": ["01.mp3"],
        "files": [{"filename": "@@s\\A\\01.mp3", "id": "1", "size": 10, "username": "peer", "file_dir": "@@s\\A", "status": None}],
        "monitor": {"started_at": 1.0, "last_progress_at": 1.0, "queued_since": None},
        "slskd_id": "1",
    },
}


def with_extra(**changes):
    return dict(GOOD, task_id="peer|b", extra=dict(GOOD["extra"], **changes))


@pytest.mark.parametrize(
    "bad",
    [
        with_extra(expected_files=[None]),
        with_extra(expected_files=[""]),
        with_extra(expected_files=["01.mp3", 2]),
        with_extra(files=[{"filename": "a", "id": 5}]),
        with_extra(files=[{"filename": "a", "size": "big"}]),
        with_extra(files=[{"filename": "a", "status": "InProgress"}]),
        with_extra(username=None),
        with_extra(file_dir=3),
        with_extra(monitor="yesterday"),
        with_extra(monitor={"started_at": "noon"}),
        with_extra(import_pending={"local_dir": "/x", "files": [None]}),
        with_extra(import_pending={"local_dir": "/x", "since": "today"}),
        dict(GOOD, task_id="peer|b", local_dir=None),
    ],
)
def test_damaged_value_inside_an_entry_is_caught(tmp_path, bad):
    (tmp_path / "grab_list_state.json").write_text(json.dumps([GOOD, bad]))
    assert [i["task_id"] for i in StateManager(str(tmp_path)).get_items()] == ["peer|a"]


def test_entries_written_by_this_and_older_versions_are_accepted(tmp_path):
    no_ids = dict(GOOD, task_id="old", extra={"username": "peer", "file_dir": "@@s\\A", "media_type": "audiobook", "expected_files": ["01.mp3"]})
    stacks = {"task_id": "md5", "backend_name": "stacks", "book_title": "A", "author_name": "X", "book_id": 2, "filename": "a.epub", "extra": {"md5": "abc", "path": "/x", "isbn": ""}}
    pending = with_extra(import_pending={"folder": "A", "local_dir": "/dl/A", "readarr_path": "/r/A", "files": ["/dl/A/01.mp3"], "command_id": None, "submits": 0, "since": 1.5})
    legacy = {"username": "peer", "filename": "a.epub", "files": []}
    (tmp_path / "grab_list_state.json").write_text(json.dumps([GOOD, no_ids, stacks, pending, legacy]))

    assert len(StateManager(str(tmp_path)).get_items()) == 5
    assert [p.name for p in tmp_path.iterdir()] == ["grab_list_state.json"]


def test_surprise_while_resuming_drops_one_download_not_the_run():
    class Backend:
        name = "slskd"

        def reconcile_task(self, data):
            if data["task_id"] == "bad":
                raise TypeError("unsupported operand type(s) for /: 'PosixPath' and 'NoneType'")
            return SlskdBackend._reconcile_stub(data)

    class Stub:
        pass

    def stub(data):
        task = Stub()
        task.task_id, task.filename = data["task_id"], "f"
        return task

    SlskdBackend._reconcile_stub = staticmethod(stub)
    try:
        orch = DownloadOrchestrator([Backend()], Context(config=make_config(), slskd=None, readarr=None))
        resumed = orch.resume_tasks([{"task_id": "bad", "backend_name": "slskd"}, {"task_id": "good", "backend_name": "slskd"}])
    finally:
        del SlskdBackend._reconcile_stub

    assert [t.task_id for t in resumed] == ["good"]
    assert orch.unchecked_task_ids == set()  # dropped, not kept for another crash next run


# ---------------------------------------------------------------------------
# 5. Which failures exclude a peer for good
# ---------------------------------------------------------------------------


def test_audiobook_that_cannot_be_staged_only_pauses_the_peer(tmp_path):
    local = tmp_path / "dl"
    (local / "Mistborn").mkdir(parents=True)  # the chapter isn't where it should be
    history = HistoryManager(str(tmp_path))
    ctx = Context(config=make_config(slskd={"download_dir": str(local)}), slskd=None, readarr=None, history=history)
    item = {"author_name": AUTHOR, "title": "T", "bookId": 7, "dir": "Mistborn", "filename": "01.mp3", "expected_files": ["01.mp3"], "backend_name": "slskd", "media_type": "audiobook", "source_id": "peer"}

    assert postprocess.process_imports(ctx, [item]) == {7: False}
    assert history.is_failed("peer", "T")
    assert "retry_after" in history.history[0]


def test_ebook_that_is_not_where_expected_only_pauses_the_peer(tmp_path):
    local = tmp_path / "dl"
    local.mkdir()
    history = HistoryManager(str(tmp_path))
    ctx = Context(config=make_config(slskd={"download_dir": str(local)}), slskd=None, readarr=None, history=history)
    item = {"author_name": AUTHOR, "title": "T", "bookId": 7, "dir": "x", "filename": "a.epub", "backend_name": "slskd", "source_id": "peer"}

    postprocess.process_imports(ctx, [item])
    assert "retry_after" in history.history[0]


def test_ebook_with_the_wrong_content_excludes_the_peer_for_good(tmp_path, monkeypatch):
    local = tmp_path / "dl"
    (local / "x").mkdir(parents=True)
    (local / "x" / "a.epub").write_bytes(b"not the book")
    monkeypatch.setattr(postprocess, "validate_metadata", lambda *args, **kwargs: False)
    history = HistoryManager(str(tmp_path))
    ctx = Context(config=make_config(slskd={"download_dir": str(local)}), slskd=None, readarr=None, history=history)
    item = {"author_name": AUTHOR, "title": "T", "bookId": 7, "dir": "x", "filename": "a.epub", "backend_name": "slskd", "source_id": "peer"}

    postprocess.process_imports(ctx, [item])
    assert history.history[0]["reason"] == "Metadata validation failed"
    assert "retry_after" not in history.history[0]
