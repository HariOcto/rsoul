"""Recovery must prove command scope and current ownership before moving anything.

These exercise the actual CLI entry point, durable state and filesystem. Chaptarr
responses and explicit filesystem/checkpoint failures are the only test doubles.
"""

import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rsoul import import_transaction, postprocess, recover
from rsoul.persistence import StatePersistenceError
from rsoul.state import StateManager
from test_v11_imports import setup
from test_v11_recover import CommandServer, STAGE, legacy, pending, run


def discard(ctx, server, tmp_path):
    out = io.StringIO()
    code = recover.main(["--config-dir", str(tmp_path), "--book", "42", "--discard"],
                        client=server, state=ctx.state, out=out,
                        local_root=ctx.config["Slskd"]["download_dir"],
                        server_root=ctx.config["Slskd"]["readarr_download_dir"])
    return code, out.getvalue()


def save(ctx, record):
    task = next(task for task in ctx.state.get_tasks_for_orchestrator() if task["task_id"] == "task-42")
    ctx.state.set_extra(task["task_id"], dict(task["extra"], import_pending=record))


def fresh(tmp_path, staged=False, server_root="/remote"):
    ctx, item, _, _ = setup(tmp_path)
    root = tmp_path / "downloads"
    server = CommandServer(root)
    ctx.readarr = server
    ctx.config["Slskd"]["readarr_download_dir"] = server_root
    record = import_transaction.new_transaction(item, str(root), server_root, "targeted")
    save(ctx, record)
    if staged:
        record.update(phase="staging", inputs_validated=True)
        import_transaction.recover_staging(record, lambda value: save(ctx, value))
        record.update(phase="needs_review", reason="Operator is reviewing this recording")
        save(ctx, record)
    return ctx, server, root, record


def assert_blocked_unchanged(ctx, server, tmp_path, text_fragment):
    before = ctx.state.filepath.read_bytes()
    code, text = discard(ctx, server, tmp_path)
    assert code == 1 and text_fragment in text and "Nothing changed" in text
    assert ctx.state.filepath.read_bytes() == before
    assert not (tmp_path / "recover_archive.jsonl").exists()
    return text


@pytest.mark.parametrize("name", [None, "", "  ", "---", 17, 17.2, True, False, {},
                                  {"value": "DownloadedBooksScan"}, ["ManualImport"]])
def test_unreadable_command_names_never_authorize_recovery(tmp_path, name):
    ctx, server, _ = legacy(tmp_path)
    server.command_list = [{"id": 801, "name": name, "status": "queued", "body": {"path": "/remote"}}]
    before = ctx.state.filepath.read_bytes()
    code, text = run(ctx, server, "--book", "42", "--apply")
    assert code == 1 and "no readable name" in text
    assert ctx.state.filepath.read_bytes() == before


@pytest.mark.parametrize("path", [None, 13, "", "relative/file.wav", "\\Downloads\\file.wav",
                                  "C:file.wav", "/unrelated/\x00file.wav", "C:\\Downloads\\\x00file.wav"])
def test_every_manual_import_path_must_be_usable(tmp_path, path):
    ctx, server, _ = legacy(tmp_path)
    server.command_list = [{"id": 802, "name": "ManualImport", "status": "queued",
                            "body": {"files": [{"path": "/unrelated/good.wav"}, {"path": path}]}}]
    before = ctx.state.filepath.read_bytes()
    assert run(ctx, server, "--book", "42", "--apply")[0] == 1
    assert ctx.state.filepath.read_bytes() == before


@pytest.mark.parametrize("server_root, scan_path", [
    ("/remote", "/"), ("/remote", "/remote/"),
    ("C:\\Media\\Downloads", "C:\\"), ("C:\\Media\\Downloads", "c:\\"),
    ("C:\\Media\\Downloads", "c:/MEDIA/downloads/rsoul_audiobooks"),
    ("\\\\NAS\\Audio\\Downloads", "\\\\nas\\audio"),
])
def test_recursive_scans_match_posix_drive_and_unc_roots(tmp_path, server_root, scan_path):
    ctx, server, _ = legacy(tmp_path)
    ctx.config["Slskd"]["readarr_download_dir"] = server_root
    record = pending(ctx)
    record["readarr_path"] = server_root.rstrip("/\\") + "/" + STAGE
    save(ctx, record)
    server.command_list = [{"id": 803, "name": "DownloadedBooksScan", "status": "started", "body": {"path": scan_path}}]
    before = ctx.state.filepath.read_bytes()
    assert run(ctx, server, "--book", "42", "--apply")[0] == 1
    assert ctx.state.filepath.read_bytes() == before


def test_unc_manual_import_compares_complete_paths_case_insensitively(tmp_path):
    ctx, server, _ = legacy(tmp_path)
    ctx.config["Slskd"]["readarr_download_dir"] = "\\\\NAS\\Audio\\Downloads"
    record = pending(ctx)
    record["readarr_path"] = "\\\\NAS\\Audio\\Downloads/" + STAGE
    save(ctx, record)
    server.command_list = [{"id": 804, "name": "ManualImport", "status": "queued", "body": {
        "files": [{"path": "\\\\nas\\audio\\downloads/" + STAGE + "/chapter0.wav"}]}}]
    assert run(ctx, server, "--book", "42", "--apply")[0] == 1


@pytest.mark.parametrize("name, body", [
    ("ManualImport", {"path": None, "files": [{"path": "/unrelated/good.wav"}]}),
    ("DownloadedBooksScan", {"path": "/unrelated", "files": [{}]}),
])
def test_only_the_commands_actual_operation_targets_determine_scope(tmp_path, name, body):
    ctx, server, _ = legacy(tmp_path)
    server.command_list = [{"id": 805, "name": name, "status": "started", "body": body}]
    assert run(ctx, server, "--book", "42", "--apply")[0] == 0


@pytest.mark.parametrize("command", ["ManualImport", "DownloadedBooksScan"])
@pytest.mark.parametrize("mapping", ["/saved", "/current"])
def test_discard_checks_actual_sources_under_saved_and_current_mappings(tmp_path, command, mapping):
    ctx, server, root, record = fresh(tmp_path, server_root="/saved")
    ctx.config["Slskd"]["readarr_download_dir"] = "/current"
    body = {"files": [{"path": mapping + "/peer-folder/chapter0.wav"}]} if command == "ManualImport" else {"path": mapping + "/peer-folder"}
    server.command_list = [{"id": 806, "name": command, "status": "started", "body": body}]
    assert_blocked_unchanged(ctx, server, tmp_path, "still started")
    assert all(Path(entry["source_path"]).exists() for entry in record["manifest"])


def test_manual_import_of_another_file_in_shared_source_folder_does_not_block(tmp_path):
    ctx, server, _, _ = fresh(tmp_path)
    server.command_list = [{"id": 807, "name": "ManualImport", "status": "started",
                            "body": {"files": [{"path": "/remote/peer-folder/unrelated.wav"}]}}]
    assert discard(ctx, server, tmp_path)[0] == 0


@pytest.mark.parametrize("damage", ["missing_saved_root", "missing_both_roots", "conflicting_stage_path", "relative_current_root"])
def test_discard_requires_resolved_consistent_mappings(tmp_path, damage):
    ctx, server, _, record = fresh(tmp_path)
    if damage == "missing_saved_root":
        record.pop("server_root")
    elif damage == "missing_both_roots":
        record.pop("server_root")
        record.pop("download_root")
    elif damage == "conflicting_stage_path":
        record["manifest"][0]["readarr_path"] = "/other/chapter0.wav"
    else:
        ctx.config["Slskd"]["readarr_download_dir"] = "relative"
    save(ctx, record)
    assert_blocked_unchanged(ctx, server, tmp_path, "BLOCKS DISCARD")


@pytest.mark.parametrize("other_task", [False, True])
def test_discard_never_claims_a_reappeared_source_path(tmp_path, other_task):
    ctx, server, _, record = fresh(tmp_path, staged=True)
    source = Path(record["manifest"][0]["source_path"])
    source.write_bytes(b"Another release now owns this old pathname")
    if other_task:
        ctx.state.add_task(SimpleNamespace(task_id="task-99", book_id=99, book_title="Other Work", author_name="Other Author",
                                          filename=source.name, backend_name="slskd", local_dir="peer-folder", series_title="",
                                          extra={"expected_files": [source.name]}))
    assert_blocked_unchanged(ctx, server, tmp_path, "Both staged and source files exist")
    assert source.read_bytes() == b"Another release now owns this old pathname"
    assert all(Path(entry["stage_path"]).exists() for entry in record["manifest"])


def test_discard_refuses_a_source_reserved_by_another_task(tmp_path):
    ctx, server, _, record = fresh(tmp_path)
    ctx.state.add_task(SimpleNamespace(task_id="task-99", book_id=99, book_title="Other Work", author_name="Other Author",
                                      filename="chapter0.wav", backend_name="slskd", local_dir="peer-folder", series_title="",
                                      extra={"expected_files": ["chapter0.wav"]}))
    assert_blocked_unchanged(ctx, server, tmp_path, "also claimed by saved task task-99")
    assert Path(record["manifest"][0]["source_path"]).exists()


def test_discard_does_not_fall_back_to_a_source_after_submission(tmp_path):
    ctx, server, _, record = fresh(tmp_path, staged=True)
    for entry in record["manifest"]:
        Path(entry["stage_path"]).rename(entry["source_path"])
    record.update(phase="submission_unknown", attempts=[{"state": "unknown"}], submits=1)
    save(ctx, record)
    assert_blocked_unchanged(ctx, server, tmp_path, "not proven to be an unsubmitted manifest input")


def test_discard_does_not_reclaim_a_source_after_staging_started(tmp_path):
    ctx, server, _, record = fresh(tmp_path, staged=True)
    for entry in record["manifest"]:
        Path(entry["stage_path"]).rename(entry["source_path"])
    assert record["inputs_validated"] and not record["attempts"]
    assert_blocked_unchanged(ctx, server, tmp_path, "not proven to be an unsubmitted manifest input")


def test_discard_allows_damaged_uniquely_owned_staged_files(tmp_path):
    ctx, server, root, record = fresh(tmp_path, staged=True)
    broken = Path(record["manifest"][0]["stage_path"])
    broken.write_bytes(b"damaged recording")
    code, text = discard(ctx, server, tmp_path)
    assert code == 0 and "Discarded" in text and ctx.state.get_tasks_for_orchestrator() == []
    assert not broken.exists()
    assert any(path.read_bytes() == b"damaged recording" for path in (root / "failed_imports").rglob("*.wav"))
    archive = [json.loads(line) for line in (tmp_path / "recover_archive.jsonl").read_text().splitlines()]
    assert [row["phase"] for row in archive] == ["prepared", "complete"]
    assert archive[0]["task"]["extra"]["import_pending"] == record
    assert all(Path(file["destination"]).is_file() for file in archive[-1]["files"])


def test_discard_refuses_changed_shared_source_bytes(tmp_path):
    ctx, server, _, record = fresh(tmp_path)
    Path(record["manifest"][0]["source_path"]).write_bytes(b"different download")
    assert_blocked_unchanged(ctx, server, tmp_path, "not proven to be an unsubmitted manifest input")


def test_discard_reconciles_pending_files_with_manifest(tmp_path):
    ctx, server, _, record = fresh(tmp_path, staged=True)
    record["files"] = record["files"][:1]
    save(ctx, record)
    assert_blocked_unchanged(ctx, server, tmp_path, "does not match the complete import manifest")


@pytest.mark.parametrize("problem", ["symlink", "unreadable", "missing_mount"])
def test_unreadable_local_evidence_cannot_be_treated_as_absent(tmp_path, monkeypatch, problem):
    ctx, server, root, record = fresh(tmp_path)
    source = Path(record["manifest"][0]["source_path"])
    if problem == "symlink":
        original = source.with_suffix(".original")
        source.rename(original)
        source.symlink_to(original)
    elif problem == "unreadable":
        original_lstat = recover.os.lstat
        def lstat(path, *args, **kwargs):
            if str(path) == str(source):
                raise PermissionError("test: source cannot be read")
            return original_lstat(path, *args, **kwargs)
        monkeypatch.setattr(recover.os, "lstat", lstat)
    else:
        root.rename(tmp_path / "unmounted-downloads")
    assert_blocked_unchanged(ctx, server, tmp_path, "BLOCKS DISCARD")


def interrupt_second_move(ctx, server, tmp_path, monkeypatch):
    real_move = postprocess.shutil.move
    calls = []
    def move(source, destination, *args, **kwargs):
        calls.append(source)
        if len(calls) == 2:
            raise OSError("test: second file move failed")
        return real_move(source, destination, *args, **kwargs)
    monkeypatch.setattr(postprocess.shutil, "move", move)
    code, text = discard(ctx, server, tmp_path)
    assert code == 1 and "Discard stopped" in text and "Nothing changed" not in text
    task = ctx.state.get_tasks_for_orchestrator()[0]
    journal = task["extra"]["operator_discard"]
    assert task["extra"]["import_pending"]["operator_discard"] == journal["operation_id"]
    assert Path(journal["files"][0]["destination"]).is_file()
    assert Path(journal["files"][1]["source"]).is_file()
    monkeypatch.setattr(postprocess.shutil, "move", real_move)
    return journal


def test_partial_discard_retries_the_same_durable_destinations(tmp_path, monkeypatch):
    ctx, server, root = legacy(tmp_path)
    journal = interrupt_second_move(ctx, server, tmp_path, monkeypatch)
    ctx.state = StateManager(str(ctx.state.filepath.parent))
    assert discard(ctx, server, tmp_path)[0] == 0
    assert ctx.state.get_tasks_for_orchestrator() == []
    assert all(Path(file["destination"]).is_file() and not Path(file["source"]).exists() for file in journal["files"])
    assert len(list((root / "failed_imports").iterdir())) == 1


def test_noop_move_after_a_transient_missing_file_does_not_release_ownership(tmp_path, monkeypatch):
    ctx, server, _ = legacy(tmp_path)
    first = pending(ctx)["files"][0]
    real_aside = postprocess.move_files_aside
    real_lstat = postprocess.os.lstat
    def transient_missing(*args, **kwargs):
        observed_missing = []
        def lstat(path, *stat_args, **stat_kwargs):
            if str(path) == first and not observed_missing:
                observed_missing.append(True)
                raise FileNotFoundError("test: transient filesystem observation")
            return real_lstat(path, *stat_args, **stat_kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(postprocess.os, "lstat", lstat)
            return real_aside(*args, **kwargs)
    monkeypatch.setattr(postprocess, "move_files_aside", transient_missing)
    code, text = discard(ctx, server, tmp_path)
    assert code == 1 and "No complete move" in text
    assert len(ctx.state.get_tasks_for_orchestrator()) == 1 and Path(first).is_file()
    journal = ctx.state.get_tasks_for_orchestrator()[0]["extra"]["operator_discard"]
    assert journal["moved"] == 0
    monkeypatch.setattr(postprocess, "move_files_aside", real_aside)
    assert discard(ctx, server, tmp_path)[0] == 0


def test_retry_rearchives_the_original_task_if_the_old_archive_was_lost(tmp_path, monkeypatch):
    ctx, server, _ = legacy(tmp_path)
    original = copy.deepcopy(pending(ctx))
    interrupt_second_move(ctx, server, tmp_path, monkeypatch)
    archive_path = tmp_path / "recover_archive.jsonl"
    archive_path.unlink()
    assert discard(ctx, server, tmp_path)[0] == 0
    archive = [json.loads(line) for line in archive_path.read_text().splitlines()]
    assert archive[0]["resumed"] is True
    assert archive[0]["task"]["extra"]["import_pending"] == original
    assert archive[-1]["phase"] == "complete"


def test_apply_cannot_replace_an_interrupted_discard(tmp_path, monkeypatch):
    ctx, server, _ = legacy(tmp_path)
    interrupt_second_move(ctx, server, tmp_path, monkeypatch)
    before = ctx.state.filepath.read_bytes()
    code, text = run(ctx, server, "--book", "42", "--apply")
    assert code == 1 and "operator discard in progress" in text and "--discard" in text
    assert ctx.state.filepath.read_bytes() == before


def test_crash_after_move_before_checkpoint_resumes_from_exact_destination(tmp_path, monkeypatch):
    ctx, server, _ = legacy(tmp_path)
    real_move = postprocess.shutil.move
    def crash_after_move(source, destination, *args, **kwargs):
        real_move(source, destination, *args, **kwargs)
        raise KeyboardInterrupt("test: process stopped after the rename")
    monkeypatch.setattr(postprocess.shutil, "move", crash_after_move)
    with pytest.raises(KeyboardInterrupt):
        discard(ctx, server, tmp_path)
    ctx.state = StateManager(str(ctx.state.filepath.parent))
    journal = ctx.state.get_tasks_for_orchestrator()[0]["extra"]["operator_discard"]
    assert journal["phase"] == "prepared" and Path(journal["files"][0]["destination"]).is_file()
    monkeypatch.setattr(postprocess.shutil, "move", real_move)
    assert discard(ctx, server, tmp_path)[0] == 0


@pytest.mark.parametrize("boundary", ["before_first_move", "after_first_move", "task_removal"])
def test_persistence_failure_keeps_recoverable_evidence_and_reports_mutations(tmp_path, monkeypatch, boundary):
    ctx, server, _ = legacy(tmp_path)
    original = copy.deepcopy(pending(ctx))
    real_set = ctx.state.set_extra
    calls = []
    def checkpoint(task_id, extra):
        calls.append(extra)
        if len(calls) == (1 if boundary == "before_first_move" else 2):
            raise StatePersistenceError("test: checkpoint unavailable")
        return real_set(task_id, extra)
    if boundary == "task_removal":
        monkeypatch.setattr(ctx.state, "remove_task", lambda task_id: (_ for _ in ()).throw(StatePersistenceError("test: removal unavailable")))
    else:
        monkeypatch.setattr(ctx.state, "set_extra", checkpoint)
    code, text = discard(ctx, server, tmp_path)
    assert code == 3 and "Discard stopped" in text and "Nothing changed" not in text
    archive = [json.loads(line) for line in (tmp_path / "recover_archive.jsonl").read_text().splitlines()]
    assert archive[0]["task"]["extra"]["import_pending"] == original
    assert len(ctx.state.get_tasks_for_orchestrator()) == 1
    if boundary == "before_first_move":
        assert all(Path(path).is_file() for path in original["files"])
    else:
        assert any(Path(file["destination"]).is_file() for file in archive[0]["files"])
    monkeypatch.undo()
    ctx.state = StateManager(str(ctx.state.filepath.parent))
    assert discard(ctx, server, tmp_path)[0] == 0


@pytest.mark.parametrize("new_evidence", ["changed_destination", "active_destination_import", "reappeared_source"])
def test_discard_retry_rechecks_files_and_chaptarr_evidence(tmp_path, monkeypatch, new_evidence):
    ctx, server, root = legacy(tmp_path)
    journal = interrupt_second_move(ctx, server, tmp_path, monkeypatch)
    first = journal["files"][0]
    if new_evidence == "changed_destination":
        Path(first["destination"]).write_bytes(b"changed quarantine file")
    elif new_evidence == "reappeared_source":
        Path(first["source"]).write_bytes(b"new unrelated file")
    else:
        path = "/remote/" + str(Path(first["destination"]).relative_to(root))
        server.command_list = [{"id": 808, "name": "ManualImport", "status": "started", "body": {"files": [{"path": path}]}}]
    before = ctx.state.filepath.read_bytes()
    code, text = discard(ctx, server, tmp_path)
    assert code == 1 and "No further files moved" in text and "Nothing changed" not in text
    assert ctx.state.filepath.read_bytes() == before
    assert Path(journal["files"][1]["source"]).is_file()


def test_read_only_state_cannot_authorize_discard_moves(tmp_path):
    ctx, server, _ = legacy(tmp_path)
    ctx.state.read_only = True
    assert_blocked_unchanged(ctx, server, tmp_path, "read-only")


def test_apply_checkpoint_error_does_not_claim_nothing_changed(tmp_path, monkeypatch):
    ctx, server, _ = legacy(tmp_path)
    real_set = ctx.state.set_extra
    def error_after_commit(task_id, extra):
        real_set(task_id, extra)
        raise StatePersistenceError("test: error after checkpoint rename")
    monkeypatch.setattr(ctx.state, "set_extra", error_after_commit)
    code, text = run(ctx, server, "--book", "42", "--apply")
    assert code == 3 and "checkpoint may already have reached disk" in text and "Nothing changed" not in text
    assert pending(ctx).get("recovery")
