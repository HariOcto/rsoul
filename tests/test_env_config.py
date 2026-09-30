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
