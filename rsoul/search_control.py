"""Persisted pacing/backoff for the slskd endpoint, shared by books and runs."""
import hashlib
import json
import logging
from pathlib import Path
import random
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from . import health
from .outcomes import FailureKind, SearchIncomplete, check_fatal_state
from .persistence import atomic_write_json

logger = logging.getLogger(__name__)


def responsive_wait(ctx, seconds):
    """Keep owned transfers moving while searches/backoff wait."""
    check_fatal_state(ctx)
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        check_fatal_state(ctx)
        tick = getattr(getattr(ctx, "orchestrator", None), "poll_during_search", None)
        if callable(tick):
            tick()
        health.heartbeat()
        interval = min(remaining, 1.0)
        time.sleep(interval)
        remaining -= interval
    check_fatal_state(ctx)


class SearchControl:
    def __init__(self, ctx):
        self.ctx = ctx
        self.path = Path(getattr(ctx, "config_dir", ".")) / "slskd_search_backoff.json"
        endpoint = ctx.config.get("Slskd", "host_url", fallback="slskd")
        self.endpoint = hashlib.sha256(endpoint.rstrip("/").encode()).hexdigest()
        self._lock = threading.RLock()
        self.state = {"endpoint": self.endpoint, "next_search_at": 0.0, "rate_limit_until": 0.0, "rate_limit_count": 0}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("endpoint") == self.endpoint:
                self.state.update(data)
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as error:
            raise SearchIncomplete(f"Cannot read persisted search backoff: {error}", FailureKind.SERVICE_UNAVAILABLE) from error

    def _save(self):
        state = getattr(self.ctx, "state", None)
        if state is not None and not getattr(state, "read_only", False):
            atomic_write_json(self.path, self.state)

    def before_search(self):
        check_fatal_state(self.ctx)
        with self._lock:
            due = max(float(self.state.get("next_search_at", 0)), float(self.state.get("rate_limit_until", 0)))
            wait = max(0.0, due - time.time())
            maximum = self.ctx.config.getfloat("Search Settings", "search_rate_limit_max_wait", fallback=60)
            if wait > maximum:
                raise SearchIncomplete(f"slskd search backoff continues for {wait:.0f}s", FailureKind.RATE_LIMITED, due)
            if wait:
                responsive_wait(self.ctx, wait)
            interval = max(0.0, self.ctx.config.getfloat("Search Settings", "search_min_interval", fallback=1))
            self.state["next_search_at"] = time.time() + interval
            self._save()

    def success(self):
        with self._lock:
            self.state["rate_limit_count"] = 0
            self.state["rate_limit_until"] = 0.0
            self._save()

    def rate_limited(self, error):
        with self._lock:
            now = time.time()
            response = getattr(error, "response", None)
            header = getattr(response, "headers", {}).get("Retry-After", "")
            delay = None
            if header:
                try:
                    delay = max(0.0, float(header))
                except (TypeError, ValueError):
                    try:
                        moment = parsedate_to_datetime(header)
                        if moment.tzinfo is None:
                            moment = moment.replace(tzinfo=timezone.utc)
                        delay = max(0.0, moment.timestamp() - now)
                    except (TypeError, ValueError, OverflowError):
                        pass
            count = int(self.state.get("rate_limit_count", 0)) + 1
            self.state["rate_limit_count"] = count
            if delay is None:
                delay = min(300.0, 15.0 * 2 ** min(count - 1, 5))
            due = now + delay + random.uniform(0.0, min(3.0, delay * .1))
            self.state["rate_limit_until"] = max(float(self.state.get("rate_limit_until", 0)), due)
            self._save()
            return self.state["rate_limit_until"]
