"""
Stacks backend for downloading books via Anna's Archive.

Flow:
1. Search Anna's Archive by ISBN13 to get MD5 hashes
2. Filter results by title/author matching
3. Submit best match MD5 to Stacks API queue
4. Poll Stacks status until download completes

Supports FlareSolverr for bypassing DDoS-Guard/Cloudflare protection.
"""

import difflib
import logging
import re
import urllib.parse
import time
import os
from typing import List, Optional, Any, Dict, Tuple, TYPE_CHECKING

import requests
from requests import Session

from .base import (
    DownloadBackend,
    SearchResult,
    DownloadTask,
    DownloadTarget,
    DownloadStatus,
)
from . import register_backend
from ..utils import (
    normalize_for_matching,
    jaccard_similarity,
    title_contained_in_filename,
    extract_author_title,
)

if TYPE_CHECKING:
    from ..config import Context

logger = logging.getLogger(__name__)

# Anna's Archive mirrors for round-robin load balancing
AA_MIRRORS = [
    "https://annas-archive.li",
    "https://annas-archive.gl",
]

# Track current mirror index for round-robin (module-level state)
_aa_mirror_index = 0

# Persistent session for Anna's Archive direct requests
_aa_session: Optional[Session] = None

# Timestamp of last Anna's Archive request for elapsed-time-aware rate limiting
_last_aa_request_time: float = 0.0


def _get_aa_session() -> Session:
    """Get or create persistent session for Anna's Archive requests."""
    global _aa_session
    if _aa_session is None:
        _aa_session = Session()
        _aa_session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }
        )
    return _aa_session


def _get_next_aa_mirror() -> str:
    """Get the next Anna's Archive mirror URL using round-robin."""
    global _aa_mirror_index
    mirror = AA_MIRRORS[_aa_mirror_index]
    _aa_mirror_index = (_aa_mirror_index + 1) % len(AA_MIRRORS)
    return mirror


def _fetch_with_flaresolverr(
    target_url: str,
    flaresolverr_url: str,
    timeout: int = 300000,
) -> Optional[str]:
    """Fetch a URL using FlareSolverr to bypass DDoS-Guard/Cloudflare.

    Args:
        target_url: The URL to fetch
        flaresolverr_url: FlareSolverr API endpoint
        timeout: Max timeout in milliseconds (default: 300000 = 5 minutes)

    Returns:
        HTML content if successful, None otherwise
    """
    # Simple request without session management - let FlareSolverr handle everything
    payload = {
        "cmd": "request.get",
        "url": target_url,
        "maxTimeout": timeout,
    }

    try:
        logger.debug(f"FlareSolverr request to: {target_url}")
        resp = requests.post(
            flaresolverr_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout // 1000 + 10,  # Convert to seconds + buffer
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") == "ok":
            solution = data.get("solution", {})
            html = solution.get("response")
            status_code = solution.get("status")

            # Check if we actually got content
            if not html:
                logger.warning(f"FlareSolverr returned empty response (status={status_code})")
                return None

            logger.info(f"FlareSolverr success: status={status_code}, length={len(html)}")

            # Log cookies for debugging
            cookies = solution.get("cookies", [])
            if cookies:
                logger.debug(f"FlareSolverr cookies: {len(cookies)} cookies received")

            return html
        else:
            error_msg = data.get("message", "Unknown error")
            logger.error(f"FlareSolverr failed: {error_msg}")
            return None

    except requests.exceptions.Timeout:
        logger.error(f"FlareSolverr request timed out for: {target_url}")
        return None
    except Exception as e:
        logger.error(f"FlareSolverr request failed: {e}")
        return None


# Anna's Archive search URL template (mirror will be prepended)
AA_SEARCH_PATH = "/search?index=&page=1&sort=&ext=epub&ext=mobi&ext=azw3&lang=en&display=&q={query}"

# Combined pattern: captures font-mono path and immediately following MD5 link as a pair
# This ensures we get the correct path-MD5 association from the same result block
# Structure: <div class="...font-mono...">path</div>\n<a href="/md5/{md5}"...
RESULT_PATTERN = re.compile(
    r'<div[^>]*class="[^"]*font-mono[^"]*"[^>]*>([^<]+)</div>\s*'
    r'<a\s+href="/md5/([a-f0-9]{32})"',
    re.IGNORECASE | re.DOTALL,
)


def score_aa_result(
    path: str,
    book_title: str,
    author_name: str,
    series_title: str,
    min_match_ratio: float = 0.5,
) -> float:
    """Score an Anna's Archive result against target book.

    Uses the same matching logic as slskd backend:
    - Jaccard similarity for word overlap
    - Title containment check
    - Author/title component matching

    Args:
        path: File path from AA (e.g., "zlib/Genre/Author/Title_123.epub")
        book_title: Target book title
        author_name: Target author name
        series_title: Target series title (optional)
        min_match_ratio: Minimum score to consider valid

    Returns:
        Match score (0.0 - 1.0+), higher is better. Returns 0.0 if no match.
    """
    if not path:
        return 0.0

    # Extract filename from path
    filename = path.replace("\\", "/").split("/")[-1]
    # Unescape HTML entities
    filename = filename.replace("&amp;", "&").replace("&#39;", "'")

    # Strip extension for matching to prevent format inflation (e.g. .mobi matching query parts)
    filename_for_match = filename.rsplit(".", 1)[0] if "." in filename else filename

    # Also check the full path for author/title info
    # AA paths often have format: source/Genre/Author/Title_id.ext
    path_clean = path.replace("&amp;", "&").replace("&#39;", "'")

    # Build expected patterns
    expected_author = normalize_for_matching(author_name)
    normalized_filename = normalize_for_matching(filename_for_match)
    normalized_path = normalize_for_matching(path_clean)

    score = 0.0

    # Check 1: Title containment (strong signal)
    if title_contained_in_filename(book_title, filename_for_match):
        score += 0.4
        logger.debug(f"Title contained in filename: +0.4")

    # Also check against series title if available
    if series_title and title_contained_in_filename(series_title, filename_for_match):
        score += 0.2
        logger.debug(f"Series title contained in filename: +0.2")

    # Check 2: Jaccard similarity on filename
    jaccard_score, overlap_count, _ = jaccard_similarity(f"{book_title} {author_name}", filename_for_match)
    score += jaccard_score * 0.3
    logger.debug(f"Jaccard similarity: {jaccard_score:.2f} (+{jaccard_score * 0.3:.2f})")

    # Check 3: Component-wise matching (Calculated early for gating Author Path bonus)
    # Use MIN score instead of AVG to prevent strong Author match from carrying weak Title match
    component_title_score = 0.0

    found_part1, found_part2 = extract_author_title(filename)  # extract_author_title handles extension stripping internally
    if found_part2:
        # Try both orderings
        author_as_p1 = jaccard_similarity(author_name, found_part1)[0]
        title_as_p2 = jaccard_similarity(book_title, found_part2)[0]
        score_order1 = min(author_as_p1, title_as_p2)

        author_as_p2 = jaccard_similarity(author_name, found_part2)[0]
        title_as_p1 = jaccard_similarity(book_title, found_part1)[0]
        score_order2 = min(author_as_p2, title_as_p1)

        # Track the best title component score found
        if score_order1 >= score_order2:
            component_title_score = title_as_p2
            final_component_score = score_order1
        else:
            component_title_score = title_as_p1
            final_component_score = score_order2

        weighted_score = final_component_score * 0.2
        score += weighted_score
        logger.debug(f"Component matching: +{weighted_score:.2f} (min score {final_component_score:.2f})")

    # Check 4: Author in path (AA often has author as folder name)
    # GATED: Only apply if we have some evidence of the title matching
    if expected_author in normalized_path:
        title_matches = title_contained_in_filename(book_title, filename_for_match)

        # Require either strong title match, decent component title match, or very high Jaccard
        if title_matches or component_title_score > 0.4 or jaccard_score > 0.6:
            score += 0.2
            logger.debug(f"Author found in path: +0.2")
        else:
            logger.debug(f"Author found in path but skipped (weak title match): +0.0")

    # Check 5: Direct sequence matching as fallback
    direct_ratio = difflib.SequenceMatcher(None, normalize_for_matching(f"{author_name} {book_title}"), normalized_filename).ratio()
    score += direct_ratio * 0.1
    logger.debug(f"Direct sequence match: {direct_ratio:.2f} (+{direct_ratio * 0.1:.2f})")

    logger.debug(f"Total score for '{filename}': {score:.2f}")

    return score if score >= min_match_ratio else 0.0


@register_backend("stacks")
class StacksBackend(DownloadBackend):
    """Stacks + Anna's Archive implementation of DownloadBackend."""

    def __init__(self, ctx: "Context"):
        self.ctx = ctx
        self.config = ctx.config

        # Stacks API configuration
        self.api_key = self.config.get("Stacks", "api_key", fallback="")
        self.base_url = self.config.get("Stacks", "host_url", fallback="http://localhost:7788")
        self.download_dir_path = self.config.get("Stacks", "download_dir", fallback="")

        # Timeouts
        self.search_timeout = self.config.getint("Stacks", "search_timeout", fallback=30)
        self.download_timeout = self.config.getint("Stacks", "download_timeout", fallback=600)
        self.poll_interval = self.config.getint("Stacks", "poll_interval", fallback=5)

        # Matching threshold
        self.min_match_ratio = self.config.getfloat("Stacks", "min_match_ratio", fallback=0.6)

        # FlareSolverr configuration
        self.flaresolverr_enabled = self.config.getboolean("Stacks", "flaresolverr_enabled", fallback=False)
        self.flaresolverr_url = self.config.get("Stacks", "flaresolverr_url", fallback="http://localhost:8191/v1")

    @property
    def name(self) -> str:
        return "stacks"

    @property
    def priority(self) -> int:
        return self.config.getint("Backends", "stacks_priority", fallback=20)

    @property
    def download_dir(self) -> str:
        return self.download_dir_path

    @property
    def readarr_download_dir(self) -> str:
        """Directory path as seen by Readarr (mapped path)."""
        return self.config.get("Stacks", "readarr_download_dir", fallback=self.download_dir)

    def is_available(self) -> bool:
        """Check if Stacks is configured and reachable."""
        if not self.config.has_section("Stacks"):
            return False

        if not self.api_key:
            logger.warning("Stacks API key not configured")
            return False

        try:
            resp = requests.get(f"{self.base_url}/api/health", timeout=5)
            return resp.status_code == 200
        except Exception as e:
            logger.debug(f"Stacks health check failed: {e}")
            return False

    def search(self, target: DownloadTarget) -> List[SearchResult]:
        """Search Anna's Archive by ISBN13, then fallback to Author - Title search."""
        if getattr(target, "media_type", "ebook") == "audiobook":
            logger.debug(f"Stacks only handles ebooks - skipping audiobook: {target.book_title}")
            return []

        primary_isbn = target.get_primary_isbn13()
        all_isbns = target.get_isbn13s()

        # Prioritized list: primary ISBN first, then any other unique ISBNs
        priority_isbns = []
        if primary_isbn:
            priority_isbns.append(primary_isbn)

        for isbn in all_isbns:
            if isbn not in priority_isbns:
                priority_isbns.append(isbn)

        # Try ISBN searches first
        if priority_isbns:
            num_isbns = len(priority_isbns)
            for i, isbn in enumerate(priority_isbns, 1):
                logger.info(f"Attempt {i}/{num_isbns}: Searching Anna's Archive for ISBN: {isbn}")

                try:
                    results = self._search_annas_archive(isbn, target)
                    if results:
                        logger.info(f"Found {len(results)} matching result(s) on Anna's Archive for ISBN {isbn}")
                        return results

                    logger.info(f"No matching results on Anna's Archive for ISBN {isbn}")
                except Exception as e:
                    logger.error(f"Anna's Archive search failed for ISBN {isbn}: {e}")
                    # Continue to next ISBN if one fails
                    continue
        else:
            logger.info(f"No ISBN13 available for '{target.book_title}', trying Author - Title search")

        # Fallback: Search by "Author - Title"
        author_title_query = f"{target.author_name} - {target.book_title}"
        logger.info(f"Fallback: Searching Anna's Archive for: {author_title_query}")

        try:
            results = self._search_annas_archive(author_title_query, target)
            if results:
                logger.info(f"Found {len(results)} matching result(s) on Anna's Archive for Author-Title search")
                return results

            logger.info(f"No matching results on Anna's Archive for Author-Title search")
        except Exception as e:
            logger.error(f"Anna's Archive Author-Title search failed: {e}")

        return []

    def _parse_aa_results(self, html: str) -> List[Tuple[str, str]]:
        """Parse Anna's Archive HTML and extract (path, md5) pairs.

        Uses a combined regex that captures the font-mono path div
        immediately followed by the MD5 link, ensuring correct pairing.

        Args:
            html: Raw HTML from Anna's Archive search page

        Returns:
            List of (path, md5) tuples
        """
        matches = RESULT_PATTERN.findall(html)
        results = []
        for path, md5 in matches:
            path = path.strip()
            md5 = md5.lower()
            results.append((path, md5))

        logger.debug(f"Parsed {len(results)} path-MD5 pairs from HTML")
        return results

    def _search_annas_archive(self, isbn: str, target: DownloadTarget) -> List[SearchResult]:
        """Fetch Anna's Archive search page, parse results, and filter by match score."""
        global _last_aa_request_time

        # Elapsed-time-aware rate limiting: only sleep if not enough time has passed
        aa_delay = self.config.getfloat("Stacks", "aa_request_delay", fallback=5.0)
        elapsed = time.time() - _last_aa_request_time
        if elapsed < aa_delay:
            wait_time = aa_delay - elapsed
            logger.debug(f"Rate limiting: waiting {wait_time:.1f}s before next AA request")
            time.sleep(wait_time)

        _last_aa_request_time = time.time()

        # Get next mirror using round-robin
        mirror = _get_next_aa_mirror()
        url = mirror + AA_SEARCH_PATH.format(query=urllib.parse.quote(isbn))
        logger.info(f"Using Anna's Archive mirror: {mirror}")

        html = None

        # Try FlareSolverr if enabled
        if self.flaresolverr_enabled:
            logger.info("Using FlareSolverr to bypass DDoS protection")
            html = _fetch_with_flaresolverr(
                target_url=url,
                flaresolverr_url=self.flaresolverr_url,
                timeout=300000,  # 5 minutes (maximum recommended for FlareSolverr)
            )

            if html is None:
                logger.warning("FlareSolverr failed, falling back to direct request")

        # Fall back to direct request if FlareSolverr disabled or failed/returned empty
        if not html:
            logger.info(f"Attempting direct request to: {mirror}")
            session = _get_aa_session()

            try:
                resp = session.get(url, timeout=self.search_timeout)

                # Enhanced error handling for 403 and other HTTP errors
                if resp.status_code == 403:
                    logger.error(f"403 Forbidden from {mirror}")
                    logger.error(f"Response headers: {dict(resp.headers)}")
                    logger.error(f"Response body (first 2000 chars): {resp.text[:2000]}")
                    logger.error(f"Cookies in session: {dict(session.cookies)}")
                    # Don't raise - return empty results so orchestrator can try next backend
                    return []
                elif resp.status_code >= 400:
                    logger.error(f"HTTP {resp.status_code} from {mirror}")
                    logger.error(f"Response body (first 1000 chars): {resp.text[:1000]}")
                    return []

                html = resp.text

            except requests.exceptions.RequestException as e:
                logger.error(f"Request failed to {mirror}: {e}")
                return []

        # Parse path-MD5 pairs from HTML
        parsed_results = self._parse_aa_results(html)

        # Score and filter results
        scored_results: List[Tuple[float, str, str]] = []  # (score, md5, path)

        for path, md5 in parsed_results:
            # Skip if this AA result is already in the blocklist
            if self.ctx.history and self.ctx.history.is_failed(path, target.book_title):
                logger.debug(f"Skipping blocklisted result: {path[:60]}...")
                continue

            # Score this result
            match_score = score_aa_result(
                path=path,
                book_title=target.book_title,
                author_name=target.author_name,
                series_title=target.series_title,
                min_match_ratio=self.min_match_ratio,
            )

            if match_score > 0:
                scored_results.append((match_score, md5, path))
                logger.debug(f"Matched: score={match_score:.2f}, path={path[:60]}...")
            else:
                logger.debug(f"Rejected: path={path[:60]}...")

        # Sort by score (highest first)
        scored_results.sort(key=lambda x: x[0], reverse=True)

        # Convert to SearchResult objects
        results: List[SearchResult] = []
        for score, md5, path in scored_results:
            # Extract filename from path
            if path:
                # Handle both forward and backslashes in AA paths
                filename = path.replace("\\", "/").split("/")[-1]
                filename = filename.replace("&amp;", "&").replace("&#39;", "'")
            else:
                filename = f"{target.book_title}.epub"

            # Determine extension
            ext = ""
            if "." in filename:
                ext = filename.rsplit(".", 1)[-1].lower()

            # Skip if extension not in allowed list
            if target.allowed_filetypes and ext not in target.allowed_filetypes:
                logger.debug(f"Skipping {filename} - extension '{ext}' not in allowed types")
                continue

            results.append(
                SearchResult(
                    title=target.book_title,
                    author=target.author_name,
                    filename=filename,
                    size_bytes=0,  # AA doesn't show size in search
                    extension=ext,
                    backend_name=self.name,
                    source_id=md5,
                    score=score,
                    extra={
                        "md5": md5,
                        "path": path,
                        "isbn": isbn,
                    },
                )
            )

        return results

    def download(self, target: DownloadTarget, result: SearchResult) -> Optional[DownloadTask]:
        """Submit MD5 to Stacks queue."""
        md5 = result.extra.get("md5") or result.source_id

        logger.info(f"Submitting to Stacks queue: {result.filename} (MD5: {md5}, score: {result.score:.2f})")

        try:
            resp = requests.post(
                f"{self.base_url}/api/queue/add",
                json={"md5": md5, "source": "rsoul"},
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": self.api_key,
                },
                timeout=10,
            )

            if resp.status_code != 200:
                logger.error(f"Stacks queue add failed: {resp.status_code} - {resp.text}")
                return None

            data = resp.json()
            if not data.get("success"):
                logger.error(f"Stacks queue add rejected: {data.get('message')}")
                return None

            logger.info(f"Successfully queued in Stacks: {md5}")

            return DownloadTask(
                task_id=md5,
                backend_name=self.name,
                status=DownloadStatus.QUEUED,
                book_title=target.book_title,
                author_name=target.author_name,
                book_id=target.book_id,
                filename=result.filename,
                series_title=target.series_title,
                local_dir="",  # Files are in the base download_dir
                extra={
                    "md5": md5,
                    "path": result.extra.get("path", ""),
                    "isbn": result.extra.get("isbn", ""),
                },
            )

        except Exception as e:
            logger.error(f"Failed to submit to Stacks: {e}")
            return None

    def get_status(self, task: DownloadTask) -> DownloadTask:
        """Poll Stacks status API for download progress."""
        md5 = task.extra.get("md5") or task.task_id

        try:
            resp = requests.get(f"{self.base_url}/api/status", headers={"X-API-Key": self.api_key}, timeout=10)

            if resp.status_code != 200:
                logger.warning(f"Stacks status check failed: {resp.status_code}")
                return task

            data = resp.json()

            # Check if this MD5 is the active download
            current = data.get("current")
            if current and current.get("md5") == md5:
                task.status = DownloadStatus.DOWNLOADING

                # Robustly handle progress value
                prog = current.get("progress", 0)
                if isinstance(prog, dict):
                    # Handle case where progress is a dict
                    prog = prog.get("percent", prog.get("percentage", 0))

                try:
                    task.progress_percent = float(prog)
                except (ValueError, TypeError):
                    task.progress_percent = 0.0

                return task

            # Check if in queue
            queue = data.get("queue", [])
            for item in queue:
                if item.get("md5") == md5:
                    task.status = DownloadStatus.QUEUED
                    return task

            # Check history for completion/failure
            history = data.get("recent_history", [])
            for item in history:
                if item.get("md5") == md5:
                    if item.get("success"):
                        task.status = DownloadStatus.COMPLETED
                        if item.get("filename"):
                            task.filename = item["filename"]
                    else:
                        task.status = DownloadStatus.FAILED
                        task.error_message = item.get("error") or "Unknown error"
                    return task

            # Not found anywhere - might still be processing
            # Check local file as fallback (in case it dropped off history)
            # Use backend's internal directory path which is more reliable
            filename = task.filename
            if filename:
                # Ensure we only use the basename, stripping any directory components (both / and \)
                clean_filename = filename.replace("\\", "/").split("/")[-1]
                filepath = os.path.join(self.download_dir, clean_filename)
                if os.path.exists(filepath):
                    logger.info(f"MD5 {md5} not found in Stacks status, but file exists on disk: {filepath}")
                    task.status = DownloadStatus.COMPLETED
                    return task

            # If not found and not on disk, assume it failed/lost
            # We can't wait forever, so mark as FAILED
            logger.warning(f"MD5 {md5} not found in Stacks status or on disk - marking as failed (lost)")
            task.status = DownloadStatus.FAILED
            task.error_message = "Task lost from Stacks history and not found on disk"
            return task

        except Exception as e:
            logger.error(f"Failed to get Stacks status: {e}")
            return task

    def cancel(self, task: DownloadTask) -> bool:
        """Cancel is not directly supported by Stacks API."""
        logger.warning("Stacks backend does not support direct cancellation")
        return False

    def cleanup(self, task: DownloadTask) -> None:
        """No cleanup needed for Stacks."""
        pass

    def reconcile_task(self, task_data: Dict[str, Any]) -> Optional[DownloadTask]:
        """Reconcile a persisted task with current Stacks state."""
        md5 = task_data.get("extra", {}).get("md5") or task_data.get("task_id")

        if not md5:
            return None

        task = DownloadTask(
            task_id=task_data.get("task_id", md5),
            backend_name=self.name,
            status=DownloadStatus.PENDING,
            book_title=task_data.get("book_title", ""),
            author_name=task_data.get("author_name", ""),
            book_id=task_data.get("book_id", 0),
            filename=task_data.get("filename", ""),
            series_title=task_data.get("series_title", ""),
            local_dir=task_data.get("local_dir", self.download_dir),
            extra=task_data.get("extra", {"md5": md5}),
        )

        return self.get_status(task)
