"""
Download Orchestrator - manages multiple backends with fallback logic.

The orchestrator tries backends in priority order until one succeeds.
It handles the search → download → monitor lifecycle.
"""

import time
import logging
import inspect
from typing import List, Optional, Dict, Any, TYPE_CHECKING, Callable, Tuple

from . import health
from .outcomes import FailureKind, SearchIncomplete, check_fatal_state
from .persistence import StatePersistenceError
from .search_control import responsive_wait
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

    # How many search results are tried, best first, until one download starts
    MAX_START_ATTEMPTS = 3
    supports_interleaved_search = True

    def __init__(self, backends: List[DownloadBackend], ctx: "Context"):
        """Initialize orchestrator.

        Args:
            backends: List of backends, already sorted by priority
            ctx: Application context
        """
        self.backends = backends
        # Older subclasses may override start_targets with the original protocol.
        parameters = inspect.signature(self.start_targets).parameters
        self.supports_interleaved_search = all(name in parameters for name in ("existing_tasks", "max_active"))
        self.ctx = ctx
        self.active_tasks: List[DownloadTask] = []

        # Progress-based timeouts (seconds, 0 = disabled). A download is only given up on when it
        # stops moving, waits too long in the peer's queue, or exceeds the overall safety cap.
        cfg = ctx.config
        # Read as numbers, not whole numbers: validate_config accepts "1800.5" and "1800.0"
        self.stall_timeout = cfg.getfloat("Download Settings", "stall_timeout", fallback=1800)
        self.queue_timeout = cfg.getfloat("Download Settings", "queue_timeout", fallback=3600)
        self.max_download_time = cfg.getfloat("Download Settings", "max_download_time", fallback=86400)

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
        self.last_start_failure = "not_found"
        self._found_results = False
        self.unchecked_task_ids: set = set()
        self.attempted_book_ids: set = set()
        self.completed_during_search = []
        self.remaining_existing_tasks = []
        self._search_active = None
        self._search_callback = None
        self._last_search_poll = 0.0
        self._polling_search = False
        self._progress_logs = {}
        self.last_start_kind = FailureKind.NO_MATCH.value
        self.last_start_retry_at = None
        self.ctx.orchestrator = self

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
            check_fatal_state(self.ctx)
            if not backend.is_available():
                logger.debug(f"Backend {backend.name} not available, skipping")
                continue

            logger.info(f"Trying backend: {backend.name}")

            try:
                task = self._try_backend(backend, target)
                if task and task.status == DownloadStatus.COMPLETED:
                    return task
            except StatePersistenceError:
                raise
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
        searched = False
        incomplete = False
        self._found_results = False
        self.last_start_kind = FailureKind.NO_MATCH.value
        self.last_start_retry_at = None
        for backend in self.backends:
            check_fatal_state(self.ctx)
            if not backend.is_available():
                incomplete = True
                self.last_start_kind = FailureKind.SERVICE_UNAVAILABLE.value
                logger.warning("Backend %s is unavailable; search outcome deferred", backend.name)
                continue
            try:
                task = self._start_backend_download(backend, target)
                searched = True
                if task:
                    return task
            except StatePersistenceError:
                raise
            except SearchIncomplete as error:
                incomplete = True
                self.last_start_kind = error.kind
                self.last_start_retry_at = error.retry_at
                logger.warning("Search deferred for %s: %s", target.book_title, error)
            except Exception as error:
                incomplete = True
                self.last_start_kind = FailureKind.SERVICE_UNAVAILABLE.value
                logger.warning("Backend %s could not complete search/start: %s", backend.name, error)
        self.last_start_failure = "not_searched" if incomplete or not searched else "not_started" if self._found_results else "not_found"
        logger.info("No download started for %s (%s; %s)", target.book_title, self.last_start_failure, self.last_start_kind)
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
        logger.info(f"Beginning search for: {target.author_name} - {target.book_title} (book {target.book_id}, {target.media_type})")
        check_fatal_state(self.ctx)
        results = backend.search(target)
        if not results and getattr(backend, "found_but_unavailable", lambda book_id: False)(target.book_id):
            # Matches exist, but none could be used right now (peer not answering, local folder
            # in use): the book was found, just not started
            self._found_results = True
        if not results:
            logger.info(f"No results from {backend.name} for: {target.book_title}")
            return None

        logger.info(f"Found {len(results)} results from {backend.name}")
        self._found_results = True

        # 2. Download the best result; if it can't be started (peer gone, its local folder is
        # in use...), try the next ones instead of failing on the same result run after run
        task = None
        for attempt, result in enumerate(results[: self.MAX_START_ATTEMPTS], start=1):
            check_fatal_state(self.ctx)
            task = backend.download(target, result)
            if task:
                break
            logger.warning(f"Failed to start download from {backend.name} (result {attempt} of {len(results)})")
        if not task:
            return None

        # Timers start when the download is queued, not at the first status poll (which only
        # happens after every other book in the batch has been searched)
        now = time.time()
        task.extra.setdefault("monitor", {}).setdefault("started_at", now)
        task.extra["monitor"].setdefault("last_progress_at", now)

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

        if task.extra.get("enqueue", {}).get("phase") in ("prepared", "unknown") or task.extra.get("failure_kind") == FailureKind.LEGACY_UNVERIFIED.value:
            if self.max_download_time > 0 and now - m["started_at"] >= self.max_download_time:
                task.extra["needs_review"] = True
                task.error_message = "Ownership/completeness remains uncertain beyond max_download_time; manual review required"
            return None

        if self.max_download_time > 0 and now - m["started_at"] >= self.max_download_time:
            return f"Exceeded max_download_time ({self.max_download_time:g}s)"

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
                return f"Waited more than {self.queue_timeout:g}s in a queue without starting"
            # Waiting in a queue is not a stall
            m["last_progress_at"] = now
            return None

        m["queued_since"] = None
        if moved:
            return None

        if self.stall_timeout > 0 and now - m["last_progress_at"] >= self.stall_timeout:
            return f"No data received for {self.stall_timeout:g}s"
        return None

    def _poll(self, backend: DownloadBackend, task: DownloadTask) -> DownloadTask:
        """Refresh a task's status. Tasks that are already finished (e.g. a resumed download
        found complete on disk) are left as they are; failed polls are flagged, not fatal."""
        if task.status in (DownloadStatus.COMPLETED, DownloadStatus.FAILED, DownloadStatus.CANCELLED):
            return task
        try:
            task.poll_failed = False
            return backend.get_status(task)
        except StatePersistenceError:
            raise
        except Exception as e:
            # Transient API errors: keep the task and try again next poll
            logger.error(f"Error checking status for {task.filename}: {e}")
            task.poll_failed = True
            return task

    def _cancel(self, backend: DownloadBackend, task: DownloadTask) -> None:
        """Cancel a download that is being given up on; a failing cancel must not stop the
        monitoring of every other download."""
        try:
            check_fatal_state(self.ctx)
            backend.cancel(task)
        except StatePersistenceError:
            raise
        except Exception as e:
            logger.warning(f"Could not cancel {task.book_title} in {backend.name}: {e}")

    def _monitor_task(self, backend: DownloadBackend, task: DownloadTask) -> DownloadTask:
        """Monitor a download until completion or timeout (blocking).

        Args:
            backend: Backend that owns the task
            task: Task to monitor

        Returns:
            Updated task with final status
        """
        while True:
            if self._advance_task(task, None):
                return task
            health.heartbeat()
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

    def _checkpoint(self, task):
        check_fatal_state(self.ctx)
        state = getattr(self.ctx, "state", None)
        if state is not None and not getattr(state, "read_only", False):
            state.update_task(task)

    def _advance_task(self, task, on_complete):
        check_fatal_state(self.ctx)
        backend = self.get_backend(task.backend_name)
        if backend is None:
            task.poll_failed = True
            task.error_message = "Backend unavailable; retaining transfer ownership"
            return False
        refreshed = self._poll(backend, task)
        if refreshed is not task:
            task.__dict__.update(refreshed.__dict__)
        terminal = task.status in (DownloadStatus.COMPLETED, DownloadStatus.FAILED, DownloadStatus.CANCELLED)
        if not terminal:
            reason = self.check_timeouts(task)
            if reason:
                self._cancel(backend, task)
                task.status = DownloadStatus.FAILED
                task.extra["failure_kind"] = FailureKind.TRANSFER_TIMEOUT.value
                task.error_message = reason
                terminal = True
        if terminal and task.status == DownloadStatus.FAILED:
            retry = getattr(backend, "retry_after_failure", None)
            replacement = retry(task) if callable(retry) else None
            if replacement is not None:
                if replacement is not task:
                    task.__dict__.update(replacement.__dict__)
                terminal = task.status in (DownloadStatus.COMPLETED, DownloadStatus.FAILED, DownloadStatus.CANCELLED)
        # Timer updates and complete transfer evidence survive a crash before callbacks.
        self._checkpoint(task)
        if terminal:
            self._finish(task, on_complete)
            return True
        self._log_task_progress(task)
        return False

    def poll_during_search(self, force=False):
        if self._search_active is None or self._polling_search:
            return
        now = time.time()
        if not force and now - self._last_search_poll < self.poll_interval:
            return
        self._last_search_poll = now
        self._polling_search = True
        try:
            for key, task in list(self._search_active.items()):
                if self._advance_task(task, self._search_callback):
                    self.completed_during_search.append(task)
                    del self._search_active[key]
                elif task.task_id != key:
                    del self._search_active[key]
                    self._search_active[task.task_id] = task
        finally:
            self._polling_search = False

    def start_targets(self, targets: List[DownloadTarget], on_complete=None, existing_tasks=None, max_active=None) -> List[DownloadTask]:
        """Search new targets while polling/importing owned work, honoring current capacity.

        Only actually attempted target IDs are acknowledged to the durable wanted queue.
        Return active new tasks; existing objects are updated in-place, including retries.
        """
        existing_tasks = list(existing_tasks or [])
        existing_objects = {id(task) for task in existing_tasks}
        self.attempted_book_ids = set()
        self.completed_during_search = []
        self._search_active = {task.task_id: task for task in existing_tasks}
        self._search_callback = on_complete
        self._last_search_poll = 0.0
        failed_count = 0
        batch_delay = self.ctx.config.getfloat("General", "batch_delay", fallback=3.0)
        last_target_time = 0.0
        try:
            for target in targets:
                check_fatal_state(self.ctx)
                self.poll_during_search(force=True)
                if max_active is not None and max_active > 0 and len(self._search_active) >= max_active:
                    break
                delay = batch_delay - (time.time() - last_target_time)
                if last_target_time and delay > 0:
                    responsive_wait(self.ctx, delay)
                last_target_time = time.time()
                health.heartbeat()
                check_fatal_state(self.ctx)
                self.attempted_book_ids.add(target.book_id)
                task = self.start_download(target)
                if task is not None:
                    self._search_active[task.task_id] = task
                    continue
                failed_count += 1
                failed = DownloadTask(
                    task_id=f"not_found_{target.book_id}", backend_name="none", status=DownloadStatus.FAILED,
                    book_title=target.book_title, author_name=target.author_name, book_id=target.book_id,
                    series_title=target.series_title, filename="", error_message="No backend could finish searching or start a usable download",
                    extra={"start_failure": self.last_start_failure, "failure_kind": self.last_start_kind,
                           "retry_at": self.last_start_retry_at, "media_type": target.media_type})
                self._finish(failed, on_complete)
            self.poll_during_search(force=True)
            remaining = list(self._search_active.values())
            self.remaining_existing_tasks = [task for task in remaining if id(task) in existing_objects]
            started = [task for task in remaining if id(task) not in existing_objects]
            logger.info("Search batch complete: %s owned active, %s deferred/failed starts", len(remaining), failed_count)
            return started
        finally:
            self._search_active = None
            self._search_callback = None

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
        check_fatal_state(self.ctx)
        if task.status == DownloadStatus.COMPLETED:
            logger.info(f"Task completed: {task.book_title} ({task.filename})")
        else:
            logger.warning(f"Task failed: {task.book_title} ({task.filename}) - {task.error_message}")
        if on_complete:
            try:
                on_complete(task)
            except StatePersistenceError:
                raise
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
            for task_id in list(active_map):
                task = active_map[task_id]
                if self._advance_task(task, on_complete):
                    completed_map[task.task_id] = task
                    del active_map[task_id]
                elif task.task_id != task_id:
                    del active_map[task_id]
                    active_map[task.task_id] = task

            if not active_map:
                break
            if deadline is not None and time.time() >= deadline:
                logger.info(f"Monitor window over: handing {len(active_map)} unfinished download(s) to the next run")
                break

            self._log_batch_status(list(active_map.values()), list(completed_map.values()))
            health.heartbeat()
            time.sleep(self.poll_interval)

        return list(completed_map.values()), list(active_map.values())

    def _log_task_progress(self, task):
        now = time.time()
        progress = task.extra.get("progress", {})
        monitor = task.extra.setdefault("monitor", {})
        signature = (task.status.value, progress.get("files_complete"), task.extra.get("needs_review", False))
        logged_at, logged_signature = self._progress_logs.get(task.task_id, (0, None))
        if now - logged_at < 60 and logged_signature == signature:
            return
        self._progress_logs[task.task_id] = (now, signature)
        elapsed = max(0, now - monitor.get("last_progress_at", now))
        deadlines = []
        if self.max_download_time > 0:
            deadlines.append(monitor.get("started_at", now) + self.max_download_time)
        if task.status == DownloadStatus.QUEUED and self.queue_timeout > 0 and monitor.get("queued_since") is not None:
            deadlines.append(monitor["queued_since"] + self.queue_timeout)
        elif task.status not in (DownloadStatus.QUEUED, DownloadStatus.QUEUED_LOCALLY) and self.stall_timeout > 0:
            deadlines.append(monitor.get("last_progress_at", now) + self.stall_timeout)
        remaining = "paused/unknown" if task.poll_failed else f"{max(0, min(deadlines) - now):.0f}s" if deadlines else "disabled"
        logger.info("[Transfer] %s | %s | peer=%s | files=%s/%s bytes=%s/%s (%.1f%%) | last progress %.0fs ago | next timeout %s%s",
                    task.book_title, task.status.value, task.extra.get("username", "?"), progress.get("files_complete", "?"),
                    progress.get("files_total", len(task.extra.get("manifest", []))), task.bytes_transferred,
                    progress.get("bytes_total", "?"), task.progress_percent, elapsed, remaining,
                    " | " + task.error_message if task.error_message else "")

    def _log_batch_status(self, active, completed):
        logger.info("[Batch Monitor] Progress: %s/%s tasks. Active: %s", len(completed), len(active) + len(completed), len(active))
        for task in active:
            self._log_task_progress(task)

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
            except StatePersistenceError:
                raise
            except Exception:
                logger.exception(f"Error resuming {task_data.get('book_title', 'download')}; retaining it for review")
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
