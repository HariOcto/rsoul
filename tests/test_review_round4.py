"""Regression tests for the fourth review round (points raised by an outside review).

1. A book whose own files are numbered chapters in a shared folder is downloaded completely
2. If the first audiobook candidate can't be started, the next one is tried
3. Untracked transfers are looked for more than once, and files that arrived are moved aside
4. A peer whose files weren't imported is only skipped for a while
5. Damaged entries in the state file are dropped and the file is kept for troubleshooting
"""

import json
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_review_round3 import AUTHOR, LIBRARY, MB, Client, RejectingReadarr, Transfers, audiobook_item, backend, make_config

from rsoul import download as download_module
from rsoul import postprocess
from rsoul.backends import slskd_backend
from rsoul.backends.base import DownloadTarget
from rsoul.config import Context, validate_config
from rsoul.history import HistoryManager
from rsoul.match import audiobook_folder_match, book_key
from rsoul.state import StateManager

TITLE = "The Final Empire"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(slskd_backend.time, "sleep", lambda s: None)
    monkeypatch.setattr(download_module.time, "sleep", lambda s: None)


def target():
    return DownloadTarget(
        book_id=7,
        book_title=TITLE,
        author_name=AUTHOR,
        series_title="",
        allowed_filetypes=["mp3", "m4b"],
        readarr_book={"title": TITLE, "id": 7},
        readarr_author={"authorName": AUTHOR},
        media_type="audiobook",
    )


def chapter(i, directory=LIBRARY):
    return f"{directory}\\Brandon Sanderson - The Final Empire - {i:02d}.mp3"


# ---------------------------------------------------------------------------
# 1. Numbered chapters in a shared folder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a, b, same",
    [
        ("Author - Title - 01.mp3", "Author - Title - 02.mp3", True),
        ("Title (Part 1 of 3).m4b", "Title (Part 2 of 3).m4b", True),
        ("01 - Author - Title.mp3", "02 - Author - Title.mp3", True),
        ("Title_pt1.mp3", "Title_pt2.mp3", True),
        ("Author - Title.m4b", "Author - Other Title.m4b", False),
        ("George Orwell - 1984.m4b", "George Orwell - 1985.m4b", False),  # not chapter numbers
        ("01.mp3", "02.mp3", False),  # nothing but a number: no shared name to go by
    ],
)
def test_book_key_groups_parts_of_one_book(a, b, same):
    assert (book_key(a) == book_key(b)) is same


def shared_folder_client(found=(1,), chapters=12):
    """A peer's "Audiobooks" folder holding one book as numbered chapters next to other books.
    The search only returns the chapters in `found`."""
    listing = [{"filename": chapter(i).rsplit("\\", 1)[1], "size": 40 * MB} for i in range(1, chapters + 1)]
    listing += [{"filename": f"Some Author - Other Book {i}.mp3", "size": 300 * MB} for i in range(5)]
    responses = [{"username": "peer", "files": [{"filename": chapter(i), "size": 40 * MB} for i in found]}]
    return Client(responses=responses, listings={("peer", LIBRARY): listing})


@pytest.mark.parametrize("found", [(1,), (1, 2, 7), tuple(range(1, 13))])
def test_all_chapters_of_a_file_based_book_are_downloaded(found):
    client = shared_folder_client(found)
    results = backend(client).search(target())

    assert len(results) == 1
    names = [f["filename"] for f in results[0].extra["files"]]
    assert names == [chapter(i) for i in range(1, 13)]  # every chapter, and none of the other books
    assert client.users.browsed == [("peer", LIBRARY)]


def test_file_based_book_is_skipped_when_the_folder_cannot_be_listed():
    client = shared_folder_client()
    client.users.listings = {}  # browsing fails: one search hit is no proof of a complete book
    assert backend(client).search(target()) == []


def test_chapter_task_tracks_every_chapter():
    client = shared_folder_client()
    b = backend(client)
    task = b.download(target(), b.search(target())[0])

    assert task.task_id == f"peer|{chapter(1)}"
    assert len(task.extra["files"]) == 12
    assert len(task.extra["expected_files"]) == 12
    assert len(client.transfers.records) == 12


def test_several_search_hits_for_one_book_in_a_shared_folder_match():
    t = {"book": {"title": TITLE}, "author": {"authorName": AUTHOR}}
    files = [{"filename": chapter(i), "size": 40 * MB} for i in (1, 2, 3)] + [{"filename": f"{LIBRARY}\\Brandon Sanderson - Elantris - 01.mp3", "size": 40 * MB}]
    matches = audiobook_folder_match(t, files, "peer", ["mp3"], [], 0.7)

    assert len(matches) == 1
    assert matches[0]["file_based"] is True
    assert [f["filename"] for f in matches[0]["files"]] == [chapter(i) for i in (1, 2, 3)]


# ---------------------------------------------------------------------------
# 2. Next audiobook candidate
# ---------------------------------------------------------------------------


def two_peer_client(transfers):
    directory = "@@s\\Brandon Sanderson\\The Final Empire"
    files = [{"filename": f"{directory}\\{i:02d}.mp3", "size": 40 * MB} for i in range(1, 4)]
    listing = [{"filename": f["filename"].rsplit("\\", 1)[1], "size": f["size"]} for f in files]
    responses = [{"username": "first", "files": files}, {"username": "second", "files": files}]
    return Client(responses=responses, listings={("first", directory): listing, ("second", directory): listing}, transfers=transfers)


class RefusingTransfers(Transfers):
    """slskd accepts the request, but nothing from `refused` ever shows up in the list."""

    def __init__(self, refused):
        super().__init__()
        self.refused = refused

    def enqueue(self, username, files):
        return True if username in self.refused else super().enqueue(username, files)


def test_second_audiobook_candidate_is_used_when_the_first_cannot_be_queued():
    client = two_peer_client(RefusingTransfers(refused={"first"}))
    b = backend(client)
    results = b.search(target())

    assert [r.username for r in results] == ["first"]
    assert client.users.browsed == [("first", "@@s\\Brandon Sanderson\\The Final Empire")]  # the second isn't browsed yet

    task = b.download(target(), results[0])
    assert task is not None
    assert task.extra["username"] == "second"
    assert task.task_id == "second|@@s\\Brandon Sanderson\\The Final Empire"
    assert len(client.users.browsed) == 2


def test_no_task_when_every_candidate_fails():
    client = two_peer_client(RefusingTransfers(refused={"first", "second"}))
    b = backend(client)
    assert b.download(target(), b.search(target())[0]) is None
    assert b._alternates == {}


def test_alternates_of_an_earlier_search_are_not_reused():
    client = two_peer_client(Transfers())
    b = backend(client)
    first = b.search(target())[0]
    b.search(target())  # a second search for the same book replaces the first one's candidates
    assert b.download(target(), first) is not None
    assert b._alternates == {}


# ---------------------------------------------------------------------------
# 3. Untracked transfers
# ---------------------------------------------------------------------------


def test_transfer_appearing_after_the_first_cleanup_check_is_still_cancelled():
    class VeryLateTransfers(Transfers):
        def get_downloads(self, username):
            # Call 1 is the snapshot, 2-5 the polls after enqueueing, 6 the first clean-up check:
            # the record only shows up for the second clean-up check
            listing = super().get_downloads(username)
            return listing if self.list_calls > 6 else {"username": username, "directories": []}

    transfers = VeryLateTransfers()
    files = [{"filename": "@@s\\Mistborn\\01.mp3", "size": 10}]

    assert download_module.slskd_do_enqueue(Client(transfers=transfers), "peer", files, "@@s\\Mistborn") is None
    assert transfers.cancelled == ["new-0"]


def test_files_that_arrived_from_an_abandoned_enqueue_are_moved_aside(tmp_path):
    class ArrivingTransfers(Transfers):
        """Never lists the new transfer, but the file lands in the download folder anyway."""

        def enqueue(self, username, files):
            (tmp_path / "The Final Empire").mkdir(exist_ok=True)
            (tmp_path / "The Final Empire" / "01.mp3").write_bytes(b"audio")
            return True

    client = two_peer_client(ArrivingTransfers())
    client.searches.responses = client.searches.responses[:1]
    b = backend(client, slskd={"download_dir": str(tmp_path)})

    assert b.download(target(), b.search(target())[0]) is None
    assert not (tmp_path / "The Final Empire").exists()  # the next attempt finds a clean folder
    assert (tmp_path / "failed_downloads" / "The Final Empire" / "01.mp3").exists()


# ---------------------------------------------------------------------------
# 4. Import failures don't blacklist a peer for good
# ---------------------------------------------------------------------------


def test_unimported_peer_is_only_skipped_for_a_while(tmp_path):
    local = tmp_path / "dl"
    history = HistoryManager(str(tmp_path))
    ctx = Context(config=make_config(slskd={"download_dir": str(local)}), slskd=None, readarr=RejectingReadarr(), history=history)
    postprocess.process_imports(ctx, [audiobook_item(local)])

    entry = history.history[0]
    assert history.is_failed("peer", "Mistborn: The Final Empire")  # no re-download on the next run...
    days = (datetime.fromisoformat(entry["retry_after"]) - datetime.fromisoformat(entry["timestamp"])).days
    assert days == 7  # ...but not for good: Chaptarr's own setup may have been the problem

    entry["retry_after"] = (datetime.now() - timedelta(minutes=1)).isoformat()
    assert not history.is_failed("peer", "Mistborn: The Final Empire")


def test_retry_period_is_configurable_and_zero_means_for_good(tmp_path):
    local = tmp_path / "dl"
    history = HistoryManager(str(tmp_path))
    config = make_config(slskd={"download_dir": str(local)})
    config["Postprocessing"] = {"import_failure_retry_days": "0"}
    ctx = Context(config=config, slskd=None, readarr=RejectingReadarr(), history=history)
    postprocess.process_imports(ctx, [audiobook_item(local)])

    assert "retry_after" not in history.history[0]
    assert history.is_failed("peer", "Mistborn: The Final Empire")


def test_expired_entry_is_replaced_not_duplicated(tmp_path):
    history = HistoryManager(str(tmp_path))
    history.add_failure("peer", "Book", "Not imported", retry_after_days=7)
    history.history[0]["retry_after"] = (datetime.now() - timedelta(days=1)).isoformat()
    history.add_failure("peer", "Book", "Metadata validation failed")

    assert len(history.history) == 1
    assert "retry_after" not in history.history[0]  # wrong content: skipped for good
    assert HistoryManager(str(tmp_path)).is_failed("peer", "Book")  # and saved


def test_entries_from_older_versions_never_expire(tmp_path):
    (tmp_path / "failed_downloads.json").write_text(json.dumps([{"username": "peer", "book_title": "Book", "timestamp": "2024-01-01T00:00:00"}]))
    assert HistoryManager(str(tmp_path)).is_failed("peer", "Book")


def test_negative_retry_days_are_rejected():
    config = make_config()
    config["Postprocessing"] = {"import_failure_retry_days": "-1"}
    with pytest.raises(ValueError, match="import_failure_retry_days"):
        validate_config(config)


# ---------------------------------------------------------------------------
# 5. Damaged state entries
# ---------------------------------------------------------------------------

GOOD = {"task_id": "peer|a", "backend_name": "slskd", "book_title": "A", "author_name": "X", "book_id": 1, "filename": "a.epub", "extra": {"username": "peer", "files": [{"filename": "d\\a.epub", "id": "1"}]}}


@pytest.mark.parametrize(
    "bad",
    [
        dict(GOOD, task_id="peer|b", extra="oops"),
        dict(GOOD, task_id="peer|b", extra={"files": "oops"}),
        dict(GOOD, task_id="peer|b", extra={"files": [{"id": "1"}]}),
        dict(GOOD, task_id="peer|b", extra={"import_pending": "oops"}),
        dict(GOOD, task_id=None),
        dict(GOOD, task_id="peer|b", filename=None),
    ],
)
def test_damaged_entry_is_dropped_and_the_file_kept(tmp_path, bad):
    path = tmp_path / "grab_list_state.json"
    original = json.dumps([GOOD, bad])
    path.write_text(original)

    state = StateManager(str(tmp_path))

    assert [i["task_id"] for i in state.get_items()] == ["peer|a"]  # the good download is still resumed
    copies = [p for p in tmp_path.iterdir() if ".damaged-" in p.name]
    assert len(copies) == 1 and copies[0].read_text() == original


def test_unreadable_state_file_is_set_aside_not_deleted(tmp_path):
    path = tmp_path / "grab_list_state.json"
    path.write_text('[{"task_id": "x"')  # cut off mid-write

    assert StateManager(str(tmp_path)).get_items() == []
    assert not path.exists()
    copies = [p for p in tmp_path.iterdir() if ".damaged-" in p.name]
    assert len(copies) == 1 and copies[0].read_text() == '[{"task_id": "x"'


def test_intact_state_file_makes_no_copy(tmp_path):
    (tmp_path / "grab_list_state.json").write_text(json.dumps([GOOD]))
    assert len(StateManager(str(tmp_path)).get_items()) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["grab_list_state.json"]
