"""Findings from the external review of 0.1.0-beta.1, reproduced and fixed.

1. A title word was taken out of a separate author folder: "Audiobooks\\Carrie King\\Carrie"
   left the bare surname King and was accepted as Stephen King's Carrie. Title words are now
   only set aside in file names ("Resistance Brown.m4b"); a folder is read as written.
5. Work staged under older rules and interrupted half-way through staging (some files moved,
   some still at the source) was stranded for review instead of validated as one complete set.
6. In a music folder, an audiobook whose *file* names the author in full was rejected before
   the file name was looked at.
"""

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_author_identity import match, staged_under_old_rules
from test_import_transaction import run, saved_pending, setup, writer

from rsoul import import_transaction, postprocess
from rsoul.match import BARE, CONFLICT, FULL, author_verdict


# ---------------------------------------------------------------------------
# 1. A conflicting person whose first name is the book's title
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title, author, path", [
    ("Carrie", "Stephen King", r"Audiobooks\Carrie King\Carrie\Carrie 01.mp3"),
    ("James", "Percival Everett", r"Audiobooks\James Everett\James\James 01.mp3"),
    ("Carrie", "Stephen King", r"Audiobooks\Carrie King\Carrie.m4b"),
])
def test_a_folder_naming_another_person_is_not_cleared_by_the_title(title, author, path):
    assert author_verdict(author, path, title) == CONFLICT
    assert match(title, author, path) == []


@pytest.mark.parametrize("title, author, path, verdict", [
    ("Resistance", "Ryk Brown", r"Audiobooks\Resistance - Brown.m4b", BARE),  # separated: title, then surname
    ("Resistance", "Ryk Brown", r"Audiobooks\Ryk Brown\Resistance Brown.m4b", FULL),
    ("Carrie", "Stephen King", r"Audiobooks\Stephen King\Carrie\Carrie King.m4b", FULL),
    ("Carrie", "Stephen King", r"Audiobooks\King\Carrie\Carrie - King.m4b", BARE),
])
def test_title_and_surname_file_names_still_work(title, author, path, verdict):
    assert author_verdict(author, path, title) == verdict
    assert len(match(title, author, path)) == 1


# Review of 0.1.0-beta.2: with no separator, "Carrie King.m4b" for Stephen King's Carrie is
# either title + surname or a person called Carrie King. Untagged, nothing tells them apart,
# so only the author named in full elsewhere in the path accepts it.
@pytest.mark.parametrize("title, author, path", [
    ("Carrie", "Stephen King", r"Audiobooks\Carrie King.m4b"),
    ("Carrie", "Stephen King", r"Audiobooks\King, Carrie.m4b"),
    ("Carrie", "Stephen King", r"Audiobooks\King\Carrie King.m4b"),  # a bare surname folder does not settle it
    ("Resistance", "Ryk Brown", r"Audiobooks\Resistance Brown.m4b"),
])
def test_a_title_word_beside_the_surname_in_a_file_name_is_not_evidence(title, author, path):
    assert author_verdict(author, path, title) not in (FULL, BARE)
    assert match(title, author, path) == []


def test_a_conflicting_folder_reaches_no_import_command(tmp_path):
    # The test book is "Intended Work" by "Correct Author": a folder named "Work Author" is a
    # person called Work Author, whatever the title says
    ctx, item, server, _ = setup(tmp_path)
    item["full_dir"] = item["_task_extra"]["full_dir"] = "Audiobooks\\Work Author\\Intended Work"
    result = run(ctx, item)
    assert result["phase"] == "needs_review" and "another person" in result["reason"]
    assert server.posts == []
    assert all(Path(f["source_path"]).exists() for f in result["manifest"])


def test_the_author_folder_still_imports(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    item["full_dir"] = item["_task_extra"]["full_dir"] = "Audiobooks\\Correct Author\\Intended Work"
    assert run(ctx, item) is True
    assert [name for name, _ in server.posts] == ["ManualImport"]


# ---------------------------------------------------------------------------
# 5. Staging interrupted half-way, then an upgrade
# ---------------------------------------------------------------------------


def test_half_staged_work_from_an_earlier_build_is_validated_as_one_set_and_imported(tmp_path):
    ctx, server = staged_under_old_rules(tmp_path, "Audiobooks\\Correct Author\\Intended Work")
    saved = saved_pending(ctx)
    first = saved["manifest"][0]
    Path(first["source_path"]).parent.mkdir(parents=True, exist_ok=True)
    shutil.move(first["stage_path"], first["source_path"])  # this one never got moved
    saved["phase"] = "staging"
    writer(ctx)(saved)

    seen = []
    real = import_transaction.validate_inputs
    def spy(ctx_, item, pending):
        seen.append(sorted(f["source_path"] for f in pending["manifest"]))
        return real(ctx_, item, pending)
    import_transaction.validate_inputs = spy
    try:
        result = postprocess.check_pending_import(server, saved_pending(ctx), config=ctx.config, persist=writer(ctx))
    finally:
        import_transaction.validate_inputs = real
    assert result is True
    assert [name for name, _ in server.posts] == ["ManualImport"]
    # both files were validated, each where it actually was
    assert seen and seen[0] == sorted([first["source_path"], saved["manifest"][1]["stage_path"]])


def test_half_staged_work_outside_the_mount_is_still_held(tmp_path):
    ctx, server = staged_under_old_rules(tmp_path, "Audiobooks\\Correct Author\\Intended Work")
    saved = saved_pending(ctx)
    outside = tmp_path / "outside" / "chapter0.wav"
    outside.parent.mkdir()
    shutil.move(saved["manifest"][0]["stage_path"], outside)
    saved["manifest"][0]["source_path"] = str(outside)
    writer(ctx)(saved)
    result = postprocess.check_pending_import(server, saved_pending(ctx), config=ctx.config, persist=writer(ctx))
    assert result["phase"] == "needs_review" and server.posts == []


# ---------------------------------------------------------------------------
# 6. Music folders: the file name counts too
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    r"Music\Resistance\Ryk Brown - Resistance.m4b",
    r"Music\Ryk Brown - Resistance.m4b",
])
def test_an_m4b_naming_the_author_in_full_is_accepted_in_a_music_folder(path):
    assert len(match("Resistance", "Ryk Brown", path)) == 1


@pytest.mark.parametrize("path", [
    r"Music\Resistance\Ryk Brown - Resistance.mp3",  # mp3 in a music folder needs an m4b or a narrator
    r"Music\Resistance\Brown - Resistance.m4b",  # surname only
])
def test_music_folders_still_need_strong_evidence(path):
    assert match("Resistance", "Ryk Brown", path) == []


def test_an_ambiguous_file_name_queued_earlier_reaches_no_import_command(tmp_path):
    # "Intended Work Author.wav" for "Intended Work" by "Correct Author": a title word beside the
    # surname, as "Carrie King.m4b" is for Stephen King's Carrie
    ctx, item, server, _ = setup(tmp_path, count=1)
    old = Path(tmp_path / "downloads" / "peer-folder" / item["expected_files"][0])
    new_name = "Work Author.wav"
    old.rename(old.with_name(new_name))
    item.update(filename=new_name, expected_files=[new_name])
    item["_task_extra"]["expected_files"] = [new_name]
    item["full_dir"] = item["_task_extra"]["full_dir"] = "Audiobooks"
    result = run(ctx, item)
    assert result["phase"] == "needs_review" and "may name another person" in result["reason"]
    assert server.posts == []


# ---------------------------------------------------------------------------
# Review of 0.1.0-beta.3: the author in a parent folder does not clear another person below it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title, author, path", [
    ("Intended Work", "Correct Author", r"Audiobooks\Correct Author\Wrong Author\Intended Work\chapter0.wav"),
    ("Resistance", "Ryk Brown", r"Audiobooks\Ryk Brown\Scott Brown\Resistance\01.mp3"),
    ("Resistance", "Ryk Brown", r"Audiobooks\Ryk Brown\Resistance\Scott Brown - Resistance.m4b"),
])
def test_another_person_below_the_authors_folder_is_decisive(title, author, path):
    assert author_verdict(author, path, title) == CONFLICT
    assert match(title, author, path) == []


@pytest.mark.parametrize("title, author, path", [
    ("Resistance", "Ryk Brown", r"Audiobooks\Ryk Brown & Scott Brown\Resistance\01.mp3"),  # co-credit, one segment
    ("Resistance", "Ryk Brown", r"Audiobooks\Scott Brown\Resistance\Ryk Brown - Resistance.m4b"),  # the file says whose
])
def test_a_co_credit_or_a_more_specific_full_name_still_counts(title, author, path):
    assert author_verdict(author, path, title) == FULL
    assert len(match(title, author, path)) == 1


def test_both_authors_in_the_path_reach_no_import_command(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    item["full_dir"] = item["_task_extra"]["full_dir"] = "Audiobooks\\Correct Author\\Wrong Author\\Intended Work"
    result = run(ctx, item)
    assert result["phase"] == "needs_review" and "another person" in result["reason"]
    assert server.posts == []


# ---------------------------------------------------------------------------
# Review of 0.1.0-beta.4: only a genuine list of people is a co-credit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    r"Audiobooks\Correct Author - Wrong Author\Intended Work\chapter0.wav",
    r"Audiobooks\Correct Author (Wrong Author Remix)\Intended Work\chapter0.wav",
    r"Audiobooks\Wrong Author - Correct Author\Intended Work\chapter0.wav",
    r"Audiobooks\Correct Author [Wrong Author]\Intended Work\chapter0.wav",
])
def test_two_people_side_by_side_are_no_co_credit(path):
    assert author_verdict("Correct Author", path, "Intended Work") == CONFLICT
    assert match("Intended Work", "Correct Author", path) == []


@pytest.mark.parametrize("folder", ["Audiobooks\\Correct Author - Wrong Author\\Intended Work",
                                    "Audiobooks\\Correct Author (Wrong Author Remix)\\Intended Work"])
def test_two_people_side_by_side_reach_no_import_command(tmp_path, folder):
    ctx, item, server, _ = setup(tmp_path)
    item["full_dir"] = item["_task_extra"]["full_dir"] = folder
    result = run(ctx, item)
    assert result["phase"] == "needs_review" and "another person" in result["reason"]
    assert server.posts == []


@pytest.mark.parametrize("path", [
    r"Audiobooks\Ryk Brown & Scott Brown\Resistance\01.mp3",
    r"Audiobooks\Ryk Brown and Scott Brown\Resistance\01.mp3",
    r"Audiobooks\Brown, Ryk + Scott Brown\Resistance\01.mp3",
])
def test_people_credited_together_are_a_co_credit(path):
    assert author_verdict("Ryk Brown", path, "Resistance") == FULL
    assert len(match("Resistance", "Ryk Brown", path)) == 1


def test_a_co_credit_in_a_file_name_is_read_as_one():
    # The author identity is a co-credit; the title check then decides on the extra words
    assert author_verdict("Ryk Brown", r"Audiobooks\Resistance - Ryk Brown & Scott Brown.m4b", "Resistance") == FULL


def test_file_names_joined_for_the_evidence_check_are_read_one_by_one():
    # "... - 01.mp3 Frank Herbert - Dune - 02.mp3": the extension is no first name
    joined = r"Audiobooks\Frank Herbert - Dune - 01.mp3 Frank Herbert - Dune - 02.mp3 Frank Herbert - Dune - 03.mp3"
    assert author_verdict("Frank Herbert", joined, "Dune") == FULL
