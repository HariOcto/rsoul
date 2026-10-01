"""
Download Orchestrator - manages multiple backends with fallback logic.

The orchestrator tries backends in priority order until one succeeds.
It handles the search → download → monitor lifecycle.
"""

import time
import logging
from typing import List, Optional, Dict, Any, TYPE_CHECKING, Callable, Tuple

from . import health
from .backends.base import (
    BackendUnavailable,
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

        # Progress-based timeouts (seconds, 0 = disabled). A download is only given up on when it
        # stops moving, waits too long in the peer's queue, or exceeds the overall safety cap.
        cfg = ctx.config
        self.stall_timeout = cfg.getint("Download Settings", "stall_timeout", fallback=1800)
        self.queue_timeout = cfg.getint("Download Settings", "queue_timeout", fallback=3600)
        self.max_download_time = cfg.getint("Download Settings", "max_download_time", fallback=86400)

        # Settings from older versions that no longer do anything. remote_queue_timeout was never
        # read before, so honouring it now (the sample config had 300s) would cancel most queued
        # downloads after five minutes on upgrade; it is deliberately ignored instead.
        for section, option in (("Slskd", "stalled_timeout"), ("General", "stalled_timeout"), ("Slskd", "remote_queue_timeout")):
            if cfg.has_option(section, option):
                logger.warning(
                    f"[{section}] {option} is no longer used and can be removed. Downloads now time out on lack of "
                    "progress: see stall_timeout, queue_timeout and max_download_time in [Download Settings]."
                )
        self.poll_interval = 10  # seconds between status checks
        self.unchecked_task_ids: set = set()

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

        # Timers start when the download is queued, not at the first status poll (which only
        # happens after every other book in the batch has been searched)
        now = time.time()
        task.extra.setdefault("monitor", {}).update({"started_at": now, "last_progress_at": now})

        # Add to state for resume functionality
        if self.ctx.state:
            self.ctx.state.add_task(task)

        return task

    def check_timeouts(self, task: DownloadTask, now: Optional[float] = None) -> Optional[str]:
        """Apply progress-based timeouts to a freshly polled, still-active task.

        Timer state lives in task.extra["monitor"] (wall-clock timestamps), so it
        survives being handed from one run to the next.

        Returns:
            A failure reason if the task should be given up on, else None.
        """
        now = time.time() if now is None else now
        m = task.extra.setdefault("monitor", {})
        m.setdefault("started_at", now)
        m.setdefault("last_progress_at", now)
        m.setdefault("last_bytes", 0)
        m.setdefault("last_percent", 0.0)

        if self.max_download_time > 0 and now - m["started_at"] >= self.max_download_time:
            return f"Exceeded max_download_time ({self.max_download_time}s)"

        if task.poll_failed:
            # Status unknown (e.g. slskd restarting): an outage is not the peer's fault, so the
            # stall and queue timers wait; they restart from here once polling works again
            m["last_progress_at"] = now
            if m.get("queued_since") is not None:
                m["queued_since"] = now
            return None

        # Progress is measured in bytes where the backend reports them (slskd) and in percent
        # otherwise (Stacks reports only a percentage). It is checked before the queue state:
        # a multi-file audiobook can show "queued" at every poll (the peer serves one chapter
        # at a time and the poll lands between chapters) while bytes keep arriving.
        bytes_now, percent_now = task.bytes_transferred, task.progress_percent or 0.0
        moved = bytes_now != m["last_bytes"] or percent_now != m["last_percent"]
        if moved:
            # Forward progress, or a counter that went backwards (resumed task tracking fewer
            # files, a restarted transfer): either way a fresh baseline, not a stall
            m["last_bytes"], m["last_percent"], m["last_progress_at"] = bytes_now, percent_now, now

        if task.status == DownloadStatus.QUEUED_LOCALLY:
            # Waiting for our own slskd download slots: not the peer's fault, so no timer runs
            m["queued_since"] = None
            m["last_progress_at"] = now
            return None

        if task.status == DownloadStatus.QUEUED:
            if m.get("queued_since") is None or moved:
                m["queued_since"] = now  # data arrived since the last poll, so the queue moved too
            if self.queue_timeout > 0 and now - m["queued_since"] >= self.queue_timeout:
                return f"Waited more than {self.queue_timeout}s in a queue without starting"
            # Waiting in a queue is not a stall
            m["last_progress_at"] = now
            return None

        m["queued_since"] = None
        if moved:
            return None

        if self.stall_timeout > 0 and now - m["last_progress_at"] >= self.stall_timeout:
            return f"No data received for {self.stall_timeout}s"
        return None

    def _poll(self, backend: DownloadBackend, task: DownloadTask) -> DownloadTask:
        """Refresh a task's status. Tasks that are already finished (e.g. a resumed download
        found complete on disk) are left as they are; failed polls are flagged, not fatal."""
        if task.status in (DownloadStatus.COMPLETED, DownloadStatus.FAILED, DownloadStatus.CANCELLED):
            return task
        try:
            task.poll_failed = False
            return backend.get_status(task)
        except Exception as e:
            # Transient API errors: keep the task and try again next poll
            logger.error(f"Error checking status for {task.filename}: {e}")
            task.poll_failed = True
            return task

    def _monitor_task(self, backend: DownloadBackend, task: DownloadTask) -> DownloadTask:
        """Monitor a download until completion or timeout (blocking).

        Args:
            backend: Backend that owns the task
            task: Task to monitor

        Returns:
            Updated task with final status
        """
        while True:
            task = self._poll(backend, task)
            if task.status == DownloadStatus.COMPLETED:
                logger.info(f"Download completed: {task.filename}")
                return task
            if task.status == DownloadStatus.FAILED:
                logger.warning(f"Download failed: {task.filename} - {task.error_message}")
                return task
            if task.status == DownloadStatus.CANCELLED:
                logger.info(f"Download cancelled: {task.filename}")
                return task

            reason = self.check_timeouts(task)
            if reason:
                logger.error(f"Giving up on {task.book_title} ({task.filename}): {reason}")
                backend.cancel(task)
                task.status = DownloadStatus.FAILED
                task.error_message = reason
                return task

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

    def start_targets(self, targets: List[DownloadTarget], on_complete: Optional[Callable[[DownloadTask], None]] = None) -> List[DownloadTask]:
        """Search and enqueue every target (with rate limiting), without waiting for downloads.

        Targets that no backend could start are reported through on_complete as a FAILED task.

        Returns:
            Started tasks, all downloading in parallel from here on.
        """
        active_tasks: List[DownloadTask] = []
        failed_count = 0

        batch_delay = self.ctx.config.getfloat("General", "batch_delay", fallback=3.0)
        last_target_time = 0.0

        for i, target in enumerate(targets):
            # Elapsed-time-aware rate limiting (skip for first item)
            if i > 0 and batch_delay > 0:
                elapsed = time.time() - last_target_time
                if elapsed < batch_delay:
                    time.sleep(batch_delay - elapsed)
            last_target_time = time.time()
            health.heartbeat()

            task = self.start_download(target)
            if task:
                active_tasks.append(task)
                continue

            failed_count += 1
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

        logger.info(f"Batch Enqueue Complete. Active: {len(active_tasks)}, Failed to start: {failed_count}")
        return active_tasks

    def batch_process_targets(self, targets: List[DownloadTarget], on_complete: Optional[Callable[[DownloadTask], None]] = None) -> Dict[str, Any]:
        """Process a list of download targets using batch workflow.

        Phase 1: Search & Enqueue all targets (with rate limiting)
        Phase 2: Monitor all active tasks concurrently
        Phase 3: Return results
        """
        logger.info(f"Starting batch processing for {len(targets)} targets")
        active_tasks = self.start_targets(targets, on_complete)
        completed_tasks = self.monitor_multiple_tasks(active_tasks, on_complete)

        succeeded_tasks = [t for t in completed_tasks if t.status == DownloadStatus.COMPLETED]
        failed_tasks = [t for t in completed_tasks if t.status != DownloadStatus.COMPLETED]
        return {
            "succeeded": len(succeeded_tasks),
            "failed": (len(targets) - len(active_tasks)) + len(failed_tasks),
            "tasks": completed_tasks,
        }

    def monitor_multiple_tasks(self, tasks: List[DownloadTask], on_complete: Optional[Callable[[DownloadTask], None]] = None) -> List[DownloadTask]:
        """Monitor multiple tasks concurrently until all complete or time out.

        Returns:
            List of completed tasks (including failed/cancelled)
        """
        completed, _ = self.monitor_until(tasks, on_complete)
        return completed

    def _finish(self, task: DownloadTask, on_complete: Optional[Callable[[DownloadTask], None]]) -> None:
        if task.status == DownloadStatus.COMPLETED:
            logger.info(f"Task completed: {task.book_title} ({task.filename})")
        else:
            logger.warning(f"Task failed: {task.book_title} ({task.filename}) - {task.error_message}")
        if on_complete:
            try:
                on_complete(task)
            except Exception as e:
                logger.error(f"Error in on_complete callback: {e}")

    def monitor_until(
        self,
        tasks: List[DownloadTask],
        on_complete: Optional[Callable[[DownloadTask], None]] = None,
        deadline: Optional[float] = None,
    ) -> Tuple[List[DownloadTask], List[DownloadTask]]:
        """Monitor tasks in parallel until they all finish, or until the deadline passes.

        Each task is judged on its own progress (see check_timeouts), so one slow
        download never causes another to be cancelled.

        Args:
            tasks: Active tasks
            on_complete: Called as each task finishes (successfully or not)
            deadline: Wall-clock time to stop monitoring; unfinished tasks are returned
                      so the caller can hand them to the next run. None = wait for all.

        Returns:
            (finished tasks, still-running tasks)
        """
        active_map = {t.task_id: t for t in tasks}
        completed_map: Dict[str, DownloadTask] = {}

        while active_map:
            for task_id in list(active_map.keys()):
                task = active_map[task_id]

                backend = self.get_backend(task.backend_name)
                if not backend:
                    task.status = DownloadStatus.FAILED
                    task.error_message = "Backend unavailable"
                    completed_map[task_id] = task
                    del active_map[task_id]
                    self._finish(task, on_complete)
                    continue

                task = self._poll(backend, task)

                if task.status in [DownloadStatus.COMPLETED, DownloadStatus.FAILED, DownloadStatus.CANCELLED]:
                    completed_map[task_id] = task
                    del active_map[task_id]
                    self._finish(task, on_complete)
                    continue

                reason = self.check_timeouts(task)
                if reason:
                    logger.error(f"Giving up on {task.book_title} ({task.filename}): {reason}")
                    backend.cancel(task)
                    task.status = DownloadStatus.FAILED
                    task.error_message = reason
                    completed_map[task_id] = task
                    del active_map[task_id]
                    self._finish(task, on_complete)
                    continue

                active_map[task_id] = task

            if not active_map:
                break
            if deadline is not None and time.time() >= deadline:
                logger.info(f"Monitor window over: handing {len(active_map)} unfinished download(s) to the next run")
                break

            self._log_batch_status(list(active_map.values()), list(completed_map.values()))
            health.heartbeat()
            time.sleep(self.poll_interval)

        return list(completed_map.values()), list(active_map.values())

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
        # Saved tasks that couldn't be checked (backend unreachable or disabled): not resumed
        # this run, but not given up either
        self.unchecked_task_ids = set()

        for task_data in persisted_tasks:
            backend_name = task_data.get("backend_name", "slskd")

            # Find the backend
            backend = None
            for b in self.backends:
                if b.name == backend_name:
                    backend = b
                    break

            if not backend:
                logger.warning(f"Backend {backend_name} not available: keeping {task_data.get('book_title', 'download')} for a later run")
                self.unchecked_task_ids.add(task_data.get("task_id"))
                continue

            # Let backend reconcile the task
            try:
                task = backend.reconcile_task(task_data)
            except BackendUnavailable as e:
                logger.warning(f"Could not check {task_data.get('book_title', 'download')} ({e}); keeping it for the next run")
                self.unchecked_task_ids.add(task_data.get("task_id"))
                continue
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
