import json
import os
import logging
from datetime import datetime, timedelta
from typing import List, Dict

logger = logging.getLogger(__name__)


class HistoryManager:
    """
    Manages a persistent list of failed downloads to prevent re-queueing.
    """

    def __init__(self, config_dir: str):
        self.file_path = os.path.join(config_dir, "failed_downloads.json")
        self.history: List[Dict[str, str]] = []
        self.load()

    def load(self):
        """Load history from JSON file."""
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    self.history = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load history from {self.file_path}: {e}")
                self.history = []
        else:
            self.history = []

    def save(self):
        """Save history to JSON file using atomic write (temp file + rename)."""
        tmp_path = self.file_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.history, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.file_path)
        except Exception as e:
            logger.error(f"Failed to save history to {self.file_path}: {e}")
            # Clean up temp file if it exists
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def add_failure(self, source_id: str, book_title: str, reason: str = "", retry_after_days: float = 0):
        """
        Record a failed download/import.

        Args:
            source_id: Backend-specific identifier (slskd username, AA path, etc.)
            book_title: The canonical book title from Readarr.
            reason: Optional reason for failure.
            retry_after_days: If > 0, the entry only holds for this many days. Used when the
                files themselves may be fine (Readarr/Chaptarr didn't import them, which can
                also be a path, permission or import-setting problem on its side).
        """
        if not source_id or not book_title:
            return

        # Check if already exists to avoid duplicates
        if self.is_failed(source_id, book_title):
            return

        # Entries that have run out make room for the new one
        self.history = [e for e in self.history if not self._expired(e)]

        entry = {"source_id": source_id, "book_title": book_title, "timestamp": datetime.now().isoformat(), "reason": reason}
        if retry_after_days and retry_after_days > 0:
            entry["retry_after"] = (datetime.now() + timedelta(days=retry_after_days)).isoformat()
        self.history.append(entry)
        logger.info(f"Added to blocklist: '{source_id}' for Book '{book_title}'")
        self.save()

    @staticmethod
    def _expired(entry: Dict[str, str]) -> bool:
        """True for a temporary entry whose time is up. Entries without retry_after never expire."""
        retry_after = entry.get("retry_after") if isinstance(entry, dict) else None
        if not retry_after:
            return False
        try:
            return datetime.now() >= datetime.fromisoformat(retry_after)
        except (TypeError, ValueError):
            return False

    def is_failed(self, source_id: str, book_title: str) -> bool:
        """
        Check if a source_id/book combo is in the blocklist.
        Case-insensitive comparison.
        Backwards-compatible: reads both 'source_id' and legacy 'username' fields.
        """
        if not source_id or not book_title:
            return False

        target_source = source_id.strip().lower()
        target_book = book_title.strip().lower()

        for entry in self.history:
            if self._expired(entry):
                continue
            # Support both new 'source_id' and legacy 'username' field
            h_source = entry.get("source_id", entry.get("username", "")).strip().lower()
            h_book = entry.get("book_title", "").strip().lower()

            if h_source == target_source and h_book == target_book:
                return True

        return False
