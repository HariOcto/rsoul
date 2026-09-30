"""
State management for resume functionality.

Persists grab_list to disk so downloads can be resumed after interruption.
Uses (username, filename) as composite key since slskd IDs are ephemeral.
"""

import json
import os
import logging
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
        self._load()

    def _load(self) -> None:
        """Load state from disk."""
        if not self.filepath.exists():
            logger.debug(f"No state file found at {self.filepath}")
            return

        try:
            with open(self.filepath, "r") as f:
                self.items = json.load(f)
            logger.info(f"Loaded {len(self.items)} items from state file")
        except json.JSONDecodeError:
            logger.error("State file corrupted. Starting fresh.")
            self.filepath.unlink(missing_ok=True)
            self.items = []
        except Exception as e:
            logger.error(f"Error loading state file: {e}")
            self.items = []

    def _save(self) -> None:
        """Atomic write to disk using temp file + rename."""
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
        for item in self.items:
            if item.get("task_id") == task.task_id:
                item["extra"] = task.extra
                item["local_dir"] = getattr(task, "local_dir", item.get("local_dir", ""))
                item["filename"] = task.filename
                self._save()
                return
        self.add_task(task)

    def remove_task(self, task_id: str) -> None:
        """Remove a task by its task_id.

        Args:
            task_id: Unique task identifier
        """
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
        self.items = []
        if self.filepath.exists():
            self.filepath.unlink()
            logger.info("State file deleted - all items processed")

    def get_items(self) -> List[Dict[str, Any]]:
        """Get all items from state."""
        return self.items.copy()

