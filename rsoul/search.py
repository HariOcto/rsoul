import logging
import os
import math
from typing import Any, Dict, List, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Context

from .utils import get_current_page, update_current_page
from .types import Book
from .media import get_media_mode, api_media_filter

logger = logging.getLogger(__name__)


class SearchError(Exception):
    """The wanted list couldn't be fetched (e.g. Readarr/Chaptarr unreachable).

    Raised instead of returning an empty list, so a failed search isn't mistaken for
    "nothing is wanted".
    """


def get_books(ctx: "Context", search_source: str, search_type: str, page_size: int, advance_page: bool = True, *, durable_queue: bool = False) -> List[Book]:
    """Get books from Readarr based on search source and type.

    advance_page=False (dry run) leaves the page counter of incrementing_page untouched, so the
    next real run gets the same books."""
    if durable_queue and advance_page:
        from .wanted_queue import WantedQueue
        from .persistence import StatePersistenceError
        if not getattr(ctx, "wanted_queue", None):
            ctx.wanted_queue = WantedQueue(ctx.config_dir)
        try:
            return ctx.wanted_queue.fetch(ctx, search_source, search_type, page_size)
        except (StatePersistenceError, ValueError):
            raise
        except Exception as exc:
            raise SearchError(f"Could not update the {search_source} wanted queue: {exc}") from exc

    # One page counter per list: with search_source = all, a shared counter would be advanced
    # twice per run, so each list would only ever see every other page
    page_file = ".current_page.txt" if search_source == "missing" else f".current_page_{search_source}.txt"
    current_page_file_path = os.path.join(ctx.config_dir, page_file)

    base_method = ctx.readarr.get_missing if search_source == "missing" else ctx.readarr.get_cutoff

    # Chaptarr: restrict the wanted list to ebooks or audiobooks unless media_mode = both
    media_filter = api_media_filter(get_media_mode(ctx.config))

    def api_method(**kwargs):
        if media_filter:
            kwargs["media_type"] = media_filter
        return base_method(**kwargs)

    if search_type not in ("all", "incrementing_page", "first_page"):
        raise ValueError(f"[Search Settings] - {search_type = } is not valid")

    try:
        wanted = api_method(page_size=page_size, sort_dir="ascending", sort_key="title")
    except Exception as e:
        raise SearchError(f"Could not get the {search_source} list from Readarr/Chaptarr: {e}") from e

    total_wanted = wanted["totalRecords"]
    wanted_records: List[Book] = []

    if search_type == "all":
        page = 1
        while len(wanted_records) < total_wanted:
            try:
                wanted = api_method(page=page, page_size=page_size, sort_dir="ascending", sort_key="title")
            except Exception as e:
                raise SearchError(f"Could not get page {page} of the {search_source} list: {e}") from e
            if not wanted.get("records"):
                break  # the list shrank while paging; don't loop forever
            wanted_records.extend(wanted["records"])
            page += 1

    elif search_type == "incrementing_page":
        last_page = max(1, math.ceil(total_wanted / page_size))
        # (a dry run only reads the counter; it doesn't create or repair the file either)
        page = get_current_page(current_page_file_path, repair=advance_page)
        if page > last_page:
            page = 1  # the list got shorter since the last run: don't waste a run on an empty page
        try:
            wanted_records = api_method(page=page, page_size=page_size, sort_dir="ascending", sort_key="title")["records"]
        except Exception as e:
            # Don't move on to the next page: these books would otherwise be skipped
            raise SearchError(f"Could not get page {page} of the {search_source} list: {e}") from e

        if advance_page:
            page = 1 if page >= last_page else page + 1
            update_current_page(current_page_file_path, page)

    else:  # first_page
        wanted_records = wanted["records"]

    return wanted_records
