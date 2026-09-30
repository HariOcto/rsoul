import configparser
import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rsoul.backends.base import DownloadStatus, DownloadTask
from rsoul.backends.slskd_backend import SlskdBackend
from rsoul.config import Context
from rsoul.orchestrator import DownloadOrchestrator
from rsoul import orchestrator as orchestrator_module
from rsoul.state import StateManager
from rsoul import workflow

MB = 1024 * 1024


def make_config(**download_settings):
    config = configparser.ConfigParser(interpolation=None)
    config["Readarr"] = {"api_key": "x", "host_url": "http://chaptarr"}
    config["Slskd"] = {"api_key": "x", "host_url": "http://slskd", "download_dir": "/downloads"}
    config["Search Settings"] = {}
    config["Download Settings"] = {"stall_timeout": "1800", "queue_timeout": "3600", "max_download_time": "86400", **download_settings}
    config["Backends"] = {"slskd_enabled": "False"}
    return config


def make_task(task_id="t1", status=DownloadStatus.DOWNLOADING, bytes_transferred=0, book_id=1):
    return DownloadTask(
        task_id=task_id,
        backend_name="fake",
        status=status,
        book_title=f"Book {book_id}",
        author_name="Author",
        book_id=book_id,
        filename=f"{task_id}.mp3",
        bytes_transferred=bytes_transferred,
    )


def orchestrator(config=None):
    ctx = Context(config=config or make_config(), slskd=None, readarr=None)
    return DownloadOrchestrator([], ctx)


# ---------------------------------------------------------------------------
# check_timeouts
# ---------------------------------------------------------------------------


def test_slow_but_steady_500mb_audiobook_is_never_cancelled():
    orch = orchestrator()
    task = make_task()
    speed = 35 * 1024  # 35 KB/s: about 4 hours for 500 MB
    t0 = 1_000_000.0
    minute = 0
    while task.bytes_transferred < 500 * MB:
        task.bytes_transferred = min(500 * MB, int(speed * minute * 60))
        assert orch.check_timeouts(task, now=t0 + minute * 60) is None
        minute += 1
    # Took just over 4 hours, which the old fixed limit would have cancelled
    assert minute > 4 * 60


def test_stall_detected_when_no_new_data():
    orch = orchestrator()
    task = make_task(bytes_transferred=10 * MB)
    t0 = 1_000_000.0
    assert orch.check_timeouts(task, now=t0) is None
    task.bytes_transferred = 20 * MB
    assert orch.check_timeouts(task, now=t0 + 600) is None
    # No progress since t0 + 600
    assert orch.check_timeouts(task, now=t0 + 600 + 1799) is None
    assert "No data received" in orch.check_timeouts(task, now=t0 + 600 + 1800)


def test_remote_queue_timeout_and_queue_is_not_a_stall():
    orch = orchestrator()
    task = make_task(status=DownloadStatus.QUEUED)
    t0 = 1_000_000.0
    assert orch.check_timeouts(task, now=t0) is None
    # Queued for 50 minutes: longer than stall_timeout, but queueing is not stalling
    assert orch.check_timeouts(task, now=t0 + 3000) is None
    assert "queue without starting" in orch.check_timeouts(task, now=t0 + 3600)


def test_queue_timer_resets_once_transfer_starts():
    orch = orchestrator()
    task = make_task(status=DownloadStatus.QUEUED)
    t0 = 1_000_000.0
    orch.check_timeouts(task, now=t0)
    task.status = DownloadStatus.DOWNLOADING
    task.bytes_transferred = 5 * MB
    assert orch.check_timeouts(task, now=t0 + 3000) is None
    # Back in the queue between chapters: a fresh queue period starts
    task.status = DownloadStatus.QUEUED
    assert orch.check_timeouts(task, now=t0 + 3100) is None
    assert orch.check_timeouts(task, now=t0 + 3100 + 3599) is None


def test_local_queue_pauses_timers():
    orch = orchestrator()
    task = make_task(status=DownloadStatus.QUEUED_LOCALLY)
    t0 = 1_000_000.0
    for hours in range(0, 10):
        assert orch.check_timeouts(task, now=t0 + hours * 3600) is None


def test_overall_cap_applies_even_with_progress():
    orch = orchestrator(make_config(max_download_time="7200"))
    task = make_task()
    t0 = 1_000_000.0
    orch.check_timeouts(task, now=t0)
    task.bytes_transferred = 100 * MB
    assert "max_download_time" in orch.check_timeouts(task, now=t0 + 7200)


def test_zero_disables_limits():
    orch = orchestrator(make_config(stall_timeout="0", max_download_time="0"))
    task = make_task()
    t0 = 1_000_000.0
    orch.check_timeouts(task, now=t0)
    assert orch.check_timeouts(task, now=t0 + 10 * 86400) is None


# ---------------------------------------------------------------------------
# slskd status mapping
# ---------------------------------------------------------------------------


class StatusTransfers:
    def __init__(self, states):
        self.states = states  # id -> (state, bytes)

    def get_download(self, username, id):
        state, done = self.states[id]
        return {"state": state, "bytesTransferred": done}


class StatusClient:
    def __init__(self, states):
        self.transfers = StatusTransfers(states)


def slskd_task(states, sizes):
    config = make_config()
    backend = SlskdBackend(Context(config=config, slskd=StatusClient(states), readarr=None))
    task = make_task()
    task.backend_name = "slskd"
    task.extra = {"files": [{"filename": f"f{i}", "id": i, "size": size, "username": "peer"} for i, size in enumerate(sizes)]}
    return backend, task


@pytest.mark.parametrize(
    "state,expected",
    [
        ("InProgress", DownloadStatus.DOWNLOADING),
        ("Initializing", DownloadStatus.DOWNLOADING),
        ("Queued, Remotely", DownloadStatus.QUEUED),
        ("Queued, Locally", DownloadStatus.QUEUED_LOCALLY),
        ("Requested", DownloadStatus.PENDING),
    ],
)
def test_slskd_state_mapping(state, expected):
    backend, task = slskd_task({0: (state, 3 * MB)}, [10 * MB])
    task = backend.get_status(task)
    assert task.status == expected
    assert task.bytes_transferred == 3 * MB


def test_slskd_partial_audiobook_waiting_for_next_slot_counts_as_queued():
    states = {0: ("Completed, Succeeded", 10 * MB), 1: ("Completed, Succeeded", 10 * MB), 2: ("Queued, Remotely", 0)}
    backend, task = slskd_task(states, [10 * MB, 10 * MB, 10 * MB])
    task = backend.get_status(task)
    assert task.status == DownloadStatus.QUEUED
    assert task.bytes_transferred == 20 * MB
    assert round(task.progress_percent) == 67


# ---------------------------------------------------------------------------
# Parallel monitoring and deadlines
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ScriptedBackend:
    """Advances each task according to a per-task script of (status, bytes) per poll."""

    name = "fake"

    def __init__(self, scripts):
        self.scripts = scripts
        self.polls = {}
        self.cancelled = []

    def is_available(self):
        return True

    def get_status(self, task):
        i = self.polls.get(task.task_id, 0)
        self.polls[task.task_id] = i + 1
        script = self.scripts[task.task_id]
        status, done = script[min(i, len(script) - 1)]
        task.status = status
        task.bytes_transferred = done
        return task

    def cancel(self, task):
        self.cancelled.append(task.task_id)
        return True


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(orchestrator_module.time, "time", c.time)
    monkeypatch.setattr(orchestrator_module.time, "sleep", c.sleep)
    monkeypatch.setattr(workflow.time, "time", c.time)
    return c


def test_stalled_download_fails_without_affecting_others(clock):
    config = make_config(stall_timeout="60")
    orch = orchestrator(config)
    DL = DownloadStatus.DOWNLOADING
    backend = ScriptedBackend(
        {
            # Stuck at 5 MB forever
            "stuck": [(DL, 5 * MB)],
            # Keeps moving, then completes on poll 20 (well past the other's stall)
            "steady": [(DL, i * MB) for i in range(1, 20)] + [(DownloadStatus.COMPLETED, 20 * MB)],
        }
    )
    orch.backends = [backend]
    finished = []

    done, unfinished = orch.monitor_until([make_task("stuck"), make_task("steady", book_id=2)], on_complete=finished.append)

    assert unfinished == []
    by_id = {t.task_id: t for t in done}
    assert by_id["stuck"].status == DownloadStatus.FAILED
    assert "No data received" in by_id["stuck"].error_message
    assert by_id["steady"].status == DownloadStatus.COMPLETED
    assert backend.cancelled == ["stuck"]
    assert len(finished) == 2


def test_deadline_hands_back_unfinished_tasks(clock):
    orch = orchestrator()
    DL = DownloadStatus.DOWNLOADING
    backend = ScriptedBackend({"slow": [(DL, i * MB) for i in range(1, 1000)], "quick": [(DownloadStatus.COMPLETED, MB)]})
    orch.backends = [backend]

    done, unfinished = orch.monitor_until([make_task("slow"), make_task("quick", book_id=2)], deadline=clock.now + 60)

    assert [t.task_id for t in done] == ["quick"]
    assert [t.task_id for t in unfinished] == ["slow"]
    assert unfinished[0].extra["monitor"]["last_bytes"] > 0
    assert backend.cancelled == []


# ---------------------------------------------------------------------------
# Hand-off between runs
# ---------------------------------------------------------------------------


class HandoffBackend(ScriptedBackend):
    def reconcile_task(self, task_data):
        task = make_task(task_data["task_id"], book_id=task_data["book_id"])
        task.extra = task_data["extra"]
        return task


class RecordingStartOrchestrator(DownloadOrchestrator):
    """Real monitoring; start_targets records which books were searched."""

    def __init__(self, backends, ctx, start_tasks):
        super().__init__(backends, ctx)
        self.searched = []
        self.start_tasks = start_tasks

    def start_targets(self, targets, on_complete=None):
        self.searched = [t.book_id for t in targets]
        return [self.start_tasks[t.book_id] for t in targets if t.book_id in self.start_tasks]


class NoEditions:
    def get_edition(self, book_id):
        return []


def wanted(*ids):
    return [{"book": {"id": i, "title": f"Book {i}", "mediaType": "audiobook"}, "author": {"authorName": "Author"}} for i in ids]


def test_handoff_persists_unfinished_and_next_run_resumes_and_searches(clock, tmp_path, monkeypatch):
    config = make_config(monitor_window="120")
    DL = DownloadStatus.DOWNLOADING

    # Run 1: book 1 is a slow audiobook still going when the window ends
    state = StateManager(str(tmp_path))
    ctx = Context(config=config, slskd=None, readarr=NoEditions(), config_dir=str(tmp_path), state=state)
    backend = HandoffBackend({"t-book1": [(DL, i * MB) for i in range(1, 10_000)]})
    slow = make_task("t-book1", book_id=1)
    orch = RecordingStartOrchestrator([backend], ctx, {1: slow})
    ctx.orchestrator = orch
    state.add_task(slow)

    result = workflow.run_workflow(ctx, wanted(1))

    assert result["still_running"] == 1
    saved = StateManager(str(tmp_path)).get_tasks_for_orchestrator()
    assert [t["task_id"] for t in saved] == ["t-book1"]
    assert saved[0]["extra"]["monitor"]["last_bytes"] > 0

    # Run 2: book 1 is resumed (not searched again), book 2 is new; both finish
    state2 = StateManager(str(tmp_path))
    ctx2 = Context(config=config, slskd=None, readarr=NoEditions(), config_dir=str(tmp_path), state=state2)
    backend2 = HandoffBackend(
        {
            "t-book1": [(DownloadStatus.COMPLETED, 500 * MB)],
            "t-book2": [(DownloadStatus.FAILED, 0)],
        }
    )
    orch2 = RecordingStartOrchestrator([backend2], ctx2, {2: make_task("t-book2", book_id=2)})
    ctx2.orchestrator = orch2
    imported = []
    def fake_import(ctx, items):
        imported.extend(items)
        return {item["bookId"]: True for item in items}

    monkeypatch.setattr(workflow.postprocess, "process_imports", fake_import)

    result2 = workflow.run_workflow(ctx2, wanted(1, 2))

    assert orch2.searched == [2]
    assert [i["bookId"] for i in imported] == [1]
    assert result2["still_running"] == 0
    assert result2["failed_download"] == 1
    assert not (tmp_path / "grab_list_state.json").exists()
