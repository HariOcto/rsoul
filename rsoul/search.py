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


def get_books(ctx: "Context", search_source: str, search_type: str, page_size: int) -> List[Book]:
    """Get books from Readarr based on search source and type."""
    current_page_file_path = os.path.join(ctx.config_dir, ".current_page.txt")

    base_method = ctx.readarr.get_missing if search_source == "missing" else ctx.readarr.get_cutoff

    # Chaptarr: restrict the wanted list to ebooks or audiobooks unless media_mode = both
    media_filter = api_media_filter(get_media_mode(ctx.config))

    def api_method(**kwargs):
        if media_filter:
            kwargs["media_type"] = media_filter
        return base_method(**kwargs)

    try:
        wanted = api_method(page_size=page_size, sort_dir="ascending", sort_key="title")
    except Exception:
        logger.error(f"An error occurred when attempting to get records from {search_source}", exc_info=True)
        return []

    total_wanted = wanted["totalRecords"]
    wanted_records: List[Book] = []

    if search_type == "all":
        page = 1
        while len(wanted_records) < total_wanted:
            try:
                wanted = api_method(page=page, page_size=page_size, sort_dir="ascending", sort_key="title")
                wanted_records.extend(wanted["records"])
            except Exception:
                logger.error(f"Failed to grab records from {search_source} page {page}", exc_info=True)
                break
            page += 1

    elif search_type == "incrementing_page":
        page = get_current_page(current_page_file_path)
        try:
            wanted_records = api_method(page=page, page_size=page_size, sort_dir="ascending", sort_key="title")["records"]
        except Exception:
            logger.error(f"Failed to grab record from {search_source}", exc_info=True)

        page = 1 if page >= math.ceil(total_wanted / page_size) else page + 1
        update_current_page(current_page_file_path, page)

    elif search_type == "first_page":
        wanted_records = wanted["records"]
    else:
        raise ValueError(f"[Search Settings] - {search_type = } is not valid")

    return wanted_records
