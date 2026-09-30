import time
import logging
from typing import Any, Optional, Dict, List, Tuple
from .types import SlskdFile, SlskdDirectory

logger = logging.getLogger(__name__)


def is_not_found(error: Exception) -> bool:
    """True for an HTTP 404 from slskd.

    slskd answers 404 for a user with no transfers at all, and for a transfer ID it doesn't
    know (any more). slskd-api raises on every HTTP error, so these arrive as exceptions,
    but they are definite answers, not outages.
    """
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None) == 404


def get_user_downloads(slskd_client: Any, username: str) -> Dict[str, Any]:
    """slskd's transfer list for one user; an empty list when the user has none (404)."""
    try:
        return slskd_client.transfers.get_downloads(username=username)
    except Exception as e:
        if is_not_found(e):
            return {"username": username, "directories": []}
        raise


def _transfer_ids(slskd_client: Any, username: str) -> Optional[set]:
    """IDs of all transfers slskd currently lists for a user, or None if the list is unavailable."""
    try:
        download_list = get_user_downloads(slskd_client, username)
    except Exception as e:
        logger.error(f"Could not read slskd's transfer list for {username}: {e}")
        return None
    return {f["id"] for d in download_list.get("directories", []) for f in d.get("files", [])}


def slskd_do_enqueue(slskd_client: Any, username: str, files: List[SlskdFile], file_dir: str) -> Optional[List[SlskdFile]]:
    """
    Takes a list of files to download and returns a list of files that were successfully added to the download queue
    It also adds to each file the details needed to track that specific file.

    slskd keeps old transfer records (finished, failed or cancelled) in its list, so the same
    user/folder/filename can appear more than once. Records that existed before this enqueue
    are only reused if they are still active (slskd refuses to enqueue a file that is already
    queued or transferring); finished old records are never mistaken for the new download.
    """
    pre_existing = _transfer_ids(slskd_client, username)
    if pre_existing is None:
        # Without this snapshot an old record of the same file could be mistaken for the new
        # download, so don't queue anything; the book is retried on a later run.
        return None

    try:
        enqueue = slskd_client.transfers.enqueue(username=username, files=files)
    except Exception:
        logger.error("Enqueue failed", exc_info=True)
        return None

    if not enqueue:
        return None

    downloads: List[SlskdFile] = []
    leaf = file_dir.split("\\")[-1]

    # Poll for downloads to appear (handle race conditions)
    for attempt in range(4):
        time.sleep(2)
        downloads = []  # Reset on each attempt
        try:
            download_list = get_user_downloads(slskd_client, username)
            for file in files:
                target_filename = file["filename"]
                target_basename = target_filename.split("\\")[-1]

                chosen = None
                for directory in download_list["directories"]:
                    # Match directory name (full path or basename)
                    if directory["directory"] not in (file_dir, leaf):
                        continue
                    for slskd_file in directory["files"]:
                        if slskd_file["filename"] not in (target_filename, target_basename):
                            continue
                        if slskd_file["id"] not in pre_existing:
                            chosen = slskd_file  # created by this enqueue
                            break
                        if chosen is None and not str(slskd_file.get("state", "")).startswith("Completed"):
                            chosen = slskd_file  # already queued/transferring before: same download
                    if chosen is not None and chosen["id"] not in pre_existing:
                        break

                if chosen is not None:
                    downloads.append(
                        {
                            "filename": file["filename"],
                            "id": chosen["id"],
                            "file_dir": file_dir,
                            "username": username,
                            "size": file["size"],
                        }
                    )

            # Return as soon as every requested file shows up in the transfer list.
            # (Multi-file audiobook folders can take a moment to register fully.)
            if downloads and len(downloads) >= len(files):
                return downloads

        except Exception:
            logger.error("Error getting download list after enqueue", exc_info=True)
            if attempt == 3:
                return downloads or None

    # Partial result: some files never appeared. Callers decide whether that is acceptable.
    if downloads:
        logger.warning(f"Only {len(downloads)} of {len(files)} files appeared in the slskd transfer list")
    return downloads or None


TERMINAL_SUCCESS = "Completed, Succeeded"


def slskd_download_status(slskd_client: Any, downloads: List[SlskdFile]) -> bool:
    """
    Refresh the status of each file, packing it into the file object.

    Uses one transfer-list request per user instead of one request per file (an audiobook can
    have dozens of chapters), and skips files that already finished successfully. Falls back
    to per-file requests if the list request fails. A file whose status can't be read gets
    status None.

    Returns:
        True if every file's status is known.
    """
    ok = True
    pending: Dict[str, List[SlskdFile]] = {}
    for file in downloads:
        if (file.get("status") or {}).get("state") == TERMINAL_SUCCESS:
            continue  # finished files don't change any more
        pending.setdefault(file["username"], []).append(file)

    for username, files in pending.items():
        try:
            listing = get_user_downloads(slskd_client, username)
            by_id = {f["id"]: f for d in listing.get("directories", []) for f in d.get("files", [])}
        except Exception as e:
            logger.warning(f"Could not list transfers for {username} ({e}); checking files one by one")
            by_id = None

        for file in files:
            if by_id is not None:
                record = by_id.get(file["id"])
                if record is None:
                    # slskd answered but no longer lists this transfer: something cleared it
                    # ("clear completed" in the UI, or another tool such as Soularr, which clears
                    # all finished transfers after each run). The caller decides from the disk
                    # whether it had finished.
                    file["status"] = None
                    file["missing"] = True
                    ok = False
                else:
                    file["status"] = record
                    file.pop("missing", None)
                continue
            try:
                record = slskd_client.transfers.get_download(file["username"], file["id"])
                file["status"] = record if isinstance(record, dict) and "state" in record else None
                if file["status"] is None:
                    ok = False
            except Exception as e:
                file["status"] = None
                ok = False
                if is_not_found(e):
                    file["missing"] = True  # slskd doesn't know this transfer (any more)
                else:
                    logger.warning(f"Error getting download status of {file['filename']}: {e}")
    return ok


def downloads_all_done(downloads: List[SlskdFile]) -> Tuple[bool, bool]:
    """
    Check whether all files in a download have reached a terminal state.

    Returns:
        Tuple of (all_succeeded, has_errors):
            all_succeeded: True if every file is "Completed, Succeeded"
            has_errors: True if any file is in a terminal error state
    """
    all_succeeded = True
    has_errors = False
    for file in downloads:
        if file.get("status") is None:
            # Unknown (the status request failed): not proof that the file finished
            all_succeeded = False
            continue
        state = file["status"]["state"]
        if state != TERMINAL_SUCCESS:
            all_succeeded = False
            if state in [
                "Completed, Cancelled",
                "Completed, TimedOut",
                "Completed, Errored",
                "Completed, Rejected",
                "Completed, Aborted",
            ]:
                has_errors = True

    return all_succeeded, has_errors
