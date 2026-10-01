import os
import shutil
import logging
import re
import difflib
import time
import operator
from typing import Any, Optional

from mobi_header import MobiHeader
import ebookmeta
from .utils import sanitize_folder_name, jaccard_similarity, extract_author_title
from .display import print_import_summary, print_section_header
from . import health

logger = logging.getLogger("readarr_soul")


def move_failed_import(src_path: str, base_dir: str = ""):
    """Move failed import to failed_imports directory with better error handling.

    Args:
        src_path: Path to the source file/folder to move
        base_dir: Base directory for the failed_imports folder. If empty, uses
                  the parent directory of src_path.
    """
    try:
        if not base_dir:
            base_dir = os.path.dirname(src_path)
        failed_imports_dir = os.path.join(base_dir, "failed_imports")
        if not os.path.exists(failed_imports_dir):
            os.makedirs(failed_imports_dir)
            logger.info(f"Created failed imports directory: {failed_imports_dir}")

        folder_name = os.path.basename(src_path)
        target_path = os.path.join(failed_imports_dir, folder_name)
        counter = 1

        while os.path.exists(target_path):
            target_path = os.path.join(failed_imports_dir, f"{folder_name}_{counter}")
            counter += 1

        if os.path.exists(src_path):
            shutil.move(src_path, target_path)
            logger.info(f"Failed import moved to: {target_path}")
        else:
            logger.warning(f"Failed import source not found: {src_path}")

    except Exception:
        logger.exception(f"Error moving failed import from {src_path}")


def check_title_similarity(
    metadata_title: str,
    expected_title: str,
    ratio_exact: float,
    ratio_normalized: float,
    ratio_word: float,
    ratio_loose: float,
    ratio_jaccard: float,
    label: str = "title",
) -> bool:
    """Check if metadata title matches expected title using multiple methods.

    Args:
        metadata_title: Title extracted from file metadata
        expected_title: Expected title from Readarr
        ratio_exact: Threshold for exact match
        ratio_normalized: Threshold for normalized match
        ratio_word: Threshold for word-based similarity
        ratio_loose: Threshold for loose match (brackets removed)
        ratio_jaccard: Threshold for Jaccard similarity
        label: Label for logging (e.g., "title" or "seriesTitle")

    Returns:
        True if any method exceeds its threshold
    """
    # Exact match
    diff = difflib.SequenceMatcher(None, metadata_title, expected_title).ratio()
    logger.debug(f"[{label}] Exact match ratio: {diff:.3f}")

    # Normalized match
    normalized_meta = re.sub(r"[^\w\s]", "", metadata_title.lower())
    normalized_expected = re.sub(r"[^\w\s]", "", expected_title.lower())
    normalized_diff = difflib.SequenceMatcher(None, normalized_meta, normalized_expected).ratio()
    logger.debug(f"[{label}] Normalized match ratio: {normalized_diff:.3f}")

    # Word-based similarity
    meta_words = set(metadata_title.lower().split())
    expected_words = set(expected_title.lower().split())
    word_intersection = len(meta_words.intersection(expected_words))
    word_union = len(meta_words.union(expected_words))
    word_similarity = word_intersection / word_union if word_union > 0 else 0
    logger.debug(f"[{label}] Word-based similarity: {word_similarity:.3f}")

    # Loose match (brackets/parentheses removed)
    clean_meta = re.sub(r"\s*[\(\[].*?[\)\]]", "", metadata_title).strip()
    clean_expected = re.sub(r"\s*[\(\[].*?[\)\]]", "", expected_title).strip()
    clean_diff = 0.0
    if clean_meta and clean_expected:
        clean_diff = difflib.SequenceMatcher(None, clean_meta.lower(), clean_expected.lower()).ratio()
        logger.debug(f"[{label}] Loose match ratio: {clean_diff:.3f}")

    # Jaccard similarity
    jaccard_score, overlap_count, _ = jaccard_similarity(metadata_title, expected_title)
    logger.debug(f"[{label}] Jaccard similarity: {jaccard_score:.3f} ({overlap_count} words)")

    # Check if any threshold is met
    if diff > ratio_exact:
        logger.info(f"[{label}] Passed on exact match: {diff:.3f} > {ratio_exact}")
        return True
    if normalized_diff > ratio_normalized:
        logger.info(f"[{label}] Passed on normalized match: {normalized_diff:.3f} > {ratio_normalized}")
        return True
    if word_similarity > ratio_word:
        logger.info(f"[{label}] Passed on word similarity: {word_similarity:.3f} > {ratio_word}")
        return True
    if clean_diff > ratio_loose:
        logger.info(f"[{label}] Passed on loose match: {clean_diff:.3f} > {ratio_loose}")
        return True
    if jaccard_score > ratio_jaccard:
        logger.info(f"[{label}] Passed on Jaccard: {jaccard_score:.3f} > {ratio_jaccard}")
        return True

    return False


def check_swapped_author_title(metadata_title: str, expected_author: str, expected_title: str) -> bool:
    """Check if author and title appear to be swapped in metadata.

    Detects cases where the metadata title field contains the author name
    (suggesting the fields are reversed in the ebook).

    Args:
        metadata_title: Title extracted from file metadata
        expected_author: Expected author name from Readarr
        expected_title: Expected book title from Readarr

    Returns:
        True if fields appear swapped (author in title field), False otherwise
    """
    # Normalize for comparison
    meta_title_norm = metadata_title.lower().strip()
    author_norm = expected_author.lower().strip()
    title_norm = expected_title.lower().strip()

    # Check if metadata title matches author better than it matches title
    author_similarity = difflib.SequenceMatcher(None, meta_title_norm, author_norm).ratio()
    title_similarity = difflib.SequenceMatcher(None, meta_title_norm, title_norm).ratio()

    # If metadata title is very similar to author (>0.7) and not similar to title (<0.4)
    # then fields are likely swapped
    if author_similarity > 0.7 and title_similarity < 0.4:
        logger.warning(f"Detected swapped metadata: title field contains author name")
        logger.warning(f"  Metadata title: '{metadata_title}'")
        logger.warning(f"  Expected author: '{expected_author}' (similarity: {author_similarity:.2f})")
        logger.warning(f"  Expected title: '{expected_title}' (similarity: {title_similarity:.2f})")
        return True

    # Also check if author name is contained within the title field
    if author_norm in meta_title_norm and title_norm not in meta_title_norm:
        # Author name found in title, but actual title not found
        if len(author_norm) > 5:  # Only flag if author name is substantial
            logger.warning(f"Detected author name in title field: '{metadata_title}' contains '{expected_author}'")
            return True

    return False


def get_metadata_author(file_path: str, extension: str, metadata: Any = None) -> str:
    """Extract author from metadata based on file extension."""
    author = ""
    try:
        if extension == "epub":
            if not metadata:
                metadata = ebookmeta.get_metadata(file_path)
            # ebookmeta usually returns author_list
            if hasattr(metadata, "author_list") and metadata.author_list:
                author = metadata.author_list[0]
            elif hasattr(metadata, "creator"):
                author = metadata.creator
        elif extension in ["azw3", "mobi"]:
            if not metadata:
                metadata = MobiHeader(file_path)
            # Try EXTH 100 (Author)
            val = metadata.get_exth_value_by_id(100)
            if val:
                if isinstance(val, bytes):
                    author = val.decode("utf-8", errors="ignore")
                else:
                    author = str(val)
    except Exception as e:
        logger.debug(f"Failed to extract author from metadata: {e}")

    return author.strip()


def validate_metadata(file_path: str, book_title: str, book_id: int, ctx: Any, series_title: str = "", author_name: str = "") -> bool:
    """
    Validate file metadata against Readarr book info.
    Returns True if validation passes or is skipped for the file type, False otherwise.

    For EPUB/MOBI/AZW3 files, checks metadata title against both book_title and series_title.
    Also detects swapped author/title fields.
    If either matches (exceeds threshold), validation passes.

    Can be globally disabled via config: [Postprocessing] skip_validation = True
    """
    # Check for global skip flag
    skip_validation = ctx.config.getboolean("Postprocessing", "skip_validation", fallback=False)
    if skip_validation:
        logger.info(f"Metadata validation disabled via config - skipping for: {file_path}")
        return True

    extension = file_path.split(".")[-1].lower()
    match = False
    readarr_client = ctx.readarr

    # Get thresholds from config
    ratio_exact = ctx.config.getfloat("Postprocessing", "match_ratio_exact", fallback=0.8)
    ratio_normalized = ctx.config.getfloat("Postprocessing", "match_ratio_normalized", fallback=0.85)
    ratio_word = ctx.config.getfloat("Postprocessing", "match_ratio_word", fallback=0.7)
    ratio_loose = ctx.config.getfloat("Postprocessing", "match_ratio_loose", fallback=0.85)
    ratio_jaccard = ctx.config.getfloat("Postprocessing", "match_ratio_jaccard", fallback=0.5)

    # Enhanced metadata validation with better error handling
    if extension in ["azw3", "mobi"]:
        try:
            logger.info(f"Reading MOBI/AZW3 metadata from: {file_path}")
            metadata = MobiHeader(file_path)

            # 1. Try Title Validation (Same as EPUB)
            title = None
            # Try getting full name from metadata dict
            if hasattr(metadata, "metadata") and "full_name" in metadata.metadata:
                title = metadata.metadata["full_name"].get("value")  # type: ignore

            # Fallback to EXTH 503 (Updated Title)
            if not title:
                title = metadata.get_exth_value_by_id(503)

            if title:
                # Decode bytes if necessary (MobiHeader might return bytes)
                if isinstance(title, bytes):
                    try:
                        title = title.decode("utf-8")
                    except Exception:
                        title = str(title)

                logger.info(f"Found title in metadata: '{title}'")
                logger.info(f"Expected title: '{book_title}'")
                if series_title:
                    logger.info(f"Series title: '{series_title}'")

                # Check for swapped author/title fields
                if author_name and check_swapped_author_title(title, author_name, book_title):
                    logger.warning("Metadata appears to have swapped author/title (Title field contains Author name)")

                    # Check if the Author field contains the Title (confirm swap)
                    meta_author = get_metadata_author(file_path, extension, metadata)
                    logger.info(f"Checking metadata Author field: '{meta_author}' against expected Title: '{book_title}'")

                    if meta_author and check_title_similarity(meta_author, book_title, ratio_exact, ratio_normalized, ratio_word, ratio_loose, ratio_jaccard, label="swapped_author"):
                        logger.info("Confirmed valid swapped metadata (Author field matches Title). Accepting.")
                        match = True
                    else:
                        logger.warning("Could not confirm valid title in Author field - rejecting.")
                        match = False
                else:
                    # Check against book title
                    title_match = check_title_similarity(title, book_title, ratio_exact, ratio_normalized, ratio_word, ratio_loose, ratio_jaccard, label="title")

                    # Check against series title if provided and title didn't match
                    series_match = False
                    if not title_match and series_title:
                        series_match = check_title_similarity(title, series_title, ratio_exact, ratio_normalized, ratio_word, ratio_loose, ratio_jaccard, label="seriesTitle")

                    if title_match or series_match:
                        logger.info("Title validation passed")
                        match = True
                    else:
                        logger.warning("Title validation failed - insufficient similarity to both title and seriesTitle")
                        match = False
            else:
                logger.warning("No title found in MOBI/AZW3 metadata - cannot verify")
                match = False

        except Exception as e:
            logger.error(f"Error reading MOBI/AZW3 metadata: {e}")
            match = False

    elif extension == "epub":
        try:
            logger.info(f"Reading EPUB metadata from: {file_path}")
            metadata = ebookmeta.get_metadata(file_path)
            title = metadata.title

            if title:
                logger.info(f"Found title in metadata: '{title}'")
                logger.info(f"Expected title: '{book_title}'")
                if series_title:
                    logger.info(f"Series title: '{series_title}'")

                # Check for swapped author/title fields
                if author_name and check_swapped_author_title(title, author_name, book_title):
                    logger.warning("Metadata appears to have swapped author/title (Title field contains Author name)")

                    # Check if the Author field contains the Title (confirm swap)
                    meta_author = get_metadata_author(file_path, extension, metadata)
                    logger.info(f"Checking metadata Author field: '{meta_author}' against expected Title: '{book_title}'")

                    if meta_author and check_title_similarity(meta_author, book_title, ratio_exact, ratio_normalized, ratio_word, ratio_loose, ratio_jaccard, label="swapped_author"):
                        logger.info("Confirmed valid swapped metadata (Author field matches Title). Accepting.")
                        match = True
                    else:
                        logger.warning("Could not confirm valid title in Author field - rejecting.")
                        match = False
                else:
                    # Check against book title
                    title_match = check_title_similarity(title, book_title, ratio_exact, ratio_normalized, ratio_word, ratio_loose, ratio_jaccard, label="title")

                    # Check against series title if provided and title didn't match
                    series_match = False
                    if not title_match and series_title:
                        series_match = check_title_similarity(title, series_title, ratio_exact, ratio_normalized, ratio_word, ratio_loose, ratio_jaccard, label="seriesTitle")

                    if title_match or series_match:
                        logger.info("Title validation passed")
                        match = True
                    else:
                        logger.warning("Title validation failed - insufficient similarity to both title and seriesTitle")
                        match = False
            else:
                logger.warning("No title found in EPUB metadata - cannot verify")
                match = False

        except Exception as e:
            logger.error(f"Error reading EPUB metadata: {e}")
            match = False

    else:
        logger.info(f"File type {extension} - skipping metadata validation")
        match = True

    return match


def organize_file(source_path: str, target_folder: str, filename: str, original_folder: str, base_dir: str = "") -> bool:
    """
    Organize file into author folder and clean up source directory.
    Returns True if successful, False on error.

    Args:
        source_path: Absolute path to the source file
        target_folder: Author folder name (relative to base_dir)
        filename: Filename to use in the target folder
        original_folder: Source directory to clean up if empty
        base_dir: Base directory for the target folder. If empty, target_folder
                  is used as-is (legacy behavior).
    """
    try:
        # Resolve target directory as absolute path
        abs_target_folder = os.path.join(base_dir, target_folder) if base_dir else target_folder

        # Create target directory
        if not os.path.exists(abs_target_folder):
            logger.info(f"Creating author directory: {abs_target_folder}")
            os.makedirs(abs_target_folder, exist_ok=True)

        target_file_path = os.path.join(abs_target_folder, filename)

        if os.path.exists(source_path) and not os.path.exists(target_file_path):
            logger.info(f"Moving file from {source_path} to {target_file_path}")
            shutil.move(source_path, target_file_path)
            logger.info("File moved successfully")

            # Clean up source directory if empty (but don't delete base download dirs)
            try:
                abs_original = os.path.join(base_dir, original_folder) if base_dir else original_folder
                if abs_original and abs_original != base_dir and os.path.exists(abs_original) and not os.listdir(abs_original):
                    logger.info(f"Removing empty source directory: {abs_original}")
                    shutil.rmtree(abs_original)
            except OSError as e:
                logger.warning(f"Could not remove source directory {original_folder}: {e}")

            return True
        else:
            if not os.path.exists(source_path):
                logger.warning(f"Source file no longer exists: {source_path}")
            if os.path.exists(target_file_path):
                logger.warning(f"Target file already exists: {target_file_path}")
            return False

    except Exception as e:
        logger.error(f"Failed to organize file: {e}")
        return False


def move_files_aside(base_dir: str, folder: str, names: list, bucket: str = "failed_imports") -> str:
    """Move just these files from <base_dir>/<folder> into <base_dir>/<bucket>/<folder>[_n].

    Only the named files are moved: slskd puts downloads from different peers that share a
    folder name into the same local folder, so moving the whole folder could take other
    books' files with it. Returns the destination folder ("" if nothing was moved).
    """
    source_dir = os.path.join(base_dir, folder)
    present = [n for n in names if os.path.isfile(os.path.join(source_dir, n))]
    if not present:
        return ""

    target_dir = os.path.join(base_dir, bucket, os.path.basename(folder.rstrip(os.sep)) or "unknown")
    counter = 1
    while os.path.exists(target_dir):
        target_dir = os.path.join(base_dir, bucket, f"{os.path.basename(folder.rstrip(os.sep)) or 'unknown'}_{counter}")
        counter += 1

    try:
        os.makedirs(target_dir, exist_ok=True)
        for name in present:
            shutil.move(os.path.join(source_dir, name), os.path.join(target_dir, name))
        logger.info(f"Moved {len(present)} file(s) to {target_dir}")
        if os.path.abspath(source_dir) != os.path.abspath(base_dir) and os.path.isdir(source_dir) and not os.listdir(source_dir):
            shutil.rmtree(source_dir)
    except Exception as e:
        logger.error(f"Failed to move files to {target_dir}: {e}")
    return target_dir


# Audiobooks are staged in their own subfolder of the download dir, one folder per book,
# so an ebook import scan of an author folder never picks up a half-organised audiobook.
AUDIOBOOK_STAGING_DIR = "rsoul_audiobooks"


def organize_audiobook(book_download: dict, local_download_dir: str) -> tuple:
    """Move a completed audiobook folder into <staging>/<Author>/<Title>/.

    Returns:
        (relative_import_folder, None) on success, or (None, reason) on failure.
    """
    folder = book_download["dir"]
    source_dir = os.path.join(local_download_dir, folder)
    expected = book_download.get("expected_files") or [f["filename"].split("\\")[-1] for f in book_download.get("files", [])]

    if not expected:
        return None, "No audiobook files recorded for this download"

    missing = [name for name in expected if not os.path.exists(os.path.join(source_dir, name))]
    if missing:
        return None, f"{len(missing)} of {len(expected)} audiobook files missing in {source_dir}"

    author_folder = sanitize_folder_name(book_download["author_name"])
    title_folder = sanitize_folder_name(book_download["title"]) or f"book_{book_download.get('bookId', 'unknown')}"
    relative = os.path.join(AUDIOBOOK_STAGING_DIR, author_folder, title_folder)

    target_dir = os.path.join(local_download_dir, relative)
    counter = 2
    while os.path.exists(target_dir) and os.listdir(target_dir):
        relative = os.path.join(AUDIOBOOK_STAGING_DIR, author_folder, f"{title_folder} ({counter})")
        target_dir = os.path.join(local_download_dir, relative)
        counter += 1

    moved = []
    try:
        os.makedirs(target_dir, exist_ok=True)
        for name in expected:
            shutil.move(os.path.join(source_dir, name), os.path.join(target_dir, name))
            moved.append(name)
        logger.info(f"Moved {len(expected)} audiobook files to {target_dir}")
    except Exception as e:
        # Put the chapters already moved back next to the others, so the failure handling
        # moves the whole book to failed_imports instead of leaving part of it in staging
        for name in moved:
            try:
                shutil.move(os.path.join(target_dir, name), os.path.join(source_dir, name))
            except Exception as back_error:
                logger.error(f"Could not move {name} back from {target_dir}: {back_error}")
        try:
            if os.path.isdir(target_dir) and not os.listdir(target_dir):
                os.rmdir(target_dir)
        except OSError:
            pass
        return None, f"Failed to organize audiobook: {e}"

    # Clean up the source folder if nothing else is left in it
    try:
        if os.path.abspath(source_dir) != os.path.abspath(local_download_dir) and os.path.isdir(source_dir) and not os.listdir(source_dir):
            shutil.rmtree(source_dir)
    except OSError as e:
        logger.warning(f"Could not remove source directory {source_dir}: {e}")

    return relative, None


def trigger_imports(readarr_client: Any, readarr_download_dir: str, author_folders: list) -> list:
    """
    Trigger Readarr scan commands for processed author folders.
    Returns a list of command objects.
    """
    commands = []
    if not author_folders:
        return commands

    logger.info("Starting Readarr import commands...")
    for author_folder in author_folders:
        try:
            download_dir = os.path.join(readarr_download_dir, author_folder)
            logger.info(f"Importing from: {download_dir}")

            command = readarr_client.post_command(name="DownloadedBooksScan", path=download_dir)
            command["_rsoul_folder"] = author_folder  # lets process_imports map results back to books
            commands.append(command)
            logger.info(f"Import command created - ID: {command['id']} for folder: {author_folder}")

        except Exception:
            logger.exception(f"Failed to create import command for {author_folder}")

    if commands:
        print_import_summary(commands)

    return commands


def to_local_path(path: str, readarr_download_dir: str, local_download_dir: str) -> str:
    """Translate a path as Readarr/Chaptarr sees it into the path R:soul sees.

    Import commands report paths from Readarr's side of the Docker volume mapping; moving
    a failed import has to use R:soul's side, or the folder is never found.
    """
    if not readarr_download_dir or not local_download_dir:
        return path
    remote = os.path.normpath(readarr_download_dir)
    normalized = os.path.normpath(path)
    if normalized == remote or normalized.startswith(remote + os.sep):
        return os.path.join(local_download_dir, os.path.relpath(normalized, remote))
    return path


# How long a run waits for an import command before leaving it for the next run
IMPORT_TIMEOUT_SECONDS = 600


def _import_succeeded(command: dict) -> bool:
    """Whether a finished DownloadedBooksScan command actually imported something.

    Readarr and Chaptarr finish the command with status "completed" even when nothing was
    imported; they report that through result = "unsuccessful" (message "Failed to import").
    The message check covers older versions without a result field.
    """
    if command.get("status") != "completed":
        return False
    if str(command.get("result", "")).lower() == "unsuccessful":
        return False
    message = (command.get("message") or "").lower()
    return "failed" not in message and "no files found" not in message


def monitor_imports(readarr_client: Any, commands: list, readarr_download_dir: str = "", local_download_dir: str = "", timeout: float = IMPORT_TIMEOUT_SECONDS) -> dict:
    """Wait for Readarr import commands to finish and report results.

    Returns:
        {command id: True if the import succeeded, False if it failed, None if it was still
        running (or Readarr/Chaptarr couldn't be reached) when the wait ended}. Failed imports
        are moved to failed_imports; unfinished ones are left in place to be checked again.
    """
    results: dict = {}
    if not commands:
        return results

    logger.info("Monitoring import progress...")
    deadline = time.time() + timeout
    final: dict = {}
    while len(final) < len(commands) and time.time() < deadline:
        for task in commands:
            if task["id"] in final:
                continue
            try:
                current_task = readarr_client.get_command(task["id"])
            except Exception as e:
                # Transient API error: keep polling until the timeout instead of giving up on an
                # import that may still succeed
                logger.warning(f"Error checking import command {task['id']}: {e}")
                continue
            if current_task.get("status") in ["completed", "failed", "aborted", "cancelled", "orphaned"]:
                final[task["id"]] = current_task
        if len(final) < len(commands):
            health.heartbeat()
            time.sleep(2)

    # Report final results
    logger.info("Import Results:")
    for task in commands:
        current_task = final.get(task["id"])
        if current_task is None:
            logger.warning(f"Import command {task['id']} hasn't finished after {timeout:.0f}s; it will be checked again on the next run")
            results[task["id"]] = None
            continue

        body = current_task.get("body") or {}
        path = body.get("path", "")
        folder_name = os.path.basename(path) if path else f"Task {task['id']}"
        message = current_task.get("message", "")

        results[task["id"]] = _import_succeeded(current_task)
        if results[task["id"]]:
            logger.info(f"{folder_name}: Import command finished (status: {current_task.get('status')}, result: {current_task.get('result', 'n/a')}). Message: {message or '-'}")
            continue

        logger.warning(f"{folder_name}: Import did not succeed (status: {current_task.get('status')}, result: {current_task.get('result', 'n/a')}): {message}")
        if path:
            move_failed_import(to_local_path(path, readarr_download_dir, local_download_dir))

    return results


IMPORT_SETTLE_SECONDS = 30


def files_left_after_import(paths: list, folder: str, settle: Optional[float] = None):
    """Double-check a reported import: Readarr and Chaptarr move imported files out of the
    download folder, so files still sitting there mean nothing was actually imported.

    Waits up to `settle` seconds for the files to go. Returns the list of files still there
    (logged as a failure), or None if they have all been moved.
    """
    deadline = time.time() + (IMPORT_SETTLE_SECONDS if settle is None else settle)
    remaining = [p for p in paths if os.path.exists(p)]
    while remaining and time.time() < deadline:
        time.sleep(2)
        remaining = [p for p in remaining if os.path.exists(p)]
    if not remaining:
        return None
    logger.warning(
        f"Import reported as finished, but {len(remaining)} of {len(paths)} file(s) are still in {folder}: "
        "nothing was imported. Check Readarr/Chaptarr's logs (lines with DOWNLOAD-IMPORT) for the reason; "
        "the files are left there so you can import them manually."
    )
    return remaining


# How long an import may stay unconfirmed across runs before R:soul gives up on it
PENDING_IMPORT_MAX_AGE = 24 * 3600
PENDING_IMPORT_MAX_SUBMITS = 3


def pending_import_record(folder: str, readarr_download_dir: str, local_download_dir: str, files: list, command_id: Optional[int]) -> dict:
    """Everything needed to finish an import on a later run."""
    return {
        "folder": folder,
        "local_dir": os.path.join(local_download_dir, folder),
        "readarr_path": os.path.join(readarr_download_dir, folder),
        "files": list(files),
        "command_id": command_id,
        "submits": 1 if command_id is not None else 0,
        "since": time.time(),
    }


def _give_up(local_dir: str, left: list) -> None:
    """Move the files of an import R:soul stops following into failed_imports, like every
    other failed import, so nothing is left untracked in the staging folder."""
    if left:
        move_failed_import(local_dir)
        logger.error("Its files are in failed_imports for a manual import in Readarr/Chaptarr")


def check_pending_import(readarr_client: Any, pending: dict, now: Optional[float] = None):
    """Follow up an import left unconfirmed by an earlier run.

    Returns True (imported), False (failed or given up), None (still running; keep waiting),
    or an updated pending record (re-submitted; check again next run).
    """
    now = time.time() if now is None else now
    local_dir, files = pending["local_dir"], pending.get("files", [])
    left = [p for p in files if os.path.exists(p)]
    label = pending.get("folder", local_dir)

    if now - pending.get("since", now) > PENDING_IMPORT_MAX_AGE:
        if files and not left:
            # Chaptarr moves the files when it imports them, even if R:soul never heard back
            logger.info(f"Files of {label} are gone from the download folder: treating the import as done")
            return True
        logger.error(f"Import of {label} still unconfirmed after {PENDING_IMPORT_MAX_AGE // 3600} hours; giving up")
        _give_up(local_dir, left)
        return False

    command_id = pending.get("command_id")
    if command_id is not None:
        try:
            command = readarr_client.get_command(command_id)
        except Exception as e:
            if getattr(getattr(e, "response", None), "status_code", None) != 404:
                logger.warning(f"Could not check import of {label} ({e}); trying again next run")
                return None
            command = None  # Readarr/Chaptarr no longer knows the command (e.g. it restarted)

        if command is not None:
            status = command.get("status")
            if status not in ("completed", "failed", "aborted", "cancelled", "orphaned"):
                logger.info(f"Import of {label} is still running; checking again next run")
                return None
            if _import_succeeded(command) and files_left_after_import(files, local_dir) is None:
                logger.info(f"Import of {label} finished (result: {command.get('result', 'n/a')})")
                return True
            logger.warning(f"Import of {label} did not succeed (status: {status}, result: {command.get('result', 'n/a')})")
            if left:
                move_failed_import(local_dir)
            return False

    # No command (never submitted, or forgotten by the server)
    if files and not left:
        logger.info(f"Files of {label} are gone from the download folder: treating the import as done")
        return True
    if pending.get("submits", 0) >= PENDING_IMPORT_MAX_SUBMITS:
        logger.error(f"Import of {label} submitted {pending['submits']} times without a result; giving up")
        _give_up(local_dir, left)
        return False
    try:
        command = readarr_client.post_command(name="DownloadedBooksScan", path=pending["readarr_path"])
    except Exception as e:
        logger.warning(f"Could not submit import of {label} ({e}); trying again next run")
        return None
    logger.info(f"Submitted import of {label} again - command ID {command['id']}")
    return dict(pending, command_id=command["id"], submits=pending.get("submits", 0) + 1)


def process_imports(ctx: Any, grab_list: list) -> dict:
    """Process downloaded files, validate metadata, and trigger Readarr import.

    Handles items from multiple backends by grouping them and using backend-specific paths.

    Returns:
        {bookId: True if the book was imported (or sync is disabled), False if it failed, or a
        pending-import record (dict) when the import couldn't be confirmed yet: the command
        was still running, or Readarr/Chaptarr couldn't be reached. Pending imports are
        checked again on the next run by check_pending_import.}
    """
    results = {item.get("bookId"): False for item in grab_list}
    print_section_header("METADATA VALIDATION & IMPORT PHASE")

    readarr_disable_sync = ctx.config.getboolean("Readarr", "disable_sync", fallback=False)

    # Legacy fallback
    default_slskd_dir = ctx.config.get("Slskd", "download_dir", fallback="")
    default_readarr_dir = ctx.config.get("Slskd", "readarr_download_dir", fallback=default_slskd_dir)

    readarr = ctx.readarr

    # Check if sync is disabled first
    if readarr_disable_sync:
        logger.warning("Readarr sync is disabled in config. Skipping import phase.")
        logger.info("Files downloaded but not imported.")
        return {book_id: True for book_id in results}

    # Group items by backend to handle directory switching
    items_by_backend = {}
    for item in grab_list:
        backend_name = item.get("backend_name", "slskd")  # Default to slskd for legacy items
        if backend_name not in items_by_backend:
            items_by_backend[backend_name] = []
        items_by_backend[backend_name].append(item)

    # Process each backend's items
    for backend_name, items in items_by_backend.items():
        logger.info(f"Processing {len(items)} items for backend: {backend_name}")

        # Determine directories
        local_download_dir = ""
        readarr_download_dir = ""

        if ctx.orchestrator:
            backend = ctx.orchestrator.get_backend(backend_name)
            if backend:
                local_download_dir = backend.download_dir
                readarr_download_dir = backend.readarr_download_dir
            else:
                logger.warning(f"Backend {backend_name} not found in orchestrator - using defaults")

        # Fallbacks for legacy/missing backend
        if not local_download_dir:
            local_download_dir = default_slskd_dir
        if not readarr_download_dir:
            readarr_download_dir = default_readarr_dir

        if not local_download_dir or not os.path.exists(local_download_dir):
            logger.error(f"Download directory not found for {backend_name}: {local_download_dir}")
            continue

        logger.info(f"Using download directory: {local_download_dir}")

        items.sort(key=operator.itemgetter("author_name"))
        failed_imports = []
        author_folders = set()
        folder_books: dict = {}  # import folder -> book IDs whose files were moved there
        folder_files: dict = {}  # import folder -> local paths of the files handed to Readarr/Chaptarr

        for book_download in items:
            if book_download.get("media_type") == "audiobook":
                # Whole folder of audio files: no per-file metadata validation, one import per book folder
                book_title = book_download.get("title", "")
                source_id = book_download.get("source_id", book_download.get("username", ""))
                logger.info(f"Processing audiobook: {book_title}")
                relative, reason = organize_audiobook(book_download, local_download_dir)
                if relative:
                    author_folders.add(relative)
                    folder_books.setdefault(relative, []).append(book_download.get("bookId"))
                    names = book_download.get("expected_files") or []
                    folder_files.setdefault(relative, []).extend(os.path.join(local_download_dir, relative, n) for n in names)
                else:
                    logger.warning(f"Audiobook failed: {book_title} - {reason}")
                    names = book_download.get("expected_files") or [f["filename"].split("\\")[-1] for f in book_download.get("files", [])]
                    move_files_aside(local_download_dir, book_download.get("dir", ""), names)
                    if ctx.history and source_id and book_title:
                        ctx.history.add_failure(source_id, book_title, reason or "Audiobook import failed")
                continue

            try:
                author_name = book_download["author_name"]
                author_name_sanitized = sanitize_folder_name(author_name)
                folder = book_download["dir"]

                # Backend-specific filename extraction
                if backend_name == "slskd":
                    # Slskd returns Windows-style paths even on Linux (e.g. C:\Books\Author - Title.epub)
                    # Use legacy splitting on backslash to preserve compatibility
                    filename = book_download["filename"].split("\\")[-1]
                else:
                    # Stacks/Other: Standard extraction (handle both separators safely)
                    filename = re.split(r"[\\/]", book_download["filename"])[-1]

                book_title = book_download["title"]
                series_title = book_download.get("seriesTitle", "")
                book_id = book_download["bookId"]
                source_id = book_download.get("source_id", book_download.get("username", ""))

                logger.info(f"Processing file: {filename} for book: {book_title}")
                source_file_path = os.path.join(local_download_dir, folder, filename)

                if not os.path.exists(source_file_path):
                    logger.error(f"Source file not found: {source_file_path}")
                    failed_imports.append((folder, filename, author_name_sanitized, f"Source file not found: {source_file_path}"))
                    if ctx.history:
                        ctx.history.add_failure(source_id, book_title, "Source file not found")
                    continue

                # 1. Validate Metadata (skip for stacks backend - trust AA matching)
                skip_validation = backend_name == "stacks"
                if skip_validation:
                    logger.info(f"Skipping metadata validation for stacks backend: {filename}")
                    validation_passed = True
                else:
                    validation_passed = validate_metadata(source_file_path, book_title, book_id, ctx, series_title, author_name)

                if validation_passed:
                    # 2. Organize File
                    if organize_file(source_file_path, author_name_sanitized, filename, folder, local_download_dir):
                        logger.info(f"Successfully processed {filename}")
                        author_folders.add(author_name_sanitized)
                        folder_books.setdefault(author_name_sanitized, []).append(book_id)
                        folder_files.setdefault(author_name_sanitized, []).append(os.path.join(local_download_dir, author_name_sanitized, filename))
                    else:
                        failed_imports.append((folder, filename, author_name_sanitized, "Failed to organize file"))
                        if ctx.history:
                            ctx.history.add_failure(source_id, book_title, "Failed to organize file")
                else:
                    logger.warning(f"Metadata validation failed for {filename}")
                    failed_imports.append((folder, filename, author_name_sanitized, "Metadata validation failed"))
                    if ctx.history:
                        ctx.history.add_failure(source_id, book_title, "Metadata validation failed")

            except Exception:
                logger.exception(f"Unexpected error processing {book_download.get('filename', 'unknown')}")
                failed_imports.append((book_download.get("dir", "unknown"), book_download.get("filename", "unknown"), book_download.get("author_name", "unknown"), "Unexpected error"))
                if ctx.history:
                    sid = book_download.get("source_id", book_download.get("username", ""))
                    t = book_download.get("title", "")
                    if sid and t:
                        ctx.history.add_failure(sid, t, "Unexpected processing error")

        # Handle failed imports
        if failed_imports:
            logger.warning(f"{len(failed_imports)} files failed validation/processing")

            for folder, filename, author_name_sanitized, error_reason in failed_imports:
                logger.warning(f"Failed: {filename} - Reason: {error_reason}")

                failed_imports_dir = os.path.join(local_download_dir, "failed_imports")
                try:
                    if not os.path.exists(failed_imports_dir):
                        os.makedirs(failed_imports_dir)
                        logger.info(f"Created failed imports directory: {failed_imports_dir}")

                    target_path = os.path.join(failed_imports_dir, author_name_sanitized)
                    counter = 1
                    while os.path.exists(target_path):
                        target_path = os.path.join(failed_imports_dir, f"{author_name_sanitized}_{counter}")
                        counter += 1

                    os.makedirs(target_path, exist_ok=True)

                    source_file_path = os.path.join(local_download_dir, folder, filename)
                    if os.path.exists(source_file_path):
                        shutil.move(source_file_path, target_path)
                        logger.info(f"Moved failed file to: {target_path}")

                        abs_folder = os.path.join(local_download_dir, folder)
                        if os.path.exists(abs_folder) and not os.listdir(abs_folder):
                            shutil.rmtree(abs_folder)

                except Exception as e:
                    logger.error(f"Failed to move failed import: {e}")

        # Trigger imports for this backend's successful folders using the mapped path
        if author_folders:
            logger.info(f"Triggering imports for backend {backend_name} using path: {readarr_download_dir}")
            commands = trigger_imports(readarr, readarr_download_dir, list(author_folders))
            poll_timeout = ctx.config.getfloat("Readarr", "import_poll_timeout", fallback=IMPORT_TIMEOUT_SECONDS)
            command_results = monitor_imports(readarr, commands, readarr_download_dir, local_download_dir, timeout=poll_timeout) if commands else {}
            command_for = {c.get("_rsoul_folder"): c for c in commands}

            for folder in author_folders:
                command = command_for.get(folder)
                if command is None:
                    # Readarr/Chaptarr didn't accept the import (unreachable?): keep the files
                    # staged and submit again on the next run instead of losing track of them
                    outcome = pending_import_record(folder, readarr_download_dir, local_download_dir, folder_files.get(folder, []), None)
                else:
                    ok = command_results.get(command["id"], False)
                    if ok is None:
                        outcome = pending_import_record(folder, readarr_download_dir, local_download_dir, folder_files.get(folder, []), command["id"])
                    elif ok:
                        outcome = files_left_after_import(folder_files.get(folder, []), os.path.join(local_download_dir, folder)) is None
                    else:
                        outcome = False
                for book_id in folder_books.get(folder, []):
                    results[book_id] = outcome

        else:
            logger.warning(f"No successful imports for backend {backend_name}")

    if not items_by_backend:
        logger.warning("No author folders found to import")

    return results
