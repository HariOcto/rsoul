"""A small status file for Docker's health check.

R:soul writes <config dir>/health.json when a run starts and ends, and about once a minute
while it searches, monitors downloads or waits for imports (the steps that can take long). `python -m rsoul.health` reports
the container as unhealthy when that file hasn't been updated for longer than a run
plus the wait between runs should take, which means R:soul is stuck or not running.
It only affects the status shown by Docker/TrueNAS; nothing is restarted.
"""

import json
import math
import os
import sys
import threading
import time
from typing import Any, Optional

STATUS_FILE = "health.json"
HEARTBEAT_EVERY = 60  # seconds between "still busy" updates

_lock = threading.Lock()
_last_beat = 0.0
_config_dir: Optional[str] = None


def configure(config_dir: str) -> None:
    """Remember where to write; called once at startup."""
    global _config_dir
    _config_dir = config_dir


def write_status(state: str, **details: Any) -> None:
    """Record the current state ("running", "idle", ...). Never raises."""
    global _last_beat
    if not _config_dir:
        return
    with _lock:
        path = os.path.join(_config_dir, STATUS_FILE)
        data = {}
        try:
            with open(path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data.update(details, state=state, updated_at=time.time())
        try:
            tmp = f"{path}.tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)
            _last_beat = time.time()
        except OSError:
            pass  # health reporting must never break a run


def heartbeat() -> None:
    """Show that a long-running step is still alive (at most once a minute)."""
    if time.time() - _last_beat >= HEARTBEAT_EVERY:
        write_status("running")


def _positive(value: str) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def max_age() -> float:
    """How old the status may be: a run plus the wait between runs, with margin.
    Invalid values fall back to the defaults instead of breaking the check."""
    override = _positive(os.environ.get("RSOUL_HEALTH_MAX_AGE", ""))
    if override:
        return override
    interval = _positive(os.environ.get("SCRIPT_INTERVAL", "300")) or 300.0
    return max(900.0, 2 * interval + 120)


def check(config_dir: str, now: Optional[float] = None) -> tuple:
    """(healthy, reason) for the status file in config_dir."""
    now = time.time() if now is None else now
    try:
        with open(os.path.join(config_dir, STATUS_FILE)) as f:
            data = json.load(f)
        updated = float(data["updated_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return False, "no status written yet"
    age = now - updated
    if age > max_age():
        return False, f"status is {age:.0f}s old (limit {max_age():.0f}s): R:soul looks stuck or isn't running"
    return True, f"{data.get('state', '?')}, updated {age:.0f}s ago"


def main() -> int:
    default_dir = "/data" if os.environ.get("IN_DOCKER") else os.getcwd()
    healthy, reason = check(os.environ.get("RSOUL_CONFIG_DIR", default_dir))
    print(("healthy: " if healthy else "unhealthy: ") + reason)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
