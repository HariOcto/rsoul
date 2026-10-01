"""One R:soul at a time per data folder.

An OS-level lock (flock) on <config dir>/.rsoul.lock, in Docker too. Unlike the old
"lock file exists" check, it can't go stale: the operating system releases it when the
process ends, even after a crash. Two containers sharing a data folder can't run at once,
on filesystems that support OS file locks (local disks and ZFS datasets do; some network
filesystems don't, and R:soul then runs without the lock and logs a warning).
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


class InstanceLock:
    def __init__(self, config_dir: str):
        self.path = os.path.join(config_dir, LOCK_FILENAME)
        self._file: Optional[IO] = None

    def acquire(self) -> None:
        if fcntl is None:
            logger.debug("File locking isn't available on this platform; running without an instance lock")
            return
        try:
            self._file = open(self.path, "a+")
        except OSError as e:
            logger.warning(f"Could not open lock file {self.path} ({e}); running without an instance lock")
            return
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._file.close()
            self._file = None
            raise AlreadyRunning(f"Another R:soul is already running with the data folder {os.path.dirname(self.path)}")
        except OSError as e:
            # Some network filesystems don't support locks; don't refuse to run because of that
            logger.warning(f"Could not lock {self.path} ({e}); running without an instance lock")
            self._file.close()
            self._file = None

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
