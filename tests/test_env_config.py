"""Configuration through RSOUL__<SECTION>__<OPTION> environment variables, and
slskd cleanup that only touches R:soul's own transfers."""

import configparser
import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rsoul.backends.base import DownloadStatus, DownloadTask
from rsoul.backends.slskd_backend import SlskdBackend
from rsoul.config import Context, apply_env_overrides, validate_config

TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.ini")


def template():
    config = configparser.ConfigParser(interpolation=None)
    config.read(TEMPLATE)
    return config


def test_overrides_match_sections_case_and_space_insensitively():
    config = template()
    applied = apply_env_overrides(
        config,
        {
            "RSOUL__READARR__HOST_URL": "http://nas:8789",
            "rsoul__search_settings__MEDIA_MODE": "audiobook",
            "RSOUL__Download_Settings__monitor_window": "1800",
        },
    )
    assert config["Readarr"]["host_url"] == "http://nas:8789"
    assert config["Search Settings"]["media_mode"] == "audiobook"
    assert config["Download Settings"]["monitor_window"] == "1800"
    assert len(applied) == 3


def test_environment_wins_over_config_file():
    config = template()
    config["Slskd"]["download_dir"] = "/from/file"
    apply_env_overrides(config, {"RSOUL__SLSKD__DOWNLOAD_DIR": "/downloads"})
    assert config["Slskd"]["download_dir"] == "/downloads"


def test_secrets_are_not_logged():
    applied = apply_env_overrides(template(), {"RSOUL__SLSKD__API_KEY": "super-secret"})
    assert "super-secret" not in applied[0]
    assert "(hidden)" in applied[0]


def test_unknown_section_is_reported_not_created():
    config = template()
    applied = apply_env_overrides(config, {"RSOUL__SEARCH_SETTING__MEDIA_MODE": "audiobook", "RSOUL__NOSECTION": "x"})
    assert "Search Setting" not in config.sections()
    assert all(line.startswith("Ignored") for line in applied)


def test_general_section_is_created_when_needed():
    config = template()
    apply_env_overrides(config, {"RSOUL__GENERAL__BATCH_DELAY": "0"})
    assert config["General"]["batch_delay"] == "0"


def test_unrelated_environment_is_ignored():
    config = template()
    assert apply_env_overrides(config, {"PATH": "/usr/bin", "SCRIPT_INTERVAL": "300"}) == []


def test_placeholder_api_key_is_rejected_with_hint():
    with pytest.raises(ValueError, match="RSOUL__READARR__API_KEY"):
        validate_config(template())


def test_env_only_config_validates():
    config = template()
    apply_env_overrides(config, {"RSOUL__READARR__API_KEY": "a", "RSOUL__SLSKD__API_KEY": "b"})
    validate_config(config)


# ---------------------------------------------------------------------------
# slskd cleanup: only this task's transfer records
# ---------------------------------------------------------------------------


class Transfers:
    def __init__(self):
        self.removed = []
        self.cleared_all = False

    def cancel_download(self, username, id, remove=False):
        self.removed.append((username, id, remove))
        return True

    def remove_completed_downloads(self):
        self.cleared_all = True


def test_cleanup_removes_only_own_transfers():
    class Client:
        transfers = Transfers()

    config = configparser.ConfigParser()
    config["Slskd"] = {"download_dir": "/downloads"}
    backend = SlskdBackend(Context(config=config, slskd=Client(), readarr=None))
    task = DownloadTask("t", "slskd", DownloadStatus.COMPLETED, "b", "a", 1, "f")
    task.extra = {"username": "peer", "files": [{"id": "x1", "username": "peer"}, {"id": "x2", "username": "peer"}]}

    backend.cleanup(task)

    assert Client.transfers.removed == [("peer", "x1", True), ("peer", "x2", True)]
    assert Client.transfers.cleared_all is False


# ---------------------------------------------------------------------------
# Transfers cleared by another tool (e.g. Soularr's "remove all completed")
# ---------------------------------------------------------------------------


class ListClient:
    def __init__(self, records):
        class T:
            def get_downloads(self_inner, username):
                return {"directories": [{"directory": "d", "files": records}]}

            def get_all_downloads(self_inner):
                return [{"username": "other", "directories": [{"directory": "@@o\\Mistborn", "files": records}]}]

        self.transfers = T()


def slskd_task(tmp_path, client, size=10):
    config = configparser.ConfigParser()
    config["Slskd"] = {"download_dir": str(tmp_path)}
    backend = SlskdBackend(Context(config=config, slskd=client, readarr=None))
    task = DownloadTask("t", "slskd", DownloadStatus.DOWNLOADING, "b", "a", 1, "f", local_dir="B")
    task.extra = {"files": [{"filename": "@@p\\B\\01.mp3", "id": "gone", "size": size, "username": "peer"}]}
    return backend, task


def test_cleared_transfer_not_on_disk_fails_instead_of_waiting(tmp_path):
    backend, task = slskd_task(tmp_path, ListClient([]))
    task = backend.get_status(task)
    assert task.status == DownloadStatus.FAILED
    assert task.poll_failed is False  # a definite answer, not an outage


def test_cleared_transfer_on_disk_completes(tmp_path):
    (tmp_path / "B").mkdir()
    (tmp_path / "B" / "01.mp3").write_bytes(b"x" * 10)
    backend, task = slskd_task(tmp_path, ListClient([]))
    assert backend.get_status(task).status == DownloadStatus.COMPLETED


def test_empty_404_body_is_unknown_not_a_crash(tmp_path):
    class T:
        def get_downloads(self, username):
            raise RuntimeError("list unavailable")

        def get_download(self, username, id):
            return {}  # what slskd-api returns for an unknown ID once the body is parsed

    class C:
        transfers = T()

    backend, task = slskd_task(tmp_path, C())
    task = backend.get_status(task)
    assert task.status != DownloadStatus.COMPLETED
    assert task.poll_failed


def test_folder_with_unfinished_foreign_download_is_avoided(tmp_path):
    # Another tool is downloading different files into the same local folder name
    records = [{"id": "s1", "filename": "@@o\\Mistborn\\cover.jpg", "state": "InProgress"}]
    backend, _ = slskd_task(tmp_path, ListClient(records))
    clashes = backend._local_clashes("@@p\\Mistborn", [{"filename": "@@p\\Mistborn\\01.mp3"}])
    assert clashes == ["cover.jpg"]
