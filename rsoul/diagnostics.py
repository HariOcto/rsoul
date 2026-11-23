"""Small, credential-free startup report and read-only recovery inspection."""

import argparse
import json
import logging
from pathlib import Path
import platform

from .persistence import atomic_write_json
from .version import __version__, BUILD_ID, STATE_SCHEMA

logger = logging.getLogger(__name__)


def report_versions(ctx):
    report = {"rsoul": __version__, "build": BUILD_ID, "python": platform.python_version(),
              "import_journal_schema": STATE_SCHEMA, "readarr": {"version": "unknown"}, "slskd": {"version": "unknown"}}
    if ctx.config.getboolean("General", "log_api_versions", fallback=True):
        try:
            status = ctx.readarr.get_system_status()
            if isinstance(status, dict):
                report["readarr"] = {k: status[k] for k in ("appName", "version", "branch") if isinstance(status.get(k), (str, int, float, bool))}
        except Exception as exc:
            logger.info("Readarr/Chaptarr version unavailable: %s", type(exc).__name__)
        if ctx.slskd:
            try:
                version = ctx.slskd.application.version()
                if isinstance(version, (str, int, float)):
                    report["slskd"]["version"] = str(version)
                elif isinstance(version, dict):
                    report["slskd"] = {k: version[k] for k in ("version", "current", "full") if isinstance(version.get(k), str)}
            except Exception as exc:
                logger.info("slskd version unavailable: %s", type(exc).__name__)
    logger.info("R:soul %s (%s); Python %s; Readarr/Chaptarr %s; slskd %s", __version__, BUILD_ID,
        report["python"], report["readarr"].get("version", "unknown"), report["slskd"].get("version", "unknown"))
    logger.info("Import capabilities are verified per transaction: explicit identity, complete manifest, and source/output correlation; missing evidence remains pending")
    if not getattr(ctx.state, "read_only", False):
        try:
            atomic_write_json(Path(ctx.config_dir) / "diagnostics.json", report)
        except Exception as exc:
            logger.warning("Could not write optional diagnostics: %s", exc)
    return report


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _strings(value):
    """Recorded values only; diagnostics never resolves or inspects these paths."""
    return list(dict.fromkeys(v for v in value if isinstance(v, str) and v)) if isinstance(value, list) else []


def _command_ids(pending):
    values = [pending.get("command_id")]
    attempts = pending.get("attempts")
    for attempt in attempts if isinstance(attempts, list) else []:
        if isinstance(attempt, dict):
            values.extend((attempt.get("command_id"), _mapping(attempt.get("receipt")).get("id")))
    result, seen = [], set()
    for value in values:
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip() and str(value) not in seen:
            result.append(value)
            seen.add(str(value))
    return result


def _task_report(item):
    extra = _mapping(item.get("extra"))
    pending = _mapping(extra.get("import_pending"))
    review = _mapping(extra.get("recovery_review"))
    enqueue = _mapping(extra.get("enqueue"))
    cleanup = _mapping(extra.get("cleanup_pending"))
    discard = _mapping(extra.get("discard"))
    import_phase = pending.get("phase")
    needs_review = bool(extra.get("needs_review") or review or import_phase in ("needs_review", "partial", "identity_changed"))
    cleanup_pending = bool(extra.get("cleanup_pending") or extra.get("retry_cleanup_pending")
                           or extra.get("terminal_failure") or discard.get("phase") in ("prepared", "incomplete"))
    if cleanup_pending:
        phase = "cleanup_pending"
        reason = cleanup.get("reason") or discard.get("error") or extra.get("terminal_failure") or "Owned transfer or file cleanup remains unconfirmed"
    elif needs_review:
        phase = import_phase if import_phase in ("needs_review", "partial", "identity_changed") else "needs_review"
        reason = review.get("reason") or pending.get("reason") or extra.get("last_error") or "Saved task requires manual review"
    else:
        phase = import_phase or enqueue.get("phase") or "download"
        reason = pending.get("reason") or pending.get("last_error") or extra.get("last_error") or enqueue.get("detail")

    manifest = pending.get("manifest")
    manifest = manifest if isinstance(manifest, list) else []
    source_files = [entry.get("source_path") for entry in manifest if isinstance(entry, dict)]
    staged_files = [entry.get("stage_path") for entry in manifest if isinstance(entry, dict)]
    paths = {}
    for label, value in (
        ("download_directory", item.get("local_dir") or item.get("dir")),
        ("source_directory", pending.get("source_dir")),
        ("staging_directory", pending.get("local_dir")),
        ("server_staging_directory", pending.get("readarr_path")),
        ("cleanup_source_directory", discard.get("source") or cleanup.get("source")),
        ("cleanup_destination_directory", discard.get("destination") or cleanup.get("destination")),
        ("peer_directory", extra.get("file_dir") or item.get("full_dir")),
    ):
        if isinstance(value, str) and value:
            paths[label] = value
    for label, values in (
        ("source_files", source_files),
        ("staged_files", staged_files + _strings(pending.get("files"))),
        ("retained_inputs", pending.get("retained_inputs")),
        ("review_directories", pending.get("candidate_directories") or review.get("candidate_directories")),
        ("cleanup_moved_names", discard.get("moved")),
        ("cleanup_remaining_names", discard.get("remaining")),
    ):
        values = _strings(values)
        if values:
            paths[label] = values
    expected = manifest or extra.get("manifest") or extra.get("expected_files") or []
    return {"task_id": item.get("task_id"), "book_id": item.get("book_id", item.get("bookId")),
        "author": item.get("author_name"), "title": item.get("book_title", item.get("title")),
        "phase": phase, "reason": reason, "needs_review": needs_review, "cleanup_pending": cleanup_pending,
        "import_phase": import_phase, "enqueue_phase": enqueue.get("phase"), "cleanup_phase": discard.get("phase"),
        "failure_kind": extra.get("failure_kind"),
        "directory": pending.get("local_dir") or discard.get("source") or item.get("local_dir") or item.get("dir"),
        "paths": paths, "expected_files": len(expected) if isinstance(expected, list) else 0,
        "command_ids": _command_ids(pending)}


def main():
    parser = argparse.ArgumentParser(description="Read-only central-state recovery report (no API calls or mutations; sidecars are adopted by normal startup)")
    parser.add_argument("--config-dir", "-c", default="/data")
    args = parser.parse_args()
    from .state import StateManager
    state = StateManager(args.config_dir, read_only=True)
    rows = [_task_report(item) for item in state.get_items()]
    print(json.dumps({"version": __version__, "scope": "central_state",
        "notice": "Saved statuses and paths only; no live service or retained-file verification. Sidecars are adopted by normal startup.",
        "state_error": state.load_error, "pending": rows}, indent=2))
    return 1 if state.load_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
