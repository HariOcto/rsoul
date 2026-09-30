"""
Download Orchestrator - manages multiple backends with fallback logic.

The orchestrator tries backends in priority order until one succeeds.
It handles the search → download → monitor lifecycle.
"""

import time
import logging
from typing import List, Optional, Dict, Any, TYPE_CHECKING, Callable

from .backends.base import (
    DownloadBackend,
    DownloadStatus,
    DownloadTask,
    DownloadTarget,
)

if TYPE_CHECKING:
    from .config import Context

logger = logging.getLogger(__name__)


class DownloadOrchestrator:
    """Manages multiple download backends with fallback logic.

    Tries each backend in priority order until success or all fail.
    """

    def __init__(self, backends: List[DownloadBackend], ctx: "Context"):
        """Initialize orchestrator.

        Args:
            backends: List of backends, already sorted by priority
            ctx: Application context
        """
        self.backends = backends
        self.ctx = ctx
        self.active_tasks: List[DownloadTask] = []

        # Timeouts from config
        self.stalled_timeout = ctx.config.getint(
            "General", "stalled_timeout",
            fallback=ctx.config.getint("Slskd", "stalled_timeout", fallback=3600)
        )
        self.poll_interval = 10  # seconds between status checks

    def get_backend(self, name: str) -> Optional[DownloadBackend]:
        """Get a backend instance by name.

        Args:
            name: Backend name

        Returns:
            Backend instance or None if not found
        """
        for backend in self.backends:
            if backend.name == name:
                return backend
        return None

    def acquire_book(self, target: DownloadTarget) -> Optional[DownloadTask]:
        """Try to acquire a book using available backends.

        Iterates through backends in priority order until one succeeds.

        Args:
            target: Book to download

        Returns:
            Completed DownloadTask if successful, None if all backends failed
        """
        for backend in self.backends:
            if not backend.is_available():
                logger.debug(f"Backend {backend.name} not available, skipping")
                continue

            logger.info(f"Trying backend: {backend.name}")

            try:
                task = self._try_backend(backend, target)
                if task and task.status == DownloadStatus.COMPLETED:
                    return task
            except Exception as e:
                logger.warning(f"Backend {backend.name} failed: {e}")
                continue

        logger.warning(f"All backends failed for: {target.book_title}")
        return None

    def start_download(self, target: DownloadTarget) -> Optional[DownloadTask]:
        """Try to start download for a book using available backends (Non-blocking).

        Iterates through backends in priority order until one succeeds in STARTING a download.
        Does NOT wait for completion.

        Args:
            target: Book to download

        Returns:
            Started DownloadTask if successful, None if all backends failed
        """
        for backend in self.backends:
            if not backend.is_available():
                logger.debug(f"Backend {backend.name} not available, skipping")
                continue

            logger.info(f"Trying backend: {backend.name}")

            try:
                task = self._start_backend_download(backend, target)
                if task:
                    return task
            except Exception as e:
                logger.warning(f"Backend {backend.name} failed: {e}")
                continue

        logger.warning(f"All backends failed to start download for: {target.book_title}")
        return None

    def _try_backend(self, backend: DownloadBackend, target: DownloadTarget) -> Optional[DownloadTask]:
        """Try to acquire book using a single backend.

        Args:
            backend: Backend to use
            target: Book to download

        Returns:
            DownloadTask if successful, None if failed
        """
        # Start download (Search + Enqueue)
        task = self._start_backend_download(backend, target)
        if not task:
            return None

        # Monitor until completion (Blocking)
        completed_task = self._monitor_task(backend, task)

        return completed_task

    def _start_backend_download(self, backend: DownloadBackend, target: DownloadTarget) -> Optional[DownloadTask]:
        """Search and start download without monitoring.

        Args:
            backend: Backend to use
            target: Book to download

        Returns:
            DownloadTask if started, None if failed
        """
        # 1. Search
        logger.info(f"Beginning search for: {target.author_name} - {target.book_title}")
        results = backend.search(target)
        if not results:
            logger.info(f"No results from {backend.name} for: {target.book_title}")
            return None

        logger.info(f"Found {len(results)} results from {backend.name}")

        # 2. Download best result
        best_result = results[0]

        task = backend.download(target, best_result)
        if not task:
            logger.warning(f"Failed to start download from {backend.name}")
            return None

        # Add to state for resume functionality
        if self.ctx.state:
            self.ctx.state.add_task(task)

        return task

    def _monitor_task(self, backend: DownloadBackend, task: DownloadTask) -> DownloadTask:
        """Monitor a download until completion or timeout.

        Args:
            backend: Backend that owns the task
            task: Task to monitor

        Returns:
            Updated task with final status
        """
        start_time = time.time()

        while True:
            # Check timeout
            elapsed = time.time() - start_time
            if elapsed >= task.extra.get("stalled_timeout", self.stalled_timeout):
                logger.error(f"Download timed out after {elapsed:.0f}s: {task.filename}")
                backend.cancel(task)
                task.status = DownloadStatus.FAILED
                task.error_message = "Stalled timeout"
                return task

            # Poll status
            task = backend.get_status(task)

            if task.status == DownloadStatus.COMPLETED:
                logger.info(f"Download completed: {task.filename}")
                return task

            if task.status == DownloadStatus.FAILED:
                logger.warning(f"Download failed: {task.filename} - {task.error_message}")
                return task

            if task.status == DownloadStatus.CANCELLED:
                logger.info(f"Download cancelled: {task.filename}")
                return task

            # Still in progress
            logger.debug(f"Download progress: {task.filename} - {task.progress_percent:.1f}%")
            time.sleep(self.poll_interval)

    def process_targets(self, targets: List[DownloadTarget]) -> Dict[str, Any]:
        """Process a list of download targets.

        Args:
            targets: Books to download

        Returns:
            Dict with stats: succeeded, failed, tasks
        """
        succeeded = 0
        failed = 0
        completed_tasks: List[DownloadTask] = []

        for target in targets:
            task = self.acquire_book(target)

            if task and task.status == DownloadStatus.COMPLETED:
                succeeded += 1
                completed_tasks.append(task)
            else:
                failed += 1

        return {
            "succeeded": succeeded,
            "failed": failed,
            "tasks": completed_tasks,
        }

    def batch_process_targets(self, targets: List[DownloadTarget], on_complete: Optional[Callable[[DownloadTask], None]] = None) -> Dict[str, Any]:
        """Process a list of download targets using batch workflow.

        Phase 1: Search & Enqueue all targets (with rate limiting)
        Phase 2: Monitor all active tasks concurrently
        Phase 3: Return results

        Args:
            targets: Books to download
            on_complete: Callback for completed tasks

        Returns:
            Dict with stats: succeeded, failed, tasks
        """
        active_tasks: List[DownloadTask] = []
        failed_targets: List[DownloadTarget] = []

        logger.info(f"Starting batch processing for {len(targets)} targets")

        # Phase 1: Search & Enqueue
        batch_delay = self.ctx.config.getfloat("General", "batch_delay", fallback=3.0)
        last_target_time = 0.0

        for i, target in enumerate(targets):
            # Elapsed-time-aware rate limiting (skip for first item)
            if i > 0 and batch_delay > 0:
                elapsed = time.time() - last_target_time
                if elapsed < batch_delay:
                    time.sleep(batch_delay - elapsed)

            last_target_time = time.time()

            task = self.start_download(target)
            if task:
                active_tasks.append(task)
            else:
                failed_targets.append(target)
                # Fire callback with synthetic FAILED task so caller can handle
                # (summary, unmonitor, failure_list.txt)
                if on_complete:
                    failed_task = DownloadTask(
                        task_id=f"not_found_{target.book_id}",
                        backend_name="none",
                        status=DownloadStatus.FAILED,
                        book_title=target.book_title,
                        author_name=target.author_name,
                        book_id=target.book_id,
                        series_title=target.series_title,
                        filename="",
                        error_message="No backend could find or start download",
                    )
                    try:
                        on_complete(failed_task)
                    except Exception as e:
                        logger.error(f"Error in on_complete callback for failed target: {e}")

        logger.info(f"Batch Enqueue Complete. Active: {len(active_tasks)}, Failed to start: {len(failed_targets)}")

        # Phase 2: Monitor
        completed_tasks = self.monitor_multiple_tasks(active_tasks, on_complete)

        # Phase 3: Compile results
        succeeded_tasks = [t for t in completed_tasks if t.status == DownloadStatus.COMPLETED]
        failed_tasks = [t for t in completed_tasks if t.status != DownloadStatus.COMPLETED]

        return {
            "succeeded": len(succeeded_tasks),
            "failed": len(failed_targets) + len(failed_tasks),
            "tasks": completed_tasks,
        }

    def monitor_multiple_tasks(self, tasks: List[DownloadTask], on_complete: Optional[Callable[[DownloadTask], None]] = None) -> List[DownloadTask]:
        """Monitor multiple tasks concurrently until all complete or timeout.

        Args:
            tasks: List of active tasks
            on_complete: Callback for completed tasks

        Returns:
            List of completed tasks (including failed/cancelled)
        """
        active_map = {t.task_id: t for t in tasks}
        completed_map = {}

        # Track start time for timeouts (relative to monitoring start)
        start_times = {t.task_id: time.time() for t in tasks}

        while active_map:
            # Snapshot keys to allow modification during iteration
            current_active_ids = list(active_map.keys())

            for task_id in current_active_ids:
                task = active_map[task_id]

                # Check timeout
                elapsed = time.time() - start_times[task_id]
                if elapsed >= task.extra.get("stalled_timeout", self.stalled_timeout):
                    logger.error(f"Download timed out after {elapsed:.0f}s: {task.filename}")
                    backend = self.get_backend(task.backend_name)
                    if backend:
                        backend.cancel(task)
                    task.status = DownloadStatus.FAILED
                    task.error_message = "Stalled timeout"
                    completed_map[task_id] = task
                    del active_map[task_id]

                    if on_complete:
                        try:
                            on_complete(task)
                        except Exception as e:
                            logger.error(f"Error in on_complete callback: {e}")

                    continue

                # Poll status
                backend = self.get_backend(task.backend_name)
                if not backend:
                    task.status = DownloadStatus.FAILED
                    task.error_message = "Backend unavailable"
                    completed_map[task_id] = task
                    del active_map[task_id]

                    if on_complete:
                        try:
                            on_complete(task)
                        except Exception as e:
                            logger.error(f"Error in on_complete callback: {e}")

                    continue

                try:
                    updated_task = backend.get_status(task)
                except Exception as e:
                    logger.error(f"Error checking status for {task.filename}: {e}")
                    # Don't fail immediately, wait for next poll? Or fail?
                    # Robustness: keep checking unless it persists. For now, just log.
                    updated_task = task

                if updated_task.status in [DownloadStatus.COMPLETED, DownloadStatus.FAILED, DownloadStatus.CANCELLED]:
                    completed_map[task_id] = updated_task
                    del active_map[task_id]

                    # Log completion immediately
                    if updated_task.status == DownloadStatus.COMPLETED:
                        logger.info(f"Task completed: {updated_task.filename}")
                    else:
                        logger.warning(f"Task failed: {updated_task.filename} - {updated_task.error_message}")

                    if on_complete:
                        try:
                            on_complete(updated_task)
                        except Exception as e:
                            logger.error(f"Error in on_complete callback: {e}")
                else:
                    # Update the task object in map
                    active_map[task_id] = updated_task

            # Log summary
            if active_map:
                self._log_batch_status(list(active_map.values()), list(completed_map.values()))
                time.sleep(self.poll_interval)

        return list(completed_map.values())

    def _log_batch_status(self, active: List[DownloadTask], completed: List[DownloadTask]) -> None:
        """Log a summary of batch progress."""
        total = len(active) + len(completed)
        done = len(completed)

        logger.info(f"[Batch Monitor] Progress: {done}/{total} tasks. Active: {len(active)}")

    def resume_tasks(self, persisted_tasks: List[Dict[str, Any]]) -> List[DownloadTask]:
        """Resume tasks from persisted state.

        Args:
            persisted_tasks: Task data from state file

        Returns:
            List of reconciled and still-active tasks
        """
        resumed: List[DownloadTask] = []

        for task_data in persisted_tasks:
            backend_name = task_data.get("backend_name", "slskd")

            # Find the backend
            backend = None
            for b in self.backends:
                if b.name == backend_name:
                    backend = b
                    break

            if not backend:
                logger.warning(f"Backend {backend_name} not available for resume")
                continue

            # Let backend reconcile the task
            task = backend.reconcile_task(task_data)
            if task:
                resumed.append(task)
                logger.info(f"Resumed task: {task.filename} from {backend_name}")
            else:
                logger.warning(f"Could not resume task: {task_data.get('filename', 'unknown')}")

        return resumed

    def monitor_resumed_tasks(self, tasks: List[DownloadTask], on_complete: Optional[Callable[[DownloadTask], None]] = None) -> List[DownloadTask]:
        """Monitor resumed tasks until all complete.

        Delegates to monitor_multiple_tasks for consistent behavior.

        Args:
            tasks: Resumed tasks to monitor
            on_complete: Callback for completed tasks

        Returns:
            List of completed tasks
        """
        return self.monitor_multiple_tasks(tasks, on_complete)
