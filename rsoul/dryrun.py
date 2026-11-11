"""Dry run: search for the wanted books and report what R:soul would download, without
downloading anything.

Matching books by the names people give their folders is guesswork, and the only real test
is your own wanted list against the real Soulseek network. A dry run does everything a
normal run does up to the point of queueing: it fetches the wanted list, searches, matches,
and lists candidate folders on the peers' shares. Then it writes down, per book, what it
would have downloaded and what it turned away, and stops.

Nothing is changed: no download is queued, no saved download is resumed or dropped, nothing
is imported, no book is unmonitored, no peer is recorded as failed, and the page counter of
search_type = incrementing_page stays where it is (so the next real run sees the same books).
"""

import logging
import os
import time
from typing import Any, Dict, List, TYPE_CHECKING

from . import health
from .backends import DownloadTarget, SearchResult

if TYPE_CHECKING:
    from .config import Context

logger = logging.getLogger(__name__)

REPORT_FILENAME = "dry_run_report.txt"
# How many file names, other candidates and rejected folders are listed per book
MAX_LISTED = 8


def is_dry_run(config: Any, flag: bool = False) -> bool:
    """--dry-run on the command line, or [General] dry_run = True (RSOUL__GENERAL__DRY_RUN)."""
    return bool(flag) or config.getboolean("General", "dry_run", fallback=False)


def _mb(size: float) -> str:
    return f"{size / 1048576:.0f} MB" if size >= 1048576 else f"{size / 1024:.0f} KB"


def _basename(path: str) -> str:
    return path.split("\\")[-1]


def _describe_choice(target: DownloadTarget, result: SearchResult) -> List[str]:
    files = result.extra.get("files", [])
    names = [_basename(f["filename"]) for f in files]
    directory = result.extra.get("file_dir", "") or "(share root)"
    lines = [f"  WOULD DOWNLOAD from {result.username or result.backend_name}: {len(files) or 1} {result.extension} file(s), {_mb(result.size_bytes)}"]
    if target.is_audiobook:
        if result.extra.get("file_based"):
            lines.append(f"    matched on: the files' own names (score {result.score:.2f}); only these files, not the rest of the folder")
        else:
            lines.append(f"    matched on: the folder name (score {result.score:.2f}); every {result.extension} file in the folder")
    lines.append(f"    folder: {directory}")
    shown = names if len(names) <= MAX_LISTED else names[: MAX_LISTED - 1] + [f"... {len(names) - MAX_LISTED} more ...", names[-1]]
    lines.extend(f"      {name}" for name in shown)
    return lines


def _describe_notes(notes: Dict[str, Any], chosen: List[SearchResult]) -> List[str]:
    """What the audiobook matcher considered besides the choice, and what it turned away."""
    lines: List[str] = []
    picked = {(r.username, r.extra.get("file_dir")) for r in chosen}
    others = [c for c in notes.get("candidates", []) if (c["username"], c["directory"]) not in picked]
    if others:
        lines.append(f"  other matching folders ({len(others)}), in the order they would be tried:")
        for c in others[:MAX_LISTED]:
            how = "file names" if c["file_based"] else "folder name"
            lines.append(f"    {c['score']:.2f}  {c['username']}: {c['directory']}  [{c['extension']}, matched on {how}]")
        if len(others) > MAX_LISTED:
            lines.append(f"    ... and {len(others) - MAX_LISTED} more")

    stats = notes.get("stats") or {}
    if stats:
        lines.append("  turned away: " + ", ".join(f"{reason}: {count}" for reason, count in sorted(stats.items())))
    rejected = notes.get("rejected") or []
    seen = set()
    listed = 0
    for entry in rejected:
        key = (entry["reason"], _basename(entry["directory"]).lower())
        if key in seen:
            continue
        seen.add(key)
        if listed == MAX_LISTED:
            lines.append("    ...")
            break
        lines.append(f"    {entry['reason']}: {entry['directory']}  ({entry['username']})")
        listed += 1
    return lines


def run_dry_run(ctx: "Context", targets: List[DownloadTarget]) -> Dict[str, int]:
    """Search for each target and report the outcome. Returns counts for the run summary."""
    orchestrator = ctx.orchestrator
    report: List[str] = [
        f"R:soul dry run, {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "Nothing was downloaded, imported or changed. Check each WOULD DOWNLOAD entry: is it the right",
        "book, and all of it? Check the folders that were turned away: should one of them have matched?",
        "",
    ]
    would_download = nothing = not_searched = 0
    batch_delay = ctx.config.getfloat("General", "batch_delay", fallback=3.0)

    for number, target in enumerate(targets, start=1):
        if number > 1 and batch_delay > 0:
            time.sleep(batch_delay)
        health.heartbeat()
        entry = [f"[{number}/{len(targets)}] {target.author_name} - {target.book_title}  ({target.media_type}, formats: {', '.join(target.allowed_filetypes)})"]
        if target.series_title:
            entry.append(f"  series: {target.series_title}")

        results: List[SearchResult] = []
        notes: Dict[str, Any] = {}
        searched = False
        for backend in orchestrator.backends if orchestrator else []:
            if not backend.is_available():
                entry.append(f"  backend {backend.name} is not available")
                continue
            try:
                results = backend.search(target)
                searched = True
            except Exception as e:
                entry.append(f"  backend {backend.name} could not search: {e}")
                continue
            finally:
                # Candidates held back for a real download are not needed here
                getattr(backend, "_alternates", {}).pop(target.book_id, None)
                notes = getattr(backend, "search_notes", {}).pop(target.book_id, None) or notes
            if results:
                break

        if results:
            would_download += 1
            entry.extend(_describe_choice(target, results[0]))
            if not target.is_audiobook and len(results) > 1:
                entry.append(f"  other matching files ({len(results) - 1}), tried in this order if the first can't be started:")
                entry.extend(f"    {r.username}: {r.filename} ({_mb(r.size_bytes)})" for r in results[1:MAX_LISTED])
        elif searched:
            nothing += 1
            entry.append("  NOTHING WOULD BE DOWNLOADED: no file or folder matched")
        else:
            not_searched += 1
            entry.append("  NOT SEARCHED: no backend could be asked")
        entry.extend(_describe_notes(notes, results[:1]))

        for line in entry:
            logger.info(line)
        report.extend(entry + [""])

    summary = f"Dry run finished: {would_download} would be downloaded, {nothing} without a match, {not_searched} not searched (of {len(targets)} wanted)"
    logger.info(summary)
    report.append(summary)

    path = os.path.join(ctx.config_dir, REPORT_FILENAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(report) + "\n")
        logger.info(f"Dry run report written to {path}")
    except OSError as e:
        logger.warning(f"Could not write the dry run report to {path}: {e}")

    return {"would_download": would_download, "no_match": nothing, "not_searched": not_searched}
