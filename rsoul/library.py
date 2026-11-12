"""What Readarr/Chaptarr knows about the author's other books.

Whether a folder or a file is the wanted book is mostly judged from its own name. The one
piece of outside knowledge that makes such a judgement certain is the list of the author's
other titles: a file named "Dune Messiah" in a folder called "Dune" is not a chapter with an
odd name, it is another book. Chaptarr has that list (the author's bibliography), so it is
fetched once per author and run.
"""

import logging
from typing import Any, Dict, List, TYPE_CHECKING

from .match import title_segment_match, title_variants

if TYPE_CHECKING:
    from .config import Context

logger = logging.getLogger(__name__)

# author ID -> titles, for the length of one run (stored on ctx.stats, which every run recreates)
_CACHE_KEY = "author_titles"


def _author_titles(ctx: "Context", author_id: Any) -> List[str]:
    """Titles of all books Readarr/Chaptarr lists for the author; [] if they can't be fetched."""
    cache: Dict[Any, List[str]] = ctx.stats.setdefault(_CACHE_KEY, {}) if isinstance(ctx.stats, dict) else {}
    if author_id in cache:
        return cache[author_id]
    titles: List[str] = []
    try:
        books = ctx.readarr._get("book", ctx.readarr.ver_uri, params={"authorId": author_id})
        for book in books if isinstance(books, list) else []:
            title = book.get("title") if isinstance(book, dict) else None
            # Only this author's: a server that ignores the filter would hand back everything
            if isinstance(title, str) and title.strip() and book.get("authorId", author_id) == author_id and title not in titles:
                titles.append(title)
    except Exception as e:
        logger.debug(f"Could not fetch the other books of author {author_id}: {e}")
    cache[author_id] = titles
    return titles


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
    for other in _author_titles(ctx, author_id):
        same_book = any(title_segment_match(variant, other, author_name) for variant in own) or any(title_segment_match(variant, title, author_name) for variant in title_variants(other))
        if not same_book:
            siblings.append(other)
    return siblings
