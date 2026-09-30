"""
Base classes and interfaces for download backends.

All download backends (slskd, libgen, etc.) must implement the DownloadBackend ABC.
This provides a consistent interface for the orchestrator to use.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional, Any, Dict


class DownloadStatus(Enum):
    """Normalized download status across all backends."""

    PENDING = "pending"
    QUEUED = "queued"  # Waiting in the remote peer's upload queue
    QUEUED_LOCALLY = "queued_locally"  # Waiting for a free download slot on our own side
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    NOT_FOUND = "not_found"
    CANCELLED = "cancelled"


@dataclass
class SearchResult:
    """Normalized search result from any backend.

    Backends transform their native results into this format.
    """

    title: str
    author: str
    filename: str
    size_bytes: int
    extension: str
    backend_name: str
    source_id: str  # Backend-specific ID to retrieve/download this result

    # Optional fields for ranking/display
    score: float = 0.0
    username: str = ""  # For P2P backends like slskd

    # Backend-specific data that shouldn't be parsed by orchestrator
    # but may be needed by the backend for download
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DownloadTask:
    """Normalized download task from any backend.

    Represents an active or completed download.
    """

    task_id: str
    backend_name: str
    status: DownloadStatus

    # Book metadata (for state persistence and import)
    book_title: str
    author_name: str
    book_id: int

    # File info
    filename: str

    # Fields with defaults must come after required fields
    series_title: str = ""
    local_dir: str = ""  # Directory where file will be/is saved
    output_path: Optional[Path] = None
    error_message: Optional[str] = None
    progress_percent: float = 0.0
    bytes_transferred: int = 0  # Across all files in the task; used for stall detection

    # Backend-specific data
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DownloadTarget:
    """Book to be downloaded, sourced from Readarr."""

    book_id: int
    book_title: str
    author_name: str
    series_title: str
    allowed_filetypes: List[str]

    # Original Readarr data for import
    readarr_book: Dict[str, Any] = field(default_factory=dict)
    readarr_author: Dict[str, Any] = field(default_factory=dict)

    # Editions from Readarr (contains ISBNs, ASINs, etc.)
    editions: List[Dict[str, Any]] = field(default_factory=list)

    # "ebook" (single file) or "audiobook" (whole folder of audio files)
    media_type: str = "ebook"

    @property
    def is_audiobook(self) -> bool:
        return self.media_type == "audiobook"

    def get_isbn13s(self) -> List[str]:
        """Extract all ISBN-13s from editions.

        Returns:
            List of ISBN-13 strings (13 digits), deduplicated.
        """
        isbns = set()
        for edition in self.editions:
            isbn = edition.get("isbn13")
            if isbn and len(str(isbn)) == 13:
                isbns.add(str(isbn))
        return list(isbns)

    def get_primary_isbn13(self) -> Optional[str]:
        """Get the ISBN-13 from the monitored edition, or first available.

        Returns:
            ISBN-13 string or None if not available.
        """
        # Prefer monitored edition
        for edition in self.editions:
            if edition.get("monitored"):
                isbn = edition.get("isbn13")
                if isbn and len(str(isbn)) == 13:
                    return str(isbn)

        # Fall back to any edition with ISBN-13
        for edition in self.editions:
            isbn = edition.get("isbn13")
            if isbn and len(str(isbn)) == 13:
                return str(isbn)

        return None

    def get_asins(self) -> List[str]:
        """Extract all ASINs from editions.

        Returns:
            List of ASIN strings, deduplicated.
        """
        asins = set()
        for edition in self.editions:
            asin = edition.get("asin")
            if asin:
                asins.add(str(asin))
        return list(asins)


class DownloadBackend(ABC):
    """Abstract base class that all download backends must implement.

    Each backend handles:
    - Searching for books
    - Initiating downloads
    - Monitoring download progress
    - Its own matching logic (with shared helpers available)

    The orchestrator calls these methods and handles fallback between backends.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique backend identifier (e.g., 'slskd', 'libgen').

        Used in config, logging, and state persistence.
        """
        pass

    @property
    def priority(self) -> int:
        """Priority for backend selection. Lower = tried first.

        Default is 100. Override to change.
        """
        return 100

    @property
    def download_dir(self) -> str:
        """Base directory where this backend saves files.

        Used by postprocessing to locate downloaded files locally.
        """
        return ""

    @property
    def readarr_download_dir(self) -> str:
        """Directory path as seen by Readarr.

        Used when telling Readarr where to import files from.
        Useful for Docker path mappings. Defaults to download_dir if not set.
        """
        return self.download_dir

    @abstractmethod
    def is_available(self) -> bool:
        """Check if backend is configured and reachable.

        Called before attempting to use the backend.
        Should check config, connectivity, API health, etc.
        """
        pass

    @abstractmethod
    def search(self, target: DownloadTarget) -> List[SearchResult]:
        """Search for a book.

        Each backend implements its own search and matching logic.
        Shared matching helpers are available in rsoul.utils.

        Args:
            target: Book to search for

        Returns:
            List of matching results, sorted by quality/preference.
            Empty list if nothing found.
        """
        pass

    @abstractmethod
    def download(self, target: DownloadTarget, result: SearchResult) -> Optional[DownloadTask]:
        """Initiate download of a search result.

        Args:
            target: Original book target (for metadata)
            result: Search result to download

        Returns:
            DownloadTask for monitoring, or None if failed to start.
        """
        pass

    @abstractmethod
    def get_status(self, task: DownloadTask) -> DownloadTask:
        """Poll download status.

        Updates and returns the task with current status.

        Args:
            task: Task to check

        Returns:
            Updated task with current status
        """
        pass

    @abstractmethod
    def cancel(self, task: DownloadTask) -> bool:
        """Cancel and cleanup a download.

        Args:
            task: Task to cancel

        Returns:
            True if successfully cancelled
        """
        pass

    def cleanup(self, task: DownloadTask) -> None:
        """Optional cleanup after successful import.

        Override if backend needs post-import cleanup
        (e.g., removing from transfer list).
        """
        pass

    def reconcile_task(self, task_data: Dict[str, Any]) -> Optional[DownloadTask]:
        """Reconcile a persisted task with current backend state.

        Called on resume to match saved state with live backend state.
        IDs may have changed (e.g., slskd restart).

        Args:
            task_data: Persisted task data from state file

        Returns:
            Reconciled DownloadTask, or None if task is lost/unrecoverable
        """
        return None
