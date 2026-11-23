"""Optional, best-effort webhook notifications when something needs the operator.

Off unless [Notifications] webhook_url is set. Events are needs_review, partial,
submission_unknown and persistence_error. A successful send() means queued, not delivered.
One daemon worker and a bounded queue keep an unresponsive webhook off the transaction path.
HTTP uses a three-second socket inactivity timeout, reads no response body, and never retries
or follows redirects. Shutdown gives all outstanding notifications one shared second, then
discards queued work; a stuck request cannot delay process exit.

The JSON carries "content" (Discord), "text" (Slack), and "body"/"title" (Apprise and similar).
"""

import logging
import threading
import time
from collections import deque
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

EVENTS = ("needs_review", "partial", "submission_unknown", "persistence_error")
DEFAULT_EVENTS = "needs_review,persistence_error"
TIMEOUT = 3
SHUTDOWN_TIMEOUT = 1
QUEUE_SIZE = 32
MAX_URL_LENGTH = 8192
MAX_TITLE_LENGTH = 200
MAX_MESSAGE_LENGTH = 4000

_config: Optional[Any] = None
_http_context = threading.local()


class _WebhookHTTPFilter(logging.Filter):
    """Hide HTTP-library details only on this webhook worker, never other app traffic.

    urllib3's DEBUG request lines include credentials embedded in webhook URL paths. Its
    warnings can include the same URL on connection failures. The delivery result below is
    still logged, without URLs or exception messages.
    """

    def filter(self, record):
        return not getattr(_http_context, "sending", False)


_http_filter = _WebhookHTTPFilter()


def _protect_http_logging():
    # requests has already imported its HTTP stack. Attach to concrete loggers because a
    # filter on their parent alone does not filter records propagated from child loggers.
    prefixes = ("urllib3", "requests")
    names = set(logging.root.manager.loggerDict)
    names.update(("urllib3.connectionpool", "urllib3.util.retry",
                  "requests.packages.urllib3.connectionpool", "requests.packages.urllib3.util.retry"))
    for name in names:
        if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
            logging.getLogger(name).addFilter(_http_filter)


class _WebhookSession(requests.Session):
    def get_redirect_target(self, response):
        # Requests may consume a redirect body to prepare Response.next even when
        # allow_redirects=False. Webhooks need only the status, never a redirect request.
        return None


def _post_headers_only(url, **kwargs):
    with _WebhookSession() as session:
        return session.post(url, **kwargs)


class _Dispatcher:
    """A reusable single-worker queue; even reconfiguration cannot accumulate hung workers."""

    def __init__(self, transport=None, capacity=QUEUE_SIZE):
        self.transport = transport
        self.capacity = capacity
        self._condition = threading.Condition()
        self._queue = deque()
        self._thread = None
        self._active = False
        self._accepting = True
        self._stopping = False

    def configure(self, enabled):
        with self._condition:
            # A previous configuration's queued messages must not escape after a switch.
            # An already active delivery owns its captured URL and transport until it ends.
            self._queue.clear()
            self._accepting = enabled
            self._stopping = not enabled
            self._condition.notify_all()

    def submit(self, url, payload, event):
        with self._condition:
            if not self._accepting or len(self._queue) >= self.capacity:
                return False
            # Capture the callable too, so tests/config changes cannot redirect queued work.
            self._queue.append((url, payload, event, self.transport or _post_headers_only))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="rsoul-notifications", daemon=True)
                try:
                    self._thread.start()
                except Exception:
                    self._thread = None
                    self._queue.clear()
                    self._condition.notify_all()
                    raise
            self._condition.notify_all()
            return True

    def _run(self):
        while True:
            with self._condition:
                while not self._queue and not self._stopping:
                    self._condition.wait()
                if not self._queue:
                    self._thread = None
                    self._condition.notify_all()
                    return
                notice = self._queue.popleft()
                self._active = True
            try:
                self._deliver(*notice)
            finally:
                with self._condition:
                    self._active = False
                    self._condition.notify_all()

    @staticmethod
    def _deliver(url, payload, event, transport):
        response = None
        _http_context.sending = True
        try:
            response = transport(url, json=payload, timeout=TIMEOUT, stream=True, allow_redirects=False)
            if not 200 <= response.status_code < 300:
                logger.warning("Notification for %s was refused by the webhook (HTTP %s)", event, response.status_code)
                return
            logger.debug("Notification for %s was delivered (HTTP %s)", event, response.status_code)
        except Exception as exc:
            logger.warning("Notification for %s could not be delivered: %s", event, type(exc).__name__)
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception as exc:
                logger.warning("Notification connection could not be closed: %s", type(exc).__name__)
            _http_context.sending = False

    def flush(self, timeout):
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            while self._queue or self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def shutdown(self, timeout):
        with self._condition:
            self._accepting = False
        finished = self.flush(timeout)
        with self._condition:
            self._queue.clear()
            self._stopping = True
            self._condition.notify_all()
        return finished


_dispatcher = _Dispatcher()


def configure(config: Any) -> None:
    """Set startup configuration and discard queued work from any previous configuration."""
    global _config
    _config = config
    _protect_http_logging()
    _dispatcher.configure(config is not None)


def _enabled(event: str) -> Optional[str]:
    if _config is None or event not in EVENTS:
        return None
    url = _config.get("Notifications", "webhook_url", fallback="").strip()
    if not url:
        return None
    if len(url) > MAX_URL_LENGTH:
        logger.warning("Notification webhook URL is too long; notification skipped")
        return None
    wanted = {e.strip().lower() for e in _config.get("Notifications", "events", fallback=DEFAULT_EVENTS).split(",") if e.strip()}
    return url if event in wanted else None


def send(event: str, title: str, message: str) -> bool:
    """Queue a bounded notice without waiting; True means accepted by the queue only.

    Delivery failures are logged by the worker. Disabled events, a full queue, and shutdown
    return False. Notifications are best effort and are never automatically retried.
    """
    try:
        url = _enabled(event)
        if not url:
            return False
        title = str(title)[:MAX_TITLE_LENGTH]
        message = str(message)[:MAX_MESSAGE_LENGTH]
        text = f"[R:soul | {event}] {title}: {message}"
        payload = {"content": text[:1900], "text": text, "title": f"R:soul: {title}", "body": message, "event": event}
        if _dispatcher.submit(url, payload, event):
            return True
        logger.warning("Notification for %s was not queued: delivery queue full or shutting down", event)
    except Exception as exc:
        logger.warning("Notification for %s could not be queued: %s", event, type(exc).__name__)
    return False


def flush(timeout=SHUTDOWN_TIMEOUT) -> bool:
    """Wait at most timeout seconds for queued delivery attempts, not proof of success."""
    return _dispatcher.flush(timeout)


def shutdown(timeout=SHUTDOWN_TIMEOUT) -> bool:
    """Give all notices one shared bounded grace period, then discard pending deliveries."""
    return _dispatcher.shutdown(timeout)


def phase_entered(pending: dict, previous_phase: Any, reason: str) -> None:
    """Announce an attention phase; the transaction records unresolved-attempt deduplication."""
    phase = pending.get("phase")
    if phase in ("needs_review", "partial", "submission_unknown") and phase != previous_phase:
        identity = pending.get("intended_identity")
        identity = identity if isinstance(identity, dict) else {}
        label = " - ".join(str(x) for x in (identity.get("author_name"), identity.get("title")) if x) or "unknown title"
        send(phase, f"Book {pending.get('book_id')} ({label})", reason)
