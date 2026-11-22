"""Conservative Chaptarr/Readarr edition selection and import read-back checks.

A completed ManualImport command is not proof that Chaptarr attached the files to the
requested book. Chaptarr can remap a supplied bookId/editionId while importing. Read back
new bookfile IDs on the requested book and edition before declaring success. All snapshots
must be taken BEFORE the command is submitted; a pre-existing file is not proof.
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

AUDIOBOOK = "audiobook"
EBOOK = "ebook"


def edition_type(edition: dict) -> Optional[str]:
    """Chaptarr's structured discriminator: 1 physical, 2 audio, 3 ebook.

    An absent discriminator and isEbook=False do not mean audiobook: it might be a
    physical edition. Format strings are provider prose, not reliable identifiers.
    """
    format_id = edition.get("readingFormatId")
    if format_id not in (None, "", 0, "0"):
        try:
            return {1: "physical", 2: AUDIOBOOK, 3: EBOOK}.get(int(format_id), "unknown")
        except (TypeError, ValueError):
            return "unknown"
    if edition.get("isEbook") is True:
        return EBOOK
    return None


def select_edition(book: dict, editions: Any, media_type: str) -> Optional[dict]:
    """Select the edition to import for, or None if there is no safe choice.

    In order:
    1. the one monitored edition of the right medium (readingFormatId: 2 audio, 3 ebook);
    2. the only edition of the right medium;
    3. on a server without media types (plain Readarr, ebooks only): the one monitored
       edition, or the only one.
    Several editions of the right medium with none or several monitored are a choice between
    recordings or narrators that R:soul can't make: refused, with the editions in the log.
    """
    rows = [e for e in editions if isinstance(e, dict) and e.get("id")] if isinstance(editions, list) else []
    book_type = str(book.get("mediaType") or "").lower()
    if book_type and book_type in (AUDIOBOOK, EBOOK) and book_type != media_type:
        logger.warning("Targeted import rejected: book's media type %s is not %s", book_type, media_type)
        return None

    typed = [e for e in rows if edition_type(e) == media_type]
    monitored_typed = [e for e in typed if e.get("monitored") is True]
    if len(monitored_typed) == 1:
        return monitored_typed[0]
    if len(typed) == 1:
        return typed[0]

    if not typed:
        # No edition names the medium. Only plain Readarr (no media types at all, ebooks only)
        # gets the benefit of the doubt: an untyped edition on Chaptarr may be a physical one.
        record_is_right_medium = not book_type and media_type == EBOOK
        untyped = [e for e in rows if edition_type(e) is None]
        monitored_untyped = [e for e in untyped if e.get("monitored") is True]
        if record_is_right_medium and len(untyped) == len(rows):
            if len(monitored_untyped) == 1:
                return monitored_untyped[0]
            if len(untyped) == 1:
                return untyped[0]

    logger.warning(
        "Targeted import: no safe %s edition for book %s among %d (%d of that medium, %d of them monitored): %s",
        media_type, book.get("id", "?"), len(rows), len(typed), len(monitored_typed),
        [(e.get("id"), e.get("readingFormatId"), "monitored" if e.get("monitored") else "", (e.get("title") or "")[:40]) for e in rows[:12]],
    )
    return None


def bookfile_rows(client: Any, book_id: Any) -> Optional[list]:
    """Retrieve a book's attached files, None when identity cannot be inspected."""
    try:
        rows = client._get("bookfile", client.ver_uri, params={"bookId": book_id})
    except Exception as exc:
        logger.warning("Cannot read bookfile records for book %s: %s", book_id, exc)
        return None
    if isinstance(rows, dict):
        rows = rows.get("records")
    if not isinstance(rows, list) or any(not isinstance(row, dict) or row.get("id") is None for row in rows):
        logger.warning("Cannot verify book %s: Chaptarr returned an unsupported bookfile response", book_id)
        return None
    return rows


def bookfile_ids_before(client: Any, book_id: Any) -> Optional[list]:
    """Save the pre-import baseline; without it an existing file can look newly imported."""
    rows = bookfile_rows(client, book_id)
    return sorted({str(row["id"]) for row in rows}) if rows is not None else None


def verify_new_bookfiles(client: Any, book_id: Any, edition_id: Any, before_ids: Any, expected_files: int = 0) -> Optional[bool]:
    """Did the import attach new files to the wanted book?

    True  = new book files appeared on the wanted book.
    False = a new file is there, but Chaptarr itself reports another book or edition for it.
    None  = nothing new on the wanted book (yet), or it can't be checked.

    What counts as proof is the query, not the rows: `bookfile?bookId=N` returns the files of
    book N, so a file ID that wasn't there before the import is a file the import attached to
    that book. The rows themselves say little. Chaptarr's book-file resource has no
    `editionId` at all, and its `bookId` is 0 when the edition isn't loaded with the file
    (checked against Chaptarr v0.9.1000), so neither field may be required. They are only
    used where a server does report them and they contradict the request.
    """
    if before_ids is None:
        return None
    rows = bookfile_rows(client, book_id)
    if rows is None:
        return None
    old = {str(i) for i in before_ids}
    fresh = [row for row in rows if str(row["id"]) not in old]
    if not fresh:
        logger.warning("Book %s: the import finished, but no new book file has appeared on the wanted record", book_id)
        return None

    def contradicts(row: dict) -> bool:
        reported_book = row.get("bookId")
        reported_edition = row.get("editionId")
        wrong_book = reported_book not in (None, 0, "0", "") and str(reported_book) != str(book_id)
        wrong_edition = edition_id is not None and reported_edition not in (None, 0, "0", "") and str(reported_edition) != str(edition_id)
        return wrong_book or wrong_edition

    mismatched = [row for row in fresh if contradicts(row)]
    if mismatched:
        logger.error(
            "WRONG IMPORT DESTINATION: wanted book %s edition %s, but new bookfile(s) reported %s",
            book_id, edition_id, [(r.get("id"), r.get("bookId"), r.get("editionId")) for r in mismatched],
        )
        return False
    if expected_files and len(fresh) < expected_files:
        logger.warning("Book %s: %d file(s) were imported, %d new book file(s) appeared on the wanted record", book_id, expected_files, len(fresh))
    logger.info("Verified %d new book file(s) %s on book %s", len(fresh), [row["id"] for row in fresh], book_id)
    return True
