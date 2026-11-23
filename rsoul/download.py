import time
import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Dict, List, Tuple
from .types import SlskdFile, SlskdDirectory
from .outcomes import http_status

logger = logging.getLogger(__name__)


@dataclass
class EnqueueOutcome:
    """A POST acknowledgement and the exact records its durable intent owns.

    `unknown` is deliberately not a failed download. The caller must keep the
    intent and reconcile later; repeating the POST could start an untracked copy.
    """
    state: str
    files: List[Dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    kind: str = ""


def matching_new_transfers(client, username, manifest, file_dir, pre_existing):
    """Match one new transfer per exact requested path; never reuse an old ID.

    A basename is accepted only inside the expected directory (slskd API versions
    differ between full path and leaf directory). Ambiguity remains unknown.
    """
    listing = get_user_downloads(client, username)
    leaf = file_dir.split("\\")[-1]
    matches = []
    ambiguous = False
    for expected in manifest:
        full = expected["filename"]
        basename = full.split("\\")[-1]
        candidates = {}
        for directory in listing.get("directories", []):
            observed_dir = directory.get("directory", "")
            if observed_dir not in (file_dir, leaf):
                continue
            for record in directory.get("files", []):
                rid = record.get("id")
                if not rid or rid in pre_existing:
                    continue
                observed_name = record.get("filename", "")
                if observed_name != full and not (observed_name == basename and observed_dir in (file_dir, leaf)):
                    continue
                # Size disagreement is not ownership proof; don't adopt an unrelated file.
                size = record.get("size")
                if size is not None and expected.get("size") and int(size) != int(expected["size"]):
                    continue
                candidates[rid] = record
        if len(candidates) > 1:
            ambiguous = True
        if len(candidates) == 1:
            record = next(iter(candidates.values()))
            matches.append({"filename": full, "size": expected.get("size"), "id": record["id"], "username": username, "file_dir": file_dir, "status": record})
    if len({row["id"] for row in matches}) != len(matches):
        ambiguous = True
    return matches, ambiguous


def enqueue_owned(client, username, manifest, file_dir, pre_existing, wait=None):
    """POST only after the caller durably recorded the complete enqueue intent.

    Registration may be asynchronous or the response may be lost. Even zero
    visible records after the observation window leaves the operation owned.
    """
    definite_rejection = False
    reason = ""
    kind = ""
    try:
        accepted = client.transfers.enqueue(username=username, files=[{"filename": f["filename"], "size": f["size"]} for f in manifest])
        definite_rejection = accepted is False
        if definite_rejection:
            reason = "slskd explicitly refused enqueue"
    except Exception as error:
        status = http_status(error)
        definite_rejection = status in (400, 401, 403, 404, 405, 409, 413, 415, 422, 429)
        reason = f"enqueue response {status or 'unknown'}: {error}"
        if definite_rejection:
            kind = "rate_limited" if status == 429 else "service_unavailable"
    observations = []
    for attempt in range(4):
        if attempt:
            (wait or time.sleep)(2)
        try:
            observations, ambiguous = matching_new_transfers(client, username, manifest, file_dir, pre_existing)
        except Exception as error:
            reason = f"{reason}; cannot reconcile enqueue: {error}".strip("; ")
            continue
        if not ambiguous and len(observations) == len(manifest):
            return EnqueueOutcome("registered", observations, reason)
        if definite_rejection and not observations and not ambiguous:
            return EnqueueOutcome("rejected", reason=reason, kind=kind)
        if ambiguous:
            reason = "multiple new transfer IDs match an enqueue intent; ownership is ambiguous"
    return EnqueueOutcome("unknown", observations, reason or "enqueue accepted but not every requested transfer has registered")


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


# How often, a few seconds apart, the transfer list is checked again for late arrivals
UNTRACKED_CHECKS = 3


def _cancel_untracked(slskd_client: Any, username: str, files: List[SlskdFile], file_dir: str, pre_existing: set) -> None:
    """Cancel what an enqueue started but R:soul isn't going to track.

    slskd accepted the request, yet not every transfer showed up in its list in time. The
    ones that did, and any that appear in the next few seconds, would download unattended
    into the local folder and then block every later attempt at it, so they are cancelled.
    The list is checked a few times because slskd registers transfers asynchronously. A
    transfer that appears even later is caught by the local-folder clash check, which then
    names it in the log.
    """
    leaf = file_dir.split("\\")[-1]
    wanted = {f["filename"] for f in files} | {f["filename"].split("\\")[-1] for f in files}
    for check in range(UNTRACKED_CHECKS):
        if check:
            time.sleep(2)
        try:
            download_list = get_user_downloads(slskd_client, username)
        except Exception as e:
            logger.warning(f"Could not look for untracked transfers from {username}: {e}")
            continue
        for directory in download_list.get("directories", []):
            if directory.get("directory") not in (file_dir, leaf):
                continue
            for record in directory.get("files", []):
                if record.get("id") in pre_existing or record.get("filename") not in wanted:
                    continue
                try:
                    slskd_client.transfers.cancel_download(username=username, id=record["id"], remove=True)
                    logger.info(f"Cancelled untracked transfer {record.get('filename', '').split(chr(92))[-1]} from {username}")
                except Exception as e:
                    logger.warning(f"Could not cancel untracked transfer {record.get('filename')} from {username}: {e}")


def slskd_do_enqueue(slskd_client: Any, username: str, files: List[SlskdFile], file_dir: str, require_all: bool = False) -> Optional[List[SlskdFile]]:
    """
    Takes a list of files to download and returns a list of files that were successfully added to the download queue
    It also adds to each file the details needed to track that specific file.

    With require_all (audiobooks), anything short of every file counts as a failure: the
    transfers that did get queued are cancelled again and None is returned.

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

    # Partial result: some files never appeared
    if downloads:
        logger.warning(f"Only {len(downloads)} of {len(files)} files appeared in the slskd transfer list")
    if not downloads or require_all:
        _cancel_untracked(slskd_client, username, files, file_dir, pre_existing)
        return None
    return downloads


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
            # "Completed, Cancelled", "Completed, TimedOut", "Completed, Errored", "Completed,
            # Rejected", "Completed, Aborted"... every finished state other than success is a
            # failure, including ones a newer slskd may add
            if str(state).startswith("Completed"):
                has_errors = True

    return all_succeeded, has_errors
