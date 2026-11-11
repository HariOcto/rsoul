import difflib
import logging
import re
from typing import Any, Optional, Dict, List
from .display import print_match_details
from .utils import normalize_for_matching, title_contained_in_filename, jaccard_similarity, length_ratio, extract_author_title, STOP_WORDS

logger = logging.getLogger(__name__)


def verify_filetype(file: Dict[str, Any], allowed_filetype: str) -> bool:
    current_filetype = file["filename"].split(".")[-1].lower()
    logger.debug(f"Current file type: {current_filetype}")
    if current_filetype == allowed_filetype.split(" ")[0]:
        return True
    else:
        return False


def check_ratio(separator: str, ratio: float, book_filename: str, slskd_filename: str, minimum_match_ratio: float) -> float:
    if ratio < minimum_match_ratio:
        if separator != "":
            book_filename_word_count = len(book_filename.split()) * -1
            truncated_slskd_filename = " ".join(slskd_filename.split(separator)[book_filename_word_count:])
            ratio = difflib.SequenceMatcher(None, book_filename, truncated_slskd_filename).ratio()
        else:
            ratio = difflib.SequenceMatcher(None, book_filename, slskd_filename).ratio()
        return ratio
    return ratio


def score_name(
    book_title: str,
    author_name: str,
    candidate: str,
    minimum_match_ratio: float,
    min_length_ratio: float = 0.4,
    min_jaccard_ratio: float = 0.25,
    min_word_overlap: int = 2,
    min_title_jaccard: float = 0.3,
    min_author_jaccard: float = 0.5,
    display_name: Optional[str] = None,
) -> Optional[float]:
    """Score one candidate name (filename without extension, or a folder name) against a book.

    Applies the same pre-filters and fuzzy patterns as book_match. Returns the
    best similarity ratio, or None if a pre-filter rejects the candidate.
    Thresholding against minimum_match_ratio is left to the caller.
    """
    slskd_filename = candidate
    slskd_filename_full = display_name or candidate

    # Build expected filename pattern for pre-filter comparison (without extension)
    expected_pattern = f"{book_title} - {author_name}"

    # Pre-filter 1: Length ratio gate
    len_ratio = length_ratio(expected_pattern, slskd_filename)
    if len_ratio < min_length_ratio:
        logger.debug(f"Skipping {slskd_filename_full}: length ratio {len_ratio:.2f} < {min_length_ratio}")
        return None

    # Pre-filter 2: Jaccard token overlap
    jaccard_score, overlap_count, _ = jaccard_similarity(expected_pattern, slskd_filename)
    if jaccard_score < min_jaccard_ratio:
        logger.debug(f"Skipping {slskd_filename_full}: Jaccard {jaccard_score:.2f} < {min_jaccard_ratio}")
        return None

    # Pre-filter 3: Minimum word overlap
    if overlap_count < min_word_overlap:
        logger.debug(f"Skipping {slskd_filename_full}: word overlap {overlap_count} < {min_word_overlap}")
        return None

    # Pre-filter 4: Component-wise matching (author vs author, title vs title)
    found_part1, found_part2 = extract_author_title(slskd_filename)

    if found_part2:  # Only apply if we found a separator
        # Try both orderings: "Author - Title" and "Title - Author"
        author_as_p1 = jaccard_similarity(author_name, found_part1)[0]
        title_as_p2 = jaccard_similarity(book_title, found_part2)[0]
        score_order1 = min(author_as_p1, title_as_p2)  # Author-Title order

        author_as_p2 = jaccard_similarity(author_name, found_part2)[0]
        title_as_p1 = jaccard_similarity(book_title, found_part1)[0]
        score_order2 = min(author_as_p2, title_as_p1)  # Title-Author order

        # Use the better ordering
        if score_order1 >= score_order2:
            author_score, title_score = author_as_p1, title_as_p2
        else:
            author_score, title_score = author_as_p2, title_as_p1

        # Both components must meet their thresholds
        if author_score < min_author_jaccard:
            logger.debug(f"Skipping {slskd_filename_full}: author Jaccard {author_score:.2f} < {min_author_jaccard}")
            return None

        if title_score < min_title_jaccard:
            logger.debug(f"Skipping {slskd_filename_full}: title Jaccard {title_score:.2f} < {min_title_jaccard}")
            return None

        logger.debug(f"Component match passed: author={author_score:.2f}, title={title_score:.2f}")

    logger.info(f"Checking ratio on {slskd_filename_full} vs wanted {book_title} - {author_name}")

    # Mandatory requirement: Title must be contained in the filename
    if not title_contained_in_filename(book_title, slskd_filename):
        logger.debug(f"Skipping {slskd_filename_full}: Title '{book_title}' not found in filename")
        return None

    # Try multiple filename patterns for matching
    patterns_to_try = [
        f"{book_title} - {author_name}",
        f"{author_name} - {book_title}",
        f"{book_title}",
        f"{author_name} {book_title}",
    ]

    max_ratio = 0.0

    for pattern in patterns_to_try:
        # Direct ratio
        ratio = difflib.SequenceMatcher(None, pattern, slskd_filename).ratio()
        max_ratio = max(max_ratio, ratio)

        # Try with normalized strings for better matching
        normalized_pattern = normalize_for_matching(pattern)
        normalized_filename = normalize_for_matching(slskd_filename)
        normalized_ratio = difflib.SequenceMatcher(None, normalized_pattern, normalized_filename).ratio()
        max_ratio = max(max_ratio, normalized_ratio)

        # Try with different separators
        ratio = check_ratio(" ", ratio, pattern, slskd_filename, minimum_match_ratio)
        max_ratio = max(max_ratio, ratio)

        ratio = check_ratio("_", ratio, pattern, slskd_filename, minimum_match_ratio)
        max_ratio = max(max_ratio, ratio)

    return max_ratio


def book_match(
    target: Dict[str, Any],
    slskd_files: List[Dict[str, Any]],
    username: str,
    filetype: str,
    ignored_users: List[str],
    minimum_match_ratio: float,
    min_length_ratio: float = 0.4,
    min_jaccard_ratio: float = 0.25,
    min_word_overlap: int = 2,
    min_title_jaccard: float = 0.3,
    min_author_jaccard: float = 0.5,
) -> Optional[Dict[str, Any]]:
    """
    Match target book with available files, filtering by correct filetype.
    Enhanced to handle variations in punctuation, underscores, and additional text.

    Pre-filters applied before fuzzy matching:
    - Length ratio gate: Rejects if string lengths differ too much
    - Jaccard token overlap: Rejects if word overlap is too low
    - Minimum word overlap: Rejects if fewer than N words match
    - Component-wise matching: Matches author and title segments separately

    Args:
        target: Target book information
        slskd_files: List of available files
        username: Username of the file owner
        filetype: Required file type (e.g., 'epub', 'pdf')
        ignored_users: List of ignored users
        minimum_match_ratio: Minimum ratio to consider a match
        min_length_ratio: Minimum length ratio (shorter/longer) - default 0.4
        min_jaccard_ratio: Minimum Jaccard similarity threshold - default 0.25
        min_word_overlap: Minimum number of overlapping words required - default 2
        min_title_jaccard: Minimum Jaccard for title component - default 0.3
        min_author_jaccard: Minimum Jaccard for author component - default 0.5

    Returns:
        Matching file object or None
    """
    book_title = target["book"]["title"]
    author_name = target["author"]["authorName"]
    best_match = 0.0
    current_match = None

    # Filter files by the correct filetype first
    filtered_files = []
    for slskd_file in slskd_files:
        if verify_filetype(slskd_file, filetype):
            filtered_files.append(slskd_file)

    # If no files match the desired filetype, return None
    if not filtered_files:
        logger.debug(f"No files found matching filetype: {filetype}")
        return None

    for slskd_file in filtered_files:
        slskd_filename_full = slskd_file["filename"].split("\\")[-1]
        # Remove extension for matching to prevent "epub" from inflating scores
        slskd_filename = slskd_filename_full.rsplit(".", 1)[0] if "." in slskd_filename_full else slskd_filename_full

        final_ratio = score_name(
            book_title,
            author_name,
            slskd_filename,
            minimum_match_ratio,
            min_length_ratio=min_length_ratio,
            min_jaccard_ratio=min_jaccard_ratio,
            min_word_overlap=min_word_overlap,
            min_title_jaccard=min_title_jaccard,
            min_author_jaccard=min_author_jaccard,
            display_name=slskd_filename_full,
        )
        if final_ratio is None:
            continue

        if final_ratio > best_match:
            logger.info(f"New best match found! Ratio: {final_ratio:.3f}")
            best_match = final_ratio
            current_match = slskd_file
        else:
            logger.info(f"Ratio: {final_ratio:.3f} (not better than current best: {best_match:.3f})")

    if (current_match != None) and (username not in ignored_users) and (best_match >= minimum_match_ratio):
        # Log match found (toned down - details logged at DEBUG level)
        short_filename = current_match["filename"].split("\\")[-1] if "\\" in current_match["filename"] else current_match["filename"]
        logger.info(f"Match found: {short_filename} (ratio: {best_match:.3f})")

        # Print match details at debug level
        print_match_details(current_match["filename"], best_match, username, filetype)

        return current_match

    return None


# ---------------------------------------------------------------------------
# Audiobook folder matching
# ---------------------------------------------------------------------------

# Bracketed segments like "[Narrator]", "(Unabridged)", "{2006}"
_BRACKETED = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}")
# Release noise common in audiobook folder names
_AUDIOBOOK_NOISE = re.compile(
    r"\b(?:unabridged|abridged|audiobook|audio\s*book|hoerbuch|h\u00f6rbuch|retail|"
    r"mp3|m4b|m4a|aac|flac|\d{2,3}\s?k(?:bps)?|\d{2,3}\s?kbit|vbr|cbr)\b",
    re.IGNORECASE,
)


def split_slskd_path(path: str) -> tuple:
    """Split a Soulseek path (Windows-style backslashes) into (directory, basename)."""
    if "\\" in path:
        directory, basename = path.rsplit("\\", 1)
        return directory, basename
    return "", path


def clean_audiobook_name(name: str) -> str:
    """Strip bracketed segments and release noise from an audiobook folder/file name."""
    cleaned = _BRACKETED.sub(" ", name)
    cleaned = _AUDIOBOOK_NOISE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Remove separators left dangling at either end ("Author - Title -")
    cleaned = re.sub(r"^[\s\-_.,]+|[\s\-_.,]+$", "", cleaned)
    return cleaned


def folder_name_candidates(directory: str, files: List[Dict[str, Any]]) -> List[str]:
    """Names to match an audiobook folder on: leaf folder, parent + leaf, and the
    file name itself when the folder holds a single file (e.g. one .m4b)."""
    parts = [p for p in directory.split("\\") if p]
    raw: List[str] = []
    if parts:
        raw.append(parts[-1])
        if len(parts) >= 2:
            raw.append(f"{parts[-2]} - {parts[-1]}")
    if len(files) == 1:
        basename = split_slskd_path(files[0]["filename"])[1]
        raw.append(basename.rsplit(".", 1)[0] if "." in basename else basename)

    candidates: List[str] = []
    for name in raw:
        for variant in (name, clean_audiobook_name(name)):
            if variant and variant not in candidates:
                candidates.append(variant)
    return candidates


# Chapter and part numbers in file names.
#
# A number at the end of a name is only *evidently* a chapter or part number when something
# sets it apart from the title: a word such as "Part" or "CD", a separator ("Title - 3",
# "Title (3)") or zero padding ("Title 03"). A bare "Title 2" is more often the
# second book of a series, so it is never taken for a part of "Title" on its own. The one
# exception, "Title 10" next to "Title 01"..."Title 09", is handled in same_book_files.
_PART_WORD = r"(?:part|pt|chapter|chap|ch|cd|disc|disk|track|teil|kapitel)"
_NUMBER = r"(?<!\d)(?P<n>\d{1,3})(?:\s*(?:of|von|/)\s*\d{1,3})?[\s)\]]*$"
_TRAILING_EVIDENT = re.compile(
    rf"(?:[\s\-_.,(\[]*(?<![A-Za-z]){_PART_WORD}\.?\s*"  # "Part 2", "CD2", "(pt. 3"
    rf"|(?:\s+[\-–]\s*|\s*[(\[]\s*)"  # " - 2", "(2"; not "_2" or ".2", which often stand for a space
    rf"|(?:\s+|[\-_.])(?=0\d))"  # " 02", "-02"
    rf"{_NUMBER}",
    re.IGNORECASE,
)
_TRAILING_ANY = re.compile(rf"[\s\-_.,(\[]*(?:(?<![A-Za-z]){_PART_WORD}\.?\s*)?{_NUMBER}", re.IGNORECASE)
# A number in front of the name is a track number: "03 - Title", "03. Title", "CD2 Title"
_LEADING_PART = re.compile(rf"^[\s(\[]*(?:{_PART_WORD}\.?\s*)?(?P<n>\d{{1,3}})(?!\d)[\s)\]\-_.]+", re.IGNORECASE)


def _stem(basename: str) -> str:
    return basename.rsplit(".", 1)[0] if "." in basename else basename


def _strip_numbers(name: str, trailing: "re.Pattern") -> str:
    stripped = _LEADING_PART.sub("", trailing.sub("", name)).strip()
    return stripped or name  # nothing but a number: leave it


def strip_part_numbers(name: str) -> str:
    """Remove evident chapter/part numbers: "Author - Title - 01" -> "Author - Title",
    "03 - Title" -> "Title", "Title (Part 2 of 5)" -> "Title". "Title 2" is left as it is."""
    return _strip_numbers(name, _TRAILING_EVIDENT)


def book_key(basename: str) -> str:
    """What the files of one book have in common: the file name without its extension and
    without evident chapter/part numbers, normalised. "Author - Title - 01.mp3" and
    "Author - Title - 02.mp3" share a key; "Author - Title 2.m4b" (a sequel) has its own."""
    return normalize_for_matching(strip_part_numbers(_stem(basename)))


def _loose_key(basename: str) -> str:
    return normalize_for_matching(_strip_numbers(_stem(basename), _TRAILING_ANY))


def part_number(basename: str) -> Optional[int]:
    """The chapter/part number a file name carries at its start or end, if any."""
    stem = _stem(basename)
    found = _LEADING_PART.search(stem) or _TRAILING_ANY.search(stem)
    return int(found.group("n")) if found else None


def same_book_files(matched: List[str], basenames: List[str]) -> List[str]:
    """Of the files in a folder, those that belong to the same book as the matched ones.

    Files with the same book_key belong together. Files that differ only by a bare number
    ("Title 10" next to "Title 01") are added when the others show a zero-padded numbering
    that such a number continues; otherwise ("Title 2" next to "Title") they are other books.
    """
    keys = {book_key(name) for name in matched}
    loose = {_loose_key(name) for name in matched}
    sure = [name for name in basenames if book_key(name) in keys]
    # "Title.m4b" next to "Title (2).m4b" is a book and a second copy (or something else), not
    # two parts: numbered and unnumbered files only go together with their own kind
    numbered = {name: strip_part_numbers(_stem(name)) != _stem(name) for name in set(sure) | set(matched)}
    kinds = {numbered[name] for name in matched}
    if len(kinds) == 1:
        sure = [name for name in sure if numbered[name] in kinds]
    unsure = [name for name in basenames if name not in sure and _loose_key(name) in loose]
    if unsure:
        tokens = [found.group("n") for found in (_TRAILING_ANY.search(_stem(name)) for name in sure) if found]
        padded = [len(token) for token in tokens if token.startswith("0")]
        if padded and len(tokens) == len(sure):  # every sure file is numbered, in a padded scheme
            width = max(padded)
            sure = sure + [name for name in unsure if len(_TRAILING_ANY.search(_stem(name)).group("n")) >= width]
    return sure


def file_name_candidates(basename: str) -> List[str]:
    """Names to match a book's own file on: with and without part numbers and release noise."""
    stem = _stem(basename)
    candidates: List[str] = []
    for name in (stem, strip_part_numbers(stem)):
        for variant in (name, clean_audiobook_name(name)):
            if variant and variant not in candidates:
                candidates.append(variant)
    return candidates


def _meaningful_words(text: str) -> List[str]:
    return [w for w in normalize_for_matching(text).split() if w not in STOP_WORDS]


# Folders holding one disc or part of a multi-disc audiobook ("CD1", "Disc 02", "Part 3",
# "Mistborn - CD1"). Downloading one of them would import an incomplete book.
_DISC_FOLDER = re.compile(r"^(?:cd|disc|disk|part|teil|vol|volume)\s*[-_.]?\s*\d{1,3}$|\b(?:cd|disc|disk)\s*[-_.]?\s*\d{1,3}\b", re.IGNORECASE)

# Name suffixes that are not a surname
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd"}


def is_disc_folder(directory: str) -> bool:
    """True if the folder's own name marks it as one disc/part of a larger audiobook."""
    leaf = directory.split("\\")[-1].strip() if directory else ""
    return bool(leaf) and bool(_DISC_FOLDER.search(leaf))


def title_variants(title: str) -> List[str]:
    """The full title plus shorter forms that folders are often named after.

    - A trailing "(...)" or "[...]" is dropped: "The Final Empire (Mistborn #1)" also gives
      "The Final Empire", and "Dune (Dune Chronicles, #1)" also gives "Dune".
    - Either side of a colon is used if it has at least two meaningful words:
      "Mistborn: The Final Empire" also gives "The Final Empire", but not "Mistborn" alone,
      which would match every book in the series.

    Audiobook matching requires the author in the folder path for every variant.
    """
    variants = [title]
    no_suffix = re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*$", "", title).strip()
    if no_suffix and no_suffix != title:
        variants.append(no_suffix)
    for text in list(variants):
        if ":" in text:
            main, sub = (part.strip() for part in text.split(":", 1))
            for part in (sub, main):
                if len(_meaningful_words(part)) >= 2 and part not in variants:
                    variants.append(part)
    return variants


# Words that may sit next to the title in a folder name without making it a different book
_TITLE_NOISE = {
    "book", "books", "bk", "vol", "volume", "part", "series", "sequence", "saga", "cycle", "trilogy",
    "unabridged", "unabr", "abridged", "audiobook", "audio", "hoerbuch", "hörbuch", "retail",
    "complete", "edition", "narrated", "read", "by", "m4b", "mp3", "m4a", "aac", "flac", "kbps",
}


def title_segment_match(title: str, candidate: str, author_name: str = "", series_title: str = "") -> bool:
    """True if some part of the name is this title and nothing else.

    The fuzzy score alone can't tell "Salvation" from "The Saints of Salvation" or "Salvation
    Lost": it compares the last words of a name with the title and scores 1.0. Here the name is
    split at " - ", commas, colons and folder boundaries, and one part must contain every word
    of the title plus nothing except the author's name, the series name, numbers and noise such
    as "Book", "Unabridged" or "m4b". "Frontlines 2 - Lines of Departure" and "Salvation
    Sequence 1 - Salvation" pass; "The Saints of Salvation" and "Salvation Lost" don't.
    """
    title_words = set(_meaningful_words(title))
    if not title_words:
        return True

    name = candidate
    if " " not in name.strip():
        name = re.sub(r"[-_.]+", " ", name)  # "Marko-Kloos-Lines-of-Departure-Unabr"
    name = _BRACKETED.sub(" ", name)
    ignore = set(normalize_for_matching(author_name).split()) | set(_meaningful_words(series_title)) | _TITLE_NOISE

    for segment in re.split(r"\s+[-–]\s+|\\|,|;|:", name):
        words = [w for w in normalize_for_matching(segment).split() if w not in STOP_WORDS]
        if not title_words <= set(words):
            continue
        # Numbers, including ordinals such as 1st, 3rd or 4th, don't make it a different title
        extras = [w for w in words if w not in title_words and w not in ignore and not re.fullmatch(r"\d+(?:[snrt][tdh])?", w)]
        if extras:
            continue
        # ...except a bare number right behind the title: "Dune 2" is the next book, not "Dune".
        # A number that is set apart is fine: zero-padded ("Dune 01", a chapter), a year
        # ("Dune 1965"), after another word ("Dune Book 1") or in front ("1 Dune").
        if any(_sequel_number(w, words[i - 1] if i else None, title_words) for i, w in enumerate(words) if w not in title_words):
            continue
        return True
    return False


def _sequel_number(word: str, previous: Optional[str], title_words: set) -> bool:
    """True for a bare number (or ordinal) that directly follows a word of the title."""
    if previous not in title_words or not re.fullmatch(r"\d+(?:[snrt][tdh])?", word):
        return False
    if word.startswith("0") and len(word) >= 2:
        return False  # zero-padded: a chapter or track number
    if re.fullmatch(r"(?:1[89]|20)\d\d", word):
        return False  # a year
    return True


def author_in_name(author_name: str, candidate: str) -> bool:
    """True if the author's surname appears in the candidate name (or path)."""
    words = [w for w in normalize_for_matching(author_name).split() if len(w) > 1 and w not in _NAME_SUFFIXES]
    return bool(words) and words[-1] in normalize_for_matching(candidate.replace("\\", " ")).split()


def audiobook_folder_match(
    target: Dict[str, Any],
    slskd_files: List[Dict[str, Any]],
    username: str,
    allowed_filetypes: List[str],
    ignored_users: List[str],
    minimum_match_ratio: float,
    min_length_ratio: float = 0.4,
    min_jaccard_ratio: float = 0.25,
    min_word_overlap: int = 2,
    min_title_jaccard: float = 0.3,
    min_author_jaccard: float = 0.5,
    stats: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Match an audiobook against one user's search results, folder by folder.

    If `stats` is given, counts of what was found and why folders were rejected are added to it.

    Audiobooks usually arrive as a folder of chapter files ("01.mp3", "02.mp3"...)
    whose names say nothing about the book, so the folder name is matched instead.
    Files are grouped by (folder, extension). Disc/part subfolders are skipped, and the
    author's surname must appear in the folder path. The best folder per extension that
    scores at least minimum_match_ratio is returned, best match first.

    Returns:
        List of dicts: directory, extension, files, score, total_size, file_based.
        file_based is True when the folder's name didn't match but the names of files in it
        did: the files sharing that name (see book_key) are the book, and the folder may hold
        other books.
    """
    if username in ignored_users:
        return []

    book_title = target["book"]["title"]
    author_name = target["author"]["authorName"]
    series_title = target["book"].get("seriesTitle") or ""
    allowed = [ext.split(" ")[0].lower() for ext in allowed_filetypes]

    titles = title_variants(book_title)

    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for slskd_file in slskd_files:
        directory, basename = split_slskd_path(slskd_file["filename"])
        ext = basename.rsplit(".", 1)[-1].lower() if "." in basename else ""
        if ext in allowed:
            groups.setdefault((directory, ext), []).append(slskd_file)
        elif stats is not None:
            stats["files in other formats"] = stats.get("files in other formats", 0) + 1

    def note(reason: str, directory: str) -> None:
        if stats is not None:
            stats[reason] = stats.get(reason, 0) + 1
        logger.debug(f"Audiobook folder rejected ({reason}): {directory} from {username}")

    def best_score_for(names: List[str]) -> Optional[float]:
        """Best fuzzy score of these names, or None if none of them is exactly this title."""
        if not any(title_segment_match(title, name, author_name, series_title) for name in names for title in titles):
            return None
        best = None
        for name in names:
            for title in titles:
                score = score_name(
                    title,
                    author_name,
                    name,
                    minimum_match_ratio,
                    min_length_ratio=min_length_ratio,
                    min_jaccard_ratio=min_jaccard_ratio,
                    min_word_overlap=min_word_overlap,
                    min_title_jaccard=min_title_jaccard,
                    min_author_jaccard=min_author_jaccard,
                )
                if score is not None and (best is None or score > best):
                    best = score
        return -1.0 if best is None else best  # -1: the title is there, but the fuzzy score rejects it

    best_per_ext: Dict[str, Dict[str, Any]] = {}

    def offer(directory: str, ext: str, files: List[Dict[str, Any]], score: float, file_based: bool) -> None:
        candidate = {
            "directory": directory,
            "extension": ext,
            "files": files,
            "score": score,
            "total_size": sum(f.get("size", 0) for f in files),
            # True: the files' own names matched, not the folder's. Only the files that share
            # this name are the book; the rest of the folder is something else.
            "file_based": file_based,
        }
        current = best_per_ext.get(ext)
        if current is None or (candidate["score"], candidate["total_size"]) > (current["score"], current["total_size"]):
            best_per_ext[ext] = candidate

    for (directory, ext), files in groups.items():
        if is_disc_folder(directory):
            logger.info(f"Skipping {directory} from {username}: looks like one disc/part of a multi-part audiobook")
            note("disc/part folders", directory)
            continue

        # Was it the folder that matched, or only the names of files inside it? Files named
        # "Author - Title.m4b" or "Author - Title - 01.mp3" often sit in a folder full of other
        # books ("Audiobooks", "Brandon Sanderson"): then only they belong to the book.
        folder_names = folder_name_candidates(directory, [])
        if any(title_segment_match(title, name, author_name, series_title) for name in folder_names for title in titles):
            # Folder names often omit the author ("Audiobooks\\Dune"), and titles repeat across
            # authors, so the author's surname must appear somewhere in the path (or, for a
            # single-file book, the filename). This also rules out other books with the same title.
            path_text = directory + (" " + split_slskd_path(files[0]["filename"])[1] if len(files) == 1 else "")
            if not author_in_name(author_name, path_text):
                note("folders without the author's surname", directory)
                continue
            score = best_score_for(folder_name_candidates(directory, files))
            if score is None or score < minimum_match_ratio:
                note("folders not matching the title", directory)
                continue
            offer(directory, ext, files, score, file_based=False)
            continue

        # The folder is not this book. Look at the files instead, grouped by the name they
        # share once chapter and part numbers are set aside.
        by_book: Dict[str, List[Dict[str, Any]]] = {}
        for slskd_file in files:
            by_book.setdefault(book_key(split_slskd_path(slskd_file["filename"])[1]), []).append(slskd_file)

        reasons = set()
        for book_files in by_book.values():
            basename = split_slskd_path(book_files[0]["filename"])[1]
            if not author_in_name(author_name, directory + " " + basename):
                reasons.add("author")
                continue
            score = best_score_for(file_name_candidates(basename))
            if score is None:
                reasons.add("title")
            elif score < minimum_match_ratio:
                reasons.add("score")
            else:
                offer(directory, ext, book_files, score, file_based=True)
                reasons.add("matched")
        if "matched" not in reasons:
            if "score" in reasons:
                note("folders not matching the title", directory)
            elif "title" in reasons:
                # e.g. "The Saints of Salvation" or "Salvation Lost" when "Salvation" is wanted
                note("folders for a different title", directory)
            else:
                note("folders without the author's surname", directory)

    # Best match first; the configured format order only breaks ties
    matches = sorted(best_per_ext.values(), key=lambda c: (-c["score"], allowed.index(c["extension"])))
    for match in matches:
        logger.info(f"Audiobook folder match: {match['directory']} [{match['extension']}, {len(match['files'])} files] (ratio: {match['score']:.3f}) from {username}")
    return matches
