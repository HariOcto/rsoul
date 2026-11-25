"""Conservative Chaptarr/Readarr edition selection and import read-back checks.

A completed ManualImport command is not proof that Chaptarr attached the files to the
requested book. Chaptarr can remap a supplied bookId/editionId while importing. Read back
the intended work's new bookfile records and correlate EVERY manifest input with an
output before declaring success. All snapshots must precede submission. A new ID,
a count match, or the disappearance of a source file is not by itself proof.
"""

import logging
import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
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


_NAME_SPLIT = re.compile(r"\s*(?:[;/|&+]|\s-\s|\band\b|\bund\b)\s*", re.IGNORECASE)
_NAME_WORDS = re.compile(r"[^\W\d_]+(?:['-][^\W\d_]+)*")
_UNNAMED_NARRATORS = {
    "unknown narrator", "various narrators", "various artists", "multiple narrators", "full cast",
}


def _narrator_texts(value: Any):
    """Only explicit text is evidence; objects must never become invented names."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _narrator_texts(item)


def _person_name(words):
    # Keep order and all tokens (including repeated initials), never a surname
    # fragment. Only an explicit comma inversion can change the name order.
    if len(words) < 2 or " ".join(words) in _UNNAMED_NARRATORS:
        return None
    return tuple(words)


def narrator_names(value: Any) -> set:
    """Conservative complete personal identities from narrator metadata.

    Natural names and simple inverted names compare equally, while a surname
    alone is never enough. A two-field name with a single-word field is treated
    as ``surname, given``. Repeated unambiguous inverted pairs are supported.
    Multiword/multiword comma fields could be a compound inverted name or a
    cast list, so neither fragment supplies evidence. Semicolons, "and", or
    separate tag values disambiguate complete cast names. This does not guess
    initials or missing name parts.
    """
    names = set()
    for text in _narrator_texts(value):
        normalized = unicodedata.normalize("NFKC", text).casefold().replace("\u2019", "'")
        for part in _NAME_SPLIT.split(normalized):
            fields = [_NAME_WORDS.findall(field.strip()) for field in part.split(",")]
            if not fields or any(not field for field in fields):
                continue
            if len(fields) == 1:
                people = fields
            elif len(fields) % 2 == 0 and all(
                len(fields[index]) == 1 or len(fields[index + 1]) == 1
                for index in range(0, len(fields), 2)
            ):
                people = [fields[index + 1] + fields[index] for index in range(0, len(fields), 2)]
            else:
                continue
            for words in people:
                identity = _person_name(words)
                if identity is not None:
                    names.add(identity)
    return names


def _edition_narrators(edition: dict) -> set:
    return narrator_names([edition.get("narrator"), edition.get("narratorNames"), edition.get("narrators")])


def select_edition(book: dict, editions: Any, media_type: str, narrators: Any = None) -> Optional[dict]:
    """Select the edition to import for, or None if there is no safe choice.

    In order:
    1. the one monitored edition of the right medium (readingFormatId: 2 audio, 3 ebook);
    2. the only edition of the right medium;
    3. when several eligible audio editions remain, complete narrator identities
       from the downloaded files may identify exactly one of those editions;
    4. when the book record itself is of the wanted medium (Chaptarr keeps one record per
       medium) or the server has no media types (plain Readarr, ebooks only): the record's one
       monitored edition, or its only edition, whatever format the edition names.

    Why 4 is safe on Chaptarr: its manual import only checks that the edition belongs to the
    book (ManualEditionSelectionMatchesBook, v0.9.1000); the medium of the import comes from
    the record and the files, not from the edition. And real audiobook records often carry
    only ebook or print editions from the metadata provider (seen: "The Algebraist" with one
    ebook edition, "Cleaning up" with two print editions), so insisting on an audio edition
    would block those books for good.
    Several eligible editions without a unique narrator-supported choice are
    refused, with the editions in the log.
    """
    rows = [e for e in editions if isinstance(e, dict) and e.get("id")] if isinstance(editions, list) else []
    book_type = str(book.get("mediaType") or "").strip().lower()
    if book_type and book_type in (AUDIOBOOK, EBOOK) and book_type != media_type:
        logger.warning("Targeted import rejected: book's media type %s is not %s", book_type, media_type)
        return None

    typed = [e for e in rows if edition_type(e) == media_type]
    monitored_typed = [e for e in typed if e.get("monitored") is True]
    if len(monitored_typed) == 1:
        return monitored_typed[0]
    if len(typed) == 1:
        return typed[0]

    # Several editions of the medium and no single monitored one: the narrator tags of the
    # downloaded files may single one out. Require every known complete name;
    # a shared member of otherwise different casts cannot resolve the choice.
    pool = monitored_typed or typed
    heard = narrator_names(narrators or [])
    if len(pool) > 1 and heard:
        matches = [e for e in pool if heard <= _edition_narrators(e)]
        if len(matches) == 1:
            logger.info("Targeted import: edition %s chosen by the narrator named in the audio tags", matches[0].get("id"))
            return matches[0]

    record_is_right_medium = book_type == media_type or (not book_type and media_type == EBOOK)
    if record_is_right_medium and not typed:
        monitored = [e for e in rows if e.get("monitored") is True]
        if len(monitored) == 1:
            return monitored[0]
        if len(rows) == 1:
            return rows[0]

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


def path_key(path: Any) -> str:
    """Compare complete server paths, never a basename or a fuzzy title."""
    if not isinstance(path, str) or not path:
        return ""
    path = os.path.normpath(path.replace("\\", "/"))
    return path.casefold() if re.match(r"^[A-Za-z]:/", path) else path


def file_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _event_time(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError):
        return None


def history_rows(client: Any, book_id: Any, since: float) -> tuple:
    """Bounded, book-filtered read of import history; unavailable is never success.

    Field names follow Chaptarr HistoryController/HistoryService at
    04c1ed1fba2ba08b72ea5e80f85578eaa076ffe3. Data dictionary casing differs
    between server serializers, so its keys are compared case-insensitively.
    """
    collected = []
    try:
        for page in range(1, 11):
            response = client._get("history", client.ver_uri, params={
                "bookId": book_id, "page": page, "pageSize": 100,
                "sortKey": "date", "sortDirection": "descending",
            })
            rows = response.get("records") if isinstance(response, dict) else response
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                return collected, "unsupported import-history response"
            collected.extend(rows)
            times = [_event_time(r.get("date")) for r in rows]
            if len(rows) < 100 or (times and all(t is not None and t < since for t in times)):
                return collected, ""
            if isinstance(response, dict) and isinstance(response.get("totalRecords"), int) and page * 100 >= response["totalRecords"]:
                return collected, ""
        return collected, "import-history inspection reached its page limit"
    except Exception as exc:
        return collected, f"import history unavailable: {exc}"


def readable_exclusions(data: dict) -> Optional[list]:
    """ExcludedSourcePaths of a history receipt: [] when absent, the list of paths when readable,
    None when present in any other form.

    Chaptarr writes the field only when something was excluded, always as a JSON list of path
    strings (HistoryService.cs, BookFileImported). An empty string, "null", an object, or a list
    with anything but non-empty strings is not something it writes, so it is not read as
    "nothing excluded"."""
    if "excludedsourcepaths" not in data:
        return []
    value = data["excludedsourcepaths"]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, list) or not all(isinstance(p, str) and p.strip() for p in value):
        return None
    return value


def input_size_agrees(data: dict, entry: dict) -> bool:
    """A plain import receipt's Size against the file R:soul staged.

    On BookFileImported Chaptarr records the size of the file it picked up (BookInfo.Size,
    falling back to the imported file, then 0), so it is compared with the staged input, not
    the library file, which tagging may change. A Size of 0 or none is no evidence either way;
    an unreadable one, or one that differs from the staged file's, means another file."""
    raw = data.get("size")
    if raw in (None, ""):
        return True
    try:
        size = int(raw)
    except (TypeError, ValueError):
        return False
    if size == 0:
        return True
    staged = entry.get("size")
    return not (isinstance(staged, int) and not isinstance(staged, bool) and staged > 0) or size == staged


def verify_import_evidence(client: Any, pending: dict) -> dict:
    """Correlate every immutable input with fresh files on the wanted record.

    Valid proof is a readable final file with the staged SHA-256, or a dated
    successful import-history event linking the EXACT staged server path to a
    fresh final FileId/ImportedPath. Explicit successful conversion history can
    link several inputs to one output. Counts and title/filename similarity are
    never substitutes for this linkage. Missing editionId is supported.
    """
    manifest = pending.get("manifest") or []
    result = {"status": "unverified", "matched": [], "missing": [], "output_ids": [], "reason": ""}
    if not manifest or any(not isinstance(f, dict) or not f.get("readarr_path") for f in manifest):
        result["reason"] = "No complete input manifest is available"
        return result
    inputs = {path_key(f["readarr_path"]): f for f in manifest}
    result["missing"] = list(inputs)
    if len(inputs) != len(manifest) or not all(inputs):
        result["reason"] = "Input manifest has ambiguous paths"
        return result
    before = pending.get("before_ids")
    if before is None:
        result["reason"] = "The pre-import file baseline was not recorded"
        return result
    rows = bookfile_rows(client, pending.get("book_id"))
    if rows is None:
        result["reason"] = "Cannot inspect files on the intended book"
        return result
    old = {str(i) for i in before}
    fresh = {str(r["id"]): r for r in rows if str(r["id"]) not in old}
    if not fresh:
        result["reason"] = "No new files on the intended book"
        return result
    for row in fresh.values():
        wrong_book = row.get("bookId") not in (None, 0, "0", "") and str(row["bookId"]) != str(pending.get("book_id"))
        edition = pending.get("edition_id")
        wrong_edition = edition is not None and row.get("editionId") not in (None, 0, "0", "") and str(row["editionId"]) != str(edition)
        intended = pending.get("intended_identity") or {}
        wrong_author = intended.get("author_id") is not None and row.get("authorId") not in (None, 0, "0", "") and str(row["authorId"]) != str(intended["author_id"])
        own_media = str(row.get("mediaType") or "").strip().casefold()
        wrong_media = bool(own_media and pending.get("media_type") and own_media != pending["media_type"])
        if wrong_book or wrong_edition or wrong_author or wrong_media:
            result.update(status="mismatch", reason="Server reports another book, author, edition, or medium for a new file")
            return result

    matches = {}
    output_sources = {}
    conversion_outputs = {}
    since = float(pending.get("since") or 0)
    events, history_error = history_rows(client, pending.get("book_id"), since)
    for event in events:
        # Never trust a server's query filtering alone for cross-operation history.
        if str(event.get("bookId")) != str(pending.get("book_id")):
            continue
        author_id = (pending.get("intended_identity") or {}).get("author_id")
        if author_id is not None and event.get("authorId") not in (None, 0, "0", "") and str(event["authorId"]) != str(author_id):
            continue
        stamp = _event_time(event.get("date"))
        if stamp is None or stamp < since:
            continue
        event_type = event.get("eventType")
        # Pinned Chaptarr EntityHistoryEventType: 3 imported, 11 converted.
        # Numeric 7 (incomplete), 8 (download summary), 12 (conversion failed)
        # intentionally do not prove any input was successfully imported.
        kind = {3: "bookfileimported", 11: "bookfileconverted"}.get(event_type) if isinstance(event_type, int) and not isinstance(event_type, bool) else None
        kind = kind or re.sub(r"[^a-z]", "", str(event_type or "").casefold())
        if kind not in ("bookfileimported", "bookfileconverted", "downloadfolderimported"):
            continue
        raw = event.get("data")
        if not isinstance(raw, dict):
            continue
        data = {str(k).casefold(): v for k, v in raw.items()}
        imported = path_key(data.get("importedpath"))
        file_id = str(data.get("fileid") or "")
        row = fresh.get(file_id) if file_id else None
        if row is None and not file_id:
            candidates = [r for r in fresh.values() if imported and path_key(r.get("path")) == imported]
            row = candidates[0] if len(candidates) == 1 else None
        if row is None or not imported or path_key(row.get("path")) != imported:
            continue
        if not isinstance(row.get("size"), (int, float)) or isinstance(row["size"], bool) or row["size"] <= 0:
            continue
        output_id = str(row["id"])
        excluded = readable_exclusions(data)
        if excluded is None:
            continue  # present but not a list of paths: what was left out is unknown
        if any(path_key(s) in inputs for s in excluded):
            continue
        source_paths = data.get("sourcepaths")
        if isinstance(source_paths, str):
            try:
                source_paths = json.loads(source_paths)
            except (ValueError, TypeError):
                source_paths = None
        if source_paths is not None:
            # Pinned Chaptarr also puts GeneratedConversionSourcePaths on a
            # successful BookFileImported event (HistoryService.cs 507-509).
            # That alternate conversion receipt carries the final FileId and
            # Size. A folder-level summary has no equivalent per-output proof.
            if kind not in ("bookfileconverted", "bookfileimported"):
                continue
            if not isinstance(source_paths, list) or not source_paths or not all(isinstance(p, str) for p in source_paths):
                continue
            sources = [path_key(p) for p in source_paths]
            if len(set(sources)) != len(sources) or any(s not in inputs for s in sources):
                continue  # A conversion involving unrequested inputs is not this release.
            count = data.get("sourcefilecount")
            if count not in (None, ""):
                try:
                    if int(count) != len(sources):
                        continue
                except (ValueError, TypeError):
                    continue
            if kind == "bookfileconverted":
                try:
                    if int(data.get("outputsize") or 0) != int(row["size"]):
                        continue
                except (ValueError, TypeError):
                    continue
            else:
                if not file_id:
                    continue
                try:
                    if int(data.get("size") or 0) != int(row["size"]):
                        continue
                except (ValueError, TypeError):
                    continue
            conversion_outputs.setdefault(output_id, set()).add(frozenset(sources))
        else:
            if kind == "bookfileconverted":
                continue
            sources = [path_key(data.get("droppedpath"))]
            if sources[0] not in inputs:
                continue
            if not input_size_agrees(data, inputs[sources[0]]):
                continue  # the receipt is for a file of another size than the one staged
        for source in sources:
            matches[source] = output_id
            output_sources.setdefault(output_id, set()).add(source)

    # Hash proof permits servers without history, provided the destination is mounted.
    # One fresh output can account for only one unchanged input, even for identical bytes.
    for output_id, row in fresh.items():
        if output_id in output_sources:
            continue
        path = row.get("path")
        if not isinstance(path, str) or not os.path.isfile(path) or os.path.islink(path):
            continue
        try:
            size = os.path.getsize(path)
            candidates = [s for s, f in inputs.items() if s not in matches and f.get("sha256") and f.get("size") == size]
            if not candidates:
                continue
            digest = file_digest(path)
            matching = [s for s in candidates if inputs[s]["sha256"] == digest]
            if matching:
                matches[matching[0]] = output_id
                output_sources[output_id] = {matching[0]}
        except OSError:
            continue
    # Several ordinary import events pointing at one overwritten file are not a merge.
    unsafe_outputs = set()
    for output_id, sources in output_sources.items():
        groups = conversion_outputs.get(output_id, set())
        if len(groups) > 1:
            unsafe_outputs.add(output_id)  # Conflicting conversion receipts for one output.
        elif groups and not sources.issubset(next(iter(groups))):
            unsafe_outputs.add(output_id)  # An unrelated ordinary event cannot enlarge a merge.
        elif len(sources) > 1 and not groups:
            unsafe_outputs.add(output_id)
    matches = {s: i for s, i in matches.items() if i not in unsafe_outputs}
    result.update(matched=sorted(matches), missing=sorted(set(inputs) - set(matches)), output_ids=sorted(set(matches.values())))
    if len(matches) == len(inputs):
        result.update(status="verified", reason="Every staged input is linked to an output on the intended book", conversion=bool(conversion_outputs))
    elif matches:
        result.update(status="partial", reason=f"Only {len(matches)} of {len(inputs)} inputs have correlated output evidence")
    else:
        result["reason"] = history_error or "New book files have no reliable linkage to the staged inputs"
    return result


def verify_new_bookfiles(client: Any, book_id: Any, edition_id: Any, before_ids: Any,
                         expected_files: int = 0, manifest: Optional[list] = None,
                         since: float = 0) -> Optional[bool]:
    """Compatibility API: complete correlated evidence, never a file-count shortcut.

    Older callers without a manifest now get an unverified result. The detailed
    transaction API preserves partial/mismatch diagnostics for follow-up.
    """
    proof = verify_import_evidence(client, {"book_id": book_id, "edition_id": edition_id,
        "before_ids": before_ids, "manifest": manifest, "since": since})
    if proof["status"] == "verified":
        return True
    if proof["status"] == "mismatch":
        return False
    return None
