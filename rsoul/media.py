"""
Media mode handling: ebook, audiobook, or both.

Readarr instances hold a single media type, so "ebook" or "audiobook" mode is
enough there. Chaptarr holds both in one instance and tags every book with a
``mediaType`` field ("ebook" or "audiobook"), and its wanted endpoints accept a
``mediaType`` filter. "both" mode relies on that field to decide, per book,
which formats to look for.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

EBOOK = "ebook"
AUDIOBOOK = "audiobook"
BOTH = "both"

MEDIA_MODES = (EBOOK, AUDIOBOOK, BOTH)

DEFAULT_EBOOK_FORMATS = ["epub", "azw3", "mobi"]
DEFAULT_AUDIOBOOK_FORMATS = ["m4b", "mp3"]

# Extensions treated as audio for folder grabbing and import handling.
AUDIO_EXTENSIONS = {"m4b", "m4a", "mp3", "aac", "flac", "ogg", "opus", "wma"}

_warned_missing_media_type = False


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
    (e.g. plain Readarr), fall back to 'ebook' rather than guessing formats.
    """
    global _warned_missing_media_type

    if mode in (EBOOK, AUDIOBOOK):
        return mode

    value = str(book.get("mediaType") or "").strip().lower()
    if value in (EBOOK, AUDIOBOOK):
        return value

    if not _warned_missing_media_type:
        logger.warning(
            "media_mode = both, but the wanted list has no 'mediaType' field (plain Readarr?). "
            "Treating such books as ebooks. Use media_mode = audiobook for an audiobook-only instance."
        )
        _warned_missing_media_type = True
    return EBOOK


def _parse_formats(raw: str) -> List[str]:
    return [f.strip().lower().lstrip(".") for f in raw.split(",") if f.strip()]


def get_formats(config: Any, media_type: str) -> List[str]:
    """Allowed file extensions, in priority order, for the given media type."""
    if media_type == AUDIOBOOK:
        formats = _parse_formats(config.get("Search Settings", "audiobook_formats", fallback=",".join(DEFAULT_AUDIOBOOK_FORMATS)))
        return formats or list(DEFAULT_AUDIOBOOK_FORMATS)

    formats = _parse_formats(config.get("Search Settings", "preferred_formats", fallback=",".join(DEFAULT_EBOOK_FORMATS)))
    return formats or list(DEFAULT_EBOOK_FORMATS)


def is_audio_extension(extension: str) -> bool:
    return extension.strip().lower().lstrip(".") in AUDIO_EXTENSIONS
