"""Durable local checkpoints used before remote or filesystem mutations."""

import json
import os
from pathlib import Path
import tempfile


class StatePersistenceError(RuntimeError):
    """A recovery checkpoint could not be committed; further mutations must stop."""


def atomic_write_json(path, value):
    """Write, fsync, rename, and fsync the containing directory.

    Callers own thread/process synchronization. A unique temporary file prevents a
    failed writer from removing another writer's checkpoint. Never create an absent
    data mount: that commonly means the configured persistent volume is unavailable.
    """
    path = Path(path)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        fsync_directory(path.parent)
    except Exception as exc:
        raise StatePersistenceError(f"Could not durably save {path}: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def fsync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
