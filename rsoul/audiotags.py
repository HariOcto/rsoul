"""Check a downloaded audiobook against its own audio tags.

Before a download, R:soul only has names: the folder's and the files'. After it, the files
themselves say what they are, in their album and title tags. This is a second, independent
look, taken before the files are handed to Readarr/Chaptarr: a folder called "Red Rising"
whose files are tagged "Golden Son" is caught here.

The check has three outcomes, and only the second has consequences:

- confirmed match: a tag names the wanted book;
- confirmed wrong book: the tags name another book by the same author (from Chaptarr's list
  of the author's books), or say collection or another position in the series;
- inconclusive: everything else. No tags, the series or the publisher as album ("Red
  Rising" on the files of "Golden Son", "Audible"), the title with extras ("Dune Disc 1"),
  a title in another language. Tags on Soulseek are too sloppy to read more into them.

Which tag counts for what:

- The album is the book, or its series. An album that names another book of the author is
  the wrong book, unless it reads as a series (many series are named after their first
  book): it consists of a series Chaptarr lists for the author, or cites the other title
  with a position ("Red Rising Book 2"). Where Chaptarr lists no series for the author at
  all, an album alone proves nothing.
- The title tag of a single-file book is the most specific statement the file makes: if it
  names another book, a fitting album (often the series) doesn't outweigh it.
- The title tags of a book in several files are chapter names. They are only looked at for
  another book's title, and only count when most of the sampled files agree. "Book Two" in
  a chapter name is a part of the novel, not a position in the series.
"""

import logging
import os
import re
from typing import Any, Dict, List, Sequence, Tuple

from .match import _BOOK_WORDS, _COLLECTION_WORDS, _FORMAT_NOISE, _NUMBER_WORD, _meaningful_words, _people_words, names_another_release, title_segment_match, title_variants

logger = logging.getLogger(__name__)

AGREE, MISMATCH, UNCLEAR, UNTAGGED = "agree", "mismatch", "unclear", "untagged"

_BRACKETS = re.compile(r"[\[\](){}]")

# Words that say nothing about which book it is
_FILLER = {"unknown", "album", "untitled", "various", "artists", "track", "disc", "disk", "cd", "part", "chapter", "book", "vol", "volume"}
# How many files of a multi-file book are looked at (first, middle, last)
MAX_SAMPLED = 3

_warned_missing = False


def read_tags(path: str) -> Dict[str, str]:
    """Album, title, artist and album artist of an audio file; {} if it has none or can't be read."""
    global _warned_missing
    try:
        import mutagen
        from mutagen.id3 import ID3, ID3NoHeaderError
        from mutagen.mp4 import MP4
    except ImportError:
        if not _warned_missing:
            logger.warning("The audio tag check needs the 'mutagen' package (pip install mutagen); skipping it")
            _warned_missing = True
        return {}

    def first(value: Any) -> str:
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        text = getattr(value, "text", value)
        if isinstance(text, (list, tuple)):
            text = text[0] if text else ""
        return str(text or "").strip()

    try:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".mp3":
            # Read the ID3 tag directly: it doesn't need a parseable audio stream
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                return {}
            found = {"album": first(tags.get("TALB")), "title": first(tags.get("TIT2")), "artist": first(tags.get("TPE1")), "albumartist": first(tags.get("TPE2"))}
        elif ext in (".m4b", ".m4a", ".mp4", ".aac"):
            tags = MP4(path).tags or {}
            found = {"album": first(tags.get("\xa9alb")), "title": first(tags.get("\xa9nam")), "artist": first(tags.get("\xa9ART")), "albumartist": first(tags.get("aART"))}
        else:
            audio = mutagen.File(path, easy=True)
            tags = (audio.tags if audio is not None else None) or {}
            found = {key: first(tags.get(key)) for key in ("album", "title", "artist", "albumartist")}
    except Exception as e:
        logger.debug(f"Could not read the tags of {path}: {e}")
        return {}
    return {key: value for key, value in found.items() if value}


def _phrase_in(phrase: List[str], words: List[str], taken: List[bool]) -> bool:
    """Find the phrase in the words, skipping words already taken by a longer title; mark it."""
    n = len(phrase)
    for start in range(len(words) - n + 1):
        if words[start : start + n] == phrase and not any(taken[start : start + n]):
            taken[start : start + n] = [True] * n
            return True
    return False


def named_books(text: str, title: str, siblings: Sequence[str]) -> Tuple[bool, List[str]]:
    """Which books a tag mentions by name: (the wanted one?, other books of the author).

    Longer titles are looked for first and use up their words, so "Dune Messiah" mentions
    Dune Messiah and not also Dune. A one-word title of another book is too weak a signal
    inside a longer text ("Dune" in "Dune Chronicles") and only counts as the whole text.
    """
    words = _meaningful_words(_BRACKETS.sub(" ", text))
    taken = [False] * len(words)
    phrases = [(_meaningful_words(variant), None) for variant in title_variants(title)]
    phrases += [(_meaningful_words(variant), sibling) for sibling in siblings for variant in title_variants(sibling)]
    wanted = False
    others: List[str] = []
    for phrase, sibling in sorted((p for p in phrases if p[0]), key=lambda p: -len(p[0])):
        if sibling is not None and len(phrase) < 2 and phrase != words:
            continue
        if _phrase_in(phrase, words, taken):
            if sibling is None:
                wanted = True
            elif sibling not in others:
                others.append(sibling)
    return wanted, others


def _series_reference(text: str, sibling: str) -> bool:
    """True if the text uses another book's title the way a series is cited: the title plus a
    position or a series word ("Red Rising Book 2", "Red Rising #2", "Red Rising 2", "Red
    Rising Saga") and nothing else. Many series go by the name of their first book."""
    rest = _meaningful_words(_BRACKETS.sub(" ", text))
    for variant in title_variants(sibling):
        phrase = _meaningful_words(variant)
        for start in range(len(rest) - len(phrase) + 1):
            if phrase and rest[start : start + len(phrase)] == phrase:
                remaining = rest[:start] + rest[start + len(phrase) :]
                marks_series = any(_NUMBER_WORD.fullmatch(w) or w in _COLLECTION_WORDS for w in remaining)
                return marks_series and all(_NUMBER_WORD.fullmatch(w) or w in _BOOK_WORDS or w in _COLLECTION_WORDS for w in remaining)
    return False


def judge_text(
    text: str,
    title: str,
    author_name: str,
    series_title: str = "",
    narrators: Sequence[str] = (),
    siblings: Sequence[str] = (),
    author_series: Sequence[str] = (),
    is_chapter: bool = False,
    is_album: bool = False,
) -> Tuple[str, str]:
    """What one tag value says about the wanted book: (verdict, reason).

    AGREE: it names the wanted book. MISMATCH: it names another book of the author, a
    collection, or another position in the series. UNCLEAR: anything else.
    is_chapter: the text is the title tag of one file among several, i.e. a chapter name:
        "Book Two" there is a part of the novel, and a number behind another book's title is
        a track number, not a series position.
    author_series: the series of the author's other books, to recognise a series-level album.
    is_album: the text is an album tag, which may hold a series instead of a book. Another
        book's title there only counts when Chaptarr gave series for this author at all;
        without them a series named after one of its books can't be told from that book.
    """
    series_words = {w for label in [series_title, *author_series] for w in _meaningful_words(label) if not _NUMBER_WORD.fullmatch(w)}
    neutral = _people_words(author_name, narrators) | series_words | _FORMAT_NOISE | _FILLER | _COLLECTION_WORDS
    words = set(_meaningful_words(text))
    series_level = bool(words) and all(word in neutral or _NUMBER_WORD.fullmatch(word) for word in words)

    wanted, others = named_books(text, title, siblings)
    # Another book's title that is itself a series of the author ("The Lord of the Rings",
    # "Red Rising") is the series being named, not that book
    others = [other for other in others if not set(_meaningful_words(other)) <= series_words]
    if others:
        # A tag that names the wanted book too: one of the two is the series, or it is a
        # bundle. It is only the other book when the wanted title is what the series is
        # called ("Red Rising 2 - Golden Son" for Red Rising); without series from Chaptarr
        # that can't be decided ("The Fellowship of the Ring (The Lord of the Rings #1)").
        wanted_is_series_name = bool(series_words) and any(set(_meaningful_words(variant)) <= series_words for variant in title_variants(title))
        names_only_other = not wanted or wanted_is_series_name
        cited_as_series = not is_chapter and _series_reference(text, others[0])
        if names_only_other and not series_level and not cited_as_series and (series_words or not is_album):
            return MISMATCH, f'"{text}" names another book by the author ("{others[0]}")'
    if names_another_release(title, text, author_name, "" if is_chapter else series_title, narrators):
        return MISMATCH, f'"{text}" is a collection or another book of the series'
    if any(title_segment_match(variant, text, author_name, "" if is_chapter else series_title, narrators) for variant in title_variants(title)):
        return AGREE, ""
    return UNCLEAR, ""


def check_audiobook_tags(
    paths: List[str], title: str, author_name: str, series_title: str = "", narrators: Sequence[str] = (), siblings: Sequence[str] = (), author_series: Sequence[str] = ()
) -> Tuple[str, str]:
    """Compare the tags of a downloaded audiobook with the wanted book.

    paths: the book's audio files (first, middle and last are read).

    Returns (verdict, detail): AGREE (confirmed match), MISMATCH (confirmed wrong book; detail
    says which tag and why), UNCLEAR (tagged, but not conclusive) or UNTAGGED.
    """
    paths = sorted(paths)
    if not paths:
        return UNTAGGED, ""
    sample = paths if len(paths) <= MAX_SAMPLED else [paths[0], paths[len(paths) // 2], paths[-1]]
    single_file = len(paths) == 1

    def judge(text: str, is_chapter: bool = False, is_album: bool = False) -> Tuple[str, str]:
        return judge_text(text, title, author_name, series_title, narrators, siblings, author_series, is_chapter, is_album)

    verdicts: List[Tuple[str, str]] = []
    chapter_titles_naming_another: List[str] = []
    titled = 0
    for path in sample:
        tags = read_tags(path)
        name = os.path.basename(path)
        album = judge(tags["album"], is_album=True) if tags.get("album") else None

        if single_file:
            # The title tag is the file's most specific statement
            title_tag = judge(tags["title"]) if tags.get("title") else None
            if title_tag and title_tag[0] == MISMATCH:
                verdicts.append((MISMATCH, f"title tag of {name}: {title_tag[1]}"))
            elif title_tag and title_tag[0] == AGREE:
                # An album naming another book next to it is a contradiction, not a confirmation
                verdicts.append((UNCLEAR, "") if album and album[0] == MISMATCH else (AGREE, ""))
            elif album and album[0] == MISMATCH:
                verdicts.append((MISMATCH, f"album tag of {name}: {album[1]}"))
            elif album and album[0] == AGREE:
                verdicts.append((AGREE, ""))
            else:
                verdicts.append((UNCLEAR, "") if album or title_tag else (UNTAGGED, ""))
            continue

        if tags.get("title"):
            titled += 1
            chapter = judge(tags["title"], is_chapter=True)
            if chapter[0] == MISMATCH and "another book by the author" in chapter[1]:
                chapter_titles_naming_another.append(f"title tag of {name}: {chapter[1]}")
        if album and album[0] == MISMATCH:
            verdicts.append((MISMATCH, f"album tag of {name}: {album[1]}"))
        elif album:
            verdicts.append(album)
        else:
            verdicts.append((UNTAGGED, ""))

    # Chapter names: one chapter that happens to share a book's title proves nothing, but
    # most of the sampled files naming another book do
    if chapter_titles_naming_another and len(chapter_titles_naming_another) * 2 > titled:
        return MISMATCH, chapter_titles_naming_another[0]

    # One file with another book's album is enough: the folder is not (only) the wanted book
    for wanted in (MISMATCH, AGREE, UNCLEAR):
        for verdict, detail in verdicts:
            if verdict == wanted:
                return verdict, detail
    if titled:
        return UNCLEAR, ""
    return UNTAGGED, ""
