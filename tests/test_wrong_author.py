"""A dance track imported, and a pop album downloaded, as audiobooks (live run, 23 November 2026).

For Ryk Brown's "Resistance" (book 6801), R:soul downloaded
"stuffz\\Hardcore Library\\Scott Brown\\Hardcore Tracks\\01 Resistance (JTS Mix).mp3",
a 14 MB track by the DJ Scott Brown, and Chaptarr accepted it. Three checks let it through:
- the author check only looked for the surname, and "Scott Brown" contains "Brown";
- the tag check found "Resistance" in the title tag and did not look at the artist;
- nothing asked whether a few minutes of audio can be a book.
"""

import os
import shutil
import sys
from configparser import ConfigParser
from types import SimpleNamespace

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rsoul import audiotags, import_transaction
from rsoul.match import audiobook_folder_match, author_in_name, author_mentions

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
THRESHOLDS = dict(ignored_users=[], minimum_match_ratio=0.7, min_length_ratio=0.4, min_jaccard_ratio=0.25,
                  min_word_overlap=2, min_title_jaccard=0.3, min_author_jaccard=0.5)
MB = 1024 * 1024

# The candidates the search for "Brown - Resistance" offered, as logged
LOGGED = [
    ("facecam", r"stuffz\Hardcore Library\Scott Brown\Hardcore Tracks\01 Resistance (JTS Mix).mp3", 14 * MB),
    ("SynapticPlasticity", r"@@hmsiw\Music\Bill Brown & Mikael Sandgren\Command And Conquer Generals Soundtrack\Bill Brown & Mikael Sandgren - Command & Conquer Generals-Zero Hour - 03 - Resistance.mp3", 5 * MB),
    ("mistermike", r"moremusic\Terry Lee Brown Jr\Terry's Cafe 3\10 Resistance.mp3", 12 * MB),
    ("Ardefisty", r"@@eddaq\Música\Evolution Records (1994-2017)\JTS-Resistance_(Scott_Brown_Remix)-(JTS39)-WEB-2014-wAx\01-resistance_(scott_brown_remix).mp3", 15 * MB),
    ("Fendibull", r"Music\Bill Brown & Mikael Sandgren - Command & Conquer Generals. Zero Hour (Original Soundtrack) (2005)\03. Bill Brown & Mikael Sandgren - Resistance.mp3", 6 * MB),
]


def match(user, path, size, title="Resistance", author="Ryk Brown"):
    target = {"book": {"title": title}, "author": {"authorName": author}}
    return audiobook_folder_match(target, [{"filename": path, "size": size}], user, ["m4b", "mp3"], **THRESHOLDS)


@pytest.mark.parametrize("user, path, size", LOGGED)
def test_music_by_another_brown_is_not_ryk_browns_book(user, path, size):
    assert match(user, path, size) == []


@pytest.mark.parametrize("path", [
    r"audiobooks\Ryk Brown\The Frontiers Saga\10 - Resistance\Resistance.m4b",
    r"audiobooks\Brown, Ryk\Resistance\01.mp3",
    r"audiobooks\Ryk Brown - Resistance.m4b",
    r"Audio\Frontiers Saga 10 - Resistance (read by Jason Scott)\Ryk Brown - Resistance.mp3",
])
def test_ryk_browns_own_book_still_matches(path):
    assert len(match("peer", path, 300 * MB)) == 1


@pytest.mark.parametrize("author, path, expected", [
    ("Ryk Brown", r"Scott Brown\Hardcore Tracks", (0, 1)),
    ("Ryk Brown", r"Brown, Scott\Resistance", (0, 1)),
    ("Ryk Brown", r"Brown, Ryk\Resistance", (1, 0)),
    ("Ryk Brown", r"Resistance - read by Brown", (1, 0)),
    ("Ryk Brown", r"Bill Brown & Mikael Sandgren", (0, 1)),
    ("Christopher G. Nuttall", r"TE_Audiobooks_C\Christopher G Nuttall", (1, 0)),
    ("Christopher G. Nuttall", r"C. Nuttall - Ark Royal", (1, 0)),
    ("Christopher G. Nuttall", r"J. Nuttall - Ark Royal", (0, 1)),
    ("Iain M. Banks", r"Banks, Iain M\The Culture 1 - Consider Phlebas", (1, 0)),
    ("Iain M. Banks", r"SS-Complete\00 Books, Audiobooks and Radio\!Audiobooks New\Iain Banks\Iain Banks", (2, 0)),
    ("Robert Jordan", r"Audiobooks on 16TB-3\J\Jordan, Robert\Wheel of Time 09", (1, 0)),
    ("Ursula K. Le Guin", r"Audiobooks\Ursula K. Le Guin\A Wizard of Earthsea", (1, 0)),
    ("J.R.R. Tolkien", r"Audiobooks\J.R.R. Tolkien\The Hobbit", (1, 0)),
    ("J.R.R. Tolkien", r"Audiobooks\Tolkien\The Hobbit", (1, 0)),
    ("Kevin J. Anderson", r"TE_Audiobooks_K\KEVIN J ANDERSON", (1, 0)),
    ("Peter F. Hamilton", r"Audiobooks\Peter Hamilton\Salvation", (1, 0)),
    ("Moebius", r"Comics\Moebius\Arzach", (1, 0)),  # one-name authors have nothing to contradict
])
def test_the_surname_has_to_belong_to_the_author(author, path, expected):
    assert author_mentions(author, path) == expected
    assert author_in_name(author, path) is (expected[0] > 0)


# ---------------------------------------------------------------------------
# Artist tags
# ---------------------------------------------------------------------------


def tagged(tmp_path, name, **frames):
    from mutagen.id3 import ID3, TALB, TIT2, TPE1, TPE2
    path = tmp_path / name
    shutil.copy(os.path.join(FIXTURES, "silence.mp3"), path)
    tags = ID3(str(path))
    for key, cls in (("title", TIT2), ("album", TALB), ("artist", TPE1), ("albumartist", TPE2)):
        if frames.get(key):
            tags.add(cls(encoding=3, text=[frames[key]]))
    tags.save(str(path))
    return str(path)


def test_an_artist_tag_naming_another_brown_is_a_mismatch(tmp_path):
    path = tagged(tmp_path, "01 Resistance (JTS Mix).mp3", title="Resistance (JTS Mix)", artist="Scott Brown")
    verdict, detail = audiotags.check_audiobook_tags([path], "Resistance", "Ryk Brown")
    assert verdict == audiotags.MISMATCH and "Scott Brown" in detail


@pytest.mark.parametrize("frames", [
    {"title": "Resistance", "artist": "Ryk Brown"},
    {"title": "Resistance", "artist": "Brown, Ryk", "albumartist": "Jeffrey Kafer"},
    {"title": "Resistance", "artist": "Jeffrey Kafer"},  # the narrator as artist proves nothing either way
    {"title": "Resistance", "artist": "Ryk Brown; Scott Brown"},  # a confirming credit outweighs
])
def test_the_authors_own_credits_or_a_narrator_are_not_contradictions(tmp_path, frames):
    path = tagged(tmp_path, "Resistance.mp3", **frames)
    verdict, _ = audiotags.check_audiobook_tags([path], "Resistance", "Ryk Brown")
    assert verdict == audiotags.AGREE


# ---------------------------------------------------------------------------
# Minimum running time
# ---------------------------------------------------------------------------


def ctx(minutes=None):
    config = ConfigParser()
    config["Search Settings"] = {} if minutes is None else {"audiobook_min_duration_minutes": str(minutes)}
    return SimpleNamespace(config=config)


def test_a_six_minute_track_is_not_an_audiobook(monkeypatch):
    monkeypatch.setattr(import_transaction, "DEFAULT_MIN_AUDIOBOOK_MINUTES", 15)  # the shipped default
    reason = import_transaction._too_short(ctx(), [{"duration_seconds": 6 * 60 + 12}])
    assert "6.2 minutes" in reason and "not an audiobook" in reason


@pytest.mark.parametrize("seconds, minutes, rejected", [
    ([20 * 60], None, False),            # a short story of 20 minutes passes the default
    ([5 * 60] * 4, None, False),         # chapters add up
    ([10 * 60], 5, False),               # a lower setting
    ([10 * 60], 0, False),               # switched off
    ([None], None, False),               # a length that can't be read is no evidence
    ([60 * 60], 120, True),
])
def test_minimum_running_time(monkeypatch, seconds, minutes, rejected):
    monkeypatch.setattr(import_transaction, "DEFAULT_MIN_AUDIOBOOK_MINUTES", 15)
    records = [{"duration_seconds": s} for s in seconds]
    assert bool(import_transaction._too_short(ctx(minutes), records)) is rejected


def test_the_setting_is_validated():
    from rsoul.config import validate_config
    from test_download_cleanup_and_retries import make_config
    with pytest.raises(ValueError, match="audiobook_min_duration_minutes"):
        validate_config(make_config(search={"audiobook_min_duration_minutes": "-5"}))



# ---------------------------------------------------------------------------
# The second case from the same run: tacica's album "singularity" for Ryk Brown's Singularity
# (12 songs, 111 MB, in "@@jshid\Music\Japanese\tacica\2022 - singularity"). The search
# "Brown - Singularity" found it because one song's name contains "Brown"; the album plays for
# about 45 minutes, so the minimum running time can't catch it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    r"@@jshid\Music\Japanese\tacica\2022 - singularity\07 - Brown.mp3",
    r"music\tacica\singularity - 2022\05 brown eyes.mp3",
    r"shared\tacica\2022 - singularity\05 brown eyes.mp3",  # no music folder: the song name alone doesn't make it Ryk Brown's
])
def test_an_album_titled_like_the_book_is_not_the_book(path):
    assert match("iayh46v2", path, 9 * MB, title="Singularity") == []


@pytest.mark.parametrize("title, author, path", [
    ("Singularity", "Ryk Brown", r"audiobooks\Ryk Brown\Frontiers Saga\Singularity\01.mp3"),
    ("Singularity", "Ryk Brown", r"audiobooks\Ryk Brown - Singularity.m4b"),
    ("Singularity", "Ryk Brown", r"Audiobooks\Singularity\Ryk Brown - Singularity.m4b"),  # the file names author and book
    ("Winter's Heart", "Robert Jordan", r"music\Audio Books\Robert Jordan\The Wheel of Time Narrated by Michael Kramer & Kate Reading\09 - Winter's Heart\01.mp3"),
    ("Canal Dreams", "Iain M. Banks", r"music\Audio Books\Iain Banks\1989 - Canal Dreams\Canal Dreams.mp3"),
    ("Complicity", "Iain M. Banks", r"@@dlumd\Stuff\Audiobooks & Spoken Word\Banks, Iain\1993 - Complicity\01 - Complicity.mp3"),
    ("Trafalgar Gambit", "Christopher G. Nuttall", r"Audio books Sorting\TE_Audiobooks_C\Christopher G Nuttall\Trafalgar Gambit (Ark Royal 3).mp3"),
])
def test_audiobooks_from_the_logs_still_match(title, author, path):
    assert len(match("peer", path, 300 * MB, title=title, author=author)) == 1


@pytest.mark.parametrize("directory, music", [
    (r"@@jshid\Music\Japanese\tacica", True),
    (r"Musique\DJ Premier", True),
    (r"@@eddaq\Música\JTS Recordings", True),
    (r"OST\Video Games", True),
    (r"music\Audio Books\Robert Jordan", False),
    (r"Music\Hörbücher\Banks", False),
    (r"audiobooks\Ryk Brown", False),
    (r"stuffz\Hardcore Library\Scott Brown", False),
])
def test_music_library_folders(directory, music):
    from rsoul.match import music_library
    assert music_library(directory) is music


# ---------------------------------------------------------------------------
# The underlying rule: a candidate needs positive evidence that it is this author's audiobook,
# not only a title match and the surname somewhere (Soulseek matches words anywhere in a path).
# ---------------------------------------------------------------------------

from rsoul.backends.slskd_backend import _generate_fallback_queries
from rsoul.match import audiobook_evidence

# Every pick for a correct book in the November logs
CORRECT_PICKS = [
    ("Christopher G. Nuttall", r"Audio books Sorting\TE_Audiobooks_C\Christopher G Nuttall\Trafalgar Gambit (Ark Royal 3).mp3", "mp3"),
    ("Kevin J. Anderson", r"Audio books Sorting\TE_Audiobooks_K\KEVIN J ANDERSON\Scattered Suns (Saga of Seven Suns 4).mp3", "mp3"),
    ("Kevin J. Anderson", r"Audiobooks\Kevin J. Anderson\The Saga of Seven Suns\The Saga of Seven Suns #4 - Scattered Suns\01.mp3", "mp3"),
    ("Robert Jordan", r"audiobooks\Robert Jordan\Wheel of Time Series\9 Winter's Heart\9 Winter's Heart.m4b", "m4b"),
    ("Robert Jordan", r"## MUSIC TO SORT\11 Years of Hugo and Nebula Best Novel Nominees 2014-2024 - 63 Audiobooks jZQ\0 Series Books\Robert Jordan - The Wheel of Time 12GB\(9) Winter's Heart [64k 24;12;52 667MB]\01.mp3", "mp3"),
    ("Robert Jordan", r"audiobooks\robert.jordan\wheel.of.time\09.winters.heart\x.m4b", "m4b"),
    ("Iain M. Banks", r"Audiobook\Iain Banks\1989 - Canal Dreams\Canal Dreams (Unabridged).mp3", "mp3"),
    ("Iain M. Banks", r"@@dlumd\Stuff\Audiobooks & Spoken Word\Banks, Iain\1993 - Complicity\01 - Complicity.mp3", "mp3"),
    ("Iain M. Banks", r"audiobooks\Iain M Banks - Consider Phlebas\01 Consider Phlebas [BT]\Culture Book 1 - Consider Phlebas [Booktrack Edition].m4b", "m4b"),
    ("Isaac Asimov", r"audiobooks\Isaac Asimov\Robot 4 - Robots and Empire.m4b", "m4b"),
]
WRONG_PICKS = [
    ("Ryk Brown", r"stuffz\Hardcore Library\Scott Brown\Hardcore Tracks\01 Resistance (JTS Mix).mp3", "mp3"),
    ("Ryk Brown", r"@@jshid\Music\Japanese\tacica\2022 - singularity\07 - Brown.mp3", "mp3"),
    ("Ryk Brown", r"shared\tacica\2022 - singularity\05 brown eyes.mp3", "mp3"),
    ("Ryk Brown", r"moremusic\Terry Lee Brown Jr\Terry's Cafe 3\10 Resistance.mp3", "mp3"),
    ("Ryk Brown", r"Unmixed\Hardcore\Evolution Records\Brown - Resistance.mp3", "mp3"),  # surname, title, nothing more
]


@pytest.mark.parametrize("author, path, ext", CORRECT_PICKS)
def test_every_correct_pick_from_the_logs_shows_evidence(author, path, ext):
    assert audiobook_evidence(author, path, ext)


@pytest.mark.parametrize("author, path, ext", WRONG_PICKS)
def test_no_wrong_pick_from_the_logs_shows_evidence(author, path, ext):
    assert audiobook_evidence(author, path, ext) == ""


@pytest.mark.parametrize("path, ext, why", [
    (r"shared\Ryk Brown\Resistance\01.mp3", "mp3", "author named in full"),
    (r"shared\Brown, Ryk\Resistance\01.mp3", "mp3", "author named in full"),
    (r"shared\Brown\Resistance (Unabridged)\01.mp3", "mp3", "spoken-word folder or name"),
    (r"shared\Hörbücher\Brown\Resistance\01.mp3", "mp3", "spoken-word folder or name"),
    (r"shared\Brown\Resistance\Resistance.m4b", "m4b", "audiobook format"),
])
def test_kinds_of_evidence(path, ext, why):
    assert audiobook_evidence("Ryk Brown", path, ext) == why


def test_a_narrator_chaptarr_lists_is_evidence():
    assert audiobook_evidence("Ryk Brown", r"shared\Brown\Resistance - Jeffrey Kafer\01.mp3", "mp3", ["Jeffrey Kafer"]) == "narrator named"


def test_a_surname_and_title_alone_are_no_longer_enough():
    assert match("peer", r"Unmixed\Hardcore\Evolution Records\Brown - Resistance.mp3", 300 * MB) == []
    assert len(match("peer", r"Unmixed\Brown - Resistance (Unabridged).mp3", 300 * MB)) == 1


@pytest.mark.parametrize("author, title, expected", [
    ("Ryk Brown", "Resistance", ["Brown - Resistance"]),
    ("J.R.R. Tolkien", "The Hobbit", ["Tolkien - The Hobbit", "Tolkien - Hobbit"]),  # an article may go, the word stays
    ("Ryk Brown", "The Vengeance", ["Brown - The Vengeance", "Brown - Vengeance"]),
    ("Ryk Brown", "Enemies of my Enemy", ["Brown - Enemies of my Enemy", "Brown - Enemies Enemy"]),  # no "Brown - my Enemy"
    ("Ryk Brown", "The First Ranger", ["Brown - The First Ranger", "Brown - First Ranger"]),  # no "Brown - First"
])
def test_fallback_queries_keep_at_least_two_distinctive_words(author, title, expected):
    # dict.fromkeys: the backend removes duplicate queries
    assert list(dict.fromkeys(_generate_fallback_queries(author, title, 5))) == expected


def test_fallback_queries_with_longer_titles_still_drop_one_word():
    queries = _generate_fallback_queries("Iain M. Banks", "Against a Dark Background", 5)
    assert "Banks - Dark Background" in queries and all(len(q.split(" - ", 1)[1].split()) >= 2 for q in queries)


# The same rules before import, for downloads queued before them or resumed from saved state

def validate(full_dir, names, author="Ryk Brown", title="Singularity", narrators=()):
    item = {"title": title, "author_name": author, "full_dir": full_dir, "dir": "", "filename": names[0], "bookId": 6856}
    pending = {"media_type": "audiobook", "manifest": [{"name": n, "source_path": "/nonexistent/" + n} for n in names]}
    catalog = SimpleNamespace(get_book=lambda book_id: {"id": book_id, "narratorNames": list(narrators)})
    return import_transaction.validate_inputs(SimpleNamespace(config=ConfigParser(), readarr=catalog), item, pending)


@pytest.mark.parametrize("full_dir, expected", [
    (r"@@jshid\Music\Japanese\tacica\2022 - singularity", "music library"),
    (r"stuffz\Hardcore Library\Scott Brown\Hardcore Tracks", "another person"),
    (r"shared\tacica\2022 - singularity", "Nothing in the source folder"),
])
def test_a_queued_wrong_download_is_stopped_before_import(full_dir, expected):
    assert expected in validate(full_dir, [f"{i:02d}.mp3" for i in range(1, 13)])
