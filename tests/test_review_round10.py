"""Regression tests for the tenth round: findings from the first real runs on TrueNAS.

1. Several wanted books from one folder of a peer can download at the same time
2. Short titles named only by the file ("Cry Wolf (Empire's Corps 15).mp3" in the author's
   folder) are matched
3. A title that is wanted twice, is already downloading, or already has files under another
   record is not downloaded again
4. A failed audiobook import goes to <download_dir>/failed_imports
5. Other people's "CD2" folders are not logged one by one
"""

import logging
import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_review_round3 import MB, Client, Transfers, make_config

from rsoul import download as download_module
from rsoul import postprocess, workflow
from rsoul.backends import slskd_backend
from rsoul.backends.base import DownloadTarget
from rsoul.backends.slskd_backend import SlskdBackend
from rsoul.config import Context
from rsoul.library import record_with_files
from rsoul.match import audiobook_folder_match
from rsoul.orchestrator import DownloadOrchestrator
from rsoul.state import StateManager

NUTTALL = "Christopher G. Nuttall"
PEER_DIR = "Audio books Sorting\\TE_Audiobooks_C\\Christopher G Nuttall"
THRESHOLDS = dict(ignored_users=[], minimum_match_ratio=0.7, min_length_ratio=0.4, min_jaccard_ratio=0.25, min_word_overlap=2, min_title_jaccard=0.3, min_author_jaccard=0.5)  # as shipped in config.ini


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(slskd_backend.time, "sleep", lambda s: None)
    monkeypatch.setattr(download_module.time, "sleep", lambda s: None)


def target(book_id, title, author=NUTTALL):
    book = {"title": title, "id": book_id}
    return DownloadTarget(book_id=book_id, book_title=title, author_name=author, series_title="", allowed_filetypes=["mp3", "m4b"], readarr_book=book, readarr_author={"authorName": author}, media_type="audiobook")


# ---------------------------------------------------------------------------
# 1. Books from one folder of a peer
# ---------------------------------------------------------------------------

SHELF = ["A Savage War Of Peace (Ark Royal 5).mp3", "A Small Colonial War (Ark Royal 6).mp3", "Fear God And Dread Naught (Ark Royal 8).mp3"]


class OneQueryClient(Client):
    """A peer whose author folder holds several books; each search returns the file asked for."""

    def __init__(self, transfers=None):
        listing = [{"filename": name, "size": 180 * MB} for name in SHELF]
        super().__init__(listings={("peer", PEER_DIR): listing}, transfers=transfers)
        self.searches.search_text = self._search
        self.searches.search_responses = lambda search_id: self._responses

    def _search(self, **kwargs):
        text = kwargs["searchText"].lower()
        hits = [name for name in SHELF if name.split(" (")[0].lower() in text]
        self._responses = [{"username": "peer", "files": [{"filename": f"{PEER_DIR}\\{name}", "size": 180 * MB} for name in hits]}]
        return {"id": "s"}


def started_through_orchestrator(tmp_path, titles, transfers=None):
    client = OneQueryClient(transfers)
    ctx = Context(config=make_config(), slskd=client, readarr=None, config_dir=str(tmp_path), state=StateManager(str(tmp_path)))
    ctx.orchestrator = DownloadOrchestrator([SlskdBackend(ctx)], ctx)
    ctx.orchestrator.backends[0].is_available = lambda: True
    failed = []
    tasks = ctx.orchestrator.start_targets([target(i, t) for i, t in enumerate(titles, start=1)], on_complete=failed.append)
    return tasks, failed, client


def test_books_from_the_same_peer_folder_download_side_by_side(tmp_path):
    tasks, failed, client = started_through_orchestrator(tmp_path, ["A Savage War Of Peace", "A Small Colonial War", "Fear God and Dread Naught"])

    assert failed == []
    assert [t.filename for t in tasks] == SHELF
    assert {t.local_dir for t in tasks} == {"Christopher G Nuttall"}  # one local folder for all three
    assert [t.extra["expected_files"] for t in tasks] == [[name] for name in SHELF]  # each only moves its own file
    assert len(client.transfers.records) == 3


def test_someone_elses_download_in_the_folder_still_blocks(tmp_path):
    # e.g. Soularr, which deletes the whole local folder of an album it gives up on
    transfers = Transfers()
    transfers.records = {"x": {"username": "peer", "filename": f"{PEER_DIR}\\Some Album Track.mp3", "size": 5, "state": "InProgress", "bytesTransferred": 1}}
    tasks, failed, _ = started_through_orchestrator(tmp_path, ["A Small Colonial War"], transfers)

    assert tasks == []
    assert failed[0].extra["start_failure"] == "not_started"  # found, and kept on the wanted list


def test_the_same_file_twice_still_clashes(tmp_path):
    # Two wanted records for one book would otherwise queue the same file twice
    tasks, failed, client = started_through_orchestrator(tmp_path, ["A Small Colonial War", "A Small Colonial War"])
    assert len(tasks) == 1
    assert len(client.transfers.records) == 1


# ---------------------------------------------------------------------------
# 2. Short titles named only by the file
# ---------------------------------------------------------------------------


def file_match(title, filename, author=NUTTALL, directory=PEER_DIR):
    t = {"book": {"title": title}, "author": {"authorName": author}}
    return [m["score"] for m in audiobook_folder_match(t, [{"filename": f"{directory}\\{filename}", "size": 180 * MB}], "peer", ["mp3", "m4b"], **THRESHOLDS)]


@pytest.mark.parametrize(
    "title, filename",
    [
        ("Ark Royal", "Ark Royal (Ark Royal 1).mp3"),
        ("Cry Wolf", "Cry Wolf (Empire's Corps 15).mp3"),
        ("Semper Fi", "Semper Fi (Empire's Corps 4).mp3"),
        ("Invincible", "Invincible (Ark Royal 12).mp3"),
        ("Endeavour", "Endeavour (Ark Royal 19).mp3"),
        ("A Small Colonial War", "A Small Colonial War (Ark Royal 6).mp3"),
    ],
)
def test_short_title_in_the_authors_folder_matches(title, filename):
    assert file_match(title, filename) == [1.0]


def test_one_word_title_in_the_authors_folder_matches():
    assert file_match("Corvus", "Corvus (Frontlines 7.5).mp3", "Marko Kloos", "Audio books Sorting\\TE_Audiobooks_M\\Marko Kloos") == [1.0]
    assert file_match("Dune", "Dune.m4b", "Frank Herbert", "@@x\\Audiobooks\\Herbert, Frank") == [1.0]


@pytest.mark.parametrize(
    "title, filename",
    [
        ("Ark Royal", "The Nelson Touch (Ark Royal 2).mp3"),  # the series in brackets is not the title
        ("Ark Royal", "Ark Royal 2 - The Nelson Touch.mp3"),
        ("Invincible", "Invincible Summer.mp3"),
        ("Cry Wolf", "Cry Wolf Hollow.mp3"),
    ],
)
def test_other_books_in_the_authors_folder_still_do_not_match(title, filename):
    assert file_match(title, filename) == []


def test_title_in_another_authors_folder_still_does_not_match():
    assert file_match("Invincible", "Invincible.mp3", directory="Audio books Sorting\\TE_Audiobooks_J\\Jack Campbell") == []


# ---------------------------------------------------------------------------
# 3. Duplicate records
# ---------------------------------------------------------------------------


class Chaptarr:
    def __init__(self, records=()):
        self.records = list(records)
        self.ver_uri = "/api/v1"

    def _get(self, path, ver_uri="", params=None):
        return [dict(r, authorId=params["authorId"]) for r in self.records]

    def get_edition(self, book_id):
        return []


def wanted(book_id, title, media_type="audiobook", author="Isaac Asimov"):
    return {"book": {"id": book_id, "title": title, "authorId": 3, "mediaType": media_type}, "author": {"authorName": author}}


def built(tmp_path, books, records=(), mode="audiobook", state_items=()):
    config = make_config(search={"media_mode": mode})
    state = StateManager(str(tmp_path))
    state.items = list(state_items)
    ctx = Context(config=config, slskd=None, readarr=Chaptarr(records), config_dir=str(tmp_path), state=state)
    return [t.book_id for t in workflow.build_targets(ctx, books)]


def test_same_title_wanted_twice_is_downloaded_once(tmp_path, caplog):
    books = [wanted(1, "Robots and Empire"), wanted(2, "Robots and Empire"), wanted(3, "Robot Dreams"), wanted(4, "Robots & Empire")]
    with caplog.at_level(logging.WARNING):
        assert built(tmp_path, books) == [1, 3]
    assert "Skipping Isaac Asimov - Robots and Empire (book 2)" in caplog.text


def test_same_title_by_another_author_is_not_a_duplicate(tmp_path):
    books = [wanted(1, "Salvation"), wanted(2, "Salvation", author="Peter F. Hamilton")]
    assert built(tmp_path, books) == [1, 2]


def test_title_already_downloading_from_an_earlier_run_is_not_started_again(tmp_path):
    saved = {"task_id": "peer|x", "backend_name": "slskd", "book_id": 1, "book_title": "Robots and Empire", "author_name": "Isaac Asimov", "filename": "a.m4b", "extra": {"media_type": "audiobook"}}
    # the saved book itself (id 1) is kept for the workflow, which resumes it; its twin (id 2) is not started
    assert built(tmp_path, [wanted(1, "Robots and Empire"), wanted(2, "Robots and Empire"), wanted(3, "Robot Dreams")], state_items=[saved]) == [1, 3]


def test_title_that_already_has_files_under_another_record_is_skipped(tmp_path, caplog):
    records = [{"id": 1, "title": "Salvation", "mediaType": "audiobook", "hasFiles": True}, {"id": 2, "title": "Salvation", "mediaType": "audiobook", "hasFiles": False}]
    with caplog.at_level(logging.WARNING):
        assert built(tmp_path, [wanted(2, "Salvation"), wanted(5, "Salvation Lost")], records) == [5]
    assert "already has files for another record of this title (book 1)" in caplog.text


def test_the_ebook_of_an_audiobook_is_not_a_duplicate(tmp_path):
    records = [{"id": 1, "title": "Salvation", "mediaType": "ebook", "hasFiles": True}, {"id": 2, "title": "Salvation", "mediaType": "audiobook"}]
    assert built(tmp_path, [wanted(2, "Salvation")], records) == [2]


def test_record_with_files_details():
    ctx = Context(config=make_config(), slskd=None, readarr=Chaptarr([{"id": 1, "title": "Salvation", "statistics": {"bookFileCount": 1}}, {"id": 2, "title": "Salvation"}]))
    assert record_with_files(ctx, {"id": 2, "title": "Salvation", "authorId": 3})["id"] == 1  # plain Readarr: file count, no media type
    assert record_with_files(ctx, {"id": 1, "title": "Salvation", "authorId": 3}) == {}  # a record is not its own duplicate
    assert record_with_files(ctx, {"id": 9, "title": "Salvation Lost", "authorId": 3}) == {}
    assert record_with_files(Context(config=make_config(), slskd=None, readarr=object()), {"id": 2, "title": "Salvation", "authorId": 3}) == {}


def test_record_of_the_other_media_type_is_left_out_of_a_single_type_run(tmp_path, caplog):
    books = [wanted(1, "Salvation", "ebook"), wanted(2, "Salvation", "audiobook")]
    with caplog.at_level(logging.WARNING):
        assert built(tmp_path, books, mode="audiobook") == [2]
    assert "it is an ebook record, and media_mode = audiobook" in caplog.text
    assert built(tmp_path, books, mode="ebook") == [1]
    assert built(tmp_path, books, mode="both") == [1, 2]  # one of each is not a duplicate


def test_plain_readarr_books_without_media_type_are_unaffected(tmp_path):
    books = [{"book": {"id": 1, "title": "Dune", "authorId": 3}, "author": {"authorName": "Frank Herbert"}}]
    assert built(tmp_path, books, mode="ebook") == [1]
    assert built(tmp_path, books, mode="audiobook") == [1]


# ---------------------------------------------------------------------------
# 4. Where a failed audiobook import goes
# ---------------------------------------------------------------------------


def test_failed_audiobook_import_goes_to_the_download_folders_failed_imports(tmp_path):
    staged = tmp_path / postprocess.AUDIOBOOK_STAGING_DIR / "Isaac Asimov" / "Robot Dreams"
    staged.mkdir(parents=True)
    (staged / "Robot Dreams (Robot 0.4).mp3").write_bytes(b"x")

    postprocess.move_failed_import(str(staged))

    assert (tmp_path / "failed_imports" / "Robot Dreams" / "Robot Dreams (Robot 0.4).mp3").exists()
    assert not (tmp_path / postprocess.AUDIOBOOK_STAGING_DIR / "Isaac Asimov").exists()  # no empty author folder, no nested failed_imports


def test_other_books_of_the_author_stay_in_the_staging_area(tmp_path):
    author = tmp_path / postprocess.AUDIOBOOK_STAGING_DIR / "Isaac Asimov"
    for title in ("Robot Dreams", "Robots and Empire"):
        (author / title).mkdir(parents=True)
        (author / title / "book.mp3").write_bytes(b"x")

    postprocess.move_failed_import(str(author / "Robot Dreams"))
    assert (author / "Robots and Empire" / "book.mp3").exists()
    assert (tmp_path / "failed_imports" / "Robot Dreams" / "book.mp3").exists()


def test_failed_ebook_import_location_is_unchanged(tmp_path):
    (tmp_path / "Isaac Asimov").mkdir()
    (tmp_path / "Isaac Asimov" / "book.epub").write_bytes(b"x")
    postprocess.move_failed_import(str(tmp_path / "Isaac Asimov"))
    assert (tmp_path / "failed_imports" / "Isaac Asimov" / "book.epub").exists()


# ---------------------------------------------------------------------------
# 5. Log noise
# ---------------------------------------------------------------------------


def test_disc_folders_of_other_artists_are_not_logged(caplog):
    t = {"book": {"title": "Cry Wolf"}, "author": {"authorName": NUTTALL}}
    files = [
        {"filename": "@@x\\Music\\Various Artists - c86 [Disc 2]\\01 cry.mp3", "size": 5 * MB},
        {"filename": "@@y\\Christopher Nuttall\\Cry Wolf\\CD1\\01.mp3", "size": 5 * MB},
    ]
    stats = {}
    with caplog.at_level(logging.INFO):
        assert audiobook_folder_match(t, files, "peer", ["mp3"], stats=stats, **THRESHOLDS) == []
    assert "c86" not in caplog.text
    assert "Cry Wolf\\CD1" in caplog.text  # the author's own disc folder is still explained
    assert stats == {"disc/part folders": 1, "folders without the author's surname": 1}
