import os
import re


# Words ignored when checking whether a title appears in a file or folder name
STOP_WORDS = frozenset({"the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for", "by", "with"})


def sanitize_folder_name(folder_name):
    valid_characters = re.sub(r'[<>:."/\\|?*]', "", folder_name)
    return valid_characters.strip()


def is_docker():
    return os.getenv("IN_DOCKER") is not None


def get_current_page(path: str, default_page=1) -> int:
    if os.path.exists(path):
        with open(path, "r") as file:
            page_string = file.read().strip()
            if page_string:
                return int(page_string)
            else:
                with open(path, "w") as file:
                    file.write(str(default_page))
                return default_page
    else:
        with open(path, "w") as file:
            file.write(str(default_page))
        return default_page


def update_current_page(path: str, page: int) -> None:
    with open(path, "w") as file:
        file.write(str(page))


def normalize_for_matching(text: str) -> str:
    """Normalize text for better matching by handling common variations"""
    # Convert to lowercase
    text = text.lower()
    # Replace underscores with spaces
    text = text.replace("_", " ")
    # Remove common punctuation that might vary
    text = re.sub(r"[^\w\s]", " ", text)
    # Normalize multiple spaces to single space
    text = re.sub(r"\s+", " ", text)
    # Strip whitespace
    return text.strip()


def jaccard_similarity(str_a: str, str_b: str) -> tuple[float, int, int]:
    """Calculate Jaccard similarity between two strings based on word tokens.

    Args:
        str_a: First string
        str_b: Second string

    Returns:
        Tuple of (jaccard_score, overlap_count, union_count)
    """
    set_a = set(normalize_for_matching(str_a).split())
    set_b = set(normalize_for_matching(str_b).split())

    if not set_a or not set_b:
        return 0.0, 0, 0

    intersection = set_a & set_b
    union = set_a | set_b

    overlap_count = len(intersection)
    union_count = len(union)

    score = overlap_count / union_count if union_count > 0 else 0.0
    return score, overlap_count, union_count


def length_ratio(str_a: str, str_b: str) -> float:
    """Calculate length ratio between two strings.

    Returns:
        Ratio of shorter/longer string (0.0-1.0)
    """
    len_a, len_b = len(str_a), len(str_b)
    if len_a == 0 or len_b == 0:
        return 0.0
    return min(len_a, len_b) / max(len_a, len_b)


def extract_author_title(filename: str) -> tuple[str, str]:
    """Extract author and title components from a filename.

    Tries common separators: " - ", " _ ", " by ".
    Handles both "Author - Title" and "Title - Author" patterns.
    Strips file extension before parsing.

    Args:
        filename: Filename to parse

    Returns:
        Tuple of (part1, part2) or (filename, "") if no separator found
    """
    # Remove extension
    name = filename.rsplit(".", 1)[0] if "." in filename else filename

    # Try common separators in order of preference
    separators = [" - ", " _ ", " by "]

    for sep in separators:
        if sep in name:
            parts = name.split(sep, 1)
            if len(parts) == 2:
                return parts[0].strip(), parts[1].strip()

    return name.strip(), ""


def title_contained_in_filename(target_title: str, filename: str) -> bool:
    """Check if the target title is contained in the filename with fuzzy matching"""
    normalized_target = normalize_for_matching(target_title)
    normalized_filename = normalize_for_matching(filename)

    # Check direct containment
    if normalized_target in normalized_filename:
        return True

    # Check word-by-word containment for partial matches
    target_word_list = normalized_target.split()
    if not target_word_list:
        return False

    target_words = set(target_word_list)
    filename_words = set(normalized_filename.split())

    # Filter out common stop words to avoid false positives on "The", "A", etc.
    filtered_target_words = {w for w in target_words if w not in STOP_WORDS}

    # If all words were stop words (unlikely), revert to full set
    if not filtered_target_words:
        filtered_target_words = target_words

    overlap = len(filtered_target_words.intersection(filename_words))

    # Require 80% overlap of meaningful words (up from 70% of all words)
    if overlap < len(filtered_target_words) * 0.8:
        return False

    # --- NEW: Order Check for all titles ---
    # Identify matching words from the target_title that are present in the filename
    matching_words_in_order = [w for w in target_word_list if w in filename_words]

    if matching_words_in_order:
        # Construct a regex to verify that these words appear in the filename in that specific sequence
        # Use \b for word boundaries to avoid matching parts of words
        pattern = r".*".join([rf"\b{re.escape(w)}\b" for w in matching_words_in_order])
        if not re.search(pattern, normalized_filename):
            return False

    # For short titles (<= 2 words), enforce stricter proximity to avoid false positives
    # e.g. "Dark One" shouldn't match "Dark Watch Volume One" (too many words in between)
    if len(filtered_target_words) <= 2:
        # Get meaningful words in their original order
        ordered_filtered_words = [w for w in target_word_list if w in filtered_target_words]

        # Build pattern: word1 + (0-1 intervening words) + word2
        # Escaping words to handle special regex chars if any
        pattern_parts = [re.escape(w) for w in ordered_filtered_words]
        proximity_pattern = r"(?:\s+\S+){0,1}\s+".join(pattern_parts)

        if not re.search(proximity_pattern, normalized_filename):
            return False

    return True
