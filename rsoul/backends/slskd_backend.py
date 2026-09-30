import logging
import os
import time
from typing import List, Optional, Any, Dict, Tuple, TYPE_CHECKING
from pathlib import Path

from .base import (
    DownloadBackend,
    SearchResult,
    DownloadTask,
    DownloadTarget,
    DownloadStatus,
)
from . import register_backend

if TYPE_CHECKING:
    from ..config import Context

from ..download import slskd_do_enqueue, slskd_download_status, downloads_all_done
from ..match import book_match, verify_filetype, audiobook_folder_match, split_slskd_path
from ..display import print_search_summary
from ..postprocess import move_files_aside

logger = logging.getLogger(__name__)


def _generate_fallback_queries(author_name: str, book_title: str, max_fallbacks: int) -> List[str]:
    """Generate progressively degraded search queries for blocked word workaround.

    Strategy:
    1. First name + full title
    2. First name + title with word 0 dropped
    3. First name + title with word 1 dropped
    ...continues left-to-right until max_fallbacks reached
    """
    queries = []
    first_name = author_name.split()[0] if author_name else author_name
    title_words = book_title.split()

    # Step 1: First name + full title
    queries.append(f"{first_name} - {book_title}")

    # Steps 2+: Drop words left-to-right
    for i in range(min(len(title_words), max_fallbacks - 1)):
        remaining = " ".join(title_words[:i] + title_words[i + 1:])
        if remaining:
            queries.append(f"{first_name} - {remaining}")

    return queries[:max_fallbacks]


def _execute_search(ctx: "Context", query: str, search_type: str) -> Tuple[List[Dict[str, Any]], str]:
    """Execute a single SLSKD search and return results.

    Args:
        ctx: Application context
        query: Search query string
        search_type: Label for display (e.g., "main", "fallback-1")

    Returns:
        Tuple of (search results list, search ID for cleanup)
    """
    print_search_summary(query, 0, search_type, "searching")

    search = ctx.slskd.searches.search_text(
        searchText=query,
        searchTimeout=ctx.config.getint("Search Settings", "search_timeout", fallback=5000),
        filterResponses=True,
        maximumPeerQueueLength=ctx.config.getint("Search Settings", "maximum_peer_queue", fallback=50),
        minimumPeerUploadSpeed=ctx.config.getint("Search Settings", "minimum_peer_upload_speed", fallback=0),
    )

    time.sleep(10)

    while ctx.slskd.searches.state(search["id"], False)["state"] == "InProgress":
        time.sleep(1)

    results = ctx.slskd.searches.search_responses(search["id"])
    print_search_summary(query, len(results), search_type, "completed")

    return results, search["id"]


@register_backend("slskd")
class SlskdBackend(DownloadBackend):
    """slskd implementation of DownloadBackend."""

    def __init__(self, ctx: "Context"):
        self.ctx = ctx
        self.client = ctx.slskd
        self.config = ctx.config

    @property
    def name(self) -> str:
        return "slskd"

    @property
    def priority(self) -> int:
        return self.config.getint("Backends", "slskd_priority", fallback=10)

    @property
    def download_dir(self) -> str:
        """Base directory where slskd saves files."""
        return self.config.get("Slskd", "download_dir", fallback="")

    @property
    def readarr_download_dir(self) -> str:
        """Directory path as seen by Readarr (mapped path)."""
        return self.config.get("Slskd", "readarr_download_dir", fallback=self.download_dir)

    def is_available(self) -> bool:
        """Check if slskd is configured and reachable."""
        if not self.config.has_section("Slskd"):
            return False
        try:
            # Simple check to see if client is reachable
            self.client.options.get()
            return True
        except Exception:
            return False

    def search(self, target: DownloadTarget) -> List[SearchResult]:
        """Search for a book on Soulseek via slskd."""
        author_name = target.author_name
        book_title = target.book_title

        delete_searches = self.config.getboolean("Slskd", "delete_searches", fallback=True)

        # Build queries
        queries = []
        queries.append(f"{author_name} - {book_title}")

        if ":" in book_title:
            main_title = book_title.split(":")[0].strip()
            queries.append(f"{author_name} - {main_title}")

        max_fallbacks = self.config.getint("Search Settings", "max_search_fallbacks", fallback=5)
        queries.extend(_generate_fallback_queries(author_name, book_title, max_fallbacks))

        all_results: List[SearchResult] = []
        seen_queries = set()

        # Prepare target dict for book_match
        target_dict = {
            "book": target.readarr_book or {"title": target.book_title, "id": target.book_id, "seriesTitle": target.series_title},
            "author": target.readarr_author or {"authorName": target.author_name},
        }

        for i, query in enumerate(queries):
            if query in seen_queries:
                continue
            seen_queries.add(query)

            search_label = "main" if i == 0 else f"fallback-{i}"
            try:
                search_results, search_id = _execute_search(self.ctx, query, search_label)
            except Exception as e:
                logger.error(f"Search failed for query '{query}': {e}")
                continue

            if delete_searches:
                try:
                    self.client.searches.delete(search_id)
                except Exception:
                    pass

            if search_results:
                if target.is_audiobook:
                    all_results.extend(self._match_audiobook_results(target, target_dict, search_results))
                else:
                    all_results.extend(self._match_ebook_results(target, target_dict, search_results))

                # If we found any matches for this query, stop searching further queries
                if all_results:
                    break

        return all_results

    def _match_thresholds(self) -> Dict[str, Any]:
        """Matching thresholds shared by ebook and audiobook matching."""
        return dict(
            ignored_users=self.config.get("Search Settings", "ignored_users", fallback="").split(","),
            minimum_match_ratio=self.config.getfloat("Search Settings", "minimum_filename_match_ratio", fallback=0.5),
            min_length_ratio=self.config.getfloat("Search Settings", "min_length_ratio", fallback=0.4),
            min_jaccard_ratio=self.config.getfloat("Search Settings", "min_jaccard_ratio", fallback=0.25),
            min_word_overlap=self.config.getint("Search Settings", "min_word_overlap", fallback=2),
            min_title_jaccard=self.config.getfloat("Search Settings", "min_title_jaccard", fallback=0.3),
            min_author_jaccard=self.config.getfloat("Search Settings", "min_author_jaccard", fallback=0.5),
        )

    def _match_ebook_results(self, target: DownloadTarget, target_dict: Dict[str, Any], search_results: List[Dict[str, Any]]) -> List[SearchResult]:
        """Single-file matching (original R:soul behaviour): best file per user and format."""
        book_title = target.book_title
        allowed_filetypes = target.allowed_filetypes
        thresholds = self._match_thresholds()
        results: List[SearchResult] = []

        file_cache: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        for result in search_results:
            username = result["username"]
            # Skip if in history (failed previously)
            if self.ctx.history and self.ctx.history.is_failed(username, book_title):
                continue

            if username not in file_cache:
                file_cache[username] = {}

            for file in result["files"]:
                for ext in allowed_filetypes:
                    if verify_filetype(file, ext):
                        if ext not in file_cache[username]:
                            file_cache[username][ext] = []
                        file_cache[username][ext].append(file)

        # Match for each user
        for username, types in file_cache.items():
            for ext, files in types.items():
                match = book_match(target_dict, files, username, ext, **thresholds)

                if match:
                    file_dir = match["filename"].rsplit("\\", 1)[0] if "\\" in match["filename"] else ""
                    filename = match["filename"].split("\\")[-1]

                    sr = SearchResult(
                        title=target.book_title,
                        author=target.author_name,
                        filename=filename,
                        size_bytes=match["size"],
                        extension=ext,
                        backend_name=self.name,
                        source_id=f"{username}|{match['filename']}",
                        username=username,
                        extra={
                            "username": username,
                            "file_dir": file_dir,
                            "files": [match],
                        },
                    )
                    results.append(sr)

        return results

    def _browse_directory(self, username: str, directory: str) -> Optional[List[Dict[str, Any]]]:
        """Fetch the full listing of a remote folder.

        Search responses only contain the files that matched the query, which for
        a folder of chapters is not guaranteed to be all of them. Returns files with
        full remote paths, or None if browsing fails.
        """
        try:
            listing = self.client.users.directory(username=username, directory=directory)
        except Exception as e:
            logger.warning(f"Could not browse folder '{directory}' from {username}: {e}")
            return None

        # slskd > 0.22.2 returns a list of directories ({"name", "fileCount", "files"}); older
        # versions return a single directory object
        if isinstance(listing, list):
            listing = next((d for d in listing if d.get("name") == directory), listing[0] if listing else None)
        if not isinstance(listing, dict):
            return None

        files = []
        for f in listing.get("files", []):
            name = f.get("filename", "")
            full = name if "\\" in name else f"{directory}\\{name}"
            files.append({"filename": full, "size": f.get("size", 0)})
        return files

    def _match_audiobook_results(self, target: DownloadTarget, target_dict: Dict[str, Any], search_results: List[Dict[str, Any]]) -> List[SearchResult]:
        """Folder matching for audiobooks: pick the best-matching folder and grab every audio file in it."""
        book_title = target.book_title
        thresholds = self._match_thresholds()
        min_size_bytes = int(self.config.getfloat("Search Settings", "audiobook_min_size_mb", fallback=10) * 1024 * 1024)
        browse_limit = self.config.getint("Search Settings", "audiobook_browse_limit", fallback=5)
        allowed = [ext.split(" ")[0].lower() for ext in target.allowed_filetypes]

        candidates = []
        for result in search_results:
            username = result["username"]
            if self.ctx.history and self.ctx.history.is_failed(username, book_title):
                continue
            for match in audiobook_folder_match(target_dict, result.get("files", []), username, target.allowed_filetypes, **thresholds):
                candidates.append((username, match))

        # Best name match first, then the configured format order, then the largest folder
        candidates.sort(key=lambda c: (-c[1]["score"], allowed.index(c[1]["extension"]), -c[1]["total_size"]))

        # Browse candidates in order and stop at the first usable folder: the orchestrator only
        # downloads the first result, and each browse is a slow round trip to the peer.
        for username, match in candidates[:browse_limit]:
            directory = match["directory"]
            ext = match["extension"]

            browsed = self._browse_directory(username, directory) if directory else None
            if browsed is None:
                # Search results list only the files that matched the query, so without the full
                # listing we can't know the folder is complete. Try the next candidate instead.
                logger.info(f"Skipping {directory or '(share root)'} from {username}: could not list the full folder")
                continue

            files = [
                {"filename": f["filename"], "size": f.get("size", 0)}
                for f in browsed
                if split_slskd_path(f["filename"])[1].lower().endswith(f".{ext}")
                # Only this folder's own files, not ones from subfolders
                and split_slskd_path(f["filename"])[0] == directory
            ]
            if not files:
                continue

            files.sort(key=lambda f: f["filename"].lower())
            total_size = sum(f["size"] for f in files)
            if total_size < min_size_bytes:
                logger.info(f"Skipping {directory} from {username}: {total_size / 1048576:.1f} MB is below audiobook_min_size_mb")
                continue

            logger.info(f"Audiobook candidate: {directory} ({len(files)} {ext} files, {total_size / 1048576:.0f} MB) from {username}")
            return [
                SearchResult(
                    title=target.book_title,
                    author=target.author_name,
                    filename=split_slskd_path(files[0]["filename"])[1],
                    size_bytes=total_size,
                    extension=ext,
                    backend_name=self.name,
                    source_id=f"{username}|{directory}",
                    score=match["score"],
                    username=username,
                    extra={
                        "username": username,
                        "file_dir": directory,
                        "files": files,
                        "media_type": "audiobook",
                    },
                )
            ]

        return []

    def download(self, target: DownloadTarget, result: SearchResult) -> Optional[DownloadTask]:
        """Initiate download of a Soulseek file."""
        username = result.extra["username"]
        files = result.extra["files"]
        file_dir = result.extra["file_dir"]

        # Ensure full paths for files before enqueuing
        for i in range(len(files)):
            if "\\" not in files[i]["filename"]:
                files[i]["filename"] = file_dir + "\\" + files[i]["filename"]

        # slskd saves into <download_dir>/<remote folder name>/ and renames a new file whose name
        # is already taken, so R:soul would then pick up the wrong (old or other) file.
        clashes = self._local_clashes(file_dir, files)
        if clashes:
            logger.warning(
                f"Not downloading {target.book_title} from {username}: the local folder "
                f"'{file_dir.split(chr(92))[-1]}' already has or expects {len(clashes)} file(s) with the same "
                f"name (e.g. {clashes[0]}). Clear it out, or wait for the other download to finish."
            )
            return None

        downloads = slskd_do_enqueue(self.client, username, files, file_dir)

        if not downloads:
            logger.warning(f"Failed to enqueue download for {target.book_title} from {username}")
            return None

        is_audiobook = result.extra.get("media_type") == "audiobook"
        if is_audiobook and len(downloads) < len(files):
            # An audiobook with missing chapters is worse than none: cancel and let the run report a failure
            logger.warning(f"Only {len(downloads)} of {len(files)} audiobook files were enqueued from {username} - cancelling")
            for d in downloads:
                try:
                    self.client.transfers.cancel_download(username=username, id=d["id"], remove=True)
                except Exception:
                    pass
            return None

        # Log what was enqueued
        short_filename = result.filename.split("\\")[-1] if "\\" in result.filename else result.filename
        logger.info(f"Enqueued: {short_filename} from {username}")

        # local_dir should be the flattened directory name (bottom-most folder)
        local_dir = file_dir.split("\\")[-1] if file_dir else ""

        # Use (username, filename) as task_id; audiobooks are one task per folder
        task_id = f"{username}|{file_dir}" if is_audiobook else f"{username}|{result.filename}"

        extra = {"username": username, "file_dir": file_dir, "files": downloads, "slskd_id": downloads[0]["id"] if downloads else None}
        if is_audiobook:
            extra["media_type"] = "audiobook"
            extra["expected_files"] = [f["filename"].split("\\")[-1] for f in files]
            logger.info(f"Enqueued audiobook folder: {local_dir} ({len(downloads)} files) from {username}")

        return DownloadTask(
            task_id=task_id,
            backend_name=self.name,
            status=DownloadStatus.QUEUED,
            book_title=target.book_title,
            author_name=target.author_name,
            book_id=target.book_id,
            filename=result.filename,
            series_title=target.series_title,
            local_dir=local_dir,
            extra=extra,
        )

    def get_status(self, task: DownloadTask) -> DownloadTask:
        """Poll slskd for transfer status."""
        downloads = task.extra.get("files", [])
        if not downloads:
            task.status = DownloadStatus.FAILED
            task.error_message = "No files in task"
            return task

        ok = slskd_download_status(self.client, downloads)
        if not ok:
            logger.debug(f"Failed to get status for some files in task {task.task_id}")
            # slskd forgets transfers (restart, "clear completed"); a file that is already in the
            # download folder at full size has finished. slskd writes unfinished files to its
            # separate incomplete directory, so a file here is complete.
            for f in downloads:
                if f.get("status") is None and self._finished_on_disk(task, f):
                    f["status"] = {"state": "Completed, Succeeded", "bytesTransferred": f.get("size", 0)}

        # Still-unknown files mean this poll can't be trusted; the orchestrator pauses timeouts
        task.poll_failed = any(f.get("status") is None for f in downloads)

        all_succeeded, has_errors = downloads_all_done(downloads)

        if all_succeeded:
            task.status = DownloadStatus.COMPLETED
            task.progress_percent = 100.0
            return task

        # Fail fast: if any file has a terminal error, don't wait for others
        if has_errors:
            states = set(
                f.get("status", {}).get("state", "")
                for f in downloads
                if f.get("status")
            )
            task.status = DownloadStatus.FAILED
            task.error_message = f"File(s) failed: {', '.join(sorted(states))}"
            return task

        # Map remaining slskd states to DownloadStatus
        states = [f.get("status", {}).get("state", "") for f in downloads if f.get("status")]

        # Aggregate progress across all files (completed files count in full). offline_bytes are
        # chapters that finished in an earlier run and are no longer tracked in slskd.
        offline = task.extra.get("offline_bytes", 0)
        total_size = sum(f.get("size", 0) for f in downloads) + offline
        bytes_transferred = sum(f.get("status", {}).get("bytesTransferred", 0) for f in downloads if f.get("status")) + offline
        task.bytes_transferred = bytes_transferred
        if total_size > 0:
            task.progress_percent = (bytes_transferred / total_size) * 100

        # slskd reports transfer states such as "InProgress", "Initializing", "Queued, Remotely",
        # "Queued, Locally" and "Requested"
        if any(s in ("InProgress", "Initializing", "Downloading") for s in states):
            task.status = DownloadStatus.DOWNLOADING
        elif any(s == "Queued, Remotely" for s in states):
            task.status = DownloadStatus.QUEUED
        elif any(s == "Queued, Locally" for s in states):
            task.status = DownloadStatus.QUEUED_LOCALLY
        else:
            # Requested / pending states
            task.status = DownloadStatus.PENDING

        return task

    def _finished_on_disk(self, task: DownloadTask, file: Dict[str, Any]) -> bool:
        """True if the file is already in the local download folder at its expected size."""
        if not self.download_dir or not file.get("size"):
            return False  # without a known size, an old file of the same name could be mistaken for it
        path = Path(self.download_dir) / (task.local_dir or "") / file["filename"].split("\\")[-1]
        try:
            return path.is_file() and path.stat().st_size == file["size"]
        except OSError:
            return False

    def _local_clashes(self, file_dir: str, files: List[Dict[str, Any]]) -> List[str]:
        """File names this download would share with files already in, or on their way to,
        the same local folder."""
        leaf = file_dir.split("\\")[-1] if file_dir else ""
        names = {f["filename"].split("\\")[-1] for f in files}
        clashes = set()

        if self.download_dir and os.path.isdir(self.download_dir):
            folder = Path(self.download_dir) / leaf
            clashes.update(n for n in names if (folder / n).exists())

        # Unfinished downloads (from any peer) that will land in the same local folder
        try:
            for user_transfer in self.client.transfers.get_all_downloads():
                for directory in user_transfer.get("directories", []):
                    if directory.get("directory", "").split("\\")[-1] != leaf:
                        continue
                    for f in directory.get("files", []):
                        name = f.get("filename", "").split("\\")[-1]
                        if name in names and not str(f.get("state", "")).startswith("Completed"):
                            clashes.add(name)
        except Exception as e:
            logger.warning(f"Could not check slskd's transfer list for clashing downloads: {e}")

        return sorted(clashes)

    def discard(self, task: DownloadTask) -> None:
        """Move files a failed download left in the local folder (e.g. finished chapters)
        into <download_dir>/failed_downloads/, so a retry starts from a clean folder."""
        if not self.download_dir or not task.local_dir:
            return
        names = task.extra.get("expected_files") or [f["filename"].split("\\")[-1] for f in task.extra.get("files", [])]
        moved_to = move_files_aside(self.download_dir, task.local_dir, names, bucket="failed_downloads")
        if moved_to:
            logger.info(f"Moved leftovers of the failed download of {task.book_title} to {moved_to}")

    def cancel(self, task: DownloadTask) -> bool:
        """Cancel the download in slskd."""
        username = task.extra.get("username")
        files = task.extra.get("files", [])
        if not username or not files:
            return False

        success = True
        for file in files:
            try:
                self.client.transfers.cancel_download(username=username, id=file["id"])
            except Exception as e:
                logger.error(f"Failed to cancel download {file.get('id')} for {username}: {e}")
                success = False
        return success

    def cleanup(self, task: DownloadTask) -> None:
        """Remove this task's own records from slskd's transfer list.

        slskd is often shared with other tools (e.g. Soularr for music), so R:soul
        no longer clears every finished transfer; it removes only the ones it queued.
        """
        username = task.extra.get("username")
        removed = 0
        for f in task.extra.get("files", []):
            if not f.get("id"):
                continue
            try:
                # remove=True drops the record; for an unfinished transfer slskd cancels it first
                if self.client.transfers.cancel_download(username=f.get("username") or username, id=f["id"], remove=True):
                    removed += 1
            except Exception as e:
                logger.debug(f"Could not remove transfer {f['id']} from slskd: {e}")
        if removed:
            logger.debug(f"Removed {removed} transfer record(s) of {task.book_title} from slskd")

    def reconcile_task(self, task_data: Dict[str, Any]) -> Optional[DownloadTask]:
        """Reconcile a persisted task with live slskd state."""
        if task_data.get("extra", {}).get("media_type") == "audiobook":
            return self._reconcile_audiobook_task(task_data)

        username = task_data.get("extra", {}).get("username")
        filename = task_data.get("filename")
        local_dir = task_data.get("local_dir", "")

        if not username or not filename:
            return None

        # The transfer ID saved when the download was queued identifies it exactly; slskd keeps
        # old records of earlier attempts with the same filename, so names alone are ambiguous.
        saved_ids = {f.get("id") for f in task_data.get("extra", {}).get("files", []) if f.get("id")}

        try:
            # Query slskd for all downloads to find matching transfer
            all_downloads = self.client.transfers.get_all_downloads()

            candidates = []
            for user_transfer in all_downloads:
                if user_transfer["username"] == username:
                    for directory in user_transfer["directories"]:
                        for file in directory["files"]:
                            slskd_filename = file["filename"]
                            if slskd_filename == filename or slskd_filename.split("\\")[-1] == filename:
                                candidates.append((directory, file))

            if saved_ids:
                candidates = [c for c in candidates if c[1]["id"] in saved_ids]
            else:
                # Old state file without IDs: prefer a record that is still active
                candidates.sort(key=lambda c: str(c[1].get("state", "")).startswith("Completed"))

            if candidates:
                directory, file = candidates[0]
                # Found it! Re-create task with updated data
                new_files = [{"filename": file["filename"], "id": file["id"], "size": file["size"], "username": username, "file_dir": directory["directory"]}]

                task_data["extra"]["files"] = new_files
                task_data["extra"]["slskd_id"] = file["id"]
                task_data["extra"]["file_dir"] = directory["directory"]

                task = DownloadTask(
                    task_id=task_data["task_id"],
                    backend_name=self.name,
                    status=DownloadStatus.PENDING,
                    book_title=task_data["book_title"],
                    author_name=task_data["author_name"],
                    book_id=task_data["book_id"],
                    filename=task_data["filename"],
                    series_title=task_data.get("series_title", ""),
                    local_dir=task_data.get("local_dir", ""),
                    extra=task_data["extra"],
                )
                return self.get_status(task)

            # If not found in slskd, check if it's already on disk in the download dir
            if self.download_dir and local_dir:
                local_path = Path(self.download_dir) / local_dir / filename
                if local_path.exists():
                    return DownloadTask(
                        task_id=task_data["task_id"],
                        backend_name=self.name,
                        status=DownloadStatus.COMPLETED,
                        book_title=task_data["book_title"],
                        author_name=task_data["author_name"],
                        book_id=task_data["book_id"],
                        filename=filename,
                        series_title=task_data.get("series_title", ""),
                        local_dir=local_dir,
                        output_path=local_path,
                        progress_percent=100.0,
                        extra=task_data["extra"],
                    )

            return None

        except Exception as e:
            logger.error(f"Error reconciling slskd task: {e}")
            return None

    def _reconcile_audiobook_task(self, task_data: Dict[str, Any]) -> Optional[DownloadTask]:
        """Reconcile a persisted audiobook folder task.

        Files still in the slskd transfer list are tracked again; files already on
        disk count as done (slskd drops completed transfers from its list).
        """
        extra = task_data.get("extra", {})
        username = extra.get("username")
        file_dir = extra.get("file_dir", "")
        expected = extra.get("expected_files") or []
        local_dir = task_data.get("local_dir", "")
        if not username or not expected:
            return None

        leaf = file_dir.split("\\")[-1] if file_dir else ""
        # Chapters are matched by the transfer IDs saved when they were queued: slskd keeps old
        # records of earlier attempts with the same names (e.g. "Completed, Errored").
        saved_ids = {f["id"] for f in extra.get("files", []) if f.get("id")}
        tracked: Dict[str, Dict[str, Any]] = {}
        try:
            for user_transfer in self.client.transfers.get_all_downloads():
                if user_transfer["username"] != username:
                    continue
                for directory in user_transfer["directories"]:
                    if directory["directory"] not in (file_dir, leaf):
                        continue
                    for file in directory["files"]:
                        basename = file["filename"].split("\\")[-1]
                        if basename not in expected:
                            continue
                        if saved_ids and file["id"] not in saved_ids:
                            continue  # an older (or unrelated) record with the same name
                        previous = tracked.get(basename)
                        # Without saved IDs (old state file), prefer a record that is still active
                        if previous is None or str(previous.get("state", "")).startswith("Completed"):
                            tracked[basename] = {
                                "filename": file["filename"],
                                "id": file["id"],
                                "size": file["size"],
                                "username": username,
                                "file_dir": file_dir,
                                "state": file.get("state", ""),
                            }
        except Exception as e:
            logger.error(f"Error reconciling slskd audiobook task: {e}")
            return None
        for f in tracked.values():
            f.pop("state", None)

        on_disk = set()
        if self.download_dir and local_dir:
            on_disk = {name for name in expected if name not in tracked and (Path(self.download_dir) / local_dir / name).exists()}

        missing = [name for name in expected if name not in tracked and name not in on_disk]
        if missing:
            logger.warning(f"Cannot resume audiobook {task_data.get('book_title')}: {len(missing)} of {len(expected)} files are neither queued nor on disk")
            return None

        extra["files"] = list(tracked.values())
        # Chapters finished in an earlier run still count towards progress
        extra["offline_bytes"] = sum((Path(self.download_dir) / local_dir / name).stat().st_size for name in on_disk)
        task = DownloadTask(
            task_id=task_data["task_id"],
            backend_name=self.name,
            status=DownloadStatus.PENDING,
            book_title=task_data["book_title"],
            author_name=task_data["author_name"],
            book_id=task_data["book_id"],
            filename=task_data["filename"],
            series_title=task_data.get("series_title", ""),
            local_dir=local_dir,
            extra=extra,
        )
        if not tracked:
            # Everything already downloaded
            task.status = DownloadStatus.COMPLETED
            task.progress_percent = 100.0
            return task
        return self.get_status(task)
