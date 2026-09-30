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
    assert len(clashes) == 1 and clashes[0].startswith("cover.jpg (still downloading from other")


# ---------------------------------------------------------------------------
# Search without author initials; explain why no audiobook folder matched
# ---------------------------------------------------------------------------


def test_strip_initials():
    from rsoul.backends.slskd_backend import _strip_initials

    assert _strip_initials("Christopher G. Nuttall") == "Christopher Nuttall"
    assert _strip_initials("J.R.R. Tolkien") == "Tolkien"
    assert _strip_initials("Brandon Sanderson") == "Brandon Sanderson"


def test_search_tries_author_without_initials(monkeypatch):
    from rsoul.backends import slskd_backend
    from rsoul.backends.base import DownloadTarget

    queries = []
    monkeypatch.setattr(slskd_backend, "_execute_search", lambda ctx, query, label: (queries.append(query) or ([], None)))
    config = configparser.ConfigParser()
    config["Slskd"] = {"download_dir": "/downloads", "delete_searches": "False"}
    config["Search Settings"] = {"max_search_fallbacks": "1"}
    backend = SlskdBackend(Context(config=config, slskd=object(), readarr=None))
    target = DownloadTarget(1, "A Savage War Of Peace", "Christopher G. Nuttall", "", ["mp3"], {"title": "A Savage War Of Peace", "id": 1}, {"authorName": "Christopher G. Nuttall"}, media_type="audiobook")

    backend.search(target)

    assert queries[:2] == ["Christopher G. Nuttall - A Savage War Of Peace", "Christopher Nuttall - A Savage War Of Peace"]


def test_no_match_is_explained(caplog):
    import logging

    config = configparser.ConfigParser()
    config["Slskd"] = {"download_dir": "/downloads"}
    config["Search Settings"] = {"minimum_filename_match_ratio": "0.7"}
    backend = SlskdBackend(Context(config=config, slskd=object(), readarr=None))
    from rsoul.backends.base import DownloadTarget

    target = DownloadTarget(1, "A Savage War Of Peace", "Christopher G. Nuttall", "", ["m4b", "mp3"], {"title": "A Savage War Of Peace", "id": 1}, {"authorName": "Christopher G. Nuttall"}, media_type="audiobook")
    files = [
        {"filename": "@@u\\\\Books\\\\Christopher Nuttall - A Savage War of Peace.epub", "size": 1},
        {"filename": "@@u\\\\Audio\\\\A Savage War of Peace\\\\01.mp3", "size": 1},
    ]
    with caplog.at_level(logging.INFO):
        assert backend._match_audiobook_results(target, {"book": target.readarr_book, "author": target.readarr_author}, [{"username": "u", "files": files}]) == []
    assert "files in other formats: 1" in caplog.text
    assert "folders without the author's surname: 1" in caplog.text


# ---------------------------------------------------------------------------
# slskd answers 404 for a user without transfers and for unknown IDs, and slskd-api
# raises requests.HTTPError for it (found in the first live test)
# ---------------------------------------------------------------------------


def http_404():
    import requests

    response = requests.Response()
    response.status_code = 404
    return requests.HTTPError("404 Client Error: Not Found", response=response)


def test_first_download_from_a_new_peer(monkeypatch):
    from rsoul import download

    monkeypatch.setattr(download.time, "sleep", lambda s: None)

    class Transfers:
        queued = []

        def get_downloads(self, username):
            if not self.queued:
                raise http_404()  # slskd: this user has no transfers yet
            return {"directories": [{"directory": "@@p\\Book", "files": self.queued}]}

        def enqueue(self, username, files):
            self.queued = [{"id": "n1", "filename": files[0]["filename"], "size": 10, "state": "Requested"}]
            return True

    class C:
        transfers = Transfers()

    downloads = download.slskd_do_enqueue(C(), "peer", [{"filename": "@@p\\Book\\book.m4b", "size": 10}], "@@p\\Book")
    assert [d["id"] for d in downloads] == ["n1"]


def test_user_list_404_means_transfer_gone(tmp_path):
    class T:
        def get_downloads(self, username):
            raise http_404()

    class C:
        transfers = T()

    backend, task = slskd_task(tmp_path, C())
    task = backend.get_status(task)
    assert task.status == DownloadStatus.FAILED
    assert task.poll_failed is False


def test_single_download_404_means_transfer_gone(tmp_path):
    class T:
        def get_downloads(self, username):
            raise ConnectionError("list endpoint down")

        def get_download(self, username, id):
            raise http_404()

    class C:
        transfers = T()

    backend, task = slskd_task(tmp_path, C())
    assert backend.get_status(task).status == DownloadStatus.FAILED


# ---------------------------------------------------------------------------
# From the live test: wanted "Salvation" (Peter F. Hamilton) must not download
# "The Saints of Salvation" or "Salvation Lost"
# ---------------------------------------------------------------------------


def test_salvation_does_not_match_other_books_in_the_series():
    from rsoul.match import audiobook_folder_match

    folders = {
        "darri": [
            ("@@mbhkr\\audiobooks\\Sci-Fi\\Peter F. Hamilton\\Peter F Hamilton - Salvation (m4b)", "m4b", 1),
            ("@@mbhkr\\audiobooks\\Sci-Fi\\Peter F. Hamilton\\Peter F. Hamilton - Salvation Lost", "mp3", 35),
        ],
        "Azazin1711": [("Hörbücher\\+ Hörbücher Sci-Fi +\\Peter F. Hamilton - Die Salvation Saga 3 - Erlösung", "mp3", 224)],
        "squickle": [("Audiobooks on 16TB-3\\H\\Hamilton, Peter F\\Salvation Sequence 1 - Salvation", "m4b", 1)],
        "hast": [("Virtual Voice Audiobooks\\Peter F. Hamilton\\Salvation (Unabridged)", "m4b", 1)],
        "QZm": [("books\\audio_books\\Peter F. Hamilton\\The Saints of Salvation", "mp3", 193)],
        "wobble7582": [("Audiobooks\\Peter F. Hamilton\\The Saints of Salvation", "m4b", 1)],
    }
    target = {"book": {"title": "Salvation", "seriesTitle": "The Salvation Sequence"}, "author": {"authorName": "Peter F. Hamilton"}}
    matched = {}
    for user, dirs in folders.items():
        files = [{"filename": f"{d}\\{i:02d}.{ext}", "size": 10} for d, ext, n in dirs for i in range(1, n + 1)]
        for m in audiobook_folder_match(target, files, user, ["m4b", "mp3"], [], 0.7):
            matched[user] = m["directory"].split("\\")[-1]

    assert matched == {
        "darri": "Peter F Hamilton - Salvation (m4b)",
        "squickle": "Salvation Sequence 1 - Salvation",
        "hast": "Salvation (Unabridged)",
    }


def test_title_segment_match_cases():
    from rsoul.match import title_segment_match as t

    assert t("Lines of Departure", "Frontlines 2 - Lines of Departure", "Marko Kloos")
    assert t("Lines of Departure", "Marko-Kloos-Lines-of-Departure-Unabr", "Marko Kloos")
    assert t("Knife Edge", "Christopher G. Nuttall - Knife Edge Empire's Corps, Book 17", "Christopher G. Nuttall", "Empire's Corps")
    assert not t("Knife Edge", "Christopher G. Nuttall - Knife Edge Empire's Corps, Book 17", "Christopher G. Nuttall", "")
    assert not t("Salvation", "Peter F. Hamilton - Salvation Lost", "Peter F. Hamilton")
