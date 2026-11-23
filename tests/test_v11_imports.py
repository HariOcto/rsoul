"""Durable import regressions using real audio headers and pinned Chaptarr schemas.

The HTTP client double models the API data, not the old count-only verifier.
History fixtures follow Chaptarr 04c1ed1fba2ba08b72ea5e80f85578eaa076ffe3.
"""

import configparser
import copy
import json
import os
import time
import wave
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from rsoul import import_identity, import_transaction, postprocess
from rsoul.persistence import StatePersistenceError
from rsoul.state import StateManager


def audio(path, value=1):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(int(value).to_bytes(2, "little", signed=True) * 800)


class Server:
    ver_uri = "/api/v1"

    def __init__(self, root):
        self.root = Path(root)
        self.book = {"id": 42, "authorId": 7, "title": "Intended Work", "mediaType": "audiobook", "foreignBookId": "work:stable"}
        self.author = {"id": 7, "authorName": "Correct Author", "foreignAuthorId": "author:stable"}
        self.editions = [{"id": 17, "monitored": True, "readingFormatId": 2, "foreignEditionId": "edition:stable"}]
        self.bookfiles, self.events, self.posts = [], [], []
        self.status, self.result = "completed", "successful"
        self.action = "all"
        self.preview_error = None
        self.lose_response = False
        self.before_post = None
        self.get_command_count = 0
        self.no_history = False

    def get_book(self, book_id):
        return copy.deepcopy(self.book)

    def get_author(self, author_id):
        return copy.deepcopy(self.author)

    def get_edition(self, book_id):
        return copy.deepcopy(self.editions)

    def local(self, path):
        return self.root / path.removeprefix("/remote/")

    def _get(self, resource, ver_uri=None, params=None):
        if resource == "bookfile":
            return copy.deepcopy(self.bookfiles)
        if resource == "history":
            if self.no_history:
                raise RuntimeError("history API unavailable")
            return {"records": copy.deepcopy(self.events), "totalRecords": len(self.events)}
        if resource == "manualimport":
            if self.preview_error:
                raise self.preview_error
            return [{"path": params["folder"] + "/" + p.name,
                     "quality": {"quality": {"id": 1, "name": "Audio"}}}
                    for p in self.local(params["folder"]).iterdir() if p.is_file()]
        raise AssertionError(resource)

    def publish(self, source_paths, conversion=False, consume=True, event_type=None):
        sources = [str(p) for p in source_paths]
        sizes = [self.local(p).stat().st_size if self.local(p).exists() else 1644 for p in sources]
        groups = [sources] if conversion else [[p] for p in sources]
        for group in groups:
            file_id = 100 + len(self.bookfiles)
            size = sum(sizes[sources.index(p)] for p in group)
            output = f"/server-only/library/{file_id}.m4b"
            self.bookfiles.append({"id": file_id, "bookId": 42, "authorId": 7, "mediaType": "audiobook", "path": output, "size": size})
            data = {"ImportedPath": output}
            if conversion:
                data.update(SourcePaths=json.dumps(group), SourceFileCount=str(len(group)), OutputSize=str(size))
            else:
                data.update(FileId=str(file_id), DroppedPath=group[0], Size=str(size))
            self.events.append({"id": file_id, "bookId": 42, "authorId": 7,
                                "date": datetime.now(timezone.utc).isoformat(),
                                "eventType": event_type if event_type is not None else ("bookFileConverted" if conversion else "bookFileImported"), "data": data})
        if consume:
            for source in sources:
                self.local(source).unlink(missing_ok=True)

    def post_command(self, name, **kwargs):
        if self.before_post:
            self.before_post(name, kwargs)
        self.posts.append((name, copy.deepcopy(kwargs)))
        sources = [f["path"] for f in kwargs.get("files", [])]
        if name == "DownloadedBooksScan":
            sources = [kwargs["path"] + "/" + p.name for p in self.local(kwargs["path"]).iterdir() if p.is_file()]
        if self.action == "all":
            self.publish(sources)
        elif self.action == "partial":
            self.publish(sources[:1])
        elif self.action == "convert":
            self.publish(sources, conversion=True)
        if self.lose_response:
            raise TimeoutError("accepted but response lost")
        return {"id": 900, "name": name}

    def get_command(self, command_id):
        self.get_command_count += 1
        return {"id": command_id, "status": self.status, "result": self.result}


def setup(tmp_path, count=2, state=True, media="audiobook"):
    root = tmp_path / "downloads"
    source = root / "peer-folder"
    source.mkdir(parents=True)
    names = [f"chapter{i}.wav" if media == "audiobook" else f"part{i}.txt" for i in range(count)]
    for i, name in enumerate(names):
        if media == "audiobook":
            audio(source / name, i + 1)
        else:
            (source / name).write_text("ebook part " + str(i))
    config = configparser.ConfigParser()
    config["Readarr"] = {"import_method": "targeted", "import_poll_timeout": "0"}
    config["Slskd"] = {"download_dir": str(root), "readarr_download_dir": "/remote"}
    config["Postprocessing"] = {"audiobook_tag_check": "off"}
    config["Search Settings"] = {}
    server = Server(root)
    server.book["mediaType"] = media
    if media == "ebook":
        server.editions[0]["readingFormatId"] = 3
    intended = {"book_id": 42, "author_id": 7, "foreign_book_id": "work:stable", "foreign_author_id": "author:stable",
                "title": "Intended Work", "author_name": "Correct Author", "media_type": media}
    item = {"task_id": "task-42", "bookId": 42, "title": "Intended Work", "author_name": "Correct Author",
            "media_type": media, "dir": "peer-folder", "filename": names[0], "expected_files": names,
            "backend_name": "slskd", "source_id": "peer", "_task_extra": {"intended_identity": intended, "media_type": media, "expected_files": names}}
    saved = None
    if state:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        saved = StateManager(str(state_dir))
        saved.add_task(SimpleNamespace(task_id="task-42", book_id=42, book_title="Intended Work", author_name="Correct Author",
                                      filename=names[0], backend_name="slskd", local_dir="peer-folder", series_title="", extra=item["_task_extra"]))
    penalties = []
    ctx = SimpleNamespace(config=config, readarr=server, state=saved, orchestrator=None,
                          history=SimpleNamespace(add_failure=lambda *a, **k: penalties.append((a, k))))
    return ctx, item, server, penalties


def saved_pending(ctx):
    return ctx.state.get_tasks_for_orchestrator()[0]["extra"]["import_pending"]


def writer(ctx):
    def save(value):
        extra = ctx.state.get_tasks_for_orchestrator()[0]["extra"]
        extra["import_pending"] = value
        ctx.state.set_extra("task-42", extra)
    return save


def run(ctx, item):
    return postprocess.process_imports(ctx, [item])[42]


def test_manifest_baseline_and_payload_are_durable_before_post(tmp_path):
    ctx, item, server, penalties = setup(tmp_path)
    def before_post(name, kwargs):
        disk = json.loads(ctx.state.filepath.read_text())[0]["extra"]["import_pending"]
        assert disk["phase"] == "submission_unknown"
        assert disk["before_ids"] == []
        assert len(disk["manifest"]) == 2
        assert all(len(f["sha256"]) == 64 and Path(f["stage_path"]).exists() for f in disk["manifest"])
        assert disk["attempts"][0]["payload"]["name"] == name
        assert disk["attempts"][0]["payload"]["files"] == kwargs["files"]
    server.before_post = before_post
    assert run(ctx, item) is True
    pending = saved_pending(ctx)
    assert pending["phase"] == "verified" and pending["attempts"][0]["command_id"] == 900
    assert len(pending["verification"]["matched"]) == 2
    assert not penalties


def test_partial_import_retained_and_never_penalizes_peer(tmp_path):
    ctx, item, server, penalties = setup(tmp_path)
    server.action = "partial"
    result = run(ctx, item)
    assert result["phase"] == "partial"
    assert len(result["verification"]["matched"]) == 1
    assert sum(Path(p).exists() for p in result["files"]) == 1
    assert not (Path(result["download_root"]) / "failed_imports").exists()
    assert not penalties


def test_unrelated_fresh_file_on_same_book_is_not_success(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    server.action = "none"
    server.bookfiles = [{"id": 55, "bookId": 42, "path": "/unrelated/preexisting", "size": 10}]
    pending = run(ctx, item)
    server.bookfiles.append({"id": 56, "bookId": 42, "path": "/unrelated/new", "size": 10})
    for path in pending["files"]:
        Path(path).unlink()
    result = postprocess.check_pending_import(server, pending, config=ctx.config, persist=writer(ctx))
    assert result["phase"] == "needs_review"
    assert result["before_ids"] == ["55"]
    assert result["verification"]["status"] == "unverified"


@pytest.mark.parametrize("event_type", ["bookFileConverted", 11])
def test_explicit_complete_conversion_history_accounts_for_all_inputs(tmp_path, event_type):
    ctx, item, server, _ = setup(tmp_path)
    server.action = "none"
    pending = run(ctx, item)
    server.publish([f["readarr_path"] for f in pending["manifest"]], conversion=True, event_type=event_type)
    assert postprocess.check_pending_import(server, pending, config=ctx.config, persist=writer(ctx)) is True
    proof = saved_pending(ctx)["verification"]
    assert proof["conversion"] is True and len(proof["output_ids"]) == 1 and len(proof["matched"]) == 2


@pytest.mark.parametrize("damage", ["missing_source", "extra_source", "wrong_count", "failed", "wrong_size", "excluded_source"])
def test_incomplete_or_failed_conversion_is_not_verified(tmp_path, damage):
    ctx, item, server, _ = setup(tmp_path)
    server.action = "none"
    pending = run(ctx, item)
    paths = [f["readarr_path"] for f in pending["manifest"]]
    server.publish(paths, conversion=True)
    event, data = server.events[0], server.events[0]["data"]
    if damage == "missing_source":
        data.update(SourcePaths=json.dumps(paths[:1]), SourceFileCount="1")
    elif damage == "extra_source":
        data.update(SourcePaths=json.dumps(paths + ["/elsewhere/unwanted.wav"]), SourceFileCount="3")
    elif damage == "wrong_count":
        data["SourceFileCount"] = "1"
    elif damage == "failed":
        event["eventType"] = 12
    elif damage == "wrong_size":
        data["OutputSize"] = "1"
    else:
        data["ExcludedSourcePaths"] = json.dumps(paths[:1])
    result = postprocess.check_pending_import(server, pending, config=ctx.config, persist=writer(ctx))
    assert isinstance(result, dict) and result["phase"] in ("partial", "needs_review")


def test_one_output_with_two_ordinary_import_events_is_not_a_conversion(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    server.action = "none"
    pending = run(ctx, item)
    paths = [f["readarr_path"] for f in pending["manifest"]]
    server.publish(paths[:1])
    second = copy.deepcopy(server.events[0])
    second["data"]["DroppedPath"] = paths[1]
    server.events.append(second)
    proof = import_identity.verify_import_evidence(server, pending)
    assert proof["status"] == "unverified"


@pytest.mark.parametrize("event_type", ["bookFileImported", 3])
def test_generated_conversion_source_paths_on_imported_event_are_supported(tmp_path, event_type):
    # HistoryService adds GeneratedConversionSourcePaths to the successful
    # per-file import event, together with final FileId, ImportedPath and Size.
    ctx, item, server, _ = setup(tmp_path)
    server.action = "none"
    pending = run(ctx, item)
    paths = [f["readarr_path"] for f in pending["manifest"]]
    server.publish(paths, conversion=True, event_type=event_type)
    event, row = server.events[0], server.bookfiles[0]
    event["data"] = {"SourcePaths": json.dumps(paths), "FileId": str(row["id"]),
                     "ImportedPath": row["path"], "Size": str(row["size"]),
                     "DroppedPath": "/server-generated/merged.m4b"}
    assert import_identity.verify_import_evidence(server, pending)["status"] == "verified"


@pytest.mark.parametrize("damage", ["folder_summary", "missing_file_id", "wrong_size"])
def test_generated_import_conversion_requires_successful_per_output_receipt(tmp_path, damage):
    ctx, item, server, _ = setup(tmp_path)
    server.action = "none"
    pending = run(ctx, item)
    paths = [f["readarr_path"] for f in pending["manifest"]]
    server.publish(paths, conversion=True, event_type="bookFileImported")
    event, row = server.events[0], server.bookfiles[0]
    event["data"].update(FileId=str(row["id"]), Size=str(row["size"]))
    if damage == "folder_summary":
        event["eventType"] = "downloadFolderImported"
    elif damage == "missing_file_id":
        del event["data"]["FileId"]
    else:
        event["data"]["Size"] = "1"
    assert import_identity.verify_import_evidence(server, pending)["status"] == "unverified"


def test_ordinary_event_cannot_enlarge_a_partial_conversion_receipt(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    server.action = "none"
    pending = run(ctx, item)
    paths = [f["readarr_path"] for f in pending["manifest"]]
    server.publish(paths[:1], conversion=True)
    second = copy.deepcopy(server.events[0])
    second["eventType"] = "bookFileImported"
    second["data"] = {"FileId": str(server.bookfiles[0]["id"]), "ImportedPath": server.bookfiles[0]["path"], "DroppedPath": paths[1]}
    server.events.append(second)
    assert import_identity.verify_import_evidence(server, pending)["status"] == "unverified"


def test_complete_readable_output_hashes_can_verify_when_history_is_unavailable(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    server.action = "none"
    pending = run(ctx, item)
    output_dir = tmp_path / "library"
    output_dir.mkdir()
    for index, entry in enumerate(pending["manifest"]):
        final_path = output_dir / str(index)
        shutil.copyfile(entry["stage_path"], final_path)
        server.bookfiles.append({"id": index + 100, "bookId": 42, "path": str(final_path), "size": entry["size"]})
    server.no_history = True
    assert postprocess.check_pending_import(server, pending, config=ctx.config, persist=writer(ctx)) is True
    assert len(saved_pending(ctx)["retained_inputs"]) == 2  # Copied sources are preserved, not auto-deleted.


def test_lost_post_response_reconciles_original_baseline_without_resubmission(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    server.action, server.lose_response = "none", True
    pending = run(ctx, item)
    assert pending["phase"] == "submission_unknown" and pending["command_id"] is None
    baseline, attempt = copy.deepcopy(pending["before_ids"]), copy.deepcopy(pending["attempts"])
    ctx.state = StateManager(str(ctx.state.filepath.parent))  # a real process-style reload
    server.publish([f["readarr_path"] for f in pending["manifest"]])
    assert postprocess.check_pending_import(server, saved_pending(ctx), config=ctx.config, persist=writer(ctx)) is True
    assert len(server.posts) == 1
    assert saved_pending(ctx)["before_ids"] == baseline
    assert saved_pending(ctx)["attempts"] == attempt


def test_unknown_submission_never_retries_when_output_evidence_is_unavailable(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    server.action, server.lose_response = "none", True
    pending = run(ctx, item)
    for _ in range(3):
        pending = postprocess.check_pending_import(server, pending, config=ctx.config, persist=writer(ctx))
    assert pending["phase"] == "submission_unknown" and len(server.posts) == 1
    assert pending["before_ids"] == [] and len(pending["attempts"]) == 1


def test_lost_response_after_actual_complete_import_can_be_verified_immediately(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    server.lose_response = True
    assert run(ctx, item) is True
    assert len(server.posts) == 1 and saved_pending(ctx)["attempts"][0]["state"] == "submission_unknown"


def test_active_command_is_checked_after_review_age_and_inputs_are_not_moved(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    server.action, server.status = "none", "started"
    pending = run(ctx, item)
    count = server.get_command_count
    outcome = postprocess.check_pending_import(server, pending, now=pending["since"] + import_transaction.MAX_AGE + 1,
                                               config=ctx.config, persist=writer(ctx))
    assert outcome["phase"] == "importing" and outcome["overdue"]
    assert server.get_command_count > count
    assert all(Path(p).exists() for p in outcome["files"])
    assert len(server.posts) == 1


def test_disable_sync_honored_before_pending_preparation_retry(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    server.preview_error = TimeoutError("temporary preview outage")
    pending = run(ctx, item)
    assert pending["phase"] == "staged" and not server.posts
    server.preview_error = None
    ctx.config["Readarr"]["disable_sync"] = "True"
    result = postprocess.check_pending_import(server, pending, config=ctx.config, persist=writer(ctx))
    assert result["phase"] == "staged" and not server.posts
    assert all(Path(p).exists() for p in result["files"])


def test_disabled_sync_new_download_is_durably_paused_for_later_enable(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    ctx.config["Readarr"]["disable_sync"] = "True"
    pending = run(ctx, item)
    assert pending["phase"] == "validating" and not server.posts
    assert saved_pending(ctx)["manifest"] == pending["manifest"]
    assert all((server.root / "peer-folder" / n).exists() for n in item["expected_files"])
    ctx.config["Readarr"]["disable_sync"] = "False"
    assert postprocess.check_pending_import(server, saved_pending(ctx), config=ctx.config, persist=writer(ctx)) is True


@pytest.mark.parametrize("field", ["full_dir", "file_dir"])
def test_validation_restart_keeps_original_peer_directory_recording_evidence(tmp_path, field):
    ctx, item, server, _ = setup(tmp_path)
    ctx.config["Readarr"]["disable_sync"] = "True"
    ctx.config["Search Settings"]["audiobook_languages"] = "en"
    item["_task_extra"][field] = "Audiobooks\\Correct Author\\Intended Work [German]"
    pending = run(ctx, item)
    assert pending["phase"] == "validating"
    ctx.config["Readarr"]["disable_sync"] = "False"
    result = postprocess.check_pending_import(server, saved_pending(ctx), config=ctx.config, persist=writer(ctx))
    assert result["phase"] == "needs_review"
    assert "recording language" in result["reason"]
    assert not server.posts
    assert all(Path(f["source_path"]).exists() for f in result["manifest"])


@pytest.mark.parametrize("field", ["title", "authorId", "foreignBookId", "mediaType", "foreignAuthorId"])
def test_changed_stable_identity_prevents_retry_and_false_readback(tmp_path, field):
    ctx, item, server, penalties = setup(tmp_path)
    server.preview_error = TimeoutError("temporary preview outage")
    pending = run(ctx, item)
    server.preview_error = None
    if field == "foreignAuthorId":
        server.author[field] = "other-author"
    else:
        server.book[field] = {"title": "Different Work", "authorId": 8, "foreignBookId": "other-work", "mediaType": "ebook"}[field]
    result = postprocess.check_pending_import(server, pending, config=ctx.config, persist=writer(ctx))
    assert result["phase"] == "needs_review" and not server.posts and not penalties


def test_provider_ebook_edition_on_audiobook_record_is_still_supported(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    server.editions[0]["readingFormatId"] = 3
    assert run(ctx, item) is True
    assert server.posts[0][1]["files"][0]["editionId"] == 17
    assert all("editionId" not in f for f in server.bookfiles)


def test_persistence_failure_prevents_every_staging_move(tmp_path, monkeypatch):
    ctx, item, server, _ = setup(tmp_path)
    monkeypatch.setattr(ctx.state, "set_extra", lambda *a, **k: (_ for _ in ()).throw(StatePersistenceError("disk full")))
    with pytest.raises(StatePersistenceError):
        run(ctx, item)
    assert all((server.root / "peer-folder" / n).exists() for n in item["expected_files"])
    assert not server.posts


def test_persistence_failure_of_post_intent_prevents_post(tmp_path, monkeypatch):
    ctx, item, server, _ = setup(tmp_path)
    actual = ctx.state.set_extra
    def fail_before_post(task_id, extra):
        if extra["import_pending"]["phase"] == "submission_unknown":
            raise StatePersistenceError("cannot save POST intent")
        actual(task_id, extra)
    monkeypatch.setattr(ctx.state, "set_extra", fail_before_post)
    with pytest.raises(StatePersistenceError):
        run(ctx, item)
    assert not server.posts
    assert saved_pending(ctx)["phase"] == "staged"
    assert all(Path(p).exists() for p in saved_pending(ctx)["files"])


def test_crash_after_first_staging_move_resumes_exact_manifest(tmp_path, monkeypatch):
    ctx, item, server, _ = setup(tmp_path)
    actual, count = os.rename, 0
    def crash(src, dst):
        nonlocal count
        count += 1
        if count == 2:
            raise KeyboardInterrupt("simulated process death between moves")
        return actual(src, dst)
    monkeypatch.setattr(import_transaction.os, "rename", crash)
    with pytest.raises(KeyboardInterrupt):
        run(ctx, item)
    first = saved_pending(ctx)
    assert first["phase"] == "staging" and first["inputs_validated"]
    assert sum(Path(p).exists() for p in first["files"]) == 1
    monkeypatch.setattr(import_transaction.os, "rename", actual)
    ctx.state = StateManager(str(ctx.state.filepath.parent))
    assert postprocess.check_pending_import(server, saved_pending(ctx), config=ctx.config, persist=writer(ctx)) is True
    assert len(server.posts) == 1
    assert saved_pending(ctx)["manifest"] == first["manifest"]


def test_crash_before_validation_does_not_bypass_validation_on_restart(tmp_path, monkeypatch):
    ctx, item, server, _ = setup(tmp_path)
    actual = import_transaction.validate_inputs
    monkeypatch.setattr(import_transaction, "validate_inputs", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run(ctx, item)
    pending = saved_pending(ctx)
    assert pending["phase"] == "validating"
    # Malformed replacement before restart must be inspected and refused.
    (server.root / "peer-folder" / item["expected_files"][0]).write_bytes(b"not playable")
    monkeypatch.setattr(import_transaction, "validate_inputs", actual)
    result = postprocess.check_pending_import(server, pending, config=ctx.config, persist=writer(ctx))
    assert result["phase"] == "needs_review" and "audio" in result["reason"].lower()
    assert not server.posts


def test_invalid_audio_is_rejected_even_when_tag_checks_are_off(tmp_path):
    ctx, item, server, penalties = setup(tmp_path)
    (server.root / "peer-folder" / item["expected_files"][0]).write_bytes(b"fake audio")
    result = run(ctx, item)
    assert result["phase"] == "needs_review" and "audio" in result["reason"].lower()
    assert not server.posts and not penalties


def test_missing_file_is_retained_without_peer_penalty(tmp_path):
    ctx, item, server, penalties = setup(tmp_path)
    (server.root / "peer-folder" / item["expected_files"][0]).unlink()
    result = run(ctx, item)
    assert result["phase"] == "needs_review"
    assert not server.posts and not penalties


def test_download_manifest_size_is_checked_before_new_import_baseline(tmp_path):
    ctx, item, server, penalties = setup(tmp_path)
    item["_task_extra"]["manifest"] = [
        {"name": name, "filename": "remote\\" + name, "size": (server.root / "peer-folder" / name).stat().st_size + 100}
        for name in item["expected_files"]]
    result = run(ctx, item)
    assert result["phase"] == "needs_review" and "expected download size" in result["reason"]
    assert not server.posts and not penalties


def test_each_new_operation_has_a_distinct_staging_and_server_path(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    first = import_transaction.new_transaction(item, str(server.root), "/remote", "targeted")
    second = import_transaction.new_transaction(item, str(server.root), "/remote", "targeted")
    assert first["local_dir"] != second["local_dir"]
    assert first["readarr_path"] != second["readarr_path"]
    assert first["operation_id"] in first["local_dir"]


def test_sidecar_can_be_adopted_once_without_reissuing_unknown_post(tmp_path):
    ctx, item, server, _ = setup(tmp_path, state=False)
    server.action, server.lose_response = "none", True
    result = run(ctx, item)
    assert Path(result["journal_path"]).exists()
    directory = tmp_path / "state"
    directory.mkdir()
    ctx.state = StateManager(str(directory))
    assert postprocess.recover_pending_imports(ctx) == 1
    assert saved_pending(ctx)["phase"] == "submission_unknown"
    assert postprocess.recover_pending_imports(ctx) == 0
    assert len(server.posts) == 1


def test_legacy_pending_without_baseline_never_invents_success_or_retries(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    source = server.root / "peer-folder"
    pending = {"method": "targeted", "folder": "peer-folder", "local_dir": str(source), "readarr_path": "/remote/peer-folder",
               "files": [str(source / n) for n in item["expected_files"]], "command_id": None, "submits": 0,
               "book_id": 42, "since": time.time() - 100, "media_type": "audiobook", "intended_identity": item["_task_extra"]["intended_identity"]}
    result = postprocess.check_pending_import(server, pending, config=ctx.config, persist=writer(ctx))
    assert result["legacy"] and result["phase"] == "submission_unknown"
    assert result.get("before_ids") is None and not server.posts


def test_old_staged_task_is_preserved_for_review_when_backend_cannot_resume(tmp_path):
    ctx, item, server, penalties = setup(tmp_path)
    stage = server.root / "rsoul_audiobooks" / "Correct Author" / "Intended Work"
    stage.mkdir(parents=True)
    for name in item["expected_files"]:
        os.rename(server.root / "peer-folder" / name, stage / name)
    record = ctx.state.get_tasks_for_orchestrator()[0]
    result = postprocess.preserve_unresumable_task(ctx, record, "Transfer no longer exists")
    assert result["phase"] == "needs_review" and str(stage) in result["candidate_directories"]
    assert saved_pending(ctx)["manifest"] == []
    assert all((stage / n).exists() for n in item["expected_files"])
    assert not penalties and not server.posts


def test_explicit_scan_also_requires_full_correlated_outputs(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    ctx.config["Readarr"]["import_method"] = "scan"
    server.action = "partial"
    result = run(ctx, item)
    assert server.posts[0][0] == "DownloadedBooksScan"
    assert result["phase"] == "partial"


def test_count_only_compatibility_helper_never_certifies_success(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    server.bookfiles = [{"id": 100, "bookId": 42, "path": "/unrelated", "size": 100}]
    assert import_identity.verify_new_bookfiles(server, 42, 17, [], expected_files=2) is None


def test_preexisting_matching_files_are_not_counted_as_this_import(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    server.action = "none"
    pending = run(ctx, item)
    server.publish([f["readarr_path"] for f in pending["manifest"]], consume=False)
    pending["before_ids"] = [str(r["id"]) for r in server.bookfiles]
    assert import_identity.verify_import_evidence(server, pending)["status"] == "unverified"


def test_wrong_file_id_does_not_fall_back_to_matching_path(tmp_path):
    ctx, item, server, _ = setup(tmp_path)
    server.action = "none"
    pending = run(ctx, item)
    server.publish([f["readarr_path"] for f in pending["manifest"]])
    for event in server.events:
        event["data"]["FileId"] = "999999"
    assert import_identity.verify_import_evidence(server, pending)["status"] == "unverified"
