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


class CatalogUnavailable(RuntimeError):
    """An author's bibliography could not be verified, rather than being empty."""


def _author_books(ctx: "Context", author_id: Any) -> Dict[str, List[str]]:
    """Fetch/cache successful catalog responses; failures remain retryable."""
    stats = getattr(ctx, "stats", None)
    if not isinstance(stats, dict):
        stats = {}
        try:
            ctx.stats = stats
        except (AttributeError, TypeError):
            pass
    cache = stats.setdefault(_CACHE_KEY, {})
    if not isinstance(cache, dict):
        cache = {}
        stats[_CACHE_KEY] = cache
    if author_id in cache:
        return cache[author_id]
    found: Dict[str, List[Any]] = {"titles": [], "series": [], "records": []}
    try:
        books = ctx.readarr._get("book", ctx.readarr.ver_uri, params={"authorId": author_id})
        if not isinstance(books, list):
            raise CatalogUnavailable("Author bibliography API returned an unsupported response")
        for book in books:
            if not isinstance(book, dict):
                raise CatalogUnavailable("Author bibliography API returned a malformed book")
            # Only this author's: a server that ignores the filter would hand back everything
            if str(book.get("authorId", author_id)) != str(author_id):
                continue
            if not isinstance(book.get("title"), str) or not book["title"].strip():
                raise CatalogUnavailable("Author bibliography API returned a book without a title")
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
    except CatalogUnavailable:
        raise
    except Exception as e:
        raise CatalogUnavailable(f"Could not fetch the bibliography of author {author_id}: {e}") from e
    cache[author_id] = found
    return found


def require_author_catalog(ctx: "Context", book: Dict[str, Any]) -> Dict[str, List[str]]:
    """Return verified author metadata or explicitly report that it is unavailable.

    An actual empty API list is a successful response. A failed or malformed
    lookup never becomes an empty cached bibliography that disables safety checks.
    """
    author_id = book.get("authorId") if isinstance(book, dict) else None
    if not author_id or not isinstance(author_id, (int, str)) or isinstance(author_id, bool):
        raise CatalogUnavailable("The wanted book has no usable author ID for its bibliography")
    if not getattr(ctx, "readarr", None):
        raise CatalogUnavailable("Readarr/Chaptarr is unavailable for author bibliography validation")
    return _author_books(ctx, author_id)


def author_series(ctx: "Context", book: Dict[str, Any]) -> List[str]:
    """The series of the author's books as Readarr/Chaptarr labels them ("Red Rising Saga #1").

    Album tags often hold the series rather than the book. Knowing the author's series tells
    such an album ("Red Rising" on the files of "Golden Son") from the name of another book.
    """
    author_id = book.get("authorId")
    if not author_id or not getattr(ctx, "readarr", None):
        return []
    try:
        return list(require_author_catalog(ctx, book)["series"])
    except CatalogUnavailable as exc:
        logger.debug("Author series unavailable: %s", exc)
        return []


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
    try:
        records = require_author_catalog(ctx, book)["records"]
    except CatalogUnavailable as exc:
        logger.debug("Duplicate catalog lookup unavailable: %s", exc)
        return {}
    for record in records:
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
    try:
        titles = require_author_catalog(ctx, book)["titles"]
    except CatalogUnavailable as exc:
        logger.debug("Sibling titles unavailable: %s", exc)
        return []
    for other in titles:
        same_book = any(title_segment_match(variant, other, author_name) for variant in own) or any(title_segment_match(variant, title, author_name) for variant in title_variants(other))
        if not same_book:
            siblings.append(other)
    return siblings
