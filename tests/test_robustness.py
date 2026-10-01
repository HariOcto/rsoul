"""HTTP timeouts, queue timer with chapter progress, imports that survive a run,
exit codes, and the health check."""

import configparser
import importlib.util
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rsoul import health, postprocess, workflow
from rsoul.backends.base import DownloadStatus, DownloadTask
from rsoul.config import Context
from rsoul.net import apply_timeout
from rsoul.orchestrator import DownloadOrchestrator
from rsoul.state import StateManager

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MB = 1024 * 1024
T0 = 1_000_000.0


# ---------------------------------------------------------------------------
# HTTP timeouts
# ---------------------------------------------------------------------------


@pytest.fixture
def hanging_server():
    """Accepts connections but answers far too late."""

    class Slow(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            time.sleep(1.0)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"[]")
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_readarr_client_times_out(hanging_server):
    from readarr_api import ReadarrAPI
    from readarr_api.exceptions import PyarrConnectionError

    client = ReadarrAPI(hanging_server, "key")
    apply_timeout(client.session, 0.1)
    start = time.monotonic()
    with pytest.raises(PyarrConnectionError):
        client.get_missing()
    assert time.monotonic() - start < 0.9


def test_slskd_client_times_out(hanging_server):
    import slskd_api

    client = slskd_api.SlskdClient(host=hanging_server, api_key="key", timeout=0.1)
    start = time.monotonic()
    with pytest.raises(requests.Timeout):
        client.transfers.get_all_downloads()
    assert time.monotonic() - start < 0.9


def test_zero_means_no_timeout():
    session = requests.Session()
    before = session.get_adapter("http://x")
    apply_timeout(session, 0)
    assert session.get_adapter("http://x") is before


# ---------------------------------------------------------------------------
# Queue timer and chapter progress
# ---------------------------------------------------------------------------


def orchestrator(queue_timeout="600"):
    config = configparser.ConfigParser()
    config["Download Settings"] = {"queue_timeout": queue_timeout, "stall_timeout": "1800"}
    return DownloadOrchestrator([], Context(config=config, slskd=None, readarr=None))


def task(status, done=0):
    return DownloadTask("t", "slskd", status, "Book", "Author", 1, "01.mp3", bytes_transferred=done)


def test_queued_between_chapters_but_progressing_is_not_a_queue_timeout():
    # Every poll lands between chapters ("Queued, Remotely"), yet a chapter arrives each time
    orch = orchestrator()
    t = task(DownloadStatus.QUEUED)
    for minute in range(0, 120, 5):
        t.bytes_transferred = minute * MB
        assert orch.check_timeouts(t, now=T0 + minute * 60) is None


def test_queued_without_progress_still_times_out():
    orch = orchestrator()
    t = task(DownloadStatus.QUEUED, done=5 * MB)
    orch.check_timeouts(t, now=T0)  # data arrived up to here: the queue clock starts now
    assert orch.check_timeouts(t, now=T0 + 599) is None
    assert "queue" in orch.check_timeouts(t, now=T0 + 600)


# ---------------------------------------------------------------------------
# Imports that survive a run
# ---------------------------------------------------------------------------


@pytest.fixture
def staged(tmp_path):
    local = tmp_path / "dl"
    (local / "Book").mkdir(parents=True)
    (local / "Book" / "01.m4b").write_bytes(b"x")
    config = configparser.ConfigParser()
    config["Slskd"] = {"download_dir": str(local), "readarr_download_dir": "/chaptarr/dl"}
    config["Readarr"] = {"import_poll_timeout": "0"}
    item = {"author_name": "A", "title": "T", "bookId": 7, "dir": "Book", "filename": "01.m4b", "expected_files": ["01.m4b"], "backend_name": "slskd", "media_type": "audiobook"}
    return local, config, item


class StillRunning:
    def post_command(self, name, **kwargs):
        return {"id": 11, "body": {"path": kwargs["path"]}}

    def get_command(self, id_):
        return {"id": id_, "status": "started"}


class Unreachable:
    def post_command(self, name, **kwargs):
        raise requests.ConnectionError("Chaptarr is restarting")


def test_unfinished_import_becomes_pending(staged):
    local, config, item = staged
    result = postprocess.process_imports(Context(config=config, slskd=None, readarr=StillRunning()), [item])[7]
    assert isinstance(result, dict)
    assert result["command_id"] == 11
    assert result["readarr_path"].startswith("/chaptarr/dl/rsoul_audiobooks/")
    assert all(os.path.exists(p) for p in result["files"])  # nothing moved to failed_imports


def test_unreachable_readarr_becomes_pending_without_command(staged):
    local, config, item = staged
    result = postprocess.process_imports(Context(config=config, slskd=None, readarr=Unreachable()), [item])[7]
    assert isinstance(result, dict) and result["command_id"] is None


def pending(tmp_path, command_id=5, files_exist=True, **extra):
    folder = tmp_path / "staged"
    folder.mkdir(exist_ok=True)
    f = folder / "01.m4b"
    if files_exist:
        f.write_bytes(b"x")
    return dict({"folder": "staged", "local_dir": str(folder), "readarr_path": "/c/staged", "files": [str(f)], "command_id": command_id, "submits": 1, "since": time.time()}, **extra)


class Commands:
    def __init__(self, command=None, error=None, post_error=None, on_get=None):
        self.command, self.error, self.post_error, self.on_get = command, error, post_error, on_get
        self.posted = []

    def get_command(self, id_):
        if self.on_get:
            self.on_get()
        if self.error:
            raise self.error
        return self.command

    def post_command(self, name, **kwargs):
        if self.post_error:
            raise self.post_error
        self.posted.append(kwargs["path"])
        return {"id": 99}


def http_404():
    response = requests.Response()
    response.status_code = 404
    return requests.HTTPError("404", response=response)


def test_pending_still_running(tmp_path):
    assert postprocess.check_pending_import(Commands({"status": "started"}), pending(tmp_path)) is None


def test_pending_finished_and_files_moved(tmp_path):
    p = pending(tmp_path)
    readarr = Commands({"status": "completed", "result": "successful"}, on_get=lambda: os.remove(p["files"][0]))
    assert postprocess.check_pending_import(readarr, p) is True


def test_pending_finished_unsuccessful(tmp_path):
    p = pending(tmp_path)
    assert postprocess.check_pending_import(Commands({"status": "completed", "result": "unsuccessful"}), p) is False
    assert not os.path.exists(p["files"][0])  # moved to failed_imports
    assert (tmp_path / "failed_imports" / "staged" / "01.m4b").exists()


def test_pending_readarr_unreachable_waits(tmp_path):
    assert postprocess.check_pending_import(Commands(error=requests.ConnectionError("down")), pending(tmp_path)) is None


def test_pending_command_forgotten_files_gone_counts_as_done(tmp_path):
    assert postprocess.check_pending_import(Commands(error=http_404()), pending(tmp_path, files_exist=False)) is True


def test_pending_command_forgotten_files_present_is_resubmitted(tmp_path):
    readarr = Commands(error=http_404())
    result = postprocess.check_pending_import(readarr, pending(tmp_path))
    assert result["command_id"] == 99 and result["submits"] == 2
    assert readarr.posted == ["/c/staged"]


def test_pending_never_submitted_is_submitted(tmp_path):
    readarr = Commands()
    result = postprocess.check_pending_import(readarr, pending(tmp_path, command_id=None, submits=0))
    assert result["command_id"] == 99


def test_pending_gives_up_after_a_day(tmp_path):
    p = pending(tmp_path, since=time.time() - 25 * 3600)
    assert postprocess.check_pending_import(Commands({"status": "started"}), p) is False
    # Not left untracked in staging: moved to failed_imports like any failed import
    assert not os.path.exists(p["files"][0])
    assert (tmp_path / "failed_imports" / "staged" / "01.m4b").exists()


def test_pending_gives_up_after_three_submits(tmp_path):
    p = pending(tmp_path, command_id=None, submits=3)
    assert postprocess.check_pending_import(Commands(), p) is False
    assert (tmp_path / "failed_imports" / "staged" / "01.m4b").exists()


def test_pending_import_is_saved_and_finished_next_run(tmp_path, monkeypatch):
    state = StateManager(str(tmp_path))
    config = configparser.ConfigParser()
    config["Slskd"] = {"download_dir": str(tmp_path)}
    done = DownloadTask("t1", "slskd", DownloadStatus.COMPLETED, "Book", "Author", 7, "01.m4b")
    state.add_task(done)
    p = pending(tmp_path)

    # Run 1: the import doesn't finish in time
    monkeypatch.setattr(workflow.postprocess, "process_imports", lambda ctx, items: {7: p})
    ctx = Context(config=config, slskd=None, readarr=None, config_dir=str(tmp_path), state=state)
    results = workflow._RunResults(ctx, [])
    results.on_complete(done)
    results.wait_for_imports()
    assert results.completed_tasks == [] and results.failed_imports == []
    saved = StateManager(str(tmp_path)).get_items()
    assert saved[0]["extra"]["import_pending"]["command_id"] == 5

    # Run 2: the follow-up finds it finished; it is not treated as a download to resume
    class Orchestrator:
        def resume_tasks(self, persisted):
            raise AssertionError("a pending import must not be resumed as a download")

        def monitor_until(self, tasks, on_complete=None, deadline=None):
            return [], []

        def get_backend(self, name):
            return None

    readarr = Commands({"status": "completed", "result": "successful"}, on_get=lambda: os.path.exists(p["files"][0]) and os.remove(p["files"][0]))
    ctx2 = Context(config=config, slskd=None, readarr=readarr, config_dir=str(tmp_path), state=StateManager(str(tmp_path)), orchestrator=Orchestrator())
    summary = workflow.run_workflow(ctx2, [])
    assert summary["grabbed_count"] == 1
    assert summary["pending_imports"] == 0
    assert StateManager(str(tmp_path)).get_items() == []


def test_still_pending_import_keeps_the_state(tmp_path):
    state = StateManager(str(tmp_path))
    t = DownloadTask("t1", "slskd", DownloadStatus.COMPLETED, "Book", "Author", 7, "01.m4b")
    t.extra = {"import_pending": pending(tmp_path)}
    state.add_task(t)

    class Orchestrator:
        def resume_tasks(self, persisted):
            assert persisted == []
            return []

        def monitor_until(self, tasks, on_complete=None, deadline=None):
            return [], []

        def get_backend(self, name):
            return None

    config = configparser.ConfigParser()
    ctx = Context(config=config, slskd=None, readarr=Commands({"status": "started"}), config_dir=str(tmp_path), state=state, orchestrator=Orchestrator())
    summary = workflow.run_workflow(ctx, [])
    assert summary["pending_imports"] == 1
    assert len(StateManager(str(tmp_path)).get_items()) == 1


# ---------------------------------------------------------------------------
# Exit codes, and a bad search setting not stranding saved downloads
# ---------------------------------------------------------------------------


def load_main():
    spec = importlib.util.spec_from_file_location("rsoul_main_under_test", os.path.join(REPO, "rsoul.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config_dir_with(tmp_path, **search):
    config = configparser.ConfigParser(interpolation=None)
    config.read(os.path.join(REPO, "config.ini"))
    config["Readarr"]["api_key"] = "key"
    config["Readarr"]["host_url"] = "http://127.0.0.1:9"
    config["Slskd"]["api_key"] = "key"
    config["Backends"]["slskd_enabled"] = "False"
    config["Search Settings"].update(search)
    with open(tmp_path / "config.ini", "w") as f:
        config.write(f)
    return str(tmp_path)


def run_main(monkeypatch, config_dir, **patches):
    module = load_main()
    for name, value in patches.items():
        monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(sys, "argv", ["rsoul.py", "-c", config_dir])
    monkeypatch.delenv("IN_DOCKER", raising=False)
    return module.main()


def test_missing_config_is_an_error(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.upper().startswith("RSOUL__"):
            monkeypatch.delenv(key)
    assert run_main(monkeypatch, str(tmp_path)) == 1


def bad_search(*args, **kwargs):
    raise ValueError("[Search Settings] - search_type = 'bogus' is not valid")


def test_bad_search_setting_without_saved_downloads_is_an_error(tmp_path, monkeypatch):
    calls = []
    code = run_main(monkeypatch, config_dir_with(tmp_path), get_books=bad_search, run_workflow=lambda ctx, targets: calls.append(targets))
    assert code == 1 and calls == []


def test_bad_search_setting_still_resumes_saved_downloads(tmp_path, monkeypatch):
    # With a monitor window, a run with saved downloads also searches for new books
    config_dir = config_dir_with(tmp_path)
    config = configparser.ConfigParser(interpolation=None)
    config.read(os.path.join(config_dir, "config.ini"))
    config["Download Settings"]["monitor_window"] = "1800"
    with open(os.path.join(config_dir, "config.ini"), "w") as f:
        config.write(f)
    StateManager(config_dir).add_task(DownloadTask("t1", "slskd", DownloadStatus.DOWNLOADING, "Book", "Author", 7, "01.mp3"))
    calls = []
    code = run_main(monkeypatch, config_dir, get_books=bad_search, run_workflow=lambda ctx, targets: calls.append(targets))
    assert calls == [[]]  # the workflow ran, with the saved downloads only
    assert code == 2


def test_normal_run_exits_zero(tmp_path, monkeypatch):
    code = run_main(monkeypatch, config_dir_with(tmp_path), get_books=lambda *a, **k: [], run_workflow=lambda ctx, targets: None)
    assert code == 0


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@pytest.fixture
def health_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "_config_dir", None)
    monkeypatch.setattr(health, "_last_beat", 0.0)
    monkeypatch.delenv("RSOUL_HEALTH_MAX_AGE", raising=False)
    monkeypatch.setenv("SCRIPT_INTERVAL", "300")
    health.configure(str(tmp_path))
    return tmp_path


def test_fresh_status_is_healthy(health_dir):
    health.write_status("running")
    assert health.check(str(health_dir))[0] is True


def test_old_status_is_unhealthy(health_dir):
    health.write_status("idle")
    healthy, reason = health.check(str(health_dir), now=time.time() + 901)
    assert healthy is False and "stuck" in reason


def test_missing_status_is_unhealthy(tmp_path):
    assert health.check(str(tmp_path))[0] is False


def test_limit_follows_script_interval(health_dir, monkeypatch):
    monkeypatch.setenv("SCRIPT_INTERVAL", "1800")
    assert health.max_age() == 2 * 1800 + 120
    monkeypatch.setenv("RSOUL_HEALTH_MAX_AGE", "60")
    assert health.max_age() == 60


def test_status_keeps_earlier_details(health_dir):
    health.write_status("idle", last_success_at=123.0)
    health.write_status("running")
    data = json.loads((health_dir / "health.json").read_text())
    assert data["state"] == "running" and data["last_success_at"] == 123.0


def test_heartbeat_is_rate_limited(health_dir):
    health.write_status("running")
    first = json.loads((health_dir / "health.json").read_text())["updated_at"]
    health.heartbeat()  # within a minute: no write
    assert json.loads((health_dir / "health.json").read_text())["updated_at"] == first


def test_unwritable_status_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "_config_dir", str(tmp_path / "does-not-exist"))
    health.write_status("running")  # must not raise


# ---------------------------------------------------------------------------
# Review feedback: outages, failed searches, validation, locking, partial staging
# ---------------------------------------------------------------------------


class SlskdDown:
    def get_all_downloads(self):
        raise requests.ConnectionError("slskd restarting")

    def get_downloads(self, username):
        raise requests.ConnectionError("slskd restarting")

    def get_download(self, username, id):
        raise requests.ConnectionError("slskd restarting")


def saved_downloads(state):
    for i, media_type in enumerate(["audiobook", "ebook"]):
        t = DownloadTask(f"t{i}", "slskd", DownloadStatus.DOWNLOADING, f"Book{i}", "A", i, "01.mp3" if media_type == "audiobook" else "b.epub", local_dir="X")
        t.extra = {"username": "peer", "file_dir": "@@p\\X", "media_type": media_type, "expected_files": ["01.mp3"], "files": [{"id": "a", "filename": "@@p\\X\\01.mp3", "size": 10, "username": "peer"}]}
        state.add_task(t)


def test_slskd_outage_at_startup_keeps_saved_downloads(tmp_path, monkeypatch):
    from rsoul.backends.slskd_backend import SlskdBackend
    from rsoul import orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module.time, "sleep", lambda s: None)
    state = StateManager(str(tmp_path))
    saved_downloads(state)

    class Client:
        transfers = SlskdDown()

    config = configparser.ConfigParser()
    config["Slskd"] = {"download_dir": str(tmp_path)}
    config["Download Settings"] = {"monitor_window": "60"}
    ctx = Context(config=config, slskd=Client(), readarr=None, config_dir=str(tmp_path), state=state)
    ctx.orchestrator = DownloadOrchestrator([SlskdBackend(ctx)], ctx)

    summary = workflow.run_workflow(ctx, [{"book": {"id": 0, "title": "Book0"}, "author": {"authorName": "A"}}])

    assert len(StateManager(str(tmp_path)).get_items()) == 2
    assert summary["unchecked_downloads"] == 2


def test_disabled_backend_keeps_saved_downloads(tmp_path):
    state = StateManager(str(tmp_path))
    saved_downloads(state)
    config = configparser.ConfigParser()
    ctx = Context(config=config, slskd=None, readarr=None, config_dir=str(tmp_path), state=state)
    ctx.orchestrator = DownloadOrchestrator([], ctx)  # slskd backend switched off
    workflow.run_workflow(ctx, [])
    assert len(StateManager(str(tmp_path)).get_items()) == 2


def test_unreachable_readarr_is_a_search_error_not_an_empty_list(tmp_path):
    from rsoul.search import SearchError, get_books

    class Readarr:
        def get_missing(self, **kwargs):
            raise requests.ConnectionError("Chaptarr restarting")

    config = configparser.ConfigParser()
    config["Search Settings"] = {}
    with pytest.raises(SearchError):
        get_books(Context(config=config, slskd=None, readarr=Readarr(), config_dir=str(tmp_path)), "missing", "first_page", 10)


def test_failed_page_is_not_skipped(tmp_path):
    from rsoul.search import SearchError, get_books

    class Readarr:
        def get_missing(self, page=None, **kwargs):
            if page is None:
                return {"totalRecords": 30, "records": []}
            raise requests.ConnectionError("Chaptarr restarting")

    (tmp_path / ".current_page.txt").write_text("2")
    config = configparser.ConfigParser()
    config["Search Settings"] = {}
    with pytest.raises(SearchError):
        get_books(Context(config=config, slskd=None, readarr=Readarr(), config_dir=str(tmp_path)), "missing", "incrementing_page", 10)
    assert (tmp_path / ".current_page.txt").read_text().strip() == "2"


def unreachable_search(*args, **kwargs):
    from rsoul.search import SearchError

    raise SearchError("Could not get the missing list from Readarr/Chaptarr: connection refused")


def test_search_error_without_saved_downloads_exits_2(tmp_path, monkeypatch):
    calls = []
    code = run_main(monkeypatch, config_dir_with(tmp_path), get_books=unreachable_search, run_workflow=lambda ctx, targets: calls.append(targets))
    assert code == 2 and calls == []


def test_health_records_how_the_last_run_went(tmp_path, monkeypatch):
    module = load_main()
    monkeypatch.setattr(module, "get_books", lambda *a, **k: [])
    monkeypatch.setattr(module, "run_workflow", lambda ctx, targets: {"grabbed_count": 2, "failed_download": 1, "still_running": 0, "pending_imports": 1, "unchecked_downloads": 0})
    config_dir = config_dir_with(tmp_path)
    StateManager(config_dir).add_task(DownloadTask("t1", "slskd", DownloadStatus.DOWNLOADING, "Book", "Author", 7, "01.mp3"))
    monkeypatch.setattr(sys, "argv", ["rsoul.py", "-c", config_dir])
    monkeypatch.setattr(health, "_last_beat", 0.0)
    assert module._main_with_status() == 0
    data = json.loads((tmp_path / "health.json").read_text())
    assert data["last_run"] == {"search_failed": False, "grabbed": 2, "failed": 1, "still_downloading": 0, "pending_imports": 1, "unchecked_downloads": 0}
    assert data["state"] == "idle" and "last_success_at" in data


@pytest.mark.parametrize("value", ["-100", "nan", "inf", "abc"])
def test_invalid_time_limits_are_rejected(value):
    from rsoul.config import validate_config

    config = configparser.ConfigParser(interpolation=None)
    config.read(os.path.join(REPO, "config.ini"))
    config["Readarr"]["api_key"] = "a"
    config["Slskd"]["api_key"] = "b"
    config["Slskd"]["request_timeout"] = value
    with pytest.raises(ValueError, match="request_timeout"):
        validate_config(config)


def test_zero_time_limit_is_allowed():
    from rsoul.config import validate_config

    config = configparser.ConfigParser(interpolation=None)
    config.read(os.path.join(REPO, "config.ini"))
    config["Readarr"]["api_key"] = "a"
    config["Slskd"]["api_key"] = "b"
    config["Download Settings"]["stall_timeout"] = "0"
    validate_config(config)


def test_invalid_health_limit_falls_back(monkeypatch):
    monkeypatch.setenv("RSOUL_HEALTH_MAX_AGE", "nan")
    monkeypatch.setenv("SCRIPT_INTERVAL", "-5")
    assert health.max_age() == 900


def test_only_one_instance_per_data_folder(tmp_path):
    from rsoul.locking import AlreadyRunning, InstanceLock

    first = InstanceLock(str(tmp_path))
    first.acquire()
    second = InstanceLock(str(tmp_path))
    with pytest.raises(AlreadyRunning):
        second.acquire()
    first.release()
    second.acquire()  # free again once the first one is done
    second.release()


def test_lock_file_left_behind_does_not_block(tmp_path):
    from rsoul.locking import InstanceLock

    (tmp_path / ".rsoul.lock").write_text("locked")  # e.g. after a crash
    lock = InstanceLock(str(tmp_path))
    lock.acquire()
    lock.release()


def test_lock_held_by_another_process_blocks(tmp_path):
    import subprocess

    code = (
        "import fcntl, sys, time; f = open(sys.argv[1], 'a+'); fcntl.flock(f, fcntl.LOCK_EX); "
        "print('locked', flush=True); time.sleep(5)"
    )
    holder = subprocess.Popen([sys.executable, "-c", code, str(tmp_path / ".rsoul.lock")], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        from rsoul.locking import AlreadyRunning, InstanceLock

        with pytest.raises(AlreadyRunning):
            InstanceLock(str(tmp_path)).acquire()
    finally:
        holder.kill()
        holder.wait()


def test_half_moved_audiobook_is_put_back_together(tmp_path, monkeypatch):
    source = tmp_path / "Book"
    source.mkdir()
    for i in range(1, 4):
        (source / f"{i:02d}.mp3").write_bytes(b"x")
    real_move = postprocess.shutil.move
    moves = {"n": 0}

    def flaky_move(src, dst):
        if str(dst).startswith(str(tmp_path / "rsoul_audiobooks")):
            moves["n"] += 1
            if moves["n"] == 3:
                raise OSError("No space left on device")
        return real_move(src, dst)

    monkeypatch.setattr(postprocess.shutil, "move", flaky_move)
    item = {"author_name": "A", "title": "T", "bookId": 1, "dir": "Book", "expected_files": ["01.mp3", "02.mp3", "03.mp3"]}
    relative, reason = postprocess.organize_audiobook(item, str(tmp_path))

    assert relative is None and "No space left" in reason
    assert sorted(os.listdir(source)) == ["01.mp3", "02.mp3", "03.mp3"]  # all back together
    assert not (tmp_path / "rsoul_audiobooks" / "A" / "T").exists()


def test_lock_unsupported_by_filesystem_runs_without_it(tmp_path, monkeypatch):
    from rsoul import locking

    calls = []

    def flock(fd, op):
        calls.append(op)
        raise OSError(38, "Function not implemented")  # e.g. some network filesystems

    monkeypatch.setattr(locking.fcntl, "flock", flock)
    lock = locking.InstanceLock(str(tmp_path))
    lock.acquire()  # warns, doesn't refuse to run
    assert lock._file is None  # no half-open lock left behind
    lock.release()  # must not raise
    assert calls == [locking.fcntl.LOCK_EX | locking.fcntl.LOCK_NB]


def test_unlock_failure_does_not_raise(tmp_path, monkeypatch):
    from rsoul import locking

    lock = locking.InstanceLock(str(tmp_path))
    lock.acquire()
    real_flock = locking.fcntl.flock

    def flock(fd, op):
        if op == locking.fcntl.LOCK_UN:
            raise OSError("unlock failed")
        return real_flock(fd, op)

    monkeypatch.setattr(locking.fcntl, "flock", flock)
    lock.release()  # logs a warning instead of raising
    assert lock._file is None


def test_pending_after_a_day_with_files_gone_counts_as_imported(tmp_path):
    # Chaptarr imported (and moved the files) while R:soul couldn't reach it
    p = pending(tmp_path, files_exist=False, since=time.time() - 25 * 3600)
    assert postprocess.check_pending_import(Commands(error=requests.ConnectionError("down")), p) is True
    assert not (tmp_path / "failed_imports").exists()


@pytest.mark.parametrize("option,value", [("monitor_window", "1.5"), ("max_active_downloads", "2.5"), ("max_active_downloads", "-1"), ("monitor_window", "abc")])
def test_whole_number_settings_reject_other_values(option, value):
    from rsoul.config import validate_config

    config = configparser.ConfigParser(interpolation=None)
    config.read(os.path.join(REPO, "config.ini"))
    config["Readarr"]["api_key"] = "a"
    config["Slskd"]["api_key"] = "b"
    config["Download Settings"][option] = value
    with pytest.raises(ValueError, match=option):
        validate_config(config)


def test_search_phase_keeps_health_fresh(tmp_path, monkeypatch):
    # Found live: searching 10 books can take over 15 minutes without a heartbeat
    from rsoul.backends.base import DownloadTarget

    beats = []
    monkeypatch.setattr(health, "heartbeat", lambda: beats.append(1))

    class Backend:
        name = "fake"

        def is_available(self):
            return True

        def search(self, target):
            return []

    config = configparser.ConfigParser()
    config["General"] = {"batch_delay": "0"}
    orch = DownloadOrchestrator([Backend()], Context(config=config, slskd=None, readarr=None))
    targets = [DownloadTarget(i, f"B{i}", "A", "", ["mp3"], {"id": i, "title": f"B{i}"}, {"authorName": "A"}) for i in range(3)]
    orch.start_targets(targets)
    assert len(beats) == 3
