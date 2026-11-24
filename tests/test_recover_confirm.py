"""recover --confirm: closing a review item that Chaptarr evidently imported.

R:soul keeps an import for review when it cannot prove the outcome itself (lost command
response, new book files not tied to the staged ones). --apply and --discard both refuse once
Chaptarr's history shows an import, so such an item used to stay in review for ever.
"""

import io
import json
import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_import_transaction import saved_pending, setup, writer
from test_recover_apply import CommandServer

from rsoul import postprocess, recover
from rsoul.ledger import BookLedger


def stuck(tmp_path, phase="needs_review"):
    """A real journaled import whose command response was lost and that was then set aside."""
    ctx, item, _, _ = setup(tmp_path)
    server = CommandServer(tmp_path / "downloads")
    ctx.readarr = server
    server.action, server.lose_response = "none", True
    pending = postprocess.process_imports(ctx, [item])[42]
    assert pending["phase"] == "submission_unknown"
    pending = dict(pending, phase=phase, reason="Submission outcome is unknown; review age exceeded, all local files retained")
    writer(ctx)(pending)
    return ctx, server, pending


def run(ctx, server, tmp_path, *args):
    out = io.StringIO()
    ledger = BookLedger(str(tmp_path))
    code = recover.main(["--config-dir", str(tmp_path), *args], client=server, state=ctx.state, out=out,
                        local_root=ctx.config["Slskd"]["download_dir"], server_root=ctx.config["Slskd"]["readarr_download_dir"],
                        ledger=ledger)
    return code, out.getvalue(), ledger


def staged(pending):
    return [f["readarr_path"] for f in pending["manifest"]]


def tasks(ctx):
    return ctx.state.get_tasks_for_orchestrator()


def test_complete_import_in_history_is_confirmed_and_the_task_removed(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending))

    code, text, _ = run(ctx, server, tmp_path, "--book", "42")  # inspection only
    assert code == 0 and "close it with --confirm" in text and "2 of 2" in text
    assert len(tasks(ctx)) == 1

    code, text, ledger = run(ctx, server, tmp_path, "--book", "42", "--confirm")
    assert code == 0 and "Confirmed as imported" in text
    assert tasks(ctx) == []
    archived = [json.loads(line) for line in (tmp_path / "recover_archive.jsonl").read_text().splitlines()]
    assert archived[-1]["action"] == "confirm" and archived[-1]["task"]["extra"]["import_pending"]["phase"] == "needs_review"
    assert ledger.books["42"]["imported_at"] and ledger.books["42"]["media_type"] == "audiobook"


def test_submission_unknown_can_be_confirmed_too(tmp_path):
    ctx, server, pending = stuck(tmp_path, phase="submission_unknown")
    server.publish(staged(pending))
    assert run(ctx, server, tmp_path, "--book", "42", "--confirm")[0] == 0
    assert tasks(ctx) == []


def test_no_import_in_history_blocks(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm")
    assert code == 1 and "shows no import for this book" in text and "Nothing changed" in text
    assert saved_pending(ctx)["phase"] == "needs_review"
    assert not (tmp_path / "recover_archive.jsonl").exists()


def test_a_file_missing_from_history_blocks(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending)[:1])
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm")
    assert code == 1 and "1 staged file(s) are not named" in text
    assert len(tasks(ctx)) == 1


def test_unreadable_history_blocks(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending))
    server.no_history = True
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm")
    assert code == 1 and "history cannot be read" in text
    assert len(tasks(ctx)) == 1


@pytest.mark.parametrize("status", ["queued", "started", "weird"])
def test_an_unsettled_command_for_the_files_blocks(tmp_path, status):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending))
    server.command_list = [{"id": 900, "name": "ManualImport", "status": status, "result": None,
                            "body": {"files": [{"path": p} for p in staged(pending)]}}]
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm")
    assert code == 1 and "Command 900" in text
    assert len(tasks(ctx)) == 1


def test_a_successful_command_for_the_files_is_evidence_not_an_obstacle(tmp_path):
    # --apply refuses here (the import may have happened); --confirm is for exactly this case
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending))
    server.command_list = [{"id": 900, "name": "ManualImport", "status": "completed", "result": "successful",
                            "body": {"files": [{"path": p} for p in staged(pending)]}}]
    assert run(ctx, server, tmp_path, "--book", "42", "--confirm")[0] == 0


def test_partial_import_is_not_confirmed(tmp_path):
    ctx, server, pending = stuck(tmp_path, phase="partial")
    server.publish(staged(pending))
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm")
    assert code == 1 and "partial import still lacks files" in text
    assert len(tasks(ctx)) == 1


def test_history_under_a_changed_root_mapping_still_counts(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending), consume=False)
    for event in server.events:
        event["data"]["DroppedPath"] = event["data"]["DroppedPath"].replace("/remote/", "/mnt/media/downloads/")
    assert run(ctx, server, tmp_path, "--book", "42", "--confirm")[0] == 0


def test_another_folder_with_the_same_file_names_does_not_count(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending), consume=False)
    for event in server.events:
        name = event["data"]["DroppedPath"].rsplit("/", 1)[1]
        event["data"]["DroppedPath"] = "/remote/some other book/" + name
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm")
    assert code == 1 and "2 staged file(s) are not named" in text


def test_leftover_staged_files_are_listed_and_left_alone(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending), consume=False)  # Chaptarr copied instead of moving
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm")
    assert code == 0 and "were left alone" in text
    for entry in pending["manifest"]:
        assert os.path.exists(entry["stage_path"]) and entry["stage_path"] in text


def test_listing_names_review_items_with_their_book_id(tmp_path):
    ctx, server, _ = stuck(tmp_path)
    code, text, _ = run(ctx, server, tmp_path)
    assert code == 0 and "book 42: Correct Author - Intended Work (needs_review); inspect with --book 42" in text


def test_confirm_needs_a_book():
    with pytest.raises(SystemExit):
        recover.main(["--confirm"], out=io.StringIO())
