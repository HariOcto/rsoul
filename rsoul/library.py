"""What Readarr/Chaptarr knows about the author's other books.

Whether a folder or a file is the wanted book is mostly judged from its own name. The one
piece of outside knowledge that makes such a judgement certain is the list of the author's
other titles: a file named "Dune Messiah" in a folder called "Dune" is not a chapter with an
odd name, it is another book. Chaptarr has that list (the author's bibliography), so it is
fetched once per author and run.
"""

import logging
from typing import Any, Dict, List, TYPE_CHECKING

from .match import title_key, title_segment_match, title_variants

if TYPE_CHECKING:
    from .config import Context

logger = logging.getLogger(__name__)

# author ID -> titles, for the length of one run (stored on ctx.stats, which every run recreates)
_CACHE_KEY = "author_titles"


def _author_books(ctx: "Context", author_id: Any) -> Dict[str, List[str]]:
    """Titles and series of all books Readarr/Chaptarr lists for the author; empty lists if
    they can't be fetched."""
    cache: Dict[Any, Dict[str, List[str]]] = ctx.stats.setdefault(_CACHE_KEY, {}) if isinstance(ctx.stats, dict) else {}
    if author_id in cache:
        return cache[author_id]
    found: Dict[str, List[Any]] = {"titles": [], "series": [], "records": []}
    try:
        books = ctx.readarr._get("book", ctx.readarr.ver_uri, params={"authorId": author_id})
        for book in books if isinstance(books, list) else []:
            # Only this author's: a server that ignores the filter would hand back everything
            if not isinstance(book, dict) or book.get("authorId", author_id) != author_id:
                continue
            for key, value in (("titles", book.get("title")), ("series", book.get("seriesTitle"))):
                if isinstance(value, str) and value.strip() and value not in found[key]:
                    found[key].append(value)
            statistics = book.get("statistics") if isinstance(book.get("statistics"), dict) else {}
            found["records"].append(
                {
                    "id": book.get("id"),
                    "title": book.get("title") if isinstance(book.get("title"), str) else "",
                    "media_type": str(book.get("mediaType") or "").strip().lower(),
                    "has_files": bool(book.get("hasFiles")) or bool(statistics.get("bookFileCount")),
                }
            )
    except Exception as e:
        logger.debug(f"Could not fetch the other books of author {author_id}: {e}")
    cache[author_id] = found
    return found


def author_series(ctx: "Context", book: Dict[str, Any]) -> List[str]:
    """The series of the author's books as Readarr/Chaptarr labels them ("Red Rising Saga #1").

    Album tags often hold the series rather than the book. Knowing the author's series tells
    such an album ("Red Rising" on the files of "Golden Son") from the name of another book.
    """
    author_id = book.get("authorId")
    if not author_id or not getattr(ctx, "readarr", None):
        return []
    return list(_author_books(ctx, author_id)["series"])


def record_with_files(ctx: "Context", book: Dict[str, Any]) -> Dict[str, Any]:
    """Another record of the same author, title and media type that already has files, or {}.

    Readarr/Chaptarr sometimes lists one book twice (two metadata records with the same
    title). A download imported for one record leaves the other "wanted" for ever, and R:soul
    would fetch the same book again every time its page comes round.
    """
    author_id = book.get("authorId")
    title = title_key(book.get("title") or "")
    if not author_id or not title or not getattr(ctx, "readarr", None):
        return {}
    media_type = str(book.get("mediaType") or "").strip().lower()
    for record in _author_books(ctx, author_id)["records"]:
        if record["id"] == book.get("id") or not record["has_files"]:
            continue
        if media_type and record["media_type"] and record["media_type"] != media_type:
            continue  # the ebook of an audiobook (or the reverse) is not a duplicate
        if title_key(record["title"]) == title:
            return record
    return {}


def sibling_titles(ctx: "Context", book: Dict[str, Any], author_name: str = "") -> List[str]:
    """Titles of the author's other books: every listed title that isn't this book.

    Other editions and the other media type of the same book ("Dune", "Dune (Unabridged)")
    are not siblings. Returns [] when the list isn't available; callers then go without.
    """
    author_id = book.get("authorId")
    title = book.get("title") or ""
    if not author_id or not title or not getattr(ctx, "readarr", None):
        return []
    own = title_variants(title)
    siblings = []
    for other in _author_books(ctx, author_id)["titles"]:
        same_book = any(title_segment_match(variant, other, author_name) for variant in own) or any(title_segment_match(variant, title, author_name) for variant in title_variants(other))
        if not same_book:
            siblings.append(other)
    return siblings
