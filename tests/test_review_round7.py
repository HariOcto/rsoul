"""Regression tests for the seventh round (third outside review).

1. Collections named in brackets are not the single book
2. A folder named after the book that holds several books
3. A complete listing of an incomplete book
4. Matches that could not be used right now keep the book on the wanted list
5. The dry run report gives the real reason, and the dry run only reads
"""

import json
import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_review_round3 import MB, Client, PagedReadarr, Transfers, backend, make_config, paged_ctx

from rsoul import download as download_module
from rsoul import dryrun
from rsoul.backends import slskd_backend
from rsoul.backends.base import DownloadTarget
from rsoul.backends.slskd_backend import SlskdBackend
from rsoul.config import Context
from rsoul.match import books_in_folder, missing_parts, title_segment_match
from rsoul.orchestrator import DownloadOrchestrator
from rsoul.search import get_books
from rsoul.state import StateManager
from rsoul.utils import get_current_page

HERBERT = "Frank Herbert"
BOOK = 500 * MB
CHAPTER = 30 * MB


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(slskd_backend.time, "sleep", lambda s: None)
    monkeypatch.setattr(download_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(dryrun.time, "sleep", lambda s: None)


def target(title="Dune", author=HERBERT, series="", formats=("m4b", "mp3")):
    book = {"title": title, "id": 7, "seriesTitle": series, "monitored": True}
    return DownloadTarget(book_id=7, book_title=title, author_name=author, series_title=series, allowed_filetypes=list(formats), readarr_book=book, readarr_author={"authorName": author}, media_type="audiobook")


def share(directory, names, size=BOOK, found=None, listable=True, transfers=None):
    """A fake slskd where one peer shares `names` in `directory`; the search returns `found`."""
    listing = [{"filename": n, "size": size} for n in names]
    responses = [{"username": "peer", "files": [{"filename": f"{directory}\\{n}", "size": size} for n in (found or names)]}]
    return Client(responses=responses, listings={("peer", directory): listing} if listable else {}, transfers=transfers)


def picked(client, wanted=None, **config):
    results = backend(client, **config).search(wanted or target())
    return [f["filename"].rsplit("\\", 1)[1] for f in results[0].extra["files"]] if results else []


# ---------------------------------------------------------------------------
# 1. Collections in brackets
# ---------------------------------------------------------------------------

RR1 = "Red Rising Saga #1"


@pytest.mark.parametrize("series", [RR1, ""])
@pytest.mark.parametrize(
    "name, same_book",
    [
        ("Red Rising (Books 1-6)", False),
        ("Red Rising [Complete Series]", False),
        ("Red Rising (Trilogy)", False),
        ("Red Rising (Box Set)", False),
        ("Red Rising [Omnibus]", False),
        ("Red Rising {The Complete Collection}", False),
        ("Red Rising Books 1 & 2", False),
        ("Red Rising (Vol. 1-3)", False),
        # Ordinary details in brackets stay acceptable
        ("Red Rising (Unabridged)", True),
        ("Red Rising (2014)", True),
        ("Red Rising [Tim Gerard Reynolds]", True),
        ("Red Rising (Pierce Brown)", True),
        ("Red Rising (Red Rising Saga #1)", True),
        ("Red Rising (Red Rising Saga, Book 1)", True),
        ("Red Rising [64kbps mp3]", True),
    ],
)
def test_collection_in_brackets_is_not_the_single_book(name, same_book, series):
    assert title_segment_match("Red Rising", name, "Pierce Brown", series) is same_book


def test_series_named_in_brackets_without_a_number():
    # With the series known from Chaptarr it is an annotation; without, "Red Rising (Red Rising
    # Saga)" could be the saga itself and is left alone
    assert title_segment_match("Red Rising", "Red Rising (Red Rising Saga)", "Pierce Brown", RR1)
    assert not title_segment_match("Red Rising", "Red Rising (Red Rising Saga)", "Pierce Brown", "")
    assert title_segment_match("Golden Son", "Golden Son (Red Rising Saga)", "Pierce Brown", "")


def test_title_that_contains_a_collection_word_still_matches():
    assert title_segment_match("The Complete Stories", "Isaac Asimov - The Complete Stories", "Isaac Asimov")
    assert title_segment_match("The Trilogy of Two", "The Trilogy of Two (Unabridged)", "Juman Malouf")


@pytest.mark.parametrize("leaf", ["Red Rising (Books 1-6)", "Red Rising [Complete Series]", "Red Rising (Trilogy)", "Red Rising (Book 2)", "Pierce Brown - Red Rising [Red Rising Saga #2]"])
def test_bracketed_collection_or_sequel_folder_is_not_downloaded(leaf):
    # Through the backend, where folder names are also tried with their brackets stripped
    directory = f"@@s\\Pierce Brown\\{leaf}"
    client = share(directory, [f"{i:02d}.mp3" for i in range(1, 7)], size=CHAPTER)
    assert picked(client, target("Red Rising", "Pierce Brown", RR1)) == []
    assert client.users.browsed == []


@pytest.mark.parametrize("leaf", ["Red Rising (Unabridged)", "Red Rising (2014) [Tim Gerard Reynolds]", "Red Rising (Red Rising Saga #1)"])
def test_folder_with_ordinary_brackets_is_downloaded(leaf):
    client = share(f"@@s\\Pierce Brown\\{leaf}", [f"{i:02d}.mp3" for i in range(1, 7)], size=CHAPTER)
    assert len(picked(client, target("Red Rising", "Pierce Brown", RR1))) == 6


def test_bracketed_collection_file_is_not_downloaded():
    client = share("@@s\\Audiobooks", ["Pierce Brown - Red Rising (Books 1-3).m4b"])
    assert picked(client, target("Red Rising", "Pierce Brown", RR1)) == []


# ---------------------------------------------------------------------------
# 2. Several books in a folder named after one
# ---------------------------------------------------------------------------

DUNE_DIR = "@@s\\Frank Herbert\\Dune"
T = {"book": {"title": "Dune"}, "author": {"authorName": HERBERT}}


def test_only_the_book_itself_is_taken_from_a_shelf_folder():
    assert picked(share(DUNE_DIR, ["Dune.m4b", "Dune Messiah.m4b", "Children of Dune.m4b"])) == ["Dune.m4b"]
    assert picked(share(DUNE_DIR, ["01 Dune.m4b", "02 Dune Messiah.m4b", "03 Children of Dune.m4b"])) == ["01 Dune.m4b"]
    # a multi-part book on the shelf is taken with all its parts
    assert picked(share(DUNE_DIR, ["Dune - Part 1.m4b", "Dune - Part 2.m4b", "Dune Messiah.m4b"])) == ["Dune - Part 1.m4b", "Dune - Part 2.m4b"]


def test_shelf_folder_without_the_book_is_skipped():
    client = share(DUNE_DIR, ["Dune Messiah.m4b", "Children of Dune.m4b", "God Emperor of Dune.m4b"])
    b = backend(client)
    assert b.search(target()) == []
    assert "several other books" in b.search_notes[7]["skipped"][0]["reason"]


@pytest.mark.parametrize(
    "names, size",
    [
        ([f"{i:02d}.mp3" for i in range(1, 5)], CHAPTER),
        ([f"{i:02d} - Chapter {i}.mp3" for i in range(1, 5)], CHAPTER),
        (["01 - Arrakis.mp3", "02 - Muad'Dib.mp3", "03 - The Prophet.mp3"], CHAPTER),  # named chapters
        (["Dune - 01.mp3", "Dune - 02.mp3", "Dune - 03.mp3"], CHAPTER),
        (["Dune Part 1.m4b", "Dune Part 2.m4b"], BOOK),
        (["Dune (1 of 2).m4b", "Dune (2 of 2).m4b"], BOOK),
        (["00 - Introduction.mp3", "01 - Book One.mp3", "02 - Book Two.mp3"], BOOK),  # big, but chapter-like names
    ],
)
def test_chapter_folders_are_still_taken_whole(names, size):
    assert picked(share(DUNE_DIR, names, size=size)) == sorted(names, key=str.lower)


def test_books_in_folder_verdicts():
    assert books_in_folder(T, [("Dune.m4b", BOOK), ("Dune Messiah.m4b", BOOK)]) == ("own", ["Dune.m4b"])
    assert books_in_folder(T, [("Dune Messiah.m4b", BOOK), ("Children of Dune.m4b", BOOK)]) == ("several", [])
    assert books_in_folder(T, [("Dune.m4b", BOOK)]) == ("folder", [])
    # Small files named like other works are chapters with names, not books: no guessing
    assert books_in_folder(T, [("Dune.mp3", CHAPTER), ("Arrakis.mp3", CHAPTER)]) == ("folder", [])
    assert books_in_folder(T, [("Arrakis.mp3", CHAPTER), ("Caladan.mp3", CHAPTER)]) == ("folder", [])


# ---------------------------------------------------------------------------
# 3. Incomplete books
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "names, incomplete",
    [
        (["Frank Herbert - Dune - Part 2 of 4.m4b"], True),
        (["Dune (1 of 3).m4b", "Dune (3 of 3).m4b"], True),
        (["Dune (1 of 2).m4b", "Dune (2 of 2).m4b"], False),
        (["Frank Herbert - Dune - 02.mp3", "Frank Herbert - Dune - 03.mp3"], True),  # no chapter 1
        (["01.mp3", "02.mp3", "04.mp3"], True),  # a gap
        ([f"{i:02d}.mp3" for i in range(1, 13)], False),
        (["00 - Intro.mp3", "01 - One.mp3", "02 - Two.mp3"], False),  # starting at 0 is fine
        (["Dune - Part 2.m4b"], True),  # a lone part 2
        (["Dune - Part 1.m4b"], False),  # can't be told from a complete "part 1 of 1"
        (["Dune.m4b"], False),
        (["02 Golden Son.m4b"], False),  # a number in front is the place in the series
        (["George Orwell - 1984.m4b"], False),
        (["intro.mp3", "01.mp3", "03.mp3"], False),  # not a plain numbering: not judged
        (["101.mp3", "102.mp3", "201.mp3"], False),  # disc-and-track numbers: not judged
    ],
)
def test_numbering_shows_whether_the_book_is_complete(names, incomplete):
    assert (missing_parts(names) is not None) is incomplete


def test_incomplete_books_are_not_downloaded():
    lib = "@@s\\Audiobooks"
    b = backend(share(lib, ["Frank Herbert - Dune - Part 2 of 4.m4b"]))
    assert b.search(target()) == []
    assert b.search_notes[7]["skipped"][0]["reason"] == "incomplete (part 1, 3, 4 of 4 missing)"

    assert picked(share(lib, ["Frank Herbert - Dune - 02.mp3", "Frank Herbert - Dune - 03.mp3"], size=CHAPTER)) == []
    assert picked(share(DUNE_DIR, ["01.mp3", "02.mp3", "04.mp3"], size=CHAPTER)) == []
    assert picked(share(lib, [f"Frank Herbert - Dune - {i:02d}.mp3" for i in range(1, 4)], size=CHAPTER)) == [f"Frank Herbert - Dune - {i:02d}.mp3" for i in range(1, 4)]


# ---------------------------------------------------------------------------
# 4. Found but unavailable is not "not found"
# ---------------------------------------------------------------------------


class RecordingReadarr:
    def __init__(self):
        self.updated = []

    def get_edition(self, book_id):
        return []

    def upd_book(self, book, editions):
        self.updated.append(dict(book))


def run_start(client, tmp_path, search=None, **slskd):
    """Start one wanted book through the real backend, orchestrator and failure handling."""
    from rsoul import workflow

    readarr = RecordingReadarr()
    config = make_config(search={"remove_wanted_on_failure": "True", **(search or {})}, slskd=slskd)
    ctx = Context(config=config, slskd=client, readarr=readarr, config_dir=str(tmp_path), state=StateManager(str(tmp_path)))
    ctx.orchestrator = DownloadOrchestrator([SlskdBackend(ctx)], ctx)
    ctx.orchestrator.backends[0].is_available = lambda: True
    results = workflow._RunResults(ctx, [target()])
    started = ctx.orchestrator.start_targets([target()], on_complete=results.on_complete)
    results.wait_for_imports()
    return started, readarr, results


def chapters_share(**kwargs):
    return share(DUNE_DIR, [f"{i:02d}.mp3" for i in range(1, 4)], size=CHAPTER, **kwargs)


def test_book_stays_wanted_when_the_peer_cannot_be_browsed(tmp_path):
    started, readarr, results = run_start(chapters_share(listable=False), tmp_path)
    assert started == []
    assert readarr.updated == []  # still monitored
    assert not (tmp_path / "failure_list.txt").exists()
    assert results.failed_books == [(HERBERT, "Dune")]


def test_book_stays_wanted_when_its_local_folder_is_in_use(tmp_path):
    downloads = tmp_path / "downloads"
    (downloads / "Dune").mkdir(parents=True)
    (downloads / "Dune" / "01.mp3").write_bytes(b"left over")
    started, readarr, _ = run_start(chapters_share(), tmp_path, download_dir=str(downloads))
    assert started == []
    assert readarr.updated == []


def test_book_stays_wanted_when_matches_beyond_the_browse_limit_were_never_tried(tmp_path):
    directory = "@@s\\Frank Herbert\\Dune"
    files = [{"filename": f"{directory}\\0{i}.mp3", "size": CHAPTER} for i in (2, 3)]  # incomplete: no chapter 1
    responses = [{"username": f"peer{n}", "files": files} for n in range(7)]
    listing = [{"filename": f["filename"].rsplit("\\", 1)[1], "size": CHAPTER} for f in files]
    client = Client(responses=responses, listings={(f"peer{n}", directory): listing for n in range(7)})
    started, readarr, _ = run_start(client, tmp_path, search={"max_search_fallbacks": "0"})
    assert started == []
    assert len(client.users.browsed) == 5  # audiobook_browse_limit
    assert readarr.updated == []


def test_fallback_queries_do_not_list_the_same_folders_again(tmp_path):
    directory = "@@s\\Frank Herbert\\Dune"
    files = [{"filename": f"{directory}\\0{i}.mp3", "size": CHAPTER} for i in (2, 3)]
    listing = [{"filename": f["filename"].rsplit("\\", 1)[1], "size": CHAPTER} for f in files]
    client = Client(responses=[{"username": f"peer{n}", "files": files} for n in range(7)], listings={(f"peer{n}", directory): listing for n in range(7)})
    started, readarr, _ = run_start(client, tmp_path)

    # The fallback queries bring back the same seven folders: each is listed once, the two
    # beyond the limit of the first query in the second
    assert sorted(client.users.browsed) == sorted((f"peer{n}", directory) for n in range(7))
    assert [book["monitored"] for book in readarr.updated] == [False]  # all seven seen, all incomplete


def test_book_nobody_shares_whole_is_still_taken_off_the_list(tmp_path):
    # Every match is unusable for good (incomplete): that is "not found"
    client = share(DUNE_DIR, ["02.mp3", "03.mp3"], size=CHAPTER)
    started, readarr, _ = run_start(client, tmp_path)
    assert started == []
    assert [book["monitored"] for book in readarr.updated] == [False]


def test_book_without_any_match_is_still_taken_off_the_list(tmp_path):
    started, readarr, _ = run_start(Client(responses=[]), tmp_path)
    assert [book["monitored"] for book in readarr.updated] == [False]


# ---------------------------------------------------------------------------
# 5. Dry run: real reasons, and reading only
# ---------------------------------------------------------------------------


def dry_run_report(client, tmp_path, remarks=()):
    ctx = Context(config=make_config(), slskd=client, readarr=None, config_dir=str(tmp_path))
    ctx.orchestrator = DownloadOrchestrator([SlskdBackend(ctx)], ctx)
    ctx.orchestrator.backends[0].is_available = lambda: True
    counts = dryrun.run_dry_run(ctx, [target()], remarks)
    return counts, (tmp_path / dryrun.REPORT_FILENAME).read_text()


def test_report_says_a_matching_folder_could_not_be_listed(tmp_path):
    counts, report = dry_run_report(chapters_share(listable=False), tmp_path)

    assert counts == {"would_download": 0, "no_match": 1, "not_searched": 0}
    assert "no file or folder matched" not in report
    assert "matching folders were found, but none could be used" in report
    assert "the book stays wanted" in report
    assert f"peer: {DUNE_DIR}" in report
    assert "could not list the full folder" in report and "(may work on a later run)" in report
    assert "other matching folders" not in report  # it is not listed a second time as untried


@pytest.mark.parametrize(
    "client, reason",
    [
        (share("@@s\\Audiobooks", ["Frank Herbert - Dune - Part 2 of 4.m4b"]), "incomplete (part 1, 3, 4 of 4 missing)"),
        (share(DUNE_DIR, ["01.mp3", "02.m4a"], size=CHAPTER), "omits numbered chapters present in another format"),
        (share(DUNE_DIR, ["01.mp3"], size=1 * MB), "below audiobook_min_size_mb"),
        (share(DUNE_DIR, ["Dune Messiah.m4b", "Children of Dune.m4b"]), "several other books"),
    ],
)
def test_report_gives_the_reason_a_match_was_passed_over(tmp_path, client, reason):
    _, report = dry_run_report(client, tmp_path)
    assert reason in report
    assert "(counts as not found)" in report


def test_report_shows_skipped_folders_next_to_the_choice(tmp_path):
    directory = "@@s\\Frank Herbert\\Dune"
    files = [{"filename": f"{directory}\\0{i}.mp3", "size": CHAPTER} for i in (1, 2)]
    listing = [{"filename": f["filename"].rsplit("\\", 1)[1], "size": CHAPTER} for f in files]
    client = Client(responses=[{"username": "offline", "files": files}, {"username": "online", "files": files}], listings={("online", directory): listing})
    counts, report = dry_run_report(client, tmp_path, remarks=["2 saved download(s) are waiting"])

    assert counts["would_download"] == 1
    assert "WOULD DOWNLOAD from online" in report
    assert "offline: " in report and "could not list the full folder" in report
    assert "NOTE: 2 saved download(s) are waiting" in report


def test_dry_run_leaves_a_damaged_page_file_alone(tmp_path):
    path = tmp_path / ".current_page.txt"
    path.write_text("abc")
    readarr = PagedReadarr()
    get_books(paged_ctx(tmp_path, readarr), "missing", "incrementing_page", 10, advance_page=False)

    assert readarr.pages["missing"] == [1]
    assert path.read_text() == "abc"
    assert get_current_page(str(path)) == 1 and path.read_text() == "1"  # a real run repairs it


def test_dry_run_leaves_a_damaged_state_file_alone(tmp_path):
    path = tmp_path / "grab_list_state.json"
    for content in ('[{"task_id": "x"', json.dumps([{"task_id": "a", "extra": "oops"}])):
        path.write_text(content)
        state = StateManager(str(tmp_path), read_only=True)
        state.clear()
        state.add_item({"task_id": "new"})
        assert path.read_text() == content
        assert [p.name for p in tmp_path.iterdir()] == ["grab_list_state.json"]


# ---------------------------------------------------------------------------
# The title tables again, through the whole search (helpers alone proved too little: the
# backend also tries names with their brackets stripped)
# ---------------------------------------------------------------------------

from test_review_round6 import NAMES  # noqa: E402

OTHER_BOOKS = [(title, series, name) for title, series, name, same_book in NAMES if not same_book]


@pytest.mark.parametrize("title, series, name", OTHER_BOOKS)
@pytest.mark.parametrize("layout", ["folder", "file"])
def test_names_of_other_books_are_never_downloaded(title, series, name, layout):
    author = "Peter F. Hamilton" if "Salvation" in name else "Frank Herbert" if "Dune" in name else "Pierce Brown"
    if layout == "folder":
        client = share(f"@@s\\{author}\\{name}", [f"{i:02d}.mp3" for i in range(1, 4)], size=CHAPTER)
    else:
        client = share("@@s\\Audiobooks", [f"{author} - {name}.m4b"])
    assert picked(client, target(title, author, series)) == []
