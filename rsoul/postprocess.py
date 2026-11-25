import os
import shutil
import logging
import re
import difflib
import time
import math
from contextlib import nullcontext
from typing import Any, Optional

from mobi_header import MobiHeader
import ebookmeta
from .utils import sanitize_folder_name, jaccard_similarity, extract_author_title
from .display import print_import_summary, print_section_header
from . import health
from . import audiotags
from . import import_identity
from .library import CatalogUnavailable, author_series, require_author_catalog, sibling_titles
from .match import book_narrators

logger = logging.getLogger("readarr_soul")


def move_failed_import(src_path: str, base_dir: str = ""):
    """Move failed import to failed_imports directory with better error handling.

    Args:
        src_path: Path to the source file/folder to move
        base_dir: Base directory for the failed_imports folder. If empty, uses
                  the parent directory of src_path.
    """
    try:
        staging_parent = None
        if not base_dir:
            # An audiobook is imported from <download_dir>/rsoul_audiobooks/<Author>/<Title>. Its
            # failed imports belong in <download_dir>/failed_imports like all others, not in a
            # failed_imports folder inside the staging area.
            parts = os.path.normpath(src_path).split(os.sep)
            if AUDIOBOOK_STAGING_DIR in parts[:-1]:
                base_dir = os.sep.join(parts[: parts.index(AUDIOBOOK_STAGING_DIR)]) or os.sep
                staging_parent = os.path.dirname(os.path.normpath(src_path))
            else:
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
            # Don't leave an empty <Author> folder behind in the staging area
            if staging_parent and os.path.isdir(staging_parent) and not os.listdir(staging_parent):
                os.rmdir(staging_parent)
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


class FileMoveIncomplete(OSError):
    """Some named files could not be isolated; retain the caller's ownership journal."""

    def __init__(self, message, destination, moved, remaining):
        super().__init__(message)
        self.destination = destination
        self.moved = list(moved)
        self.remaining = list(remaining)


def move_files_aside(base_dir: str, folder: str, names: list, bucket: str = "failed_imports", *, destination=None, before_move=None) -> str:
    """Move just these files from <base_dir>/<folder> into <base_dir>/<bucket>/<folder>[_n].

    Only the named files are moved: slskd puts downloads from different peers that share a
    folder name into the same local folder, so moving the whole folder could take other
    books' files with it. Returns the destination folder ("" if nothing was moved).
    A partial move raises FileMoveIncomplete with its destination and moved names.
    The optional destination lets callers checkpoint the exact recovery path first.
    """
    import stat
    from .persistence import StatePersistenceError, fsync_directory

    source_dir = os.path.join(base_dir, folder)
    names = list(dict.fromkeys(names))
    target_dir = destination or os.path.join(base_dir, bucket, os.path.basename(folder.rstrip(os.sep)) or "unknown")
    moved = []
    try:
        if any(not n or n in (".", "..") or os.path.basename(n) != n or "\\" in n for n in names):
            raise ValueError("Refusing to isolate unsafe filenames")
        if os.path.islink(source_dir):
            raise ValueError("Refusing to follow a symlink as the source directory")
        if os.path.commonpath((os.path.abspath(target_dir), os.path.abspath(os.path.join(base_dir, bucket)))) != os.path.abspath(os.path.join(base_dir, bucket)):
            raise ValueError("Recovery destination is outside the requested bucket")
        present = []
        for name in names:
            try:
                mode = os.lstat(os.path.join(source_dir, name)).st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(mode):
                raise ValueError(f"Expected a regular file for {name}")
            present.append(name)
        if not present:
            return ""
        if destination is None:
            counter = 1
            original = target_dir
            while os.path.lexists(target_dir):
                target_dir = f"{original}_{counter}"
                counter += 1
        if before_move:
            before_move()
        if os.path.islink(target_dir):
            raise ValueError("Recovery destination is a symlink")
        os.makedirs(target_dir, exist_ok=True)
        for name in present:
            if before_move:
                before_move()
            if os.path.lexists(os.path.join(target_dir, name)):
                raise FileExistsError(f"Recovery destination already contains {name}")
            shutil.move(os.path.join(source_dir, name), os.path.join(target_dir, name))
            moved.append(name)
        remaining = [name for name in names if os.path.lexists(os.path.join(source_dir, name))]
        if remaining:
            raise OSError("Named files remain in the source directory")
        _mark_quarantined(target_dir, base_dir)
        fsync_directory(target_dir)
        fsync_directory(os.path.dirname(target_dir))
        fsync_directory(source_dir)
        logger.info(f"Moved {len(present)} file(s) to {target_dir}")
        try:
            if os.path.abspath(source_dir) != os.path.abspath(base_dir) and not os.listdir(source_dir):
                os.rmdir(source_dir)
        except OSError:
            pass  # Empty-directory tidying does not change named-file ownership.
    except StatePersistenceError:
        raise
    except Exception as e:
        remaining = [name for name in names if os.path.lexists(os.path.join(source_dir, name))]
        raise FileMoveIncomplete(f"Failed to isolate named files in {target_dir}: {e}", target_dir, moved, remaining) from e
    return target_dir


QUARANTINE_BUCKETS = ("failed_imports", "failed_downloads")
QUARANTINE_MARKER = ".rsoul-quarantined"


def _mark_quarantined(target_dir: str, base_dir: str) -> None:
    """Record when R:soul set a folder aside. A moved folder keeps its old modification time,
    so the retention clock (failed_retention_days) runs from this marker, never from mtime."""
    parent = os.path.basename(os.path.dirname(os.path.abspath(target_dir)))
    if parent not in QUARANTINE_BUCKETS or os.path.dirname(os.path.dirname(os.path.abspath(target_dir))) != os.path.abspath(base_dir):
        return
    marker = os.path.join(target_dir, QUARANTINE_MARKER)
    if not os.path.lexists(marker):
        try:
            with open(marker, "w", encoding="utf-8") as out:
                out.write(f"{time.time():.0f}\n")
        except OSError as exc:
            logger.debug(f"Could not write the quarantine marker in {target_dir}: {exc}")


_OWNED_PATH_FIELDS = {
    "local_dir", "dir", "folder", "source_dir", "stage_dir", "target_dir", "destination_dir",
    "source_path", "stage_path", "readarr_path", "path", "source", "destination",
    "files", "retained_inputs", "candidate_directories", "expected_paths", "server_paths", "server_folders",
}


def _quarantine_ownership(base_dir: str, state: Any):
    """Return normalized owned paths, or None when the ownership inventory is unsafe.

    Inspect structured path fields, not JSON text: serialization escapes Unicode, quotes
    and backslashes. Configuration mappings such as download_root do not own their subtree.
    Both native paths and saved Windows-style separators are considered conservatively.
    """
    if state is None or getattr(state, "read_only", False) or getattr(state, "load_error", None):
        return None
    try:
        state_path = getattr(state, "filepath", None)
        if state_path is not None and not os.path.isfile(state_path):
            return None
        read = getattr(state, "get_items", None) or getattr(state, "get_tasks_for_orchestrator", None)
        if not callable(read):
            return None
        records = read()
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            return None
        references = set()

        def add_path(value):
            if not value:
                return
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError("Invalid owned path in saved state")
            for spelling in {value, value.replace("\\", "/")}:
                path = os.path.abspath(os.path.join(base_dir, spelling))
                references.update((os.path.normcase(path), os.path.normcase(os.path.realpath(path))))
                # Server and older host mappings may have another prefix. An exact bucket
                # component still identifies a potentially owned quarantine destination.
                parts = spelling.split("/")
                for index, part in enumerate(parts):
                    if part in QUARANTINE_BUCKETS:
                        mapped = os.path.abspath(os.path.join(base_dir, *parts[index:]))
                        references.add(os.path.normcase(mapped))

        def visit(value, owned=False):
            if isinstance(value, dict):
                for key, child in value.items():
                    visit(child, key in _OWNED_PATH_FIELDS)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    visit(child, owned)
            elif owned and value is not None:
                add_path(value)

        visit(records)
        return references
    except Exception as exc:
        logger.warning("Quarantine retention paused: saved ownership cannot be inspected (%s)", exc)
        return None


def _quarantine_is_owned(directory: str, base_dir: str, references: set) -> bool:
    directory = os.path.normcase(os.path.abspath(directory))
    base_dir = os.path.normcase(os.path.abspath(base_dir))
    for path in references:
        try:
            shared = os.path.commonpath((directory, path))
            # A path inside the folder owns it. An explicit reference to the bucket also
            # protects its contents; a download mount by itself is not a recursive claim.
            if shared == directory or (shared == path and path != base_dir
                                       and os.path.commonpath((base_dir, path)) == base_dir):
                return True
        except ValueError:
            continue
    return False


def prune_quarantine(base_dir: str, retention_days: float, state: Any = None, now: Optional[float] = None,
                     *, before_mutation=None) -> int:
    """[Postprocessing] failed_retention_days (0 = off): delete folders in failed_imports and
    failed_downloads that R:soul set aside more than that many days ago.

    - The age comes from the marker R:soul writes when it sets a folder aside. A folder without
      one (older versions, or put there by hand) gets a marker now and is kept: its clock starts.
    - A folder that any saved task still refers to is never deleted. Missing, unreadable or
      read-only state pauses retention instead of assuming that no task owns the files.
    - Only real folders directly inside the two buckets; symlinks are never followed.
    """
    if not base_dir or not retention_days or not math.isfinite(retention_days) or retention_days <= 0:
        return 0
    base_dir = os.path.abspath(base_dir)
    if _quarantine_ownership(base_dir, state) is None:
        return 0
    now = time.time() if now is None else now
    removed = 0
    for bucket in QUARANTINE_BUCKETS:
        root = os.path.join(base_dir, bucket)
        if not os.path.isdir(root) or os.path.islink(root):
            continue
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                marker = os.path.join(entry.path, QUARANTINE_MARKER)
                if os.path.islink(marker):
                    continue
                try:
                    with open(marker, encoding="utf-8") as stream:
                        since = float(stream.read().strip() or "nan")
                except (OSError, ValueError):
                    if before_mutation:
                        before_mutation()
                    _mark_quarantined(entry.path, base_dir)
                    continue
                if not math.isfinite(since) or now - since < retention_days * 86400:
                    continue
                # StateManager uses an RLock. Keep the final live inventory and deletion
                # together so a foreground callback cannot claim a folder between them.
                with getattr(state, "_lock", None) or nullcontext():
                    references = _quarantine_ownership(base_dir, state)
                    if references is None:
                        return removed
                    if _quarantine_is_owned(entry.path, base_dir, references):
                        continue
                    if before_mutation:
                        before_mutation()
                    shutil.rmtree(entry.path)
                removed += 1
                logger.info(f"Deleted {entry.path}: set aside {(now - since) / 86400:.0f} days ago (failed_retention_days = {retention_days:g})")
            except OSError as exc:
                logger.warning(f"Could not delete the old quarantine folder {entry.path}: {exc}")
    return removed


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


IMPORT_METHODS = ("scan", "targeted")


def import_method(config: Any) -> str:
    """[Readarr] import_method: "targeted" (default) or explicit legacy "scan".

    scan      hands the folder to Readarr/Chaptarr, which works out from the files' tags and
              names which book they are. It may decide differently from R:soul: file a book
              under another title of the same series, or create a new record and leave the
              wanted one wanted.
    targeted  requests a manual import naming the intended book and selected edition.
              Success requires every manifest input correlated to that book's outputs.
    """
    method = config.get("Readarr", "import_method", fallback="targeted").strip().lower()
    return method if method in IMPORT_METHODS else "targeted"


def targeted_import_command(readarr_client: Any, readarr_path: str, book_id: Any, filenames: list,
                            media_type: str = "ebook", *, transaction: Optional[dict] = None,
                            config: Any = None, persist: Any = None) -> Optional[dict]:
    """Compatibility entry point requiring an already durable transaction.

    New callers should use process_imports. The old signature alone no longer
    submits a request whose acceptance could be lost without a recovery record.
    Targeted imports never fall back to a folder scan.
    """
    from .import_transaction import submit_prepared
    if transaction is None or config is None or (persist is None and not transaction.get("journal_path")):
        logger.error("Targeted import requires a durable input transaction and active configuration; no command submitted")
        return None
    names = [f.get("name") for f in transaction.get("manifest", [])]
    if (transaction.get("method") != "targeted" or str(transaction.get("book_id")) != str(book_id)
            or transaction.get("media_type") != media_type
            or import_identity.path_key(transaction.get("readarr_path")) != import_identity.path_key(readarr_path)
            or len(names) != len(filenames) or set(names) != set(filenames)):
        logger.error("Targeted import arguments differ from the durable manifest; no command submitted")
        return None
    updated = submit_prepared(readarr_client, transaction, config, persist)
    if updated.get("command_id") is None:
        return None
    return {"id": updated["command_id"], "_rsoul_method": "targeted", "_rsoul_book_id": book_id,
            "_rsoul_edition_id": updated.get("edition_id"), "_rsoul_before_ids": updated.get("before_ids"),
            "_rsoul_manifest": updated.get("manifest"), "_rsoul_media_type": media_type}


def wanted_record_still_empty(readarr_client: Any, book_id: Any) -> bool:
    """After a successful import: does the record that was wanted still have no files?

    True only if Readarr/Chaptarr says so in as many words. It means the files went to
    another record (a duplicate, another book of the series, a newly created one).
    """
    try:
        book = readarr_client.get_book(book_id)
    except Exception as e:
        logger.debug(f"Could not check book {book_id} after its import: {e}")
        return False
    if not isinstance(book, dict):
        return False
    if "hasFiles" in book:
        return book["hasFiles"] is False
    statistics = book.get("statistics")
    if isinstance(statistics, dict) and "bookFileCount" in statistics:
        return statistics["bookFileCount"] == 0
    return False


def trigger_imports(readarr_client: Any, readarr_download_dir: str, author_folders: list,
                    method: str = "targeted", folder_books: Optional[dict] = None,
                    folder_files: Optional[dict] = None, folder_media: Optional[dict] = None,
                    *, transactions: Optional[dict] = None, config: Any = None, persist: Any = None) -> list:
    """Compatibility dispatcher: every submitted folder must have a durable manifest.

    process_imports owns new transactions. This function no longer creates
    untracked requests from folder names, including explicit legacy scans.
    """
    from .import_transaction import submit_prepared
    commands = []
    for folder in author_folders:
        transaction = (transactions or {}).get(folder)
        books = (folder_books or {}).get(folder, [])
        if (not isinstance(transaction, dict) or config is None or len(set(books)) != 1
                or str(transaction.get("book_id")) != str(books[0])
                or transaction.get("method") != method):
            logger.error("Import of %s lacks one matching durable transaction; no command submitted", folder)
            continue
        if persist is None and not transaction.get("journal_path"):
            logger.error("Import of %s lacks a durable checkpoint writer; no command submitted", folder)
            continue
        updated = submit_prepared(readarr_client, transaction, config, persist)
        if updated.get("command_id") is not None:
            commands.append({"id": updated["command_id"], "_rsoul_folder": folder,
                             "_rsoul_readarr_path": updated["readarr_path"], "_rsoul_method": method})
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


TAG_CHECK_MODES = ("reject", "warn", "off")


def audiobook_tag_check_mode(config: Any) -> str:
    """[Postprocessing] audiobook_tag_check: reject (default), warn or off. skip_validation = True
    switches it off together with the ebook checks."""
    if config.getboolean("Postprocessing", "skip_validation", fallback=False):
        return "off"
    mode = config.get("Postprocessing", "audiobook_tag_check", fallback="reject").strip().lower()
    return mode if mode in TAG_CHECK_MODES else "reject"


def audiobook_tags_contradict(ctx: Any, book_download: dict, local_download_dir: str) -> str:
    """Check a downloaded audiobook's audio tags against the wanted book (see audiotags.py).

    Returns a proven conflicting tag in reject mode, otherwise "". In reject
    mode, unavailable book/catalogue evidence raises CatalogUnavailable so the
    durable transaction can retry validation without moving or importing files.
    """
    mode = audiobook_tag_check_mode(ctx.config)
    if mode == "off":
        return ""
    try:
        title = book_download.get("title", "")
        author_name = book_download.get("author_name", "")
        if book_download.get("local_paths"):
            # The exact files about to be imported (they may sit in two folders mid-staging)
            paths = [p for p in book_download["local_paths"] if os.path.isfile(p)]
        else:
            folder = os.path.join(local_download_dir, book_download.get("dir", ""))
            names = book_download.get("expected_files") or [f["filename"].split("\\")[-1] for f in book_download.get("files", [])]
            paths = [os.path.join(folder, name) for name in names if os.path.isfile(os.path.join(folder, name))]
        if not paths:
            return ""

        # What Chaptarr knows about the book: series position, narrators, the author's other titles
        book: dict = {}
        try:
            fetched = ctx.readarr.get_book(book_download.get("bookId")) if ctx.readarr else None
            book = fetched if isinstance(fetched, dict) else {}
        except Exception as e:
            if mode == "reject":
                raise CatalogUnavailable(f"Cannot fetch the intended book for its tag check: {e}") from e
            logger.debug(f"Could not fetch book {book_download.get('bookId')} for the tag check: {e}")
        if mode == "reject":
            require_author_catalog(ctx, book)
        series_title = book.get("seriesTitle") or book_download.get("seriesTitle") or ""
        siblings = sibling_titles(ctx, dict(book, title=title), author_name) if book.get("authorId") else []
        series = author_series(ctx, book)

        verdict, detail = audiotags.check_audiobook_tags(paths, title, author_name, series_title, book_narrators(book), siblings, series)
        if verdict == audiotags.MISMATCH:
            if mode == "warn":
                logger.warning(f"Audio tags of {title} name another book ({detail}); importing anyway (audiobook_tag_check = warn)")
                return ""
            return detail
        logger.info(f"Audio tag check for {title}: {'tags name this book' if verdict == audiotags.AGREE else 'no usable tags' if verdict == audiotags.UNTAGGED else 'tags are inconclusive'}")
    except CatalogUnavailable:
        raise
    except Exception as exc:
        if mode == "reject":
            raise CatalogUnavailable(f"Audio tag comparison is unavailable: {exc}") from exc
        logger.exception(f"Audio tag check failed for {book_download.get('title', 'audiobook')}; continuing without it")
    return ""


def import_failure_retry_days(config: Any) -> float:
    """Legacy setting retained for compatibility; local import failures do not block peers."""
    return config.getfloat("Postprocessing", "import_failure_retry_days", fallback=IMPORT_FAILURE_RETRY_DAYS)


IMPORT_FAILURE_RETRY_DAYS = 7

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
        running (or Readarr/Chaptarr couldn't be reached) when the wait ended}.
        This reports command status only; all source files remain in place.
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
        # ManualImport commands commonly have files[] rather than body.path. Rely on our
        # own staged folder, not on an optional server body which may omit the path.
        path = task.get("_rsoul_readarr_path") or body.get("path", "")
        folder_name = os.path.basename(path) if path else f"Task {task['id']}"
        message = current_task.get("message", "")

        results[task["id"]] = _import_succeeded(current_task)
        if results[task["id"]]:
            logger.info(f"{folder_name}: Import command finished (status: {current_task.get('status')}, result: {current_task.get('result', 'n/a')}). Message: {message or '-'}")
            continue

        logger.warning(f"{folder_name}: Import did not succeed (status: {current_task.get('status')}, result: {current_task.get('result', 'n/a')}): {message}")
        logger.warning("Source files are retained; command status alone does not prove the import outcome")

    return results


IMPORT_SETTLE_SECONDS = 30


def files_left_after_import(paths: list, folder: str, settle: Optional[float] = None):
    """Double-check a reported import: Readarr and Chaptarr move imported files out of the
    download folder. This legacy helper reports remaining sources only; their
    presence or absence does not prove whether all, some, or none were imported.

    Waits up to `settle` seconds for the files to go. Returns the list of files still there
    (logged for inspection), or None if all source paths have disappeared.
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
        "the command outcome needs correlated output verification. Files are retained for review."
    )
    return remaining


# How long an import may stay unconfirmed across runs before R:soul gives up on it
PENDING_IMPORT_MAX_AGE = 24 * 3600
PENDING_IMPORT_MAX_SUBMITS = 3


def pending_import_record(folder: str, readarr_download_dir: str, local_download_dir: str, files: list, command_id: Optional[int],
                          method: str = "scan", book_id: Any = None, media_type: str = "", edition_id: Any = None,
                          before_ids: Optional[list] = None) -> dict:
    """Everything needed to finish an import on a later run."""
    return {
        "folder": folder,
        "local_dir": os.path.join(local_download_dir, folder),
        "readarr_path": os.path.join(readarr_download_dir, folder),
        "files": list(files),
        "command_id": command_id,
        "submits": 1 if command_id is not None else 0,
        "since": time.time(),
        "method": method,
        "book_id": book_id,
        "media_type": media_type,
        "edition_id": edition_id,
        "before_ids": before_ids,
    }


def check_pending_import(readarr_client: Any, pending: dict, now: Optional[float] = None,
                         config: Any = None, persist: Any = None):
    """Reconcile durable imports; retained dictionaries describe partial/unknown/review work.

    A new POST or a staging move requires both config (including disable_sync)
    and a durable persistence writer. Complete correlated evidence is the only
    success condition. Uncertain work never moves to failed_imports automatically.
    """
    from .import_transaction import check_pending
    return check_pending(readarr_client, pending, now, config, persist)


def recover_pending_imports(ctx: Any) -> int:
    """Recover standalone write-ahead import sidecars without issuing an import."""
    from .import_transaction import recover_pending_imports as recover
    return recover(ctx)


def preserve_unresumable_task(ctx: Any, item: dict, reason: str) -> dict:
    """Keep legacy staged or orphan work available for explicit reconciliation."""
    from .import_transaction import preserve_unresumable_task as preserve
    return preserve(ctx, item, reason)


def process_imports(ctx: Any, grab_list: list, *, poll: bool = True) -> dict:
    """Validate, durably stage, submit and reconcile one transaction per wanted book.

    True means complete correlated import.
    A dictionary retains recoverable work and its diagnosis. Local filesystem,
    API, identity, or import failures do not penalize the supplying peer. poll=False
    leaves accepted commands to the workflow's serialized background coordinator.
    """
    from .import_transaction import check_pending, context_writer, process_one
    from .outcomes import check_fatal_state
    check_fatal_state(ctx)
    print_section_header("METADATA VALIDATION & IMPORT PHASE")
    results = {}
    submitted = {}
    default_root = ctx.config.get("Slskd", "download_dir", fallback="")
    default_server = ctx.config.get("Slskd", "readarr_download_dir", fallback=default_root)
    prune_quarantine(default_root, ctx.config.getfloat("Postprocessing", "failed_retention_days", fallback=0),
                     getattr(ctx, "state", None), before_mutation=lambda: check_fatal_state(ctx))
    for item in grab_list:
        root, server = default_root, default_server
        orchestrator = getattr(ctx, "orchestrator", None)
        if orchestrator:
            backend = orchestrator.get_backend(item.get("backend_name", "slskd"))
            if backend:
                root = backend.download_dir or root
                server = backend.readarr_download_dir or server
        method = import_method(ctx.config)
        if method == "scan":
            logger.warning("Legacy folder scan may select another record; complete intended-book verification is still required")
        outcome = process_one(ctx, item, root, server, method, poll=False)
        if poll and isinstance(outcome, dict) and outcome.get("phase") in ("importing", "submission_unknown"):
            submitted[item.get("bookId")] = (item, outcome)  # followed below, together with the others
        else:
            results[item.get("bookId")] = outcome

    # Every import of this batch is submitted before any is waited for, so one slow import
    # no longer holds up the submission of the next.
    timeout = ctx.config.getfloat("Readarr", "import_poll_timeout", fallback=600)
    deadline = time.monotonic() + max(0, timeout)
    while submitted:
        for book_id, (item, pending) in list(submitted.items()):
            outcome = check_pending(ctx.readarr, pending, config=ctx.config, persist=context_writer(ctx, item, pending))
            if outcome is True or not isinstance(outcome, dict) or outcome.get("phase") != "importing":
                results[book_id] = outcome
                del submitted[book_id]
            else:
                submitted[book_id] = (item, outcome)
        if not submitted:
            break
        if time.monotonic() >= deadline:
            results.update({book_id: pending for book_id, (_, pending) in submitted.items()})
            break
        health.heartbeat()
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    return results
