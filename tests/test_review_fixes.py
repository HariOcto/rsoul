"""Regression tests for the issues found when reviewing the audiobook/timeout pull request.

Response shapes follow the slskd source (v0.26): transfer states are serialized flag names
such as "Queued, Remotely", "InProgress" and "Completed, Errored"; transfer records carry
"id", "filename" (full remote path), "size", "state" and "bytesTransferred".
"""

import configparser
import logging
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rsoul import download as download_module
from rsoul import postprocess, workflow
from rsoul.backends.base import DownloadStatus, DownloadTask
from rsoul.backends.slskd_backend import SlskdBackend
from rsoul.config import Context, validate_config
from rsoul.match import audiobook_folder_match
from rsoul.orchestrator import DownloadOrchestrator

MB = 1024 * 1024
T0 = 1_000_000.0


def make_config(slskd=None, download=None, search=None):
    config = configparser.ConfigParser(interpolation=None)
    config["Readarr"] = {"api_key": "x", "host_url": "http://chaptarr"}
    config["Slskd"] = {"api_key": "x", "host_url": "http://slskd", "download_dir": "/downloads", **(slskd or {})}
    config["Search Settings"] = {"minimum_filename_match_ratio": "0.5", **(search or {})}
    config["Download Settings"] = {**(download or {})}
    config["Backends"] = {"slskd_enabled": "False"}
    return config


def task(status=DownloadStatus.DOWNLOADING, bytes_transferred=0, extra=None, local_dir=""):
    return DownloadTask(
        task_id="t",
        backend_name="slskd",
        status=status,
        book_title="Mistborn: The Final Empire",
        author_name="Brandon Sanderson",
        book_id=7,
        filename="01.mp3",
        local_dir=local_dir,
        bytes_transferred=bytes_transferred,
        extra=extra or {},
    )


def orch(config=None):
    return DownloadOrchestrator([], Context(config=config or make_config(), slskd=None, readarr=None))


# 1. Old settings from an existing config.ini must not change behaviour on upgrade


def test_old_remote_queue_timeout_is_ignored(caplog):
    with caplog.at_level(logging.WARNING):
        o = orch(make_config(slskd={"remote_queue_timeout": "300", "stalled_timeout": "3600"}))
    t = task(status=DownloadStatus.QUEUED)
    o.check_timeouts(t, now=T0)
    assert o.check_timeouts(t, now=T0 + 600) is None  # 10 min queued: fine
    assert "remote_queue_timeout is no longer used" in caplog.text
    assert "stalled_timeout is no longer used" in caplog.text


def test_new_queue_timeout_is_used():
    o = orch(make_config(download={"queue_timeout": "900"}))
    t = task(status=DownloadStatus.QUEUED)
    o.check_timeouts(t, now=T0)
    assert o.check_timeouts(t, now=T0 + 900) is not None


# 2. A resumed audiobook tracking fewer files must not look stalled


def test_byte_counter_going_backwards_is_not_a_stall():
    o = orch()
    t = task(extra={"monitor": {"started_at": T0, "last_progress_at": T0, "last_bytes": 300 * MB}})
    for minute in range(0, 60):
        t.bytes_transferred = int(minute * 60 * 50 * 1024)  # 50 KB/s on the remaining chapters
        assert o.check_timeouts(t, now=T0 + minute * 60) is None


def test_resumed_audiobook_counts_chapters_already_on_disk(tmp_path):
    folder = tmp_path / "Mistborn"
    folder.mkdir()
    for name in ("01.mp3", "02.mp3"):
        (folder / name).write_bytes(b"x" * 1000)

    class Transfers:
        def get_all_downloads(self):
            return [
                {
                    "username": "peer",
                    "directories": [
                        {"directory": "@@s\\Mistborn", "files": [{"id": "a", "filename": "@@s\\Mistborn\\03.mp3", "size": 1000, "state": "InProgress"}]}
                    ],
                }
            ]

        def get_download(self, username, id):
            return {"state": "InProgress", "bytesTransferred": 400}

    class Client:
        transfers = Transfers()

    config = make_config(slskd={"download_dir": str(tmp_path)})
    backend = SlskdBackend(Context(config=config, slskd=Client(), readarr=None))
    resumed = backend.reconcile_task(
        {
            "task_id": "t",
            "book_title": "b",
            "author_name": "a",
            "book_id": 7,
            "filename": "01.mp3",
            "local_dir": "Mistborn",
            "extra": {"username": "peer", "file_dir": "@@s\\Mistborn", "media_type": "audiobook", "expected_files": ["01.mp3", "02.mp3", "03.mp3"]},
        }
    )
    assert resumed.extra["offline_bytes"] == 2000
    assert resumed.bytes_transferred == 2400
    assert round(resumed.progress_percent) == 80


# 3. An slskd API error is not a finished download


class FailingTransfers:
    def get_download(self, username, id):
        raise RuntimeError("slskd restarting")


class FailingClient:
    transfers = FailingTransfers()


def test_api_error_is_not_completed(tmp_path):
    backend = SlskdBackend(Context(config=make_config(slskd={"download_dir": str(tmp_path)}), slskd=FailingClient(), readarr=None))
    t = task(extra={"files": [{"filename": "@@s\\B\\book.epub", "id": 1, "size": 10, "username": "u"}]}, local_dir="B")
    t = backend.get_status(t)
    assert t.status != DownloadStatus.COMPLETED
    assert t.poll_failed


def test_file_forgotten_by_slskd_but_on_disk_is_completed(tmp_path):
    (tmp_path / "B").mkdir()
    (tmp_path / "B" / "book.epub").write_bytes(b"x" * 10)
    backend = SlskdBackend(Context(config=make_config(slskd={"download_dir": str(tmp_path)}), slskd=FailingClient(), readarr=None))
    t = task(extra={"files": [{"filename": "@@s\\B\\book.epub", "id": 1, "size": 10, "username": "u"}]}, local_dir="B")
    assert backend.get_status(t).status == DownloadStatus.COMPLETED


def test_partial_file_on_disk_is_not_completed(tmp_path):
    (tmp_path / "B").mkdir()
    (tmp_path / "B" / "book.epub").write_bytes(b"x" * 4)
    backend = SlskdBackend(Context(config=make_config(slskd={"download_dir": str(tmp_path)}), slskd=FailingClient(), readarr=None))
    t = task(extra={"files": [{"filename": "@@s\\B\\book.epub", "id": 1, "size": 10, "username": "u"}]}, local_dir="B")
    assert backend.get_status(t).status != DownloadStatus.COMPLETED


# 4. An slskd outage pauses the stall and queue timers


def test_outage_does_not_cancel_downloads():
    o = orch(make_config(download={"stall_timeout": "1800"}))
    t = task(bytes_transferred=10 * MB)
    o.check_timeouts(t, now=T0)
    t.poll_failed = True
    for minutes in range(0, 185, 5):  # three hours of outage, polled every 5 minutes
        assert o.check_timeouts(t, now=T0 + minutes * 60) is None
    # Polling works again but the transfer is stuck: the stall timer counts from the end of
    # the outage, so it takes a full stall_timeout before giving up
    t.poll_failed = False
    resumed_at = T0 + 180 * 60
    assert o.check_timeouts(t, now=resumed_at + 1799) is None
    assert o.check_timeouts(t, now=resumed_at + 1800) is not None


def test_poll_exception_marks_task_untrusted():
    class Boom:
        name = "slskd"

        def get_status(self, t):
            raise RuntimeError("down")

    o = orch()
    t = o._poll(Boom(), task())
    assert t.poll_failed


# 5. Old transfer records in slskd's list are not mistaken for the new download


class StaleTransfers:
    def __init__(self, old_state):
        self.old = {"id": "old", "filename": "@@s\\B\\book.epub", "size": 10, "state": old_state, "bytesTransferred": 0}
        self.enqueued = False

    def get_downloads(self, username):
        files = [self.old]
        if self.enqueued:
            files.append({"id": "new", "filename": "@@s\\B\\book.epub", "size": 10, "state": "Queued, Remotely", "bytesTransferred": 0})
        return {"directories": [{"directory": "@@s\\B", "fileCount": len(files), "files": files}]}

    def enqueue(self, username, files):
        # slskd refuses files that are already queued or transferring
        self.enqueued = not self.old["state"].startswith(("Queued", "InProgress", "Initializing"))
        return True


def _enqueue(transfers, monkeypatch):
    monkeypatch.setattr(download_module.time, "sleep", lambda s: None)

    class Client:
        pass

    client = Client()
    client.transfers = transfers
    return download_module.slskd_do_enqueue(client, "peer", [{"filename": "@@s\\B\\book.epub", "size": 10}], "@@s\\B")


def test_old_failed_record_is_not_reused(monkeypatch):
    downloads = _enqueue(StaleTransfers("Completed, Errored"), monkeypatch)
    assert [d["id"] for d in downloads] == ["new"]


def test_old_succeeded_record_is_not_reused(monkeypatch):
    downloads = _enqueue(StaleTransfers("Completed, Succeeded"), monkeypatch)
    assert [d["id"] for d in downloads] == ["new"]


def test_still_active_old_record_is_the_download(monkeypatch):
    downloads = _enqueue(StaleTransfers("Queued, Remotely"), monkeypatch)
    assert [d["id"] for d in downloads] == ["old"]


# 6. remove_wanted_on_failure also covers resumed downloads, and works with Chaptarr


class UnmonitorReadarr:
    def __init__(self):
        self.updated = None

    def get_book(self, id_):
        return {"id": id_, "title": "Mistborn: The Final Empire", "mediaType": "audiobook", "monitored": True, "audiobookMonitored": True, "ebookMonitored": True}

    def get_edition(self, id_):
        return []

    def upd_book(self, book, editions):
        self.updated = book


def test_resumed_failure_is_unmonitored_on_the_right_media_side(tmp_path):
    readarr = UnmonitorReadarr()
    config = make_config(search={"remove_wanted_on_failure": "True"})
    ctx = Context(config=config, slskd=None, readarr=readarr, config_dir=str(tmp_path))
    results = workflow._RunResults(ctx, targets=[])

    failed = task(status=DownloadStatus.FAILED, extra={"media_type": "audiobook"})
    results.on_complete(failed)

    assert readarr.updated["monitored"] is False
    assert readarr.updated["audiobookMonitored"] is False
    assert readarr.updated["ebookMonitored"] is True  # the ebook is still wanted
    assert "Mistborn" in (tmp_path / "failure_list.txt").read_text()


# 7. A queue timer started at timestamp 0 is still a started timer


def test_queue_timer_started_at_zero():
    o = orch(make_config(download={"queue_timeout": "300"}))
    t = task(status=DownloadStatus.QUEUED)
    o.check_timeouts(t, now=0)
    assert o.check_timeouts(t, now=300) is not None


# 8. Failed imports are moved using R:soul's own path, not Chaptarr's


def test_to_local_path():
    assert postprocess.to_local_path("/chaptarr/dl/A/B", "/chaptarr/dl", "/rsoul/dl") == os.path.join("/rsoul/dl", "A/B")
    assert postprocess.to_local_path("/elsewhere/A", "/chaptarr/dl", "/rsoul/dl") == "/elsewhere/A"


def test_failed_import_folder_is_moved_locally(tmp_path):
    local = tmp_path / "dl"
    book_dir = local / "rsoul_audiobooks" / "Author" / "Title"
    book_dir.mkdir(parents=True)
    (book_dir / "01.mp3").write_bytes(b"x")

    class Client:
        def get_command(self, id_):
            return {"id": 1, "status": "completed", "message": "No files found are eligible for import", "body": {"path": "/chaptarr/dl/rsoul_audiobooks/Author/Title"}}

    postprocess.monitor_imports(Client(), [{"id": 1}], "/chaptarr/dl", str(local))

    assert not book_dir.exists()
    assert (local / "rsoul_audiobooks" / "Author" / "failed_imports" / "Title" / "01.mp3").exists()


# 9. Folders named without the series part of the title still match


def _match(directory):
    target = {"book": {"title": "Mistborn: The Final Empire"}, "author": {"authorName": "Brandon Sanderson"}}
    files = [{"filename": f"{directory}\\{i:02d}.mp3", "size": 40 * MB} for i in range(1, 6)]
    return audiobook_folder_match(target, files, "peer", ["mp3"], [], 0.5)


def test_folder_without_series_name_matches():
    assert _match("@@s\\Audiobooks\\Brandon Sanderson\\The Final Empire")


def test_short_title_needs_the_author():
    assert _match("@@s\\Audiobooks\\Someone Else\\The Final Empire") == []


# 10. Resumed downloads found complete on disk are not re-polled into a failure


def test_finished_resumed_task_is_not_repolled():
    class Backend:
        name = "slskd"

        def get_status(self, t):
            raise AssertionError("should not poll a finished task")

    o = orch()
    o.backends = [Backend()]
    done_task = task(status=DownloadStatus.COMPLETED, extra={"files": []})
    finished, unfinished = o.monitor_until([done_task])
    assert [t.status for t in finished] == [DownloadStatus.COMPLETED]
    assert unfinished == []


# 11. Audiobook formats Chaptarr can't import are flagged at startup


def test_unsupported_audiobook_format_warns(caplog):
    config = make_config(search={"media_mode": "audiobook", "audiobook_formats": "m4b,opus"})
    with caplog.at_level(logging.WARNING):
        validate_config(config)
    assert "opus" in caplog.text
