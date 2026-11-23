"""Failure categories shared by search, transfers and the workflow."""
from enum import Enum
from .persistence import StatePersistenceError


class FailureKind(str, Enum):
    NO_MATCH = "no_match"
    SEARCH_INCOMPLETE = "search_incomplete"
    RATE_LIMITED = "rate_limited"
    SERVICE_UNAVAILABLE = "service_unavailable"
    ENQUEUE_UNKNOWN = "enqueue_unknown"
    PEER_REJECTED = "peer_rejected"
    TRANSFER_FAILED = "transfer_failed"
    TRANSFER_TIMEOUT = "transfer_timeout"
    LOCAL_FILE_INVALID = "local_file_invalid"
    LEGACY_UNVERIFIED = "legacy_unverified"


class SearchIncomplete(RuntimeError):
    """Search cannot justify a negative cache entry, even if another query was empty."""
    def __init__(self, message, kind=FailureKind.SEARCH_INCOMPLETE, retry_at=None):
        super().__init__(message)
        self.kind = kind.value if isinstance(kind, FailureKind) else str(kind)
        self.retry_at = retry_at


def http_status(error):
    return getattr(getattr(error, "response", None), "status_code", None)


def check_fatal_state(ctx):
    """Stop subsequent mutations after another worker loses durable recovery state."""
    error = getattr(ctx, "fatal_state_error", None)
    if error is not None:
        if isinstance(error, StatePersistenceError):
            raise error
        raise StatePersistenceError(f"A worker could not preserve recovery state: {error}")
