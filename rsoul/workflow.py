import logging
import copy
import datetime
import threading
import time
import os
from collections import deque
from concurrent.futures import Future
from typing import Any, Dict, List, TYPE_CHECKING
from . import health, postprocess

if TYPE_CHECKING:
    from .config import Context

from .display import print_section_header, print_run_summary
from .backends import DownloadTarget, DownloadTask, DownloadStatus
from .media import get_media_mode, resolve_book_media_type, get_formats, EBOOK, AUDIOBOOK, BOTH
from .match import normalize_for_matching, title_key
from .library import record_with_files
from .state import StatePersistenceError
from .outcomes import check_fatal_state

logger = logging.getLogger(__name__)


def task_to_grab_item(task: DownloadTask, download_dir: str) -> Dict[str, Any]:
    """Convert DownloadTask to legacy grab_list item format."""
    return {
        "task_id": task.task_id,
        "_task_extra": copy.deepcopy(task.extra),
        "author_name": task.author_name,
        "title": task.book_title,
        "bookId": task.book_id,
        "dir": task.local_dir,
        "full_dir": task.extra.get("file_dir", ""),
        "username": task.extra.get("username", ""),
        "source_id": task.extra.get("username", "") or task.extra.get("path", ""),
        "filename": task.filename,
        "files": copy.deepcopy(task.extra.get("files", [])),
        "seriesTitle": task.series_title,
        "backend_name": task.backend_name,  # Added to identify source backend
        "media_type": task.extra.get("media_type", "ebook"),
        "expected_files": task.extra.get("expected_files", []),
    }


# How long a peer is left alone for a book after it refused or broke off the download. Long
# enough for the next runs to try other peers, short enough to come back to it.
DOWNLOAD_FAILURE_RETRY_DAYS = 1
# How the backends report a failure on the peer's side (see slskd_backend.get_status)
PEER_FAILURE_PREFIX = "File(s) failed"


def build_targets(ctx: "Context", download_targets: List[Dict[str, Any]], use_ledger: bool = True) -> List[DownloadTarget]:
    """Turn wanted books into DownloadTargets with the right media type and formats.

    Books that should not be searched for right now are left out, each with a log line:
    records of the other media type, duplicates, and (use_ledger) books R:soul imported a
    short while ago or searched for in vain recently."""
    batch_targets: List[DownloadTarget] = []
    ledger = getattr(ctx, "ledger", None) if use_ledger else None
    redownload_days = ctx.config.getfloat("Search Settings", "redownload_after_days", fallback=30)
    not_found_days = ctx.config.getfloat("Search Settings", "not_found_retry_days", fallback=1)
    resting: List[str] = []
    media_mode = get_media_mode(ctx.config)
    skipped = getattr(ctx, "_skipped_wanted_ids", set())
    ctx._skipped_wanted_ids = skipped
    excluded_ids = {s.strip() for s in ctx.config.get("Search Settings", "excluded_book_ids", fallback="").split(",") if s.strip()}
    excluded_titles = {title_key(s) for s in ctx.config.get("Search Settings", "excluded_titles", fallback="").split("|") if s.strip()}
    excluded_types = {s.strip().casefold() for s in ctx.config.get("Search Settings", "excluded_publication_types", fallback="").split(",") if s.strip()}

    missing_media_type = sum(1 for t in download_targets if not t["book"].get("mediaType"))
    if media_mode == BOTH and missing_media_type:
        logger.warning(
            f"media_mode = both, but {missing_media_type} wanted book(s) have no 'mediaType' field (plain Readarr?). "
            f"Treating them as {EBOOK}s; use media_mode = audiobook for an audiobook-only instance."
        )

    # Titles R:soul is already dealing with: downloads saved from earlier runs, and (below)
    # the books accepted so far in this run
    seen = set()
    state = getattr(ctx, "state", None)
    for item in state.get_tasks_for_orchestrator() if state else []:
        saved_media = (item.get("extra") or {}).get("media_type", EBOOK)
        seen.add((normalize_for_matching(item.get("author_name", "")), title_key(item.get("book_title", "")), saved_media))
    saved_ids = {item.get("book_id") for item in state.get_tasks_for_orchestrator()} if state else set()

    for target_dict in download_targets:
        book = target_dict["book"]
        author = target_dict["author"]
        label = f"{author.get('authorName', '?')} - {book.get('title', '?')} (book {book.get('id')})"
        publication_type = str(book.get("publicationType") or book.get("bookType") or "").casefold()
        if str(book.get("id")) in excluded_ids or title_key(book.get("title", "")) in excluded_titles or publication_type in excluded_types:
            logger.info("Skipping %s: explicit catalog exclusion", label)
            skipped.add(book.get("id"))
            continue

        # Ebook or audiobook decides the formats (priority order) and how results are grabbed
        media_type = resolve_book_media_type(book, media_mode)
        filetypes = get_formats(ctx.config, media_type)
        monitored_field = "audiobookMonitored" if media_type == AUDIOBOOK else "ebookMonitored"
        if book.get(monitored_field, book.get("monitored", True)) is False:
            logger.info("Skipping %s: this media type is not monitored", label)
            skipped.add(book.get("id"))
            continue

        # A record of the other media type has no business in an ebook-only or audiobook-only
        # run: the download would be imported for its counterpart and this record stay wanted
        own_type = str(book.get("mediaType") or "").strip().lower()
        if media_mode in (EBOOK, AUDIOBOOK) and own_type in (EBOOK, AUDIOBOOK) and own_type != media_mode:
            logger.warning(f"Skipping {label}: it is an {own_type} record, and media_mode = {media_mode}")
            skipped.add(book.get("id"))
            continue

        # The same author and title twice (Readarr/Chaptarr can hold two records for one book)
        # would be downloaded twice. Books resumed from an earlier run are not new duplicates.
        key = (normalize_for_matching(author.get("authorName", "")), title_key(book.get("title", "")), media_type)
        if book.get("id") not in saved_ids:
            if key in seen:
                logger.warning(f"Skipping {label}: another wanted record with the same author and title is already being handled (downloading, or earlier in this run)")
                skipped.add(book.get("id"))
                continue
            duplicate = record_with_files(ctx, book)
            if duplicate:
                logger.warning(
                    f"Skipping {label}: Chaptarr already has files for another record of this title (book {duplicate['id']}). "
                    "Unmonitor or remove the duplicate record there to stop it showing up as wanted."
                )
                skipped.add(book.get("id"))
                continue
            if ledger:
                # Wanted again although R:soul imported it? Only a book without files counts:
                # one that has files is wanted for a better quality, which is a real request.
                has_files = bool(book.get("hasFiles")) or bool((book.get("statistics") or {}).get("bookFileCount"))
                imported = None if has_files else ledger.imported_recently(book.get("id"), author.get("authorName", ""), book.get("title", ""), media_type, redownload_days)
                if imported:
                    logger.warning(
                        f"Skipping {label}: R:soul downloaded this title and it was imported on {imported:%Y-%m-%d %H:%M}, yet it is wanted again. "
                        "Check the book in Readarr/Chaptarr: the file may be attached to another record. "
                        f"Not downloaded again for {redownload_days:g} days (redownload_after_days); delete its entry in book_ledger.json to retry sooner."
                    )
                    skipped.add(book.get("id"))
                    continue
                if ledger.not_found_recently(book.get("id"), not_found_days, author.get("authorName", ""), book.get("title", ""), media_type):
                    resting.append(f"{author.get('authorName', '?')} - {book.get('title', '?')}")
                    skipped.add(book.get("id"))
                    continue
        seen.add(key)

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
    if resting:
        logger.info(f"Not searching again yet for {len(resting)} book(s) that were not found in the last {not_found_days:g} day(s) (not_found_retry_days): " + "; ".join(resting))
    return batch_targets


class _RunResults:
    """Collects outcomes across the run and handles each finished task.

    One background worker submits queued downloads and checks accepted imports between
    submissions. Waiting for one command never occupies the submission queue. All import
    state changes remain serialized; the foreground can continue monitoring downloads.
    """

    def __init__(self, ctx: "Context", targets: List[DownloadTarget]):
        self.ctx = ctx
        self.targets_by_id = {t.book_id: t for t in targets}
        self.completed_tasks: List[DownloadTask] = []
        self.failed_books: List[tuple] = []  # (author, title) - not found, or not started
        self.failed_downloads: List[tuple] = []  # (author, title, reason) - started, then failed
        self.failed_imports: List[tuple] = []  # (author, title) - downloaded but import failed
        self.failed_download = 0
        self.remove_wanted_on_failure = ctx.config.getboolean("Search Settings", "remove_wanted_on_failure", fallback=False)
        self.failure_file_path = os.path.join(ctx.config_dir, "failure_list.txt")
        self.slskd_download_dir = ctx.config.get("Slskd", "download_dir", fallback="")
        self.pending_imports: List[tuple] = []  # (author, title) - import not confirmed yet
        self._lock = threading.Lock()
        self._import_condition = threading.Condition()
        self._import_queue = deque()
        self._import_thread = None
        self._imports_closed = False
        self._pending: List[Future] = []
        self._handled = set()

    def on_complete(self, task: DownloadTask) -> None:
        check_fatal_state(self.ctx)
        with self._lock:
            if task.task_id in self._handled:
                return
            self._handled.add(task.task_id)
        if self._operator_discard_pending(task):
            with self._lock:
                self.pending_imports.append((task.author_name, task.book_title))
            logger.warning("Operator discard remains journaled for %s; automatic work is paused", task.book_title)
            return
        if task.status == DownloadStatus.COMPLETED:
            # Keep transfer evidence until the durable import transaction is committed.
            if self.ctx.state:
                task.extra["transfer_complete"] = True
                self.ctx.state.update_task(task)
            with self._import_condition:
                if self._imports_closed:
                    raise RuntimeError("Import queue is closed; completed download remains saved")
                future = Future()
                self._pending.append(future)
                self._import_queue.append((task, future))
                if self._import_thread is None:
                    self._import_thread = threading.Thread(target=self._coordinate_imports, name="rsoul-import")
                    self._import_thread.start()
                self._import_condition.notify_all()
            return

        # Transfers first: this cancels chapters that are still running, so none of them can
        # land in the download folder after the leftovers have been moved away
        if not task.extra.get("start_failure"):
            if self.ctx.state:
                task.extra["terminal_failure"] = task.error_message or "Download failed"
                self.ctx.state.update_task(task)
            if not self._forget_transfers(task):
                logger.warning("Backend cleanup unconfirmed for %s; retaining files and failure journal", task.book_title)
                return
        try:
            retired = self._handle_failure(task)
        except StatePersistenceError:
            raise
        else:
            if retired and self.ctx.state:
                self.ctx.state.remove_task(task.task_id)

    def _forget_transfers(self, task: DownloadTask) -> bool:
        """Remove this task's records from the backend (e.g. slskd's transfer list). Files on
        disk are untouched; resume recognises finished files there if R:soul stops mid-import."""
        backend = self.ctx.orchestrator.get_backend(task.backend_name) if self.ctx.orchestrator else None
        if backend:
            try:
                return backend.cleanup(task) is not False
            except StatePersistenceError:
                raise
            except Exception as e:
                logger.warning(f"Could not clean up backend records for {task.book_title}: {e}")
                return False
        return backend is None and not task.extra.get("files")

    def _retire_import(self, task):
        """Keep cleanup separate from the already verified import transaction."""
        if not self.ctx.state:
            return
        if self._forget_transfers(task):
            self.ctx.state.remove_task(task.task_id)
        else:
            saved = next((i for i in self.ctx.state.get_tasks_for_orchestrator() if i["task_id"] == task.task_id), None)
            if saved:
                task.extra = saved["extra"]
            task.extra["cleanup_pending"] = {"outcome": "import_processed", "since": time.time()}
            self.ctx.state.update_task(task)
            logger.warning("Import processed for %s; backend cleanup remains pending", task.book_title)

    def wait_for_imports(self) -> None:
        """Close submissions and drain checks to each operation's original deadline.

        An unexpected error in one import is logged rather than raised, so the remaining imports
        still finish and the run can still save its unfinished downloads for the next run.
        """
        with self._import_condition:
            self._imports_closed = True
            self._import_condition.notify_all()
        if self._import_thread is not None:
            self._import_thread.join()
        error = None
        for future in self._pending:
            try:
                future.result()
            except StatePersistenceError as exc:
                error = exc
                logger.exception("Import checkpoint failed; pending work has been preserved")
            except Exception:
                logger.exception("Unexpected error in background import")
        if error:
            raise error

    def _operator_discard_pending(self, task) -> bool:
        extra = task.extra
        if self.ctx.state:
            saved = next((item for item in self.ctx.state.get_tasks_for_orchestrator()
                          if item.get("task_id") == task.task_id), None)
            if saved:
                extra = saved.get("extra") or extra
        return bool(extra.get("operator_discard") or (extra.get("import_pending") or {}).get("operator_discard"))

    def _coordinate_imports(self) -> None:
        """Serialize submission/check steps; the condition wait wakes for new downloads."""
        active = {}
        while True:
            with self._import_condition:
                if self._import_queue:
                    task, future = self._import_queue.popleft()
                    record = None
                    future.set_running_or_notify_cancel()
                else:
                    now = time.monotonic()
                    ready = next((key for key, value in active.items() if value["next_check"] <= now), None)
                    if ready is not None:
                        record = active.pop(ready)
                        task, future = record["task"], record["future"]
                    elif self._imports_closed and not active:
                        return
                    else:
                        delay = min((max(0, value["next_check"] - now) for value in active.values()), default=2)
                        health.heartbeat()
                        self._import_condition.wait(timeout=min(2, delay))
                        continue
            try:
                check_fatal_state(self.ctx)
                if record is None:
                    pending = self._import_one(task, poll=False)
                    if not isinstance(pending, dict):
                        future.set_result(None)
                        continue
                    now = time.monotonic()
                    timeout = self.ctx.config.getfloat("Readarr", "import_poll_timeout", fallback=600)
                    record = {"task": task, "future": future, "pending": pending,
                              "deadline": now + max(0, timeout), "next_check": now}
                    active[task.task_id] = record
                    continue

                pending = record["pending"]
                if self._operator_discard_pending(task):
                    # A discard journal owns its paths even if task removal failed.
                    with self._lock:
                        self.pending_imports.append((task.author_name, task.book_title))
                    future.set_result(None)
                    continue
                if self.ctx.config.getboolean("Readarr", "disable_sync", fallback=False):
                    self._finish_import(task, pending)
                    future.set_result(None)
                    continue
                try:
                    from .import_transaction import context_writer
                    item = task_to_grab_item(task, self.slskd_download_dir)
                    outcome = postprocess.check_pending_import(self.ctx.readarr, pending, config=self.ctx.config,
                                                               persist=context_writer(self.ctx, item, pending))
                except StatePersistenceError:
                    raise
                except Exception as exc:
                    self._import_interrupted(task, exc)
                    future.set_result(None)
                    continue
                now = time.monotonic()
                if isinstance(outcome, dict) and outcome.get("phase") == "importing" and now < record["deadline"]:
                    record.update(pending=outcome, next_check=min(record["deadline"], now + 2))
                    active[task.task_id] = record
                else:
                    self._finish_import(task, outcome)
                    future.set_result(None)
            except StatePersistenceError as exc:
                # No finalization writes after durability fails. Queued downloads already
                # have transfer_complete; active imports retain their last durable journal.
                self.ctx.fatal_state_error = exc
                with self._import_condition:
                    self._imports_closed = True
                    self._import_queue.clear()
                    for pending_future in self._pending:
                        if not pending_future.done():
                            pending_future.set_exception(exc)
                    self._import_condition.notify_all()
                return
            except Exception as exc:
                future.set_exception(exc)

    def _import(self, task: DownloadTask) -> None:
        try:
            check_fatal_state(self.ctx)
            self._import_one(task)
        except StatePersistenceError as exc:
            # Observed by the foreground search/transfer loop before its next
            # mutation, not just when it joins this worker at the end of a run.
            self.ctx.fatal_state_error = exc
            raise

    def _import_one(self, task: DownloadTask, *, poll=True):
        if self._operator_discard_pending(task):
            with self._lock:
                self.pending_imports.append((task.author_name, task.book_title))
            return None
        try:
            grab_item = task_to_grab_item(task, self.slskd_download_dir)
            imported = postprocess.process_imports(self.ctx, [grab_item], poll=poll).get(task.book_id, False)
        except StatePersistenceError:
            raise
        except Exception as e:
            self._import_interrupted(task, e)
            return

        if not poll and isinstance(imported, dict) and imported.get("phase") in ("importing", "submission_unknown"):
            return imported
        self._finish_import(task, imported)
        return None

    def _import_interrupted(self, task, error):
        logger.error("Error importing task %s: %s", task.filename, error)
        # Never erase a completed download because local processing or an API call
        # raised. A durable transaction, if created, remains authoritative.
        saved = next((i for i in self.ctx.state.get_tasks_for_orchestrator() if i["task_id"] == task.task_id), None) if self.ctx.state else None
        if saved:
            task.extra = saved.get("extra", task.extra)
        if not task.extra.get("import_pending"):
            task.extra["recovery_review"] = {"reason": f"Import interrupted: {error}", "since": time.time()}
            if self.ctx.state:
                self.ctx.state.update_task(task)
        with self._lock:
            self.pending_imports.append((task.author_name, task.book_title))

    def _finish_import(self, task, imported):
        check_fatal_state(self.ctx)

        if isinstance(imported, dict):
            # Not confirmed yet (still running, or Readarr/Chaptarr unreachable): keep the task in
            # the saved state so the next run follows it up instead of losing the staged files
            saved = next((i for i in self.ctx.state.get_tasks_for_orchestrator() if i["task_id"] == task.task_id), None) if self.ctx.state else None
            if saved:
                task.extra = saved.get("extra", task.extra)
            task.extra["import_pending"] = copy.deepcopy(imported)
            if self.ctx.state:
                self.ctx.state.update_task(task)
            with self._lock:
                self.pending_imports.append((task.author_name, task.book_title))
            logger.info(f"Import of {task.book_title} not confirmed yet; it will be checked on the next run")
            return

        if imported and getattr(self.ctx, "ledger", None) and not self.ctx.config.getboolean("Readarr", "disable_sync", fallback=False):
            self.ctx.ledger.note_imported(task.book_id, task.author_name, task.book_title, task.extra.get("media_type", EBOOK))

        with self._lock:
            if imported:
                self.completed_tasks.append(task)
            else:
                # The download itself worked: an import failure does not unmonitor
                # the book or establish a source fault that should ban its peer.
                logger.error(f"Downloaded but not imported: {task.book_title} by {task.author_name}")
                self.failed_download += 1
                self.failed_imports.append((task.author_name, task.book_title))

        # Backend cleanup follows verification or a confirmed content rejection; it
        # cannot remove the only evidence needed to recover an unknown submission.
        self._retire_import(task)

    def _handle_failure(self, task: DownloadTask) -> bool:
        # Clear away what the failed download left behind (e.g. finished chapters), so it can't
        # mix with a later attempt that lands in the same local folder
        backend = self.ctx.orchestrator.get_backend(task.backend_name) if self.ctx.orchestrator else None
        if backend and not task.extra.get("start_failure"):
            try:
                discarded = backend.discard(task) is not False
            except StatePersistenceError:
                raise
            except Exception as e:
                logger.warning(f"Could not clean up after failed download of {task.book_title}: {e}")
                discarded = False
            if not discarded:
                task.extra["cleanup_pending"] = {
                    "outcome": "failed_download", "since": time.time(),
                    "reason": "Local failed-download cleanup is unconfirmed",
                }
                if self.ctx.state:
                    self.ctx.state.update_task(task)
                logger.warning("Retaining failed-download ownership for %s until local cleanup is confirmed", task.book_title)
                return False

        # A book nobody could be asked about (slskd unreachable, a setting that breaks every
        # search), or one that was found but couldn't be queued (its local folder is in use),
        # hasn't failed for good: it stays wanted
        start_failure = task.extra.get("start_failure")
        ledger = getattr(self.ctx, "ledger", None)
        if ledger and start_failure == "not_found":
            media_type = task.extra.get("media_type") or (
                self.targets_by_id[task.book_id].media_type if task.book_id in self.targets_by_id else get_media_mode(self.ctx.config)
            )
            if media_type in (EBOOK, AUDIOBOOK):
                ledger.note_not_found(task.book_id, task.author_name, task.book_title, media_type)
        elif ledger and not start_failure:
            ledger.note_found(task.book_id)

        # A download the peer itself refused or broke off ("Completed, Rejected", "Errored"...):
        # leave that peer alone for this book for a while, so the next run picks another one
        # instead of the same top candidate again. A download R:soul gave up on (stalled, too
        # long in a queue) is different: the peer may just have been busy, and is tried again.
        peer = task.extra.get("username")
        refused = (task.error_message or "").startswith(PEER_FAILURE_PREFIX)
        if not start_failure and refused and peer and self.ctx.history:
            self.ctx.history.add_failure(peer, task.book_title, f"Download failed: {task.error_message or 'unknown reason'}", retry_after_days=DOWNLOAD_FAILURE_RETRY_DAYS,
                backend=task.backend_name, author=task.author_name, media_type=task.extra.get("media_type", EBOOK),
                release_id=task.extra.get("release_id") or task.extra.get("source_id") or task.extra.get("file_dir") or task.filename, failure_kind="peer_rejected")

        if self.remove_wanted_on_failure and start_failure not in (None, "not_found"):
            logger.error(f"Could not {'search for' if start_failure == 'not_searched' else 'start'} {task.book_title} by {task.author_name}; it stays on the wanted list")
        elif self.remove_wanted_on_failure:
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
                try:
                    with open(self.failure_file_path, "a") as file:
                        file.write(failure_string)
                except OSError as e:
                    logger.warning(f"Could not write to {self.failure_file_path}: {e}")
            else:
                logger.error(f"Failed to grab book: {task.book_title} for author: {task.author_name}")
        else:
            logger.error(f"Failed to grab book: {task.book_title} for author: {task.author_name}")

        with self._lock:
            self.failed_download += 1
            if start_failure:
                self.failed_books.append((task.author_name, task.book_title))
            else:
                self.failed_downloads.append((task.author_name, task.book_title, task.error_message or ""))
        return True


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


def _saved_task(item, status=DownloadStatus.PENDING):
    return DownloadTask(
        task_id=item["task_id"], backend_name=item.get("backend_name", "slskd"), status=status,
        book_title=item.get("book_title", ""), author_name=item.get("author_name", ""),
        book_id=item.get("book_id", 0), filename=item.get("filename", ""),
        local_dir=item.get("local_dir", ""), series_title=item.get("series_title", ""),
        extra=copy.deepcopy(item.get("extra") or {}),
    )


def _follow_up_pending_imports(ctx: "Context", results: "_RunResults") -> int:
    """Resume journaled transactions without treating uncertainty as a peer fault."""
    items = [i for i in (ctx.state.get_tasks_for_orchestrator() if ctx.state else []) if i.get("extra", {}).get("import_pending") and not i.get("extra", {}).get("cleanup_pending")]
    still_pending = 0
    disabled = ctx.config.getboolean("Readarr", "disable_sync", fallback=False)
    for item in items:
        author, title = item.get("author_name", ""), item.get("book_title", "")
        if item["extra"].get("operator_discard") or item["extra"]["import_pending"].get("operator_discard"):
            still_pending += 1
            results.pending_imports.append((author, title))
            logger.warning("Operator discard remains journaled for %s; automatic follow-up is paused", title)
            continue
        item["extra"]["import_pending"].setdefault("intended_identity", {
            "book_id": item.get("book_id"), "title": title, "author_name": author,
            "media_type": item.get("extra", {}).get("media_type", EBOOK),
        })
        item["extra"]["import_pending"].setdefault("source_task", {k: copy.deepcopy(v) for k, v in item.items() if k != "extra"})
        def persist(pending):
            item["extra"]["import_pending"] = copy.deepcopy(pending)
            ctx.state.set_extra(item["task_id"], item["extra"])
        if disabled:
            outcome = item["extra"]["import_pending"]
            logger.info("Import follow-up paused by disable_sync: %s", title)
        else:
            try:
                outcome = postprocess.check_pending_import(ctx.readarr, item["extra"]["import_pending"], config=ctx.config, persist=persist)
            except StatePersistenceError:
                raise
            except Exception as exc:
                logger.warning("Could not check import of %s; retaining its journal: %s", title, exc)
                outcome = item["extra"]["import_pending"]
        if outcome is True:
            task = _saved_task(item, DownloadStatus.COMPLETED)
            if getattr(ctx, "ledger", None):
                ctx.ledger.note_imported(item.get("book_id"), author, title, item.get("extra", {}).get("media_type", EBOOK))
            results.completed_tasks.append(task)
            results._retire_import(task)
        elif outcome is False:
            results.failed_download += 1
            results.failed_imports.append((author, title))
            results._retire_import(_saved_task(item, DownloadStatus.FAILED))
        else:
            if isinstance(outcome, dict):
                persist(outcome)
                logger.info("Import state for %s: %s (%s)", title, outcome.get("phase", "pending"), outcome.get("reason", outcome.get("last_error", "awaiting evidence")))
            still_pending += 1
            results.pending_imports.append((author, title))
    return still_pending


def _resume_persisted(ctx: "Context", results=None) -> List[DownloadTask]:
    """Reconcile downloads and retain ambiguous legacy/restart state for review."""
    persisted = ctx.state.get_tasks_for_orchestrator() if ctx.state else []
    # An alternate attempt may already own these filenames. This tombstone was
    # committed only after both remote and local cleanup were confirmed; retire
    # it without ever inspecting or moving the replacement's files.
    retained_discards = [i for i in persisted if i.get("extra", {}).get("operator_discard")]
    persisted = [i for i in persisted if not i.get("extra", {}).get("operator_discard")]
    retired = [i for i in persisted if i.get("extra", {}).get("retry_retired")]
    for item in retired:
        ctx.state.remove_task(item["task_id"])
    persisted = [i for i in persisted if not i.get("extra", {}).get("retry_retired")]
    persisted = [i for i in persisted if not i.get("extra", {}).get("import_pending") and not i.get("extra", {}).get("cleanup_pending")]
    review = [i for i in persisted if i.get("extra", {}).get("recovery_review")]
    candidates = [i for i in persisted if not i.get("extra", {}).get("recovery_review")]
    failures = [i for i in candidates if i.get("extra", {}).get("terminal_failure")]
    candidates = [i for i in candidates if not i.get("extra", {}).get("terminal_failure")]
    if results:
        for item in failures:
            task = _saved_task(item, DownloadStatus.FAILED)
            task.error_message = item["extra"]["terminal_failure"]
            results.on_complete(task)
    tasks = ctx.orchestrator.resume_tasks(candidates) if candidates else []
    resumed_ids = {t.task_id for t in tasks}
    unchecked = set(getattr(ctx.orchestrator, "unchecked_task_ids", set()))
    unchecked.update(i["task_id"] for i in retained_discards)
    unchecked.update(i["task_id"] for i in review)
    for item in candidates:
        if item["task_id"] in unchecked or item["task_id"] in resumed_ids:
            continue
        reason = "Saved download cannot be reconciled; its files and transfer evidence need review"
        logger.warning("Keeping %s: %s", item.get("book_title", item["task_id"]), reason)
        preserve = getattr(postprocess, "preserve_unresumable_task", None)
        if preserve:
            pending = preserve(ctx, item, reason)
            if isinstance(pending, dict):
                item["extra"]["import_pending"] = pending
        else:
            item["extra"]["recovery_review"] = {"reason": reason, "since": time.time()}
        ctx.state.set_extra(item["task_id"], item["extra"])
        unchecked.add(item["task_id"])
    ctx.orchestrator.unchecked_task_ids = unchecked
    return tasks



def run_workflow(ctx: "Context", download_targets: List[Dict[str, Any]]) -> Dict[str, int]:
    """Search eligible work, reconcile durable operations, and import verified sets."""
    if not ctx.orchestrator:
        logger.error("Download Orchestrator not available. Check your backend configuration.")
        return {"failed_download": 0, "grabbed_count": 0}

    monitor_window = ctx.config.getint("Download Settings", "monitor_window", fallback=0)
    max_active = ctx.config.getint("Download Settings", "max_active_downloads", fallback=0)
    batch_limit = ctx.config.getint("Search Settings", "number_of_books_to_grab", fallback=10)
    active, unfinished, resumed = [], [], []
    results = _RunResults(ctx, [])
    ctx._skipped_wanted_ids = set()
    queue = getattr(ctx, "wanted_queue", None)
    try:
        if ctx.state:
            ctx.state.migrate_legacy_tasks()
        recover = getattr(postprocess, "recover_pending_imports", None)
        if recover:
            recover(ctx)
        for saved_item in ctx.state.get_tasks_for_orchestrator() if ctx.state else []:
            if saved_item.get("extra", {}).get("operator_discard"):
                continue
            if saved_item.get("extra", {}).get("cleanup_pending"):
                failure = saved_item["extra"]["cleanup_pending"].get("outcome") == "failed_download"
                task = _saved_task(saved_item, DownloadStatus.FAILED if failure else DownloadStatus.COMPLETED)
                if failure:
                    task.error_message = task.extra.get("terminal_failure", "Download failed")
                    results.on_complete(task)
                elif results._forget_transfers(task):
                    ctx.state.remove_task(task.task_id)
        _follow_up_pending_imports(ctx, results)

        saved = ctx.state.get_tasks_for_orchestrator() if ctx.state else []
        if any(not i.get("extra", {}).get("import_pending") for i in saved):
            print_section_header("RESUMING PREVIOUS SESSION")
            resumed = _resume_persisted(ctx, results)
            if resumed:
                logger.info("Resuming %s downloads from previous session", len(resumed))

        saved = ctx.state.get_tasks_for_orchestrator() if ctx.state else []
        unchecked_ids = set(getattr(ctx.orchestrator, "unchecked_task_ids", set()))
        kept = [i for i in saved if i["task_id"] in unchecked_ids]
        if kept:
            logger.warning("%s saved download(s) could not be verified and remain protected", len(kept))
        in_flight = {i.get("book_id") for i in saved}
        if queue:
            queue.acknowledge(in_flight)

        targets = []
        if monitor_window > 0 or not (resumed or kept):
            wanted = [t for t in download_targets if t["book"]["id"] not in in_flight]
            eligible = build_targets(ctx, wanted)
            # Eligibility comes first: duplicate records, cooldowns and exclusions do
            # not waste available slots or the per-run search budget.
            targets = eligible[:batch_limit]
            logger.info("Eligible wanted books: %s; attempting up to %s this run", len(eligible), len(targets))
            if queue:
                queue.acknowledge(ctx._skipped_wanted_ids)

        results.targets_by_id = {t.book_id: t for t in targets}
        if targets:
            print_section_header("STARTING BATCH SEARCH PHASE")
            if getattr(ctx.orchestrator, "supports_interleaved_search", False):
                # Unchecked transfers still own capacity. A pending import does not
                # occupy a transfer slot, but its book remains excluded above.
                available_cap = max(0, max_active - len(kept)) if max_active else 0
                if max_active and available_cap == 0:
                    logger.info("All download slots are owned by unchecked transfers; new work stays queued")
                else:
                    active = ctx.orchestrator.start_targets(targets, on_complete=results.on_complete,
                        existing_tasks=resumed, max_active=available_cap)
                    if queue:
                        queue.acknowledge(getattr(ctx.orchestrator, "attempted_book_ids", set()))
            else:
                # Compatibility for alternate orchestrators using the original API.
                room = max(0, max_active - len(resumed) - len(kept)) if max_active else len(targets)
                selected = targets[:room]
                if selected:
                    active = ctx.orchestrator.start_targets(selected, on_complete=results.on_complete)
                    if queue:
                        queue.acknowledge(t.book_id for t in selected)

        # Tasks already finished during interleaved searching must not be imported
        # twice. The completion handler also provides idempotence as a second guard.
        monitor = [t for t in resumed + active if t.task_id not in results._handled]
        deadline = time.time() + monitor_window if monitor_window > 0 else None
        _, unfinished = ctx.orchestrator.monitor_until(monitor, on_complete=results.on_complete, deadline=deadline)
    except StatePersistenceError as exc:
        ctx.fatal_state_error = exc
        raise
    finally:
        results.wait_for_imports()

    for task in unfinished:
        if ctx.state:
            ctx.state.update_task(task)
    if unfinished:
        logger.info("%s unfinished download(s) saved for the next run", len(unfinished))
    if ctx.state and not ctx.state.has_pending_state():
        ctx.state.clear()

    print_run_summary(len(results.completed_tasks), results.failed_download,
        results.failed_books or None, results.failed_imports or None, results.failed_downloads or None)
    remaining = ctx.state.get_tasks_for_orchestrator() if ctx.state else []
    pending = [i for i in remaining if i.get("extra", {}).get("import_pending") and not i.get("extra", {}).get("cleanup_pending")]
    needs_review = [i for i in remaining if i.get("extra", {}).get("recovery_review") or i.get("extra", {}).get("needs_review") or
        i.get("extra", {}).get("import_pending", {}).get("phase") in ("needs_review", "partial", "identity_changed")]
    for item in needs_review:
        pending_info = item.get("extra", {}).get("import_pending") or item.get("extra", {}).get("recovery_review") or {}
        logger.warning("Needs review: %s — %s; %s", item.get("author_name"), item.get("book_title"),
            pending_info.get("reason", pending_info.get("last_error", "completion could not be proved")))
    if pending:
        logger.info("Imports awaiting confirmation: %s (including %s needing review)", len(pending), len(needs_review))
    return {
        "failed_download": results.failed_download, "grabbed_count": len(results.completed_tasks),
        "still_running": len(unfinished), "pending_imports": len(pending),
        "needs_review": len(needs_review), "unchecked_downloads": len(getattr(ctx.orchestrator, "unchecked_task_ids", set())),
        "pending_cleanup": sum(bool(i.get("extra", {}).get("cleanup_pending")) for i in remaining),
    }
