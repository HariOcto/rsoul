"""recover --confirm: closing a review item that Chaptarr evidently imported.

--apply and --discard both refuse once Chaptarr's history shows an import, so an import R:soul
could not prove (lost response, unlinkable files) used to stay in review for ever. --confirm
decides it with the checks of normal reconciliation: the saved file lists agree, the book is
still the intended work, no import command for the files is still running, and the import
verifier proves every input imported. Only with --accept-history does history alone suffice,
and that is recorded as an operator decision.

Includes the reproductions from the external review of 0.1.0-beta.1: a manifest entry without
a path shrinking the expected count, a conversion that excludes a requested input, a running
command missed after a mapping change, and a changed author identity.
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

NEW_ROOT = "/mnt/media/downloads"


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


def save(ctx, pending):
    writer(ctx)(pending)


def run(ctx, server, tmp_path, *args, server_root=None, local_root=None):
    out = io.StringIO()
    ledger = BookLedger(str(tmp_path))
    code = recover.main(["--config-dir", str(tmp_path), *args], client=server, state=ctx.state, out=out,
                        local_root=local_root or ctx.config["Slskd"]["download_dir"],
                        server_root=server_root or ctx.config["Slskd"]["readarr_download_dir"], ledger=ledger)
    return code, out.getvalue(), ledger


def staged(pending):
    return [f["readarr_path"] for f in pending["manifest"]]


def tasks(ctx):
    return ctx.state.get_tasks_for_orchestrator()


def without_baseline(ctx, pending):
    """No pre-import file baseline was recorded: history still links the files, but the import
    verifier cannot tell new files from old ones and proves nothing."""
    pending["before_ids"] = None
    save(ctx, pending)


def archived(tmp_path):
    path = tmp_path / "recover_archive.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# --- verified by the import verifier -------------------------------------------------------


def test_a_verified_import_is_confirmed_and_recorded_as_verified(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending))

    code, text, _ = run(ctx, server, tmp_path, "--book", "42")  # inspection only
    assert code == 0 and "import verifier: verified" in text and "close it with --confirm" in text
    assert len(tasks(ctx)) == 1

    code, text, ledger = run(ctx, server, tmp_path, "--book", "42", "--confirm")
    assert code == 0 and "Verified and confirmed as imported" in text
    assert tasks(ctx) == []
    assert archived(tmp_path)[-1]["action"] == "confirm" and archived(tmp_path)[-1]["basis"] == "verified"
    assert ledger.books["42"]["imported_at"] and ledger.books["42"]["import_basis"] == "verified"


def test_submission_unknown_can_be_confirmed_too(tmp_path):
    ctx, server, pending = stuck(tmp_path, phase="submission_unknown")
    server.publish(staged(pending))
    assert run(ctx, server, tmp_path, "--book", "42", "--confirm")[0] == 0
    assert tasks(ctx) == []


def test_a_successful_command_for_the_files_is_evidence_not_an_obstacle(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending))
    server.command_list = [{"id": 900, "name": "ManualImport", "status": "completed", "result": "successful",
                            "body": {"files": [{"path": p} for p in staged(pending)]}}]
    assert run(ctx, server, tmp_path, "--book", "42", "--confirm")[0] == 0


# --- history only: an explicit operator decision -------------------------------------------


def test_history_only_needs_accept_history_and_is_recorded_as_such(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending))
    without_baseline(ctx, pending)

    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm")
    assert code == 1 and "--accept-history" in text and len(tasks(ctx)) == 1

    code, text, _ = run(ctx, server, tmp_path, "--book", "42")
    assert code == 0 and "Only history links every file" in text

    code, text, ledger = run(ctx, server, tmp_path, "--book", "42", "--confirm", "--accept-history")
    assert code == 0 and "history only, not verified" in text
    assert tasks(ctx) == []
    assert archived(tmp_path)[-1]["basis"] == "history"
    assert ledger.books["42"]["import_basis"] == "operator: history only"


def test_accept_history_needs_confirm():
    with pytest.raises(SystemExit):
        recover.main(["--book", "42", "--accept-history"], out=io.StringIO())


def test_history_whose_output_left_the_book_does_not_count(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending))
    without_baseline(ctx, pending)
    server.bookfiles.pop()  # the second file's output is no longer on the book
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm", "--accept-history")
    assert code == 1 and "1 file(s) have no import on the book now: chapter1.wav" in text
    assert len(tasks(ctx)) == 1


# --- review finding 2: complete inventory, conversion exclusions -----------------------------


def test_a_manifest_entry_without_a_path_does_not_shrink_the_expected_set(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending)[:1], consume=False)  # only the first file imported
    pending["manifest"][1].pop("readarr_path")
    save(ctx, pending)
    for extra in ([], ["--accept-history"]):
        code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm", *extra)
        assert code == 1 and "chapter1.wav has no usable saved server path" in text
    assert len(tasks(ctx)) == 1 and not archived(tmp_path)


def test_a_conversion_that_excludes_a_requested_input_proves_nothing(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending), conversion=True)
    server.events[-1]["data"]["ExcludedSourcePaths"] = json.dumps([staged(pending)[1]])
    for extra in ([], ["--accept-history"]):
        code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm", *extra)
        assert code == 1 and "2 file(s) have no import on the book now" in text
    assert len(tasks(ctx)) == 1


def test_a_clean_conversion_is_verified(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending), conversion=True)
    assert run(ctx, server, tmp_path, "--book", "42", "--confirm")[0] == 0


def test_a_file_missing_from_history_blocks(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending)[:1])
    for extra in ([], ["--accept-history"]):
        code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm", *extra)
        assert code == 1 and "1 file(s) have no import on the book now: chapter1.wav" in text
    assert len(tasks(ctx)) == 1


def test_no_import_at_all_blocks(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm", "--accept-history")
    assert code == 1 and "2 file(s) have no import" in text
    assert saved_pending(ctx)["phase"] == "needs_review" and not archived(tmp_path)


def test_the_same_file_names_in_another_folder_do_not_count(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending), consume=False)
    for event in server.events:
        event["data"]["DroppedPath"] = "/remote/some other book/" + event["data"]["DroppedPath"].rsplit("/", 1)[1]
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm", "--accept-history")
    assert code == 1 and "2 file(s) have no import" in text


# --- review finding 3: one path mapping for history, commands and files ----------------------


def remapped(server):
    for event in server.events:
        event["data"]["DroppedPath"] = event["data"]["DroppedPath"].replace("/remote/", NEW_ROOT + "/")


def test_history_under_the_current_mapping_is_verified(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending), consume=False)
    remapped(server)
    assert run(ctx, server, tmp_path, "--book", "42", "--confirm")[0] == 1  # old mapping: no proof
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm", server_root=NEW_ROOT)
    assert code == 0 and "Verified and confirmed" in text


@pytest.mark.parametrize("status", ["queued", "started", "weird"])
def test_a_running_command_on_the_current_paths_blocks(tmp_path, status):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending), consume=False)
    remapped(server)
    current = [p.replace("/remote/", NEW_ROOT + "/") for p in staged(pending)]
    server.command_list = [{"id": 901, "name": "ManualImport", "status": status, "result": None,
                            "body": {"files": [{"path": p} for p in current]}}]
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm", "--accept-history", server_root=NEW_ROOT)
    assert code == 1 and "Command 901" in text
    assert len(tasks(ctx)) == 1


@pytest.mark.parametrize("status", ["queued", "started"])
def test_a_running_command_on_the_saved_paths_blocks(tmp_path, status):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending))
    server.command_list = [{"id": 902, "name": "ManualImport", "status": status, "result": None,
                            "body": {"files": [{"path": p} for p in staged(pending)]}}]
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm")
    assert code == 1 and "Command 902" in text


def test_a_changed_mapping_that_cannot_be_derived_blocks(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm", server_root=NEW_ROOT, local_root=str(elsewhere))
    assert code == 1 and "cannot be derived" in text
    assert len(tasks(ctx)) == 1


def test_unreadable_history_blocks(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending))
    server.no_history = True
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm", "--accept-history")
    assert code == 1 and "history cannot be read" in text
    assert len(tasks(ctx)) == 1


# --- review finding 4: the current identity of the work --------------------------------------


@pytest.mark.parametrize("change", ["author name", "stable author id", "title"])
def test_a_changed_work_or_author_blocks(tmp_path, change):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending))
    if change == "author name":
        server.author["authorName"] = "Somebody Else"
    elif change == "stable author id":
        server.author["foreignAuthorId"] = "author:other"
    else:
        server.book["title"] = "Different Work"
    for extra in ([], ["--accept-history"]):
        code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm", *extra)
        assert code == 1 and "BLOCKS CONFIRM" in text
    assert len(tasks(ctx)) == 1 and not archived(tmp_path)


# --- the rest ---------------------------------------------------------------------------------


def test_partial_import_is_not_confirmed(tmp_path):
    ctx, server, pending = stuck(tmp_path, phase="partial")
    server.publish(staged(pending))
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm")
    assert code == 1 and "partial import still lacks files" in text


def test_leftover_files_are_listed_and_left_alone(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending), consume=False)
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


# --- review of 0.1.0-beta.2: history-only receipts follow the verifier's rules ---------------


def history_only(tmp_path, conversion=False):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending), conversion=conversion)
    without_baseline(ctx, pending)
    return ctx, server, pending


def refused(ctx, server, tmp_path, expected):
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm", "--accept-history")
    assert code == 1 and expected in text, text
    assert len(tasks(ctx)) == 1 and not archived(tmp_path)


def test_two_independent_imports_onto_one_output_link_nothing(tmp_path):
    ctx, server, _ = history_only(tmp_path)
    first, second = server.events
    second["data"].update(FileId=first["data"]["FileId"], ImportedPath=first["data"]["ImportedPath"], Size=first["data"]["Size"])
    server.bookfiles[:] = [r for r in server.bookfiles if str(r["id"]) == first["data"]["FileId"]]
    refused(ctx, server, tmp_path, "2 file(s) have no import on the book now")


def test_a_recorded_file_id_that_is_gone_is_not_replaced_by_its_path(tmp_path):
    ctx, server, _ = history_only(tmp_path)
    gone = server.bookfiles.pop()
    server.bookfiles.append(dict(gone, id=200))  # another file now at the same path
    refused(ctx, server, tmp_path, "1 file(s) have no import on the book now: chapter1.wav")


def test_a_recorded_size_that_disagrees_with_the_file_does_not_count(tmp_path):
    ctx, server, _ = history_only(tmp_path)
    server.events[1]["data"]["Size"] = "123"
    refused(ctx, server, tmp_path, "1 file(s) have no import on the book now: chapter1.wav")


def test_a_recorded_path_that_is_not_the_files_path_does_not_count(tmp_path):
    ctx, server, _ = history_only(tmp_path)
    server.events[1]["data"]["ImportedPath"] = "/server-only/library/elsewhere.m4b"
    refused(ctx, server, tmp_path, "1 file(s) have no import on the book now: chapter1.wav")


@pytest.mark.parametrize("count", ["99", "1", "two"])
def test_a_conversion_count_that_disagrees_with_its_sources_proves_nothing(tmp_path, count):
    ctx, server, _ = history_only(tmp_path, conversion=True)
    server.events[0]["data"]["SourceFileCount"] = count
    refused(ctx, server, tmp_path, "2 file(s) have no import on the book now")


def test_a_conversion_output_size_that_disagrees_proves_nothing(tmp_path):
    ctx, server, _ = history_only(tmp_path, conversion=True)
    server.events[0]["data"]["OutputSize"] = "5"
    refused(ctx, server, tmp_path, "2 file(s) have no import on the book now")


def test_a_consistent_conversion_is_accepted_from_history(tmp_path):
    ctx, server, _ = history_only(tmp_path, conversion=True)
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm", "--accept-history")
    assert code == 0 and "history only, not verified" in text


@pytest.mark.parametrize("field, value, what", [("bookId", 43, "book"), ("authorId", 8, "author"),
                                                ("mediaType", "ebook", "medium")])
def test_an_output_on_another_book_author_or_medium_blocks(tmp_path, field, value, what):
    ctx, server, _ = history_only(tmp_path)
    server.bookfiles[0][field] = value
    refused(ctx, server, tmp_path, f"on another {what}")


def test_an_output_with_another_edition_blocks(tmp_path):
    ctx, server, pending = history_only(tmp_path)
    pending["edition_id"] = 17
    save(ctx, pending)
    server.bookfiles[0]["editionId"] = 99
    refused(ctx, server, tmp_path, "on another edition")


# --- review of 0.1.0-beta.3: complete receipts, readable exclusions ---------------------------


@pytest.mark.parametrize("missing", [("ImportedPath",), ("Size",), ("ImportedPath", "Size")])
def test_a_history_receipt_must_record_its_output_path_and_size(tmp_path, missing):
    ctx, server, _ = history_only(tmp_path)
    for event in server.events:
        for field in missing:
            event["data"].pop(field)
    refused(ctx, server, tmp_path, "2 file(s) have no import on the book now")


def test_a_conversion_receipt_must_record_its_output_size(tmp_path):
    ctx, server, _ = history_only(tmp_path, conversion=True)
    server.events[0]["data"].pop("OutputSize")
    refused(ctx, server, tmp_path, "2 file(s) have no import on the book now")


@pytest.mark.parametrize("exclusions", [
    json.dumps([{"path": "PLACEHOLDER"}]),
    json.dumps(["PLACEHOLDER", 5]),
    json.dumps({"path": "PLACEHOLDER"}),
    "not json",
])
def test_an_unreadable_exclusion_list_makes_a_conversion_inconclusive(tmp_path, exclusions):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending), conversion=True)
    server.events[0]["data"]["ExcludedSourcePaths"] = exclusions.replace("PLACEHOLDER", staged(pending)[1])
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm")  # the import verifier itself
    assert code == 1 and "import verifier: verified" not in text
    without_baseline(ctx, pending)
    refused(ctx, server, tmp_path, "2 file(s) have no import on the book now")


def test_an_empty_exclusion_list_excludes_nothing(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending), conversion=True)
    server.events[0]["data"]["ExcludedSourcePaths"] = "[]"
    assert run(ctx, server, tmp_path, "--book", "42", "--confirm")[0] == 0


# --- review of 0.1.0-beta.4: the ordinary verifier and Chaptarr's own fields ------------------
# Chaptarr (HistoryService.cs) records on BookFileImported the size of the file it picked up
# (BookInfo.Size, else the imported file's, else 0), and writes ExcludedSourcePaths only when
# something was excluded, as a JSON list of paths.


def test_a_receipt_for_a_file_of_another_size_is_not_proof(tmp_path):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending))
    server.events[1]["data"]["Size"] = "999999"
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm")
    assert code == 1 and "import verifier: verified" not in text and len(tasks(ctx)) == 1


@pytest.mark.parametrize("size", [None, "", "0"])
def test_a_receipt_without_a_size_is_still_proof_for_the_verifier(tmp_path, size):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending))
    for event in server.events:
        if size is None:
            event["data"].pop("Size")
        else:
            event["data"]["Size"] = size
    assert run(ctx, server, tmp_path, "--book", "42", "--confirm")[0] == 0


def test_a_retagged_output_of_another_size_is_still_proof(tmp_path):
    # The receipt's Size is the staged file's; the library file grew when Chaptarr wrote tags
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending))
    for row in server.bookfiles:
        row["size"] += 4096
    assert run(ctx, server, tmp_path, "--book", "42", "--confirm")[0] == 0


def test_history_only_compares_the_receipt_size_with_the_staged_file(tmp_path):
    ctx, server, _ = history_only(tmp_path)
    for row in server.bookfiles:
        row["size"] += 4096  # retagged in the library: still the same import
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm", "--accept-history")
    assert code == 0 and "history only, not verified" in text


def test_history_only_needs_a_recorded_size_other_than_zero(tmp_path):
    ctx, server, _ = history_only(tmp_path)
    for event in server.events:
        event["data"]["Size"] = "0"
    refused(ctx, server, tmp_path, "2 file(s) have no import on the book now")


@pytest.mark.parametrize("exclusions", ["", "null", "{}", "[\"\"]"])
def test_an_exclusion_field_that_is_present_but_not_a_list_of_paths_is_inconclusive(tmp_path, exclusions):
    ctx, server, pending = stuck(tmp_path)
    server.publish(staged(pending), conversion=True)
    server.events[0]["data"]["ExcludedSourcePaths"] = exclusions
    code, text, _ = run(ctx, server, tmp_path, "--book", "42", "--confirm")
    assert code == 1 and "import verifier: verified" not in text
    without_baseline(ctx, pending)
    refused(ctx, server, tmp_path, "2 file(s) have no import on the book now")
