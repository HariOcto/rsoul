"""Tests for the eighth round: what the files and Chaptarr know, beyond folder names.

1. Audio tags are read and compared with the wanted book before import
2. The author's other titles (from Chaptarr) settle what a file name leaves open
3. Narrator names (from Chaptarr) are not mistaken for another title
4. A download far too small for the book's duration is not the book
"""

import os
import shutil
import sys
from datetime import datetime, timezone

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_review_round3 import MB, Client, backend, make_config, sample_config

from rsoul import audiotags, postprocess
from rsoul import download as download_module
from rsoul.audiotags import AGREE, MISMATCH, UNCLEAR, UNTAGGED, check_audiobook_tags, judge_text, read_tags
from rsoul.backends import slskd_backend
from rsoul.backends.base import DownloadTarget
from rsoul.config import Context, validate_config
from rsoul.history import HistoryManager
from rsoul.library import sibling_titles
from rsoul.match import audiobook_folder_match, books_in_folder, title_segment_match

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
HERBERT = "Frank Herbert"
BROWN = "Pierce Brown"
BOOK = 500 * MB


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(slskd_backend.time, "sleep", lambda s: None)
    monkeypatch.setattr(download_module.time, "sleep", lambda s: None)


def audio_file(path, album=None, title=None, artist=None):
    """A real (silent) audio file with the given tags, .m4b or .mp3 by extension."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if str(path).endswith(".mp3"):
        from mutagen.id3 import ID3, TALB, TIT2, TPE1

        shutil.copy(os.path.join(FIXTURES, "silence.mp3"), path)
        tags = ID3(path)
        tags.delall("TSSE")
        for frame, value in ((TALB, album), (TIT2, title), (TPE1, artist)):
            if value:
                tags.add(frame(encoding=3, text=[value]))
        tags.save(path)
    else:
        from mutagen.mp4 import MP4

        shutil.copy(os.path.join(FIXTURES, "silence.m4b"), path)
        audio = MP4(path)
        for key, value in (("\xa9alb", album), ("\xa9nam", title), ("\xa9ART", artist)):
            if value:
                audio[key] = [value]
        audio.save()
    return str(path)


# ---------------------------------------------------------------------------
# 1. Audio tags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["book.m4b", "chapter.mp3"])
def test_tags_are_read_from_real_files(tmp_path, name):
    path = audio_file(tmp_path / name, album="Dune (Unabridged)", title="Chapter 1", artist=HERBERT)
    # "credits" holds every artist and album-artist value (multi-value frames), for the identity check
    assert read_tags(path) == {"album": "Dune (Unabridged)", "title": "Chapter 1", "artist": HERBERT, "credits": HERBERT}


def test_files_without_tags_or_audio_give_no_tags(tmp_path):
    assert read_tags(audio_file(tmp_path / "a.mp3")) == {}
    (tmp_path / "junk.mp3").write_bytes(b"not audio at all")
    (tmp_path / "junk.m4b").write_bytes(b"not audio at all")
    assert read_tags(str(tmp_path / "junk.mp3")) == {}
    assert read_tags(str(tmp_path / "junk.m4b")) == {}
    assert read_tags(str(tmp_path / "missing.m4b")) == {}


@pytest.mark.parametrize(
    "text, title, series, siblings, verdict",
    [
        ("Dune", "Dune", "", [], AGREE),
        ("Dune (Unabridged)", "Dune", "", [], AGREE),
        ("Frank Herbert - Dune", "Dune", "", [], AGREE),
        ("Red Rising (Red Rising Saga #1)", "Red Rising", "Red Rising Saga #1", [], AGREE),
        # Clearly another book
        ("Dune Messiah", "Dune", "", ["Dune Messiah", "Children of Dune"], MISMATCH),
        ("Golden Son", "Red Rising", "Red Rising Saga #1", ["Golden Son"], MISMATCH),
        ("Red Rising (Books 1-3)", "Red Rising", "", [], MISMATCH),
        ("Red Rising, Book 2", "Red Rising", "Red Rising Saga #1", [], MISMATCH),
        # Not clear enough to turn a download away
        ("Dune Messiah", "Dune", "", [], UNCLEAR),  # without Chaptarr's list it could be a subtitle
        ("Dune Disc 1", "Dune", "", [], UNCLEAR),
        ("Dune: Book One in the Dune Chronicles", "Dune", "", [], UNCLEAR),
        ("The Stormlight Archive", "The Way of Kings", "The Stormlight Archive #1", [], UNCLEAR),  # the series as album
        ("Frank Herbert", "Dune", "", [], UNCLEAR),
        ("Unknown Album", "Dune", "", [], UNCLEAR),
        ("Golden Son", "Red Rising", "Red Rising Saga #1", [], UNCLEAR),  # not a book Chaptarr lists: no proof
        ("Der Wüstenplanet", "Dune", "", [], UNCLEAR),  # a title in another language
    ],
)
def test_what_a_tag_says_about_the_book(text, title, series, siblings, verdict):
    assert judge_text(text, title, HERBERT, series, [], siblings)[0] == verdict


def test_sibling_does_not_overrule_the_wanted_title():
    # Wanted "Dune Messiah"; "Dune" is the sibling. The tag names the wanted book.
    assert judge_text("Dune Messiah", "Dune Messiah", HERBERT, "", [], ["Dune"])[0] == AGREE
    assert judge_text("Dune", "Dune Messiah", HERBERT, "", [], ["Dune"])[0] == MISMATCH


def test_single_file_may_carry_the_book_in_either_tag(tmp_path):
    ok = audio_file(tmp_path / "a" / "book.m4b", album="Dune Chronicles", title="Dune")
    assert check_audiobook_tags([ok], "Dune", HERBERT)[0] == AGREE
    wrong = audio_file(tmp_path / "b" / "book.m4b", album="Golden Son")
    verdict, detail = check_audiobook_tags([wrong], "Red Rising", BROWN, "Red Rising Saga #1", siblings=["Golden Son"])
    assert verdict == MISMATCH and "album tag of book.m4b" in detail and "Golden Son" in detail


def test_chapters_are_judged_by_their_album_not_their_chapter_titles(tmp_path):
    paths = [audio_file(tmp_path / f"{i:02d}.mp3", album="Dune", title=f"Chapter {i}") for i in range(1, 6)]
    assert check_audiobook_tags(paths, "Dune", HERBERT)[0] == AGREE


def test_one_file_of_another_book_is_enough(tmp_path):
    paths = [audio_file(tmp_path / f"{i:02d}.mp3", album="Golden Son" if i == 5 else "Red Rising") for i in range(1, 6)]
    assert check_audiobook_tags(paths, "Red Rising", BROWN, "Red Rising Saga #1", siblings=["Golden Son"])[0] == MISMATCH
    assert check_audiobook_tags(paths, "Red Rising", BROWN)[0] == AGREE  # not a known book: the other files decide


def test_untagged_and_vague_downloads_are_not_a_mismatch(tmp_path):
    untagged = [audio_file(tmp_path / "u" / f"{i:02d}.mp3") for i in range(1, 4)]
    assert check_audiobook_tags(untagged, "Dune", HERBERT)[0] == UNTAGGED
    vague = [audio_file(tmp_path / "v" / f"{i:02d}.mp3", album="Dune Disc 1") for i in range(1, 4)]
    assert check_audiobook_tags(vague, "Dune", HERBERT)[0] == UNCLEAR
    assert check_audiobook_tags([], "Dune", HERBERT)[0] == UNTAGGED


# --- the check inside the import step ---


class Chaptarr:
    """Chaptarr-shaped preview, import receipt and source-correlated book files."""

    def __init__(self, book=None, author_books=(), takes_files=True):
        self.book = book or {}
        self.author_books = list(author_books)
        self.author_series = {}  # title -> seriesTitle of the author's other books
        self.takes_files = takes_files
        self.commands = []
        self.bookfiles = []
        self.events = []
        self.ver_uri = "/api/v1"

    def get_book(self, book_id):
        return dict(self.book, id=book_id)

    def get_author(self, author_id):
        return {"id": author_id, "authorName": self.author_name}

    def get_edition(self, book_id):
        return [{"id": 20, "bookId": book_id, "readingFormatId": 2, "monitored": True}]

    def _get(self, path, ver_uri="", params=None):
        if path == "bookfile":
            return list(self.bookfiles)
        if path == "history":
            return {"records": list(self.events), "totalRecords": len(self.events)}
        if path == "manualimport":
            return [
                {"path": os.path.join(params["folder"], name), "quality": {"quality": {"id": 1}}}
                for name in os.listdir(params["folder"])
                if os.path.isfile(os.path.join(params["folder"], name))
            ]
        assert path == "book" and params == {"authorId": self.book.get("authorId")}
        return [{"title": t, "authorId": self.book.get("authorId"), "seriesTitle": self.author_series.get(t, "")} for t in self.author_books]

    def post_command(self, name, **kwargs):
        paths = [item["path"] for item in kwargs["files"]] if name == "ManualImport" else [
            os.path.join(kwargs["path"], name) for name in os.listdir(kwargs["path"])
            if os.path.isfile(os.path.join(kwargs["path"], name))
        ]
        self.commands.append(os.path.dirname(paths[0]))
        if self.takes_files:
            for path in paths:
                file_id = len(self.bookfiles) + 1
                imported_path = f"/library/book-{file_id}.m4b"
                self.bookfiles.append({"id": file_id, "bookId": 7, "authorId": 3,
                                       "path": imported_path, "size": os.path.getsize(path), "mediaType": "audiobook"})
                self.events.append({"id": file_id, "bookId": 7, "authorId": 3,
                                    "date": datetime.now(timezone.utc).isoformat(), "eventType": "bookFileImported",
                                    "data": {"FileId": str(file_id), "DroppedPath": path, "ImportedPath": imported_path}})
                os.remove(path)
        return {"id": 1, "body": {"path": self.commands[-1]}}

    def get_command(self, id_):
        return {"id": 1, "status": "completed", "result": "successful", "message": "", "body": {"path": ""}}


def run_import(tmp_path, album, title="Red Rising", author=BROWN, chaptarr=None, files=("book.m4b",), **postprocessing):
    local = tmp_path / "dl"
    for name in files:
        audio_file(local / "Folder" / name, album=album)
    chaptarr = chaptarr or Chaptarr()
    chaptarr.local_root = str(local)
    chaptarr.author_name = author
    chaptarr.book.setdefault("authorId", 3)
    chaptarr.book.setdefault("title", title)
    chaptarr.book.setdefault("mediaType", "audiobook")
    config = make_config(slskd={"download_dir": str(local)})
    config["Readarr"]["import_poll_timeout"] = "0"
    config["Postprocessing"] = {k: str(v) for k, v in postprocessing.items()}
    history = HistoryManager(str(tmp_path))
    ctx = Context(config=config, slskd=None, readarr=chaptarr, history=history)
    item = {"author_name": author, "title": title, "bookId": 7, "dir": "Folder", "filename": files[0], "expected_files": list(files), "backend_name": "slskd", "media_type": "audiobook", "source_id": "peer"}
    return postprocess.process_imports(ctx, [item]), chaptarr, history, local


def red_rising_chaptarr():
    return Chaptarr(book={"authorId": 3, "title": "Red Rising", "seriesTitle": "Red Rising Saga #1"}, author_books=["Red Rising", "Golden Son", "Morning Star"])


def test_download_tagged_as_another_book_is_not_imported(tmp_path):
    result, chaptarr, history, local = run_import(tmp_path, album="Golden Son", chaptarr=red_rising_chaptarr())

    assert result[7]["phase"] == "needs_review"
    assert chaptarr.commands == []  # never handed to Chaptarr
    assert (local / "Folder" / "book.m4b").exists()
    assert "Audio tags identify another work" in result[7]["reason"]
    assert history.history == []


def test_download_tagged_as_the_book_is_imported(tmp_path):
    result, chaptarr, history, _ = run_import(tmp_path, album="Red Rising (Unabridged)")
    assert result == {7: True}
    assert len(chaptarr.commands) == 1
    assert history.history == []


@pytest.mark.parametrize("album", [None, "Unknown Album", "Red Rising Disc 1"])
def test_untagged_or_vague_download_is_imported(tmp_path, album):
    result, chaptarr, _, _ = run_import(tmp_path, album=album)
    assert result == {7: True}


def test_other_book_of_the_author_is_recognised_from_chaptarrs_list(tmp_path):
    chaptarr = Chaptarr(book={"authorId": 3, "title": "Dune", "seriesTitle": "Dune #1"}, author_books=["Dune", "Dune Messiah", "Children of Dune"])
    result, chaptarr, history, _ = run_import(tmp_path, album="Dune Messiah", title="Dune", author=HERBERT, chaptarr=chaptarr)
    assert result[7]["phase"] == "needs_review"
    assert chaptarr.commands == []
    assert '"Dune Messiah"' in result[7]["reason"]
    assert history.history == []


def test_series_position_from_chaptarr_is_used(tmp_path):
    chaptarr = Chaptarr(book={"authorId": 3, "title": "Red Rising", "seriesTitle": "Red Rising Saga #1"})
    result, _, _, _ = run_import(tmp_path, album="Red Rising Saga, Book 2", chaptarr=chaptarr)
    assert result[7]["phase"] == "needs_review"


@pytest.mark.parametrize("setting", [{"audiobook_tag_check": "warn"}, {"audiobook_tag_check": "off"}, {"skip_validation": True}])
def test_check_can_be_softened_or_switched_off(tmp_path, setting):
    result, chaptarr, history, _ = run_import(tmp_path, album="Golden Son", chaptarr=red_rising_chaptarr(), **setting)
    assert result == {7: True}
    assert len(chaptarr.commands) == 1
    assert history.history == []


def test_tag_check_trouble_defers_strict_import_without_moving_files(tmp_path, monkeypatch):
    monkeypatch.setattr(audiotags, "check_audiobook_tags", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    result, chaptarr, history, local = run_import(tmp_path, album="Golden Son", chaptarr=red_rising_chaptarr())
    assert result[7]["phase"] == "validating"
    assert "Audio tag comparison is unavailable" in result[7]["reason"]
    assert not chaptarr.commands and not history.history
    assert (local / "Folder" / "book.m4b").exists()


@pytest.mark.parametrize("album,expected", [("Golden Son", "needs_review"), ("Red Rising", True)])
def test_catalog_outage_defers_then_rechecks_tags_after_restart(tmp_path, album, expected):
    chaptarr = red_rising_chaptarr()
    original = chaptarr._get
    unavailable = [True]

    def get(resource, *args, **kwargs):
        if resource == "book" and unavailable[0]:
            raise TimeoutError("catalog temporarily unavailable")
        return original(resource, *args, **kwargs)

    chaptarr._get = get
    result, _, history, local = run_import(tmp_path, album=album, chaptarr=chaptarr)
    pending = result[7]
    assert pending["phase"] == "validating" and pending["before_ids"] is None
    assert (local / "Folder" / "book.m4b").exists() and not chaptarr.commands
    unavailable[0] = False
    config = make_config(slskd={"download_dir": str(local)})
    config["Readarr"]["import_poll_timeout"] = "0"
    resumed = postprocess.check_pending_import(chaptarr, pending, config=config)
    if expected is True:
        assert resumed is True and len(chaptarr.commands) == 1
    else:
        assert resumed["phase"] == "needs_review" and "Golden Son" in resumed["reason"]
        assert (local / "Folder" / "book.m4b").exists()
        assert not chaptarr.commands
    assert not history.history


@pytest.mark.parametrize("mode", ["warn", "off"])
def test_explicit_tag_override_allows_catalog_outage_with_full_import_proof(tmp_path, mode):
    chaptarr = red_rising_chaptarr()
    original = chaptarr._get

    def get(resource, *args, **kwargs):
        if resource == "book":
            raise TimeoutError("catalog temporarily unavailable")
        return original(resource, *args, **kwargs)

    chaptarr._get = get
    result, _, history, _ = run_import(tmp_path, album="Golden Son", chaptarr=chaptarr, audiobook_tag_check=mode)
    assert result == {7: True}
    assert len(chaptarr.commands) == 1 and not history.history


def test_missing_mutagen_skips_the_check(tmp_path, monkeypatch):
    path = audio_file(tmp_path / "book.m4b", album="Golden Son")
    monkeypatch.setitem(sys.modules, "mutagen", None)  # importing it now fails
    assert read_tags(path) == {}
    assert check_audiobook_tags([path], "Red Rising", BROWN, siblings=["Golden Son"])[0] == UNTAGGED


def test_tag_check_setting_is_validated():
    config = sample_config()
    config["Slskd"]["download_dir"] = "/downloads"
    validate_config(config)  # the sample's "reject" is fine
    config["Postprocessing"]["audiobook_tag_check"] = "strict"
    with pytest.raises(ValueError, match="audiobook_tag_check"):
        validate_config(config)


# ---------------------------------------------------------------------------
# 2. The author's other titles
# ---------------------------------------------------------------------------


def ctx_with_books(titles, author_id=3):
    chaptarr = Chaptarr(book={"authorId": author_id}, author_books=titles)
    return Context(config=make_config(), slskd=None, readarr=chaptarr), chaptarr


def test_sibling_titles_are_the_authors_other_books():
    ctx, _ = ctx_with_books(["Dune", "Dune (Unabridged)", "Dune Messiah", "Children of Dune"])
    assert sibling_titles(ctx, {"authorId": 3, "title": "Dune"}, HERBERT) == ["Dune Messiah", "Children of Dune"]
    assert sibling_titles(ctx, {"authorId": 3, "title": "Dune Messiah"}, HERBERT) == ["Dune", "Dune (Unabridged)", "Children of Dune"]


def test_sibling_titles_are_fetched_once_per_author_and_survive_failures():
    ctx, chaptarr = ctx_with_books(["Dune", "Dune Messiah"])
    calls = []
    original = chaptarr._get
    chaptarr._get = lambda *a, **k: (calls.append(1), original(*a, **k))[1]
    for _ in range(3):
        sibling_titles(ctx, {"authorId": 3, "title": "Dune"}, HERBERT)
    assert len(calls) == 1

    broken = Context(config=make_config(), slskd=None, readarr=object())  # no such API
    assert sibling_titles(broken, {"authorId": 3, "title": "Dune"}, HERBERT) == []
    assert sibling_titles(ctx, {"title": "Dune"}, HERBERT) == []  # no author id


def dune(siblings=()):
    return {"book": {"title": "Dune"}, "author": {"authorName": HERBERT}, "sibling_titles": list(siblings)}


def test_lone_file_named_like_another_book_is_not_the_book():
    siblings = ["Dune Messiah", "Children of Dune"]
    assert books_in_folder(dune(siblings), [("Dune Messiah.m4b", BOOK)]) == ("several", [])
    assert books_in_folder(dune(siblings), [("Frank Herbert - Dune Messiah (Unabridged).m4b", 20 * MB)]) == ("several", [])  # whatever its size
    # ...but an oddly named file of the book itself is kept
    assert books_in_folder(dune(siblings), [("dune_fh_64k.m4b", BOOK)]) == ("folder", [])
    assert books_in_folder(dune(siblings), [("Dune.m4b", BOOK)]) == ("folder", [])
    assert books_in_folder(dune(), [("Dune Messiah.m4b", BOOK)]) == ("folder", [])  # without the list: not judged


def test_small_shelf_is_recognised_with_the_authors_titles():
    small = 40 * MB  # below the size from which a file counts as a book on its own
    files = [("Dune Messiah.mp3", small), ("Children of Dune.mp3", small)]
    assert books_in_folder(dune(), files) == ("folder", [])
    assert books_in_folder(dune(["Dune Messiah", "Children of Dune"]), files) == ("several", [])


def test_search_uses_the_authors_titles():
    directory = "@@s\\Frank Herbert\\Dune"
    client = Client(responses=[{"username": "peer", "files": [{"filename": f"{directory}\\Dune Messiah.m4b", "size": BOOK}]}], listings={("peer", directory): [{"filename": "Dune Messiah.m4b", "size": BOOK}]})
    b = backend(client)
    b.ctx.readarr = Chaptarr(book={"authorId": 3}, author_books=["Dune", "Dune Messiah"])
    target = DownloadTarget(book_id=7, book_title="Dune", author_name=HERBERT, series_title="", allowed_filetypes=["m4b"], readarr_book={"title": "Dune", "id": 7, "authorId": 3}, readarr_author={"authorName": HERBERT}, media_type="audiobook")

    assert b.search(target) == []
    assert "several other books" in b.search_notes[7]["skipped"][0]["reason"]


# ---------------------------------------------------------------------------
# 3. Narrators
# ---------------------------------------------------------------------------


def test_known_narrator_is_not_another_title():
    name = "Red Rising - Tim Gerard Reynolds"
    assert not title_segment_match("Red Rising", name, BROWN)
    assert title_segment_match("Red Rising", name, BROWN, "", ["Tim Gerard Reynolds"])
    assert not title_segment_match("Red Rising", "Red Rising - Golden Son", BROWN, "", ["Tim Gerard Reynolds"])


@pytest.mark.parametrize("field", ["narratorNames", "availableNarrators"])
def test_narrators_from_chaptarr_reach_the_matcher(field):
    path = "@@s\\Audiobooks\\Pierce Brown - Red Rising - Tim Gerard Reynolds.m4b"
    files = [{"filename": path, "size": BOOK}]
    without = {"book": {"title": "Red Rising"}, "author": {"authorName": BROWN}}
    known = {"book": {"title": "Red Rising", field: ["Tim Gerard Reynolds"]}, "author": {"authorName": BROWN}}
    assert audiobook_folder_match(without, files, "peer", ["m4b"], [], 0.7) == []
    assert len(audiobook_folder_match(known, files, "peer", ["m4b"], [], 0.7)) == 1


# ---------------------------------------------------------------------------
# 4. Duration
# ---------------------------------------------------------------------------


def duration_search(size, minutes):
    directory = "@@s\\Frank Herbert\\Dune"
    client = Client(responses=[{"username": "peer", "files": [{"filename": f"{directory}\\Dune.m4b", "size": size}]}], listings={("peer", directory): [{"filename": "Dune.m4b", "size": size}]})
    book = {"title": "Dune", "id": 7}
    if minutes is not None:
        book["durationMinutes"] = minutes
    target = DownloadTarget(book_id=7, book_title="Dune", author_name=HERBERT, series_title="", allowed_filetypes=["m4b"], readarr_book=book, readarr_author={"authorName": HERBERT}, media_type="audiobook")
    b = backend(client)
    return b.search(target), b.search_notes[7]["skipped"]


def test_download_far_too_small_for_the_duration_is_skipped():
    results, skipped = duration_search(60 * MB, 21 * 60)  # 60 MB for 21 hours: about 6 kbit/s
    assert results == []
    assert "too small for a book of 21.0 hours" in skipped[0]["reason"] and skipped[0]["temporary"] is False


@pytest.mark.parametrize("size, minutes", [(600 * MB, 21 * 60), (160 * MB, 21 * 60), (60 * MB, None), (60 * MB, 0), (60 * MB, "n/a"), (12 * MB, 20)])
def test_plausible_or_unknown_durations_pass(size, minutes):
    results, _ = duration_search(size, minutes)
    assert len(results) == 1
