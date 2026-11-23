"""Retention must never remove files still named by durable recovery state."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from rsoul import postprocess
from rsoul.persistence import StatePersistenceError
from rsoul.state import StateManager


NOW = 2_000_000_000


def environment(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    config = tmp_path / "state"
    config.mkdir()
    state = StateManager(str(config))
    state.ensure_writable()
    return root, state


def folder(root, name="Old Book", marker=True):
    directory = root / "failed_imports" / name
    directory.mkdir(parents=True)
    (directory / "01.mp3").write_bytes(b"owned audiobook bytes")
    if marker:
        (directory / postprocess.QUARANTINE_MARKER).write_text(str(NOW - 40 * 86400))
    return directory


def save(state, extra, local_dir="peer"):
    state.add_task(SimpleNamespace(task_id="task", backend_name="slskd", book_id=7,
                                  book_title="Die Flüsse von London", author_name="Ben Aaronovitch",
                                  filename="01.mp3", series_title="", local_dir=local_dir, extra=extra))
    reloaded = StateManager(str(state.filepath.parent))
    assert reloaded.load_error is None
    return reloaded


@pytest.mark.parametrize("name", ["Die Flüsse von London", 'The \"Quoted\" Book', r"A literal\backslash"])
def test_real_saved_state_protects_unicode_and_escaped_folder_names(tmp_path, name):
    root, state = environment(tmp_path)
    directory = folder(root, name)
    state = save(state, {"import_pending": {"local_dir": str(directory), "phase": "needs_review",
                                           "files": [str(directory / "01.mp3")]}})
    assert postprocess.prune_quarantine(str(root), 30, state, NOW) == 0
    assert (directory / "01.mp3").read_bytes() == b"owned audiobook bytes"


@pytest.mark.parametrize("reference", [
    "failed_imports/Old Book/01.mp3",
    r"failed_imports\Old Book\01.mp3",
    "failed_imports/./Old Book/../Old Book/01.mp3",
    r"D:\downloads\failed_imports\Old Book\01.mp3",
])
def test_recovery_file_paths_protect_the_containing_folder(tmp_path, reference):
    root, state = environment(tmp_path)
    directory = folder(root)
    state = save(state, {"operator_discard": {"files": [{"source": "peer/01.mp3", "destination": reference}]}})
    assert postprocess.prune_quarantine(str(root), 30, state, NOW) == 0
    assert directory.exists()


def test_native_paths_and_component_boundaries_do_not_keep_unowned_prefixes(tmp_path):
    root, state = environment(tmp_path)
    old = folder(root, "Book")
    owned = folder(root, "Book Extra")
    state = save(state, {"import_pending": {"local_dir": str(owned), "download_root": str(root),
                                           "server_root": "/remote", "source_dir": str(root)}})
    assert postprocess.prune_quarantine(str(root), 30, state, NOW) == 1
    assert not old.exists() and owned.exists()


def test_known_download_root_is_not_an_ownership_claim_for_every_quarantine(tmp_path):
    root, state = environment(tmp_path)
    old = folder(root)
    state = save(state, {"import_pending": {"local_dir": str(root / "rsoul_audiobooks" / "Book"),
                                           "download_root": str(root), "source_dir": str(root)}})
    assert postprocess.prune_quarantine(str(root), 30, state, NOW) == 1
    assert not old.exists()


def test_live_inventory_is_checked_again_before_deleting(tmp_path):
    root, _ = environment(tmp_path)
    old = folder(root)
    snapshots = iter([[], [{"extra": {"discard": {"destination": str(old)}}}]])
    state = SimpleNamespace(read_only=False, load_error=None, get_tasks_for_orchestrator=lambda: next(snapshots))
    assert postprocess.prune_quarantine(str(root), 30, state, NOW) == 0
    assert old.exists()


@pytest.mark.parametrize("problem", ["missing", "unreadable", "read_only", "load_error", "malformed", "missing_file"])
def test_untrustworthy_state_never_allows_deletion(tmp_path, problem):
    root, state = environment(tmp_path)
    old = folder(root)
    if problem == "missing":
        state = None
    elif problem == "unreadable":
        def fail():
            raise OSError("state unavailable")
        state = SimpleNamespace(get_tasks_for_orchestrator=fail)
    elif problem == "read_only":
        state.read_only = True
    elif problem == "load_error":
        state.load_error = "damaged state"
    elif problem == "malformed":
        state = SimpleNamespace(get_tasks_for_orchestrator=lambda: [{"local_dir": 123}])
    else:
        state.filepath.unlink()
    assert postprocess.prune_quarantine(str(root), 30, state, NOW) == 0
    assert old.exists()


def test_legacy_state_paths_are_protected_before_task_migration(tmp_path):
    root, state = environment(tmp_path)
    old = folder(root)
    state.add_item({"dir": str(old), "filename": "01.mp3", "title": "Old Book"})
    assert not state.get_tasks_for_orchestrator()
    assert postprocess.prune_quarantine(str(root), 30, state, NOW) == 0
    assert old.exists()


def test_marker_clock_and_default_off_are_preserved(tmp_path, monkeypatch):
    root, state = environment(tmp_path)
    old = folder(root)
    legacy = folder(root, "Legacy", marker=False)
    os.utime(legacy, (0, 0))
    monkeypatch.setattr(postprocess.time, "time", lambda: NOW)
    assert postprocess.prune_quarantine(str(root), 0, state, NOW) == 0
    assert old.exists()
    assert postprocess.prune_quarantine(str(root), 30, state, NOW) == 1
    assert not old.exists() and legacy.exists()
    assert float((legacy / postprocess.QUARANTINE_MARKER).read_text()) == NOW


def test_symlink_folder_and_marker_are_not_followed(tmp_path):
    root, state = environment(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker"
    marker.write_text("0")
    target = folder(root)
    (target / postprocess.QUARANTINE_MARKER).unlink()
    (target / postprocess.QUARANTINE_MARKER).symlink_to(marker)
    (root / "failed_imports" / "alias").symlink_to(outside, target_is_directory=True)
    assert postprocess.prune_quarantine(str(root), 30, state, NOW) == 0
    assert target.exists() and outside.exists() and marker.read_text() == "0"


def test_fatal_guard_prevents_retention_mutation(tmp_path):
    root, state = environment(tmp_path)
    old = folder(root)
    def guard():
        raise StatePersistenceError("another worker lost its checkpoint")
    with pytest.raises(StatePersistenceError, match="lost its checkpoint"):
        postprocess.prune_quarantine(str(root), 30, state, NOW, before_mutation=guard)
    assert old.exists()
