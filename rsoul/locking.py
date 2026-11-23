"""One R:soul at a time per data folder.

An OS-level lock (flock) on <config dir>/.rsoul.lock, in Docker too. Unlike the old
"lock file exists" check, it can't go stale: the operating system releases it when the
process ends, even after a crash. Two containers sharing a data folder can't run at once,
on filesystems that support OS file locks (local disks and ZFS datasets do; some network
filesystems don't, and R:soul then stops before making changes).
"""

import logging
import os
from typing import IO, Optional

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

logger = logging.getLogger(__name__)

LOCK_FILENAME = ".rsoul.lock"


class AlreadyRunning(Exception):
    """Another R:soul process holds the lock for this data folder."""


class LockUnavailable(RuntimeError):
    """Single-writer ownership could not be established safely."""


class InstanceLock:
    def __init__(self, config_dir: str):
        self.path = os.path.join(config_dir, LOCK_FILENAME)
        self._file: Optional[IO] = None

    def acquire(self) -> None:
        if fcntl is None:
            raise LockUnavailable("OS file locking is unavailable; use a supported Linux data volume")
        try:
            self._file = open(self.path, "a+")
        except OSError as e:
            raise LockUnavailable(f"Could not open instance lock {self.path}: {e}") from e
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._file.close()
            self._file = None
            raise AlreadyRunning(f"Another R:soul is already running with the data folder {os.path.dirname(self.path)}")
        except OSError as e:
            self._file.close()
            self._file = None
            raise LockUnavailable(f"Could not lock {self.path}: {e}; no changes were started") from e

    def release(self) -> None:
        """Never raises: a problem unlocking must not hide how the run ended."""
        if self._file is not None:
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            except OSError as e:
                logger.warning(f"Could not unlock {self.path}: {e}")
            finally:
                self._file.close()
                self._file = None
