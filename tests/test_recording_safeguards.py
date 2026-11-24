"""Selection and recording safeguards using real audio fixtures.

The slskd integration cases use the existing test client's directory and search
responses. Audio inspection and tag parsing use the installed Mutagen library
and real MP3/MP4 audio; no production audio-validity bypass is involved.
"""

import configparser
from pathlib import Path
import shutil

import pytest
from mutagen.id3 import ID3, TALB, TIT2, TLAN, TXXX
from mutagen.mp4 import MP4

from rsoul import audiotags
from rsoul.audiotags import AGREE, MISMATCH, UNCLEAR, check_audiobook_tags, judge_text, validate_audio_files
from rsoul.match import audiobook_folder_match, book_key, books_in_folder, fragment_release, missing_parts
from rsoul.recording_policy import evaluate_recording, recording_evidence, validate_recording_files


MB = 1024 * 1024
FIXTURES = Path(__file__).with_name("fixtures")
DUNE = {"book": {"title": "Dune", "seriesTitle": "Dune Chronicles #1"},
        "author": {"authorName": "Frank Herbert"}, "sibling_titles": ["Dune Messiah", "Children of Dune"]}


def preferences(**settings):
    config = configparser.ConfigParser(interpolation=None)
    config["Search Settings"] = {key: str(value) for key, value in settings.items()}
    return config


def audio_file(path, album=None, title=None):
    """Make an actual short silent recording, with optional book tags."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    extension = path.suffix.lower()
    shutil.copyfile(FIXTURES / ("silence.mp3" if extension == ".mp3" else "silence.m4b"), path)
    if extension == ".mp3":
        tags = ID3(path)
        tags.clear()
        for frame, value in ((TALB, album), (TIT2, title)):
            if value:
                tags.add(frame(encoding=3, text=[value]))
        tags.save(path)
    else:
        audio = MP4(path)
        audio.clear()
        for key, value in (("\xa9alb", album), ("\xa9nam", title)):
            if value:
                audio[key] = [value]
        audio.save()
    return str(path)


@pytest.mark.parametrize("size_mb", [1, 15, 80, 150])
def test_known_sequel_is_not_a_chapter_of_a_small_named_book(size_mb):
    assert books_in_folder(DUNE, [("Dune.m4b", size_mb * MB), ("Dune Messiah.m4b", 180 * MB)]) == ("own", ["Dune.m4b"])


def test_known_sequel_is_not_added_to_small_named_parts():
    files = [("Dune - 01.mp3", MB), ("Dune - 02.mp3", MB), ("Dune Messiah.mp3", MB)]
    assert books_in_folder(DUNE, files) == ("own", ["Dune - 01.mp3", "Dune - 02.mp3"])


@pytest.mark.parametrize("marker", ["Sample", "Free Sample", "Prologue", "Preview", "Excerpt", "Epilogue only"])
def test_raw_fragment_labels_survive_name_normalization(marker):
    directory = f"@@share\\Robert Jordan\\Winter's Heart [{marker}]"
    target = {"book": {"title": "Winter's Heart", "seriesTitle": "The Wheel of Time #9"}, "author": {"authorName": "Robert Jordan"}}
    matches = audiobook_folder_match(target, [{"filename": directory + "\\01.mp3", "size": 50 * MB}], "peer", ["mp3"], [], 0.7)
    assert matches == []


@pytest.mark.parametrize("wanted,release", [
    ("Prologue", "Prologue (Unabridged)"),
    ("The Sample", "Jane Author - The Sample"),
    ("A Preview of Tomorrow", "A Preview of Tomorrow [Retail]"),
])
def test_fragment_words_in_the_actual_work_title_do_not_disqualify_it(wanted, release):
    assert not fragment_release(wanted, release)


def test_cleaning_up_is_a_named_short_story_within_a_collection():
    directory = "Audiobook\\Iain Banks\\The Culture\\04 - 1991 - The State of the Art"
    target = {"book": {"title": "Cleaning up"}, "author": {"authorName": "Iain M. Banks"}}
    files = [{"filename": directory + "\\05 Cleaning Up.mp3", "size": 15 * MB},
             {"filename": directory + "\\06 The State of the Art.mp3", "size": 40 * MB}]
    matches = audiobook_folder_match(target, files, "peer", ["mp3"], [], 0.7)
    assert len(matches) == 1
    assert matches[0]["file_based"] is True
    assert matches[0]["files"] == files[:1]


def test_distinct_same_peer_same_format_releases_survive_until_directory_validation():
    files = [{"filename": "@@share\\Frank Herbert\\Dune [A]\\Dune - Part 2 of 2.m4b", "size": 300 * MB},
             {"filename": "@@share\\Frank Herbert\\Dune [B]\\Dune.m4b", "size": 200 * MB}]
    matches = audiobook_folder_match(DUNE, files, "peer", ["m4b"], [], 0.7)
    assert {match["directory"] for match in matches} == {file["filename"].rsplit("\\", 1)[0] for file in files}


def test_missing_chapter_is_detected_above_99():
    names = [f"{number:03d}.mp3" for number in range(1, 137)]
    assert missing_parts(names) is None
    assert "65" in missing_parts([name for name in names if name != "065.mp3"])


def test_explicit_totals_and_large_malformed_totals_are_checked():
    names = [f"Dune - Part {number} of 136.mp3" for number in range(1, 136)]
    assert "136" in missing_parts(names)
    assert "unsupported" in missing_parts(["Dune - Part 1 of 99999.mp3"])
    assert "inconsistent" in missing_parts(["Dune - Part 1 of 2.mp3", "Dune - Part 2 of 3.mp3"])
    assert "inconsistent" in missing_parts(["Dune - Part 4 of 2.mp3"])


def test_numeric_titles_and_disc_track_notation_remain_distinct():
    assert book_key("George Orwell - 1984.m4b") != book_key("George Orwell - 1985.m4b")
    assert missing_parts(["George Orwell - 1984.m4b"]) is None
    assert missing_parts(["101.mp3", "102.mp3", "201.mp3"]) is None
    assert "disc 1 track 2" in missing_parts(["101.mp3", "103.mp3", "201.mp3"])


def test_numbered_sibling_title_does_not_become_a_series_without_catalog_evidence(tmp_path):
    path = audio_file(tmp_path / "Dune.m4b", album="Dune", title="Dune Messiah Book 1")
    verdict, detail = check_audiobook_tags([path], "Dune", "Frank Herbert", "Dune Chronicles #1", (), ["Dune Messiah"], ["Dune Chronicles #2"])
    assert verdict == MISMATCH
    assert "Dune Messiah" in detail


def test_different_catalog_series_cannot_combine_to_excuse_a_sibling_title():
    verdict, _ = judge_text("Dune Messiah Book 1", "Dune", "Frank Herbert", "Dune Chronicles #1", (), ["Dune Messiah"], ["Messiah Saga #2"])
    assert verdict == MISMATCH


def test_supported_series_label_remains_inconclusive_for_the_later_book():
    verdict, _ = judge_text("Red Rising Book 2", "Golden Son", "Pierce Brown", "Red Rising Saga #2", (), ["Red Rising"], (), is_album=True)
    assert verdict == UNCLEAR


def test_album_conflict_in_a_previously_unsampled_file_is_detected(tmp_path):
    paths = [audio_file(tmp_path / f"{index:02}.mp3", album="Dune Messiah" if index == 1 else "Dune", title=f"Chapter {index + 1}") for index in range(6)]
    verdict, detail = check_audiobook_tags(paths, "Dune", "Frank Herbert", "Dune Chronicles #1", (), ["Dune Messiah"], ["Dune Chronicles #2"])
    assert verdict == MISMATCH
    assert "01.mp3" in detail


def test_normal_prologue_chapter_is_allowed_with_the_full_book(tmp_path):
    paths = [audio_file(tmp_path / "01.mp3", album="Dune", title="Prologue"), audio_file(tmp_path / "02.mp3", album="Dune", title="Chapter One")]
    assert check_audiobook_tags(paths, "Dune", "Frank Herbert")[0] == AGREE


@pytest.mark.parametrize("extension", ["mp3", "m4b", "m4a"])
def test_valid_untagged_audio_is_distinct_from_unreadable_audio(tmp_path, extension):
    path = audio_file(tmp_path / ("book." + extension))
    valid, detail, records = validate_audio_files([path])
    assert valid, detail
    assert records[0]["duration_seconds"] > 0
    assert records[0]["codec"]
    assert records[0]["tags"] == {}


def test_id3_tags_alone_are_not_playable_audio(tmp_path):
    path = tmp_path / "fake.mp3"
    tags = ID3()
    tags.add(TALB(encoding=3, text=["Dune"]))
    tags.add(TIT2(encoding=3, text=["Dune"]))
    tags.save(path)
    assert audiotags.read_tags(str(path))["album"] == "Dune"
    valid, _, records = validate_audio_files([str(path)])
    assert not valid
    assert not records[0]["valid"]


def test_every_file_is_structurally_inspected(tmp_path):
    paths = [audio_file(tmp_path / f"{index:02}.mp3", album="Dune") for index in range(6)]
    Path(paths[1]).write_bytes(b"not an audio stream")
    valid, detail, records = validate_audio_files(paths)
    assert not valid
    assert "01.mp3" in detail
    assert len(records) == 6
    assert sum(record["valid"] for record in records) == 5


def test_truncated_mp4_media_atom_is_invalid(tmp_path):
    path = Path(audio_file(tmp_path / "book.m4b", album="Dune"))
    path.write_bytes(path.read_bytes()[:-12])
    assert not validate_audio_files([str(path)])[0]


def test_missing_or_empty_audio_never_becomes_an_untagged_success(tmp_path):
    empty = tmp_path / "empty.mp3"
    empty.touch()
    assert not validate_audio_files([])[0]
    valid, _, records = validate_audio_files([str(empty), str(tmp_path / "missing.m4b")])
    assert not valid
    assert not any(record["valid"] for record in records)


def test_recording_defaults_preserve_compatibility():
    decision = evaluate_recording(preferences(), ["Unknown recording"], final=True)
    assert decision.allowed and not decision.unknown and not decision.warnings


@pytest.mark.parametrize("unknown_mode,allowed,warned", [("allow", True, False), ("warn", True, True), ("reject", False, False)])
def test_unknown_recording_policy_is_explicit(unknown_mode, allowed, warned):
    config = preferences(audiobook_languages="en", audiobook_unknown_recording=unknown_mode)
    decision = evaluate_recording(config, ["The English Patient"], final=True)
    assert decision.allowed is allowed
    assert bool(decision.warnings) is warned
    assert decision.unknown == ("language",)


def test_strict_unknown_policy_waits_for_file_inspection():
    config = preferences(audiobook_languages="en", audiobook_unknown_recording="reject")
    assert evaluate_recording(config, ["Dune"]).allowed
    assert not evaluate_recording(config, ["Dune"], final=True).allowed
    assert evaluate_recording(config, ["Dune"], {"language": "eng"}, final=True).allowed


@pytest.mark.parametrize("label", ["[German]", "[de]", "Language: deu"])
def test_explicit_unwanted_language_is_rejected_before_download(label):
    assert not evaluate_recording(preferences(audiobook_languages="English"), ["Dune " + label]).allowed


def test_narrator_restriction_uses_explicit_names_not_artist_guessing():
    config = preferences(audiobook_narrators="Kate Reading|Michael Kramer", audiobook_unknown_recording="reject")
    assert evaluate_recording(config, ["Winter's Heart [read by Kate Reading]"], final=True).allowed
    assert evaluate_recording(config, ["Winter's Heart"], {"narrator": ["Kate Reading", "Michael Kramer"]}, final=True).allowed
    assert not evaluate_recording(config, ["Winter's Heart [narrated by Somebody Else]"], final=True).allowed
    assert not evaluate_recording(config, ["Winter's Heart"], {"artist": "Michael Kramer"}, final=True).allowed


@pytest.mark.parametrize("setting,label", [("abridged", "Abridged"), ("booktrack", "Booktrack Edition"), ("dramatized", "Dramatised"), ("dramatized", "GraphicAudio"), ("dramatized", "Full Cast")])
def test_disallowed_recording_variant_is_rejected(setting, label):
    assert not evaluate_recording(preferences(**{"audiobook_" + setting: "reject"}), ["Dune [" + label + "]"]).allowed


def test_unabridged_and_nondramatized_are_not_positive_conflicts():
    config = preferences(audiobook_abridged="reject", audiobook_dramatized="reject", audiobook_unknown_recording="reject")
    assert evaluate_recording(config, ["Dune (Unabridged) [Non-dramatized]"], final=True).allowed
    assert recording_evidence(["Dune [non-dramatised]"])["dramatized"] is False


@pytest.mark.parametrize("extension", ["mp3", "m4b"])
def test_actual_language_and_narrator_tags_are_read_and_enforced(tmp_path, extension):
    path = audio_file(tmp_path / ("book." + extension), album="Dune")
    if extension == "mp3":
        tags = ID3(path)
        tags.add(TLAN(encoding=3, text=["eng", "deu"]))
        tags.add(TXXX(encoding=3, desc="NARRATOR", text=["Kate Reading"]))
        tags.save(path)
    else:
        audio = MP4(path)
        audio["----:com.apple.iTunes:LANGUAGE"] = [b"eng", b"deu"]
        audio["----:com.apple.iTunes:NARRATOR"] = [b"Kate Reading"]
        audio.save()
    valid, _, records = validate_audio_files([path])
    assert valid
    assert records[0]["metadata"]["language"] == ["eng", "deu"]
    assert records[0]["metadata"]["narrator"] == ["Kate Reading"]
    config = preferences(audiobook_languages="en", audiobook_narrators="Kate Reading")
    decision = validate_recording_files(config, [path], records=records)
    assert not decision.allowed
    assert any("language" in reason for reason in decision.reasons)


def test_unknown_metadata_on_a_file_is_not_hidden_by_another_files_tags(tmp_path):
    paths = [audio_file(tmp_path / "01.mp3"), audio_file(tmp_path / "02.mp3")]
    tags = ID3(paths[0])
    tags.add(TLAN(encoding=3, text=["eng"]))
    tags.save(paths[0])
    config = preferences(audiobook_languages="en", audiobook_unknown_recording="reject")
    valid, _, records = validate_audio_files(paths)
    assert valid
    decision = validate_recording_files(config, paths, records=records)
    assert not decision.allowed and decision.unknown == ("language",)
    assert any("02.mp3" in reason for reason in decision.reasons)
    assert validate_recording_files(config, paths, source_texts=["Dune [English]"], records=records).allowed


def test_recording_policy_does_not_allow_invalid_audio_by_default(tmp_path):
    path = tmp_path / "junk.mp3"
    path.write_bytes(b"junk")
    assert not validate_recording_files(preferences(), [str(path)]).allowed


# The original externally reported selection regressions also run through the
# backend so that format partitioning and complete-directory selection are tested.
def test_backend_selection_regressions(monkeypatch):
    from test_download_cleanup_and_retries import Client, make_config
    from rsoul.backends import slskd_backend
    from rsoul.backends.base import DownloadTarget
    from rsoul.backends.slskd_backend import SlskdBackend
    from rsoul.config import Context

    monkeypatch.setattr(slskd_backend.time, "sleep", lambda *_: None)
    target = DownloadTarget(book_id=7, book_title="Dune", author_name="Frank Herbert", series_title="Dune Chronicles #1",
                            allowed_filetypes=["mp3", "m4a", "m4b"], readarr_book={"id": 7, "title": "Dune", "seriesTitle": "Dune Chronicles #1", "authorId": 3},
                            readarr_author={"authorName": "Frank Herbert"}, media_type="audiobook")

    class Catalog:
        ver_uri = "/api/v1"

        def _get(self, *args, **kwargs):
            return [{"id": 7, "title": "Dune", "seriesTitle": "Dune Chronicles #1", "authorId": 3},
                    {"id": 8, "title": "Dune Messiah", "seriesTitle": "Dune Chronicles #2", "authorId": 3}]

    def search(directory, listing):
        client = Client(responses=[{"username": "peer", "files": [dict(item, filename=directory + "\\" + item["filename"]) for item in listing]}],
                        listings={("peer", directory): listing})
        return SlskdBackend(Context(config=make_config(), slskd=client, readarr=Catalog())).search(target)

    result = search("@@share\\Frank Herbert\\Dune", [{"filename": "Dune.m4b", "size": 80 * MB}, {"filename": "Dune Messiah.m4b", "size": 180 * MB}])
    assert [file["filename"].rsplit("\\", 1)[-1] for file in result[0].extra["files"]] == ["Dune.m4b"]
    assert search("@@share\\Frank Herbert", [{"filename": "Frank Herbert - Dune - 01.mp3", "size": 60 * MB}, {"filename": "Frank Herbert - Dune - 02.m4a", "size": 60 * MB}]) == []
    result = search("@@share\\Frank Herbert\\Dune", [{"filename": "01.mp3", "size": 60 * MB}, {"filename": "01.m4a", "size": 60 * MB}, {"filename": "02.m4a", "size": 60 * MB}])
    assert len(result) == 1 and result[0].extension == "m4a" and len(result[0].extra["files"]) == 2


@pytest.mark.parametrize("title,names,accepted", [
    ("Dune", ["Prologue.mp3"], False),
    ("Dune", ["01 - Prologue.mp3"], False),
    ("Dune", ["01 - Prologue.mp3", "02 - Epilogue.mp3"], False),
    ("Dune", ["Free Sample.mp3"], False),
    ("Dune", ["Prologue.mp3", "01.mp3", "02.mp3"], True),
    ("Prologue", ["Prologue.mp3"], True),
])
def test_a_title_folder_cannot_hide_fragment_only_files(monkeypatch, title, names, accepted):
    from test_download_cleanup_and_retries import Client, make_config
    from rsoul.backends import slskd_backend
    from rsoul.backends.base import DownloadTarget
    from rsoul.backends.slskd_backend import SlskdBackend
    from rsoul.config import Context

    monkeypatch.setattr(slskd_backend.time, "sleep", lambda *_: None)
    directory = "@@share\\Frank Herbert\\" + title
    listing = [{"filename": name, "size": 20 * MB} for name in names]
    client = Client(responses=[{"username": "peer", "files": [dict(item, filename=directory + "\\" + item["filename"]) for item in listing]}],
                    listings={("peer", directory): listing})
    target = DownloadTarget(book_id=7, book_title=title, author_name="Frank Herbert", series_title="",
                            allowed_filetypes=["mp3"], readarr_book={"id": 7, "title": title},
                            readarr_author={"authorName": "Frank Herbert"}, media_type="audiobook")
    result = SlskdBackend(Context(config=make_config(), slskd=client, readarr=None)).search(target)
    assert bool(result) is accepted


def _searched_releases(monkeypatch, title, author, listings, search_files=None):
    """Run a real backend search against isolated fake server responses."""
    from test_download_cleanup_and_retries import Client, make_config
    from rsoul.backends import slskd_backend
    from rsoul.backends.base import DownloadTarget
    from rsoul.backends.slskd_backend import SlskdBackend
    from rsoul.config import Context

    monkeypatch.setattr(slskd_backend.time, "sleep", lambda *_: None)
    if search_files is None:
        search_files = [dict(item, filename=directory + "\\" + item["filename"]) for directory, files in listings.items() for item in files]
    client = Client(responses=[{"username": "peer", "files": search_files}],
                    listings={("peer", directory): files for directory, files in listings.items()})
    target = DownloadTarget(book_id=7, book_title=title, author_name=author, series_title="",
                            allowed_filetypes=["m4b", "mp3"], readarr_book={"id": 7, "title": title},
                            readarr_author={"authorName": author}, media_type="audiobook")
    backend = SlskdBackend(Context(config=make_config(), slskd=client, readarr=None))
    return backend.search(target), client


@pytest.mark.parametrize("marker", ["Prologue", "Sample"])
def test_backend_rejects_fragment_marker_before_browsing(monkeypatch, marker):
    directory = f"@@share\\Robert Jordan\\Winter's Heart [{marker}]"
    results, client = _searched_releases(monkeypatch, "Winter's Heart", "Robert Jordan", {directory: [{"filename": "01.mp3", "size": 50 * MB}]})
    assert results == []
    assert client.users.browsed == []


def test_backend_uses_complete_same_peer_alternate_after_browse_rejection(monkeypatch):
    bad_dir = "@@share\\Frank Herbert\\Dune [Edition A]"
    good_dir = "@@share\\Frank Herbert\\Dune [Edition B]"
    results, client = _searched_releases(monkeypatch, "Dune", "Frank Herbert", {
        bad_dir: [{"filename": "Dune - Part 2 of 2.m4b", "size": 300 * MB}],
        good_dir: [{"filename": "Dune.m4b", "size": 200 * MB}],
    })
    assert len(results) == 1
    assert results[0].extra["file_dir"] == good_dir
    assert ("peer", good_dir) in client.users.browsed


def test_backend_catches_missing_number_from_a_full_100_plus_listing(monkeypatch):
    directory = "@@share\\Frank Herbert\\Dune"
    listing = [{"filename": f"{number:03}.mp3", "size": MB} for number in range(1, 137) if number != 65]
    results, client = _searched_releases(monkeypatch, "Dune", "Frank Herbert", {directory: listing},
                                        search_files=[dict(listing[0], filename=directory + "\\001.mp3")])
    assert results == []
    assert ("peer", directory) in client.users.browsed
