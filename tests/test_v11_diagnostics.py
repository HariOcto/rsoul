"""Central recovery diagnostics are useful without changing evidence or using APIs."""

import json
import socket
import sys

import pytest

from rsoul import diagnostics


def snapshot(root):
    """Capture file bytes, mtimes, and complete inventory, including directories."""
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        key = str(path.relative_to(root))
        result[key] = (path.is_dir(), path.stat().st_mtime_ns,
                       None if path.is_dir() else path.read_bytes())
    return result


@pytest.fixture(autouse=True)
def forbid_network_and_optional_writes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Diagnostics must not make API calls or write reports")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(diagnostics, "report_versions", forbidden)
    monkeypatch.setattr(diagnostics, "atomic_write_json", forbidden)


def task(extra=None, task_id="t1"):
    return {"task_id": task_id, "backend_name": "slskd", "book_id": 7,
            "book_title": "A Book", "author_name": "An Author", "filename": "01.mp3",
            "local_dir": "peer-folder", "series_title": "", "extra": extra or {}}


def call_report(directory, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["diagnostics", "--config-dir", str(directory)])
    code = diagnostics.main()
    return code, json.loads(capsys.readouterr().out)


def test_legacy_and_attempt_receipts_and_import_paths_are_reported_read_only(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    data.mkdir()
    source, stage = tmp_path / "source", tmp_path / "staged"
    source.mkdir()
    stage.mkdir()
    (source / "01.mp3").write_bytes(b"retained source evidence")
    (stage / "02.mp3").write_bytes(b"retained staged evidence")
    pending = {"phase": "submission_unknown", "local_dir": str(stage), "source_dir": str(source),
               "readarr_path": "/remote/staged", "files": [str(stage / "02.mp3")], "command_id": 17,
               "attempts": [{"command_id": 17}, {"command_id": "17"}, {"receipt": {"id": 21}},
                            {"command_id": 30, "receipt": {"id": 30}}, None, {"command_id": False}],
               "manifest": [{"source_path": str(source / "01.mp3"), "stage_path": str(stage / "01.mp3")},
                            {"source_path": str(source / "02.mp3"), "stage_path": str(stage / "02.mp3")}],
               "retained_inputs": [str(stage / "02.mp3")], "reason": "Import response was lost"}
    (data / "grab_list_state.json").write_text(json.dumps([task({"import_pending": pending})]))
    (data / "health.json").write_text('{"status":"idle"}')
    before = snapshot(tmp_path)
    code, report = call_report(data, monkeypatch, capsys)
    assert code == 0 and report["scope"] == "central_state"
    row = report["pending"][0]
    assert row["command_ids"] == [17, 21, 30]
    assert row["phase"] == "submission_unknown" and row["expected_files"] == 2
    assert row["paths"]["source_directory"] == str(source)
    assert row["paths"]["staging_directory"] == str(stage)
    assert row["paths"]["server_staging_directory"] == "/remote/staged"
    assert row["paths"]["source_files"] == [str(source / "01.mp3"), str(source / "02.mp3")]
    assert row["paths"]["staged_files"] == [str(stage / "01.mp3"), str(stage / "02.mp3")]
    assert row["paths"]["retained_inputs"] == [str(stage / "02.mp3")]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("extra,phase,review,cleanup", [
    ({"needs_review": True, "failure_kind": "legacy_unverified"}, "needs_review", True, False),
    ({"recovery_review": {"reason": "Saved transfer could not be identified"}}, "needs_review", True, False),
    ({"cleanup_pending": {"outcome": "failed_download", "reason": "Owned cleanup is unconfirmed"}}, "cleanup_pending", False, True),
    ({"retry_cleanup_pending": True}, "cleanup_pending", False, True),
    ({"terminal_failure": "Transfer failed; cleanup unconfirmed"}, "cleanup_pending", False, True),
    ({"discard": {"phase": "prepared"}}, "cleanup_pending", False, True),
    ({"needs_review": True, "cleanup_pending": {"outcome": "failed_download"}}, "cleanup_pending", True, True),
    ({"import_pending": {"phase": "verified", "local_dir": "/stage", "files": []},
      "cleanup_pending": {"outcome": "import_processed"}}, "cleanup_pending", False, True),
    ({"import_pending": {"phase": "partial", "local_dir": "/stage", "files": [], "reason": "One input unverified"}}, "partial", True, False),
])
def test_transfer_review_and_cleanup_phases_are_visible(tmp_path, monkeypatch, capsys, extra, phase, review, cleanup):
    (tmp_path / "grab_list_state.json").write_text(json.dumps([task(extra)]))
    before = snapshot(tmp_path)
    code, report = call_report(tmp_path, monkeypatch, capsys)
    row = report["pending"][0]
    assert code == 0 and row["phase"] == phase
    assert row["needs_review"] is review and row["cleanup_pending"] is cleanup
    assert row["reason"]
    assert snapshot(tmp_path) == before


def test_partial_cleanup_paths_and_names_are_reported_without_inspecting_them(tmp_path, monkeypatch, capsys):
    extra = {"cleanup_pending": {"outcome": "failed_download"},
             "discard": {"phase": "incomplete", "source": "/unmounted/downloads/peer-folder",
                         "destination": "/unmounted/downloads/failed_downloads/task1", "error": "Permission denied",
                         "moved": ["01.mp3"], "remaining": ["02.mp3"]}}
    (tmp_path / "grab_list_state.json").write_text(json.dumps([task(extra)]))
    before = snapshot(tmp_path)
    code, report = call_report(tmp_path, monkeypatch, capsys)
    row = report["pending"][0]
    assert code == 0 and row["phase"] == "cleanup_pending" and row["cleanup_phase"] == "incomplete"
    assert row["reason"] == "Permission denied"
    assert row["paths"]["cleanup_source_directory"] == extra["discard"]["source"]
    assert row["paths"]["cleanup_destination_directory"] == extra["discard"]["destination"]
    assert row["paths"]["cleanup_moved_names"] == ["01.mp3"]
    assert row["paths"]["cleanup_remaining_names"] == ["02.mp3"]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("contents", ["{broken json", json.dumps([task(), {"task_id": 5}])])
def test_corrupt_recovery_state_exits_nonzero_without_backup_or_mutation(tmp_path, monkeypatch, capsys, contents):
    (tmp_path / "grab_list_state.json").write_text(contents)
    before = snapshot(tmp_path)
    code, report = call_report(tmp_path, monkeypatch, capsys)
    assert code == 1 and report["state_error"]
    assert snapshot(tmp_path) == before


def test_sidecars_are_not_adopted_by_diagnostics(tmp_path, monkeypatch, capsys):
    (tmp_path / "grab_list_state.json").write_text("[]")
    sidecars = tmp_path / ".rsoul-import-journal"
    sidecars.mkdir()
    (sidecars / "operation.json").write_text('{"phase":"submission_unknown"}')
    before = snapshot(tmp_path)
    code, report = call_report(tmp_path, monkeypatch, capsys)
    assert code == 0 and report["pending"] == []
    assert report["scope"] == "central_state" and "normal startup" in report["notice"]
    assert snapshot(tmp_path) == before


def test_missing_config_directory_is_not_created(tmp_path, monkeypatch, capsys):
    before = snapshot(tmp_path)
    code, report = call_report(tmp_path / "absent", monkeypatch, capsys)
    assert code == 0 and report["pending"] == []
    assert snapshot(tmp_path) == before
