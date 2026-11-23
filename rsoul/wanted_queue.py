"""Bounded wanted-list scans with a durable queue of work not attempted yet.

The queue and its page cursor are one checkpoint. Advancing the API cursor can
therefore never lose a book merely because all download slots were occupied.
"""

import copy
import json
import logging
import math
from pathlib import Path
import threading
import time

from .media import api_media_filter, get_media_mode
from .persistence import StatePersistenceError, atomic_write_json
from .utils import get_current_page

logger = logging.getLogger(__name__)


class WantedQueue:
    def __init__(self, config_dir):
        self.path = Path(config_dir) / "wanted_queue.json"
        self._lock = threading.RLock()
        self.data = {"schema": 1, "lists": {}}
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("lists"), dict):
                    raise ValueError("invalid queue schema")
                for entry in value["lists"].values():
                    if not isinstance(entry, dict) or not isinstance(entry.get("candidates"), list):
                        raise ValueError("invalid queue entries")
                    if any(not isinstance(row, dict) or not isinstance(row.get("book"), dict) or not row["book"].get("id") for row in entry["candidates"]):
                        raise ValueError("invalid queued book")
                self.data = value
            except (OSError, ValueError, TypeError) as exc:
                raise StatePersistenceError(f"Cannot read wanted queue {self.path}; preserve it for review: {exc}") from exc

    def _commit(self, data):
        atomic_write_json(self.path, data)
        self.data = copy.deepcopy(data)

    def acknowledge(self, book_ids):
        """Retire attempted work, explicit exclusions, and work owned by task state."""
        ids = {str(i) for i in book_ids}
        if not ids:
            return
        with self._lock:
            new = copy.deepcopy(self.data)
            changed = False
            for entry in new["lists"].values():
                previous = entry["candidates"]
                entry["candidates"] = [row for row in previous if str(row["book"]["id"]) not in ids]
                changed |= len(previous) != len(entry["candidates"])
            if changed:
                self._commit(new)

    def fetch(self, ctx, source, search_type, page_size):
        if search_type not in ("all", "incrementing_page", "first_page"):
            raise ValueError(f"Invalid search_type: {search_type}")
        pages = ctx.config.getint("Search Settings", "wanted_scan_pages", fallback=5)
        limit = max(page_size, ctx.config.getint("Search Settings", "wanted_backlog_limit", fallback=500))
        mode = get_media_mode(ctx.config)
        media_filter = api_media_filter(mode)
        api = ctx.readarr.get_missing if source == "missing" else ctx.readarr.get_cutoff
        scan_errors = getattr(ctx, "wanted_scan_errors", None)
        if scan_errors is None:
            scan_errors = {}
            ctx.wanted_scan_errors = scan_errors
        scan_errors.pop(source, None)
        def fetch_page(page):
            kwargs = dict(page=page, page_size=page_size, sort_dir="ascending", sort_key="title")
            if media_filter:
                kwargs["media_type"] = media_filter
            response = api(**kwargs)
            if not isinstance(response, dict) or not isinstance(response.get("records"), list):
                raise ValueError("wanted API returned an invalid response")
            # Validate the complete response before changing the in-memory page.
            # A bad row cannot advance the cursor or leave half of a page ready.
            if any(not isinstance(book, dict) or not book.get("id") for book in response["records"]):
                raise ValueError("wanted API returned a book without an ID")
            total = int(response.get("totalRecords", 0))
            if total < 0:
                raise ValueError("wanted API returned a negative record count")
            return response

        with self._lock:
            now = time.time()
            key = f"{source}:{mode}"
            new = copy.deepcopy(self.data)
            entry = new["lists"].get(key)
            if entry is None:
                counter = ".current_page.txt" if source == "missing" else f".current_page_{source}.txt"
                entry = {"next_page": get_current_page(str(self.path.parent / counter), repair=False), "candidates": []}
                new["lists"][key] = entry
            page = 1 if search_type == "first_page" else max(1, int(entry.get("next_page", 1)))
            rows = {str(row["book"]["id"]): row for row in entry["candidates"]}
            order = list(rows)
            scanned = set()
            seen_now = set()
            scan_error = None
            # Each fetched page is saved before proceeding to the next; an error on a
            # later page cannot strand the earlier page or advance beyond unsaved work.
            for _ in range(1 if search_type == "first_page" else pages):
                if page in scanned or len(rows) >= limit:
                    break
                try:
                    response = fetch_page(page)
                    last_page = max(1, math.ceil(int(response.get("totalRecords", 0)) / page_size))
                    if page > last_page:
                        page = 1
                        if page in scanned:
                            break
                        response = fetch_page(page)
                        last_page = max(1, math.ceil(int(response.get("totalRecords", 0)) / page_size))
                except StatePersistenceError:
                    raise
                except Exception as exc:
                    scan_error = exc
                    scan_errors[source] = str(exc)
                    logger.warning("Wanted %s page %s is unavailable; keeping its cursor and refreshing queued work: %s", source, page, exc)
                    break
                scanned.add(page)
                fitted = True
                for book in response["records"]:
                    book_key = str(book["id"])
                    if book_key not in rows:
                        if len(rows) >= limit:
                            fitted = False
                            break
                        order.append(book_key)
                    rows[book_key] = {"book": copy.deepcopy(book), "last_seen": now}
                    seen_now.add(book_key)
                next_page = 1 if page >= last_page else page + 1
                entry["next_page"] = next_page if fitted else page
                entry["candidates"] = [rows[i] for i in order]
                self._commit(new)
                if not fitted or search_type == "first_page":
                    break
                page = next_page

            # Refresh previously deferred records before considering them again. A
            # temporary outage leaves the snapshot queued but does not search stale data.
            ready = []
            invalid_ids = set()
            for book_key in order:
                book = rows[book_key]["book"]
                if book_key not in seen_now:
                    try:
                        fresh = ctx.readarr.get_book(book["id"])
                    except Exception as exc:
                        status = getattr(getattr(exc, "response", None), "status_code", None)
                        if status == 404:
                            invalid_ids.add(book["id"])
                        else:
                            logger.warning("Deferred book %s could not be refreshed; keeping it for a later run: %s", book["id"], exc)
                        continue
                    if not isinstance(fresh, dict) or str(fresh.get("id")) != book_key:
                        logger.warning("Deferred book %s returned uncertain identity; keeping it for review", book_key)
                        continue
                    book = fresh
                own_mode = str(book.get("mediaType") or mode).lower()
                flag = "audiobookMonitored" if own_mode == "audiobook" else "ebookMonitored"
                monitored = book.get(flag, book.get("monitored", True))
                if monitored is False:
                    invalid_ids.add(book["id"])
                    continue
                # A missing book that now has files has already been satisfied. For
                # cutoff searches, existing files are expected and must remain eligible.
                explicit_mode = str(book.get("mediaType") or "").strip().lower()
                files_are_for_mode = explicit_mode == own_mode or (
                    not explicit_mode and mode in ("ebook", "audiobook")
                )
                if source == "missing" and book_key not in seen_now and book.get("hasFiles") is True and files_are_for_mode:
                    invalid_ids.add(book["id"])
                    continue
                ready.append(copy.deepcopy(book))
            self.acknowledge(invalid_ids)
            logger.info("Wanted queue %s: scanned %s page(s), %s candidate(s) ready, cursor %s", key, len(scanned), len(ready), entry["next_page"])
            if scan_error is not None and not ready:
                # Preserve the normal search error when there is no independently
                # validated work. Callers can distinguish an outage from an empty list.
                raise scan_error
            return ready
