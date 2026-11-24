"""Operator-decided recovery of imports that an early draft left without an outcome.

Replaces the automatic resubmission of an earlier draft, which an outside review showed
could repeat an import Chaptarr had already accepted (queued or running, response lost) and
let recovered files skip the normal audio and recording-policy validation.
"""

import io
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_import_transaction import Server, audio, setup

from rsoul import import_transaction, postprocess, recover

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
STAGE = "rsoul_audiobooks/Correct Author/Intended Work"


class CommandServer(Server):
    """The fake Chaptarr, plus the command list."""

    def __init__(self, root):
        super().__init__(root)
        self.command_list = []

    def get_command(self, command_id=None):
        if command_id is None:
            return list(self.command_list)
        return super().get_command(command_id)


def legacy(tmp_path, names=None, writer=audio, schema=None):
    """A task whose import an early draft saved as not confirmed, files in the staging folder."""
    ctx, item, _, _ = setup(tmp_path)
    root = tmp_path / "downloads"
    server = CommandServer(root)
    ctx.readarr = server
    stage = root / STAGE
    stage.mkdir(parents=True)
    names = names or item["expected_files"]
    for i, name in enumerate(names):
        if (root / "peer-folder" / name).exists():
            os.remove(root / "peer-folder" / name)
        writer(stage / name, i + 1)
    pending = {  # an early draft's pending_import_record
        "folder": STAGE, "local_dir": str(stage), "readarr_path": "/remote/" + STAGE,
        "files": [str(stage / n) for n in names], "command_id": None, "submits": 0,
        "since": time.time() - 3600, "method": "targeted", "book_id": 42, "media_type": "audiobook",
        "edition_id": None, "before_ids": None,
    }
    if schema:
        pending = import_transaction._migrate_legacy(pending)  # already migrated to the journal format
    task = ctx.state.get_tasks_for_orchestrator()[0]
    ctx.state.set_extra(task["task_id"], dict(task["extra"], import_pending=pending, expected_files=names))
    return ctx, server, root


def run(ctx, server, *args):
    out = io.StringIO()
    code = recover.main(["--config-dir", "unused", *args], client=server, state=ctx.state, out=out,
                        local_root=ctx.config["Slskd"]["download_dir"], server_root=ctx.config["Slskd"]["readarr_download_dir"])
    return code, out.getvalue()


def pending(ctx):
    return ctx.state.get_tasks_for_orchestrator()[0]["extra"]["import_pending"]


def next_run(ctx):
    """What the workflow does with the record on its next run."""
    item = ctx.state.get_tasks_for_orchestrator()[0]
    def persist(value):
        item["extra"]["import_pending"] = value
        ctx.state.set_extra(item["task_id"], item["extra"])
    return postprocess.check_pending_import(ctx.readarr, item["extra"]["import_pending"], config=ctx.config, persist=persist)


def event(date, path="/remote/x"):
    return {"bookId": 42, "eventType": "bookFileImported", "date": date.isoformat(), "data": {"DroppedPath": path, "ImportedPath": "/lib/x"}}


# ---------------------------------------------------------------------------
# Nothing happens on its own
# ---------------------------------------------------------------------------


def test_v11_never_resubmits_a_legacy_record_by_itself(tmp_path):
    ctx, server, _ = legacy(tmp_path)
    result = next_run(ctx)
    assert server.posts == []
    assert result["phase"] in ("submission_unknown", "needs_review") and result["legacy"]


def test_listing_shows_the_evidence_and_changes_nothing(tmp_path):
    ctx, server, _ = legacy(tmp_path)
    before = pending(ctx)
    code, text = run(ctx, server)
    assert code == 0
    assert "Book 42: Intended Work" in text and "chapter0.wav" in text and "can be recovered with --apply" in text
    assert pending(ctx) == before and server.posts == []


# ---------------------------------------------------------------------------
# Review finding 1: an import Chaptarr may already have
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status, result", [("queued", None), ("started", None), ("completed", "successful")])
def test_a_command_naming_the_files_blocks_recovery(tmp_path, status, result):
    # the accepted-but-response-lost case: the earlier POST is in Chaptarr's command list
    ctx, server, _ = legacy(tmp_path)
    server.command_list = [{"id": 555, "name": "ManualImport", "status": status, "result": result,
                            "body": {"files": [{"path": "/remote/" + STAGE + "/" + n} for n in ctx.state.get_tasks_for_orchestrator()[0]["extra"]["expected_files"]]}}]
    before = pending(ctx)
    code, text = run(ctx, server, "--book", "42", "--apply")
    assert code == 1 and "Command 555" in text and "Nothing changed" in text
    assert pending(ctx) == before
    assert next_run(ctx)["phase"] in ("submission_unknown", "needs_review") and server.posts == []


def test_a_failed_command_for_the_files_does_not_block(tmp_path):
    ctx, server, _ = legacy(tmp_path)
    names = ctx.state.get_tasks_for_orchestrator()[0]["extra"]["expected_files"]
    server.command_list = [{"id": 556, "name": "ManualImport", "status": "failed", "result": "unsuccessful",
                            "body": {"files": [{"path": "/remote/" + STAGE + "/" + n} for n in names]}},
                           {"id": 557, "name": "ManualImport", "status": "completed", "result": "unsuccessful",
                            "body": {"files": [{"path": "/remote/" + STAGE + "/" + n} for n in names]}}]
    code, _ = run(ctx, server, "--book", "42", "--apply")
    assert code == 0


# ---------------------------------------------------------------------------
# Review finding 3: each history event is judged by its own date
# ---------------------------------------------------------------------------


def test_an_import_event_from_before_staging_is_ignored(tmp_path):
    ctx, server, _ = legacy(tmp_path)
    server.events = [event(datetime.now(timezone.utc) - timedelta(days=1))]
    code, text = run(ctx, server, "--book", "42", "--apply")
    assert code == 0 and "import event" not in text


def test_an_import_event_since_staging_blocks(tmp_path):
    ctx, server, _ = legacy(tmp_path)
    server.events = [event(datetime.now(timezone.utc) - timedelta(minutes=5))]
    code, text = run(ctx, server, "--book", "42", "--apply")
    assert code == 1 and "1 import event(s)" in text
    assert pending(ctx).get("recovery") is None


def test_an_unreadable_history_blocks_only_until_it_is_readable_again(tmp_path):
    ctx, server, _ = legacy(tmp_path)
    server.no_history = True
    before = pending(ctx)
    code, text = run(ctx, server, "--book", "42", "--apply")
    assert code == 1 and "history cannot be read" in text
    assert pending(ctx) == before  # nothing recorded: the next attempt checks again
    server.no_history = False
    assert run(ctx, server, "--book", "42", "--apply")[0] == 0


# ---------------------------------------------------------------------------
# Review finding 2: recovered files go through the ordinary validation
# ---------------------------------------------------------------------------


def test_recovered_record_is_validated_then_imported_and_verified(tmp_path):
    ctx, server, _ = legacy(tmp_path)
    assert run(ctx, server, "--book", "42", "--apply")[0] == 0
    record = pending(ctx)
    assert record["phase"] == "validating" and not record.get("inputs_validated") and not record.get("legacy")
    assert record["recovery"]["previous"]["submits"] == 0  # the old record is kept as evidence

    assert next_run(ctx) is True
    assert [name for name, _ in server.posts] == ["ManualImport"]
    assert {f["bookId"] for f in server.posts[0][1]["files"]} == {42}


def invalid_mp3(path, value):
    path = str(path).rsplit(".", 1)[0] + ".mp3"
    with open(path, "wb") as out:
        out.write(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\0" * 4000)


def test_invalid_audio_is_rejected_after_recovery(tmp_path):
    ctx, server, _ = legacy(tmp_path, names=["chapter0.mp3"], writer=invalid_mp3)
    assert run(ctx, server, "--book", "42", "--apply")[0] == 0
    result = next_run(ctx)
    assert server.posts == []
    assert result["phase"] == "needs_review" and "audio" in result["reason"].lower()


def german_mp3(path, value):
    from mutagen.id3 import ID3, TLAN
    shutil.copy(os.path.join(FIXTURES, "silence.mp3"), path)
    tags = ID3(path)
    tags.add(TLAN(encoding=3, text=["deu"]))
    tags.save(path)


def test_recording_policy_applies_after_recovery(tmp_path):
    ctx, server, _ = legacy(tmp_path, names=["chapter0.mp3"], writer=german_mp3)
    ctx.config["Search Settings"]["audiobook_languages"] = "en"
    assert run(ctx, server, "--book", "42", "--apply")[0] == 0
    result = next_run(ctx)
    assert server.posts == []
    assert result["phase"] == "needs_review" and "language" in result["reason"]


def test_a_staged_file_that_differs_from_the_original_download_blocks(tmp_path):
    ctx, server, root = legacy(tmp_path)
    task = ctx.state.get_tasks_for_orchestrator()[0]
    names = task["extra"]["expected_files"]
    records = [{"filename": "x\\" + n, "size": os.path.getsize(root / STAGE / n)} for n in names]
    records[0]["size"] = 999999
    ctx.state.set_extra(task["task_id"], dict(task["extra"], files=records))
    code, text = run(ctx, server, "--book", "42", "--apply")
    assert code == 1 and "999999" in text and "Nothing changed" in text


def test_a_missing_staged_file_blocks(tmp_path):
    ctx, server, root = legacy(tmp_path)
    os.remove(pending(ctx)["files"][0])
    code, text = run(ctx, server, "--book", "42", "--apply")
    assert code == 1 and "missing" in text


# ---------------------------------------------------------------------------
# Records already migrated to the journal format are recoverable too
# ---------------------------------------------------------------------------


def test_record_already_migrated_to_the_v11_format_is_a_candidate(tmp_path):
    ctx, server, _ = legacy(tmp_path, schema=True)
    assert pending(ctx)["schema"] and pending(ctx)["legacy"]
    assert run(ctx, server, "--book", "42", "--apply")[0] == 0
    assert next_run(ctx) is True


def test_a_record_with_a_command_is_not_a_candidate(tmp_path):
    ctx, server, _ = legacy(tmp_path)
    task = ctx.state.get_tasks_for_orchestrator()[0]
    ctx.state.set_extra(task["task_id"], dict(task["extra"], import_pending=dict(pending(ctx), command_id=17)))
    code, text = run(ctx, server, "--book", "42")
    assert code == 1 and "No saved import without an outcome" in text


def test_apply_needs_a_book():
    with pytest.raises(SystemExit):
        recover.main(["--apply"], client=object(), state=SimpleNamespace(get_tasks_for_orchestrator=lambda: []), out=io.StringIO())


# ---------------------------------------------------------------------------
# Second review: the six findings
# ---------------------------------------------------------------------------


def command_for(paths, status="queued", result=None, use_files=True):
    body = {"files": [{"path": p} for p in paths]} if use_files else {"path": paths[0]}
    return {"id": 700, "name": "ManualImport", "status": status, "result": result, "body": body}


@pytest.mark.parametrize("server_root", ['/downloads/My "Audio"', "C:\\Media\\Downloads"])
def test_finding1_queued_command_is_found_whatever_characters_the_path_has(tmp_path, server_root):
    ctx, server, root = legacy(tmp_path)
    ctx.config["Slskd"]["readarr_download_dir"] = server_root
    task = ctx.state.get_tasks_for_orchestrator()[0]
    sep = "\\" if "\\" in server_root else "/"
    folder = server_root + sep + STAGE.replace("/", sep)
    names = task["extra"]["expected_files"]
    record = dict(task["extra"]["import_pending"], readarr_path=folder)
    ctx.state.set_extra(task["task_id"], dict(task["extra"], import_pending=record))
    server.command_list = [command_for([folder + sep + n for n in names])]
    before = pending(ctx)
    code, text = run(ctx, server, "--book", "42", "--apply")
    assert code == 1 and "still queued" in text
    assert pending(ctx) == before


@pytest.mark.parametrize("response, expected", [
    ({"error": "upstream command list unavailable"}, "unsupported form"),
    (["not a command"], "unreadable entry"),
])
def test_finding2_unsupported_command_list_blocks(tmp_path, response, expected):
    ctx, server, _ = legacy(tmp_path)
    server.get_command = lambda command_id=None: response
    before = pending(ctx)
    code, text = run(ctx, server, "--book", "42", "--apply")
    assert code == 1 and expected in text and pending(ctx) == before


def test_finding2_command_with_unknown_status_or_unreadable_body_blocks(tmp_path):
    ctx, server, _ = legacy(tmp_path)
    server.command_list = [dict(command_for(["/remote/" + STAGE + "/chapter0.wav"]), status=None)]
    assert run(ctx, server, "--book", "42", "--apply")[0] == 1
    server.command_list = [{"id": 1, "name": "ManualImport", "status": "queued", "body": "files: ..."}]
    code, text = run(ctx, server, "--book", "42", "--apply")
    assert code == 1 and "no readable body" in text


def test_finding2_import_event_with_unreadable_date_blocks(tmp_path):
    ctx, server, _ = legacy(tmp_path)
    server.events = [{"bookId": 42, "eventType": "bookFileImported", "date": "yesterday", "data": {}}]
    code, text = run(ctx, server, "--book", "42", "--apply")
    assert code == 1 and "unreadable date" in text


def test_finding2_unreadable_book_files_block(tmp_path):
    ctx, server, _ = legacy(tmp_path)
    original = server._get
    server._get = lambda resource, ver_uri=None, params=None: (_ for _ in ()).throw(ConnectionError("down")) if resource == "bookfile" else original(resource, ver_uri, params)
    code, text = run(ctx, server, "--book", "42", "--apply")
    assert code == 1 and "book's files cannot be read" in text


def test_finding3_a_pending_list_short_of_a_chapter_is_not_redefined_as_complete(tmp_path):
    ctx, server, root = legacy(tmp_path)
    task = ctx.state.get_tasks_for_orchestrator()[0]
    names = task["extra"]["expected_files"]
    os.remove(root / STAGE / names[1])
    record = dict(task["extra"]["import_pending"], files=[str(root / STAGE / names[0])])  # inconsistent legacy record
    ctx.state.set_extra(task["task_id"], dict(task["extra"], import_pending=record))
    code, text = run(ctx, server, "--book", "42", "--apply")
    assert code == 1 and "expected files list (2 file(s)) does not match the pending import (1)" in text
    assert next_run(ctx) is not True and server.posts == []


def test_finding3_missing_file_with_consistent_lists_is_caught_by_v11s_constructor(tmp_path):
    ctx, server, root = legacy(tmp_path)
    names = ctx.state.get_tasks_for_orchestrator()[0]["extra"]["expected_files"]
    os.remove(root / STAGE / names[1])
    code, text = run(ctx, server, "--book", "42", "--apply")
    assert code == 1 and "Cannot inventory" in text


def test_finding4_server_paths_come_from_the_current_mapping(tmp_path):
    ctx, server, root = legacy(tmp_path)
    task = ctx.state.get_tasks_for_orchestrator()[0]
    record = dict(task["extra"]["import_pending"], readarr_path="/old-remote/" + STAGE)
    ctx.state.set_extra(task["task_id"], dict(task["extra"], import_pending=record))
    code, text = run(ctx, server, "--book", "42", "--apply")
    assert code == 0 and "another server path (/old-remote/" in text
    recovered = pending(ctx)
    assert recovered["readarr_path"].startswith("/remote/rsoul_audiobooks/")
    assert all(e["readarr_path"].startswith("/remote/") for e in recovered["manifest"])
    assert next_run(ctx) is True
    assert all(f["path"].startswith("/remote/") for f in server.posts[0][1]["files"])


def prologue_mp3(path, value):
    shutil.copy(os.path.join(FIXTURES, "silence.mp3"), path)


def test_finding5_a_prologue_only_download_is_rejected_before_import(tmp_path):
    ctx, server, _ = legacy(tmp_path, names=["Intended Work [Prologue].mp3"], writer=prologue_mp3)
    assert run(ctx, server, "--book", "42", "--apply")[0] == 0
    result = next_run(ctx)
    assert server.posts == []
    assert result["phase"] == "needs_review" and "fragment" in result["reason"]


def test_finding5_a_complete_book_with_a_prologue_chapter_passes(tmp_path):
    ctx, server, _ = legacy(tmp_path, names=["00 - Prologue.mp3", "01 - Chapter One.mp3"], writer=prologue_mp3)
    assert run(ctx, server, "--book", "42", "--apply")[0] == 0
    assert next_run(ctx) is True


def test_finding5_a_folder_labelled_as_a_prologue_is_rejected(tmp_path):
    ctx, server, _ = legacy(tmp_path, names=["01.mp3"], writer=prologue_mp3)
    task = ctx.state.get_tasks_for_orchestrator()[0]
    ctx.state.set_extra(task["task_id"], dict(task["extra"], full_dir="audiobooks\\Correct Author\\prologue for 9 - Intended Work Part 1"))
    assert run(ctx, server, "--book", "42", "--apply")[0] == 0
    result = next_run(ctx)
    assert server.posts == [] and "fragment" in result["reason"]


def test_finding6_the_cli_client_uses_the_configured_timeout():
    import configparser
    import socket
    import threading
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    held = []
    threading.Thread(target=lambda: held.append(listener.accept()), daemon=True).start()
    config = configparser.ConfigParser()
    config["Readarr"] = {"host_url": f"http://127.0.0.1:{listener.getsockname()[1]}", "api_key": "x", "request_timeout": "0.2"}
    client = recover._client(config)
    started = time.time()
    with pytest.raises(Exception):
        client.get_command()
    assert time.time() - started < 3
    listener.close()


# ---------------------------------------------------------------------------
# Smaller items
# ---------------------------------------------------------------------------


def write_config(tmp_path, root):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "config.ini").write_text(
        "[Readarr]\nhost_url = http://127.0.0.1:9\napi_key = x\nrequest_timeout = 0.2\n"
        f"[Slskd]\nhost_url = http://127.0.0.1:9\napi_key = x\ndownload_dir = {root}\nreadarr_download_dir = /remote\n")
    return config_dir


def test_listing_works_while_rsoul_holds_the_lock_but_apply_does_not(tmp_path):
    from rsoul.locking import InstanceLock
    ctx, server, root = legacy(tmp_path)
    config_dir = write_config(tmp_path, root)
    lock = InstanceLock(str(config_dir))
    lock.acquire()
    try:
        out = io.StringIO()
        assert recover.main(["--config-dir", str(config_dir)], client=server, state=ctx.state, out=out) == 0
        assert "Book 42" in out.getvalue()
        out = io.StringIO()
        assert recover.main(["--config-dir", str(config_dir), "--book", "42", "--apply"], client=server, state=ctx.state, out=out) == 2
        assert "running" in out.getvalue()
    finally:
        lock.release()


def test_damaged_state_is_reported_not_taken_as_empty(tmp_path):
    state = SimpleNamespace(load_error="The recovery state was damaged", get_tasks_for_orchestrator=lambda: [])
    out = io.StringIO()
    code = recover.main(["--config-dir", "unused"], client=object(), state=state, out=out, local_root=str(tmp_path), server_root="/remote")
    assert code == 3 and "cannot be inspected" in out.getvalue()


# ---------------------------------------------------------------------------
# Third review (guide items 16 to 18)
# ---------------------------------------------------------------------------


def files_of(ctx, prefix="/remote/"):
    names = ctx.state.get_tasks_for_orchestrator()[0]["extra"]["expected_files"]
    return [{"path": prefix + STAGE + "/" + n} for n in names]


@pytest.mark.parametrize("command, expected", [
    ({"id": 1, "status": "queued", "body": {"files": []}}, "no readable name"),
    ({"id": 1, "name": "ManualImport", "status": "queued", "body": {"path": "/remote/x"}}, "no readable file list"),
    ({"id": 1, "name": "ManualImport", "status": "queued", "body": {"files": []}}, "no readable file list"),
    ({"id": 1, "name": "ManualImport", "status": "queued", "body": {"files": [{"path": "/remote/elsewhere/a.mp3"}, {"name": "b.mp3"}]}}, "without a usable absolute path"),
    ({"id": 1, "name": "ManualImport", "status": "queued", "body": {"files": [{"path": "rsoul_audiobooks/x/a.mp3"}]}}, "without a usable absolute path"),
    ({"id": 1, "name": "ManualImport", "status": "queued", "body": {"files": [{"path": "\\Media\\a.mp3"}]}}, "without a usable absolute path"),
    ({"id": 1, "name": "DownloadedBooksScan", "status": "queued", "body": {}}, "no usable absolute path"),
    ({"id": 1, "name": "DownloadedBooksScan", "status": "queued", "body": {"path": "downloads"}}, "no usable absolute path"),
])
def test_item16_incomplete_import_commands_block(tmp_path, command, expected):
    ctx, server, _ = legacy(tmp_path)
    server.command_list = [command]
    before = pending(ctx)
    code, text = run(ctx, server, "--book", "42", "--apply")
    assert code == 1 and expected in text and pending(ctx) == before


def test_item16_other_commands_without_paths_are_ignored(tmp_path):
    ctx, server, _ = legacy(tmp_path)
    server.command_list = [{"id": 2, "name": "RssSync", "status": "started", "body": {}},
                           {"id": 3, "name": "RefreshAuthor", "status": "queued", "body": {"authorId": 7}}]
    assert run(ctx, server, "--book", "42", "--apply")[0] == 0


@pytest.mark.parametrize("status, result, blocks", [
    ("completed", None, True), ("completed", "", True), ("completed", "unknown", True), ("completed", "partial", True),
    ("completed", "successful", True), ("completed", "unsuccessful", False),
    ("failed", "unknown", True), ("aborted", None, False), ("cancelled", "unsuccessful", False),
    ("failed", "successful", True), ("orphaned", "weird", True),
])
def test_item17_completed_needs_an_explicit_unsuccessful_result(tmp_path, status, result, blocks):
    ctx, server, _ = legacy(tmp_path)
    server.command_list = [{"id": 9, "name": "ManualImport", "status": status, "result": result, "body": {"files": files_of(ctx)}}]
    code, _ = run(ctx, server, "--book", "42", "--apply")
    assert (code == 1) is blocks


@pytest.mark.parametrize("scan_path, blocks", [
    ("/remote", True), ("/remote/", True), ("/remote/rsoul_audiobooks", True), ("/remote/" + STAGE, True),
    ("/remote/rsoul_audiobooks/Correct Author/Intended Work-other", False), ("/remote/elsewhere", False), ("/remote-other", False),
])
def test_item18_a_running_scan_of_a_parent_folder_blocks(tmp_path, scan_path, blocks):
    ctx, server, _ = legacy(tmp_path)
    server.command_list = [{"id": 10, "name": "DownloadedBooksScan", "status": "started", "body": {"path": scan_path}}]
    code, _ = run(ctx, server, "--book", "42", "--apply")
    assert (code == 1) is blocks


def test_item18_a_manual_import_elsewhere_under_the_same_parent_does_not_block(tmp_path):
    ctx, server, _ = legacy(tmp_path)
    server.command_list = [{"id": 11, "name": "ManualImport", "status": "started", "result": None,
                            "body": {"files": [{"path": "/remote/rsoul_audiobooks/Other Author/Other Book/01.mp3"}]}}]
    assert run(ctx, server, "--book", "42", "--apply")[0] == 0
