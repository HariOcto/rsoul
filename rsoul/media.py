"""
Media mode handling: ebook, audiobook, or both.

Readarr instances hold a single media type, so "ebook" or "audiobook" mode is
enough there. Chaptarr holds both in one instance and tags every book with a
``mediaType`` field ("ebook" or "audiobook"), and its wanted endpoints accept a
``mediaType`` filter. "both" mode relies on that field to decide, per book,
which formats to look for.
"""

from typing import Any, Dict, List, Optional

EBOOK = "ebook"
AUDIOBOOK = "audiobook"
BOTH = "both"

MEDIA_MODES = (EBOOK, AUDIOBOOK, BOTH)

DEFAULT_EBOOK_FORMATS = ["epub", "azw3", "mobi"]
DEFAULT_AUDIOBOOK_FORMATS = ["m4b", "mp3"]

# Audio extensions Chaptarr can import (from its MediaFileExtensions). Formats outside this
# set would download fine but then fail to import.
AUDIO_EXTENSIONS = {"flac", "ape", "wavpack", "wav", "alac", "mp2", "mp3", "wma", "m4a", "m4p", "m4b", "mp4", "aac", "mp4a", "ogg"}

def get_media_mode(config: Any) -> str:
    """Return the configured media mode, defaulting to 'ebook' (existing behaviour)."""
    mode = config.get("Search Settings", "media_mode", fallback=EBOOK).strip().lower()
    if mode not in MEDIA_MODES:
        raise ValueError(f"[Search Settings] media_mode = {mode!r} is not valid. Use one of: {', '.join(MEDIA_MODES)}")
    return mode


def api_media_filter(mode: str) -> Optional[str]:
    """mediaType filter to send to the wanted endpoints, or None for no filter."""
    return mode if mode in (EBOOK, AUDIOBOOK) else None


def resolve_book_media_type(book: Dict[str, Any], mode: str) -> str:
    """Decide whether a wanted book should be treated as an ebook or an audiobook.

    In 'ebook'/'audiobook' mode the configured mode wins. In 'both' mode the
    book's own ``mediaType`` field (Chaptarr) decides. If that field is missing
    (e.g. plain Readarr), fall back to 'ebook' rather than guessing formats;
    build_targets warns about that once per run.
    """
    if mode in (EBOOK, AUDIOBOOK):
        return mode

    value = str(book.get("mediaType") or "").strip().lower()
    return value if value in (EBOOK, AUDIOBOOK) else EBOOK


def _parse_formats(raw: str) -> List[str]:
    return [f.strip().lower().lstrip(".") for f in raw.split(",") if f.strip()]


def get_formats(config: Any, media_type: str) -> List[str]:
    """Allowed file extensions, in priority order, for the given media type."""
    if media_type == AUDIOBOOK:
        formats = _parse_formats(config.get("Search Settings", "audiobook_formats", fallback=",".join(DEFAULT_AUDIOBOOK_FORMATS)))
        return formats or list(DEFAULT_AUDIOBOOK_FORMATS)

    formats = _parse_formats(config.get("Search Settings", "preferred_formats", fallback=",".join(DEFAULT_EBOOK_FORMATS)))
    return formats or list(DEFAULT_EBOOK_FORMATS)


def unsupported_audiobook_formats(config: Any) -> List[str]:
    """Configured audiobook formats that Chaptarr would not import."""
    return [f for f in get_formats(config, AUDIOBOOK) if f not in AUDIO_EXTENSIONS]
