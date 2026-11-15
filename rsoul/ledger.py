"""What R:soul itself has learned about wanted books, kept between runs.

Two things the wanted list of Readarr/Chaptarr does not tell:

- This book was already downloaded and imported by R:soul. If it is wanted again a little
  later, something is off on the other side (the file was attached to another record, the
  record is a duplicate...). Downloading it again only repeats that, and the second import
  usually fails and costs a good peer.
- This book was searched for and nobody shares it. Asking the Soulseek network again a few
  minutes later gives the same answer, and half a dozen searches per book and run add up.

Both are remembered in book_ledger.json in the data folder, per book ID. Delete a book's
entry (or the file) to make R:soul forget.
"""

import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from .match import title_key

logger = logging.getLogger(__name__)

LEDGER_FILENAME = "book_ledger.json"


class BookLedger:
    def __init__(self, config_dir: str, read_only: bool = False):
        self.filepath = Path(config_dir) / LEDGER_FILENAME
        self.read_only = read_only
        self._lock = threading.Lock()
        self.books: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.filepath.exists():
            return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.books = {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, ValueError) as e:
            logger.warning(f"Could not read {self.filepath} ({e}); starting with an empty ledger")

    def _save(self) -> None:
        if self.read_only:
            return
        temp = self.filepath.with_name(self.filepath.name + ".tmp")
        try:
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(self.books, f, indent=2)
            os.replace(temp, self.filepath)
        except OSError as e:
            logger.warning(f"Could not save {self.filepath}: {e}")

    @staticmethod
    def _when(value: Any) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(value) if value else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _same_identity(entry: Dict[str, Any], author: str, title: str, media_type: str) -> bool:
        """IDs can be recycled, and a book can have separate ebook/audio records.

        Never allow a previous book or format's history to suppress a new wanted item.
        An untyped legacy entry is deliberately not sufficient evidence.
        """
        return (
            title_key(entry.get("author") or "") == title_key(author)
            and title_key(entry.get("title") or "") == title_key(title)
            and str(entry.get("media_type") or "").lower() == str(media_type or "").lower()
            and bool(media_type)
        )

    def _entry(self, book_id: Any, author: str, title: str, media_type: str) -> Dict[str, Any]:
        entry = self.books.setdefault(str(book_id), {})
        if entry and not self._same_identity(entry, author, title, media_type):
            # This ID no longer represents the book whose timestamps were recorded.
            entry.clear()
        entry.update({"author": author, "title": title, "media_type": media_type})
        return entry

    def note_imported(self, book_id: Any, author: str, title: str, media_type: str = "") -> None:
        """R:soul downloaded this book and Readarr/Chaptarr imported it."""
        if book_id is None:
            return
        with self._lock:
            entry = self._entry(book_id, author, title, media_type)
            entry["imported_at"] = datetime.now().isoformat(timespec="seconds")
            entry.pop("not_found_at", None)
            self._save()

    def note_not_found(self, book_id: Any, author: str, title: str, media_type: str = "") -> None:
        """A complete search found nothing usable for this book."""
        if book_id is None:
            return
        with self._lock:
            entry = self._entry(book_id, author, title, media_type)
            entry["not_found_at"] = datetime.now().isoformat(timespec="seconds")
            self._save()

    def note_found(self, book_id: Any) -> None:
        """A download was started for the book: it is no longer "not found"."""
        with self._lock:
            entry = self.books.get(str(book_id))
            if entry and entry.pop("not_found_at", None):
                self._save()

    def imported_recently(self, book_id: Any, author: str, title: str, media_type: str, days: float) -> Optional[datetime]:
        """When R:soul imported this book, or another record with the same author, title and
        media type, if that was within the last `days`; else None."""
        if not days or days <= 0:
            return None
        limit = datetime.now() - timedelta(days=days)
        key = (title_key(author), title_key(title))
        with self._lock:
            for other_id, entry in self.books.items():
                when = self._when(entry.get("imported_at"))
                if not when or when < limit:
                    continue
                same_title = (title_key(entry.get("author", "")), title_key(entry.get("title", ""))) == key
                if same_title and self._same_identity(entry, author, title, media_type):
                    return when
        return None

    def not_found_recently(self, book_id: Any, days: float, author: str = "", title: str = "", media_type: str = "") -> bool:
        """Whether a search for this book came up empty within the last `days`."""
        if not days or days <= 0:
            return False
        with self._lock:
            entry = self.books.get(str(book_id)) or {}
            if (author or title or media_type) and not self._same_identity(entry, author, title, media_type):
                return False
            when = self._when(entry.get("not_found_at"))
        return bool(when and when >= datetime.now() - timedelta(days=days))
