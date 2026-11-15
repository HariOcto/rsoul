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
from . import audiotags
from . import import_identity
from .library import author_series, sibling_titles
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


IMPORT_METHODS = ("scan", "targeted")


def import_method(config: Any) -> str:
    """[Readarr] import_method: "scan" (default) or "targeted".

    scan      hands the folder to Readarr/Chaptarr, which works out from the files' tags and
              names which book they are. It may decide differently from R:soul: file a book
              under another title of the same series, or create a new record and leave the
              wanted one wanted.
    targeted  requests a manual import naming the wanted book and correctly typed edition;
              only NEW bookfile records on that exact book/edition confirm its success.
    """
    method = config.get("Readarr", "import_method", fallback="scan").strip().lower()
    return method if method in IMPORT_METHODS else "scan"


def targeted_import_command(readarr_client: Any, readarr_path: str, book_id: Any, filenames: list, media_type: str = "ebook") -> Optional[dict]:
    """Start an import of the files in readarr_path for exactly this book.

    Does what the "Manual Import" dialog does: ask for the importable files of the folder
    (which also gives each file's quality), fill in author, book and edition, and post that
    as a ManualImport command. Returns the command with verification metadata, or None
    if it cannot be prepared. NEVER fall back to a folder scan in targeted mode.
    """
    book = readarr_client.get_book(book_id)
    author_id = book.get("authorId") if isinstance(book, dict) else None
    editions = readarr_client.get_edition(book_id)
    edition = import_identity.select_edition(book or {}, editions, media_type)
    if not author_id or not edition:
        logger.warning(f"Targeted import not possible for book {book_id}: {'no edition' if author_id else 'no author'} in Readarr/Chaptarr")
        return None

    preview = readarr_client._get("manualimport", readarr_client.ver_uri, params={"folder": readarr_path, "filterExistingFiles": "false"})
    wanted = set(filenames)
    if not wanted or len(wanted) != len(filenames) or any(os.path.basename(n) != n for n in wanted):
        logger.warning("Targeted import cannot safely identify all file names for book %s", book_id)
        return None
    files = []
    for item in preview if isinstance(preview, list) else []:
        path = item.get("path") if isinstance(item, dict) else None
        # Only the files R:soul staged for this book, and only ones Readarr/Chaptarr can rate
        if (
            not isinstance(path, str)
            or os.path.normpath(os.path.dirname(path.replace("\\", "/"))) != os.path.normpath(readarr_path)
            or os.path.basename(path.replace("\\", "/")) not in wanted
            or not item.get("quality")
        ):
            continue
        files.append(
            {
                "path": path,
                "authorId": author_id,
                "bookId": book_id,
                "editionId": edition["id"],  # Chaptarr
                "foreignEditionId": edition.get("foreignEditionId"),  # Readarr
                "quality": item["quality"],
                "releaseGroup": item.get("releaseGroup"),
                "indexerFlags": item.get("indexerFlags") or 0,
                "disableReleaseSwitching": True,  # keep the edition chosen here
            }
        )
    if len(files) != len(wanted) or {os.path.basename(f["path"].replace("\\", "/")) for f in files} != wanted:
        logger.warning(f"Targeted import not possible for book {book_id}: Readarr/Chaptarr lists {len(files)} of {len(wanted)} file(s) in {readarr_path} as importable")
        return None

    # Capture BEFORE starting the command. Existing book files must not later be mistaken
    # for files this import attached to the wanted record.
    before_ids = import_identity.bookfile_ids_before(readarr_client, book_id)
    if before_ids is None:
        logger.error("Targeted import of book %s not submitted: its bookfile IDs cannot be read and the destination cannot be verified", book_id)
        return None
    logger.info(f"Importing {len(files)} file(s) for book {book_id} ('{book.get('title', '?')}', edition {edition['id']})")
    command = readarr_client.post_command(name="ManualImport", files=files, importMode="move", replaceExistingFiles=False)
    if not isinstance(command, dict) or command.get("id") is None:
        raise ValueError("Chaptarr did not return an import command ID; the import outcome is unknown")
    command.update({
        "_rsoul_method": "targeted", "_rsoul_book_id": book_id, "_rsoul_edition_id": edition["id"],
        "_rsoul_before_ids": before_ids, "_rsoul_media_type": media_type,
    })
    return command


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


def trigger_imports(readarr_client: Any, readarr_download_dir: str, author_folders: list, method: str = "scan", folder_books: Optional[dict] = None, folder_files: Optional[dict] = None, folder_media: Optional[dict] = None) -> list:
    """
    Trigger Readarr import commands for processed folders.
    Returns a list of command objects.

    method "targeted" imports a folder that holds one book for exactly that book (see
    import_method). Any ambiguity or API failure in targeted mode keeps the staged files
    pending for a later attempt or manual intervention. It must never trigger a scan.
    """
    commands = []
    if not author_folders:
        return commands

    logger.info("Starting Readarr import commands...")
    for author_folder in author_folders:
        try:
            download_dir = os.path.join(readarr_download_dir, author_folder)
            logger.info(f"Importing from: {download_dir}")

            command = None
            books = (folder_books or {}).get(author_folder, [])
            if method == "targeted":
                if len(books) != 1:
                    logger.error("Targeted import of %s requires exactly one book ID; %s supplied. Keeping staged files.", author_folder, len(books))
                    continue
                try:
                    names = [os.path.basename(p) for p in (folder_files or {}).get(author_folder, [])]
                    media_type = (folder_media or {}).get(author_folder)
                    if not media_type:
                        media_type = "audiobook" if names and all(os.path.splitext(n)[1].lower() in (".m4b", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav") for n in names) else "ebook"
                    command = targeted_import_command(readarr_client, download_dir, books[0], names, media_type)
                except Exception as e:
                    logger.warning("Targeted import could not be submitted for %s (%s); keeping staged files", author_folder, e)
                if command is None:
                    logger.warning("Targeted import of %s was not submitted; no folder scan fallback", author_folder)
                    continue
            else:
                command = readarr_client.post_command(name="DownloadedBooksScan", path=download_dir)
                command["_rsoul_method"] = "scan"
            command["_rsoul_folder"] = author_folder  # lets process_imports map results back to books
            command["_rsoul_readarr_path"] = download_dir
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

    Returns what the tags say if they name another book and the check is set to reject, else
    "". Never raises: a check that can't be made is not a reason to stop an import.
    """
    mode = audiobook_tag_check_mode(ctx.config)
    if mode == "off":
        return ""
    try:
        title = book_download.get("title", "")
        author_name = book_download.get("author_name", "")
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
            logger.debug(f"Could not fetch book {book_download.get('bookId')} for the tag check: {e}")
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
    except Exception:
        logger.exception(f"Audio tag check failed for {book_download.get('title', 'audiobook')}; continuing without it")
    return ""


def import_failure_retry_days(config: Any) -> float:
    """Days before a peer whose files weren't imported is tried again (0 = never again).

    Applies to every failure that may have a local cause (files not found or not movable,
    Readarr/Chaptarr not importing). Only an ebook whose metadata is for another book is
    recorded for good: that is the one case where the content is known to be wrong."""
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
        if path:
            local_path = to_local_path(path, readarr_download_dir, local_download_dir)
            # Never move an arbitrary path returned by a server command outside the
            # configured download directory.
            root = os.path.abspath(local_download_dir) if local_download_dir else ""
            target = os.path.abspath(local_path)
            if root and os.path.commonpath([root, target]) == root:
                move_failed_import(target)
            else:
                logger.error("Cannot quarantine failed import outside the download directory: %s", local_path)

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
        "the files are moved to failed_imports, from where you can import them manually."
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
    targeted = pending.get("method", "scan") == "targeted"

    def verified():
        return import_identity.verify_new_bookfiles(
            readarr_client, pending.get("book_id"), pending.get("edition_id"), pending.get("before_ids")
        )

    # A targeted import cannot be deemed successful merely because the staging files
    # disappeared. They might have been attached to another record (Chaptarr #285).
    if targeted and files and not left:
        result = verified()
        if result is not None:
            return result

    if now - pending.get("since", now) > PENDING_IMPORT_MAX_AGE:
        if files and not left and not targeted:
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
            if _import_succeeded(command):
                if files_left_after_import(files, local_dir) is None:
                    if targeted:
                        result = verified()
                        if result is None:
                            logger.warning("Import of %s finished but target book/edition cannot yet be verified; keeping the task pending", label)
                            return None
                        return result
                    logger.info(f"Import of {label} finished (result: {command.get('result', 'n/a')})")
                    return True
            logger.warning(f"Import of {label} did not succeed (status: {status}, result: {command.get('result', 'n/a')})")
            if left:
                move_failed_import(local_dir)
            return False

    # No command (never submitted, or forgotten by the server)
    if files and not left and not targeted:
        logger.info(f"Files of {label} are gone from the download folder: treating the import as done")
        return True
    if pending.get("submits", 0) >= PENDING_IMPORT_MAX_SUBMITS:
        logger.error(f"Import of {label} submitted {pending['submits']} times without a result; giving up")
        _give_up(local_dir, left)
        return False
    try:
        if targeted:
            if pending.get("book_id") is None or not files:
                logger.error("Cannot retry targeted import of %s: saved record has no book ID or files", label)
                return None
            command = targeted_import_command(
                readarr_client, pending["readarr_path"], pending["book_id"],
                [os.path.basename(p) for p in files], pending.get("media_type") or "ebook"
            )
            if command is None:
                logger.warning("Targeted import of %s still cannot be prepared; NEVER retrying with a folder scan", label)
                return None
        else:
            command = readarr_client.post_command(name="DownloadedBooksScan", path=pending["readarr_path"])
    except Exception as e:
        logger.warning(f"Could not submit import of {label} ({e}); trying again next run")
        return None
    logger.info(f"Submitted import of {label} again - command ID {command['id']}")
    updated = dict(pending, command_id=command["id"], submits=pending.get("submits", 0) + 1)
    if targeted:
        updated.update(edition_id=command.get("_rsoul_edition_id"), before_ids=command.get("_rsoul_before_ids"))
    return updated


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
        book_sources: dict = {}  # book ID -> (source id, title), to record a peer whose files were rejected
        folder_files: dict = {}  # import folder -> local paths of the files handed to Readarr/Chaptarr
        folder_media: dict = {}  # import folder -> ebook or audiobook for edition selection

        for book_download in items:
            if book_download.get("media_type") == "audiobook":
                # Whole folder of audio files: no per-file metadata validation, one import per book folder
                book_title = book_download.get("title", "")
                source_id = book_download.get("source_id", book_download.get("username", ""))
                logger.info(f"Processing audiobook: {book_title}")
                wrong_book = audiobook_tags_contradict(ctx, book_download, local_download_dir)
                if wrong_book:
                    # The files themselves say they are another book (confirmed, not merely
                    # unclear tags). Unlike a failed move, this is about the peer's files: they
                    # are not tried again.
                    logger.warning(f"Audiobook rejected: {book_title} - {wrong_book}")
                    names = book_download.get("expected_files") or [f["filename"].split("\\")[-1] for f in book_download.get("files", [])]
                    moved_to = move_files_aside(local_download_dir, book_download.get("dir", ""), names)
                    if moved_to:
                        logger.info(f"The files were moved to {moved_to}")
                    if ctx.history and source_id and book_title:
                        ctx.history.add_failure(source_id, book_title, f"Audio tags name another book ({wrong_book})")
                    continue
                relative, reason = organize_audiobook(book_download, local_download_dir)
                if relative:
                    author_folders.add(relative)
                    folder_media[relative] = "audiobook"
                    folder_books.setdefault(relative, []).append(book_download.get("bookId"))
                    book_sources[book_download.get("bookId")] = (source_id, book_title)
                    names = book_download.get("expected_files") or []
                    folder_files.setdefault(relative, []).extend(os.path.join(local_download_dir, relative, n) for n in names)
                else:
                    logger.warning(f"Audiobook failed: {book_title} - {reason}")
                    names = book_download.get("expected_files") or [f["filename"].split("\\")[-1] for f in book_download.get("files", [])]
                    move_files_aside(local_download_dir, book_download.get("dir", ""), names)
                    if ctx.history and source_id and book_title:
                        # Missing files or a failed move say nothing about the peer's files
                        ctx.history.add_failure(source_id, book_title, reason or "Audiobook import failed", retry_after_days=import_failure_retry_days(ctx.config))
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
                        ctx.history.add_failure(source_id, book_title, "Source file not found", retry_after_days=import_failure_retry_days(ctx.config))
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
                        folder_media[author_name_sanitized] = "ebook"
                        folder_books.setdefault(author_name_sanitized, []).append(book_id)
                        book_sources[book_id] = (source_id, book_title)
                        folder_files.setdefault(author_name_sanitized, []).append(os.path.join(local_download_dir, author_name_sanitized, filename))
                    else:
                        failed_imports.append((folder, filename, author_name_sanitized, "Failed to organize file"))
                        if ctx.history:
                            ctx.history.add_failure(source_id, book_title, "Failed to organize file", retry_after_days=import_failure_retry_days(ctx.config))
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
                        ctx.history.add_failure(sid, t, "Unexpected processing error", retry_after_days=import_failure_retry_days(ctx.config))

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
            method = import_method(ctx.config)
            commands = trigger_imports(readarr, readarr_download_dir, list(author_folders), method, folder_books, folder_files, folder_media)
            poll_timeout = ctx.config.getfloat("Readarr", "import_poll_timeout", fallback=IMPORT_TIMEOUT_SECONDS)
            command_results = monitor_imports(readarr, commands, readarr_download_dir, local_download_dir, timeout=poll_timeout) if commands else {}
            command_for = {c.get("_rsoul_folder"): c for c in commands}

            for folder in author_folders:
                command = command_for.get(folder)
                book_ids = folder_books.get(folder, [])
                unique_id = book_ids[0] if len(set(book_ids)) == 1 and book_ids else None
                tracked_files = folder_files.get(folder, [])

                def pending_for(cmd):
                    return pending_import_record(
                        folder, readarr_download_dir, local_download_dir, tracked_files,
                        cmd.get("id") if cmd else None,
                        method=method, book_id=unique_id, media_type=folder_media.get(folder, ""),
                        edition_id=cmd.get("_rsoul_edition_id") if cmd else None,
                        before_ids=cmd.get("_rsoul_before_ids") if cmd else None,
                    )

                if command is None:
                    # Readarr/Chaptarr didn't accept the import (unreachable?): keep the files
                    # staged and try the SAME method next run, never a fallback scan.
                    outcome = pending_for(None)
                else:
                    ok = command_results.get(command["id"], False)
                    if ok is None:
                        outcome = pending_for(command)
                    elif ok:
                        left = files_left_after_import(tracked_files, os.path.join(local_download_dir, folder))
                        outcome = left is None
                        if left:
                            # Like every other failed import: don't leave the files where the next
                            # import of this author or book would trip over them
                            move_files_aside(local_download_dir, folder, [os.path.basename(p) for p in left])
                        elif method == "targeted":
                            verified = import_identity.verify_new_bookfiles(
                                readarr, unique_id, command.get("_rsoul_edition_id"), command.get("_rsoul_before_ids")
                            )
                            if verified is None:
                                logger.warning("Import of %s finished, but the requested book/edition has not been verified; tracking across runs", folder)
                                outcome = pending_for(command)
                            else:
                                outcome = verified
                    else:
                        outcome = False
                for book_id in folder_books.get(folder, []):
                    if outcome is True and method == "scan" and wanted_record_still_empty(readarr, book_id):
                        source_id, title = book_sources.get(book_id, ("", ""))
                        logger.error(
                            f"{title or folder}: import finished but not for the record that was wanted "
                            f"(book {book_id} still has no files). It was probably attached to another record. "
                            "Not counted as imported and not recorded in the ledger. "
                            "Use [Readarr] import_method = targeted to avoid a scan selecting another book."
                        )
                        outcome = False
                    results[book_id] = outcome
                    if outcome is False and ctx.history:
                        # Readarr/Chaptarr rejected these files. Without a record of the peer the
                        # next run would find, download and fail on exactly the same files again.
                        # The record is temporary: the cause can also be on Readarr/Chaptarr's side
                        # (path mapping, permissions, import settings), and the peer's files fine.
                        source_id, title = book_sources.get(book_id, ("", ""))
                        ctx.history.add_failure(source_id, title, "Not imported by Readarr/Chaptarr", retry_after_days=import_failure_retry_days(ctx.config))

        else:
            logger.warning(f"No successful imports for backend {backend_name}")

    if not items_by_backend:
        logger.warning("No author folders found to import")

    return results
