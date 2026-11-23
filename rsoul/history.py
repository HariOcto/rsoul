"""Scoped source failures. Local import/configuration failures never poison a peer."""
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from .persistence import atomic_write_json

logger = logging.getLogger(__name__)


def _key(value):
    return " ".join(str(value or "").casefold().split())


class HistoryManager:
    """Persist source evidence with a work/media/backend and optional release scope.

    Unscoped legacy API calls remain readable by unscoped callers. Scoped production
    calls ignore legacy username/title-only bans: their cause/medium is unknowable.
    """
    def __init__(self, config_dir: str):
        self.file_path = os.path.join(config_dir, "failed_downloads.json")
        self.history: List[Dict[str, Any]] = []
        self._lock = threading.RLock()
        self.load()

    def load(self):
        with self._lock:
            try:
                with open(self.file_path, encoding="utf-8") as stream:
                    rows = json.load(stream)
                self.history = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
            except FileNotFoundError:
                self.history = []
            except (OSError, ValueError) as error:
                logger.error("Cannot read source failure history %s: %s", self.file_path, error)
                self.history = []

    def save(self):
        with self._lock:
            atomic_write_json(self.file_path, self.history)

    @staticmethod
    def _expired(entry):
        deadline = entry.get("retry_after") if isinstance(entry, dict) else None
        if not deadline:
            return False
        try:
            parsed = datetime.fromisoformat(deadline)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) >= parsed
        except (TypeError, ValueError):
            return True

    def is_failed(self, source_id: str, book_title: str, *, backend="", author="", media_type="", release_id="", failure_kind="") -> bool:
        if not source_id or not book_title:
            return False
        scoped = bool(backend and author and media_type)
        with self._lock:
            for entry in self.history:
                if self._expired(entry):
                    continue
                if _key(entry.get("source_id", entry.get("username"))) != _key(source_id) or _key(entry.get("book_title")) != _key(book_title):
                    continue
                if scoped:
                    if entry.get("schema_version") != 2:
                        continue
                    if any(_key(entry.get(name)) != _key(value) for name, value in (("backend", backend), ("author", author), ("media_type", media_type))):
                        continue
                    if entry.get("release_id") and entry["release_id"] != release_id:
                        continue
                    if failure_kind and entry.get("failure_kind") != failure_kind:
                        continue
                return True
        return False

    def add_failure(self, source_id: str, book_title: str, reason: str = "", retry_after_days: float = 0, *, backend="", author="", media_type="", release_id="", failure_kind=""):
        if not source_id or not book_title:
            return
        if failure_kind in {"import_failed", "import_unknown", "permission", "path", "service_unavailable", "rate_limited", "enqueue_unknown", "state_persistence", "local_file_invalid", "legacy_unverified"}:
            logger.info("Not blocklisting a source for %s: %s", failure_kind, book_title)
            return
        with self._lock:
            if self.is_failed(source_id, book_title, backend=backend, author=author, media_type=media_type, release_id=release_id, failure_kind=failure_kind):
                return
            now = datetime.now(timezone.utc)
            entry = {"source_id": source_id, "book_title": book_title, "timestamp": now.isoformat(), "reason": reason}
            if backend and author and media_type:
                entry.update(schema_version=2, backend=backend, author=author, media_type=media_type, release_id=release_id, failure_kind=failure_kind or "source_failed")
            else:
                entry["schema_version"] = 1
            if retry_after_days > 0:
                entry["retry_after"] = (now + timedelta(days=retry_after_days)).isoformat()
            rows = [row for row in self.history if not self._expired(row)] + [entry]
            atomic_write_json(self.file_path, rows)
            self.history = rows
            logger.info("Recorded %s failure for %s / %s (%s)", failure_kind or "legacy", author, book_title, source_id)
