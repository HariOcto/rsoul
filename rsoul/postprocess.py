import os
import shutil
import logging
import re
import difflib
import time
import operator
from typing import Any

from mobi_header import MobiHeader
import ebookmeta
from .utils import sanitize_folder_name, jaccard_similarity, extract_author_title
from .display import print_import_summary, print_section_header

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

    try:
        os.makedirs(target_dir, exist_ok=True)
        for name in expected:
            shutil.move(os.path.join(source_dir, name), os.path.join(target_dir, name))
        logger.info(f"Moved {len(expected)} audiobook files to {target_dir}")
    except Exception as e:
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


def monitor_imports(readarr_client: Any, commands: list, readarr_download_dir: str = "", local_download_dir: str = "") -> None:
    """Monitor progress of Readarr import commands and report results."""
    if not commands:
        return

    logger.info("Monitoring import progress...")
    while True:
        completed_count = 0

        for task in commands:
            try:
                current_task = readarr_client.get_command(task["id"])
                if current_task["status"] in ["completed", "failed"]:
                    completed_count += 1
            except Exception as e:
                logger.error(f"Error checking task {task['id']}: {e}")
                completed_count += 1  # Count as completed to avoid infinite loop

        if completed_count == len(commands):
            break

        time.sleep(2)

    # Report final results
    logger.info("Import Results:")
    for task in commands:
        try:
            current_task = readarr_client.get_command(task["id"])
            status = current_task.get("status", "unknown")

            if "body" in current_task and "path" in current_task["body"]:
                path = current_task["body"]["path"]
                folder_name = os.path.basename(path)
            else:
                folder_name = f"Task {task['id']}"

            message = current_task.get("message", "")

            if status == "completed":
                # Check for failure keywords in message even if status is completed
                # "No files found" is a common message when import finds nothing
                if "failed" in message.lower() or "no files found" in message.lower():
                    logger.warning(f"{folder_name}: Import completed with warnings/errors: {message}")
                    if "body" in current_task and "path" in current_task["body"]:
                        move_failed_import(to_local_path(current_task["body"]["path"], readarr_download_dir, local_download_dir))
                else:
                    logger.info(f"{folder_name}: Import completed. Message: {message}")

            elif status == "failed":
                logger.error(f"{folder_name}: Import failed")
                if "message" in current_task:
                    logger.error(f"Error message: {current_task['message']}")

                # Move failed import
                if "body" in current_task and "path" in current_task["body"]:
                    move_failed_import(to_local_path(current_task["body"]["path"], readarr_download_dir, local_download_dir))
            else:
                logger.warning(f"{folder_name}: Import status unknown - {status}")

        except Exception as e:
            logger.error(f"Error processing task result {task['id']}: {e}")


def process_imports(ctx: Any, grab_list: list):
    """Process downloaded files, validate metadata, and trigger Readarr import.

    Handles items from multiple backends by grouping them and using backend-specific paths.
    """
    print_section_header("METADATA VALIDATION & IMPORT PHASE")

    readarr_disable_sync = ctx.config.getboolean("Readarr", "disable_sync", fallback=False)

    # Legacy fallback
    default_slskd_dir = ctx.config.get("Slskd", "download_dir", fallback="")
    default_readarr_dir = ctx.config.get("Slskd", "readarr_download_dir", fallback=default_slskd_dir)

    readarr = ctx.readarr

    # Check if sync is disabled first
    if readarr_disable_sync:
        logger.warning("Readarr sync is disabled in config. Skipping import phase.")
        logger.info(f"Files downloaded but not imported.")
        return

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

        for book_download in items:
            if book_download.get("media_type") == "audiobook":
                # Whole folder of audio files: no per-file metadata validation, one import per book folder
                book_title = book_download.get("title", "")
                source_id = book_download.get("source_id", book_download.get("username", ""))
                logger.info(f"Processing audiobook: {book_title}")
                relative, reason = organize_audiobook(book_download, local_download_dir)
                if relative:
                    author_folders.add(relative)
                else:
                    logger.warning(f"Audiobook failed: {book_title} - {reason}")
                    source_dir = os.path.join(local_download_dir, book_download.get("dir", ""))
                    if book_download.get("dir") and os.path.isdir(source_dir):
                        move_failed_import(source_dir, local_download_dir)
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
            if commands:
                monitor_imports(readarr, commands, readarr_download_dir, local_download_dir)

        else:
            logger.warning(f"No successful imports for backend {backend_name}")

    if not items_by_backend:
        logger.warning("No author folders found to import")
