import logging
import datetime
import threading
import time
import os
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Dict, List, TYPE_CHECKING
from . import postprocess

if TYPE_CHECKING:
    from .config import Context

from .display import print_section_header, print_run_summary
from .backends import DownloadTarget, DownloadTask, DownloadStatus
from .media import get_media_mode, resolve_book_media_type, get_formats, EBOOK, BOTH

logger = logging.getLogger(__name__)


def task_to_grab_item(task: DownloadTask, download_dir: str) -> Dict[str, Any]:
    """Convert DownloadTask to legacy grab_list item format."""
    return {
        "author_name": task.author_name,
        "title": task.book_title,
        "bookId": task.book_id,
        "dir": task.local_dir,
        "full_dir": task.extra.get("file_dir", ""),
        "username": task.extra.get("username", ""),
        "source_id": task.extra.get("username", "") or task.extra.get("path", ""),
        "filename": task.filename,
        "files": task.extra.get("files", []),
        "seriesTitle": task.series_title,
        "backend_name": task.backend_name,  # Added to identify source backend
        "media_type": task.extra.get("media_type", "ebook"),
        "expected_files": task.extra.get("expected_files", []),
    }


def build_targets(ctx: "Context", download_targets: List[Dict[str, Any]]) -> List[DownloadTarget]:
    """Turn wanted books into DownloadTargets with the right media type and formats."""
    batch_targets: List[DownloadTarget] = []
    media_mode = get_media_mode(ctx.config)

    missing_media_type = sum(1 for t in download_targets if not t["book"].get("mediaType"))
    if media_mode == BOTH and missing_media_type:
        logger.warning(
            f"media_mode = both, but {missing_media_type} wanted book(s) have no 'mediaType' field (plain Readarr?). "
            f"Treating them as {EBOOK}s; use media_mode = audiobook for an audiobook-only instance."
        )

    for target_dict in download_targets:
        book = target_dict["book"]
        author = target_dict["author"]

        # Ebook or audiobook decides the formats (priority order) and how results are grabbed
        media_type = resolve_book_media_type(book, media_mode)
        filetypes = get_formats(ctx.config, media_type)

        # Fetch editions for this book (contains ISBNs, ASINs, etc.)
        try:
            editions = ctx.readarr.get_edition(book["id"])
        except Exception as e:
            logger.warning(f"Could not get editions for {book['title']}: {e}")
            editions = []

        batch_targets.append(
            DownloadTarget(
                book_id=book["id"],
                book_title=book["title"],
                author_name=author["authorName"],
                series_title=book.get("seriesTitle", ""),
                allowed_filetypes=filetypes,
                readarr_book=book,
                readarr_author=author,
                editions=editions,
                media_type=media_type,
            )
        )
    return batch_targets


class _RunResults:
    """Collects outcomes across the run and handles each finished task.

    Imports run on a single background thread, so the monitoring loop keeps polling the other
    downloads while Readarr/Chaptarr imports one. Counters are guarded by a lock because
    failures are handled on the monitoring thread and imports on the import thread.
    """

    def __init__(self, ctx: "Context", targets: List[DownloadTarget]):
        self.ctx = ctx
        self.targets_by_id = {t.book_id: t for t in targets}
        self.completed_tasks: List[DownloadTask] = []
        self.failed_books: List[tuple] = []  # (author, title) - not found / download failed
        self.failed_imports: List[tuple] = []  # (author, title) - downloaded but import failed
        self.failed_download = 0
        self.remove_wanted_on_failure = ctx.config.getboolean("Search Settings", "remove_wanted_on_failure", fallback=False)
        self.failure_file_path = os.path.join(ctx.config_dir, "failure_list.txt")
        self.slskd_download_dir = ctx.config.get("Slskd", "download_dir", fallback="")
        self.pending_imports: List[tuple] = []  # (author, title) - import not confirmed yet
        self._lock = threading.Lock()
        self._imports = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rsoul-import")
        self._pending: List[Future] = []

    def on_complete(self, task: DownloadTask) -> None:
        if task.status == DownloadStatus.COMPLETED:
            self._forget_transfers(task)
            self._pending.append(self._imports.submit(self._import, task))
            return

        self._handle_failure(task)
        self._forget_transfers(task)
        if self.ctx.state:
            self.ctx.state.remove_task(task.task_id)

    def _forget_transfers(self, task: DownloadTask) -> None:
        """Remove this task's records from the backend (e.g. slskd's transfer list). Files on
        disk are untouched; resume recognises finished files there if R:soul stops mid-import."""
        backend = self.ctx.orchestrator.get_backend(task.backend_name) if self.ctx.orchestrator else None
        if backend:
            try:
                backend.cleanup(task)
            except Exception as e:
                logger.warning(f"Could not clean up backend records for {task.book_title}: {e}")

    def wait_for_imports(self) -> None:
        """Block until every queued import has finished.

        An unexpected error in one import is logged rather than raised, so the remaining imports
        still finish and the run can still save its unfinished downloads for the next run.
        """
        for future in self._pending:
            try:
                future.result()
            except Exception:
                logger.exception("Unexpected error in background import")
        self._imports.shutdown(wait=True)

    def _import(self, task: DownloadTask) -> None:
        try:
            grab_item = task_to_grab_item(task, self.slskd_download_dir)
            imported = postprocess.process_imports(self.ctx, [grab_item]).get(task.book_id, False)
        except Exception as e:
            logger.error(f"Error importing task {task.filename}: {e}")
            imported = False

        if isinstance(imported, dict):
            # Not confirmed yet (still running, or Readarr/Chaptarr unreachable): keep the task in
            # the saved state so the next run follows it up instead of losing the staged files
            task.extra["import_pending"] = imported
            if self.ctx.state:
                self.ctx.state.update_task(task)
            with self._lock:
                self.pending_imports.append((task.author_name, task.book_title))
            logger.info(f"Import of {task.book_title} not confirmed yet; it will be checked on the next run")
            return

        with self._lock:
            if imported:
                self.completed_tasks.append(task)
            else:
                # Not unmonitored even with remove_wanted_on_failure: the download itself worked,
                # and the peer is already recorded in the failure history, so the next search
                # tries other peers for the same book.
                logger.error(f"Downloaded but not imported: {task.book_title} by {task.author_name}")
                self.failed_download += 1
                self.failed_imports.append((task.author_name, task.book_title))

        # Remove from state once the import is done (successful or not)
        if self.ctx.state:
            self.ctx.state.remove_task(task.task_id)

    def _handle_failure(self, task: DownloadTask) -> None:
        # Clear away what the failed download left behind (e.g. finished chapters), so it can't
        # mix with a later attempt that lands in the same local folder
        backend = self.ctx.orchestrator.get_backend(task.backend_name) if self.ctx.orchestrator else None
        if backend:
            try:
                backend.discard(task)
            except Exception as e:
                logger.warning(f"Could not clean up after failed download of {task.book_title}: {e}")

        if self.remove_wanted_on_failure:
            failed_target = self.targets_by_id.get(task.book_id)
            if failed_target:
                book = failed_target.readarr_book
                media_type = failed_target.media_type
            else:
                # Download started in an earlier run (hand-off/resume): fetch the book again
                book = None
                media_type = task.extra.get("media_type", "ebook")
                try:
                    book = self.ctx.readarr.get_book(task.book_id)
                except Exception as e:
                    logger.error(f"Could not fetch book {task.book_id} to unmonitor it: {e}")

            if book:
                logger.error(f"Failed to grab book: {book.get('title', task.book_title)} for author: {task.author_name}." + ' Failed book removed from wanted list and added to "failure_list.txt"')
                unmonitor_book(self.ctx, book, media_type)

                current_datetime_str = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
                failure_string = current_datetime_str + " - " + task.author_name + ", " + book.get("title", task.book_title) + "\n"
                with open(self.failure_file_path, "a") as file:
                    file.write(failure_string)
            else:
                logger.error(f"Failed to grab book: {task.book_title} for author: {task.author_name}")
        else:
            logger.error(f"Failed to grab book: {task.book_title} for author: {task.author_name}")

        with self._lock:
            self.failed_download += 1
            self.failed_books.append((task.author_name, task.book_title))


def unmonitor_book(ctx: "Context", book: Dict[str, Any], media_type: str) -> None:
    """Unmonitor a book in Readarr or Chaptarr.

    Chaptarr tracks monitoring per media type. When a PUT carries its audiobookMonitored /
    ebookMonitored fields, those win over the legacy "monitored" flag, so setting only
    "monitored" would silently change nothing. Clear the flag for this media type as well.
    """
    book["monitored"] = False
    side = "audiobookMonitored" if media_type == "audiobook" else "ebookMonitored"
    if side in book:
        book[side] = False
    try:
        edition = ctx.readarr.get_edition(book["id"])
        ctx.readarr.upd_book(book=book, editions=edition)
    except Exception as e:
        logger.error(f"Failed to unmonitor book: {e}")


def _follow_up_pending_imports(ctx: "Context", results: "_RunResults") -> int:
    """Check imports that an earlier run couldn't confirm. Returns how many are still pending."""
    items = [i for i in (ctx.state.get_tasks_for_orchestrator() if ctx.state else []) if i.get("extra", {}).get("import_pending")]
    still_pending = 0
    for item in items:
        author, title = item.get("author_name", ""), item.get("book_title", "")
        outcome = postprocess.check_pending_import(ctx.readarr, item["extra"]["import_pending"])
        if outcome is True:
            results.completed_tasks.append(DownloadTask(item["task_id"], item.get("backend_name", ""), DownloadStatus.COMPLETED, title, author, item.get("book_id"), item.get("filename", "")))
            ctx.state.remove_task(item["task_id"])
        elif outcome is False:
            results.failed_download += 1
            results.failed_imports.append((author, title))
            ctx.state.remove_task(item["task_id"])
        else:
            if isinstance(outcome, dict):
                ctx.state.set_extra(item["task_id"], dict(item["extra"], import_pending=outcome))
            still_pending += 1
            results.pending_imports.append((author, title))
    return still_pending


def _resume_persisted(ctx: "Context") -> List[DownloadTask]:
    """Reconcile saved downloads with the backends; drop the ones that can no longer be resumed."""
    persisted = ctx.state.get_tasks_for_orchestrator() if ctx.state else []
    # Finished downloads waiting for an import confirmation aren't downloads to resume
    persisted = [i for i in persisted if not i.get("extra", {}).get("import_pending")]
    if not persisted:
        return []

    tasks = ctx.orchestrator.resume_tasks(persisted)
    resumed_ids = {t.task_id for t in tasks}
    unchecked = getattr(ctx.orchestrator, "unchecked_task_ids", set())
    for item in persisted:
        if item["task_id"] in unchecked:
            continue  # couldn't be checked (e.g. slskd down): keep it for the next run
        if item["task_id"] not in resumed_ids:
            logger.warning(f"Dropping download that can no longer be resumed: {item.get('book_title', item['task_id'])}")
            ctx.state.remove_task(item["task_id"])
    return tasks


def run_workflow(ctx: "Context", download_targets: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Main workflow: Search, Monitor, Import, and Cleanup using DownloadOrchestrator.

    Two modes, set by [Download Settings] monitor_window (seconds):
    - 0 (default): resume saved downloads if any, otherwise search; wait until every download finishes.
    - > 0 (hand-off): resume saved downloads AND search for new books in the same run, monitor
      everything in parallel for at most monitor_window seconds, then save the unfinished
      downloads so the next run picks them up. slskd keeps downloading in between.
    """
    if not ctx.orchestrator:
        logger.error("Download Orchestrator not available. Check your backend configuration.")
        return {"failed_download": 0, "grabbed_count": 0}

    monitor_window = ctx.config.getint("Download Settings", "monitor_window", fallback=0)
    active: List[DownloadTask] = []
    unfinished: List[DownloadTask] = []

    results = _RunResults(ctx, [])

    # 0. Follow up imports an earlier run couldn't confirm
    pending_count = _follow_up_pending_imports(ctx, results)

    has_saved = bool(ctx.state and any(not i.get("extra", {}).get("import_pending") for i in ctx.state.get_items()))

    # 1. Resume saved downloads
    resumed: List[DownloadTask] = []
    if has_saved:
        logger.info("Found saved state - attempting to resume previous session")
        print_section_header("RESUMING PREVIOUS SESSION")
        resumed = _resume_persisted(ctx)
        unchecked = getattr(ctx.orchestrator, "unchecked_task_ids", set())
        if resumed:
            logger.info(f"Resuming {len(resumed)} downloads from previous session")
        if unchecked:
            logger.warning(f"{len(unchecked)} saved download(s) couldn't be checked; they are kept for the next run")
        if not resumed and not unchecked and not pending_count:
            logger.info("No items could be resumed - starting fresh")
            ctx.state.clear()

    # 2. Search & enqueue new books (legacy mode only when nothing was resumed)
    targets: List[DownloadTarget] = []
    kept = [i for i in (ctx.state.get_items() if ctx.state else []) if i.get("task_id") in getattr(ctx.orchestrator, "unchecked_task_ids", set())]
    if monitor_window > 0 or not (resumed or kept):
        in_flight = {t.book_id for t in resumed} | {i.get("book_id") for i in kept}
        wanted = [t for t in download_targets if t["book"]["id"] not in in_flight]
        if in_flight:
            logger.info(f"Skipping {len(in_flight)} book(s) that are still downloading from an earlier run")

        # Optional cap on downloads running at once (resumed ones included)
        max_active = ctx.config.getint("Download Settings", "max_active_downloads", fallback=0)
        if max_active > 0:
            running = len(resumed) + len(kept)
            room = max(0, max_active - running)
            if len(wanted) > room:
                logger.info(f"max_active_downloads = {max_active}: {running} running, starting {room} of {len(wanted)} wanted book(s)")
                wanted = wanted[:room]

        targets = build_targets(ctx, wanted)

    results.targets_by_id = {t.book_id: t for t in targets}

    if targets:
        print_section_header("STARTING BATCH SEARCH PHASE")
        active = ctx.orchestrator.start_targets(targets, on_complete=results.on_complete)

    # 3. Monitor everything in parallel (imports run alongside on their own thread)
    deadline = time.time() + monitor_window if monitor_window > 0 else None
    try:
        _, unfinished = ctx.orchestrator.monitor_until(resumed + active, on_complete=results.on_complete, deadline=deadline)
    finally:
        results.wait_for_imports()

    # 4. Hand unfinished downloads to the next run
    for task in unfinished:
        if ctx.state:
            ctx.state.update_task(task)
    if unfinished:
        logger.info(f"{len(unfinished)} download(s) still running in slskd; the next run will continue monitoring them")

    # 5. Final Cleanup
    if ctx.state and not ctx.state.has_pending_state():
        ctx.state.clear()

    # 6. Run Summary
    print_run_summary(
        len(results.completed_tasks),
        results.failed_download,
        results.failed_books if results.failed_books else None,
        results.failed_imports if results.failed_imports else None,
    )
    if results.pending_imports:
        logger.info(f"Imports not confirmed yet ({len(results.pending_imports)}), checked again next run: " + ", ".join(f"{a} — {t}" for a, t in results.pending_imports))

    return {
        "failed_download": results.failed_download,
        "grabbed_count": len(results.completed_tasks),
        "still_running": len(unfinished),
        "pending_imports": len(results.pending_imports),
        "unchecked_downloads": len(getattr(ctx.orchestrator, "unchecked_task_ids", set())),
    }
