import configparser
import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rsoul import media
from rsoul.backends.base import DownloadStatus, DownloadTarget
from rsoul.backends import slskd_backend
from rsoul.backends.slskd_backend import SlskdBackend
from rsoul import download as download_module
from rsoul.match import audiobook_folder_match, clean_audiobook_name
from rsoul.search import get_books
from rsoul import postprocess
from rsoul.config import Context

MB = 1024 * 1024

AUTHOR = "Brandon Sanderson"
TITLE = "Mistborn: The Final Empire"


def make_config(**search_settings):
    config = configparser.ConfigParser(interpolation=None)
    config["Readarr"] = {"api_key": "x", "host_url": "http://chaptarr:8789"}
    config["Slskd"] = {"api_key": "x", "host_url": "http://slskd:5030", "download_dir": "/downloads"}
    config["Search Settings"] = {"minimum_filename_match_ratio": "0.5", **search_settings}
    return config


def target_dict():
    return {"book": {"title": TITLE, "id": 7}, "author": {"authorName": AUTHOR}}


def audiobook_target(formats=("m4b", "mp3")):
    return DownloadTarget(
        book_id=7,
        book_title=TITLE,
        author_name=AUTHOR,
        series_title="",
        allowed_filetypes=list(formats),
        readarr_book={"title": TITLE, "id": 7},
        readarr_author={"authorName": AUTHOR},
        media_type="audiobook",
    )


def chapter_files(directory, count, ext="mp3", size=40 * MB):
    return [{"filename": f"{directory}\\{i:02d} - Chapter {i}.{ext}", "size": size} for i in range(1, count + 1)]


# ---------------------------------------------------------------------------
# Media mode helpers
# ---------------------------------------------------------------------------


def test_media_mode_defaults_to_ebook():
    assert media.get_media_mode(make_config()) == "ebook"


def test_media_mode_rejects_invalid_value():
    with pytest.raises(ValueError):
        media.get_media_mode(make_config(media_mode="podcasts"))


def test_resolve_media_type_both_uses_book_field():
    assert media.resolve_book_media_type({"mediaType": "audiobook"}, "both") == "audiobook"
    assert media.resolve_book_media_type({"mediaType": "ebook"}, "both") == "ebook"
    # Plain Readarr has no mediaType field: fall back to ebook rather than guessing
    assert media.resolve_book_media_type({}, "both") == "ebook"
    # Fixed modes ignore the field
    assert media.resolve_book_media_type({"mediaType": "ebook"}, "audiobook") == "audiobook"


def test_formats_per_media_type():
    config = make_config(audiobook_formats="m4b, MP3")
    assert media.get_formats(config, "audiobook") == ["m4b", "mp3"]
    assert media.get_formats(config, "ebook") == ["epub", "azw3", "mobi"]


class FakeReadarr:
    def __init__(self):
        self.calls = []

    def get_missing(self, **kwargs):
        self.calls.append(kwargs)
        return {"totalRecords": 1, "records": [{"id": 1, "title": "x", "authorId": 1}]}

    get_cutoff = get_missing


@pytest.mark.parametrize("mode,expected", [("audiobook", "audiobook"), ("ebook", "ebook"), ("both", None)])
def test_get_books_filters_wanted_list_by_media_type(tmp_path, mode, expected):
    readarr = FakeReadarr()
    ctx = Context(config=make_config(media_mode=mode), slskd=None, readarr=readarr, config_dir=str(tmp_path))
    get_books(ctx, "missing", "first_page", 10)
    assert readarr.calls
    for call in readarr.calls:
        assert call.get("media_type") == expected


# ---------------------------------------------------------------------------
# Folder matching
# ---------------------------------------------------------------------------


def test_clean_audiobook_name_strips_release_noise():
    raw = "Brandon Sanderson - Mistborn - The Final Empire (2006) [Michael Kramer] 64k Unabridged"
    assert clean_audiobook_name(raw) == "Brandon Sanderson - Mistborn - The Final Empire"


def test_folder_match_on_chapter_files():
    directory = "@@share\\Audiobooks\\Brandon Sanderson\\Mistborn - The Final Empire [Michael Kramer]"
    files = chapter_files(directory, 12)
    matches = audiobook_folder_match(target_dict(), files, "peer", ["m4b", "mp3"], [], 0.5)
    assert len(matches) == 1
    assert matches[0]["directory"] == directory
    assert matches[0]["extension"] == "mp3"
    assert len(matches[0]["files"]) == 12


def test_folder_match_single_m4b_in_generic_folder():
    files = [{"filename": "@@share\\Audiobooks\\Brandon Sanderson - Mistborn - The Final Empire.m4b", "size": 500 * MB}]
    matches = audiobook_folder_match(target_dict(), files, "peer", ["m4b", "mp3"], [], 0.5)
    assert len(matches) == 1
    assert matches[0]["extension"] == "m4b"


def test_folder_match_rejects_other_author():
    directory = "@@share\\Audiobooks\\Someone Else\\Mistborn - The Final Empire"
    matches = audiobook_folder_match(target_dict(), chapter_files(directory, 5), "peer", ["mp3"], [], 0.5)
    assert matches == []


def test_folder_match_prefers_configured_format_order():
    base = "@@share\\Audiobooks\\Brandon Sanderson"
    files = chapter_files(f"{base}\\Mistborn - The Final Empire (mp3)", 10) + [
        {"filename": f"{base}\\Mistborn - The Final Empire (m4b)\\Mistborn.m4b", "size": 600 * MB}
    ]
    matches = audiobook_folder_match(target_dict(), files, "peer", ["m4b", "mp3"], [], 0.5)
    assert [m["extension"] for m in matches] == ["m4b", "mp3"]


def test_folder_match_skips_ignored_users():
    directory = "@@share\\Brandon Sanderson\\Mistborn - The Final Empire"
    assert audiobook_folder_match(target_dict(), chapter_files(directory, 3), "blocked", ["mp3"], ["blocked"], 0.5) == []


# ---------------------------------------------------------------------------
# slskd backend with a fake client
# ---------------------------------------------------------------------------


class FakeSearches:
    def __init__(self, responses):
        self.responses = responses

    def search_text(self, **kwargs):
        return {"id": "s1"}

    def state(self, search_id, include):
        return {"state": "Completed"}

    def search_responses(self, search_id):
        return self.responses

    def delete(self, search_id):
        pass


class FakeUsers:
    def __init__(self, listings):
        self.listings = listings
        self.browsed = []

    def directory(self, username, directory):
        self.browsed.append((username, directory))
        # Shape per slskd's UsersController: a list of Soulseek directories,
        # {"name", "fileCount", "files": [{"filename": <basename>, "size", ...}]}
        listing = self.listings[(username, directory)]
        return [{"name": directory, "fileCount": len(listing["files"]), "files": listing["files"]}]


class FakeTransfers:
    def __init__(self, register_count=None):
        self.enqueued = []
        self.cancelled = []
        self.register_count = register_count

    def enqueue(self, username, files):
        self.enqueued = list(files)
        return True

    def get_downloads(self, username):
        files = self.enqueued if self.register_count is None else self.enqueued[: self.register_count]
        directory = files[0]["filename"].rsplit("\\", 1)[0] if files else ""
        return {
            "directories": [
                {
                    "directory": directory,
                    "fileCount": len(files),
                    "files": [
                        {"filename": f["filename"], "id": f"id{i}", "size": f["size"], "state": "Queued, Remotely", "bytesTransferred": 0}
                        for i, f in enumerate(files)
                    ],
                }
            ]
        }

    def cancel_download(self, username, id, remove=False):
        self.cancelled.append(id)
        return True

    def get_all_downloads(self):
        return []


class FakeSlskd:
    def __init__(self, responses=None, listings=None, register_count=None):
        self.searches = FakeSearches(responses or [])
        self.users = FakeUsers(listings or {})
        self.transfers = FakeTransfers(register_count)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(slskd_backend.time, "sleep", lambda s: None)
    monkeypatch.setattr(download_module.time, "sleep", lambda s: None)


def make_backend(slskd, **search_settings):
    config = make_config(**search_settings)
    ctx = Context(config=config, slskd=slskd, readarr=None)
    return SlskdBackend(ctx)


def test_audiobook_search_browses_full_folder():
    directory = "@@share\\Audiobooks\\Brandon Sanderson\\Mistborn - The Final Empire"
    all_chapters = chapter_files(directory, 20)
    # The search response only includes some chapters; browsing reveals all 20 plus a cover and a subfolder file
    responses = [{"username": "peer", "files": all_chapters[:6]}]
    listing = {
        "directory": directory,
        "files": [{"filename": f["filename"].rsplit("\\", 1)[1], "size": f["size"]} for f in all_chapters] + [{"filename": "cover.jpg", "size": 100}],
    }
    slskd = FakeSlskd(responses=responses, listings={("peer", directory): listing})
    backend = make_backend(slskd)

    results = backend.search(audiobook_target())

    assert len(results) == 1
    result = results[0]
    assert result.extra["media_type"] == "audiobook"
    assert len(result.extra["files"]) == 20
    assert all(f["filename"].startswith(directory + "\\") for f in result.extra["files"])
    assert slskd.users.browsed == [("peer", directory)]


def test_audiobook_search_skips_small_folders():
    directory = "@@share\\Brandon Sanderson\\Mistborn - The Final Empire"
    tiny = chapter_files(directory, 2, size=1 * MB)
    listing = {"directory": directory, "files": [{"filename": f["filename"].rsplit("\\", 1)[1], "size": f["size"]} for f in tiny]}
    slskd = FakeSlskd(responses=[{"username": "peer", "files": tiny}], listings={("peer", directory): listing})
    backend = make_backend(slskd, audiobook_min_size_mb="10", max_search_fallbacks="1")
    assert backend.search(audiobook_target()) == []


def test_ebook_search_unchanged_single_file():
    files = [{"filename": "@@share\\Books\\Brandon Sanderson - Mistborn - The Final Empire.epub", "size": 2 * MB}]
    slskd = FakeSlskd(responses=[{"username": "peer", "files": files}])
    backend = make_backend(slskd)
    target = audiobook_target(formats=("epub",))
    target.media_type = "ebook"

    results = backend.search(target)

    assert len(results) == 1
    assert results[0].extra["files"] == files
    assert "media_type" not in results[0].extra
    assert slskd.users.browsed == []


def _audiobook_result(backend, count=8):
    directory = "@@share\\Brandon Sanderson\\Mistborn - The Final Empire"
    files = chapter_files(directory, count)
    listing = {"directory": directory, "files": [{"filename": f["filename"].rsplit("\\", 1)[1], "size": f["size"]} for f in files]}
    backend.client.searches.responses = [{"username": "peer", "files": files}]
    backend.client.users.listings = {("peer", directory): listing}
    return backend.search(audiobook_target())[0], directory


def test_audiobook_download_enqueues_whole_folder():
    backend = make_backend(FakeSlskd())
    result, directory = _audiobook_result(backend)

    task = backend.download(audiobook_target(), result)

    assert task is not None
    assert task.task_id == f"peer|{directory}"
    assert len(task.extra["files"]) == 8
    assert len(task.extra["expected_files"]) == 8
    assert task.extra["media_type"] == "audiobook"
    assert task.local_dir == "Mistborn - The Final Empire"


def test_audiobook_download_cancels_when_files_missing():
    backend = make_backend(FakeSlskd(register_count=5))
    result, _ = _audiobook_result(backend)

    task = backend.download(audiobook_target(), result)

    assert task is None
    assert len(backend.client.transfers.cancelled) == 5


def test_reconcile_audiobook_all_on_disk(tmp_path):
    backend = make_backend(FakeSlskd())
    backend.config["Slskd"]["download_dir"] = str(tmp_path)
    folder = tmp_path / "Mistborn - The Final Empire"
    folder.mkdir()
    names = ["01.mp3", "02.mp3"]
    for n in names:
        (folder / n).write_bytes(b"x")
    backend.client.transfers.get_all_downloads = lambda: []

    task = backend.reconcile_task(
        {
            "task_id": "peer|dir",
            "book_title": TITLE,
            "author_name": AUTHOR,
            "book_id": 7,
            "filename": "01.mp3",
            "local_dir": folder.name,
            "extra": {"username": "peer", "file_dir": "@@share\\Mistborn - The Final Empire", "media_type": "audiobook", "expected_files": names},
        }
    )

    assert task is not None
    assert task.status == DownloadStatus.COMPLETED


def test_reconcile_audiobook_fails_when_files_lost(tmp_path):
    backend = make_backend(FakeSlskd())
    backend.config["Slskd"]["download_dir"] = str(tmp_path)
    backend.client.transfers.get_all_downloads = lambda: []

    task = backend.reconcile_task(
        {
            "task_id": "peer|dir",
            "book_title": TITLE,
            "author_name": AUTHOR,
            "book_id": 7,
            "filename": "01.mp3",
            "local_dir": "gone",
            "extra": {"username": "peer", "file_dir": "x", "media_type": "audiobook", "expected_files": ["01.mp3"]},
        }
    )
    assert task is None


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


class FakeImportClient:
    def __init__(self):
        self.commands = []

    def post_command(self, name, **kwargs):
        self.commands.append((name, kwargs))
        return {"id": len(self.commands)}

    def get_command(self, id_):
        return {"id": id_, "status": "completed", "message": "Imported 1 book", "body": {"path": self.commands[id_ - 1][1]["path"]}}


def test_audiobook_import_moves_folder_and_scans_book_folder(tmp_path):
    local = tmp_path / "downloads"
    source = local / "Mistborn - The Final Empire"
    source.mkdir(parents=True)
    names = [f"{i:02d}.mp3" for i in range(1, 4)]
    for n in names:
        (source / n).write_bytes(b"audio")

    config = make_config()
    config["Slskd"]["download_dir"] = str(local)
    config["Slskd"]["readarr_download_dir"] = "/chaptarr/downloads"
    client = FakeImportClient()
    ctx = Context(config=config, slskd=None, readarr=client)

    postprocess.process_imports(
        ctx,
        [
            {
                "author_name": AUTHOR,
                "title": TITLE,
                "bookId": 7,
                "dir": source.name,
                "filename": names[0],
                "files": [],
                "expected_files": names,
                "backend_name": "slskd",
                "media_type": "audiobook",
            }
        ],
    )

    book_dir = local / postprocess.AUDIOBOOK_STAGING_DIR / "Brandon Sanderson" / "Mistborn The Final Empire"
    assert sorted(os.listdir(book_dir)) == names
    assert not source.exists()
    assert client.commands == [
        ("DownloadedBooksScan", {"path": os.path.join("/chaptarr/downloads", postprocess.AUDIOBOOK_STAGING_DIR, "Brandon Sanderson", "Mistborn The Final Empire")})
    ]


def test_audiobook_import_with_missing_files_is_not_imported(tmp_path):
    local = tmp_path / "downloads"
    source = local / "Mistborn - The Final Empire"
    source.mkdir(parents=True)
    (source / "01.mp3").write_bytes(b"audio")

    config = make_config()
    config["Slskd"]["download_dir"] = str(local)
    client = FakeImportClient()
    ctx = Context(config=config, slskd=None, readarr=client)

    postprocess.process_imports(
        ctx,
        [
            {
                "author_name": AUTHOR,
                "title": TITLE,
                "bookId": 7,
                "dir": source.name,
                "filename": "01.mp3",
                "expected_files": ["01.mp3", "02.mp3"],
                "backend_name": "slskd",
                "media_type": "audiobook",
            }
        ],
    )

    assert client.commands == []
    assert (local / "failed_imports" / source.name / "01.mp3").exists()


# ---------------------------------------------------------------------------
# Workflow: per-book media type in "both" mode
# ---------------------------------------------------------------------------


class CapturingOrchestrator:
    def __init__(self):
        self.targets = []

    def start_targets(self, targets, on_complete=None):
        self.targets = targets
        return []

    def monitor_until(self, tasks, on_complete=None, deadline=None):
        return [], []

    def get_backend(self, name):
        return None


class EditionReadarr:
    def get_edition(self, book_id):
        return []


def test_both_mode_builds_targets_per_book_media_type(tmp_path):
    from rsoul.workflow import run_workflow

    config = make_config(media_mode="both")
    config["Backends"] = {"slskd_enabled": "False"}
    orchestrator = CapturingOrchestrator()
    ctx = Context(config=config, slskd=None, readarr=EditionReadarr(), config_dir=str(tmp_path), orchestrator=orchestrator)

    targets = [
        {"book": {"id": 1, "title": "Book A", "mediaType": "audiobook"}, "author": {"authorName": "Author A"}},
        {"book": {"id": 2, "title": "Book B", "mediaType": "ebook"}, "author": {"authorName": "Author B"}},
    ]
    run_workflow(ctx, targets)

    by_id = {t.book_id: t for t in orchestrator.targets}
    assert by_id[1].media_type == "audiobook"
    assert by_id[1].allowed_filetypes == ["m4b", "mp3"]
    assert by_id[2].media_type == "ebook"
    assert by_id[2].allowed_filetypes == ["epub", "azw3", "mobi"]
