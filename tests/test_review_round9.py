"""Regression tests for the ninth round (fourth outside review): the tag check's decision.

Three outcomes: confirmed match, confirmed wrong book, inconclusive. Only a confirmed wrong
book stops an import and excludes the peer.

1. A fitting album doesn't outweigh a title tag that names another book
2. An album that is the series is not another book, even when a book shares the name
3. Chapter titles that name another book count; ordinary chapter titles don't
4. An album R:soul can't place is inconclusive, not wrong
"""

import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_review_round8 import BROWN, HERBERT, Chaptarr, audio_file, run_import

from rsoul.audiotags import AGREE, MISMATCH, UNCLEAR, UNTAGGED, check_audiobook_tags, judge_text, named_books
from rsoul.config import Context
from rsoul.library import author_series, sibling_titles
from test_review_round3 import make_config

DUNE_BOOKS = ["Dune Messiah", "Children of Dune"]
RED_RISING_BOOKS = ["Red Rising", "Morning Star"]


def single(tmp_path, album, title, wanted="Dune", author=HERBERT, series="", siblings=(), author_series=("Dune #2",)):
    path = audio_file(tmp_path / "book.m4b", album=album, title=title)
    return check_audiobook_tags([path], wanted, author, series, [], siblings, author_series)[0]


def chapters(tmp_path, album, titles, wanted, author, series="", siblings=(), author_series=()):
    paths = [audio_file(tmp_path / f"{i:02d}.mp3", album=album, title=t) for i, t in enumerate(titles, start=1)]
    return check_audiobook_tags(paths, wanted, author, series, [], siblings, author_series)[0]


# ---------------------------------------------------------------------------
# 1. Single file: album against title tag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "album, title, series, verdict",
    [
        ("Dune", "Dune Messiah", "", MISMATCH),  # the title tag names another book; the album is the series
        ("Dune", "Dune Book 2", "Dune #1", MISMATCH),  # another position than Chaptarr gives
        ("Dune", "Children of Dune (Unabridged)", "", MISMATCH),
        ("Dune", "Dune", "", AGREE),
        ("Dune", None, "", AGREE),
        ("Dune", "Chapter 1", "", AGREE),  # ordinary labels beside a fitting album stay fine
        ("Dune", "Disc 1", "", AGREE),
        ("Dune", "Part 1 of 2", "", AGREE),
        ("Dune", "Dune Book 2", "", AGREE),  # position unknown: "Book 2" may be a part of the novel
        ("Dune Chronicles", "Dune", "", AGREE),
        ("Dune Messiah", None, "", MISMATCH),
        ("Dune Messiah", "Chapter 1", "", MISMATCH),
        ("Dune Messiah", "Dune", "", UNCLEAR),  # the tags contradict each other: no proof either way
        ("Audible", "Blackstone Audio", "", UNCLEAR),
        (None, None, "", UNTAGGED),
    ],
)
def test_single_file_album_and_title(tmp_path, album, title, series, verdict):
    assert single(tmp_path, album, title, series=series, siblings=DUNE_BOOKS) == verdict


# ---------------------------------------------------------------------------
# 2. The series as album
# ---------------------------------------------------------------------------


def test_series_album_on_a_later_book_is_not_another_book(tmp_path):
    # Golden Son's files carry the series, which is also the first book's title
    titles = [f"Chapter {i}" for i in range(1, 5)]
    assert chapters(tmp_path, "Red Rising", titles, "Golden Son", BROWN, series="Red Rising Saga #2", siblings=RED_RISING_BOOKS) == UNCLEAR


def test_series_is_recognised_from_the_authors_other_books(tmp_path):
    # Chaptarr gives no series for the wanted book, but one for its siblings
    titles = [f"Chapter {i}" for i in range(1, 5)]
    assert chapters(tmp_path, "Red Rising", titles, "Golden Son", BROWN, siblings=RED_RISING_BOOKS, author_series=["Red Rising Saga #1"]) == UNCLEAR
    assert judge_text("Red Rising Saga", "Golden Son", BROWN, "", [], RED_RISING_BOOKS, ["Red Rising Saga #1"])[0] == UNCLEAR
    assert judge_text("The Stormlight Archive", "The Way of Kings", "Brandon Sanderson", "", [], ["Words of Radiance"], ["The Stormlight Archive #2"])[0] == UNCLEAR


@pytest.mark.parametrize("album", ["Red Rising Book 2", "Red Rising, Book 2", "Red Rising #2", "Red Rising 2", "Red Rising (Book 2)"])
def test_another_title_cited_with_a_position_is_the_series(album):
    # No series from Chaptarr at all, and the album cites the first book's title as the series
    assert judge_text(album, "Golden Son", BROWN, "", [], RED_RISING_BOOKS, [], is_album=True)[0] == UNCLEAR
    assert judge_text(album, "Golden Son", BROWN, "Red Rising Saga #2", [], RED_RISING_BOOKS, [], is_album=True)[0] == UNCLEAR


def test_album_alone_proves_nothing_without_any_series_from_chaptarr(tmp_path):
    # "The Lord of the Rings" as album of "The Fellowship of the Ring": the series, or the omnibus Chaptarr lists as a book?
    titles = [f"Chapter {i}" for i in range(1, 5)]
    omnibus = ["The Lord of the Rings", "The Two Towers"]
    assert chapters(tmp_path, "The Lord of the Rings", titles, "The Fellowship of the Ring", "J.R.R. Tolkien", siblings=omnibus) == UNCLEAR
    assert chapters(tmp_path / "b", "The Lord of the Rings", titles, "The Fellowship of the Ring", "J.R.R. Tolkien", series="The Lord of the Rings #1", siblings=omnibus) == UNCLEAR
    # ...while a title tag on a single file, or the chapter titles, still say which book it is
    assert single(tmp_path / "c", "Whatever", "The Two Towers", wanted="The Fellowship of the Ring", author="J.R.R. Tolkien", siblings=omnibus, author_series=()) == MISMATCH


def test_series_album_does_not_excuse_another_books_own_name(tmp_path):
    # "Morning Star" is not the series: the files are another book
    titles = [f"Chapter {i}" for i in range(1, 5)]
    assert chapters(tmp_path, "Morning Star", titles, "Golden Son", BROWN, series="Red Rising Saga #2", siblings=RED_RISING_BOOKS) == MISMATCH
    assert judge_text("Dune Messiah", "Dune", HERBERT, "Dune #1", [], DUNE_BOOKS, is_album=True)[0] == MISMATCH
    assert judge_text("Dune Messiah Disc 2", "Dune", HERBERT, "Dune #1", [], DUNE_BOOKS, is_album=True)[0] == MISMATCH  # a disc number is not a series position


def test_golden_son_with_series_album_is_imported_and_the_peer_kept(tmp_path):
    chaptarr = Chaptarr(book={"authorId": 3, "title": "Golden Son", "seriesTitle": "Red Rising Saga #2"}, author_books=["Red Rising", "Golden Son", "Morning Star"])
    result, chaptarr, history, _ = run_import(tmp_path, album="Red Rising", title="Golden Son", chaptarr=chaptarr, files=("01.mp3", "02.mp3", "03.mp3", "04.mp3"))
    assert result == {7: True}
    assert len(chaptarr.commands) == 1
    assert history.history == []


def test_series_come_from_chaptarrs_book_list():
    chaptarr = Chaptarr(book={"authorId": 3}, author_books=["Red Rising", "Golden Son", "Standalone"])
    chaptarr.author_series = {"Red Rising": "Red Rising Saga #1", "Golden Son": "Red Rising Saga #2"}
    ctx = Context(config=make_config(), slskd=None, readarr=chaptarr)
    assert author_series(ctx, {"authorId": 3}) == ["Red Rising Saga #1", "Red Rising Saga #2"]
    assert sibling_titles(ctx, {"authorId": 3, "title": "Golden Son"}, BROWN) == ["Red Rising", "Standalone"]
    assert author_series(ctx, {}) == []


# ---------------------------------------------------------------------------
# 3. Chapter titles
# ---------------------------------------------------------------------------


def test_chapter_titles_naming_another_book_count(tmp_path):
    titles = [f"Dune Messiah, Chapter {i}" for i in range(1, 6)]
    assert chapters(tmp_path, "Dune", titles, "Dune", HERBERT, siblings=DUNE_BOOKS) == MISMATCH
    assert chapters(tmp_path / "b", None, [f"Dune Messiah - {i:02d}" for i in range(1, 6)], "Dune", HERBERT, siblings=DUNE_BOOKS) == MISMATCH


@pytest.mark.parametrize(
    "titles",
    [
        [f"Chapter {i}" for i in range(1, 6)],
        [f"Dune - Chapter {i}" for i in range(1, 6)],
        ["Book One: Dune", "Book Two: Muad'Dib", "Book Two: Muad'Dib (cont.)", "Book Three: The Prophet", "Appendix"],  # parts of the novel
        ["Book 1", "Book 2", "Book 2", "Book 3", "Book 3"],
        [f"Dune {i:03d}" for i in range(1, 6)],
        ["Opening Credits", "Arrakis", "The Spice", "Fremen", "End Credits"],
    ],
)
def test_ordinary_chapter_titles_are_fine(tmp_path, titles):
    assert chapters(tmp_path, "Dune", titles, "Dune", HERBERT, series="Dune #1", siblings=DUNE_BOOKS) == AGREE


def test_one_chapter_that_shares_a_books_title_proves_nothing(tmp_path):
    # A chapter called like another book of the author, among ordinary ones
    titles = ["Dune Messiah", "Chapter 2", "Chapter 3"]
    assert chapters(tmp_path, "Dune", titles, "Dune", HERBERT, siblings=DUNE_BOOKS) == AGREE


def test_chapter_titles_can_be_the_only_tags(tmp_path):
    assert chapters(tmp_path, None, ["Chapter 1", "Chapter 2"], "Dune", HERBERT, siblings=DUNE_BOOKS) == UNCLEAR


# ---------------------------------------------------------------------------
# 4. Albums that can't be placed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("album", ["Audible", "Blackstone Audio", "Penguin Random House Audio", "Hörbuch", "Der Wüstenplanet", "My Audiobooks", "SciFi"])
def test_unplaceable_album_is_inconclusive(tmp_path, album):
    assert judge_text(album, "Dune", HERBERT, "", [], DUNE_BOOKS)[0] == UNCLEAR
    result, chaptarr, history, _ = run_import(tmp_path, album=album, title="Dune", author=HERBERT, chaptarr=Chaptarr(book={"authorId": 3, "title": "Dune"}, author_books=["Dune"] + DUNE_BOOKS))
    assert result == {7: True}
    assert history.history == []  # and no peer is excluded over it


def test_series_album_without_any_series_from_chaptarr_is_inconclusive():
    assert judge_text("The Stormlight Archive", "The Way of Kings", "Brandon Sanderson")[0] == UNCLEAR


# ---------------------------------------------------------------------------
# Which books a tag names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, title, siblings, wanted, others",
    [
        ("Dune Messiah", "Dune", ["Dune Messiah"], False, ["Dune Messiah"]),  # not also "Dune"
        ("Dune Messiah", "Dune Messiah", ["Dune"], True, []),
        ("Dune", "Dune Messiah", ["Dune"], False, ["Dune"]),  # a one-word title counts as the whole text...
        ("Dune Chronicles", "Children of Dune", ["Dune"], False, []),  # ...but not inside a longer one
        ("Children of Dune (Unabridged)", "Dune", ["Children of Dune"], False, ["Children of Dune"]),
        ("Dune / Dune Messiah", "Dune", ["Dune Messiah"], True, ["Dune Messiah"]),
        ("Mistborn", "Mistborn: The Final Empire", ["Mistborn: The Well of Ascension"], False, []),  # the shared series prefix names neither
        ("The Well of Ascension", "Mistborn: The Final Empire", ["Mistborn: The Well of Ascension"], False, ["Mistborn: The Well of Ascension"]),
        ("Chapter 1", "Dune", ["Dune Messiah"], False, []),
    ],
)
def test_which_books_a_tag_names(text, title, siblings, wanted, others):
    assert named_books(text, title, siblings) == (wanted, others)


# ---------------------------------------------------------------------------
# Sweep: downloads that ARE the wanted book, tagged in every usual way, with and without
# series information from Chaptarr. None may be rejected. (Single hand-picked cases missed
# whole classes before, e.g. a tag naming the book together with its series.)
# ---------------------------------------------------------------------------

import itertools  # noqa: E402

from rsoul import audiotags  # noqa: E402

# (title, author, series label, position, series as people write it, the author's other books, their series)
LIBRARY = [
    ("Golden Son", "Pierce Brown", "Red Rising Saga #2", 2, "Red Rising", ["Red Rising", "Morning Star"], ["Red Rising Saga #1", "Red Rising Saga #3"]),
    ("Red Rising", "Pierce Brown", "Red Rising Saga #1", 1, "Red Rising", ["Golden Son", "Morning Star"], ["Red Rising Saga #2"]),
    ("Dune", "Frank Herbert", "Dune #1", 1, "Dune", ["Dune Messiah", "Children of Dune"], ["Dune #2", "Dune #3"]),
    ("Children of Dune", "Frank Herbert", "Dune #3", 3, "Dune", ["Dune", "Dune Messiah"], ["Dune #1"]),
    ("The Fellowship of the Ring", "J.R.R. Tolkien", "The Lord of the Rings #1", 1, "The Lord of the Rings", ["The Lord of the Rings", "The Two Towers", "The Hobbit"], ["The Lord of the Rings #2"]),
    ("Project Hail Mary", "Andy Weir", "", None, "", ["The Martian", "Artemis"], []),
    ("The Way of Kings", "Brandon Sanderson", "The Stormlight Archive #1", 1, "The Stormlight Archive", ["Words of Radiance", "Mistborn: The Final Empire"], ["The Stormlight Archive #2", "Mistborn #1"]),
]


def usual_albums(title, author, series, position):
    albums = [title, f"{title} (Unabridged)", f"{author} - {title}", f"{title} [Audible]", f"{title} Disc 1", f"{title} - Part 1", None, "Audible", author, "Unknown Album", "Audiobook"]
    if series:
        albums += [series, f"{series} Saga", f"{series} Series", f"{series} {position}", f"{series} Book {position}", f"{series}, Book {position}", f"{series} #{position}"]
        albums += [f"{title} ({series} #{position})", f"{title}: {series}, Book {position}", f"{series} {position} - {title}", f"{title} - {series} {position}"]
    return albums


def usual_chapter_titles(title, i):
    return [None, f"Chapter {i}", f"{title} - Chapter {i}", f"{title} {i:03d}", f"{i:02d}", f"Part {i}", f"Book {i}", f"{title} (Unabridged) Part {i}"]


def usual_book_titles(title, author, series, position):
    titles = [None, title, f"{title} (Unabridged)", f"{author} - {title}", f"{title} - Part 1", "Chapter 1", "Track 1"]
    if series:
        titles += [f"{title} ({series} #{position})", f"{series} {position}: {title}", f"{series} Book {position}"]
    return titles


@pytest.mark.parametrize("book", LIBRARY, ids=[b[0] for b in LIBRARY])
def test_the_wanted_book_is_never_rejected_whatever_its_tags(book, monkeypatch):
    title, author, series_label, position, series, siblings, others_series = book
    tags = {}
    monkeypatch.setattr(audiotags, "read_tags", lambda path: {k: v for k, v in tags.get(path, {}).items() if v})
    rejected = []
    judged = 0
    for series_known, others_known in itertools.product([True, False], repeat=2):
        known = (series_label if series_known else "", [], siblings, others_series if others_known else [])
        for album in usual_albums(title, author, series, position):
            for book_title in usual_book_titles(title, author, series, position):
                tags.clear()
                tags["/x/book.m4b"] = {"album": album, "title": book_title}
                judged += 1
                if check_audiobook_tags(["/x/book.m4b"], title, author, *known)[0] == MISMATCH:
                    rejected.append(("single file", album, book_title, series_known, others_known))
            for style in range(8):
                tags.clear()
                paths = [f"/x/{i:02d}.mp3" for i in range(1, 6)]
                for i, path in enumerate(paths, start=1):
                    tags[path] = {"album": album, "title": usual_chapter_titles(title, i)[style]}
                judged += 1
                if check_audiobook_tags(paths, title, author, *known)[0] == MISMATCH:
                    rejected.append(("chapters", album, usual_chapter_titles(title, 1)[style], series_known, others_known))
    assert judged > 500
    assert rejected == []


@pytest.mark.parametrize(
    "wanted, series, siblings, album, title",
    [
        # What must be caught when the download is really another book of the author
        ("Red Rising", "Red Rising Saga #1", ["Golden Son"], "Golden Son", None),
        ("Red Rising", "Red Rising Saga #1", ["Golden Son"], "Red Rising 2 - Golden Son", None),  # the wanted title here is the series
        ("Red Rising", "Red Rising Saga #1", ["Golden Son"], "Red Rising", "Golden Son"),
        ("Red Rising", "", ["Golden Son"], "Audible", "Golden Son (Unabridged)"),
        ("Dune", "Dune #1", ["Children of Dune"], "Children of Dune - Dune 3", None),
        ("Dune", "Dune #1", ["Children of Dune"], "Dune", "Dune, Book 3"),
        ("Golden Son", "Red Rising Saga #2", ["Morning Star"], "Morning Star", "Chapter 1"),
    ],
)
def test_another_book_of_the_author_is_caught(tmp_path, wanted, series, siblings, album, title):
    author = HERBERT if "Dune" in wanted else BROWN
    assert single(tmp_path, album, title, wanted=wanted, author=author, series=series, siblings=siblings, author_series=()) == MISMATCH
