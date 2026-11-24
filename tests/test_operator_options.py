"""Operator conveniences added from an outside suggestion list, each opt-in or behaviour-neutral
by default: webhook notifications, a clear cross-device staging error, a narrator tie-break between
audio editions, following submitted imports together, a configurable bitrate floor, an optional
running-time check, optional retention for quarantine folders, and an evidence-checked --discard."""

import configparser
import errno
import io
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_recover_apply import STAGE, legacy, pending as saved_pending, run

from rsoul import import_identity, import_transaction, notify, postprocess, recover
from rsoul.backends.base import DownloadTarget
from rsoul.backends.slskd_backend import SlskdBackend
from rsoul.config import apply_env_overrides, validate_config
from rsoul.library import CatalogUnavailable


@pytest.fixture(autouse=True)
def no_notifications():
    notify.configure(None)
    yield
    notify.configure(None)
    assert notify.flush(1)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class Posted:
    def __init__(self, status=204, error=None):
        self.calls, self.status, self.error = [], status, error

    def __call__(self, url, json=None, timeout=None, **kwargs):
        self.calls.append((url, json, timeout))
        if self.error:
            raise self.error
        return SimpleNamespace(status_code=self.status, close=lambda: None)


def webhook_config(events=None):
    config = configparser.ConfigParser()
    config["Notifications"] = {"webhook_url": "https://hooks.example/abc"}
    if events is not None:
        config["Notifications"]["events"] = events
    return config


def test_no_webhook_means_no_request(monkeypatch):
    posted = Posted()
    monkeypatch.setattr(notify, "_post_headers_only", posted)
    notify.configure(configparser.ConfigParser())
    assert notify.send("needs_review", "t", "m") is False
    assert posted.calls == []


def test_webhook_gets_the_event_with_a_short_timeout(monkeypatch):
    posted = Posted()
    monkeypatch.setattr(notify, "_post_headers_only", posted)
    notify.configure(webhook_config())
    assert notify.send("needs_review", "Book 7", "reason") is True
    assert notify.flush(1)
    url, payload, timeout = posted.calls[0]
    assert url == "https://hooks.example/abc" and timeout == 3
    assert payload["event"] == "needs_review" and "Book 7" in payload["content"] and payload["text"] == payload["content"]
    assert notify.send("partial", "Book 7", "x") is False  # not in the default events


def test_a_failing_webhook_never_breaks_the_run(monkeypatch, caplog):
    monkeypatch.setattr(notify, "_post_headers_only", Posted(error=ConnectionError("down")))
    notify.configure(webhook_config())
    assert notify.send("needs_review", "t", "m") is True  # accepted by the queue
    assert notify.flush(1)
    assert "could not be delivered" in caplog.text
    monkeypatch.setattr(notify, "_post_headers_only", Posted(status=500))
    assert notify.send("needs_review", "t", "m") is True
    assert notify.flush(1)
    assert "was refused" in caplog.text


def test_a_record_is_announced_when_it_enters_review_not_on_every_run(monkeypatch):
    posted = Posted()
    monkeypatch.setattr(notify, "_post_headers_only", posted)
    notify.configure(webhook_config("needs_review,submission_unknown"))
    record = {"phase": "staged", "book_id": 6744, "intended_identity": {"author_name": "Iain M. Banks", "title": "The Algebraist"}}
    import_transaction._retain(record, "needs_review", "no safe edition", persist=lambda value: None)
    import_transaction._retain(record, "needs_review", "no safe edition", persist=lambda value: None)
    assert notify.flush(1)
    assert len(posted.calls) == 1 and "Iain M. Banks - The Algebraist" in posted.calls[0][1]["content"]


def test_the_webhook_url_is_hidden_in_the_startup_log():
    config = configparser.ConfigParser()
    lines = apply_env_overrides(config, {"RSOUL__NOTIFICATIONS__WEBHOOK_URL": "https://discord.com/api/webhooks/1/secret"})
    assert lines and "secret" not in lines[0] and "(hidden)" in lines[0]


# ---------------------------------------------------------------------------
# Cross-device staging
# ---------------------------------------------------------------------------


def staging_record(tmp_path):
    root = tmp_path / "dl"
    (root / "peer").mkdir(parents=True)
    (root / "peer" / "a.mp3").write_bytes(b"x" * 10)
    import hashlib
    digest = hashlib.sha256(b"x" * 10).hexdigest()
    return {"phase": "staging", "inputs_validated": True, "download_root": str(root), "local_dir": str(root / "stage"),
            "manifest": [{"name": "a.mp3", "source_path": str(root / "peer" / "a.mp3"), "stage_path": str(root / "stage" / "a.mp3"),
                          "size": 10, "sha256": digest}], "book_id": 1}


def test_a_move_across_filesystems_is_set_aside_with_a_clear_reason(tmp_path, monkeypatch):
    def cross_device(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")
    monkeypatch.setattr(import_transaction.os, "rename", cross_device)
    result = import_transaction.recover_staging(staging_record(tmp_path), persist=lambda value: None)
    assert result["phase"] == "needs_review" and "same mount" in result["reason"]


def test_other_move_errors_still_resume_later(tmp_path, monkeypatch):
    def busy(src, dst):
        raise OSError(errno.EBUSY, "busy")
    monkeypatch.setattr(import_transaction.os, "rename", busy)
    assert import_transaction.recover_staging(staging_record(tmp_path), persist=lambda value: None)["phase"] == "staging"


# ---------------------------------------------------------------------------
# Narrator tie-break
# ---------------------------------------------------------------------------


AUDIO = {"id": 9, "mediaType": "audiobook"}


def test_narrator_tags_pick_one_of_several_audio_editions():
    editions = [{"id": 1, "readingFormatId": 2, "narrator": "Murray Porter"},
                {"id": 2, "readingFormatId": 2, "narratorNames": ["Ray Porter", "Jane Doe"]}]
    assert import_identity.select_edition(AUDIO, editions, "audiobook", ["Ray Porter"])["id"] == 2
    assert import_identity.select_edition(AUDIO, editions, "audiobook", ["Jane Doe; Ray Porter"])["id"] == 2
    # Two multiword comma fields could also be one compound inverted name.
    assert import_identity.select_edition(AUDIO, editions, "audiobook", ["Jane Doe, Ray Porter"]) is None


@pytest.mark.parametrize("heard", [["Ray"], ["Porter"], [], None, ["Someone Else"]])
def test_no_tie_break_without_a_whole_unambiguous_name(heard):
    editions = [{"id": 1, "readingFormatId": 2, "narrator": "Murray Porter"}, {"id": 2, "readingFormatId": 2, "narrator": "Ray Porter"}]
    assert import_identity.select_edition(AUDIO, editions, "audiobook", heard) is None


def test_two_editions_with_the_same_narrator_stay_ambiguous():
    editions = [{"id": 1, "readingFormatId": 2, "narrator": "Peter Kenny"}, {"id": 2, "readingFormatId": 2, "narrator": "Peter Kenny"}]
    assert import_identity.select_edition(AUDIO, editions, "audiobook", ["Peter Kenny"]) is None


def test_the_one_monitored_edition_still_comes_first():
    editions = [{"id": 1, "readingFormatId": 2, "monitored": True, "narrator": "A Person"}, {"id": 2, "readingFormatId": 2, "narrator": "Peter Kenny"}]
    assert import_identity.select_edition(AUDIO, editions, "audiobook", ["Peter Kenny"])["id"] == 1


# ---------------------------------------------------------------------------
# Submitted imports followed together
# ---------------------------------------------------------------------------


def test_all_imports_of_a_batch_are_submitted_before_any_is_waited_for(monkeypatch):
    events = []
    def fake_process_one(ctx, item, root, server, method, poll=True):
        assert poll is False
        events.append(("submit", item["bookId"]))
        return {"phase": "importing", "book_id": item["bookId"]}
    def fake_check(client, pending, config=None, persist=None):
        events.append(("check", pending["book_id"]))
        return True
    monkeypatch.setattr(import_transaction, "process_one", fake_process_one)
    monkeypatch.setattr(import_transaction, "check_pending", fake_check)
    monkeypatch.setattr(import_transaction, "context_writer", lambda ctx, item, pending: None)
    config = configparser.ConfigParser()
    config["Slskd"] = {"download_dir": "/dl"}
    ctx = SimpleNamespace(config=config, readarr=None, state=None, orchestrator=None)
    results = postprocess.process_imports(ctx, [{"bookId": 1}, {"bookId": 2}])
    assert results == {1: True, 2: True}
    assert events == [("submit", 1), ("submit", 2), ("check", 1), ("check", 2)]


# ---------------------------------------------------------------------------
# Bitrate floor and running-time check
# ---------------------------------------------------------------------------


def target(minutes):
    return DownloadTarget(book_id=1, book_title="X", author_name="Y", series_title="", allowed_filetypes=["mp3"],
                          media_type="audiobook", readarr_book={"durationMinutes": minutes})


def test_bitrate_floor_defaults_to_8_and_can_be_changed_or_switched_off():
    ten_hours, size_10kbps = 600, int(10 * 1000 / 8 * 600 * 60)
    assert SlskdBackend._too_small_for_duration(target(ten_hours), size_10kbps) == ""  # 10 kbit/s passes the default 8
    assert "minimum 16" in SlskdBackend._too_small_for_duration(target(ten_hours), size_10kbps, 16)
    assert SlskdBackend._too_small_for_duration(target(ten_hours), 1, 0) == ""


def drift_ctx(tolerance=None, minutes=600, error=None):
    config = configparser.ConfigParser()
    config["Search Settings"] = {} if tolerance is None else {"audiobook_duration_tolerance": str(tolerance)}
    def get_book(book_id):
        if error:
            raise error
        return {"durationMinutes": minutes}
    return SimpleNamespace(config=config, readarr=SimpleNamespace(get_book=get_book))


def test_running_time_check_is_off_by_default():
    assert import_transaction._runtime_drift(drift_ctx(), {"book_id": 1}, [{"duration_seconds": 60}]) == ""


def test_running_time_check_when_switched_on():
    hours = lambda h: [{"duration_seconds": h * 3600 / 2}, {"duration_seconds": h * 3600 / 2}]
    assert "differs from Chaptarr's 10.00 h by 30%" in import_transaction._runtime_drift(drift_ctx(0.25), {"book_id": 1}, hours(7))
    assert import_transaction._runtime_drift(drift_ctx(0.25), {"book_id": 1}, hours(8)) == ""
    assert import_transaction._runtime_drift(drift_ctx(0.25, minutes=0), {"book_id": 1}, hours(1)) == ""  # no length known
    assert import_transaction._runtime_drift(drift_ctx(0.25), {"book_id": 1}, [{"duration_seconds": None}]) == ""
    with pytest.raises(CatalogUnavailable):
        import_transaction._runtime_drift(drift_ctx(0.25, error=ConnectionError("down")), {"book_id": 1}, hours(7))


@pytest.mark.parametrize("option", ["audiobook_min_bitrate_kbps", "audiobook_duration_tolerance"])
def test_new_search_settings_are_validated(option):
    from test_download_cleanup_and_retries import make_config
    config = make_config(search={option: "-1"})
    with pytest.raises(ValueError, match=option):
        validate_config(config)


# ---------------------------------------------------------------------------
# Quarantine retention
# ---------------------------------------------------------------------------


def retention_state(tmp_path):
    from rsoul.state import StateManager
    directory = tmp_path / "retention-state"
    directory.mkdir()
    state = StateManager(str(directory))
    state.ensure_writable()
    return state


def quarantined(tmp_path, name="Book", age_days=None):
    root = tmp_path / "dl"
    (root / "peer").mkdir(parents=True, exist_ok=True)
    (root / "peer" / f"{name}.mp3").write_bytes(b"x")
    target = postprocess.move_files_aside(str(root), "peer", [f"{name}.mp3"], bucket="failed_imports")
    if age_days is not None:
        (open(os.path.join(target, postprocess.QUARANTINE_MARKER), "w")).write(f"{time.time() - age_days * 86400:.0f}\n")
    return root, target


def test_quarantine_marker_is_written(tmp_path):
    _, target = quarantined(tmp_path)
    with open(os.path.join(target, postprocess.QUARANTINE_MARKER)) as stream:
        assert abs(float(stream.read()) - time.time()) < 60


def test_retention_is_off_by_default_and_deletes_only_old_folders(tmp_path):
    root, old = quarantined(tmp_path, "Old", age_days=40)
    _, new = quarantined(tmp_path, "New", age_days=5)
    state = retention_state(tmp_path)
    assert postprocess.prune_quarantine(str(root), 0) == 0 and os.path.isdir(old)
    assert postprocess.prune_quarantine(str(root), 30, state) == 1
    assert not os.path.exists(old) and os.path.isdir(new)


def test_a_folder_without_a_marker_starts_its_clock_instead_of_being_deleted(tmp_path):
    root = tmp_path / "dl"
    legacy_folder = root / "failed_imports" / "Robot Dreams"
    legacy_folder.mkdir(parents=True)
    (legacy_folder / "a.mp3").write_bytes(b"x")
    os.utime(legacy_folder, (0, 0))  # moved in whole, ancient modification time
    assert postprocess.prune_quarantine(str(root), 30, retention_state(tmp_path)) == 0
    assert (legacy_folder / "a.mp3").exists() and (legacy_folder / postprocess.QUARANTINE_MARKER).exists()


def test_a_folder_a_saved_task_refers_to_is_kept(tmp_path):
    root, old = quarantined(tmp_path, "Old", age_days=40)
    state = SimpleNamespace(read_only=False, load_error=None, get_tasks_for_orchestrator=lambda: [{"extra": {"import_pending": {"local_dir": old}}}])
    assert postprocess.prune_quarantine(str(root), 30, state) == 0 and os.path.isdir(old)
    state.read_only = True  # dry run: never deletes
    state.get_tasks_for_orchestrator = lambda: []
    assert postprocess.prune_quarantine(str(root), 30, state) == 0 and os.path.isdir(old)


def test_symlinks_in_the_buckets_are_never_followed(tmp_path):
    root, _ = quarantined(tmp_path, "Old", age_days=40)
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / postprocess.QUARANTINE_MARKER).write_text("0\n")
    os.symlink(outside, root / "failed_imports" / "link")
    assert postprocess.prune_quarantine(str(root), 30, retention_state(tmp_path)) == 1
    assert outside.is_dir()


# ---------------------------------------------------------------------------
# recover --discard
# ---------------------------------------------------------------------------


def discard(ctx, server, tmp_path):
    out = io.StringIO()
    code = recover.main(["--config-dir", str(tmp_path), "--book", "42", "--discard"], client=server, state=ctx.state, out=out,
                        local_root=ctx.config["Slskd"]["download_dir"], server_root=ctx.config["Slskd"]["readarr_download_dir"])
    return code, out.getvalue()


def test_discard_archives_moves_aside_and_removes_the_task(tmp_path):
    ctx, server, root = legacy(tmp_path)
    record = saved_pending(ctx)
    code, text = discard(ctx, server, tmp_path)
    assert code == 0 and "Discarded" in text
    assert ctx.state.get_tasks_for_orchestrator() == []
    archived = [json.loads(line) for line in (tmp_path / "recover_archive.jsonl").read_text().splitlines()]
    assert archived[0]["action"] == "discard" and archived[0]["task"]["extra"]["import_pending"] == record
    aside = root / "failed_imports"
    assert any(p.name.endswith(".wav") for p in aside.rglob("*")) and any(p.name == postprocess.QUARANTINE_MARKER for p in aside.rglob("*"))
    assert not any((root / STAGE).glob("*.wav"))


def test_discard_needs_the_same_clear_evidence_as_apply(tmp_path):
    ctx, server, root = legacy(tmp_path)
    names = ctx.state.get_tasks_for_orchestrator()[0]["extra"]["expected_files"]
    server.command_list = [{"id": 5, "name": "ManualImport", "status": "started", "body": {"files": [{"path": "/remote/" + STAGE + "/" + n} for n in names]}}]
    code, text = discard(ctx, server, tmp_path)
    assert code == 1 and "BLOCKS DISCARD" in text and "still started" in text
    assert len(ctx.state.get_tasks_for_orchestrator()) == 1 and not (tmp_path / "recover_archive.jsonl").exists()
    assert all((root / STAGE / n).exists() for n in names)


def test_discard_refuses_an_import_in_progress_or_a_running_download(tmp_path):
    ctx, server, _ = legacy(tmp_path)
    task = ctx.state.get_tasks_for_orchestrator()[0]
    ctx.state.set_extra(task["task_id"], dict(task["extra"], import_pending=dict(saved_pending(ctx), schema=2, phase="importing")))
    code, text = discard(ctx, server, tmp_path)
    assert code == 1 and "phase 'importing'" in text
    extra = dict(task["extra"])
    extra.pop("import_pending")
    ctx.state.set_extra(task["task_id"], extra)
    code, text = discard(ctx, server, tmp_path)
    assert code == 1 and "still downloading" in text


def test_apply_and_discard_cannot_be_combined():
    with pytest.raises(SystemExit):
        recover.main(["--book", "1", "--apply", "--discard"], client=object(), state=SimpleNamespace(get_tasks_for_orchestrator=lambda: []), out=io.StringIO())
