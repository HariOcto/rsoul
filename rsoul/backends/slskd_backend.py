import logging
import os
import re
import time
import copy
import hashlib
import json
import stat
import uuid
from typing import List, Optional, Any, Dict, Tuple, TYPE_CHECKING
from pathlib import Path

from .base import (
    BackendUnavailable,
    DownloadBackend,
    SearchResult,
    DownloadTask,
    DownloadTarget,
    DownloadStatus,
)
from . import register_backend

if TYPE_CHECKING:
    from ..config import Context

from ..download import slskd_download_status, downloads_all_done, enqueue_owned, matching_new_transfers, _transfer_ids, get_user_downloads, is_not_found
from ..outcomes import FailureKind, SearchIncomplete, http_status, check_fatal_state
from ..persistence import StatePersistenceError
from ..search_control import SearchControl, responsive_wait
from ..recording_policy import evaluate_recording
from ..match import book_match, verify_filetype, audiobook_folder_match, split_slskd_path, same_book_files, part_number, books_in_folder, missing_parts, fragment_release
from ..media import AUDIO_EXTENSIONS
from ..library import sibling_titles, require_author_catalog, CatalogUnavailable
from ..display import print_search_summary
from ..postprocess import move_files_aside
from .. import health

logger = logging.getLogger(__name__)


def _generate_fallback_queries(author_name: str, book_title: str, max_fallbacks: int) -> List[str]:
    """Keep the surname and distinctive title words while relaxing punctuation/noise."""
    name = _strip_initials(author_name)
    surname = name.split(",", 1)[0].strip() if "," in name else (name.split()[-1] if name.split() else "")
    title = book_title.split(":", 1)[0].strip()
    words = re.findall(r"[^\W_]+(?:['’][^\W_]+)?", title, re.UNICODE)
    stop = {"the", "a", "an", "of", "and", "or", "for", "to", "in", "on", "book", "volume", "series"}
    distinctive = [word for word in words if word.casefold() not in stop]
    queries = [f"{surname} - {title}"]
    if distinctive:
        queries.append(f"{surname} - {' '.join(distinctive)}")
    # At most one distinctive word is omitted; never collapse to first name + 'the'.
    if len(distinctive) >= 2:
        for index in range(len(distinctive)):
            kept = distinctive[:index] + distinctive[index + 1:]
            if any(len(word) > 2 and not word.isdigit() for word in kept):
                queries.append(f"{surname} - {' '.join(kept)}")
    return queries[:max(0, max_fallbacks)]


def _query_key(query):
    return " ".join(re.findall(r"[^\W_]+", query.casefold(), re.UNICODE))


def _strip_initials(author_name: str) -> str:
    """"Christopher G. Nuttall" -> "Christopher Nuttall"; "J.R.R. Tolkien" -> "Tolkien"."""
    words = [w for w in author_name.split() if not re.fullmatch(r"(?:[A-Za-z]\.)+|[A-Za-z]", w)]
    return " ".join(words)


# Seconds to wait when slskd answers a search with 429 Too Many Requests
RATE_LIMIT_WAIT = 15

# Below this average bitrate over the book's known duration, a download can't be the whole book
MIN_AUDIOBOOK_KBPS = 8

# Extra seconds, beyond the 10 s head start and slskd's own search_timeout, to wait for a search
SEARCH_WAIT_MARGIN = 60


def _execute_search(ctx: "Context", query: str, search_type: str) -> Tuple[List[Dict[str, Any]], str]:
    """Execute a single SLSKD search and return results.

    Args:
        ctx: Application context
        query: Search query string
        search_type: Label for display (e.g., "main", "fallback-1")

    Returns:
        Tuple of (search results list, search ID for cleanup)
    """
    print_search_summary(query, 0, search_type, "searching")
    check_fatal_state(ctx)

    search_timeout_ms = ctx.config.getint("Search Settings", "search_timeout", fallback=5000)
    search = ctx.slskd.searches.search_text(
        searchText=query,
        searchTimeout=search_timeout_ms,
        filterResponses=True,
        maximumPeerQueueLength=ctx.config.getint("Search Settings", "maximum_peer_queue", fallback=50),
        minimumPeerUploadSpeed=ctx.config.getint("Search Settings", "minimum_peer_upload_speed", fallback=0),
    )

    try:
        health.heartbeat()
        responsive_wait(ctx, 10)
        deadline = time.time() + search_timeout_ms / 1000 + SEARCH_WAIT_MARGIN
        while ctx.slskd.searches.state(search["id"], False)["state"] == "InProgress":
            if time.time() >= deadline:
                raise SearchIncomplete(f"Search '{query}' did not finish before its bounded wait expired")
            responsive_wait(ctx, 1)
        results = ctx.slskd.searches.search_responses(search["id"])
        print_search_summary(query, len(results), search_type, "completed")
        return results, search["id"]
    except StatePersistenceError:
        raise
    except Exception:
        # This search ID belongs to this request; only it may be removed. A broken
        # polling request must not leave every subsequent search fighting its slot.
        if ctx.config.getboolean("Slskd", "delete_searches", fallback=True):
            try:
                check_fatal_state(ctx)
                ctx.slskd.searches.delete(search["id"])
            except StatePersistenceError:
                raise
            except Exception:
                pass
        raise


@register_backend("slskd")
class SlskdBackend(DownloadBackend):
    """slskd implementation of DownloadBackend."""

    def __init__(self, ctx: "Context"):
        self.ctx = ctx
        self.client = ctx.slskd
        self.config = ctx.config
        # book ID -> further usable audiobook results of the last search, produced on demand
        self._alternates: Dict[int, Any] = {}
        # book ID -> what the last search considered and rejected (read by the dry run)
        self.search_notes: Dict[int, Dict[str, Any]] = {}
        self._pending_alternates: Dict[str, Any] = {}
        self._search_control = None
        self._replacing_task_id = None

    @property
    def name(self) -> str:
        return "slskd"

    @property
    def priority(self) -> int:
        return self.config.getint("Backends", "slskd_priority", fallback=10)

    @property
    def download_dir(self) -> str:
        """Base directory where slskd saves files."""
        return self.config.get("Slskd", "download_dir", fallback="")

    @property
    def readarr_download_dir(self) -> str:
        """Directory path as seen by Readarr (mapped path)."""
        return self.config.get("Slskd", "readarr_download_dir", fallback=self.download_dir)

    def is_available(self) -> bool:
        """Check if slskd is configured and reachable."""
        if not self.config.has_section("Slskd"):
            return False
        try:
            # Simple check to see if client is reachable
            self.client.options.get()
            return True
        except Exception:
            return False

    def search(self, target: DownloadTarget) -> List[SearchResult]:
        """Search with a bounded normalized query budget and explicit incomplete outcomes."""
        check_fatal_state(self.ctx)
        if self._search_control is None:
            self._search_control = SearchControl(self.ctx)
        control = self._search_control
        author, title = target.author_name, target.book_title
        queries = [f"{author} - {title}"]
        without_initials = _strip_initials(author)
        if without_initials and without_initials != author:
            queries.append(f"{without_initials} - {title}")
        if ":" in title:
            queries.append(f"{without_initials or author} - {title.split(':', 1)[0].strip()}")
        queries.extend(_generate_fallback_queries(author, title, self.config.getint("Search Settings", "max_search_fallbacks", fallback=5)))
        budget = max(1, self.config.getint("Search Settings", "search_query_budget", fallback=6))
        seen, unique = set(), []
        for query in queries:
            key = _query_key(query)
            if key and key not in seen:
                seen.add(key)
                unique.append(query)
        queries = unique[:budget]
        self.search_notes.pop(target.book_id, None)
        self._alternates.pop(target.book_id, None)
        target_dict = {
            "book": target.readarr_book or {"title": title, "id": target.book_id, "seriesTitle": target.series_title},
            "author": target.readarr_author or {"authorName": author},
        }
        if target.is_audiobook:
            if target_dict["book"].get("authorId"):
                try:
                    require_author_catalog(self.ctx, target_dict["book"])
                except CatalogUnavailable as error:
                    raise SearchIncomplete(f"Author bibliography unavailable for safe audiobook selection: {error}", FailureKind.SERVICE_UNAVAILABLE) from error
            target_dict["sibling_titles"] = sibling_titles(self.ctx, target_dict["book"], author)
        failures = []
        for index, query in enumerate(queries):
            label = "main" if index == 0 else f"fallback-{index}"
            search_results = None
            for attempt in range(2):
                try:
                    control.before_search()
                    search_results, search_id = _execute_search(self.ctx, query, label)
                    control.success()
                    break
                except StatePersistenceError:
                    raise
                except SearchIncomplete:
                    raise
                except Exception as error:
                    if http_status(error) == 429:
                        retry_at = control.rate_limited(error)
                        if attempt == 0:
                            logger.info("slskd rate-limited search; waiting until its persisted retry deadline")
                            continue
                        raise SearchIncomplete(str(error), FailureKind.RATE_LIMITED, retry_at) from error
                    failures.append(str(error))
                    logger.warning("Search query could not finish (%s): %s", query, error)
                    break
            if search_results is None:
                continue
            if self.config.getboolean("Slskd", "delete_searches", fallback=True):
                try:
                    check_fatal_state(self.ctx)
                    self.client.searches.delete(search_id)
                except StatePersistenceError:
                    raise
                except Exception:
                    pass
            if search_results:
                results = self._match_audiobook_results(target, target_dict, search_results) if target.is_audiobook else self._match_ebook_results(target, target_dict, search_results)
                if results:
                    return results
        if failures:
            raise SearchIncomplete(f"Search coverage incomplete ({len(failures)} failed query/queries): {failures[-1]}")
        return []

    def _history_failed(self, username, target, release_id=""):
        history = getattr(self.ctx, "history", None)
        return bool(history and history.is_failed(username, target.book_title, backend=self.name, author=target.author_name, media_type=target.media_type, release_id=release_id))

    def _match_thresholds(self) -> Dict[str, Any]:
        """Matching thresholds shared by ebook and audiobook matching."""
        return dict(
            # "User1, User2": names are compared exactly, so the spaces must go
            ignored_users=[u.strip() for u in self.config.get("Search Settings", "ignored_users", fallback="").split(",") if u.strip()],
            minimum_match_ratio=self.config.getfloat("Search Settings", "minimum_filename_match_ratio", fallback=0.5),
            min_length_ratio=self.config.getfloat("Search Settings", "min_length_ratio", fallback=0.4),
            min_jaccard_ratio=self.config.getfloat("Search Settings", "min_jaccard_ratio", fallback=0.25),
            min_word_overlap=self.config.getint("Search Settings", "min_word_overlap", fallback=2),
            min_title_jaccard=self.config.getfloat("Search Settings", "min_title_jaccard", fallback=0.3),
            min_author_jaccard=self.config.getfloat("Search Settings", "min_author_jaccard", fallback=0.5),
        )

    def _match_ebook_results(self, target: DownloadTarget, target_dict: Dict[str, Any], search_results: List[Dict[str, Any]]) -> List[SearchResult]:
        """Single-file matching (original R:soul behaviour): best file per user and format."""
        book_title = target.book_title
        allowed_filetypes = target.allowed_filetypes
        thresholds = self._match_thresholds()
        results: List[SearchResult] = []

        file_cache: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        for result in search_results:
            username = result["username"]
            # Skip if in history (failed previously)
            if self._history_failed(username, target):
                continue

            if username not in file_cache:
                file_cache[username] = {}

            for file in result["files"]:
                for ext in allowed_filetypes:
                    if verify_filetype(file, ext):
                        if ext not in file_cache[username]:
                            file_cache[username][ext] = []
                        file_cache[username][ext].append(file)

        # Match for each user
        for username, types in file_cache.items():
            for ext, files in types.items():
                match = book_match(target_dict, files, username, ext, **thresholds)

                if match:
                    if self._history_failed(username, target, f"{username}|{match['filename']}"):
                        continue
                    file_dir = match["filename"].rsplit("\\", 1)[0] if "\\" in match["filename"] else ""
                    filename = match["filename"].split("\\")[-1]

                    sr = SearchResult(
                        title=target.book_title,
                        author=target.author_name,
                        filename=filename,
                        size_bytes=match["size"],
                        extension=ext,
                        backend_name=self.name,
                        source_id=f"{username}|{match['filename']}",
                        username=username,
                        extra={
                            "username": username,
                            "file_dir": file_dir,
                            "files": [match],
                        },
                    )
                    results.append(sr)

        return results

    def _browse_directory(self, username: str, directory: str) -> Optional[List[Dict[str, Any]]]:
        """Fetch the full listing of a remote folder.

        Search responses only contain the files that matched the query, which for
        a folder of chapters is not guaranteed to be all of them. Returns files with
        full remote paths, or None if browsing fails.
        """
        try:
            listing = self.client.users.directory(username=username, directory=directory)
        except Exception as e:
            logger.warning(f"Could not browse folder '{directory}' from {username}: {e}")
            return None

        # slskd > 0.22.2 returns a list of directories ({"name", "fileCount", "files"}); older
        # versions return a single directory object
        if isinstance(listing, list):
            listing = next((d for d in listing if d.get("name") == directory), None)
        if not isinstance(listing, dict):
            return None

        files = []
        for f in listing.get("files", []):
            name = f.get("filename", "")
            full = name if "\\" in name else f"{directory}\\{name}"
            files.append(dict(f, filename=full, size=f.get("size", 0)))
        return files

    def _match_audiobook_results(self, target: DownloadTarget, target_dict: Dict[str, Any], search_results: List[Dict[str, Any]]) -> List[SearchResult]:
        """Folder matching for audiobooks: pick the best-matching folder and grab every audio file in it."""
        book_title = target.book_title
        thresholds = self._match_thresholds()
        min_size_bytes = int(self.config.getfloat("Search Settings", "audiobook_min_size_mb", fallback=10) * 1024 * 1024)
        browse_limit = self.config.getint("Search Settings", "audiobook_browse_limit", fallback=5)
        allowed = [ext.split(" ")[0].lower() for ext in target.allowed_filetypes]

        candidates = []
        stats: Dict[str, int] = {}
        rejected: List[Dict[str, str]] = []
        for result in search_results:
            username = result["username"]
            if self._history_failed(username, target):
                stats["skipped (failed before)"] = stats.get("skipped (failed before)", 0) + 1
                continue
            for match in audiobook_folder_match(target_dict, result.get("files", []), username, target.allowed_filetypes, stats=stats, rejected=rejected, **thresholds):
                candidates.append((username, match))

        if not candidates:
            # Say why nothing matched; otherwise "N files found" followed by "no results" is a puzzle
            summary = ", ".join(f"{reason}: {count}" for reason, count in sorted(stats.items())) or "no files"
            logger.info(f"No audiobook folder matched '{book_title}' ({summary})")

        # Best name match first, then the configured format order, then the largest folder
        candidates.sort(key=lambda c: (-c[1]["score"], allowed.index(c[1]["extension"]), -c[1]["total_size"]))

        # Candidates are browsed lazily, best first: search() gets the first usable one, and
        # download() asks for the next only if that one can't be started. Each browse is a slow
        # round trip to the peer, so none is made before it is needed.
        notes = self.search_notes.setdefault(target.book_id, {"candidates": [], "stats": {}, "rejected": [], "skipped": [], "not_tried": 0})
        listed = {(c["username"], c["directory"], c["extension"], tuple(c.get("matched_files", []))) for c in notes["candidates"]}
        for u, m in candidates:
            matched_files = sorted(f["filename"] for f in m["files"])
            if (u, m["directory"], m["extension"], tuple(matched_files)) not in listed:
                notes["candidates"].append(
                    {"username": u, "directory": m["directory"], "extension": m["extension"], "score": m["score"], "file_based": bool(m.get("file_based")), "files_found": len(m["files"]), "matched_files": matched_files}
                )
                listed.add((u, m["directory"], m["extension"], tuple(matched_files)))
        # Fallback queries mostly bring back the same folders: count them once
        for reason, count in stats.items():
            notes["stats"][reason] = max(notes["stats"].get(reason, 0), count)
        notes["rejected"].extend(entry for entry in rejected if entry not in notes["rejected"])
        # Folders already looked into for an earlier query of this search are not listed again
        tried = {(skip["username"], skip["directory"], skip["extension"], tuple(skip.get("matched_files", []))) for skip in notes["skipped"]}
        candidates = [(u, m) for u, m in candidates if (u, m["directory"], m["extension"], tuple(sorted(f["filename"] for f in m["files"]))) not in tried]
        # Matching folders beyond the browse limit are never looked at: not proof of anything
        notes["not_tried"] = sum(not item.get("checked", False) for item in notes["candidates"])

        usable = self._usable_audiobook_results(target, target_dict, candidates[:browse_limit], min_size_bytes)
        first = next(usable, None)
        if first is None:
            return []
        self._alternates[target.book_id] = usable
        return [first]

    def found_but_unavailable(self, book_id: int) -> bool:
        """After a search without results: were there matches that just couldn't be used right now?

        True if a matching folder couldn't be listed (peer offline or busy), its local folder
        was in use, or it was never looked at (browse limit). Such a book is still out there;
        only a search where nothing matched, or every match was unsuitable for good, says
        "not found".
        """
        notes = self.search_notes.get(book_id) or {}
        return bool(notes.get("not_tried")) or any(skip["temporary"] for skip in notes.get("skipped", []))

    def _usable_audiobook_results(self, target, target_dict, candidates, min_size_bytes):
        """Establish a same-work set across formats before choosing one complete format."""
        notes = self.search_notes.setdefault(target.book_id, {"candidates": [], "stats": {}, "rejected": [], "skipped": [], "not_tried": 0})
        browse_cache = {}
        for username, match in candidates:
            directory, ext = match["directory"], match["extension"]
            file_based = bool(match.get("file_based"))
            matched_files = sorted(f["filename"] for f in match["files"])
            for item in notes["candidates"]:
                if (item["username"], item["directory"], item["extension"], item.get("matched_files", [])) == (username, directory, ext, matched_files):
                    item["checked"] = True
            notes["not_tried"] = sum(not item.get("checked", False) for item in notes["candidates"])

            def skip(reason, temporary=False, label=""):
                logger.info("Skipping %s from %s: %s", label or directory or "(share root)", username, reason)
                entry = {"username": username, "directory": directory, "extension": ext, "matched_files": matched_files, "reason": reason, "temporary": temporary}
                if entry not in notes["skipped"]:
                    notes["skipped"].append(entry)

            cache_key = (username, directory)
            if cache_key not in browse_cache:
                browse_cache[cache_key] = self._browse_directory(username, directory) if directory else None
            browsed = browse_cache[cache_key]
            if browsed is None:
                skip("could not list the full folder (peer offline or busy)", temporary=True)
                continue
            audio = [dict(f) for f in browsed if split_slskd_path(f["filename"])[0] == directory and split_slskd_path(f["filename"])[1].rsplit(".", 1)[-1].lower() in AUDIO_EXTENSIONS]
            names = [split_slskd_path(f["filename"])[1] for f in audio]
            if file_based:
                seeds = [split_slskd_path(f["filename"])[1] for f in match["files"]]
                own = set(same_book_files(seeds, names))
                audio = [f for f in audio if split_slskd_path(f["filename"])[1] in own]
            elif audio:
                verdict, own_names = books_in_folder(target_dict, [(split_slskd_path(f["filename"])[1], f.get("size", 0)) for f in audio])
                if verdict == "several":
                    skip("its files look like several other books, none of them this one")
                    continue
                if verdict == "own":
                    audio = [f for f in audio if split_slskd_path(f["filename"])[1] in set(own_names)]
                    file_based = True
            files = [f for f in audio if f["filename"].lower().endswith(f".{ext}")]
            if not files:
                skip(f"no {ext} files of this book in the folder listing")
                continue
            if self._chapters_in_other_formats(directory, ext, files, audio):
                skip("this format omits numbered chapters present in another format of this book")
                continue
            files.sort(key=lambda f: f["filename"].casefold())
            if all(fragment_release(target.book_title, split_slskd_path(f["filename"])[1]) for f in files):
                skip("explicit sample/prologue-only recording")
                continue
            total_size = sum(f.get("size", 0) for f in files)
            label = f"{files[0]['filename']} (+{len(files) - 1} more)" if file_based else directory
            missing = missing_parts([split_slskd_path(f["filename"])[1] for f in files])
            if missing:
                skip(f"incomplete ({missing})", label=label)
                continue
            if any(not isinstance(f.get("size"), int) or f["size"] <= 0 for f in files):
                skip("a chapter has no trustworthy positive byte size", temporary=True, label=label)
                continue
            if total_size < min_size_bytes:
                skip(f"{total_size / 1048576:.1f} MB is below audiobook_min_size_mb", label=label)
                continue
            too_short = self._too_small_for_duration(
                target, total_size, self.config.getfloat("Search Settings", "audiobook_min_bitrate_kbps", fallback=MIN_AUDIOBOOK_KBPS))
            if too_short:
                skip(too_short, label=label)
                continue
            policy = evaluate_recording(self.config, [directory] + [split_slskd_path(f["filename"])[1] for f in files], final=False)
            if not policy.allowed:
                skip("recording policy: " + "; ".join(policy.reasons), label=label)
                continue
            result = self._audiobook_result(target, username, directory, ext, files, total_size, match["score"], file_based=file_based)
            if self._history_failed(username, target, result.source_id):
                skip("this release failed before", label=label)
                continue
            if self._local_clashes(directory, files):
                skip("its local download folder is in use or its ownership cannot be checked", temporary=True, label=label)
                continue
            logger.info("Audiobook candidate: %s (%s %s files, %.0f MB) from %s", label, len(files), ext, total_size / 1048576, username)
            yield result

    @staticmethod
    def _too_small_for_duration(target: DownloadTarget, total_size: int, min_kbps: float = MIN_AUDIOBOOK_KBPS) -> str:
        """Say so if the files are far too small for the length Chaptarr gives for the book.

        Even heavily compressed speech needs about 12-16 kbit/s, so anything below min_kbps
        ([Search Settings] audiobook_min_bitrate_kbps, default 8) for the book's whole duration is
        a fragment or an abridgement, not the book. Returns "" when it is plausible, when Chaptarr
        gives no duration, or when the check is off (0).
        """
        try:
            minutes = float((target.readarr_book or {}).get("durationMinutes") or 0)
        except (TypeError, ValueError):
            return ""
        if minutes < 30 or not min_kbps or min_kbps <= 0:
            return ""
        kbps = total_size * 8 / 1000 / (minutes * 60)
        if kbps >= min_kbps:
            return ""
        return f"{total_size / 1048576:.0f} MB is too small for a book of {minutes / 60:.1f} hours ({kbps:.0f} kbit/s, minimum {min_kbps:g})"

    @staticmethod
    def _chapters_in_other_formats(directory, ext, files, browsed):
        """Compare compatible numbering units within the already selected same-work set.

        Two m4b parts alongside twelve mp3 chapters are separate complete encodings;
        chapter 01.mp3 alongside 01+02.m4a is evidence that the mp3 set is incomplete.
        """
        def family(name):
            stem = name.rsplit(".", 1)[0].casefold()
            if re.search(r"\b(?:part|pt)[ ._-]*[0-9]", stem):
                return "part"
            if re.search(r"\b(?:disc|disk|cd)[ ._-]*[0-9]", stem):
                return "disc"
            return "chapter"
        own = {}
        for file in files:
            name = split_slskd_path(file["filename"])[1]
            number = part_number(name)
            if number is not None:
                own.setdefault(family(name), set()).add(number)
        for file in browsed:
            folder, name = split_slskd_path(file["filename"])
            if folder != directory or name.lower().endswith(f".{ext}"):
                continue
            number = part_number(name)
            unit = family(name)
            if number is not None and unit in own and number not in own[unit]:
                return True
        return False

    def _audiobook_result(
        self, target: DownloadTarget, username: str, directory: str, ext: str, files: List[Dict[str, Any]], total_size: int, score: float, file_based: bool = False
    ) -> SearchResult:
        extra: Dict[str, Any] = {"username": username, "file_dir": directory, "files": files, "media_type": "audiobook"}
        if file_based:
            extra["file_based"] = True
        return SearchResult(
            title=target.book_title,
            author=target.author_name,
            filename=split_slskd_path(files[0]["filename"])[1],
            size_bytes=total_size,
            extension=ext,
            backend_name=self.name,
            source_id=f"{username}|{files[0]['filename'] if file_based else directory}",
            score=score,
            username=username,
            extra=extra,
        )

    def download(self, target: DownloadTarget, result: SearchResult) -> Optional[DownloadTask]:
        """Retain lazy alternatives until a definite owned attempt finishes or fails."""
        alternates = self._alternates.pop(target.book_id, None)
        task = self._download_one(target, result)
        attempts = 1
        while task is None and alternates is not None and attempts < 3:
            candidate = next(alternates, None)
            if candidate is None:
                break
            attempts += 1
            task = self._download_one(target, candidate)
        if task is not None and alternates is not None:
            self._pending_alternates[task.task_id] = (target, alternates, attempts)
        return task

    def _checkpoint(self, task):
        check_fatal_state(self.ctx)
        state = getattr(self.ctx, "state", None)
        if state is None or getattr(state, "read_only", False):
            raise StatePersistenceError("A writable durable StateManager is required before enqueueing downloads")
        state.update_task(task)

    def _download_one(self, target: DownloadTarget, result: SearchResult) -> Optional[DownloadTask]:
        check_fatal_state(self.ctx)
        username = result.extra["username"]
        file_dir = result.extra.get("file_dir", "")
        files = copy.deepcopy(result.extra["files"])
        for file in files:
            if "\\" not in file["filename"] and file_dir:
                file["filename"] = file_dir + "\\" + file["filename"]
        local_dir = file_dir.split("\\")[-1] if file_dir else ""
        manifest = [{"filename": f["filename"], "name": f["filename"].split("\\")[-1], "size": f.get("size")} for f in files]
        if (not manifest or len({f["name"] for f in manifest}) != len(manifest)
                or any(not self._safe_leaf(f["name"]) or not isinstance(f["size"], int) or isinstance(f["size"], bool) or f["size"] <= 0 for f in manifest)
                or (local_dir and not self._safe_leaf(local_dir))):
            logger.warning("Cannot enqueue %s: unsafe names, duplicate basenames, or unknown byte sizes", target.book_title)
            return None
        if self._local_clashes(file_dir, files):
            logger.info("Deferring %s: local destination is occupied or could not be checked", target.book_title)
            return None
        pre_existing = _transfer_ids(self.client, username)
        if pre_existing is None:
            return None
        now = time.time()
        task = DownloadTask(
            task_id=f"slskd|{uuid.uuid4()}", backend_name=self.name, status=DownloadStatus.PENDING,
            book_title=target.book_title, author_name=target.author_name, book_id=target.book_id,
            filename=result.filename, series_title=target.series_title, local_dir=local_dir,
            extra={
                "username": username, "file_dir": file_dir, "media_type": target.media_type,
                "release_id": result.source_id, "source_id": result.source_id,
                "manifest_version": 1, "manifest": manifest, "manifest_digest": self._manifest_digest(manifest),
                "expected_files": [f["name"] for f in manifest],
                "files": [dict(f, username=username, file_dir=file_dir) for f in manifest],
                "readarr_book": copy.deepcopy(target.readarr_book), "readarr_author": copy.deepcopy(target.readarr_author),
                "intended_identity": {"book_id": target.book_id, "author_id": target.readarr_author.get("id") or target.readarr_book.get("authorId"),
                    "foreign_book_id": target.readarr_book.get("foreignBookId"), "foreign_author_id": target.readarr_author.get("foreignAuthorId"),
                    "title": target.book_title, "author": target.author_name, "media_type": target.media_type},
                "monitor": {"started_at": now, "last_progress_at": now},
                "enqueue": {"phase": "prepared", "created_at": now, "pre_existing_ids": sorted(pre_existing)},
            },
        )
        # This is the write-ahead intent. A failed checkpoint prohibits the POST.
        self._checkpoint(task)
        check_fatal_state(self.ctx)
        outcome = enqueue_owned(self.client, username, manifest, file_dir, pre_existing,
                                wait=lambda seconds: responsive_wait(self.ctx, seconds))
        task.extra["enqueue"].update(phase=outcome.state, detail=outcome.reason)
        self._bind_records(task, outcome.files)
        if outcome.state == "rejected":
            task.status = DownloadStatus.FAILED
            task.extra["failure_kind"] = outcome.kind or FailureKind.PEER_REJECTED.value
            task.error_message = outcome.reason
            self._checkpoint(task)
            # An explicit non-acceptance and an empty observation has no remote ownership.
            self._record_source_failure(task)
            self.ctx.state.remove_task(task.task_id)
            return None
        if outcome.state == "registered":
            task.status = DownloadStatus.QUEUED
        else:
            task.poll_failed = True
            task.error_message = "Enqueue outcome unknown; retaining intent for reconciliation: " + outcome.reason
            task.extra["failure_kind"] = FailureKind.ENQUEUE_UNKNOWN.value
        self._checkpoint(task)
        logger.info("Enqueue %s for %s: tracking %s/%s files (%s)", outcome.state, task.book_title,
                    sum(bool(f.get("id")) for f in task.extra["files"]), len(manifest), task.task_id)
        return task

    @staticmethod
    def _safe_leaf(name):
        return bool(name) and name not in (".", "..") and not any(c in name for c in ("/", "\\", "\x00"))

    @staticmethod
    def _manifest_digest(manifest):
        return hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _ensure_manifest(self, task):
        """Upgrade recoverable legacy state without guessing missing file sizes."""
        extra = task.extra
        manifest = extra.get("manifest")
        if manifest is not None:
            if extra.get("manifest_digest") and extra["manifest_digest"] != self._manifest_digest(manifest):
                raise BackendUnavailable("download manifest changed; manual review required")
            return manifest
        unnamed = [file for file in extra.get("files", []) if not file.get("filename")]
        if unnamed:
            expected = extra.get("expected_files") or [task.filename.split("\\")[-1]]
            if len(extra["files"]) != 1 or len(expected) != 1 or not self._safe_leaf(expected[0]):
                raise BackendUnavailable("Saved transfer IDs lack unambiguous filenames; manual review required")
            directory = extra.get("file_dir", "")
            unnamed[0]["filename"] = directory + "\\" + expected[0] if directory else expected[0]
        previous = {str(f.get("filename", "")).split("\\")[-1]: f for f in extra.get("files", [])}
        names = list(extra.get("expected_files") or previous.keys() or [task.filename.split("\\")[-1]])
        directory = extra.get("file_dir", "")
        manifest = []
        for name in names:
            name = str(name).split("\\")[-1]
            saved = previous.get(name, {})
            full = saved.get("filename") or (directory + "\\" + name if directory else name)
            if "\\" not in full and directory:
                full = directory + "\\" + full
            size = saved.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                size = None
            manifest.append({"filename": full, "name": name, "size": size})
        extra.update(manifest_version=1, manifest=manifest, legacy_manifest=True,
                     expected_files=names, manifest_digest=self._manifest_digest(manifest))
        return manifest

    def _bind_records(self, task, records):
        """Keep every manifest entry, even after slskd clears a finished transfer."""
        previous = {f["filename"].split("\\")[-1]: f for f in task.extra.get("files", [])}
        observed = {f["filename"].split("\\")[-1]: f for f in records}
        rows = []
        for expected in self._ensure_manifest(task):
            row = dict(previous.get(expected["name"], {}))
            found = observed.get(expected["name"])
            if found:
                if row.get("id") and row["id"] != found.get("id"):
                    raise BackendUnavailable("a different transfer ID tried to replace an owned manifest entry")
                row.update(found)
            row.update(filename=expected["filename"], size=expected["size"], username=task.extra.get("username") or row.get("username"), file_dir=task.extra.get("file_dir") or row.get("file_dir", ""))
            rows.append(row)
        task.extra["files"] = rows
        task.extra["slskd_id"] = next((f.get("id") for f in rows if f.get("id")), None)

    def _reconcile_enqueue(self, task):
        intent = task.extra.get("enqueue", {})
        if intent.get("phase") not in ("prepared", "unknown"):
            return
        manifest = self._ensure_manifest(task)
        records, ambiguous = matching_new_transfers(self.client, task.extra["username"], manifest,
            task.extra.get("file_dir", ""), set(intent.get("pre_existing_ids", [])))
        if ambiguous:
            task.extra["needs_review"] = True
            task.error_message = "Ambiguous new transfer IDs; enqueue intent retained for manual review"
            task.poll_failed = True
            return
        self._bind_records(task, records)
        if all(f.get("id") for f in task.extra["files"]):
            intent["phase"] = "registered"
            task.extra.pop("failure_kind", None)
            task.error_message = None
        else:
            intent["phase"] = "unknown"
            task.extra["failure_kind"] = FailureKind.ENQUEUE_UNKNOWN.value
            task.error_message = "Waiting to reconcile every file of an enqueue with an uncertain response"
        self._checkpoint(task)

    def get_status(self, task: DownloadTask) -> DownloadTask:
        if task.extra.get("retry_retired"):
            raise BackendUnavailable("This attempt was durably retired before an alternate; do not inspect its former local paths")
        manifest = self._ensure_manifest(task)
        task.poll_failed = False
        self._reconcile_enqueue(task)
        self._bind_records(task, [])
        downloads = task.extra["files"]
        if not downloads:
            raise BackendUnavailable("Empty transfer manifest; review required")
        known = [f for f in downloads if f.get("id") is not None and f.get("id") != ""]
        if known:
            slskd_download_status(self.client, known)
        invalid = []
        for f in downloads:
            verified = self._finished_on_disk(task, f)
            status = f.get("status")
            if verified:
                # The immutable requested size and a regular completed-directory file prove
                # completion even when another client has cleared slskd's transfer history.
                f["status"] = {"state": "Completed, Succeeded", "bytesTransferred": f["size"]}
                f.pop("missing", None)
            elif status and status.get("state") == "Completed, Succeeded":
                invalid.append(f)
            elif f.get("missing") and task.extra.get("enqueue", {}).get("phase") not in ("prepared", "unknown"):
                f["status"] = {"state": "Completed, Errored", "bytesTransferred": 0,
                    "exception": "Transfer vanished without a verified complete local file"}
        if any(not f.get("size") for f in manifest):
            task.status = DownloadStatus.PENDING
            task.poll_failed = True
            task.extra.update(failure_kind=FailureKind.LEGACY_UNVERIFIED.value, needs_review=True)
            task.error_message = "Legacy state lacks expected byte sizes; completeness cannot be proved"
            return task
        total = sum(f["size"] for f in manifest)
        task.bytes_transferred = sum(min(f["size"], max(0, int((f.get("status") or {}).get("bytesTransferred") or 0))) for f in downloads)
        task.progress_percent = 100.0 * task.bytes_transferred / total if total else 0.0
        task.extra["progress"] = {"files_total": len(manifest), "files_complete": sum(self._finished_on_disk(task, f) for f in downloads), "bytes_total": total}
        if invalid:
            # slskd may publish success just before its final filesystem rename.
            started = task.extra.setdefault("local_verification_started_at", time.time())
            grace = self.config.getfloat("Download Settings", "completed_file_settle_time", fallback=30)
            if time.time() - started < grace:
                if all((f.get("status") or {}).get("state") == "Completed, Succeeded" for f in downloads):
                    task.status = DownloadStatus.PENDING
                    task.poll_failed = True
                    task.error_message = "Transfer finished; waiting for regular local files at the manifest sizes"
                    return task
            else:
                task.status = DownloadStatus.FAILED
                task.extra["failure_kind"] = FailureKind.LOCAL_FILE_INVALID.value
                task.error_message = "Completed transfer has missing, non-regular, or wrong-size local file(s): " + ", ".join(f["filename"].split("\\")[-1] for f in invalid[:3])
                return task
        else:
            task.extra.pop("local_verification_started_at", None)
        all_succeeded, has_errors = downloads_all_done(downloads)
        if all_succeeded and all(self._finished_on_disk(task, f) for f in downloads):
            task.status = DownloadStatus.COMPLETED
            task.progress_percent = 100.0
            task.bytes_transferred = total
            task.error_message = None
            return task
        if has_errors and task.extra.get("enqueue", {}).get("phase") in ("prepared", "unknown"):
            task.status = DownloadStatus.PENDING
            task.poll_failed = True
            task.error_message = "Some files were rejected; remaining enqueue ownership is still unknown"
            return task
        if has_errors:
            states = sorted({(f.get("status") or {}).get("state", "") for f in downloads})
            task.status = DownloadStatus.FAILED
            rejected = any("Rejected" in state for state in states)
            task.extra["failure_kind"] = FailureKind.PEER_REJECTED.value if rejected else FailureKind.TRANSFER_FAILED.value
            task.error_message = "File(s) failed: " + ", ".join(states)
            return task
        task.poll_failed = any(f.get("status") is None for f in downloads)
        if task.extra.get("enqueue", {}).get("phase") in ("prepared", "unknown"):
            task.poll_failed = True
        states = [(f.get("status") or {}).get("state", "") for f in downloads]
        if any(s in ("InProgress", "Initializing", "Downloading") for s in states):
            task.status = DownloadStatus.DOWNLOADING
        elif "Queued, Remotely" in states:
            task.status = DownloadStatus.QUEUED
        elif "Queued, Locally" in states:
            task.status = DownloadStatus.QUEUED_LOCALLY
        else:
            task.status = DownloadStatus.PENDING
        return task

    def _finished_on_disk(self, task, file):
        size = file.get("size")
        if not self.download_dir or not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            return False
        name = file.get("name") or str(file.get("filename", "")).split("\\")[-1]
        if not self._safe_leaf(name) or (task.local_dir and not self._safe_leaf(task.local_dir)):
            return False
        root = Path(self.download_dir)
        directory = root / (task.local_dir or "")
        path = directory / name
        try:
            if directory.is_symlink():
                return False
            info = path.lstat()
            return stat.S_ISREG(info.st_mode) and info.st_size == size
        except OSError:
            return False

    def _record_source_failure(self, task):
        history = getattr(self.ctx, "history", None)
        if history:
            history.add_failure(task.extra.get("username"), task.book_title, task.error_message or "",
                retry_after_days=1, backend=self.name, author=task.author_name,
                media_type=task.extra.get("media_type", "ebook"), release_id=task.extra.get("release_id", ""),
                failure_kind=task.extra.get("failure_kind", FailureKind.TRANSFER_FAILED.value))

    def retry_after_failure(self, task):
        """Retry a definite peer rejection, after confirming only owned IDs were removed."""
        check_fatal_state(self.ctx)
        pending = self._pending_alternates.pop(task.task_id, None)
        if not pending or task.extra.get("failure_kind") != FailureKind.PEER_REJECTED.value:
            return None
        target, alternates, attempts = pending
        if attempts >= 3:
            return None
        self._record_source_failure(task)
        if not self.cleanup_confirmed(task):
            task.status = DownloadStatus.PENDING
            task.poll_failed = True
            task.extra["retry_cleanup_pending"] = True
            task.error_message = "Peer rejected transfer, but owned cleanup could not be confirmed; retry deferred"
            self._pending_alternates[task.task_id] = pending
            self._checkpoint(task)
            return task
        if not self.discard(task):
            task.extra["needs_review"] = True
            task.error_message = "Owned transfers removed, but failed files could not be fully isolated; retry deferred"
            self._pending_alternates[task.task_id] = pending
            self._checkpoint(task)
            return None
        # A crash after a replacement intent is saved must never let the old
        # attempt claim or discard the replacement's same-name local files.
        task.extra["retry_retired"] = {"since": time.time(), "reason": "Owned transfers removed and named leftovers isolated before alternate retry"}
        self._checkpoint(task)
        # Keep the old journal until the replacement is durable, but permit that
        # one confirmed-cleaned reservation to be reused by lazy alternatives.
        previous_replacement = self._replacing_task_id
        self._replacing_task_id = task.task_id
        try:
            while attempts < 3:
                result = next(alternates, None)
                if result is None:
                    break
                attempts += 1
                replacement = self._download_one(target, result)
                if replacement is not None:
                    self._pending_alternates[replacement.task_id] = (target, alternates, attempts)
                    self.ctx.state.remove_task(task.task_id)
                    logger.info("Trying alternate %s of 3 for %s after a peer rejection", attempts, task.book_title)
                    return replacement
        finally:
            self._replacing_task_id = previous_replacement
        return None

    def _local_clashes(self, file_dir: str, files: List[Dict[str, Any]]) -> List[str]:
        """Reasons not to download into this download's local folder: files of the same name
        already there, or any unfinished download (from anyone) headed for the same folder."""
        leaf = file_dir.split("\\")[-1] if file_dir else ""
        names = {f["filename"].split("\\")[-1] for f in files}
        clashes = set()

        if self.download_dir and os.path.isdir(self.download_dir):
            folder = Path(self.download_dir) / leaf
            if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
                clashes.add("local destination is not a regular directory")
            clashes.update(f"{n} (already in the download folder)" for n in names if os.path.lexists(folder / n))

        # An uncertain POST may have no visible transfer yet. Its durable intent
        # still reserves these output names; a second book must not claim them.
        state = getattr(self.ctx, "state", None)
        if state is not None:
            try:
                for item in state.get_tasks_for_orchestrator():
                    if item.get("task_id") == self._replacing_task_id:
                        continue
                    extra = item.get("extra") or {}
                    reserved_dir = item.get("local_dir") or str(extra.get("file_dir", "")).split("\\")[-1]
                    if reserved_dir != leaf:
                        continue
                    reserved = {str(f.get("name") or f.get("filename", "")).split("\\")[-1]
                                for f in extra.get("manifest", []) or extra.get("files", [])}
                    clashes.update(f"{name} (reserved by recovery task {item.get('task_id')})" for name in names & reserved)
            except Exception as error:
                logger.warning("Could not inspect destination reservations: %s", error)
                clashes.add("saved transfer ownership could not be checked")

        # Unfinished downloads (from any peer or tool) headed for the same local folder. Any such
        # download counts, not only same-named files: Soularr deletes the whole local folder of an
        # album it gives up on, which would take R:soul's files with it.
        # R:soul's own downloads are the exception. Several wanted books often sit in one folder
        # of a peer ("Christopher G Nuttall\\A Savage War Of Peace.mp3", "...\\A Small Colonial
        # War.mp3") and so share a local folder; R:soul only ever moves the files it expects by
        # name, so they don't disturb each other unless the names are the same.
        own = self._own_transfers()
        try:
            for user_transfer in self.client.transfers.get_all_downloads():
                peer = user_transfer.get("username", "?")
                for directory in user_transfer.get("directories", []):
                    if directory.get("directory", "").split("\\")[-1] != leaf:
                        continue
                    for f in directory.get("files", []):
                        if str(f.get("state", "")).startswith("Completed"):
                            continue
                        basename = f.get("filename", "").split("\\")[-1]
                        if (peer, basename) in own and basename not in names:
                            continue
                        clashes.add(f"{basename} (still downloading from {peer})")
        except Exception as e:
            logger.warning(f"Could not check slskd's transfer list for clashing downloads: {e}")
            clashes.add("transfer ownership could not be checked")

        return sorted(clashes)

    def _own_transfers(self) -> set:
        """(peer, file name) of every file R:soul itself is downloading, from its saved tasks."""
        own = set()
        state = getattr(self.ctx, "state", None)
        try:
            for item in state.get_tasks_for_orchestrator() if state else []:
                extra = item.get("extra") or {}
                for f in extra.get("files", []) or extra.get("manifest", []):
                    own.add((f.get("username") or extra.get("username"), str(f.get("filename", "")).split("\\")[-1]))
        except Exception as e:
            logger.debug(f"Could not read R:soul's own downloads from the state: {e}")
        return own

    def discard(self, task: DownloadTask) -> bool:
        """Journal and isolate only this task's files; report confirmed source absence."""
        check_fatal_state(self.ctx)
        if task.extra.get("retry_retired"):
            return True
        if not self.download_dir or not Path(self.download_dir).is_dir():
            return False
        names = list(dict.fromkeys(task.extra.get("expected_files") or
                     [f["filename"].split("\\")[-1] for f in task.extra.get("files", [])]))
        if not names or any(not self._safe_leaf(name) for name in names) or (task.local_dir and not self._safe_leaf(task.local_dir)):
            return False
        source = Path(self.download_dir) / (task.local_dir or "")
        if source.is_symlink() or (source.exists() and not source.is_dir()):
            return False
        record = task.extra.setdefault("discard", {})
        destination = record.get("destination")
        if not destination:
            destination = str(Path(self.download_dir) / "failed_downloads" / f"{task.local_dir or 'root'}-{uuid.uuid4().hex}")
        record.update(phase="prepared", source=str(source), names=names, destination=destination)
        self._checkpoint(task)
        try:
            moved_to = move_files_aside(self.download_dir, task.local_dir or "", names, bucket="failed_downloads",
                                        destination=destination, before_move=lambda: check_fatal_state(self.ctx))
            remaining = [name for name in names if os.path.lexists(source / name)]
            record.update(phase="complete" if not remaining else "incomplete", remaining=remaining)
            if moved_to:
                logger.info("Moved leftovers of %s to %s", task.book_title, moved_to)
        except StatePersistenceError:
            raise
        except Exception as error:
            record.update(phase="incomplete", error=str(error), moved=getattr(error, "moved", []),
                          remaining=[name for name in names if os.path.lexists(source / name)])
            logger.warning("Could not isolate all failed files of %s: %s", task.book_title, error)
            self._checkpoint(task)
            return False
        self._checkpoint(task)
        return not bool(remaining)

    def cancel(self, task: DownloadTask) -> bool:
        """Cancel the download in slskd."""
        check_fatal_state(self.ctx)
        if task.extra.get("retry_retired"):
            return True
        username = task.extra.get("username")
        files = task.extra.get("files", [])
        if not username or not files:
            return False

        success = True
        for file in files:
            if not file.get("id"):
                success = False
                continue
            try:
                check_fatal_state(self.ctx)
                self.client.transfers.cancel_download(username=username, id=file["id"])
            except StatePersistenceError:
                raise
            except Exception as e:
                logger.error(f"Failed to cancel download {file.get('id')} for {username}: {e}")
                success = False
        return success

    def cleanup(self, task: DownloadTask) -> bool:
        return self.cleanup_confirmed(task)

    def cleanup_confirmed(self, task: DownloadTask) -> bool:
        """Remove only this intent's IDs, and verify that each record is gone."""
        check_fatal_state(self.ctx)
        if task.extra.get("retry_retired"):
            return True
        if task.extra.get("enqueue", {}).get("phase") in ("prepared", "unknown"):
            return False
        username = task.extra.get("username")
        owned = {f["id"] for f in task.extra.get("files", []) if f.get("id")}
        if not username:
            return False
        for transfer_id in sorted(owned):
            try:
                check_fatal_state(self.ctx)
                self.client.transfers.cancel_download(username=username, id=transfer_id, remove=True)
            except StatePersistenceError:
                raise
            except Exception as error:
                if not is_not_found(error):
                    logger.warning("Could not remove owned transfer %s: %s", transfer_id, error)
        try:
            listing = get_user_downloads(self.client, username)
            remaining = {f.get("id") for d in listing.get("directories", []) for f in d.get("files", [])}
            return not bool(owned & remaining)
        except Exception as error:
            logger.warning("Could not confirm owned transfer cleanup for %s: %s", task.book_title, error)
            return False

    def reconcile_task(self, task_data: Dict[str, Any]) -> Optional[DownloadTask]:
        """Restore the complete manifest; never turn an arbitrary existing path into success."""
        extra = copy.deepcopy(task_data.get("extra") or {})
        if extra.get("retry_retired"):
            raise BackendUnavailable("This attempt was durably retired; remove only its recovery record without touching files")
        username = extra.get("username")
        if not username:
            raise BackendUnavailable("Saved task has no peer identity; manual review required")
        task = DownloadTask(
            task_id=task_data["task_id"], backend_name=self.name, status=DownloadStatus.PENDING,
            book_title=task_data.get("book_title", ""), author_name=task_data.get("author_name", ""),
            book_id=task_data.get("book_id"), filename=task_data.get("filename", ""),
            series_title=task_data.get("series_title", ""), local_dir=task_data.get("local_dir", ""), extra=extra)
        manifest = self._ensure_manifest(task)
        if not manifest or any(not self._safe_leaf(f["name"]) for f in manifest):
            raise BackendUnavailable("Saved manifest has invalid filenames; manual review required")
        try:
            listing = get_user_downloads(self.client, username)
        except Exception as error:
            raise BackendUnavailable(f"slskd not reachable: {error}") from error
        directory = extra.get("file_dir", "")
        leaf = directory.split("\\")[-1]
        saved = {f["filename"].split("\\")[-1]: f for f in extra.get("files", [])}
        observations = []
        for entry in manifest:
            previous = saved.get(entry["name"], {})
            candidates = []
            for folder in listing.get("directories", []):
                if folder.get("directory") not in (directory, leaf):
                    continue
                for record in folder.get("files", []):
                    if record.get("filename") not in (entry["filename"], entry["name"]):
                        continue
                    if previous.get("id") and record.get("id") != previous["id"]:
                        continue
                    # No ID in a new intent: the pre-enqueue snapshot is authoritative.
                    if extra.get("enqueue") and not previous.get("id"):
                        continue
                    candidates.append(record)
            if len(candidates) > 1:
                raise BackendUnavailable(f"Several transfer records match legacy file {entry['name']}; manual review required")
            if candidates:
                record = candidates[0]
                # A saved transfer ID permits recovering its expected size in old state.
                if entry.get("size") is None and previous.get("id") == record.get("id"):
                    size = record.get("size")
                    if isinstance(size, int) and not isinstance(size, bool) and size > 0:
                        entry["size"] = size
                observations.append(dict(record, filename=entry["filename"], username=username, file_dir=directory, status=record))
        if extra.get("legacy_manifest"):
            extra["manifest_digest"] = self._manifest_digest(manifest)
        self._bind_records(task, observations)
        task = self.get_status(task)
        # Save enriched legacy sizes/IDs and unchanged full manifests for the next restart.
        if getattr(self.ctx, "state", None) is not None:
            self._checkpoint(task)
        return task

    def _reconcile_audiobook_task(self, task_data: Dict[str, Any]) -> Optional[DownloadTask]:
        return self.reconcile_task(task_data)
