"""v11 durability, scheduling, API errors and restart integration regressions."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from rsoul.config import Context
from rsoul.state import StateManager, StatePersistenceError
from rsoul.backends import DownloadTask, DownloadStatus
from rsoul.wanted_queue import WantedQueue
from rsoul.workflow import build_targets, task_to_grab_item, run_workflow, _RunResults, _follow_up_pending_imports
from rsoul import persistence


def cfg():
    import configparser
    c = configparser.ConfigParser(interpolation=None)
    c.read_dict({
        'Readarr': {'import_method': 'targeted'},
        'Slskd': {'download_dir': '/downloads'},
        'Search Settings': {'media_mode': 'audiobook', 'number_of_books_to_grab': '2', 'wanted_scan_pages': '2', 'wanted_backlog_limit': '10'},
        'Download Settings': {'max_active_downloads': '2', 'monitor_window': '30'},
    })
    return c


def task(extra=None):
    return DownloadTask('task', 'slskd', DownloadStatus.DOWNLOADING, 'Dune', 'Frank Herbert', 7, 'Dune.mp3', local_dir='Dune', extra=extra or {})


def test_state_upsert_and_deepcopy_survive_restart(tmp_path):
    s = StateManager(str(tmp_path))
    t = task({'files': [{'filename': 'Dune.mp3', 'size': 123}]})
    s.add_task(t)
    t.extra['files'][0]['size'] = 456
    assert s.get_items()[0]['extra']['files'][0]['size'] == 123
    s.update_task(t)
    s.add_task(t)
    snapshot = s.get_items()
    snapshot[0]['extra']['files'].clear()
    restarted = StateManager(str(tmp_path))
    assert len(restarted.get_items()) == 1
    assert restarted.get_items()[0]['extra']['files'][0]['size'] == 456


def test_failed_commit_preserves_previous_checkpoint(tmp_path, monkeypatch):
    s = StateManager(str(tmp_path))
    s.add_task(task({'phase': 'old'}))
    old = s.filepath.read_bytes()
    monkeypatch.setattr(persistence.os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(StatePersistenceError):
        s.set_extra('task', {'phase': 'new'})
    assert s.get_items()[0]['extra']['phase'] == 'old'
    assert s.filepath.read_bytes() == old
    assert not list(tmp_path.glob('*.tmp'))


def test_missing_state_volume_stops_mutation(tmp_path):
    s = StateManager(str(tmp_path / 'missing-mount'))
    with pytest.raises(StatePersistenceError):
        s.add_task(task())
    assert s.get_items() == []
    assert not (tmp_path / 'missing-mount').exists()


def test_missing_task_cannot_receive_untracked_intent(tmp_path):
    s = StateManager(str(tmp_path))
    with pytest.raises(StatePersistenceError):
        s.set_extra('not-saved', {'import_pending': {}})


def test_corrupt_state_stays_blocked_on_second_start(tmp_path):
    path = tmp_path / 'grab_list_state.json'
    path.write_text('{broken')
    for _ in range(2):
        with pytest.raises(StatePersistenceError):
            StateManager(str(tmp_path)).ensure_writable()
    assert path.read_text() == '{broken'


def test_legacy_task_migration_keeps_evidence_and_identity(tmp_path):
    old = {'bookId': 7, 'title': 'Dune', 'author_name': 'Frank Herbert', 'username': 'peer', 'filename': 'Dune.mp3', 'dir': 'Dune', 'files': [{'filename': 'Dune.mp3', 'size': 55}]}
    (tmp_path / 'grab_list_state.json').write_text(json.dumps([old]))
    s = StateManager(str(tmp_path))
    assert s.migrate_legacy_tasks() == 1
    saved = s.get_items()[0]
    assert saved['extra']['legacy_state'] == old
    assert saved['extra']['files'][0]['size'] == 55
    assert StateManager(str(tmp_path)).migrate_legacy_tasks() == 0


def test_task_to_import_contains_detached_durable_identity():
    t = task({'intended_identity': {'book_id': 7}, 'files': [{'filename': 'x.mp3', 'size': 3}]})
    item = task_to_grab_item(t, '/downloads')
    assert item['task_id'] == t.task_id
    item['_task_extra']['intended_identity']['book_id'] = 99
    item['files'][0]['size'] = 4
    assert t.extra['intended_identity']['book_id'] == 7
    assert t.extra['files'][0]['size'] == 3


class WantedAPI:
    def __init__(self):
        self.books = [{'id': n, 'authorId': 1, 'title': f'Title {n}', 'mediaType': 'audiobook', 'monitored': True} for n in range(1, 7)]
        self.pages = []
    def get_missing(self, page=1, page_size=2, **kwargs):
        self.pages.append(page)
        return {'totalRecords': len(self.books), 'records': copy.deepcopy(self.books[(page-1)*page_size:page*page_size])}
    get_cutoff = get_missing
    def get_book(self, book_id):
        return copy.deepcopy(next(b for b in self.books if b['id'] == book_id))


def test_queue_keeps_unattempted_records_after_advancing_cursor(tmp_path):
    api = WantedAPI()
    ctx = Context(cfg(), None, api, str(tmp_path))
    queue = WantedQueue(str(tmp_path))
    assert [b['id'] for b in queue.fetch(ctx, 'missing', 'incrementing_page', 2)] == [1, 2, 3, 4]
    queue.acknowledge({1})
    restarted = WantedQueue(str(tmp_path))
    later = restarted.fetch(ctx, 'missing', 'incrementing_page', 2)
    assert {2, 3, 4, 5, 6}.issubset({b['id'] for b in later})
    assert api.pages[:3] == [1, 2, 3]


def test_queue_checkpoint_failure_does_not_advance_disk_cursor(tmp_path, monkeypatch):
    api = WantedAPI()
    ctx = Context(cfg(), None, api, str(tmp_path))
    queue = WantedQueue(str(tmp_path))
    queue.fetch(ctx, 'missing', 'incrementing_page', 2)
    previous = queue.path.read_bytes()
    monkeypatch.setattr('rsoul.wanted_queue.atomic_write_json', lambda *args: (_ for _ in ()).throw(StatePersistenceError('full')))
    with pytest.raises(StatePersistenceError):
        queue.fetch(ctx, 'missing', 'incrementing_page', 2)
    assert queue.path.read_bytes() == previous


def test_queue_refresh_obeys_monitor_changes(tmp_path):
    api = WantedAPI(); ctx = Context(cfg(), None, api, str(tmp_path))
    queue = WantedQueue(str(tmp_path)); queue.fetch(ctx, 'missing', 'incrementing_page', 2)
    api.books[2]['audiobookMonitored'] = False
    ctx.config.set('Search Settings', 'wanted_scan_pages', '1')
    later = queue.fetch(ctx, 'missing', 'incrementing_page', 2)
    assert 3 not in {b['id'] for b in later}
    assert 3 not in {row['book']['id'] for entry in queue.data['lists'].values() for row in entry['candidates']}


def test_queue_api_outage_retains_deferred_snapshot(tmp_path):
    api = WantedAPI(); ctx = Context(cfg(), None, api, str(tmp_path))
    queue = WantedQueue(str(tmp_path)); queue.fetch(ctx, 'missing', 'incrementing_page', 2)
    ctx.config.set('Search Settings', 'wanted_scan_pages', '1')
    api.get_book = lambda _: (_ for _ in ()).throw(requests.ConnectionError('offline'))
    later = queue.fetch(ctx, 'missing', 'incrementing_page', 2)
    assert {b['id'] for b in later} == {5, 6}
    assert len(queue.data['lists']['missing:audiobook']['candidates']) == 6


def test_eligibility_is_applied_before_active_cap(tmp_path):
    api = WantedAPI(); api.ver_uri = '/v1'
    api._get = lambda *args, **kwargs: []
    api.get_edition = lambda _: []
    context = Context(cfg(), None, api, str(tmp_path), state=StateManager(str(tmp_path)))
    context.config.set('Search Settings', 'excluded_book_ids', '1,2')
    class Orch:
        unchecked_task_ids = set()
        def start_targets(self, targets, on_complete=None):
            self.selected = [t.book_id for t in targets]; return []
        def monitor_until(self, tasks, on_complete=None, deadline=None): return [], []
        def get_backend(self, name): return None
    context.orchestrator = Orch()
    target_dicts = [{'book': b, 'author': {'authorName': 'Author'}} for b in api.books]
    run_workflow(context, target_dicts)
    assert context.orchestrator.selected == [3, 4]


def test_disable_sync_never_resubmits_pending_import(tmp_path, monkeypatch):
    context = Context(cfg(), None, object(), str(tmp_path), state=StateManager(str(tmp_path)))
    context.config.set('Readarr', 'disable_sync', 'true')
    pending = {'local_dir': str(tmp_path), 'phase': 'submission_unknown', 'files': []}
    context.state.add_task(task({'media_type': 'audiobook', 'import_pending': pending}))
    monkeypatch.setattr('rsoul.postprocess.check_pending_import', lambda *args, **kwargs: pytest.fail('sync disabled'))
    result = _RunResults(context, [])
    try:
        assert _follow_up_pending_imports(context, result) == 1
        assert context.state.get_items()[0]['extra']['import_pending']['phase'] == 'submission_unknown'
    finally:
        result.wait_for_imports()


def test_import_exception_keeps_state_and_never_cleans_transfers(tmp_path, monkeypatch):
    context = Context(cfg(), None, object(), str(tmp_path), state=StateManager(str(tmp_path)))
    context.orchestrator = SimpleNamespace(get_backend=lambda _: SimpleNamespace(cleanup=lambda t: pytest.fail('premature cleanup')))
    t = task(); t.status = DownloadStatus.COMPLETED; context.state.add_task(t)
    monkeypatch.setattr('rsoul.postprocess.process_imports', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('permission denied')))
    result = _RunResults(context, [])
    result.on_complete(t); result.wait_for_imports()
    assert context.state.get_items()[0]['extra']['recovery_review']['reason'].startswith('Import interrupted')


def test_http_errors_keep_status_headers_and_body():
    from readarr_api.request_handler import _process_response
    from readarr_api.exceptions import PyarrRateLimit, PyarrError
    for status, kind in [(429, PyarrRateLimit), (503, PyarrError)]:
        response = requests.Response(); response.status_code = status
        response.headers = {'Retry-After': '90', 'Content-Type': 'application/json'}
        response._content = b'{"message":"busy"}'
        with pytest.raises(kind) as caught:
            _process_response(response)
        assert caught.value.response is response
        assert caught.value.status_code == status
        assert caught.value.headers['Retry-After'] == '90'


def test_unavailable_lock_refuses_mutation(tmp_path, monkeypatch):
    from rsoul import locking
    monkeypatch.setattr(locking, 'fcntl', None)
    with pytest.raises(locking.LockUnavailable):
        locking.InstanceLock(str(tmp_path)).acquire()


def test_foreground_checkpoint_failure_latches_before_join(tmp_path, monkeypatch):
    context = Context(cfg(), None, SimpleNamespace(get_edition=lambda _: []), str(tmp_path), state=StateManager(str(tmp_path)))
    problem = StatePersistenceError('foreground checkpoint failed')
    class Orch:
        unchecked_task_ids = set()
        def get_backend(self, _): return None
        def start_targets(self, targets, on_complete=None): raise problem
    context.orchestrator = Orch()
    original = _RunResults.wait_for_imports
    seen = []
    def wait(results):
        seen.append(getattr(results.ctx, 'fatal_state_error', None))
        original(results)
    monkeypatch.setattr(_RunResults, 'wait_for_imports', wait)
    with pytest.raises(StatePersistenceError):
        run_workflow(context, [{'book': {'id': 42, 'title': 'Book'}, 'author': {'authorName': 'Author'}}])
    assert seen == [problem]


def test_import_writer_observes_foreground_fatal_before_moving(tmp_path):
    from test_v11_imports import setup
    from rsoul import import_transaction as transaction
    ctx, item, server, _ = setup(tmp_path)
    pending = transaction.new_transaction(item, str(server.root), '/remote', 'targeted')
    persist = transaction.context_writer(ctx, item, pending)
    transaction._checkpoint(pending, persist)
    pending.update(phase='staging', inputs_validated=True)
    ctx.fatal_state_error = StatePersistenceError('disk unavailable elsewhere')
    with pytest.raises(StatePersistenceError, match='disk unavailable'):
        transaction.recover_staging(pending, persist)
    assert all(os.path.isfile(f['source_path']) for f in pending['manifest'])
    assert all(not os.path.exists(f['stage_path']) for f in pending['manifest'])
    assert server.posts == []
