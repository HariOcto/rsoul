"""
State management for resume functionality.

Persists grab_list to disk so downloads can be resumed after interruption.
Uses (username, filename) as composite key since slskd IDs are ephemeral.
"""

import json
import os
import shutil
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

STATE_FILENAME = "grab_list_state.json"


class StateManager:
    """Manages persistence of grab_list for resume functionality."""

    def __init__(self, config_dir: str):
        """Initialize StateManager.

        Args:
            config_dir: Directory to store state file
        """
        self.filepath = Path(config_dir) / STATE_FILENAME
        self.items: List[Dict[str, Any]] = []
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
            kept = self._keep_damaged_copy(move=True)
            logger.error(f"State file is damaged ({e}); starting fresh." + (f" The damaged file was kept as {kept}" if kept else ""))
            self.items = []
            return
        except Exception as e:
            logger.error(f"Error loading state file: {e}")
            self.items = []
            return

        self.items = [item for item in items if self._usable(item)]
        dropped = len(items) - len(self.items)
        if dropped:
            kept = self._keep_damaged_copy(move=False)
            logger.error(f"Ignoring {dropped} damaged entr{'y' if dropped == 1 else 'ies'} in the state file." + (f" A copy of the file was kept as {kept}" if kept else ""))
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
            return isinstance(value, (int, float)) and not isinstance(value, bool)

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
        """Atomic write to disk using temp file + rename."""
        with self._lock:
            try:
                temp_path = self.filepath.with_suffix(".tmp")

                with open(temp_path, "w") as f:
                    json.dump(self.items, f, indent=2)

                # Atomic swap
                os.replace(temp_path, self.filepath)
                logger.debug(f"State saved: {len(self.items)} items")

            except Exception as e:
                logger.error(f"Error saving state file: {e}")

    def has_pending_state(self) -> bool:
        """Check if there's a saved state to resume."""
        return len(self.items) > 0

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
            self.items.append(state_item)
            self._save()

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
            self.items.append(state_item)
            self._save()

    def update_task(self, task: Any) -> None:
        """Refresh a persisted task (current files, timers) before handing it to the next run."""
        with self._lock:
            for item in self.items:
                if item.get("task_id") == task.task_id:
                    item["extra"] = task.extra
                    item["local_dir"] = getattr(task, "local_dir", item.get("local_dir", ""))
                    item["filename"] = task.filename
                    self._save()
                    return
            self.add_task(task)

    def set_extra(self, task_id: str, extra: Dict[str, Any]) -> None:
        """Replace a saved task's extra data (e.g. a follow-up on a pending import)."""
        with self._lock:
            for item in self.items:
                if item.get("task_id") == task_id:
                    item["extra"] = extra
                    self._save()
                    return

    def remove_task(self, task_id: str) -> None:
        """Remove a task by its task_id.

        Args:
            task_id: Unique task identifier
        """
        with self._lock:
            self.items = [item for item in self.items if item.get("task_id") != task_id]
            self._save()

    def get_tasks_for_orchestrator(self) -> List[Dict[str, Any]]:
        """Get items formatted for orchestrator resume.

        Returns:
            List of task data dicts for DownloadOrchestrator.resume_tasks()
        """
        return [item for item in self.items if "task_id" in item]

    def remove_item(self, username: str, filename: str) -> None:
        """Remove an item by composite key (username, filename).

        Args:
            username: slskd username
            filename: Full filename path
        """
        with self._lock:
            filename_basename = filename.split("\\")[-1] if "\\" in filename else filename

            self.items = [item for item in self.items if not self._matches_composite_key(item, username, filename_basename)]
            self._save()

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
            self.items = []
            if self.filepath.exists():
                self.filepath.unlink()
                logger.info("State file deleted - all items processed")

    def get_items(self) -> List[Dict[str, Any]]:
        """Get all items from state."""
        return self.items.copy()

