"""Recoverable import transactions.

The input manifest and intended record identity are immutable after submission.
Every filesystem move and every import POST has a durable intent before it. A lost
POST response is an unknown outcome, never permission to submit a second command.
"""

import copy
import errno
import hashlib
import json
import logging
import os
import re
import time
import unicodedata
import uuid
from pathlib import Path
from types import SimpleNamespace

from . import audiotags, health, import_identity, notify
from .library import CatalogUnavailable
from .outcomes import check_fatal_state
from .persistence import StatePersistenceError, atomic_write_json, fsync_directory
from .recording_policy import validate_recording_files
from .utils import sanitize_folder_name

logger = logging.getLogger(__name__)
SCHEMA = 2
JOURNAL_DIR = ".rsoul-import-journal"
TERMINAL = {"completed", "failed", "aborted", "cancelled", "orphaned"}
MAX_AGE = 24 * 3600


def _text_key(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", str(value or "")).casefold()))


def _inside(root, path):
    try:
        return os.path.commonpath([os.path.realpath(root), os.path.realpath(path)]) == os.path.realpath(root)
    except (ValueError, TypeError):
        return False


def _disabled(config):
    return bool(config is not None and config.getboolean("Readarr", "disable_sync", fallback=False))


def _pause_operator_discard(pending):
    if not pending.get("operator_discard"):
        return False
    # The separate discard journal owns the paths and checkpoint. Ordinary import entry
    # points must not reconcile, move, or submit these files while the operator resumes it.
    pending.update(phase="needs_review", reason="An operator discard is incomplete; resume --discard before normal import processing")
    return True


def _identity(raw, item=None):
    raw = raw if isinstance(raw, dict) else {}
    item = item or {}
    pairs = {
        "book_id": ("book_id", "bookId", "id"),
        "author_id": ("author_id", "authorId"),
        "foreign_book_id": ("foreign_book_id", "foreignBookId"),
        "foreign_author_id": ("foreign_author_id", "foreignAuthorId"),
        "title": ("title", "book_title"),
        "author_name": ("author_name", "authorName"),
        "media_type": ("media_type", "mediaType"),
        "record_media_type": ("record_media_type",),
    }
    result = {}
    for key, aliases in pairs.items():
        result[key] = next((raw[k] for k in aliases if raw.get(k) not in (None, "")), None)
    for key, fallback in (("book_id", item.get("bookId", item.get("book_id"))),
                          ("title", item.get("title", item.get("book_title", ""))),
                          ("author_name", item.get("author_name", "")),
                          ("media_type", item.get("media_type", "ebook"))):
        if result[key] in (None, ""):
            result[key] = fallback
    return result


def inspect_identity(client, pending):
    """Re-read and compare the saved work/author/medium before submit AND read-back.

    First-time pins can be enriched only after the saved title and author's name
    agree. A numeric book ID reused after a database restore is not sufficient.
    """
    expected = _identity(pending.get("intended_identity"), {
        "bookId": pending.get("book_id"), "media_type": pending.get("media_type", "ebook")})
    if not expected.get("book_id") or not expected.get("title") or not expected.get("author_name"):
        return None, None, "Saved author/work identity is incomplete; manual review is required"
    try:
        book = client.get_book(expected["book_id"])
        if not isinstance(book, dict) or not book.get("authorId") or not book.get("title"):
            return None, None, "The intended book or author cannot be identified by the API"
        if book.get("id") is not None and str(book["id"]) != str(expected["book_id"]):
            return None, None, "The book API returned a different record ID"
        if _text_key(book["title"]) != _text_key(expected["title"]):
            return None, None, "The saved book ID now names another title"
        author = book.get("author")
        if not isinstance(author, dict) or not author.get("authorName"):
            author = client.get_author(book["authorId"])
        if not isinstance(author, dict) or not author.get("authorName"):
            return None, None, "The intended author's name cannot be read"
        if author.get("id") not in (None, 0, "0", "") and str(author["id"]) != str(book["authorId"]):
            return None, None, "The author API returned another author ID"
        if _text_key(author["authorName"]) != _text_key(expected["author_name"]):
            return None, None, "The saved book ID now belongs to another author"
        current = dict(expected, author_id=book["authorId"],
                       foreign_book_id=book.get("foreignBookId"),
                       foreign_author_id=author.get("foreignAuthorId"))
        for key in ("author_id", "foreign_book_id", "foreign_author_id"):
            if expected.get(key) not in (None, "") and str(expected[key]) != str(current.get(key)):
                return None, None, f"The saved {key} has changed or cannot be verified"
        own_type = str(book.get("mediaType") or "").strip().casefold()
        media = str(expected.get("media_type") or "ebook").strip().casefold()
        if media not in ("ebook", "audiobook") or (own_type and own_type != media):
            return None, None, "The intended record's media type no longer matches"
        if expected.get("record_media_type") not in (None, "") and own_type != expected["record_media_type"]:
            return None, None, "The record's previously verified media discriminator is now absent or changed"
        current["media_type"] = media
        current["record_media_type"] = own_type
        return book, current, ""
    except Exception as exc:
        return None, None, f"Cannot verify intended author/work identity: {exc}"


def _checkpoint(pending, persist=None):
    """A failed checkpoint is fatal to the next mutation, including a retry POST."""
    value = copy.deepcopy(pending)
    if persist is not None:
        if persist(value) is False:
            raise StatePersistenceError("Import checkpoint was refused")
        return
    path = pending.get("journal_path")
    if not path:
        raise StatePersistenceError("Import has no durable checkpoint writer; refusing mutation")
    atomic_write_json(path, value)


def context_writer(ctx, item, pending):
    """Central state when available; a durable recoverable sidecar otherwise."""
    check_fatal_state(ctx)
    state = getattr(ctx, "state", None)
    task_id = item.get("task_id")
    if state is not None and task_id:
        if getattr(state, "read_only", False):
            raise StatePersistenceError("Recovery state is read-only; import mutations are disabled")
        def persist(value):
            check_fatal_state(ctx)
            extra = copy.deepcopy(item.get("_task_extra") or {})
            # Preserve state fields another phase may have checkpointed since completion.
            for saved in state.get_tasks_for_orchestrator():
                if saved.get("task_id") == task_id:
                    extra = copy.deepcopy(saved.get("extra") or extra)
                    break
            extra["import_pending"] = value
            return state.set_extra(task_id, extra)
        persist._rsoul_before_mutation = lambda: check_fatal_state(ctx)
        return persist
    root = pending["download_root"]
    if not os.path.isdir(root):
        raise StatePersistenceError(f"Download mount is unavailable: {root}")
    directory = os.path.join(root, JOURNAL_DIR)
    if not _inside(root, directory) or os.path.islink(directory):
        raise StatePersistenceError("Import journal directory escapes the configured download mount")
    os.makedirs(directory, exist_ok=True)
    fsync_directory(root)
    pending["journal_path"] = os.path.join(directory, pending["operation_id"] + ".json")
    def persist(value):
        check_fatal_state(ctx)
        atomic_write_json(pending["journal_path"], value)
    persist._rsoul_before_mutation = lambda: check_fatal_state(ctx)
    return persist


def _before_mutation(persist):
    guard = getattr(persist, "_rsoul_before_mutation", None)
    if guard:
        guard()


def _attention_previous(pending, previous):
    """Record one notification decision per unresolved attempt before announcing it.

    The pre-POST intent is already submission_unknown for crash safety. Only reconciliation
    or an actually missing/invalid response reaches this helper, so that safety checkpoint
    neither emits a premature warning nor suppresses the later one. The marker records a
    best-effort notice, not delivery success; notification failures are never retried.
    """
    if pending.get("phase") != "submission_unknown":
        return previous
    attempts = pending.get("attempts") or []
    holder = attempts[-1] if attempts and isinstance(attempts[-1], dict) else pending
    if holder.get("submission_unknown_notice_recorded_at") is not None:
        return "submission_unknown"
    holder["submission_unknown_notice_recorded_at"] = time.time()
    return None


def _retain(pending, phase, reason, persist=None, **fields):
    previous = pending.get("phase")
    pending.update(phase=phase, reason=reason, last_checked=time.time(), **fields)
    previous = _attention_previous(pending, previous)
    _checkpoint(pending, persist)
    notify.phase_entered(pending, previous, reason)
    logger.warning("Import %s: %s [%s]", pending.get("book_id"), reason, phase)
    return pending


def _source_task(item, operation_id):
    extra = copy.deepcopy(item.get("_task_extra") or {})
    extra.pop("import_pending", None)
    for key in ("media_type", "expected_files", "files", "full_dir"):
        if key in item:
            extra.setdefault(key, copy.deepcopy(item[key]))
    return {
        "task_id": item.get("task_id") or "import-" + operation_id,
        "book_id": item.get("bookId"), "book_title": item.get("title", ""),
        "author_name": item.get("author_name", ""), "backend_name": item.get("backend_name", "slskd"),
        "filename": item.get("filename", ""), "series_title": item.get("seriesTitle", ""),
        "local_dir": item.get("dir", ""), "extra": extra,
    }


def new_transaction(item, local_root, server_root, method):
    operation_id = uuid.uuid4().hex
    media = item.get("media_type", "ebook")
    names = item.get("expected_files") or [re.split(r"[\\/]", f.get("filename", ""))[-1]
                                           for f in item.get("files", [])]
    if not names and item.get("filename"):
        names = [re.split(r"[\\/]", item["filename"])[-1]]
    author = sanitize_folder_name(item.get("author_name", "")) or "unknown_author"
    title = sanitize_folder_name(item.get("title", "")) or f"book_{item.get('bookId')}"
    # Never reuse a path from another operation: delayed history from an old
    # command cannot be mistaken for proof of this release after a restart.
    relative = os.path.join("rsoul_audiobooks" if media == "audiobook" else "rsoul_ebooks", author,
                            title + " (" + operation_id + ")")
    local_root = os.path.abspath(local_root)
    stage = os.path.join(local_root, relative)
    source = os.path.abspath(os.path.join(local_root, item.get("dir", "")))
    server = server_root.rstrip("/\\") + "/" + relative.replace(os.sep, "/")
    intended = _identity(item.get("intended_identity") or (item.get("_task_extra") or {}).get("intended_identity"), item)
    pending = {
        "schema": SCHEMA, "operation_id": operation_id, "phase": "validating",
        "folder": relative, "local_dir": stage, "readarr_path": server,
        "source_dir": source, "download_root": local_root, "server_root": server_root,
        "files": [], "manifest": [], "command_id": None, "attempts": [], "submits": 0,
        "since": time.time(), "method": method, "book_id": item.get("bookId"),
        "media_type": media, "edition_id": None, "before_ids": None,
        "intended_identity": intended, "source_task": _source_task(item, operation_id),
    }
    if not names or len(set(names)) != len(names) or any(
            not isinstance(n, str) or not n or n in (".", "..") or re.search(r"[\\/]", n) for n in names):
        pending.update(phase="needs_review", reason="Expected file names are missing or ambiguous")
        return pending
    if not _inside(local_root, source) or not _inside(local_root, stage):
        pending.update(phase="needs_review", reason="Source or staging path escapes the download mount")
        return pending
    extra = item.get("_task_extra") or {}
    original_manifest = extra.get("manifest")
    expected_sizes = {}
    if original_manifest is not None:
        digest = hashlib.sha256(json.dumps(original_manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if extra.get("manifest_digest") and digest != extra["manifest_digest"]:
            pending.update(phase="needs_review", reason="The saved download manifest digest changed")
            return pending
        if (not isinstance(original_manifest, list) or not original_manifest
                or any(not isinstance(f, dict) or not isinstance(f.get("name"), str) for f in original_manifest)):
            pending.update(phase="needs_review", reason="The saved download manifest is invalid")
            return pending
        expected_sizes = {f["name"]: f.get("size") for f in original_manifest}
        if len(expected_sizes) != len(original_manifest) or set(expected_sizes) != set(names):
            pending.update(phase="needs_review", reason="Import names no longer match the complete download manifest")
            return pending
        if any(not isinstance(size, int) or isinstance(size, bool) or size <= 0 for size in expected_sizes.values()):
            pending.update(phase="needs_review", reason="The saved download manifest lacks trustworthy expected byte sizes")
            return pending
        pending["download_manifest_digest"] = digest
    else:
        # Legacy tasks may have expected sizes in their owned transfer records.
        for record in item.get("files") or extra.get("files") or []:
            if not isinstance(record, dict):
                continue
            name = re.split(r"[\\/]", record.get("filename", ""))[-1]
            size = record.get("size")
            if name in names and isinstance(size, int) and not isinstance(size, bool) and size > 0:
                expected_sizes[name] = size
    for name in names:
        source_path, stage_path = os.path.join(source, name), os.path.join(stage, name)
        entry = {"name": name, "source_path": source_path, "stage_path": stage_path,
                 "readarr_path": server + "/" + name, "size": None, "sha256": None}
        pending["manifest"].append(entry)
        pending["files"].append(stage_path)
        try:
            if not _inside(local_root, source_path) or os.path.islink(source_path) or not os.path.isfile(source_path):
                raise ValueError("not a regular file on the configured download mount")
            entry["size"] = os.path.getsize(source_path)
            if entry["size"] <= 0:
                raise ValueError("empty file")
            if name in expected_sizes:
                entry["expected_download_size"] = expected_sizes[name]
                if entry["size"] != expected_sizes[name]:
                    raise ValueError(f"actual size {entry['size']} differs from expected download size {expected_sizes[name]}")
            entry["sha256"] = import_identity.file_digest(source_path)
        except (OSError, ValueError) as exc:
            pending.update(phase="needs_review", reason=f"Cannot inventory {name}: {exc}")
    return pending


def _same_file(path, entry, root):
    if not _inside(root, path) or os.path.islink(path) or not os.path.isfile(path):
        return False
    try:
        return (entry.get("size") is not None and os.path.getsize(path) == entry["size"]
                and bool(entry.get("sha256")) and import_identity.file_digest(path) == entry["sha256"])
    except OSError:
        return False


def recover_staging(pending, persist=None):
    """Replay a manifest after a crash between moves. Never overwrite or roll back."""
    if _pause_operator_discard(pending):
        return pending
    if pending.get("phase") != "staging":
        return pending
    if not pending.get("inputs_validated"):
        return _retain(pending, "needs_review", "Input validation was not checkpointed before staging", persist)
    root = pending.get("download_root", "")
    if not root or not os.path.isdir(root) or not _inside(root, pending.get("local_dir", "")):
        return _retain(pending, "needs_review", "Download mount or staging path is unavailable", persist)
    if not pending.get("manifest"):
        return _retain(pending, "needs_review", "Staging intent has no complete manifest", persist)
    _checkpoint(pending, persist)  # before mkdir or the first move, including after restart
    os.makedirs(pending["local_dir"], exist_ok=True)
    for entry in pending["manifest"]:
        src, dst = entry.get("source_path", ""), entry.get("stage_path", "")
        if os.path.lexists(dst):
            if not _same_file(dst, entry, root):
                return _retain(pending, "needs_review", f"Staging collision or changed file: {entry.get('name')}", persist)
            if os.path.lexists(src) and os.path.abspath(src) != os.path.abspath(dst):
                return _retain(pending, "needs_review", "Both source and staged copies exist; review the interrupted move", persist)
            continue
        if not _same_file(src, entry, root):
            return _retain(pending, "needs_review", f"Manifest input missing or changed: {entry.get('name')}", persist)
        _before_mutation(persist)
        try:
            # An atomic same-filesystem rename avoids partial copies across a crash.
            # Different devices are retained for manual handling, never copy+unlink.
            os.rename(src, dst)
            fsync_directory(os.path.dirname(src))
            fsync_directory(os.path.dirname(dst))
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.EXDEV:
                # Retrying can't help: the download and staging folders are on different
                # filesystems, and R:soul never copies and deletes in place of an atomic move.
                return _retain(pending, "needs_review", "Staging needs a move across filesystems (EXDEV): keep the download folder and "
                               "its staging folder on the same mount; files and manifest are retained for operator recovery. "
                               "Correct the mounts, then inspect with python -m rsoul.recover before --apply; review does not resume automatically", persist)
            return _retain(pending, "staging", f"Staging interrupted; saved manifest will resume: {exc}", persist)
    pending.update(phase="staged", reason="All manifest inputs are staged")
    _checkpoint(pending, persist)
    return pending


def prepare_command(client, pending, book):
    """Read-only preparation; no baseline reset and no POST hidden in this function."""
    if pending["method"] == "scan":
        return {"name": "DownloadedBooksScan", "path": pending["readarr_path"]}, None, ""
    if pending["method"] != "targeted":
        return None, None, "Unsupported import method; no scan fallback is allowed"
    narrators = [record["metadata"].get("narrator") for record in pending.get("audio_validation") or []
                 if isinstance(record, dict) and isinstance(record.get("metadata"), dict)]
    edition = import_identity.select_edition(book, client.get_edition(pending["book_id"]), pending["media_type"], narrators)
    if not edition:
        return None, None, "No safe edition can be selected for the intended record"
    if edition.get("bookId") not in (None, 0, "0", "") and str(edition["bookId"]) != str(pending["book_id"]):
        return None, None, "Selected edition belongs to a different book"
    if pending.get("edition_id") is not None and str(pending["edition_id"]) != str(edition["id"]):
        return None, None, "The pinned import edition changed; manual review is required"
    if pending.get("foreign_edition_id") not in (None, "") and pending["foreign_edition_id"] != edition.get("foreignEditionId"):
        return None, None, "The pinned provider edition identity changed; manual review is required"
    pending["foreign_edition_id"] = edition.get("foreignEditionId")
    preview = client._get("manualimport", client.ver_uri, params={
        "folder": pending["readarr_path"], "filterExistingFiles": "false"})
    if not isinstance(preview, list):
        return None, None, "Unsupported manual-import preview response"
    wanted = {import_identity.path_key(f["readarr_path"]): f for f in pending["manifest"]}
    found = {}
    for row in preview:
        if not isinstance(row, dict):
            continue
        key = import_identity.path_key(row.get("path"))
        if key not in wanted:
            continue
        if key in found:
            return None, None, "Manual-import preview contains duplicate paths"
        if not isinstance(row.get("quality"), dict) or not row["quality"]:
            return None, None, "Manual-import preview cannot identify every input's quality"
        found[key] = {
            "path": row["path"], "authorId": book["authorId"], "bookId": pending["book_id"],
            "editionId": edition["id"], "foreignEditionId": edition.get("foreignEditionId"),
            "quality": row["quality"], "releaseGroup": row.get("releaseGroup"),
            "indexerFlags": row.get("indexerFlags") or 0, "disableReleaseSwitching": True,
        }
    if set(found) != set(wanted):
        return None, None, f"Manual-import preview lists {len(found)} of {len(wanted)} exact staged inputs"
    return {"name": "ManualImport", "files": [found[p] for p in wanted],
            "importMode": "move", "replaceExistingFiles": False}, edition["id"], ""


def submit_prepared(client, pending, config=None, persist=None):
    """At most one POST per durable attempt; ambiguous responses are never retried."""
    if _pause_operator_discard(pending):
        return pending
    if _disabled(config):
        return _retain(pending, pending.get("phase", "staged"), "Readarr sync is disabled; import is paused", persist)
    if pending.get("attempts") or pending.get("command_id") is not None or pending.get("submits", 0):
        return _retain(pending, "submission_unknown", "A submission already exists; reconcile before any retry", persist)
    book, intended, error = inspect_identity(client, pending)
    if error:
        transient = error.startswith(("Cannot verify", "The intended book or author cannot", "The intended author's name cannot"))
        return _retain(pending, "staged" if transient else "needs_review", error, persist)
    pending["intended_identity"] = intended
    if not all(_same_file(f["stage_path"], f, pending["download_root"]) for f in pending["manifest"]):
        return _retain(pending, "needs_review", "One or more staged inputs changed before submission", persist)
    try:
        payload, edition_id, error = prepare_command(client, pending, book)
        if error:
            return _retain(pending, "staged", error, persist)
        if pending.get("before_ids") is None:
            before = import_identity.bookfile_ids_before(client, pending["book_id"])
            if before is None:
                return _retain(pending, "staged", "Cannot record the pre-import file baseline", persist)
            pending["before_ids"] = before
        pending["edition_id"] = edition_id
    except StatePersistenceError:
        raise
    except Exception as exc:
        return _retain(pending, "staged", f"Import preparation failed: {exc}", persist)
    attempt = {"attempt_id": uuid.uuid4().hex, "started": time.time(), "state": "submission_unknown",
               "before_ids": copy.deepcopy(pending["before_ids"]), "edition_id": edition_id,
               "payload": payload, "command_id": None}
    pending["attempts"].append(attempt)
    pending.update(phase="submission_unknown", submits=len(pending["attempts"]), reason="Import POST intent saved")
    _checkpoint(pending, persist)  # Includes immutable transaction baseline and exact request.
    _before_mutation(persist)
    try:
        command = client.post_command(**payload)
    except Exception as exc:
        attempt["error"] = str(exc)
        return _retain(pending, "submission_unknown", f"Import response unknown: {exc}; no automatic resubmission", persist)
    receipt_id = command.get("id") if isinstance(command, dict) else None
    if (isinstance(receipt_id, bool) or not isinstance(receipt_id, (int, str))
            or not str(receipt_id).isascii() or not str(receipt_id).isdigit() or not str(receipt_id).strip("0")):
        return _retain(pending, "submission_unknown", "Server returned no valid command receipt; no automatic resubmission", persist)
    attempt.update(state="accepted", command_id=command["id"], received=time.time(), receipt=copy.deepcopy(command))
    pending.update(phase="importing", command_id=command["id"], reason="Import command accepted")
    _checkpoint(pending, persist)
    return pending


def _migrate_legacy(pending):
    """Retain old records without inventing a pre-submit baseline or a receipt."""
    value = copy.deepcopy(pending)
    value.update(schema=SCHEMA, legacy=True, operation_id=value.get("operation_id") or uuid.uuid4().hex,
                 attempts=value.get("attempts") or [])
    value.setdefault("submits", 0)
    value.setdefault("since", time.time())
    value.setdefault("book_id", (value.get("intended_identity") or {}).get("book_id"))
    if not value.get("media_type"):
        value["media_type"] = (value.get("intended_identity") or {}).get("media_type", "ebook")
    value.setdefault("manifest", [])
    if not value["manifest"]:
        for path in value.get("files") or []:
            name = os.path.basename(path)
            server = value.get("readarr_path", "").rstrip("/\\") + "/" + name
            entry = {"name": name, "stage_path": path, "source_path": path,
                     "readarr_path": server, "size": None, "sha256": None}
            if os.path.isfile(path) and not os.path.islink(path):
                try:
                    entry.update(size=os.path.getsize(path), sha256=import_identity.file_digest(path))
                except OSError:
                    pass
            value["manifest"].append(entry)
    value["phase"] = "importing" if value.get("command_id") is not None else "submission_unknown"
    value["reason"] = "Legacy pending import: reconcile existing evidence; never reconstruct the baseline after submission"
    return value


def _source_item(value):
    saved = value.get("source_task") or {}
    extra = saved.get("extra") or {}
    return dict(extra, title=saved.get("book_title", ""), author_name=saved.get("author_name", ""),
                bookId=saved.get("book_id"), dir=saved.get("local_dir", ""), filename=saved.get("filename", ""),
                backend_name=saved.get("backend_name", "slskd"), seriesTitle=saved.get("series_title", ""),
                full_dir=extra.get("full_dir") or extra.get("file_dir", ""))


def _revalidate_owned(client, value, config):
    """Run validate_inputs on the files a transaction owns, at their current place.

    Each file is either at its source or already staged; an interrupted staging run leaves some
    in each, and that is a normal state the staging step finishes later. All files are checked
    together, at their own paths, as one complete set."""
    root = value.get("download_root", "")
    shadow = copy.deepcopy(value)
    manifest = shadow.get("manifest") or []
    if not manifest or not root:
        return "The transaction lists no files inside the download mount"
    for entry in manifest:
        here = next((p for p in (entry.get("stage_path"), entry.get("source_path")) if p and os.path.isfile(p) and not os.path.islink(p)), None)
        if not here:
            return f"{entry.get('name')} is neither staged nor at its source"
        if not _inside(root, here):
            return f"{entry.get('name')} is outside the download mount"
        entry["source_path"] = here
    item = _source_item(value)
    source_dir = value.get("source_dir") or os.path.dirname(manifest[0]["source_path"])
    item["dir"] = os.path.relpath(source_dir, root) if _inside(root, source_dir) else ""
    item["expected_files"] = [e["name"] for e in manifest]
    item["local_paths"] = [e["source_path"] for e in manifest]
    item["filename"] = item["expected_files"][0] if item["expected_files"] else ""
    error = validate_inputs(SimpleNamespace(config=config, readarr=client), item, shadow)
    for key in ("audio_validation", "recording_policy"):
        if key in shadow:
            value[key] = shadow[key]
    return error


def check_pending(client, pending, now=None, config=None, persist=None):
    """Read-only reconciliation first; retained dicts never imply success or peer fault."""
    now = time.time() if now is None else now
    value = copy.deepcopy(pending)
    if _pause_operator_discard(value):
        return value
    if value.get("schema") != SCHEMA:
        value = _migrate_legacy(value)
    # Without a writer callers may inspect, but never move or submit anything.
    writable = persist is not None or bool(value.get("journal_path"))
    def keep(phase, reason, **fields):
        previous = value.get("phase")
        value.update(phase=phase, reason=reason, last_checked=now, **fields)
        if writable:
            previous = _attention_previous(value, previous)
            _checkpoint(value, persist)
            notify.phase_entered(value, previous, reason)
        return value

    if value.get("phase") == "validating" and not value.get("attempts"):
        if config is None or _disabled(config) or not writable:
            return keep("validating", "Input validation is paused; no filesystem mutation permitted")
        item = _source_item(value)
        try:
            error = validate_inputs(SimpleNamespace(config=config, readarr=client), item, value)
        except StatePersistenceError:
            raise
        except CatalogUnavailable as exc:
            return keep("validating", f"Input metadata validation is deferred: {exc}")
        except Exception as exc:
            error = f"Input validation is unavailable: {exc}"
        if error:
            return keep("needs_review", error)
        value.update(inputs_validated=True, phase="staging", validation_policy=VALIDATION_POLICY)
        _checkpoint(value, persist)

    if (value.get("phase") in ("staging", "staged") and not value.get("attempts") and value.get("command_id") is None
            and not value.get("legacy") and int(value.get("validation_policy") or 1) < VALIDATION_POLICY):
        # Validated under older rules and never submitted: validate the files it owns now, wherever
        # they are (staged or still at the source), before anything is moved further or submitted.
        if config is None or _disabled(config) or not writable:
            return keep(value["phase"], "Validation under the current rules is paused; nothing is submitted")
        try:
            error = _revalidate_owned(client, value, config)
        except StatePersistenceError:
            raise
        except CatalogUnavailable as exc:
            return keep(value["phase"], f"Validation under the current rules is deferred: {exc}")
        except Exception as exc:
            error = f"Validation under the current rules is unavailable: {exc}"
        if error:
            return keep("needs_review", "Validation under the current rules: " + error)
        value["validation_policy"] = VALIDATION_POLICY
        _checkpoint(value, persist)

    if value.get("phase") == "staging" and not value.get("attempts"):
        if config is None or _disabled(config) or not writable:
            return keep("staging", "Import is paused; no filesystem mutation permitted")
        recover_staging(value, persist)
        if value["phase"] != "staged":
            return value
    if value.get("phase") == "staged" and not value.get("attempts") and not value.get("legacy"):
        if config is None or _disabled(config) or not writable:
            return keep("staged", "Import is paused; no submission permitted")
        value = submit_prepared(client, value, config, persist)
        if value.get("phase") in ("needs_review", "staged"):
            return value

    book, identity, identity_error = inspect_identity(client, value)
    if identity_error:
        return keep("needs_review", identity_error)
    value["intended_identity"] = identity
    proof = import_identity.verify_import_evidence(client, value)
    value["verification"] = proof
    command = None
    if value.get("command_id") is not None:
        try:
            command = client.get_command(value["command_id"])
        except Exception as exc:
            value["command_error"] = str(exc)
        if isinstance(command, dict):
            if str(command.get("id", value["command_id"])) != str(value["command_id"]):
                return keep("needs_review", "Command API returned a different receipt ID")
            value["last_command"] = copy.deepcopy(command)
            if str(command.get("status") or "").casefold() not in TERMINAL:
                return keep("importing", "Import command remains active; files are retained", overdue=now - value.get("since", now) > MAX_AGE)

    if proof["status"] == "verified":
        value.update(phase="verified", reason=proof["reason"], completed_at=now,
                     retained_inputs=[p for p in value.get("files", []) if os.path.exists(p)])
        if writable:
            _checkpoint(value, persist)
        logger.info("Verified complete import for book %s (%d inputs, %d outputs)",
                    value.get("book_id"), len(value["manifest"]), len(proof["output_ids"]))
        return True
    if proof["status"] == "partial":
        return keep("partial", proof["reason"] + "; keep remaining files and review the import")
    if proof["status"] == "mismatch":
        return keep("needs_review", proof["reason"])
    if isinstance(command, dict):
        return keep("needs_review", f"Command ended with {command.get('status')}/{command.get('result')}, but complete import is unverified: {proof['reason']}")
    if value.get("phase") == "needs_review":
        return keep("needs_review", value.get("reason") or proof["reason"])
    reason = "Submission outcome is unknown; complete output proof or manual reconciliation is required before retry"
    if now - value.get("since", now) > MAX_AGE:
        return keep("needs_review", reason + "; review age exceeded, all local files retained")
    return keep("submission_unknown", reason)


# Default for [Search Settings] audiobook_min_duration_minutes
DEFAULT_MIN_AUDIOBOOK_MINUTES = 15


def _too_short(ctx, records):
    """[Search Settings] audiobook_min_duration_minutes (default 15; 0 = off): a download whose
    files play for less than this in total is not an audiobook (a song, a sample), whatever its
    name says. Durations that can't be read are not evidence either way."""
    minimum = ctx.config.getfloat("Search Settings", "audiobook_min_duration_minutes", fallback=DEFAULT_MIN_AUDIOBOOK_MINUTES)
    seconds = [r.get("duration_seconds") for r in records]
    if not minimum or minimum <= 0 or not seconds or any(not isinstance(s, (int, float)) or s <= 0 for s in seconds):
        return ""
    total = float(sum(seconds))
    if total < minimum * 60:
        return (f"The files play for {total / 60:.1f} minutes in total, less than audiobook_min_duration_minutes "
                f"({minimum:g}): not an audiobook")
    return ""


def _runtime_drift(ctx, pending, records):
    """[Search Settings] audiobook_duration_tolerance (off by default): set the files aside when
    their total running time is further from Chaptarr's length for the book than the tolerance.
    A catalog that can't be read defers validation like the other catalog checks."""
    tolerance = ctx.config.getfloat("Search Settings", "audiobook_duration_tolerance", fallback=0)
    if not tolerance or tolerance <= 0:
        return ""
    seconds = [r.get("duration_seconds") for r in records]
    if not seconds or any(not isinstance(s, (int, float)) or s <= 0 for s in seconds):
        return ""  # a length that can't be read is not evidence either way
    try:
        book = ctx.readarr.get_book(pending["book_id"])
    except Exception as exc:
        raise CatalogUnavailable(f"Cannot read the book's length for the running-time check: {exc}") from exc
    try:
        minutes = float((book or {}).get("durationMinutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        return ""
    total, expected = float(sum(seconds)), minutes * 60
    drift = abs(total - expected) / expected
    if drift > tolerance:
        return (f"Running time {total / 3600:.2f} h differs from Chaptarr's {expected / 3600:.2f} h by {drift:.0%} "
                f"(audiobook_duration_tolerance {tolerance:.0%})")
    return ""


# Version of the rules validate_inputs applies. Work validated under an older version and not
# yet submitted is validated again before submission (see check_pending).
VALIDATION_POLICY = 2


def _catalog_narrators(ctx, item):
    from .match import book_narrators
    readarr = getattr(ctx, "readarr", None)
    if readarr is None:
        raise CatalogUnavailable("No catalog connection to read the book's narrators")
    try:
        book = readarr.get_book(item.get("bookId"))
    except Exception as exc:
        raise CatalogUnavailable(f"Cannot read the book's narrators: {exc}") from exc
    return book_narrators(book if isinstance(book, dict) else {})


def _evidence_problem(ctx, item, source, names, title):
    """The search-time evidence decision again, on the peer's folder, with the same inputs
    (the book's title and its narrators from the catalog). "" when the files may be imported."""
    from .match import AMBIGUOUS, CONFLICT, FULL, audiobook_evidence, author_mentions_detail, author_verdict, music_library
    author = item.get("author_name", "")
    ext = os.path.splitext(names[0])[1].lstrip(".") if names else ""
    path = source + "\\" + " ".join(names[:3])
    if author_mentions_detail(author, path, title)[AMBIGUOUS] and author_verdict(author, path, title) != FULL:
        # Search rejects these; a download queued by an earlier build is held the same way
        return f"The file names ({', '.join(names[:3])}) may name another person with the author's surname"
    if audiobook_evidence(author, path, ext, (), title, directory=source):
        return ""
    narrators = _catalog_narrators(ctx, item)  # only needed when nothing else shows it
    if audiobook_evidence(author, path, ext, narrators, title, directory=source):
        return ""
    if author_verdict(author, path, title, narrators) == CONFLICT:
        return f"The source folder ({source}) names another person with the author's surname"
    if music_library(source):
        return f"The files come from a music library folder ({source}), not this author's audiobook"
    return f"Nothing in the source folder ({source}) marks it as {author}'s audiobook"


def validate_inputs(ctx, item, pending):
    paths = [f["source_path"] for f in pending["manifest"]]
    if pending["media_type"] == "audiobook":
        # The same fragment rule as search selection, on the complete selected set: a book whose
        # every file is a sample/prologue/excerpt, or whose source folder is labelled so, is not
        # the book. A complete book with a prologue chapter among its files passes.
        from .match import fragment_release
        title = item.get("title", "")
        folder = (item.get("full_dir") or "").replace("\\", "/").rstrip("/").split("/")[-1]
        names = [f["name"] for f in pending["manifest"]]
        if (folder and fragment_release(title, folder)) or (names and all(fragment_release(title, n) for n in names)):
            return "The selected files are a fragment (sample, prologue or excerpt), not the complete book"
        # The search-time evidence rules again, on the peer's folder: a download queued before
        # they existed (or resumed from saved state) gets the same scrutiny before import
        source = item.get("full_dir") or ""
        if source:
            reason = _evidence_problem(ctx, item, source, names, title)
            if reason:
                return reason
        valid, detail, records = audiotags.validate_audio_files(paths)
        pending["audio_validation"] = records
        if not valid:
            return "Invalid or unreadable audio: " + detail
        short = _too_short(ctx, records)
        if short:
            return short
        drift = _runtime_drift(ctx, pending, records)
        if drift:
            return drift
        source_texts = [item.get("full_dir", ""), item.get("dir", ""), item.get("filename", "")]
        policy = validate_recording_files(ctx.config, paths, source_texts, records=records)
        pending["recording_policy"] = {"allowed": policy.allowed, "reasons": list(policy.reasons),
                                       "warnings": list(policy.warnings), "unknown": list(policy.unknown)}
        for warning in policy.warnings:
            logger.warning("Recording policy: %s", warning)
        if not policy.allowed:
            return "Recording policy rejected these inputs: " + "; ".join(policy.reasons)
        from .postprocess import audiobook_tags_contradict
        wrong_book = audiobook_tags_contradict(ctx, dict(item, local_paths=paths), pending["download_root"])
        if wrong_book:
            return "Audio tags identify another work: " + wrong_book
    else:
        from .postprocess import validate_metadata
        if item.get("backend_name") != "stacks":
            for path in paths:
                if not validate_metadata(path, item.get("title", ""), item.get("bookId"), ctx,
                                         item.get("seriesTitle", ""), item.get("author_name", "")):
                    return "Ebook metadata could not be validated; files retained for review"
    return ""


def process_one(ctx, item, local_root, server_root, method, poll=True):
    """One transaction per wanted book. poll=False returns right after the submission, so
    the caller can follow several submitted imports together (see process_imports)."""
    saved = (item.get("_task_extra") or {}).get("import_pending")
    state = getattr(ctx, "state", None)
    if state is not None and item.get("task_id"):
        for record in state.get_tasks_for_orchestrator():
            if record.get("task_id") == item["task_id"]:
                saved = (record.get("extra") or {}).get("import_pending") or saved
                break
    if saved:
        pending = copy.deepcopy(saved)
        persist = context_writer(ctx, item, pending)
        return check_pending(ctx.readarr, pending, config=ctx.config, persist=persist)
    if not local_root or not server_root:
        raise StatePersistenceError("Import download mappings are unavailable; files and recovery state were retained")
    pending = new_transaction(item, local_root, server_root, method)
    persist = context_writer(ctx, item, pending)
    _checkpoint(pending, persist)
    if pending["phase"] == "needs_review":
        notify.phase_entered(pending, None, pending.get("reason", ""))
        return pending
    if _disabled(ctx.config):
        return _retain(pending, "validating", "Readarr sync is disabled; downloaded files retained in place", persist)
    try:
        error = validate_inputs(ctx, item, pending)
    except StatePersistenceError:
        raise
    except CatalogUnavailable as exc:
        return _retain(pending, "validating", f"Input metadata validation is deferred: {exc}", persist)
    except Exception as exc:
        error = f"Input validation is unavailable: {exc}"
    if error:
        return _retain(pending, "needs_review", error, persist)
    pending.update(inputs_validated=True, phase="staging", validation_policy=VALIDATION_POLICY)
    _checkpoint(pending, persist)
    recover_staging(pending, persist)
    if pending["phase"] != "staged":
        return pending
    submit_prepared(ctx.readarr, pending, ctx.config, persist)
    if pending["phase"] not in ("importing", "submission_unknown"):
        return pending
    if not poll:
        return pending
    timeout = ctx.config.getfloat("Readarr", "import_poll_timeout", fallback=600)
    deadline = time.monotonic() + max(0, timeout)
    while True:
        outcome = check_pending(ctx.readarr, pending, config=ctx.config, persist=persist)
        if outcome is True:
            return True
        pending = outcome
        if pending.get("phase") != "importing" or time.monotonic() >= deadline:
            return pending
        health.heartbeat()
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def _roots(ctx):
    roots = {ctx.config.get("Slskd", "download_dir", fallback="")}
    orchestrator = getattr(ctx, "orchestrator", None)
    if orchestrator:
        backends = getattr(orchestrator, "backends", {})
        for backend in backends.values() if isinstance(backends, dict) else backends or []:
            roots.add(getattr(backend, "download_dir", ""))
    return [os.path.abspath(r) for r in roots if isinstance(r, str) and r]


def recover_pending_imports(ctx):
    """Adopt standalone durable sidecars into central state exactly once, no imports."""
    state = getattr(ctx, "state", None)
    if state is None:
        return 0
    recovered = 0
    existing = {i.get("task_id"): i for i in state.get_tasks_for_orchestrator()}
    for root in _roots(ctx):
        directory = Path(root) / JOURNAL_DIR
        if not directory.is_dir() or not _inside(root, str(directory)):
            continue
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink():
                continue
            try:
                pending = json.loads(path.read_text())
                if not isinstance(pending, dict) or pending.get("schema") != SCHEMA:
                    continue
                if pending.get("phase") == "verified" or pending.get("migrated_to_task_id"):
                    continue
                source = pending.get("source_task")
                if not isinstance(source, dict) or not source.get("task_id"):
                    logger.error("Import journal %s lacks recovery task identity; retained for review", path)
                    continue
                if os.path.realpath(pending.get("download_root", "")) != os.path.realpath(root):
                    logger.error("Import journal %s has a different download root; retained for review", path)
                    continue
                prior = existing.get(source["task_id"])
                if not prior or not (prior.get("extra") or {}).get("import_pending"):
                    source = copy.deepcopy(source)
                    source["extra"] = dict(source.get("extra") or {}, import_pending=copy.deepcopy(pending))
                    state.update_task(SimpleNamespace(**source))
                    existing[source["task_id"]] = source
                    recovered += 1
                pending["migrated_to_task_id"] = source["task_id"]
                atomic_write_json(path, pending)
            except StatePersistenceError:
                raise
            except Exception as exc:
                logger.error("Cannot recover import journal %s: %s; retained for review", path, exc)
    return recovered


def preserve_unresumable_task(ctx, item, reason):
    """Checkpoint old orphan work for review instead of discarding a moved download."""
    extra = copy.deepcopy(item.get("extra") or {})
    pending = extra.get("import_pending")
    if pending:
        return pending
    roots = _roots(ctx)
    candidates = []
    author = sanitize_folder_name(item.get("author_name", "")) or "unknown_author"
    title = sanitize_folder_name(item.get("book_title", "")) or f"book_{item.get('book_id')}"
    for root in roots:
        source = os.path.abspath(os.path.join(root, item.get("local_dir", "")))
        if _inside(root, source) and os.path.isdir(source):
            candidates.append(source)
        for bucket in ("rsoul_audiobooks", "rsoul_ebooks"):
            parent = Path(root) / bucket / author
            if parent.is_dir() and _inside(root, str(parent)):
                for directory in parent.iterdir():
                    if directory.is_dir() and not directory.is_symlink() and (
                            directory.name == title or re.fullmatch(re.escape(title) + r" \([0-9a-f]+\)", directory.name)):
                        candidates.append(str(directory))
    pending = {"schema": SCHEMA, "operation_id": uuid.uuid4().hex, "legacy": True,
               "phase": "needs_review", "reason": reason + "; old staged work may require manual reconciliation",
               "local_dir": candidates[0] if candidates else "", "readarr_path": "", "files": [],
               "manifest": [], "before_ids": None, "command_id": None, "attempts": [], "submits": 0,
               "since": time.time(), "book_id": item.get("book_id"), "media_type": extra.get("media_type", "ebook"),
               "intended_identity": _identity(extra.get("intended_identity"), dict(item, media_type=extra.get("media_type", "ebook"))),
               "source_task": copy.deepcopy(item), "candidate_directories": sorted(set(candidates))}
    extra["import_pending"] = pending
    ctx.state.set_extra(item["task_id"], extra)
    return pending
