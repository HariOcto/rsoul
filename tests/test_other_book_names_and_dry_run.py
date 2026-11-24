"""Regression tests for the sixth round: an audit by class of problem, and the dry run.

1. Names that say a different book than the one wanted (series named after their first
   book, collections, parent folders), checked as a table rather than one case at a time
2. A book that could not be searched for, or not queued, is not taken off the wanted list
3. Every number and switch in the configuration is checked at startup
4. The dry run changes nothing (see also tests/test_e2e.py::test_dry_run_end_to_end)
"""

import configparser
import os
import sys

import pytest
from rsoul.outcomes import SearchIncomplete

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_download_cleanup_and_retries import MB, Client, PagedReadarr, backend, make_config, paged_ctx, sample_config

from rsoul import download as download_module
from rsoul import workflow
from rsoul.backends import slskd_backend
from rsoul.backends.base import DownloadStatus, DownloadTarget, DownloadTask, SearchResult
from rsoul.config import Context, validate_config
from rsoul.dryrun import is_dry_run
from rsoul.match import audiobook_folder_match, series_positions, title_segment_match
from rsoul.orchestrator import DownloadOrchestrator
from rsoul.search import get_books
from rsoul.state import StateManager


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(slskd_backend.time, "sleep", lambda s: None)
    monkeypatch.setattr(download_module.time, "sleep", lambda s: None)


# ---------------------------------------------------------------------------
# 1. Names that say another book
# ---------------------------------------------------------------------------

BROWN = "Pierce Brown"
RR1 = "Red Rising Saga #1"  # how Readarr/Chaptarr report the first book's series
RR2 = "Red Rising Saga #2"

# (wanted title, series as reported, name of the folder or file, is it this book?)
NAMES = [
    # A series named after its first book: the series name is not the book
    ("Red Rising", "", "Red Rising 02 - Golden Son", False),
    ("Red Rising", "", "Red Rising 2 - Golden Son", False),
    ("Red Rising", "", "Red Rising - Golden Son", False),
    ("Red Rising", "", "Red Rising 02", False),
    ("Red Rising", "", "Red Rising 2", False),
    ("Red Rising", "", "Red Rising II", False),
    ("Red Rising", "", "Red Rising 01 - Red Rising", True),
    ("Red Rising", "", "Red Rising 1 - Red Rising", True),
    ("Red Rising", "", "Red Rising", True),
    ("Golden Son", "", "Red Rising 02 - Golden Son", True),
    ("Golden Son", "", "Pierce Brown - Red Rising 2 - Golden Son", True),
    # Collections: a series word behind the title without a book number
    ("Red Rising", "", "Red Rising Trilogy", False),
    ("Red Rising", "", "Red Rising Saga", False),
    ("Red Rising", "", "Red Rising Series", False),
    ("Red Rising", "", "Red Rising Saga (Books 1-6)", False),
    ("Red Rising", "", "Red Rising - Complete Series", False),
    ("Red Rising", RR1, "Red Rising Saga", False),
    ("Dune", "Dune Chronicles #1", "Dune Chronicles", False),
    ("Dune", "Dune Chronicles #1", "Dune Chronicles 1 - Dune", True),
    # The series with the book's number behind the title is this book
    ("Red Rising", RR1, "Red Rising Saga, Book 1", True),
    ("Red Rising", RR1, "Red Rising (Red Rising Saga #1)", True),
    ("Knife Edge", "Empire's Corps", "Christopher G. Nuttall - Knife Edge Empire's Corps, Book 17", True),
    ("Golden Son", RR2, "Golden Son - Red Rising, Book 2", True),
    ("Golden Son", RR2, "Golden Son (Red Rising Saga #2)", True),
    ("Golden Son", RR2, "Red Rising Saga Book 2 - Golden Son", True),
    # The position, where Readarr/Chaptarr gave one, rules out another book's number
    ("Red Rising", RR1, "Red Rising, Book 2", False),
    ("Red Rising", RR1, "Red Rising Book 2", False),
    ("Red Rising", RR1, "Red Rising (Book 2)", False),
    ("Red Rising", RR1, "Red Rising [Red Rising Saga #2]", False),
    ("Red Rising", RR1, "Red Rising Book 1", True),
    ("Red Rising", "", "Red Rising Book 2", True),  # position unknown: can't be told apart from "Golden Son Book 2"
    # What comes after the title
    ("Red Rising", "", "Red Rising - Unabridged - 2014", True),
    ("Red Rising", "", "Red Rising - Pierce Brown", True),
    ("Red Rising", "", "Red Rising - read by Tim Gerard Reynolds", True),
    ("Red Rising", "", "Red Rising - narrated by Tim Gerard Reynolds", True),
    ("Red Rising", "", "Red Rising - Tim Gerard Reynolds", False),  # a name or another title: can't be told apart
    ("Red Rising", "", "Red Rising (Unabridged) [Tim Gerard Reynolds]", True),
    # Earlier behaviour that must stay
    ("Lines of Departure", "", "Frontlines 2 - Lines of Departure", True),
    ("Lines of Departure", "", "Marko-Kloos-Lines-of-Departure-Unabr", True),
    ("Salvation", "Salvation Sequence", "Salvation Sequence 1 - Salvation", True),
    ("Salvation", "", "The Saints of Salvation", False),
    ("Salvation", "", "Salvation Lost", False),
    ("Dune", "", "Dune 1965", True),
    ("Catch-22", "", "Joseph Heller - Catch-22", True),
]


@pytest.mark.parametrize("title, series, name, same_book", NAMES)
def test_name_says_this_book_or_another(title, series, name, same_book):
    author = "Marko Kloos" if "Kloos" in name else "Christopher G. Nuttall" if "Nuttall" in name else BROWN
    assert title_segment_match(title, name, author, series) is same_book


def test_series_positions():
    assert series_positions("Red Rising Saga #1") == {1}
    assert series_positions("The Stormlight Archive #2; The Cosmere #5") == {2, 5}
    assert series_positions("Empire's Corps") == set()
    assert series_positions("") == set()
    assert series_positions(None) == set()


def folder_matches(path, title, author, series=""):
    t = {"book": {"title": title, "seriesTitle": series}, "author": {"authorName": author}}
    return [m["file_based"] for m in audiobook_folder_match(t, [{"filename": path, "size": 500 * MB}], "peer", ["m4b", "mp3"], [], 0.7)]


def test_parent_folder_named_like_the_title_does_not_make_the_match():
    # A series folder named after its first book, holding the other books
    assert folder_matches("@@s\\Isaac Asimov\\Foundation\\Second Foundation\\01.mp3", "Foundation", "Isaac Asimov") == []
    assert folder_matches("@@s\\Isaac Asimov\\Foundation\\Foundation and Empire\\01.mp3", "Foundation", "Isaac Asimov") == []
    assert folder_matches("@@s\\Pierce Brown\\Red Rising\\Golden Son\\01.mp3", "Red Rising", BROWN) == []
    # ...while the book's own folder inside it still matches
    assert folder_matches("@@s\\Pierce Brown\\Red Rising\\Red Rising\\01.mp3", "Red Rising", BROWN) == [False]
    assert folder_matches("@@s\\Isaac Asimov\\Foundation\\01.mp3", "Foundation", "Isaac Asimov") == [False]


def test_book_file_inside_a_series_folder_is_taken_alone():
    # "Red Rising Series" is not the book, but the file "01 Red Rising.m4b" in it is
    assert folder_matches("@@s\\Pierce Brown\\Red Rising Series\\01 Red Rising.m4b", "Red Rising", BROWN) == [True]
    assert folder_matches("@@s\\Audiobooks\\Pierce Brown - Red Rising 02 - Golden Son.m4b", "Red Rising", BROWN) == []
    assert folder_matches("@@s\\Audiobooks\\Pierce Brown - Red Rising 02 - Golden Son.m4b", "Golden Son", BROWN) == [True]


def test_sequel_folder_is_not_downloaded_end_to_end():
    directory = "@@s\\Pierce Brown - Red Rising 02 - Golden Son"
    files = [{"filename": f"{directory}\\{i:02d}.mp3", "size": 40 * MB} for i in range(1, 4)]
    client = Client(responses=[{"username": "peer", "files": files}], listings={("peer", directory): [{"filename": f["filename"].rsplit("\\", 1)[1], "size": f["size"]} for f in files]})
    b = backend(client)
    wanted = DownloadTarget(book_id=7, book_title="Red Rising", author_name=BROWN, series_title=RR1, allowed_filetypes=["mp3"], readarr_book={"title": "Red Rising", "id": 7, "seriesTitle": RR1}, readarr_author={"authorName": BROWN}, media_type="audiobook")

    assert b.search(wanted) == []
    assert client.users.browsed == []
    assert b.search_notes[7]["stats"] == {"folders for a different title": 1}
    assert b.search_notes[7]["rejected"] == [{"reason": "folders for a different title", "directory": directory, "username": "peer"}]


# ---------------------------------------------------------------------------
# 2. Books that were never really looked for stay wanted
# ---------------------------------------------------------------------------


def target():
    return DownloadTarget(book_id=7, book_title="Red Rising", author_name=BROWN, series_title="", allowed_filetypes=["epub"], readarr_book={"title": "Red Rising", "id": 7, "monitored": True}, readarr_author={"authorName": BROWN})


class FakeBackend:
    name = "fake"

    def __init__(self, available=True, results=(), starts=True, search_error=None):
        self.available, self.results, self.starts, self.search_error = available, list(results), starts, search_error

    def is_available(self):
        return self.available

    def search(self, t):
        if self.search_error:
            raise self.search_error
        return self.results

    def download(self, t, r):
        return DownloadTask("id", "fake", DownloadStatus.QUEUED, t.book_title, t.author_name, t.book_id, r.filename) if self.starts else None


RESULT = SearchResult(title="Red Rising", author=BROWN, filename="a.epub", size_bytes=1, extension="epub", backend_name="fake", source_id="x")


@pytest.mark.parametrize(
    "fake, reason",
    [
        (FakeBackend(available=False), "not_searched"),  # slskd down
        (FakeBackend(search_error=RuntimeError("none of the 3 searches could be run")), "not_searched"),
        (FakeBackend(results=[RESULT], starts=False), "not_started"),  # e.g. its local folder is in use
        (FakeBackend(results=[]), "not_found"),
    ],
)
def test_reason_a_download_did_not_start(fake, reason):
    orch = DownloadOrchestrator([fake], Context(config=make_config(), slskd=None, readarr=None))
    failed = []
    assert orch.start_targets([target()], on_complete=failed.append) == []
    assert failed[0].extra["start_failure"] == reason


class RecordingReadarr:
    def __init__(self):
        self.updated = []

    def get_edition(self, book_id):
        return []

    def upd_book(self, book, editions):
        self.updated.append(dict(book))


@pytest.mark.parametrize("reason, unmonitored", [("not_searched", False), ("not_started", False), ("not_found", True)])
def test_only_a_book_nobody_shares_is_taken_off_the_wanted_list(tmp_path, reason, unmonitored):
    class Orch:
        def get_backend(self, name):
            return None

    readarr = RecordingReadarr()
    config = make_config(search={"remove_wanted_on_failure": "True"})
    ctx = Context(config=config, slskd=None, readarr=readarr, config_dir=str(tmp_path), orchestrator=Orch(), state=StateManager(str(tmp_path)))
    results = workflow._RunResults(ctx, [target()])
    task = DownloadTask("not_found_7", "none", DownloadStatus.FAILED, "Red Rising", BROWN, 7, "", extra={"start_failure": reason})

    results.on_complete(task)
    results.wait_for_imports()

    assert bool(readarr.updated) is unmonitored
    assert (tmp_path / "failure_list.txt").exists() is unmonitored
    assert results.failed_books == [(BROWN, "Red Rising")]  # still reported in the run summary


def test_searches_that_all_fail_are_not_an_empty_result():
    class Broken(Client):
        def __init__(self):
            super().__init__()
            self.searches.search_text = lambda **kwargs: (_ for _ in ()).throw(ConnectionError("slskd gone"))

    b = backend(Broken())
    with pytest.raises(SearchIncomplete, match="Search coverage incomplete"):
        b.search(DownloadTarget(book_id=7, book_title="Red Rising", author_name=BROWN, series_title="", allowed_filetypes=["mp3"], media_type="audiobook"))


def test_search_without_matches_is_still_an_empty_result():
    assert backend(Client(responses=[])).search(DownloadTarget(book_id=7, book_title="Red Rising", author_name=BROWN, series_title="", allowed_filetypes=["mp3"], media_type="audiobook")) == []


# ---------------------------------------------------------------------------
# 3. Settings checked at startup
# ---------------------------------------------------------------------------


def valid_config():
    config = sample_config()
    config["Slskd"]["download_dir"] = "/downloads"
    return config


def test_sample_config_passes_every_check():
    validate_config(valid_config())


@pytest.mark.parametrize(
    "section, option, value",
    [
        ("Search Settings", "search_timeout", "5s"),
        ("Search Settings", "maximum_peer_queue", "fifty"),
        ("Search Settings", "max_search_fallbacks", "-1"),
        ("Search Settings", "audiobook_browse_limit", "2.5"),
        ("Search Settings", "minimum_filename_match_ratio", "high"),
        ("Search Settings", "audiobook_min_size_mb", "nan"),
        ("Postprocessing", "match_ratio_exact", "0,8"),
        ("Search Settings", "remove_wanted_on_failure", "maybe"),
        ("Slskd", "delete_searches", "yes please"),
        ("Readarr", "disable_sync", "2"),
        ("General", "dry_run", "sure"),
    ],
)
def test_bad_setting_stops_at_startup_not_in_the_middle_of_a_run(section, option, value):
    config = valid_config()
    if section not in config:
        config.add_section(section)
    config[section][option] = value
    with pytest.raises(ValueError, match=option):
        validate_config(config)


# ---------------------------------------------------------------------------
# 4. Dry run
# ---------------------------------------------------------------------------


def test_dry_run_switch():
    config = configparser.ConfigParser()
    assert is_dry_run(config) is False
    assert is_dry_run(config, flag=True) is True
    config["General"] = {"dry_run": "true"}
    assert is_dry_run(config) is True


def test_dry_run_does_not_advance_the_page(tmp_path):
    (tmp_path / ".current_page.txt").write_text("2")
    readarr = PagedReadarr()
    ctx = paged_ctx(tmp_path, readarr)

    for _ in range(2):
        books = get_books(ctx, "missing", "incrementing_page", 10, advance_page=False)
    assert readarr.pages["missing"] == [2, 2]  # the same books each time...
    assert books[0]["id"] == 10
    assert (tmp_path / ".current_page.txt").read_text() == "2"  # ...and the real run still gets page 2

    get_books(ctx, "missing", "incrementing_page", 10)
    assert (tmp_path / ".current_page.txt").read_text() == "3"


def test_dry_run_does_not_create_a_page_file(tmp_path):
    get_books(paged_ctx(tmp_path, PagedReadarr()), "missing", "incrementing_page", 10, advance_page=False)
    assert list(tmp_path.iterdir()) == []
