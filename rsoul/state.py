"""
State management for resume functionality.

Persists grab_list to disk so downloads can be resumed after interruption.
Uses (username, filename) as composite key since slskd IDs are ephemeral.
"""

import copy
import hashlib
import json
import math
import os
import shutil
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from .persistence import StatePersistenceError, atomic_write_json, fsync_directory

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

STATE_FILENAME = "grab_list_state.json"


class StateManager:
    """Manages persistence of grab_list for resume functionality."""

    def __init__(self, config_dir: str, read_only: bool = False):
        """Initialize StateManager.

        Args:
            config_dir: Directory to store state file
            read_only: Only read the file (dry run): a damaged file is reported but neither
                moved aside nor copied, and nothing is ever saved
        """
        self.read_only = read_only
        self.filepath = Path(config_dir) / STATE_FILENAME
        self.items: List[Dict[str, Any]] = []
        self.load_error = None
        # Imports finish on a background thread while the monitoring thread handles failures;
        # both update the state, so every change and save is serialized.
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        """Load state from disk."""
        if not self.filepath.exists():
            logger.debug(f"No state file found at {self.filepath}")
            return

        try:
            with open(self.filepath, "r") as f:
                items = json.load(f)
            if not isinstance(items, list):
                raise ValueError("expected a list of saved downloads")
        except ValueError as e:  # includes json.JSONDecodeError
            kept = self._keep_damaged_copy(move=False)
            logger.error(f"State file is damaged ({e}); starting fresh." + (f" The damaged file was kept as {kept}" if kept else ""))
            self.items = []
            self.load_error = f"The recovery state was damaged: {e}. Inspect the saved damaged copy before resuming mutations."
            return
        except Exception as e:
            logger.error(f"Error loading state file: {e}")
            self.items = []
            self.load_error = f"Could not read recovery state: {e}"
            return

        self.items = [item for item in items if self._usable(item)]
        dropped = len(items) - len(self.items)
        if dropped:
            kept = self._keep_damaged_copy(move=False)
            logger.error(f"Ignoring {dropped} damaged entr{'y' if dropped == 1 else 'ies'} in the state file." + (f" A copy of the file was kept as {kept}" if kept else ""))
            self.load_error = f"{dropped} damaged recovery entries need review; inspect the damaged copy before resuming mutations."
        logger.info(f"Loaded {len(self.items)} items from state file")

    @staticmethod
    def _usable(item: Any) -> bool:
        """Whether a saved entry has the shape the rest of R:soul relies on. A hand-edited or
        half-written entry is dropped here rather than crashing a run later."""
        if not isinstance(item, dict):
            return False
        if "task_id" not in item:
            return True  # entry from a version before task IDs; cleared by the workflow
        if not isinstance(item["task_id"], str) or not item["task_id"]:
            return False
        extra = item.setdefault("extra", {})
        if not isinstance(extra, dict):
            return False
        def text(value: Any, empty_ok: bool = True) -> bool:
            return isinstance(value, str) and (empty_ok or bool(value))

        def number(value: Any) -> bool:
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

        files = extra.get("files", [])
        if not isinstance(files, list):
            return False
        for f in files:
            if not isinstance(f, dict) or not text(f.get("filename"), empty_ok=False):
                return False
            # (state files from before transfer IDs were saved have no "id")
            if not (f.get("id") is None or text(f["id"], empty_ok=False)) or not number(f.get("size") or 0):
                return False
            if not all(text(f.get(key) or "") for key in ("username", "file_dir")):
                return False
            if not isinstance(f.get("status") or {}, dict):
                return False
        expected = extra.get("expected_files", [])
        if not isinstance(expected, list) or not all(text(name, empty_ok=False) for name in expected):
            return False
        if not all(text(extra.get(key, "")) for key in ("username", "file_dir", "media_type", "path")):
            return False
        if not isinstance(extra.get("monitor", {}), dict) or not all(v is None or number(v) for v in extra.get("monitor", {}).values()):
            return False
        if not number(extra.get("offline_bytes", 0)):
            return False
        pending = extra.get("import_pending")
        if pending is not None:
            if not isinstance(pending, dict) or not text(pending.get("local_dir")) or not text(pending.get("readarr_path", "")):
                return False
            if not isinstance(pending.get("files", []), list) or not all(text(path) for path in pending.get("files", [])):
                return False
            if not all(number(pending.get(key, 0)) for key in ("submits", "since")):
                return False
        return all(text(item.get(key, "")) for key in ("backend_name", "book_title", "author_name", "filename", "local_dir", "series_title"))

    def _keep_damaged_copy(self, move: bool) -> str:
        """Set the damaged state file aside for troubleshooting. Returns its new name ("" on failure)."""
        if self.read_only:
            return ""
        target = self.filepath.with_name(f"{self.filepath.name}.damaged-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            if move:
                os.replace(self.filepath, target)
            else:
                shutil.copy2(self.filepath, target)
            return str(target)
        except OSError as e:
            logger.warning(f"Could not keep a copy of the damaged state file: {e}")
            return ""

    def _save(self) -> None:
        """Commit a checkpoint, or fail before the caller performs its next mutation."""
        if self.read_only:
            return
        with self._lock:
            if self.load_error:
                raise StatePersistenceError(self.load_error)
            atomic_write_json(self.filepath, self.items)
            logger.debug(f"State saved: {len(self.items)} items")

    def ensure_writable(self) -> None:
        """Preflight the actual durable store, without discarding pending work."""
        if self.read_only:
            raise StatePersistenceError("Recovery state is read-only; mutations are disabled")
        self._save()

    def _commit(self, items) -> None:
        previous = self.items
        self.items = copy.deepcopy(items)
        try:
            self._save()
        except Exception:
            self.items = previous
            raise

    def has_pending_state(self) -> bool:
        """Check if there's a saved state to resume."""
        with self._lock:
            return len(self.items) > 0

    def migrate_legacy_tasks(self) -> int:
        """Give pre-task-ID records stable identities without losing their evidence."""
        with self._lock:
            converted = []
            changed = 0
            for old in self.items:
                if "task_id" in old:
                    converted.append(old)
                    continue
                raw = json.dumps(old, sort_keys=True, default=str).encode("utf-8")
                extra = {k: copy.deepcopy(old[k]) for k in ("username", "files", "expected_files", "media_type") if k in old}
                extra["file_dir"] = old.get("full_dir", "")
                extra["legacy_state"] = copy.deepcopy(old)
                extra.setdefault("media_type", "ebook")
                converted.append({
                    "task_id": "legacy-" + hashlib.sha256(raw).hexdigest()[:24],
                    "backend_name": old.get("backend_name", "slskd"),
                    "book_title": old.get("title", ""), "author_name": old.get("author_name", ""),
                    "book_id": old.get("bookId", 0), "filename": old.get("filename", ""),
                    "local_dir": old.get("dir", ""), "series_title": old.get("seriesTitle", ""),
                    "extra": extra, "timestamp": old.get("timestamp", time.time()),
                })
                changed += 1
            if changed:
                self._commit(converted)
                logger.warning("Migrated %s legacy task(s); unprovable completion will require review", changed)
            return changed

    def add_item(self, item: Dict[str, Any]) -> None:
        """Add an item to state and persist.

        Args:
            item: Download item dict from grab_list
        """
        with self._lock:
            # Store only the fields needed for resume
            state_item = {
                "author_name": item.get("author_name", ""),
                "title": item.get("title", ""),
                "bookId": item.get("bookId", 0),
                "dir": item.get("dir", ""),
                "full_dir": item.get("full_dir", ""),
                "username": item.get("username", ""),
                "filename": item.get("filename", ""),
                "files": item.get("files", []),
                "backend_name": item.get("backend_name", "slskd"),
                "timestamp": time.time(),
            }
            self._commit(self.items + [state_item])

    def add_task(self, task: Any) -> None:
        """Add a DownloadTask to state and persist.

        Args:
            task: DownloadTask from orchestrator
        """
        with self._lock:
            state_item = {
                "task_id": task.task_id,
                "backend_name": task.backend_name,
                "book_title": task.book_title,
                "author_name": task.author_name,
                "book_id": task.book_id,
                "filename": task.filename,
                "series_title": getattr(task, "series_title", ""),
                "local_dir": getattr(task, "local_dir", ""),
                "extra": task.extra,
                "timestamp": time.time(),
            }
            items = copy.deepcopy(self.items)
            for index, item in enumerate(items):
                if item.get("task_id") == task.task_id:
                    state_item["timestamp"] = item.get("timestamp", state_item["timestamp"])
                    items[index] = state_item
                    break
            else:
                items.append(state_item)
            self._commit(items)

    def update_task(self, task: Any) -> None:
        """Refresh a persisted task (current files, timers) before handing it to the next run."""
        self.add_task(task)

    def set_extra(self, task_id: str, extra: Dict[str, Any]) -> None:
        """Replace a saved task's extra data (e.g. a follow-up on a pending import)."""
        with self._lock:
            items = copy.deepcopy(self.items)
            for item in items:
                if item.get("task_id") == task_id:
                    item["extra"] = copy.deepcopy(extra)
                    self._commit(items)
                    return
            raise StatePersistenceError(f"Cannot checkpoint missing task {task_id}; refusing an untracked mutation")

    def remove_task(self, task_id: str) -> None:
        """Remove a task by its task_id.

        Args:
            task_id: Unique task identifier
        """
        with self._lock:
            self._commit([item for item in self.items if item.get("task_id") != task_id])

    def get_tasks_for_orchestrator(self) -> List[Dict[str, Any]]:
        """Get items formatted for orchestrator resume.

        Returns:
            List of task data dicts for DownloadOrchestrator.resume_tasks()
        """
        with self._lock:
            return copy.deepcopy([item for item in self.items if "task_id" in item])

    def remove_item(self, username: str, filename: str) -> None:
        """Remove an item by composite key (username, filename).

        Args:
            username: slskd username
            filename: Full filename path
        """
        with self._lock:
            filename_basename = filename.split("\\")[-1] if "\\" in filename else filename

            self._commit([item for item in self.items if not self._matches_composite_key(item, username, filename_basename)])

    def _matches_composite_key(self, item: Dict[str, Any], username: str, filename_basename: str) -> bool:
        """Check if item matches the composite key."""
        if item.get("username") != username:
            return False

        item_filename = item.get("filename", "")
        item_basename = item_filename.split("\\")[-1] if "\\" in item_filename else item_filename

        return item_basename == filename_basename

    def clear(self) -> None:
        """Clear all state and delete file."""
        with self._lock:
            self._commit([])
            if self.filepath.exists() and not self.read_only:
                try:
                    self.filepath.unlink()
                    fsync_directory(self.filepath.parent)
                except OSError as exc:
                    raise StatePersistenceError(f"Could not clear recovery state: {exc}") from exc
                logger.info("State file deleted - all items processed")

    def get_items(self) -> List[Dict[str, Any]]:
        """Get all items from state."""
        with self._lock:
            return copy.deepcopy(self.items)

