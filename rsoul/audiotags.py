"""Check a downloaded audiobook against its own audio tags.

Before a download, R:soul only has names: the folder's and the files'. After it, the files
themselves say what they are, in their album and title tags. This is a second, independent
look, taken before the files are handed to Readarr/Chaptarr: a folder called "Red Rising"
whose files are tagged "Golden Son" is caught here.

Tags on Soulseek are often missing or sloppy, so only a clear contradiction counts:

- the tags name another book of the same author (from Chaptarr's list of the author's books);
- the tags say collection or another position in the series ("Books 1-3", "Book 2");
- the wanted title doesn't occur in the tags at all, and they aren't just the series,
  the author, the narrator or filler ("Unknown Album").

Anything else (no tags, or the title with extra words such as "Dune Disc 1") is let through.
"""

import logging
import os
from typing import Any, Dict, List, Sequence, Tuple

from .match import _FORMAT_NOISE, _NUMBER_WORD, _meaningful_words, _people_words, names_another_release, title_segment_match, title_variants

logger = logging.getLogger(__name__)

AGREE, MISMATCH, UNCLEAR, UNTAGGED = "agree", "mismatch", "unclear", "untagged"

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


def judge_text(text: str, title: str, author_name: str, series_title: str, narrators: Sequence[str], siblings: Sequence[str]) -> Tuple[str, str]:
    """What one tag value says about the wanted book: (verdict, reason)."""
    titles = title_variants(title)
    if any(title_segment_match(variant, text, author_name, series_title, narrators) for variant in titles):
        return AGREE, ""
    for sibling in siblings:
        if any(title_segment_match(variant, text, author_name, "", narrators) for variant in title_variants(sibling)):
            return MISMATCH, f'"{text}" is another book by the author ("{sibling}")'
    if names_another_release(title, text, author_name, series_title, narrators):
        return MISMATCH, f'"{text}" is a collection or another book of the series'
    words = set(_meaningful_words(text))
    if any(set(_meaningful_words(variant)) <= words for variant in titles):
        return UNCLEAR, ""  # the title with something extra: "Dune Disc 1", "Dune - Macmillan Audio"
    neutral = _people_words(author_name, narrators) | set(_meaningful_words(series_title)) | _FORMAT_NOISE | _FILLER
    if all(word in neutral or _NUMBER_WORD.fullmatch(word) for word in words):
        return UNCLEAR, ""  # only the series, the author, the narrator or filler
    return MISMATCH, f'"{text}" does not contain the title'


def check_audiobook_tags(paths: List[str], title: str, author_name: str, series_title: str = "", narrators: Sequence[str] = (), siblings: Sequence[str] = ()) -> Tuple[str, str]:
    """Compare the tags of a downloaded audiobook with the wanted book.

    paths: the book's audio files. For a book in several files the album tag is the book (the
    title tag is the chapter); for a single file either may be.

    Returns (verdict, detail): AGREE, MISMATCH (detail says which tag and why), UNCLEAR (tagged,
    but neither clearly this book nor clearly another) or UNTAGGED.
    """
    paths = sorted(paths)
    if not paths:
        return UNTAGGED, ""
    sample = paths if len(paths) <= MAX_SAMPLED else [paths[0], paths[len(paths) // 2], paths[-1]]
    keys = ("album", "title") if len(paths) == 1 else ("album",)

    verdicts = []
    for path in sample:
        tags = read_tags(path)
        judged = [(key, tags[key], *judge_text(tags[key], title, author_name, series_title, narrators, siblings)) for key in keys if tags.get(key)]
        if not judged:
            verdicts.append((UNTAGGED, ""))
        elif any(verdict == AGREE for _, _, verdict, _ in judged):
            verdicts.append((AGREE, ""))
        elif all(verdict == MISMATCH for _, _, verdict, _ in judged):
            key, _, _, reason = judged[0]
            verdicts.append((MISMATCH, f"{key} tag of {os.path.basename(path)}: {reason}"))
        else:
            verdicts.append((UNCLEAR, ""))

    # One file of another book is enough: the folder is not (only) the wanted book
    for wanted in (MISMATCH, AGREE, UNCLEAR):
        for verdict, detail in verdicts:
            if verdict == wanted:
                return verdict, detail
    return UNTAGGED, ""
