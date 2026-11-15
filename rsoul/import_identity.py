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
    """Select one unambiguous edition of the right medium, or fail closed.

    Legacy Readarr is ebook-only and may lack the Chaptarr format fields entirely.
    Its untyped editions are acceptable for ebook-only records without mediaType.
    Chaptarr records and all audiobooks require positively identified formats.
    """
    rows = [e for e in editions if isinstance(e, dict) and e.get("id")] if isinstance(editions, list) else []
    book_type = str(book.get("mediaType") or "").lower()
    if book_type and book_type in (AUDIOBOOK, EBOOK) and book_type != media_type:
        logger.warning("Targeted import rejected: book's media type %s is not %s", book_type, media_type)
        return None
    candidates = [e for e in rows if edition_type(e) == media_type]
    if media_type == EBOOK and not book_type and not candidates:
        # The old Readarr endpoint is ebook-only. Do not accept an explicitly physical or
        # explicitly audiobook edition just because it was returned beside an untyped one.
        if rows and all(edition_type(e) is None for e in rows):
            candidates = rows
    if not candidates:
        logger.warning("Targeted import: no positively identified %s edition for book %s", media_type, book.get("id", "?"))
        return None
    monitored = [e for e in candidates if e.get("monitored") is True]
    if len(monitored) == 1:
        return monitored[0]
    if len(candidates) == 1:
        return candidates[0]
    logger.warning(
        "Targeted import: book %s has %d possible %s editions (%d monitored); cannot select narrator/edition safely",
        book.get("id", "?"), len(candidates), media_type, len(monitored),
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


def verify_new_bookfiles(client: Any, book_id: Any, edition_id: Any, before_ids: Any) -> Optional[bool]:
    """True=new attachment on requested book+edition; False=provable misassignment;
    None=not yet verifiable. In particular 'all staging files disappeared' is not proof.
    """
    if edition_id is None or before_ids is None:
        return None
    rows = bookfile_rows(client, book_id)
    if rows is None:
        return None
    old = {str(i) for i in before_ids}
    fresh = [row for row in rows if str(row["id"]) not in old]
    if not fresh:
        logger.warning("Book %s, edition %s: import completed but no NEW bookfile has appeared on the requested record", book_id, edition_id)
        return None
    # Some older endpoints omit the identifiers: do not infer them from the query filter.
    if any(row.get("bookId") is None or row.get("editionId") is None for row in fresh):
        logger.warning("Book %s: new bookfile is missing bookId/editionId; import remains unverified", book_id)
        return None
    mismatched = [row for row in fresh if str(row["bookId"]) != str(book_id) or str(row["editionId"]) != str(edition_id)]
    if mismatched:
        logger.error(
            "WRONG IMPORT DESTINATION: wanted book %s edition %s, but new bookfile(s) reported %s",
            book_id, edition_id, [(r.get("id"), r.get("bookId"), r.get("editionId")) for r in mismatched],
        )
        return False
    logger.info("Verified new bookfile(s) %s on book %s, edition %s", [row["id"] for row in fresh], book_id, edition_id)
    return True
