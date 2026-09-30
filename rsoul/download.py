import time
import logging
from typing import Any, Optional, Dict, List, Tuple
from .types import SlskdFile, SlskdDirectory

logger = logging.getLogger(__name__)


def _transfer_ids(slskd_client: Any, username: str) -> set:
    """IDs of all transfers slskd currently lists for a user (empty set if unavailable)."""
    try:
        download_list = slskd_client.transfers.get_downloads(username=username)
    except Exception:
        return set()
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
            download_list = slskd_client.transfers.get_downloads(username=username)
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


def slskd_download_status(slskd_client: Any, downloads: List[SlskdFile]) -> bool:
    """
    Takes a list of files and gets the status of each file and packs it into the file object.
    """
    ok = True
    for file in downloads:
        try:
            status = slskd_client.transfers.get_download(file["username"], file["id"])
            file["status"] = status
        except Exception:
            logger.exception(f"Error getting download status of {file['filename']}")
            file["status"] = None
            ok = False
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
        if file["status"] is not None:
            state = file["status"]["state"]
            if state != "Completed, Succeeded":
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
