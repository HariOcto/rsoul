"""Second review of the wrong-author fix: each finding, reproduced first, then fixed.

1. A bare surname elsewhere cleared a conflicting full name; a matching middle initial or surname
   particle counted as the author named in full.
2. Unsubmitted work already staged under the older rules skipped the new validation.
3. One correctly credited file hid a wrongly credited file next to it; multi-value artist tags
   were reduced to their first value.
4. Search and import judged evidence with different inputs (narrators); a listed narrator who
   shares the author's surname counted as another author.
5. "Title - Surname" read the title as a given name; the fallback queries lost "Tolkien - Hobbit".
6. A music folder vetoed a strongly identified audiobook; folder words were matched as substrings.
7. The shipped minimum running time, through a whole transaction: review, files kept, no import.
"""

import os
import shutil
import sys
from configparser import ConfigParser
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_v11_imports import run, saved_pending, setup, writer

from rsoul import audiotags, import_transaction, postprocess
from rsoul.backends.slskd_backend import _generate_fallback_queries
from rsoul.match import (CONFLICT, FULL, BARE, audiobook_evidence, audiobook_folder_match, author_mentions_detail,
                         author_verdict, music_library)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
THRESHOLDS = dict(ignored_users=[], minimum_match_ratio=0.7, min_length_ratio=0.4, min_jaccard_ratio=0.25,
                  min_word_overlap=2, min_title_jaccard=0.3, min_author_jaccard=0.5)
MB = 1024 * 1024


def match(title, author, path, narrators=None, size=300 * MB):
    book = {"title": title}
    if narrators:
        book["narratorNames"] = narrators
    return audiobook_folder_match({"book": book, "author": {"authorName": author}}, [{"filename": path, "size": size}], "peer", ["m4b", "mp3"], **THRESHOLDS)


# ---------------------------------------------------------------------------
# 1. Author identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title, author, path", [
    ("Resistance", "Ryk Brown", r"Audiobooks\Brown\Scott Brown\Resistance\01.mp3"),
    ("Salvation", "Peter F. Hamilton", r"shared\Michael F. Hamilton\Salvation\01.mp3"),
    ("The Dispossessed", "Ursula K. Le Guin", r"shared\Anne Le Guin\The Dispossessed\01.mp3"),
    ("The Dispossessed", "Ursula K. Le Guin", r"Audiobooks\Guin, Anne Le\The Dispossessed\01.mp3"),
])
def test_another_person_with_the_surname_is_never_the_author(title, author, path):
    assert match(title, author, path) == []
    assert author_verdict(author, path, title) == CONFLICT


@pytest.mark.parametrize("author, text, verdict", [
    ("Peter F. Hamilton", "Peter F. Hamilton", FULL),
    ("Peter F. Hamilton", "Peter Hamilton", FULL),
    ("Peter F. Hamilton", "P. F. Hamilton", FULL),
    ("Peter F. Hamilton", "F. Hamilton", BARE),  # only the middle initial: compatible, not the author in full
    ("Ursula K. Le Guin", "Ursula K. Le Guin", FULL),
    ("Ursula K. Le Guin", "Le Guin", BARE),
    ("Ursula K. Le Guin", "Guin", BARE),
    ("Iain M. Banks", "Banks, Iain M", FULL),
    ("Iain M. Banks", "Banks, Iain", FULL),
    ("J.R.R. Tolkien", "J.R.R. Tolkien", FULL),
    ("J.R.R. Tolkien", "Tolkien", BARE),
    ("Ryk Brown", "Brown, Scott", CONFLICT),
    ("Ryk Brown", "Ryk Brown & Scott Brown", FULL),  # a co-credit naming the author in full
    ("Ryk Brown", r"Audiobooks\Brown\Scott Brown", CONFLICT),  # a bare surname never clears a conflict
])
def test_person_names_are_read_as_a_whole(author, text, verdict):
    assert author_verdict(author, text) == verdict


# ---------------------------------------------------------------------------
# 2. Staged work validated under older rules
# ---------------------------------------------------------------------------


def staged_under_old_rules(tmp_path, peer_folder):
    ctx, item, server, _ = setup(tmp_path)
    item["_task_extra"]["full_dir"] = "Audiobooks\\Correct Author\\Intended Work"
    server.preview_error = TimeoutError("import preview unavailable")  # validated and staged, not submitted
    pending = run(ctx, item)
    assert pending["phase"] == "staged" and not server.posts
    server.preview_error = None
    saved = saved_pending(ctx)
    saved.pop("validation_policy", None)  # what an earlier build saved
    saved["source_task"]["extra"]["full_dir"] = peer_folder
    writer(ctx)(saved)
    return ctx, server


def test_staged_music_from_an_earlier_build_is_held_for_review(tmp_path):
    ctx, server = staged_under_old_rules(tmp_path, "Music\\Scott Brown\\Hardcore Tracks")
    result = postprocess.check_pending_import(server, saved_pending(ctx), config=ctx.config, persist=writer(ctx))
    assert result["phase"] == "needs_review" and "Validation under the current rules" in result["reason"]
    assert server.posts == []
    assert all(Path(f["stage_path"]).exists() for f in result["manifest"])


def test_staged_audiobook_from_an_earlier_build_is_validated_then_imported(tmp_path):
    ctx, server = staged_under_old_rules(tmp_path, "Audiobooks\\Correct Author\\Intended Work")
    assert postprocess.check_pending_import(server, saved_pending(ctx), config=ctx.config, persist=writer(ctx)) is True
    assert [name for name, _ in server.posts] == ["ManualImport"]
    assert saved_pending(ctx)["validation_policy"] == import_transaction.VALIDATION_POLICY


def test_submitted_work_is_reconciled_not_revalidated(monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("a submitted import must not be validated again")
    monkeypatch.setattr(import_transaction, "_revalidate_owned", must_not_run)
    for record in ({"phase": "staged", "attempts": [{"command_id": 5}]}, {"phase": "staged", "command_id": 5}, {"phase": "staged", "legacy": True}):
        record.update(book_id=1, media_type="audiobook", manifest=[], schema=2)
        import_transaction.check_pending(SimpleNamespace(), record, config=None, persist=lambda v: None)


# ---------------------------------------------------------------------------
# 3. Artist credits, per file and with every value
# ---------------------------------------------------------------------------


def tagged(tmp_path, name, title="Resistance", album="Resistance", artist=None, artists=None):
    from mutagen.id3 import ID3, TALB, TIT2, TPE1
    path = tmp_path / name
    shutil.copy(os.path.join(FIXTURES, "silence.mp3"), path)
    tags = ID3(str(path))
    tags.add(TIT2(encoding=3, text=[title]))
    tags.add(TALB(encoding=3, text=[album]))
    if artist or artists:
        tags.add(TPE1(encoding=3, text=artists or [artist]))
    tags.save(str(path))
    return str(path)


def test_one_correct_credit_does_not_vouch_for_another_file(tmp_path):
    good = tagged(tmp_path, "01.mp3", artist="Ryk Brown")
    wrong = tagged(tmp_path, "02.mp3", artist="Scott Brown")
    assert audiotags.check_audiobook_tags([wrong], "Resistance", "Ryk Brown")[0] == audiotags.MISMATCH
    verdict, detail = audiotags.check_audiobook_tags([good, wrong], "Resistance", "Ryk Brown")
    assert verdict == audiotags.MISMATCH and "02.mp3" in detail


def test_every_value_of_a_multi_value_artist_tag_counts(tmp_path):
    both = tagged(tmp_path, "01.mp3", artists=["Scott Brown", "Ryk Brown"])
    assert audiotags.read_tags(both)["credits"] == "Scott Brown ; Ryk Brown"
    assert audiotags.check_audiobook_tags([both], "Resistance", "Ryk Brown")[0] == audiotags.AGREE  # a co-credit in the same file


# ---------------------------------------------------------------------------
# 4. One evidence decision, with the same inputs, at search and before import
# ---------------------------------------------------------------------------


def validate(full_dir, narrators=(), title="Resistance", author="Ryk Brown"):
    item = {"title": title, "author_name": author, "full_dir": full_dir, "dir": "", "filename": "01.mp3", "bookId": 6801}
    pending = {"media_type": "audiobook", "manifest": [{"name": "01.mp3", "source_path": "/nonexistent/01.mp3"}]}
    catalog = SimpleNamespace(get_book=lambda book_id: {"id": book_id, "narratorNames": list(narrators)})
    return import_transaction.validate_inputs(SimpleNamespace(config=ConfigParser(), readarr=catalog), item, pending)


def test_narrator_evidence_counts_at_search_and_before_import():
    path = r"shared\Brown\Resistance - Jeffrey Kafer"
    assert len(match("Resistance", "Ryk Brown", path + r"\01.mp3", narrators=["Jeffrey Kafer"])) == 1
    error = validate(path, narrators=["Jeffrey Kafer"])
    assert "marks it as" not in error and "another person" not in error  # gets past the evidence check


def test_a_listed_narrator_sharing_the_surname_is_not_another_author(tmp_path):
    credited = tagged(tmp_path, "01.mp3", artist="Scott Brown")
    assert audiotags.check_audiobook_tags([credited], "Resistance", "Ryk Brown", narrators=["Scott Brown"])[0] != audiotags.MISMATCH
    assert author_mentions_detail("Ryk Brown", r"Audiobooks\Scott Brown\Resistance", narrators=["Scott Brown"])["narrator"] == 1


def test_an_unreadable_catalog_defers_instead_of_rejecting():
    from rsoul.library import CatalogUnavailable
    item = {"title": "Resistance", "author_name": "Ryk Brown", "full_dir": r"shared\Brown\Resistance", "dir": "", "filename": "01.mp3", "bookId": 6801}
    pending = {"media_type": "audiobook", "manifest": [{"name": "01.mp3", "source_path": "/nonexistent/01.mp3"}]}
    broken = SimpleNamespace(get_book=lambda book_id: (_ for _ in ()).throw(ConnectionError("down")))
    with pytest.raises(CatalogUnavailable):
        import_transaction.validate_inputs(SimpleNamespace(config=ConfigParser(), readarr=broken), item, pending)


# ---------------------------------------------------------------------------
# 5. Naming and search variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title, author, path", [
    ("Resistance", "Ryk Brown", r"Audiobooks\Resistance - Brown.m4b"),
    ("The Hobbit", "J.R.R. Tolkien", r"Audiobooks\The Hobbit - Tolkien.m4b"),
    ("Resistance", "Ryk Brown", r"Audiobooks\Resistance Brown.m4b"),  # no separator: a title word is no given name
    ("Resistance", "Ryk Brown", r"Audiobooks\Brown_-_Resistance.m4b"),
])
def test_title_and_surname_fields_are_kept_apart(title, author, path):
    assert len(match(title, author, path)) == 1


@pytest.mark.parametrize("author, title, query", [
    ("J.R.R. Tolkien", "The Hobbit", "Tolkien - Hobbit"),
    ("Andy Weir", "The Martian", "Weir - Martian"),
    ("Ryk Brown", "The Vengeance", "Brown - Vengeance"),
])
def test_an_article_may_be_dropped_from_the_query(author, title, query):
    assert query in _generate_fallback_queries(author, title, 5)


def test_dropping_meaningful_words_still_leaves_two():
    queries = _generate_fallback_queries("Ryk Brown", "The First Ranger", 5)
    assert "Brown - First" not in queries and "Brown - Ranger" not in queries


# ---------------------------------------------------------------------------
# 6. Music folders weigh against, they don't veto; whole words only
# ---------------------------------------------------------------------------


def test_a_strongly_identified_audiobook_in_a_music_folder_is_accepted():
    assert len(match("Resistance", "Ryk Brown", r"Music\Ryk Brown\Resistance\Resistance.m4b")) == 1
    assert match("Resistance", "Ryk Brown", r"Music\Ryk Brown\Resistance\01.mp3") == []  # mp3 in Music needs more
    assert len(match("Resistance", "Ryk Brown", r"Music\Ryk Brown\Resistance - Jeffrey Kafer\01.mp3", narrators=["Jeffrey Kafer"])) == 1


@pytest.mark.parametrize("directory, music", [
    (r"Music\Booker Prize Shortlist", True),  # "Booker" is not "book"
    (r"Music\Romantic Classics", True),
    (r"Music\Audio Books\Robert Jordan", False),
    (r"Music\Books\Robert Jordan", False),
])
def test_folder_words_are_whole_words(directory, music):
    assert music_library(directory) is music


# ---------------------------------------------------------------------------
# 7. The shipped minimum running time, through a whole transaction
# ---------------------------------------------------------------------------


def test_a_short_download_is_held_with_its_files_and_nothing_is_imported(tmp_path, monkeypatch):
    monkeypatch.setattr(import_transaction, "DEFAULT_MIN_AUDIOBOOK_MINUTES", 15)  # as shipped
    ctx, item, server, _ = setup(tmp_path)
    item["_task_extra"]["full_dir"] = "Audiobooks\\Correct Author\\Intended Work"
    result = run(ctx, item)
    assert result["phase"] == "needs_review" and "not an audiobook" in result["reason"]
    assert server.posts == []
    assert saved_pending(ctx)["phase"] == "needs_review"
    assert all(Path(f["source_path"]).exists() for f in result["manifest"])
