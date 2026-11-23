"""Operator-decided recovery of imports that an earlier version left without an outcome.

v10.2 to v10.4 saved a targeted import that was not confirmed with no command ID and a
submit count of 0. That covers two very different cases: the import could not be prepared
(no edition, nothing was sent), or the POST was sent and only its response was lost. The
saved record cannot tell them apart, so v11 never resubmits such an import on its own.

This command lets the operator decide, book by book, after looking at the evidence:

    python -m rsoul.recover --config-dir /data                  # list, change nothing
    python -m rsoul.recover --config-dir /data --book 6744      # evidence for one book
    python -m rsoul.recover --config-dir /data --book 6744 --apply
    python -m rsoul.recover --config-dir /data --book 6744 --discard

--discard is for an import that is not worth recovering (wrong recording or broken files).
It works on a saved import that is set aside for review, has an
unknown outcome, is partial, or is waiting before submission, and it needs the same clear
evidence as --apply: no Chaptarr command for the files that is queued, running, unclear or
possibly successful, no import of the book since staging, readable history and book files.
It establishes current file ownership and checks source and staging paths under both saved
and current server mappings. It refuses reused source paths and files claimed by other tasks.
It then archives the saved record (recover_archive.jsonl in the data folder), checkpoints the
exact quarantine destinations, moves its files to failed_imports, and removes the task.
Interrupted discards remain reserved for another --discard invocation; they do not resume
normal importing. The book stays wanted in Chaptarr and is searched again like any other.

Listing and inspecting read a snapshot of the saved state and may run while R:soul runs
(the snapshot can then be a few minutes old). --apply and --discard need R:soul stopped: each takes the
application lock and refuses if it can't.

--apply refuses, and changes nothing, unless every piece of evidence was read and is clear:
- Chaptarr's command list was read, and no command naming the staged files (compared on
  their decoded, normalised paths) is queued, running, of unknown status, or ended successfully;
- the book's history was read, and no import event for it is dated after staging (an event
  whose date can't be read counts against recovery);
- the book's files can be read;
- the saved inventories agree: the files the pending import lists, the expected files, and
  the original download manifest or transfer records name the same files, with no duplicates
  and no conflicting sizes;
- v11's own transaction constructor accepts the files: every one present, a regular file on
  the download mount, of the size the original download had where that is known.

Then it records the decision (with the evidence and the old record) and creates a fresh v11
transaction at the validating phase, with server paths derived from the *current* mapping.
The next normal run does what it does for any new download: audio, fragment, recording-policy
and tag validation, staging into a fresh folder, a baseline, the import and verification.
Nothing is imported by this command itself.
"""

import argparse
import configparser
import copy
import ntpath
import os
import re
import stat
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

from . import import_identity
from .import_transaction import _identity, _inside, new_transaction

ACTIVE = {"queued", "started"}
FINISHED = {"completed", "failed", "aborted", "cancelled", "orphaned"}
IMPORT_COMMANDS = {"manualimport", "downloadedbooksscan"}
IMPORT_KINDS = {"bookfileimported", "bookfileconverted", "downloadfolderimported"}


class Unclear(Exception):
    """Evidence that could not be established; recovery must not proceed."""


def _kind(event_type: Any) -> str:
    if isinstance(event_type, int) and not isinstance(event_type, bool):
        return {3: "bookfileimported", 11: "bookfileconverted"}.get(event_type, "")
    return "".join(c for c in str(event_type or "").casefold() if c.isalpha())


def _name(path: Any) -> str:
    return str(path or "").replace("\\", "/").rstrip("/").split("/")[-1]


def candidates(state_items: List[dict]) -> List[dict]:
    """Saved tasks whose import has no outcome and no command: what this tool is for."""
    found = []
    for item in state_items:
        extra = item.get("extra") or {}
        pending = extra.get("import_pending")
        if not isinstance(pending, dict) or pending.get("recovery"):
            continue
        if "operator_discard" in extra or pending.get("operator_discard"):
            continue
        legacy = pending.get("legacy") or not pending.get("schema")
        attempts = pending.get("attempts") or []
        commands = [pending.get("command_id")] + [a.get("command_id") for a in attempts if isinstance(a, dict)]
        if legacy and all(c is None for c in commands) and not any(isinstance(a, dict) and a.get("state") == "accepted" for a in attempts):
            found.append(item)
    return found


def inventory(item: dict) -> Dict[str, Any]:
    """Reconcile every saved list of the book's files. Returns names, the staged folder and
    the known original sizes, or 'problems' that must keep the record for review."""
    pending = item["extra"]["import_pending"]
    extra = item.get("extra") or {}
    problems: List[str] = []
    sources: Dict[str, List[str]] = {}
    manifest = pending.get("manifest") or []
    sources["pending import"] = [e.get("name") or _name(e.get("stage_path")) for e in manifest if isinstance(e, dict)] or [_name(p) for p in pending.get("files") or []]
    if extra.get("expected_files"):
        sources["expected files"] = [_name(n) for n in extra["expected_files"]]
    if extra.get("manifest"):
        sources["download manifest"] = [r.get("name") for r in extra["manifest"] if isinstance(r, dict)]
    if extra.get("files"):
        sources["transfer records"] = [_name(r.get("filename")) for r in extra["files"] if isinstance(r, dict) and r.get("filename")]
    for label, names in sources.items():
        if not names or any(not n for n in names):
            problems.append(f"the {label} list has empty names")
        elif len(set(names)) != len(names):
            problems.append(f"the {label} list names a file twice")
    reference = sources.get("pending import") or []
    for label, names in sources.items():
        if set(names) != set(reference):
            problems.append(f"the {label} list ({len(set(names))} file(s)) does not match the pending import ({len(set(reference))})")
    sizes: Dict[str, int] = {}
    for record in (extra.get("manifest") or []) + (extra.get("files") or []):
        if not isinstance(record, dict):
            continue
        name, size = record.get("name") or _name(record.get("filename")), record.get("size")
        if isinstance(size, int) and not isinstance(size, bool) and size > 0:
            if name in sizes and sizes[name] != size:
                problems.append(f"the saved records give two sizes for {name}")
            sizes.setdefault(name, size)
    stage_dirs = {os.path.dirname(e["stage_path"]) for e in manifest if isinstance(e, dict) and e.get("stage_path")} or \
                 {os.path.dirname(p) for p in pending.get("files") or []} or ({pending["local_dir"]} if pending.get("local_dir") else set())
    if len(stage_dirs) != 1:
        problems.append("the staged files are not in one folder")
    return {"names": sorted(set(reference)), "stage_dir": next(iter(stage_dirs)) if len(stage_dirs) == 1 else "",
            "sizes": sizes, "problems": problems, "sources": {k: len(v) for k, v in sources.items()}}


def _absolute(path: Any) -> bool:
    """A usable absolute server path: POSIX, a Windows drive, or a UNC share."""
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        return False
    return path.startswith("/") or bool(re.match(r"^[A-Za-z]:[\\/]", path)) or bool(re.match(r"^\\\\[^\\/]+[\\/][^\\/]+", path))


def _command_targets(command: dict, name: str) -> List[tuple]:
    """(path, recursive) for every target of an import command. Raises Unclear when the
    command's targets can't all be established: one readable entry does not excuse another."""
    ident = f"command {command.get('id')} ({command.get('name')})"
    body = command.get("body")
    if not isinstance(body, dict):
        raise Unclear(f"{ident} has no readable body")
    if name == "manualimport":
        files = body.get("files")
        if not isinstance(files, list) or not files:
            raise Unclear(f"{ident} has no readable file list")
        targets = []
        for entry in files:
            path = entry.get("path") if isinstance(entry, dict) else None
            if not _absolute(path):
                raise Unclear(f"{ident} lists a file without a usable absolute path ({path!r})")
            targets.append((path, False))
        return targets
    path = body.get("path")
    if not _absolute(path):
        raise Unclear(f"{ident} has no usable absolute path ({path!r})")
    return [(path, True)]  # a downloaded-books scan reads everything below its path


def _server_path_key(path: str) -> str:
    """Normalise server paths independently of this container's filesystem."""
    key = import_identity.path_key(path)
    if ntpath.splitdrive(path)[0]:
        key = key.casefold()  # Windows drive paths and UNC shares are case insensitive.
        if len(key) == 2 and key[1] == ":":
            key += "/"  # POSIX normpath otherwise drops a Windows drive root's slash.
    return key


def _overlaps(target: str, recursive: bool, files: List[str], folders: List[str]) -> bool:
    """Path-component comparison, never string prefixes: /downloads/book and
    /downloads/book-other are different folders."""
    key = _server_path_key(target)
    keys = {_server_path_key(path) for path in files if path}
    directories = {_server_path_key(path) for path in folders if path}

    def within(child, parent):
        return child == parent or child.startswith(parent.rstrip("/") + "/")

    if key in keys or any(within(key, folder) for folder in directories):
        return True
    # Source folders may contain other books. A recursive command overlaps their
    # exact files without treating every manual import in the shared folder as ours.
    return recursive and any(within(path, key) for path in keys | directories)


def evidence(client: Any, item: dict, server_paths: List[str], server_folders: List[str]) -> dict:
    """Read everything the operator needs to decide. 'blocking' lists what stops recovery."""
    pending = item["extra"]["import_pending"]
    book_id = pending.get("book_id") or item.get("book_id")
    since = float(pending.get("since") or 0)
    report: Dict[str, Any] = {"book_id": book_id, "title": item.get("book_title"), "since": since,
                              "commands": [], "history_imports": [], "blocking": [], "notes": []}
    try:
        commands = client.get_command()
        if not isinstance(commands, list):
            raise Unclear("Chaptarr's command list came back in an unsupported form")
        for command in commands:
            if not isinstance(command, dict):
                raise Unclear("Chaptarr's command list has an unreadable entry")
            if not isinstance(command.get("name"), str) or not command["name"].strip():
                raise Unclear(f"command {command.get('id')} has no readable name")
            name = "".join(c for c in command["name"].casefold() if c.isalpha())
            if not name:
                raise Unclear(f"command {command.get('id')} has no readable name")
            if name not in IMPORT_COMMANDS:
                continue
            targets = _command_targets(command, name)
            if not any(_overlaps(path, recursive, server_paths, server_folders) for path, recursive in targets):
                continue
            status = str(command.get("status") or "").casefold()
            result = str(command.get("result") or "").casefold()
            report["commands"].append({"id": command.get("id"), "name": command.get("name"), "status": status, "result": result})
            label = f"Command {command.get('id')} ({command.get('name')}) for these files"
            if status in ACTIVE:
                report["blocking"].append(f"{label} is still {status}")
            elif status == "completed":
                # "completed" is the command's lifecycle, not the import's outcome: only an
                # explicit unsuccessful result shows that nothing was imported
                if result != "unsuccessful":
                    report["blocking"].append(f"{label} completed with result '{result or 'none'}'; only 'unsuccessful' shows nothing was imported")
            elif status in FINISHED:
                if result not in ("", "unsuccessful"):
                    report["blocking"].append(f"{label} ended {status} with result '{result}'")
            else:
                report["blocking"].append(f"{label} has an unknown status '{status or 'none'}'")
        if not report["commands"]:
            report["notes"].append("No command in Chaptarr's command list names these files (the list only holds recent commands)")
    except Unclear as exc:
        report["blocking"].append(str(exc))
    except Exception as exc:
        report["blocking"].append(f"Chaptarr's command list cannot be read: {exc}")

    events, error = import_identity.history_rows(client, book_id, since)
    if error:
        report["blocking"].append(f"Chaptarr's history cannot be read completely: {error}")
    for event in events:
        if str(event.get("bookId")) != str(book_id) or _kind(event.get("eventType")) not in IMPORT_KINDS:
            continue
        stamp = import_identity._event_time(event.get("date"))
        if stamp is None:
            report["blocking"].append(f"An import event for this book has an unreadable date ({event.get('date')!r})")
            continue
        if stamp < since:
            continue  # each event's own date counts, not the page it was on
        data = {str(k).casefold(): v for k, v in event["data"].items()} if isinstance(event.get("data"), dict) else {}
        report["history_imports"].append({"date": event.get("date"), "dropped": data.get("droppedpath"), "imported": data.get("importedpath")})
    if report["history_imports"]:
        report["blocking"].append(f"{len(report['history_imports'])} import event(s) for this book since the files were staged")

    rows = import_identity.bookfile_rows(client, book_id)
    report["book_files_now"] = None if rows is None else len(rows)
    if rows is None:
        report["blocking"].append("The book's files cannot be read from Chaptarr")
    return report


def plan(item: dict, local_root: str, server_root: str) -> Dict[str, Any]:
    """Reconcile the inventory and build the fresh transaction through v11's own constructor.
    Returns {'inventory', 'old_server_paths', 'new', 'blocking'}; touches nothing on disk."""
    pending = item["extra"]["import_pending"]
    extra = copy.deepcopy(item.get("extra") or {})
    found = inventory(item)
    blocking = list(found["problems"])
    old_server = (pending.get("readarr_path") or "").rstrip("/\\")
    old_paths = [e.get("readarr_path") for e in pending.get("manifest") or [] if isinstance(e, dict) and e.get("readarr_path")] or \
                [f"{old_server}/{n}" for n in found["names"]] if old_server else []
    result: Dict[str, Any] = {"inventory": found, "old_server_paths": old_paths, "old_server_folder": old_server, "new": None, "blocking": blocking}
    stage_dir = found["stage_dir"]
    if not stage_dir or not _inside(local_root, stage_dir):
        blocking.append("The staged folder is not inside the configured download folder")
        return result
    if blocking:
        return result
    extra.pop("import_pending", None)
    extra["expected_files"] = found["names"]
    intended = _identity(pending.get("intended_identity") or extra.get("intended_identity"), {
        "bookId": pending.get("book_id") or item.get("book_id"), "title": item.get("book_title"),
        "author_name": item.get("author_name"), "media_type": pending.get("media_type") or extra.get("media_type", "ebook")})
    extra["intended_identity"] = intended
    work = {
        "task_id": item.get("task_id"), "bookId": intended["book_id"], "title": item.get("book_title") or intended.get("title"),
        "author_name": item.get("author_name") or intended.get("author_name"), "media_type": intended["media_type"],
        "dir": os.path.relpath(stage_dir, local_root), "filename": found["names"][0], "expected_files": found["names"],
        "backend_name": item.get("backend_name", "slskd"), "seriesTitle": item.get("series_title", ""),
        "_task_extra": extra,
    }
    if extra.get("full_dir"):
        work["full_dir"] = extra["full_dir"]  # the peer's folder name, read by the recording policy
    if extra.get("files"):
        work["files"] = extra["files"]
    fresh = new_transaction(work, local_root, server_root, "targeted")
    if fresh.get("phase") != "validating":
        blocking.append(fresh.get("reason") or "v11 does not accept these files as a new import")
        return result
    for entry in fresh["manifest"]:
        size = found["sizes"].get(entry["name"])
        if size is not None and entry.get("size") != size:
            blocking.append(f"{entry['name']} is {entry.get('size')} bytes, the original download was {size}")
    result["new"] = fresh
    return result


def _print(report: dict, planned: dict, out) -> None:
    found = planned["inventory"]
    out.write(f"\nBook {report['book_id']}: {report['title']}\n")
    out.write(f"  staged since: {time.strftime('%Y-%m-%d %H:%M', time.localtime(report['since'])) if report['since'] else 'unknown'}\n")
    out.write(f"  staged folder: {found['stage_dir'] or 'unclear'}\n")
    out.write(f"  files: {', '.join(found['names']) or 'none'}  (lists checked: {', '.join(f'{k} {v}' for k, v in found['sources'].items())})\n")
    for c in report.get("commands", []):
        out.write(f"  command {c['id']} {c['name']}: {c['status']}/{c['result']}\n")
    for e in report.get("history_imports", []):
        out.write(f"  history import {e['date']}: {e['dropped']} -> {e['imported']}\n")
    out.write(f"  files on the book now: {report.get('book_files_now')}\n")
    if planned.get("new"):
        new_folder = planned["new"]["readarr_path"]
        out.write(f"  would import from (current mapping): {new_folder}\n")
        if planned["old_server_folder"] and not import_identity.path_key(planned["old_server_folder"]).startswith(import_identity.path_key(planned["new"]["server_root"])):
            out.write(f"  note: the saved record used another server path ({planned['old_server_folder']}); the current mapping is used\n")
    for note in report["notes"]:
        out.write(f"  note: {note}\n")
    blocking = planned["blocking"] + report["blocking"]
    for block in blocking:
        out.write(f"  BLOCKS RECOVERY: {block}\n")
    out.write("  -> can be recovered with --apply\n" if not blocking else "  -> not recoverable now\n")


DISCARDABLE = {"needs_review", "submission_unknown", "partial", "validating", "staged"}


def _local_path(root: str, path: Any) -> str:
    if not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path:
        raise Unclear(f"An owned file has no readable absolute local path ({path!r})")
    path = os.path.abspath(path)
    if os.path.commonpath((os.path.abspath(root), path)) != os.path.abspath(root) or not _inside(root, path):
        raise Unclear(f"File is outside the configured download folder: {path}")
    return path


def _regular_file(path: str):
    """Do not mistake an unreadable file, symlink or directory for an absent file."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Unclear(f"Cannot inspect {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise Unclear(f"Expected a regular file, without a symlink: {path}")
    return info


def _file_stamp(info) -> dict:
    return {"size": info.st_size, "device": info.st_dev, "inode": info.st_ino,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def _discard_entries(item: dict, root: str) -> List[dict]:
    """Reconcile recorded ownership, without requiring healthy media or original bytes."""
    pending = item["extra"]["import_pending"]
    found = inventory(item)
    if found["problems"]:
        raise Unclear("; ".join(found["problems"]))
    manifest = pending.get("manifest")
    if manifest:
        if not isinstance(manifest, list) or any(not isinstance(entry, dict) for entry in manifest):
            raise Unclear("The import manifest is unreadable")
        entries = copy.deepcopy(manifest)
    else:
        files = pending.get("files")
        if not isinstance(files, list) or not files:
            raise Unclear("The saved import has no complete inventory; no file ownership can be established")
        entries = [{"name": _name(path), "stage_path": path, "source_path": path} for path in files]
    names, paths = set(), set()
    for entry in entries:
        stage = _local_path(root, entry.get("stage_path"))
        source = _local_path(root, entry.get("source_path", stage))
        name = entry.get("name") or os.path.basename(stage)
        if (not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\\" in name
                or name != os.path.basename(stage) or name != os.path.basename(source) or name in names):
            raise Unclear("The saved import has ambiguous file names")
        own_paths = {stage, source}
        if paths & own_paths:
            raise Unclear("Two manifest entries claim the same local path")
        names.add(name)
        paths.update(own_paths)
        entry.update(name=name, stage_path=stage, source_path=source)
    saved_files = pending.get("files")
    if saved_files is not None:
        if (not isinstance(saved_files, list) or len(saved_files) != len(entries)
                or {_local_path(root, path) for path in saved_files} != {entry["stage_path"] for entry in entries}):
            raise Unclear("The saved file list does not match the complete import manifest")
    if pending.get("local_dir") and any(os.path.dirname(entry["stage_path"]) != _local_path(root, pending["local_dir"]) for entry in entries):
        raise Unclear("The saved staging folder does not match its manifest")
    return entries


def _other_claims(tasks: List[dict], item: dict, root: str):
    """Other tasks reserve exact files; an unresolved import can reserve whole candidates."""
    claims, directories = {}, []
    for task in tasks:
        if task.get("task_id") == item.get("task_id"):
            continue
        ident = task.get("task_id")
        extra = task.get("extra") or {}
        if not isinstance(extra, dict):
            raise Unclear(f"Ownership for task {ident} is unreadable")
        pending = extra.get("import_pending") or {}
        if not isinstance(pending, dict):
            raise Unclear(f"Import ownership for task {ident} is unreadable")
        owned = list(pending.get("files") or [])
        for entry in pending.get("manifest") or []:
            if not isinstance(entry, dict):
                raise Unclear(f"Import manifest for task {ident} is unreadable")
            owned.extend([entry.get("source_path"), entry.get("stage_path")])
            if pending.get("local_dir") and entry.get("name"):
                owned.append(os.path.join(pending["local_dir"], _name(entry["name"])))
        for record in (extra.get("operator_discard") or {}).get("files") or []:
            owned.extend([record.get("source"), record.get("destination")])
        directory = task.get("local_dir") or task.get("dir") or _name(extra.get("file_dir"))
        if directory:
            directory = os.path.abspath(os.path.join(root, directory))
            names = list(extra.get("expected_files") or [])
            for record in (extra.get("manifest") or []) + (extra.get("files") or []):
                if not isinstance(record, dict):
                    raise Unclear(f"Download manifest for task {ident} is unreadable")
                names.append(record.get("name") or _name(record.get("filename")))
            names.append(task.get("filename"))
            owned.extend(os.path.join(directory, _name(name)) for name in names if name)
        for path in owned:
            if isinstance(path, str) and path and "\x00" not in path:
                claims[os.path.realpath(path)] = ident
        for path in pending.get("candidate_directories") or []:
            if isinstance(path, str) and path and "\x00" not in path:
                directories.append((os.path.realpath(path), ident))
    return claims, directories


def _check_other_ownership(paths: List[str], tasks: List[dict], item: dict, root: str) -> None:
    claims, directories = _other_claims(tasks, item, root)
    for path in paths:
        real = os.path.realpath(path)
        owner = claims.get(real)
        if owner is None:
            owner = next((ident for folder, ident in directories if real == folder or real.startswith(folder.rstrip(os.sep) + os.sep)), None)
        if owner is not None:
            raise Unclear(f"File is also claimed by saved task {owner}: {path}")


def _mapped_path(local_root: str, server_root: str, path: str) -> str:
    if not _absolute(server_root):
        raise Unclear(f"No readable absolute server mapping for {path}")
    path = _local_path(local_root, path)
    relative = os.path.relpath(path, local_root).replace(os.sep, "/")
    return server_root.rstrip("/\\") + ("/" + relative if relative != "." else "/")


def _discard_targets(item: dict, entries: List[dict], root: str, server_root: str, destinations=()):
    """Check both mappings, including source files and already quarantined files on retry."""
    pending = item["extra"]["import_pending"]
    local_paths = {entry[field] for entry in entries for field in ("stage_path", "source_path")} | set(destinations)
    paths = {_mapped_path(root, server_root, path) for path in local_paths}
    folders = {_mapped_path(root, server_root, os.path.dirname(entry["stage_path"])) for entry in entries}
    saved_local, saved_server = pending.get("download_root"), pending.get("server_root")
    if saved_local or saved_server:
        if not isinstance(saved_local, str) or not os.path.isabs(saved_local) or "\x00" in saved_local or not _absolute(saved_server):
            raise Unclear("The saved download-to-server mapping is incomplete")
        paths.update(_mapped_path(saved_local, saved_server, path) for path in local_paths)
        folders.update(_mapped_path(saved_local, saved_server, os.path.dirname(entry["stage_path"])) for entry in entries)
    saved_folder = pending.get("readarr_path")
    if saved_folder and not _absolute(saved_folder):
        raise Unclear("The saved staging folder has no readable absolute server path")
    if saved_folder:
        folders.add(saved_folder)
    for entry in entries:
        recorded = entry.get("readarr_path") or (saved_folder.rstrip("/\\") + "/" + entry["name"] if saved_folder else None)
        if not recorded and saved_local and saved_server:
            recorded = _mapped_path(saved_local, saved_server, entry["stage_path"])
        if not _absolute(recorded):
            raise Unclear(f"No saved server path can be established for {entry['name']}")
        if saved_folder and _server_path_key(recorded) != _server_path_key(saved_folder.rstrip("/\\") + "/" + entry["name"]):
            raise Unclear(f"Conflicting saved server paths for {entry['name']}")
        if saved_local and saved_server and _server_path_key(recorded) != _server_path_key(_mapped_path(saved_local, saved_server, entry["stage_path"])):
            raise Unclear(f"The saved root mapping conflicts with the staged path for {entry['name']}")
        paths.add(recorded)
        if entry["source_path"] != entry["stage_path"] and not (saved_local and saved_server):
            raise Unclear(f"The saved mapping for the original source of {entry['name']} cannot be established")
    return sorted(paths), sorted(folders)


def _prepare_discard(item: dict, tasks: List[dict], root: str, server_root: str) -> dict:
    entries = _discard_entries(item, root)
    pending = item["extra"]["import_pending"]
    operation = uuid.uuid4().hex
    directory = _local_path(root, os.path.join(root, "failed_imports", "recover-" + operation))
    if os.path.lexists(directory):
        raise Unclear("The proposed quarantine destination already exists")
    files = []
    for entry in entries:
        stage, source = entry["stage_path"], entry["source_path"]
        staged = _regular_file(stage)
        original = staged if source == stage else _regular_file(source)
        if staged is not None and original is not None and source != stage:
            raise Unclear(f"Both staged and source files exist for {entry['name']}; the original path may have been reused")
        path, info = (stage, staged) if staged is not None else (source, original)
        if info is None:
            continue
        digest = import_identity.file_digest(path)
        if _regular_file(path) is None or _file_stamp(os.lstat(path)) != _file_stamp(info):
            raise Unclear(f"File changed while establishing discard ownership: {path}")
        if path != stage:
            if (pending.get("phase") in {"staged", "submission_unknown", "partial"} or pending.get("attempts")
                    or pending.get("command_id") is not None or pending.get("submits") or pending.get("before_ids") is not None
                    or pending.get("inputs_validated")
                    or not entry.get("sha256") or entry.get("sha256") != digest or entry.get("size") != info.st_size):
                raise Unclear(f"The original source is not proven to be an unsubmitted manifest input: {path}")
        destination = os.path.join(directory, entry["name"])
        files.append(dict(_file_stamp(info), sha256=digest, source=path, destination=destination))
    _check_other_ownership([file["source"] for file in files], tasks, item, root)
    paths, folders = _discard_targets(item, entries, root, server_root, [file["destination"] for file in files])
    return {"schema": 1, "operation_id": operation, "phase": "prepared", "at": time.time(),
            "original_task": copy.deepcopy(item), "download_root": os.path.abspath(root),
            "destination_dir": directory, "files": files,
            "expected_paths": sorted({entry[field] for entry in entries for field in ("stage_path", "source_path")}),
            "server_paths": paths, "server_folders": folders}


def _validate_discard(journal: dict, item: dict, tasks: List[dict], root: str, server_root: str):
    """A crash is reconciled by exact source/destination pairs, never by missing sources alone."""
    original = journal.get("original_task")
    operation = journal.get("operation_id")
    if (journal.get("schema") != 1 or not isinstance(original, dict) or original.get("task_id") != item.get("task_id")
            or str(original.get("book_id")) != str(item.get("book_id"))
            or not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f]{32}", operation)
            or journal.get("download_root") != os.path.abspath(root)):
        raise Unclear("The saved discard journal or its download mapping is unreadable")
    entries = _discard_entries(original, root)
    expected = {entry[field] for entry in entries for field in ("stage_path", "source_path")}
    if set(journal.get("expected_paths") or []) != expected:
        raise Unclear("The discard journal no longer matches the original inventory")
    directory = _local_path(root, os.path.join(root, "failed_imports", "recover-" + operation))
    if journal.get("destination_dir") != directory or os.path.islink(directory):
        raise Unclear("The saved quarantine destination is unsafe")
    files = journal.get("files")
    if not isinstance(files, list) or any(not isinstance(file, dict) for file in files):
        raise Unclear("The saved discard file list is unreadable")
    sources, destinations, remaining, moved = set(), set(), [], []
    for file in files:
        source = _local_path(root, file.get("source"))
        destination = _local_path(root, file.get("destination"))
        if (source not in expected or source in sources or destination in destinations
                or destination != os.path.join(directory, os.path.basename(source))):
            raise Unclear("The discard journal contains conflicting file ownership")
        sources.add(source)
        destinations.add(destination)
        at_source, at_destination = _regular_file(source), _regular_file(destination)
        if (at_source is None) == (at_destination is None):
            raise Unclear(f"Discard needs review: both copies exist or neither copy can be found for {source}")
        current, info = (source, at_source) if at_source is not None else (destination, at_destination)
        if (not isinstance(file.get("sha256"), str) or info.st_size != file.get("size")
                or import_identity.file_digest(current) != file["sha256"]):
            raise Unclear(f"A discard input or quarantined file has changed: {current}")
        if at_source is not None and _file_stamp(info) != {key: file.get(key) for key in _file_stamp(info)}:
            raise Unclear(f"The original path was replaced after discard was prepared: {source}")
        (remaining if at_source is not None else moved).append(file)
    for path in expected - sources:
        if _regular_file(path) is not None:
            raise Unclear(f"A previously absent source or staged file has appeared; ownership is unclear: {path}")
    if os.path.isdir(directory):
        from .postprocess import QUARANTINE_MARKER
        if set(os.listdir(directory)) - {os.path.basename(path) for path in destinations} - {QUARANTINE_MARKER}:
            raise Unclear("The quarantine folder contains files outside the discard journal")
    _check_other_ownership(list(sources | destinations), tasks, item, root)
    paths, folders = _discard_targets(original, entries, root, server_root, destinations)
    old_paths, old_folders = journal.get("server_paths"), journal.get("server_folders")
    if not isinstance(old_paths, list) or not isinstance(old_folders, list) or any(not _absolute(path) for path in old_paths + old_folders):
        raise Unclear("The discard journal has unreadable server-path evidence")
    return original, remaining, moved, sorted(set(paths + old_paths)), sorted(set(folders + old_folders))


def _checkpoint_discard(state: Any, item: dict, journal: dict) -> None:
    from .persistence import StatePersistenceError
    extra = copy.deepcopy(item["extra"])
    extra["operator_discard"] = copy.deepcopy(journal)
    extra["import_pending"].update(operator_discard=journal["operation_id"], phase="needs_review",
                                    reason="Operator discard is in progress; resume with recover --discard")
    if state.set_extra(item["task_id"], extra) is False:
        raise StatePersistenceError("The discard checkpoint was refused")
    item["extra"] = extra


def _archive(config_dir: str, entry: dict) -> None:
    """Append the decision and the whole saved record; durable before anything is moved."""
    import json
    from .persistence import fsync_directory
    path = os.path.join(config_dir, "recover_archive.jsonl")
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(config_dir)


def discard(client: Any, state: Any, book: int, local_root: str, server_root: str, config_dir: str, out) -> int:
    from .postprocess import FileMoveIncomplete, move_files_aside
    tasks = state.get_tasks_for_orchestrator()
    selected = [t for t in tasks
             if str(t.get("book_id")) == str(book) or str(((t.get("extra") or {}).get("import_pending") or {}).get("book_id")) == str(book)]
    if len(selected) != 1:
        out.write(f"Expected one saved task for book {book}, found {len(selected)}. Nothing changed.\n")
        return 1
    item = selected[0]
    pending = (item.get("extra") or {}).get("import_pending")
    if not isinstance(pending, dict):
        out.write(f"Book {book} is still downloading (no import has started); nothing to discard. Nothing changed.\n")
        return 1
    phase = pending.get("phase") or ("submission_unknown" if not pending.get("schema") else "")
    if phase not in DISCARDABLE:
        out.write(f"Book {book} is in phase '{phase}'; only {', '.join(sorted(DISCARDABLE))} can be discarded. Nothing changed.\n")
        return 1
    out.write(f"\nBook {book}: {item.get('book_title')} ({phase})\n")
    has_journal = "operator_discard" in (item.get("extra") or {})
    existing = (item.get("extra") or {}).get("operator_discard")
    try:
        if getattr(state, "read_only", False):
            raise Unclear("The saved state is read-only")
        if not isinstance(local_root, str) or not os.path.isabs(local_root) or not os.path.isdir(local_root):
            raise Unclear("The configured download folder is unavailable")
        if pending.get("book_id") and item.get("book_id") and str(pending["book_id"]) != str(item["book_id"]):
            raise Unclear("The saved task and its import name different books")
        if pending.get("operator_discard") and not has_journal:
            raise Unclear("The import has a discard marker but no readable discard journal")
        journal = copy.deepcopy(existing) if has_journal else _prepare_discard(item, tasks, local_root, server_root)
        if not isinstance(journal, dict):
            raise Unclear("The saved discard journal is unreadable")
        if has_journal and pending.get("operator_discard") != journal.get("operation_id"):
            raise Unclear("The import's discard marker does not match its journal")
        original, remaining, moved, server_paths, server_folders = _validate_discard(journal, item, tasks, local_root, server_root)
        report = evidence(client, original, server_paths, server_folders)
    except Exception as exc:
        out.write(f"  BLOCKS DISCARD: {exc}\n")
        out.write("  No further files moved; the saved discard journal and archive must be kept.\n" if has_journal else "  Nothing changed.\n")
        return 1
    for file in journal["files"]:
        out.write(f"  file: {file['source']} -> {file['destination']}\n")
    for c in report.get("commands", []):
        out.write(f"  command {c['id']} {c['name']}: {c['status']}/{c['result']}\n")
    for e in report.get("history_imports", []):
        out.write(f"  history import {e['date']}: {e['dropped']} -> {e['imported']}\n")
    for block in report["blocking"]:
        out.write(f"  BLOCKS DISCARD: {block}\n")
    if report["blocking"]:
        out.write("  No further files moved; the saved discard journal and archive must be kept.\n" if has_journal else "  Nothing changed.\n")
        return 1
    journal["evidence"] = {key: report[key] for key in ("commands", "history_imports", "book_files_now", "notes")}
    journal["server_paths"], journal["server_folders"] = server_paths, server_folders
    journal["moved"] = len(moved)
    try:
        # Re-archive the full task on resume too. Its earlier archive may have been
        # moved or lost, and a completion receipt alone must not replace that record.
        _archive(config_dir, {"at": time.time(), "action": "discard", "phase": "prepared", "book_id": book,
                              "operation_id": journal["operation_id"], "resumed": has_journal, "task": original,
                              "files": journal["files"], "evidence": journal["evidence"]})
        _checkpoint_discard(state, item, journal)  # exact destinations durable BEFORE the first move
        for file in remaining:
            def before_move():
                info = _regular_file(file["source"])
                if info is None or _file_stamp(info) != {key: file.get(key) for key in _file_stamp(info)}:
                    raise Unclear(f"The file changed after discard was prepared: {file['source']}")
                _check_other_ownership([file["source"], file["destination"]], state.get_tasks_for_orchestrator(), item, local_root)

            target = move_files_aside(local_root, os.path.relpath(os.path.dirname(file["source"]), local_root),
                                      [os.path.basename(file["source"])], bucket="failed_imports",
                                      destination=journal["destination_dir"], before_move=before_move)
            if (target != journal["destination_dir"] or _regular_file(file["source"]) is not None
                    or _regular_file(file["destination"]) is None):
                raise Unclear(f"No complete move to the recorded quarantine destination was confirmed for {file['source']}")
            journal.update(phase="incomplete", moved=journal["moved"] + 1)
            _checkpoint_discard(state, item, journal)
        # A rename may have succeeded before an interrupted checkpoint; verify all
        # destinations again before giving up the task's ownership reservation.
        _, unfinished, _, _, _ = _validate_discard(journal, item, state.get_tasks_for_orchestrator(), local_root, server_root)
        if unfinished:
            raise Unclear("Discard still has files at their original locations; the task must remain reserved")
        journal.update(phase="complete", completed_at=time.time())
        _checkpoint_discard(state, item, journal)
        _archive(config_dir, {"at": time.time(), "action": "discard", "phase": "complete", "book_id": book,
                              "operation_id": journal["operation_id"], "files": journal["files"], "evidence": journal["evidence"]})
        if state.remove_task(item["task_id"]) is False:
            raise RuntimeError("The completed task could not be removed")
    except Exception as exc:
        out.write(f"  Discard stopped: {type(exc).__name__}: {exc}\n")
        out.write("  Keep the saved state and recover_archive.jsonl: they record the original task and exact file destinations. "
                  "Files or checkpoints may already have changed. Resolve the error, then rerun --discard for this book.\n")
        return 1 if isinstance(exc, (FileMoveIncomplete, Unclear)) else 3
    if journal["files"]:
        out.write(f"  Files set aside in {journal['destination_dir']}\n")
    out.write(f"  Discarded. The saved record is archived in {os.path.join(config_dir, 'recover_archive.jsonl')}; "
              "the book stays wanted in Chaptarr and is searched again.\n")
    return 0


def _load_config(config_dir: str) -> configparser.ConfigParser:
    from .config import apply_env_overrides, validate_config
    config = configparser.ConfigParser(interpolation=None)
    path = os.path.join(config_dir, "config.ini")
    template = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.ini")
    config.read(path if os.path.exists(path) else template)
    apply_env_overrides(config)
    validate_config(config)
    return config


def _client(config: configparser.ConfigParser) -> Any:
    from readarr_api import ReadarrAPI
    from .net import apply_timeout
    client = ReadarrAPI(config["Readarr"]["host_url"], config["Readarr"]["api_key"])
    apply_timeout(client.session, config.getfloat("Readarr", "request_timeout", fallback=60))
    return client


def main(argv: Optional[List[str]] = None, client: Any = None, state: Any = None, out=None,
         local_root: Optional[str] = None, server_root: Optional[str] = None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m rsoul.recover", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config-dir", default=os.environ.get("RSOUL_CONFIG_DIR", "/data"))
    parser.add_argument("--book", type=int, help="the book ID to inspect or recover")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="turn the record into a fresh, validated import for the next run")
    action.add_argument("--discard", action="store_true", help="archive the record, set its files aside in failed_imports, remove the task")
    args = parser.parse_args(argv)
    if (args.apply or args.discard) and args.book is None:
        parser.error("--apply and --discard need --book")
    changing = args.apply or args.discard

    lock = None
    mutation_started = False
    try:
        if client is None or state is None or local_root is None:
            config = _load_config(args.config_dir)
            local_root = local_root or config.get("Slskd", "download_dir")
            server_root = server_root if server_root is not None else config.get("Slskd", "readarr_download_dir", fallback="") or local_root
            if changing:
                from .locking import InstanceLock
                lock = InstanceLock(args.config_dir)
                try:
                    lock.acquire()
                except Exception as exc:
                    lock = None
                    out.write(f"R:soul appears to be running ({exc}). Stop it first; this changes its saved state.\n")
                    return 2
            if state is None:
                from .state import StateManager
                state = StateManager(args.config_dir, read_only=not changing)
            client = client or _client(config)
        if getattr(state, "load_error", None):
            out.write(f"The saved state cannot be inspected: {state.load_error}\n")
            return 3

        if args.discard:
            mutation_started = True  # discard reports its own checkpoints and partial moves
            return discard(client, state, args.book, local_root, server_root, args.config_dir, out)

        snapshot = state.get_tasks_for_orchestrator()
        for saved in snapshot:
            extra = saved.get("extra") or {}
            saved_pending = extra.get("import_pending") or {}
            if "operator_discard" in extra or saved_pending.get("operator_discard"):
                if args.book is None or str(saved.get("book_id")) == str(args.book) or str(saved_pending.get("book_id")) == str(args.book):
                    out.write(f"Book {saved.get('book_id')} has an operator discard in progress. Keep its journal and archive; "
                              "resume with --book " + str(saved.get("book_id")) + " --discard.\n")
                    if args.book is not None:
                        return 1 if args.apply else 0
        items = candidates(snapshot)
        if args.book is not None:
            items = [i for i in items if str(i.get("book_id")) == str(args.book)
                     or str((i.get("extra") or {}).get("import_pending", {}).get("book_id")) == str(args.book)]
        if not items:
            out.write("No saved import without an outcome" + (f" for book {args.book}" if args.book is not None else "") + ".\n")
            return 0 if args.book is None else 1
        for item in items:
            planned = plan(item, local_root, server_root)
            paths = list(planned["old_server_paths"]) + ([e["readarr_path"] for e in planned["new"]["manifest"]] if planned["new"] else [])
            folders = [planned["old_server_folder"]] + ([planned["new"]["readarr_path"]] if planned["new"] else [])
            report = evidence(client, item, paths, folders)
            _print(report, planned, out)
            if not args.apply:
                continue
            if planned["blocking"] or report["blocking"]:
                out.write("  Nothing changed.\n")
                return 1
            record = planned["new"]
            record["reason"] = "Recovered by operator decision; validation, staging, baseline and import follow on the next run"
            record["recovery"] = {"decided_at": time.time(), "decided_by": "operator (python -m rsoul.recover --apply)",
                                  "evidence": {k: report[k] for k in ("commands", "history_imports", "book_files_now", "notes")},
                                  "inventory": planned["inventory"]["sources"],
                                  "previous": copy.deepcopy(item["extra"]["import_pending"])}
            mutation_started = True
            state.set_extra(item["task_id"], dict(item["extra"], import_pending=record))
            out.write("  Recorded. The next R:soul run validates the staged files and imports them for this book.\n")
        return 0
    except Exception as exc:
        out.write(f"Recovery inspection failed: {type(exc).__name__}: {exc}. "
                  + ("A checkpoint may already have reached disk; inspect the saved state before retrying.\n" if mutation_started else "Nothing changed.\n"))
        return 3
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    sys.exit(main())
