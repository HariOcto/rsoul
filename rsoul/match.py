import difflib
import logging
import re
from typing import Any, Optional, Dict, List, Sequence, Tuple
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


# Words that name a chapter rather than a work of its own
_CHAPTER_WORDS = {
    "chapter", "chap", "ch", "part", "pt", "track", "cd", "disc", "disk", "kapitel", "teil", "prologue", "epilogue", "prolog", "epilog",
    "intro", "introduction", "preface", "foreword", "afterword", "credits", "opening", "closing", "end", "book",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
}
# From this size on, a single file is taken for a whole book rather than a chapter
BOOK_SIZED_BYTES = 100 * 1024 * 1024
_OF_TOTAL = re.compile(r"(?<!\d)(\d{1,3})\s*(?:of|von|/)\s*(\d{1,3})(?!\d)", re.IGNORECASE)


def books_in_folder(target: Dict[str, Any], files: List[Tuple[str, int]]) -> Tuple[str, List[str]]:
    """Decide what a folder named after the wanted book really holds.

    A folder called "Dune" is usually the book as chapters. It can also be the author's shelf
    for the series: "Dune.m4b", "Dune Messiah.m4b", "Children of Dune.m4b". The file names and
    sizes tell the two apart (files: (basename, size) of the folder's audio files).

    Returns:
        ("folder", [])     the folder is the book: take every file (chapters).
        ("own", names)     one or more book-sized files carry the title themselves and the
                           others are named differently: only those files are the book.
        ("several", [])    no file carries the title, and the files are other books: each is
                           named like another book by the author (target["sibling_titles"],
                           where Chaptarr provides them), or they are book-sized and named
                           differently from each other.
    """
    if not files:
        return "folder", []
    author_name = target["author"]["authorName"]
    series_title = target["book"].get("seriesTitle") or ""
    titles = title_variants(target["book"]["title"])
    narrators = book_narrators(target["book"])
    known = _people_words(author_name, narrators) | _FORMAT_NOISE | _CHAPTER_WORDS
    for title in titles:
        known |= set(_meaningful_words(title))

    def carries_title(basename: str) -> bool:
        if names_another_release(target["book"]["title"], basename, author_name, series_title, narrators):
            return False
        return any(title_segment_match(title, name, author_name, series_title, narrators) for name in file_name_candidates(basename) for title in titles)

    def own_name(basename: str) -> bool:
        words = _meaningful_words(clean_audiobook_name(strip_part_numbers(_stem(basename))))
        return any(w not in known and not _NUMBER_WORD.fullmatch(w) for w in words)

    siblings = target.get("sibling_titles") or []

    def names_sibling(basename: str) -> bool:
        """The file is named like another book by the author (and not like this one)."""
        return any(title_segment_match(variant, name, author_name, "", narrators) for sibling in siblings for variant in title_variants(sibling) for name in file_name_candidates(basename))

    sizes = dict(files)
    own = [name for name, _ in files if carries_title(name)]
    if not own and all(names_sibling(name) for name, _ in files):
        # Certain, whatever the sizes: "Dune\\Dune Messiah.m4b", or a shelf without the book
        return "several", []
    if len(files) < 2:
        return "folder", []
    others = [name for name, _ in files if name not in own and own_name(name)]
    if own and others and all(sizes[name] >= BOOK_SIZED_BYTES for name in own):
        return "own", same_book_files(own, [name for name, _ in files])
    if not own and len({book_key(name) for name in others}) >= 2 and len(others) == len(files) and all(sizes[name] >= BOOK_SIZED_BYTES for name in others):
        return "several", []
    return "folder", []


def missing_parts(basenames: List[str]) -> Optional[str]:
    """Say what is missing if the files' own numbering shows the book is incomplete, else None.

    Listing a folder shows everything the peer has, not everything the book has: a lone
    "Title - Part 2 of 4" or chapters 02 and 03 without 01 are complete listings of an
    incomplete book. Only plain numberings are judged (every file numbered, below 100).
    """
    stems = [_stem(name) for name in basenames]
    totals = [(int(n), int(total)) for n, total in (found.groups() for found in (_OF_TOTAL.search(stem) for stem in stems) if found)]
    if totals:
        total = max(t for _, t in totals)
        missing = sorted(set(range(1, total + 1)) - {n for n, _ in totals})
        if missing and total < 100:
            return f"part {', '.join(map(str, missing))} of {total} missing"
        return None
    if len(stems) == 1:
        # A number in front is the book's place in a series ("02 Golden Son"); only one set
        # apart at the end says "this is part N"
        found = _TRAILING_EVIDENT.search(stems[0])
        number = int(found.group("n")) if found else None
        return f"only part {number} is there" if number is not None and number > 1 else None
    numbers = [part_number(name) for name in basenames]
    if any(n is None or n >= 100 for n in numbers):
        return None
    first, last = min(numbers), max(numbers)
    if first > 1:
        return f"the numbering starts at {first}"
    missing = sorted(set(range(max(first, 1), last + 1)) - set(numbers))
    if missing:
        return f"number {', '.join(map(str, missing[:5]))}{'...' if len(missing) > 5 else ''} missing"
    return None


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


# Words about the recording, which may sit anywhere next to the title
_FORMAT_NOISE = {
    "unabridged", "unabr", "abridged", "audiobook", "audio", "hoerbuch", "hörbuch", "retail",
    "edition", "narrated", "read", "by", "m4b", "mp3", "m4a", "aac", "flac", "kbps",
}
# Words that introduce a position in a series ("Book 2", "Vol. 3")
_BOOK_WORDS = {"book", "bk", "vol", "volume"}
# Words about a series or collection. In front of the title they are a prefix ("Salvation
# Sequence 1 Salvation"); behind it they describe something bigger than the one book
# ("Red Rising Trilogy", "Dune Saga").
_COLLECTION_WORDS = {
    "books", "series", "sequence", "saga", "cycle", "complete", "collection", "omnibus", "boxset", "anthology",
    "duology", "trilogy", "tetralogy", "quartet", "quintet",
}
_SERIES_NOISE = _COLLECTION_WORDS | {"part"}
# "Books 1-6", "Vol. 1 & 2", "#1-3": several books in one
_BOOK_RANGE = re.compile(r"(?:\bbooks?|\bbks?|\bvols?|\bvolumes?|#)\.?\s*\d+\s*(?:[-–&+,]|\bto\b|\band\b|\bbis\b|\bund\b)\s*\d+", re.IGNORECASE)
_BRACKET_CONTENT = re.compile(r"\[([^\]]*)\]|\(([^)]*)\)|\{([^}]*)\}")
# A segment that starts like this names the narrator, not another book
_NARRATOR_LEAD = {"read", "narrated", "performed", "narrator", "gelesen"}

_NUMBER_WORD = re.compile(r"\d+(?:[snrt][tdh])?")
_YEAR_WORD = re.compile(r"(?:1[89]|20)\d\d")


def title_key(title: str) -> str:
    """A title reduced to what makes it the same title: case, punctuation and "&" / "and" aside."""
    return normalize_for_matching((title or "").replace("&", " and "))


def book_narrators(book: Dict[str, Any]) -> List[str]:
    """Narrator names Chaptarr reports for a book (none for ebooks, or when it knows none)."""
    names: List[str] = []
    for key in ("narratorNames", "availableNarrators"):
        value = book.get(key) or []
        for name in [value] if isinstance(value, str) else value:
            if isinstance(name, str) and name.strip() and name not in names:
                names.append(name)
    narrator = book.get("narrator")
    if isinstance(narrator, str) and narrator.strip() and narrator not in names:
        names.append(narrator)
    return names


def without_narrators(name: str, narrators: Sequence[str]) -> str:
    """The name with the narrators (and a "read by" / "narrated by" before them) taken out:
    "Author - Title - read by Narrator" -> "Author - Title"."""
    stripped = name
    for narrator in narrators:
        if narrator.strip():
            stripped = re.sub(rf"(?:\b(?:read|narrated|performed)\s+by\s+)?{re.escape(narrator.strip())}", " ", stripped, flags=re.IGNORECASE)
    if stripped == name:
        return name
    stripped = re.sub(r"[(\[{]\s*[)\]}]", " ", stripped)  # brackets left empty
    stripped = re.sub(r"(?:\s+[-–]\s*)+$|^(?:\s*[-–]\s+)+", "", re.sub(r"\s+", " ", stripped).strip())
    return re.sub(r"(\s[-–])+\s+(?=[-–]\s)", " ", stripped).strip() or name


def _people_words(author_name: str, narrators: Sequence[str] = ()) -> set:
    words = set(normalize_for_matching(author_name).split())
    for narrator in narrators:
        words |= set(normalize_for_matching(narrator).split())
    return words


def series_positions(series_title: str) -> set:
    """Positions of the book in its series, from Readarr/Chaptarr's "Red Rising Saga #1"
    (several series are separated by ";"). Empty if unknown."""
    return {int(float(n)) for n in re.findall(r"#\s*(\d+(?:\.\d+)?)", series_title or "")}


_BOOK_NUMBER = re.compile(r"(?:\b(?:book|bk|vol|volume)\b\.?\s*|#\s*)(\d+)", re.IGNORECASE)


def _check_trailing(words: List[str], allowed: set, series_words: set, positions: set) -> Optional[Dict[str, bool]]:
    """Check the words behind the title (or in a later part of the name).

    They leave it the same book if they are recording noise, the author, a year, the series
    name, or "Book N" with this book's own number. Returns None if something else is there,
    else which of the last two were seen.
    """
    seen = {"series": False, "book_number": False}
    for i, word in enumerate(words):
        if word in allowed or word in _BOOK_WORDS or _YEAR_WORD.fullmatch(word):
            continue
        if word in series_words:
            seen["series"] = True
            continue
        if _NUMBER_WORD.fullmatch(word):
            # "Book 2" / "Vol 2": fine for the second book of its series, or when the position
            # isn't known. A number without such a word ("Dune 2", "Red Rising 02") is the
            # series counting on: another book.
            if i and words[i - 1] in _BOOK_WORDS and word.isdigit() and (not positions or int(word) in positions):
                seen["book_number"] = True
                continue
            return None
        return None
    return seen


def names_another_release(title: str, raw_name: str, author_name: str = "", series_title: str = "", narrators: Sequence[str] = ()) -> bool:
    """True if a name, read as the peer wrote it, says "not this single book".

    This must see the name before brackets and release details are stripped from it, because
    that is where the evidence usually sits (clean_audiobook_name would turn "Red Rising
    (Books 1-6)" into a perfect "Red Rising"):

    - another position in the series, where Readarr/Chaptarr gave this book's: "(Book 2)", "#2";
    - several books in one: "Books 1-6", "Vol. 1 & 2";
    - a bracket that says collection and nothing else: "[Complete Series]", "(Trilogy)",
      "(Box Set)". A bracket naming the series with this book's number ("(Red Rising Saga
      #1)") or holding anything else ("(Unabridged)", "(2014)", "[Tim Gerard Reynolds]") is fine.
    """
    positions = series_positions(series_title)
    if positions and any(int(n) not in positions for n in _BOOK_NUMBER.findall(raw_name)):
        return True
    if _BOOK_RANGE.search(raw_name):
        return True
    known = set(_meaningful_words(title)) | _people_words(author_name, narrators) | set(_meaningful_words(series_title)) | _FORMAT_NOISE
    for groups in _BRACKET_CONTENT.findall(raw_name):
        content = next((g for g in groups if g), "")
        words = {w for w in _meaningful_words(content) if w not in known and not _NUMBER_WORD.fullmatch(w)}
        if words and words <= _COLLECTION_WORDS | {"box", "set"} and not _BOOK_NUMBER.search(content):
            return True
    return False


def title_segment_match(title: str, candidate: str, author_name: str = "", series_title: str = "", narrators: Sequence[str] = ()) -> bool:
    """True if the name says this title, and nothing in it says a different book.

    The fuzzy score alone can't tell "Salvation" from "The Saints of Salvation" or "Salvation
    Lost": it compares the last words of a name with the title and scores 1.0. Here the name is
    split at " - ", commas, colons and folder boundaries, and one part must contain every word
    of the title. Around the title, that part may only hold:

    - in front: the author, the series name, numbers and words like "Book" or "Unabridged"
      ("Frontlines 2 - Lines of Departure", "Salvation Sequence 1 Salvation");
    - behind: the author, recording words, a year, and the series with the book's number
      ("Knife Edge Empire's Corps, Book 17"). "Salvation Lost", "Dune 2", "Red Rising 02"
      and "Red Rising Trilogy" (a series without a book number: the whole set) are not it.

    The parts after it are held to the same rule, because many series are named after their
    first book: "Red Rising 02 - Golden Son" is Golden Son, not Red Rising. A part that names
    the narrator ("read by ...") is fine. If Readarr/Chaptarr gave the book's position in its
    series ("Red Rising Saga #1"), a "Book 2" or "#2" anywhere in the name rules it out.
    """
    title_words = set(_meaningful_words(title))
    if not title_words:
        return True

    if names_another_release(title, candidate, author_name, series_title, narrators):
        return False

    positions = series_positions(series_title)
    name = candidate
    if " " not in name.strip():
        name = re.sub(r"[-_.]+", " ", name)  # "Marko-Kloos-Lines-of-Departure-Unabr"
    name = _BRACKETED.sub(" ", name)
    # The author, and the narrators where Chaptarr knows them ("Red Rising - Tim Gerard Reynolds")
    behind = _people_words(author_name, narrators) | _FORMAT_NOISE
    series_words = ({w for w in _meaningful_words(series_title) if not _NUMBER_WORD.fullmatch(w)} | _SERIES_NOISE) - title_words
    in_front = behind | series_words | _BOOK_WORDS

    segments = [[w for w in normalize_for_matching(part).split() if w not in STOP_WORDS] for part in re.split(r"\s+[-–]\s+|\\|,|;|:", name)]
    for index, words in enumerate(segments):
        if not title_words <= set(words):
            continue
        first = min(i for i, w in enumerate(words) if w in title_words)
        last = max(i for i, w in enumerate(words) if w in title_words)
        # Numbers, including ordinals such as 1st, 3rd or 4th, in front of the title don't make
        # it a different title; words between the title's own words do
        if any(w not in in_front and not _NUMBER_WORD.fullmatch(w) for w in words[:first]):
            continue
        if any(w not in title_words for w in words[first:last]):
            continue
        later = [part for part in segments[index + 1:] if part and part[0] not in _NARRATOR_LEAD]
        checks = [_check_trailing(part, behind, series_words, positions) for part in [words[last + 1:]] + later]
        if any(check is None for check in checks):
            continue
        # A series named behind the title needs the book's number with it; without one the
        # name describes the whole series ("Red Rising Saga", "Dune Trilogy")
        if any(check["series"] for check in checks) and not any(check["book_number"] for check in checks):
            continue
        return True
    return False


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
    rejected: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """Match an audiobook against one user's search results, folder by folder.

    If `stats` is given, counts of what was found and why folders were rejected are added to it;
    if `rejected` is given, each rejected folder is appended with its reason (for the dry run).

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
    narrators = book_narrators(target["book"])

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
        if rejected is not None:
            rejected.append({"reason": reason, "directory": directory, "username": username})
        logger.debug(f"Audiobook folder rejected ({reason}): {directory} from {username}")

    def best_score_for(names: List[str], checked: bool = False) -> Optional[float]:
        """Best fuzzy score of these names, or None if none of them is exactly this title
        (checked: the caller has already established that)."""
        if not checked and not any(title_segment_match(title, name, author_name, series_title, narrators) for name in names for title in titles):
            return None
        # The fuzzy score reads "Author - Title - Narrator" as author, then a title it doesn't
        # know. Where Chaptarr names the narrators, score the name without them as well.
        names = list(dict.fromkeys(names + [without_narrators(name, narrators) for name in names]))
        # A name without the author ("Cry Wolf (Empire's Corps 15).mp3" in the author's folder)
        # is too short for the fuzzy score, which expects "Title - Author" and wants two words
        # in common. The author is established by now (author_in_name on the path), so the
        # score is also taken with the author in front; it then measures the title alone.
        names += [f"{author_name} - {name}" for name in names if author_name and not author_in_name(author_name, name)]
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

    def accepted_score(score: Optional[float], name: str) -> float:
        """The score to rank a candidate by, once its name passed the title and author checks.

        Those checks are the strict ones (title_segment_match, author_in_name). The fuzzy score
        was built for "Author - Title" file names and trips over perfectly good audiobook names
        ("Frontlines 7.5 - Corvus", a one-word title behind a long series prefix), so it only
        ranks here and no longer rejects: a low score is lifted to the threshold, which puts
        the candidate behind every better-scoring one.
        """
        if score is not None and score >= minimum_match_ratio:
            return score
        logger.info(f"Audiobook match on title and author with a low fuzzy score, kept as a last choice: {name} from {username}")
        return minimum_match_ratio

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
            if author_in_name(author_name, directory):
                logger.info(f"Skipping {directory} from {username}: looks like one disc/part of a multi-part audiobook")
                note("disc/part folders", directory)
            else:
                # Somebody else's "CD2" (broad fallback searches bring in music): not worth a log line
                note("folders without the author's surname", directory)
            continue

        # Was it the folder that matched, or only the names of files inside it? Files named
        # "Author - Title.m4b" or "Author - Title - 01.mp3" often sit in a folder full of other
        # books ("Audiobooks", "Brandon Sanderson"): then only they belong to the book.
        # The folder's own name must be the title. Its parent only supplies the author for the
        # score: series folders are often named after their first book ("Foundation\\Second
        # Foundation"), so a parent that matches says nothing about this folder.
        leaf = directory.split("\\")[-1]
        if names_another_release(book_title, leaf, author_name, series_title, narrators):
            # Judged on the name as written: stripping its brackets would hide the evidence
            note("folders for a different title", directory)
            continue
        leaf_names = [name for name in dict.fromkeys((leaf, clean_audiobook_name(leaf))) if name]
        if any(title_segment_match(title, name, author_name, series_title, narrators) for name in leaf_names for title in titles):
            # Folder names often omit the author ("Audiobooks\\Dune"), and titles repeat across
            # authors, so the author's surname must appear somewhere in the path (or, for a
            # single-file book, the filename). This also rules out other books with the same title.
            path_text = directory + (" " + split_slskd_path(files[0]["filename"])[1] if len(files) == 1 else "")
            if not author_in_name(author_name, path_text):
                note("folders without the author's surname", directory)
                continue
            score = accepted_score(best_score_for(folder_name_candidates(directory, files), checked=True), directory)
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
            if names_another_release(book_title, basename, author_name, series_title, narrators):
                reasons.add("title")
                continue
            score = best_score_for(file_name_candidates(basename))
            if score is None:
                reasons.add("title")
            else:
                offer(directory, ext, book_files, accepted_score(score, f"{directory}\\{basename}"), file_based=True)
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
