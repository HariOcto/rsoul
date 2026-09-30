import logging
import datetime
import os
from typing import Any, Dict, List, TYPE_CHECKING, Optional
from . import postprocess

if TYPE_CHECKING:
    from .config import Context

from .display import print_section_header, print_run_summary
from .backends import DownloadTarget, DownloadTask, DownloadStatus
from .media import get_media_mode, resolve_book_media_type, get_formats

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




def run_workflow(ctx: "Context", download_targets: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Main workflow: Search, Monitor, Import, and Cleanup using DownloadOrchestrator.

    Supports resume functionality - if a saved state exists, it will resume
    monitoring those downloads instead of starting new searches.
    """
    if not ctx.orchestrator:
        logger.error("Download Orchestrator not available. Check your backend configuration.")
        return {"failed_download": 0, "grabbed_count": 0}

    completed_tasks: List[DownloadTask] = []
    failed_books: List[tuple] = []  # (author, title) — not found across all backends
    failed_imports: List[tuple] = []  # (author, title) — downloaded but import failed
    failed_download = 0
    resumed = False

    remove_wanted_on_failure = ctx.config.getboolean("Search Settings", "remove_wanted_on_failure", fallback=False)
    failure_file_path = os.path.join(ctx.config_dir, "failure_list.txt")
    slskd_download_dir = ctx.config.get("Slskd", "download_dir", fallback="")

    # 1. Resume Logic
    if ctx.state and ctx.state.has_pending_state():
        logger.info("Found saved state - attempting to resume previous session")
        print_section_header("RESUMING PREVIOUS SESSION")

        persisted_tasks = ctx.state.get_tasks_for_orchestrator()
        if persisted_tasks:
            tasks = ctx.orchestrator.resume_tasks(persisted_tasks)
            if tasks:
                logger.info(f"Resuming {len(tasks)} downloads from previous session")

                def on_resume_complete(task: DownloadTask) -> None:
                    nonlocal failed_download
                    if task.status == DownloadStatus.COMPLETED:
                        # Trigger import immediately
                        try:
                            grab_item = task_to_grab_item(task, slskd_download_dir)
                            postprocess.process_imports(ctx, [grab_item])
                            completed_tasks.append(task)
                        except Exception as e:
                            logger.error(f"Error importing resumed task {task.filename}: {e}")
                            failed_download += 1
                            failed_imports.append((task.author_name, task.book_title))
                    else:
                        failed_download += 1
                        failed_books.append((task.author_name, task.book_title))

                    # Remove from state regardless of success/failure
                    if ctx.state:
                        ctx.state.remove_task(task.task_id)

                ctx.orchestrator.monitor_resumed_tasks(tasks, on_complete=on_resume_complete)
                resumed = True
            else:
                logger.info("No items could be resumed - starting fresh")
                ctx.state.clear()
        else:
            logger.info("No orchestrator tasks found in state - clearing stale state")
            ctx.state.clear()

    # 2. Search Phase
    if not resumed:
        print_section_header("STARTING BATCH SEARCH PHASE")

        batch_targets: List[DownloadTarget] = []
        media_mode = get_media_mode(ctx.config)

        # Prepare targets
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

            target = DownloadTarget(
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

            batch_targets.append(target)

        # Batch Process
        if batch_targets:

            def on_search_complete(task: DownloadTask) -> None:
                nonlocal failed_download

                if task.status == DownloadStatus.COMPLETED:
                    try:
                        # Success
                        grab_item = task_to_grab_item(task, slskd_download_dir)
                        postprocess.process_imports(ctx, [grab_item])
                        completed_tasks.append(task)
                    except Exception as e:
                        logger.error(f"Error importing task {task.filename}: {e}")
                        failed_download += 1
                        failed_imports.append((task.author_name, task.book_title))
                else:
                    # Failure handling
                    # Simple matching by book_id
                    failed_target = next((t for t in batch_targets if t.book_id == task.book_id), None)

                    if failed_target and remove_wanted_on_failure:
                        book = failed_target.readarr_book
                        author = failed_target.readarr_author

                        logger.error(f"Failed to grab book: {book['title']} for author: {author['authorName']}." + ' Failed book removed from wanted list and added to "failure_list.txt"')
                        book["monitored"] = False
                        try:
                            edition = ctx.readarr.get_edition(book["id"])
                            ctx.readarr.upd_book(book=book, editions=edition)
                        except Exception as e:
                            logger.error(f"Failed to unmonitor book: {e}")

                        current_datetime = datetime.datetime.now()
                        current_datetime_str = current_datetime.strftime("%d/%m/%Y %H:%M:%S")
                        failure_string = current_datetime_str + " - " + author["authorName"] + ", " + book["title"] + "\n"

                        with open(failure_file_path, "a") as file:
                            file.write(failure_string)
                    else:
                        logger.error(f"Failed to grab book: {task.book_title} for author: {task.author_name}")

                    failed_download += 1
                    failed_books.append((task.author_name, task.book_title))

                # Remove from state regardless of success/failure
                if ctx.state:
                    ctx.state.remove_task(task.task_id)

            results = ctx.orchestrator.batch_process_targets(batch_targets, on_complete=on_search_complete)

        # 3. Import Phase (Deprecated - Handled incrementally via callbacks)
        # if completed_tasks:
        #     grab_list = [task_to_grab_item(task, slskd_download_dir) for task in completed_tasks]
        #     postprocess.process_imports(ctx, grab_list)

    # 4. Final Cleanup
    if ctx.state and not ctx.state.has_pending_state():
        ctx.state.clear()

    # Cleanup backend transfers
    if ctx.slskd and ctx.config.getboolean("Backends", "slskd_enabled", fallback=True):
        try:
            ctx.slskd.transfers.remove_completed_downloads()
        except Exception as e:
            logger.warning(f"Failed to cleanup slskd transfers: {e}")

    # 5. Run Summary
    print_run_summary(
        len(completed_tasks),
        failed_download,
        failed_books if failed_books else None,
        failed_imports if failed_imports else None,
    )

    return {"failed_download": failed_download, "grabbed_count": len(completed_tasks)}
