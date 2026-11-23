"""v11 transfer/search contracts: real state/history IO, fake external services.

These tests never contact slskd or Readarr. Filesystem sizes, persisted restart data,
HTTP exceptions and the actual backend/orchestrator are exercised directly.
"""
import copy
import json
from configparser import ConfigParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from requests import HTTPError, Response, Timeout

from rsoul.backends.base import BackendUnavailable, DownloadStatus, DownloadTarget, SearchResult, DownloadTask
from rsoul.backends.slskd_backend import SlskdBackend, _generate_fallback_queries, _query_key
from rsoul.config import Context
from rsoul.history import HistoryManager
from rsoul.orchestrator import DownloadOrchestrator
from rsoul.outcomes import SearchIncomplete, FailureKind
from rsoul.persistence import StatePersistenceError
from rsoul.state import StateManager
from rsoul import search_control
from rsoul import postprocess
from rsoul.backends import slskd_backend


def http_error(status, retry_after=None):
    response = Response()
    response.status_code = status
    if retry_after is not None:
        response.headers['Retry-After'] = str(retry_after)
    return HTTPError(str(status), response=response)


class Transfers:
    def __init__(self):
        self.rows = []
        self.calls = []
        self.mode = 'normal'
        self.before_post = None
        self.next_number = 1
        self.cleanup_fails = False

    def enqueue(self, username, files):
        if self.before_post:
            self.before_post()
        self.calls.append((username, copy.deepcopy(files)))
        if self.mode == 'rejected':
            raise http_error(403)
        for file in files if self.mode != 'lost_no_records' else []:
            self.rows.append(dict(file, id=f'id-{self.next_number}', username=username,
                                 state='Queued, Remotely', bytesTransferred=0))
            self.next_number += 1
        if self.mode.startswith('lost'):
            raise Timeout('response lost after request sent')
        return True

    def get_downloads(self, username):
        directories = {}
        for row in self.rows:
            if row['username'] != username:
                continue
            directory = row['filename'].rsplit('\\', 1)[0]
            directories.setdefault(directory, []).append(copy.deepcopy(row))
        return {'username': username, 'directories': [{'directory': name, 'files': files} for name, files in directories.items()]}

    def get_all_downloads(self):
        return [self.get_downloads(user) for user in {row['username'] for row in self.rows}]

    def cancel_download(self, username, id, remove=False):
        if self.cleanup_fails:
            raise Timeout('cleanup unavailable')
        if remove:
            self.rows = [row for row in self.rows if not (row['username'] == username and row['id'] == id)]
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(search_control.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(slskd_backend, 'sibling_titles', lambda *args: [])
    config = ConfigParser()
    downloads = tmp_path / 'downloads'
    downloads.mkdir()
    config.read_dict({
        'Slskd': {'download_dir': str(downloads), 'host_url': 'http://fake.invalid'},
        'Search Settings': {'audiobook_min_size_mb': '0', 'search_min_interval': '0', 'max_search_fallbacks': '5'},
        'General': {'batch_delay': '0'},
        'Download Settings': {'completed_file_settle_time': '0'},
    })
    transfers = Transfers()
    client = SimpleNamespace(transfers=transfers, options=SimpleNamespace(get=lambda: {}),
                             searches=SimpleNamespace(delete=lambda sid: None))
    readarr = SimpleNamespace(ver_uri='api/v1', _get=lambda *args, **kwargs: [
        {'id': 7, 'authorId': 8, 'title': 'A Book', 'foreignBookId': 'foreign-book'}])
    ctx = Context(config, client, readarr, config_dir=str(tmp_path), state=StateManager(str(tmp_path)), history=HistoryManager(str(tmp_path)))
    backend = SlskdBackend(ctx)
    return ctx, backend, transfers, downloads


def target(book_id=7):
    return DownloadTarget(book_id, 'A Book', 'An Author', '', ['mp3'], media_type='audiobook',
                          readarr_book={'id': book_id, 'authorId': 8, 'foreignBookId': 'foreign-book'},
                          readarr_author={'id': 8, 'foreignAuthorId': 'foreign-author'})


def result(peer='peer', count=2, directory='Author\\A Book'):
    files = [{'filename': f'{directory}\\{i:02}.mp3', 'size': 10} for i in range(1, count + 1)]
    return SearchResult('A Book', 'An Author', '01.mp3', count * 10, 'mp3', 'slskd', f'{peer}|{directory}',
                        username=peer, extra={'username': peer, 'file_dir': directory, 'files': files, 'media_type': 'audiobook'})


def write_completed(downloads, task, names=None):
    directory = downloads / task.local_dir
    directory.mkdir(exist_ok=True)
    for file in task.extra['manifest']:
        if names is None or file['name'] in names:
            (directory / file['name']).write_bytes(b'x' * file['size'])


def saved(ctx, task):
    return next(row for row in ctx.state.get_tasks_for_orchestrator() if row['task_id'] == task.task_id)


def test_intent_is_durable_before_post_and_contains_original_identity(env):
    ctx, backend, transfers, _ = env
    def inspect():
        rows = json.loads(ctx.state.filepath.read_text())
        assert len(rows) == 1
        assert rows[0]['extra']['enqueue']['phase'] == 'prepared'
        assert len(rows[0]['extra']['manifest']) == 2
        assert rows[0]['extra']['intended_identity']['foreign_book_id'] == 'foreign-book'
    transfers.before_post = inspect
    task = backend.download(target(), result())
    assert saved(ctx, task)['extra']['enqueue']['phase'] == 'registered'


def test_failed_checkpoint_prevents_enqueue(env, monkeypatch):
    ctx, backend, transfers, _ = env
    def fail(task):
        raise StatePersistenceError('disk full')
    monkeypatch.setattr(ctx.state, 'update_task', fail)
    with pytest.raises(StatePersistenceError):
        backend.download(target(), result())
    assert transfers.calls == []


def test_missing_state_manager_prevents_enqueue(env):
    ctx, backend, transfers, _ = env
    ctx.state = None
    with pytest.raises(StatePersistenceError):
        backend.download(target(), result())
    assert transfers.calls == []


def test_lost_response_adopts_exact_new_ids_without_duplicate_post(env):
    ctx, backend, transfers, _ = env
    transfers.mode = 'lost'
    task = backend.download(target(), result())
    assert task.extra['enqueue']['phase'] == 'registered'
    assert len(task.extra['files']) == 2
    assert len(transfers.calls) == 1
    assert len(saved(ctx, task)['extra']['files']) == 2


def test_unknown_enqueue_kept_across_restart_and_later_reconciled(env):
    ctx, backend, transfers, _ = env
    transfers.mode = 'lost_no_records'
    task = backend.download(target(), result())
    assert task.extra['enqueue']['phase'] == 'unknown'
    assert task.status == DownloadStatus.PENDING
    assert saved(ctx, task)['extra']['manifest'] == task.extra['manifest']
    transfers.mode = 'normal'
    # Simulate late server registration; not a second application POST.
    for i, file in enumerate(task.extra['manifest']):
        transfers.rows.append(dict(file, username='peer', id=f'late-{i}', state='Queued, Remotely', bytesTransferred=0))
    restored = SlskdBackend(ctx).reconcile_task(saved(ctx, task))
    assert restored.task_id == task.task_id
    assert restored.extra['enqueue']['phase'] == 'registered'
    assert len(transfers.calls) == 1


def test_after_post_checkpoint_failure_recovers_prepared_intent(env, monkeypatch):
    ctx, backend, transfers, _ = env
    real_update = ctx.state.update_task
    writes = []
    def fail_second(task):
        writes.append(task.task_id)
        if len(writes) > 1:
            raise StatePersistenceError('failed after POST')
        real_update(task)
    monkeypatch.setattr(ctx.state, 'update_task', fail_second)
    with pytest.raises(StatePersistenceError):
        backend.download(target(), result())
    rows = json.loads(ctx.state.filepath.read_text())
    assert rows[0]['extra']['enqueue']['phase'] == 'prepared'
    monkeypatch.setattr(ctx.state, 'update_task', real_update)
    restored = backend.reconcile_task(rows[0])
    assert restored.extra['enqueue']['phase'] == 'registered'
    assert len(transfers.calls) == 1


@pytest.mark.parametrize('bad_kind', ['short', 'directory', 'symlink'])
def test_live_completion_requires_regular_exact_size_files(env, bad_kind):
    ctx, backend, transfers, downloads = env
    task = backend.download(target(), result(count=1))
    location = downloads / task.local_dir
    location.mkdir()
    bad = location / '01.mp3'
    if bad_kind == 'short':
        bad.write_bytes(b'x')
    elif bad_kind == 'directory':
        bad.mkdir()
    else:
        (downloads / 'target.mp3').write_bytes(b'x' * 10)
        bad.symlink_to(downloads / 'target.mp3')
    transfers.rows[0].update(state='Completed, Succeeded', bytesTransferred=10)
    checked = backend.get_status(task)
    assert checked.status == DownloadStatus.FAILED
    assert checked.extra['failure_kind'] == 'local_file_invalid'


def test_resume_keeps_full_manifest_and_sizes_after_cleared_chapters(env):
    ctx, backend, transfers, downloads = env
    task = backend.download(target(), result())
    write_completed(downloads, task, {'01.mp3'})
    transfers.rows = transfers.rows[1:]
    resumed = backend.reconcile_task(saved(ctx, task))
    assert len(resumed.extra['files']) == 2
    assert [f['size'] for f in resumed.extra['manifest']] == [10, 10]
    assert resumed.bytes_transferred == 10
    write_completed(downloads, resumed)
    transfers.rows = []
    resumed2 = backend.reconcile_task(saved(ctx, resumed))
    assert resumed2.status == DownloadStatus.COMPLETED
    assert len(resumed2.extra['files']) == 2
    assert resumed2.bytes_transferred == 20


def test_resume_does_not_accept_truncated_cleared_file(env):
    ctx, backend, transfers, downloads = env
    task = backend.download(target(), result(count=1))
    write_completed(downloads, task)
    (downloads / task.local_dir / '01.mp3').write_bytes(b'x')
    transfers.rows = []
    restored = backend.reconcile_task(saved(ctx, task))
    assert restored.status == DownloadStatus.FAILED
    assert restored.extra['manifest'][0]['size'] == 10


def test_legacy_no_size_is_retained_unverified_even_when_path_exists(env):
    ctx, backend, _, downloads = env
    folder = downloads / 'A Book'
    folder.mkdir()
    (folder / '01.mp3').write_bytes(b'x')
    data = dict(task_id='old', backend_name='slskd', book_title='A Book', author_name='An Author', book_id=7,
                filename='01.mp3', local_dir='A Book', extra={'username': 'peer', 'file_dir': 'Author\\A Book',
                'media_type': 'audiobook', 'expected_files': ['01.mp3'], 'files': []})
    restored = backend.reconcile_task(data)
    assert restored.status == DownloadStatus.PENDING
    assert restored.poll_failed
    assert restored.extra['needs_review']
    assert restored.extra['failure_kind'] == 'legacy_unverified'
    assert ctx.state.get_tasks_for_orchestrator()[0]['task_id'] == 'old'


def test_old_completed_transfer_cannot_be_adopted_by_lost_enqueue(env):
    _, backend, transfers, _ = env
    old = result(count=1).extra['files'][0]
    transfers.rows = [dict(old, id='old-id', username='peer', state='Completed, Rejected', bytesTransferred=0)]
    transfers.mode = 'lost_no_records'
    task = backend.download(target(), result(count=1))
    assert task.extra['enqueue']['phase'] == 'unknown'
    assert not task.extra['files'][0].get('id')


def test_mixed_empty_search_and_error_is_incomplete(env, monkeypatch):
    ctx, backend, _, _ = env
    calls = []
    def search(*args):
        calls.append(args[1])
        if len(calls) == 2:
            raise http_error(503)
        return [], str(len(calls))
    monkeypatch.setattr(slskd_backend, '_execute_search', search)
    with pytest.raises(SearchIncomplete) as error:
        backend.search(target())
    assert error.value.kind == 'search_incomplete'
    orchestrator = DownloadOrchestrator([backend], ctx)
    calls.clear()
    assert orchestrator.start_download(target()) is None
    assert orchestrator.last_start_failure == 'not_searched'


def test_retry_after_is_persisted_and_shared_across_books_and_restart(env, monkeypatch):
    ctx, backend, _, _ = env
    calls = []
    def search(*args):
        calls.append(args[1])
        raise http_error(429, 3600)
    monkeypatch.setattr(slskd_backend, '_execute_search', search)
    with pytest.raises(SearchIncomplete) as error:
        backend.search(target())
    assert error.value.kind == 'rate_limited'
    assert error.value.retry_at is not None
    with pytest.raises(SearchIncomplete):
        SlskdBackend(ctx).search(target(8))
    assert len(calls) == 1
    data = json.loads((Path(ctx.config_dir) / 'slskd_search_backoff.json').read_text())
    assert data['rate_limit_count'] == 1
    assert data['rate_limit_until'] >= data['next_search_at'] + 3599


def test_search_queries_keep_surname_and_are_bounded_deduplicated(env, monkeypatch):
    ctx, backend, _, _ = env
    ctx.config.set('Search Settings', 'search_query_budget', '3')
    queries = []
    monkeypatch.setattr(slskd_backend, '_execute_search', lambda ctx, query, label: (queries.append(query) or [], str(len(queries))))
    book = DownloadTarget(9, 'The Right of the Line', 'Christopher G. Nuttall', '', ['mp3'], media_type='audiobook')
    backend.search(book)
    assert len(queries) <= 3
    assert len({_query_key(query) for query in queries}) == len(queries)
    assert all('Nuttall' in query for query in queries)
    assert all('Nuttall' in query for query in _generate_fallback_queries(book.author_name, book.book_title, 6))


def test_source_failure_is_scoped_to_work_media_backend_and_release(env):
    ctx, _, _, _ = env
    history = ctx.history
    scope = dict(backend='slskd', author='An Author', media_type='audiobook', release_id='peer|release1')
    history.add_failure('peer', 'A Book', 'rejected', **scope, failure_kind='peer_rejected')
    assert history.is_failed('peer', 'A Book', **scope)
    for changed in [dict(author='Other Author'), dict(media_type='ebook'), dict(backend='other'), dict(release_id='peer|release2')]:
        assert not history.is_failed('peer', 'A Book', **dict(scope, **changed))
    reloaded = HistoryManager(ctx.config_dir)
    assert reloaded.is_failed('peer', 'A Book', **scope)


def test_legacy_history_is_explicitly_ignored_by_scoped_queries(env):
    ctx, _, _, _ = env
    ctx.history.add_failure('peer', 'A Book')
    assert ctx.history.is_failed('peer', 'A Book')
    assert not ctx.history.is_failed('peer', 'A Book', backend='slskd', author='An Author', media_type='audiobook')
    ctx.history.add_failure('peer2', 'A Book', failure_kind='import_failed')
    assert not ctx.history.is_failed('peer2', 'A Book')


def test_async_peer_rejection_tries_retained_candidate_same_run(env):
    ctx, backend, transfers, _ = env
    backend._alternates[7] = iter([result('other-peer', count=1, directory='Other\\A Book')])
    task = backend.download(target(), result(count=1))
    previous_id = task.task_id
    transfers.rows[0]['state'] = 'Completed, Rejected'
    orchestrator = DownloadOrchestrator([backend], ctx)
    assert orchestrator._advance_task(task, None) is False
    assert task.extra['username'] == 'other-peer'
    assert task.task_id != previous_id
    assert len(transfers.calls) == 2
    assert {row['task_id'] for row in ctx.state.get_tasks_for_orchestrator()} == {task.task_id}
    assert all(row['username'] == 'other-peer' for row in transfers.rows)


def test_unknown_enqueue_is_never_retried_or_cancelled_by_timeout(env):
    ctx, backend, transfers, _ = env
    transfers.mode = 'lost_no_records'
    backend._alternates[7] = iter([result('other-peer')])
    task = backend.download(target(), result())
    orchestrator = DownloadOrchestrator([backend], ctx)
    assert orchestrator.check_timeouts(task, task.extra['monitor']['started_at'] + 100000) is None
    assert task.extra['needs_review']
    assert len(transfers.calls) == 1
    assert not backend.cleanup(task)


def test_monitor_during_search_frees_capacity_and_tracks_attempted_ids(env):
    ctx, backend, transfers, downloads = env
    old = backend.download(target(), result(count=1))
    write_completed(downloads, old)
    completed = []
    orchestrator = DownloadOrchestrator([backend], ctx)
    # Completing the resumed task before search frees one slot. The first new target
    # fails, so a second target is attempted; the third waits for the occupied slot.
    attempts = []
    def start(book):
        attempts.append(book.book_id)
        if book.book_id == 8:
            return None
        return DownloadTask(f'new-{book.book_id}', 'slskd', DownloadStatus.QUEUED,
                            book.book_title, book.author_name, book.book_id, 'x.mp3',
                            extra={'monitor': {}, 'files': []})
    orchestrator.start_download = start
    original_advance = orchestrator._advance_task
    orchestrator._advance_task = lambda task, callback: original_advance(task, callback) if task is old else False
    active = orchestrator.start_targets([target(8), target(9), target(10)], completed.append, [old], max_active=1)
    assert attempts == [8, 9]
    assert orchestrator.attempted_book_ids == {8, 9}
    assert [task.book_id for task in active] == [9]
    assert old in completed
    assert orchestrator.remaining_existing_tasks == []


def test_cleanup_outage_does_not_start_an_alternate_or_forget_owner(env):
    ctx, backend, transfers, _ = env
    backend._alternates[7] = iter([result('other-peer', count=1)])
    task = backend.download(target(), result(count=1))
    transfers.rows[0]['state'] = 'Completed, Rejected'
    transfers.cleanup_fails = True
    orchestrator = DownloadOrchestrator([backend], ctx)
    assert orchestrator._advance_task(task, None) is False
    assert task.poll_failed
    assert task.extra['retry_cleanup_pending']
    assert len(transfers.calls) == 1
    assert ctx.state.get_tasks_for_orchestrator()[0]['task_id'] == task.task_id


def test_api_auth_rejection_does_not_blacklist_the_peer(env):
    ctx, backend, transfers, _ = env
    transfers.mode = 'rejected'
    assert backend.download(target(), result()) is None
    assert ctx.history.history == []
    assert ctx.state.get_tasks_for_orchestrator() == []


def test_real_unknown_manifest_survives_disk_reload_after_progress_logging(env):
    ctx, backend, transfers, _ = env
    transfers.mode = 'lost_no_records'
    task = backend.download(target(), result())
    orchestrator = DownloadOrchestrator([backend], ctx)
    assert not orchestrator._advance_task(task, None)
    persisted = StateManager(ctx.config_dir)
    assert persisted.load_error is None
    assert len(persisted.get_tasks_for_orchestrator()) == 1
    assert persisted.get_tasks_for_orchestrator()[0]['extra']['manifest'] == task.extra['manifest']


def test_same_peer_same_title_other_release_is_not_filtered(env, monkeypatch):
    ctx, backend, _, _ = env
    ctx.history.add_failure('peer', 'A Book', 'rejected', backend='slskd', author='An Author', media_type='audiobook',
                            release_id='peer|wrong-release', failure_kind='peer_rejected')
    assert not backend._history_failed('peer', target(), 'peer|good-release')
    assert backend._history_failed('peer', target(), 'peer|wrong-release')


def test_unknown_intent_reserves_names_against_a_different_book(env):
    ctx, backend, transfers, _ = env
    transfers.mode = 'lost_no_records'
    first = backend.download(target(), result())
    assert first.extra['enqueue']['phase'] == 'unknown'
    second_book = target(8)
    second_book.book_title = 'Another Book'
    assert backend.download(second_book, result()) is None
    assert len(transfers.calls) == 1
    assert len(ctx.state.get_tasks_for_orchestrator()) == 1


def test_partial_discard_keeps_journal_and_all_destination_evidence(env, monkeypatch):
    ctx, backend, _, downloads = env
    task = backend.download(target(), result())
    write_completed(downloads, task)
    real_move = postprocess.shutil.move
    calls = []
    def partial(source, destination):
        record = saved(ctx, task)['extra']['discard']
        assert record['phase'] == 'prepared'
        assert str(destination).startswith(record['destination'])
        calls.append(source)
        if len(calls) == 2:
            raise OSError('destination disk full')
        return real_move(source, destination)
    monkeypatch.setattr(postprocess.shutil, 'move', partial)
    assert backend.discard(task) is False
    record = saved(ctx, task)['extra']['discard']
    assert record['phase'] == 'incomplete'
    assert record['moved'] == ['01.mp3']
    assert record['remaining'] == ['02.mp3']
    assert (Path(record['destination']) / '01.mp3').read_bytes() == b'x' * 10
    assert (downloads / task.local_dir / '02.mp3').exists()
    monkeypatch.setattr(postprocess.shutil, 'move', real_move)
    assert backend.discard(task) is True
    assert (Path(record['destination']) / '02.mp3').read_bytes() == b'x' * 10
    assert saved(ctx, task)['extra']['discard']['phase'] == 'complete'


def test_fatal_background_checkpoint_stops_post_after_prepared_intent(env, monkeypatch):
    ctx, backend, transfers, _ = env
    real_update = ctx.state.update_task
    def save_then_worker_fails(task):
        real_update(task)
        ctx.fatal_state_error = StatePersistenceError('background import journal failed')
    monkeypatch.setattr(ctx.state, 'update_task', save_then_worker_fails)
    with pytest.raises(StatePersistenceError, match='background import'):
        backend.download(target(), result())
    assert transfers.calls == []
    assert ctx.state.get_tasks_for_orchestrator()[0]['extra']['enqueue']['phase'] == 'prepared'


def test_fatal_import_worker_stops_next_target_without_losing_owner(env):
    ctx, backend, transfers, downloads = env
    task = backend.download(target(), result(count=1))
    write_completed(downloads, task)
    transfers.rows[0].update(state='Completed, Succeeded', bytesTransferred=10)
    orchestrator = DownloadOrchestrator([backend], ctx)
    def callback(completed):
        # A background import has accepted the task, then failed to checkpoint.
        ctx.fatal_state_error = StatePersistenceError('import worker disk full')
    with pytest.raises(StatePersistenceError, match='worker disk full'):
        orchestrator.start_targets([target(8)], callback, [task], max_active=1)
    assert orchestrator.attempted_book_ids == set()
    assert len(transfers.calls) == 1
    assert ctx.state.get_tasks_for_orchestrator()[0]['task_id'] == task.task_id


def test_fatal_worker_latch_prevents_destructive_cleanup(env):
    ctx, backend, transfers, _ = env
    task = backend.download(target(), result(count=1))
    ctx.fatal_state_error = StatePersistenceError('worker checkpoint lost')
    with pytest.raises(StatePersistenceError):
        backend.cleanup(task)
    assert len(transfers.rows) == 1


@pytest.mark.parametrize('after_new_intent', [False, True])
def test_retry_crash_leaves_old_attempt_retired_before_replacement_can_own_names(env, monkeypatch, after_new_intent):
    ctx, backend, transfers, downloads = env
    backend._alternates[7] = iter([result('other-peer', count=1, directory='Other\\A Book')])
    task = backend.download(target(), result(count=1))
    old_id = task.task_id
    transfers.rows[0]['state'] = 'Completed, Rejected'
    backend.get_status(task)
    real_download = backend._download_one
    replacements = []
    def crash(book, candidate):
        assert saved(ctx, task)['extra']['retry_retired']
        if after_new_intent:
            replacements.append(real_download(book, candidate))
        raise RuntimeError('simulated process stop before old journal removal')
    monkeypatch.setattr(backend, '_download_one', crash)
    with pytest.raises(RuntimeError, match='simulated process stop'):
        backend.retry_after_failure(task)
    rows = StateManager(ctx.config_dir).get_tasks_for_orchestrator()
    old = next(row for row in rows if row['task_id'] == old_id)
    assert old['extra']['retry_retired']
    assert len(rows) == (2 if after_new_intent else 1)
    with pytest.raises(BackendUnavailable, match='retired'):
        SlskdBackend(ctx).reconcile_task(old)
    if replacements:
        replacement = replacements[0]
        write_completed(downloads, replacement)
        assert backend.discard(task) is True
        assert (downloads / replacement.local_dir / '01.mp3').read_bytes() == b'x' * 10
        assert len(transfers.rows) == 1


@pytest.mark.parametrize('broken_response', ['outage', 'malformed'])
def test_catalog_failure_is_incomplete_and_retried_before_any_search(env, monkeypatch, broken_response):
    ctx, backend, _, _ = env
    catalog_calls, searches = [], []
    def catalog(*args, **kwargs):
        catalog_calls.append(kwargs)
        if len(catalog_calls) == 1:
            if broken_response == 'outage':
                raise Timeout('catalog offline')
            return {'error': 'not a book list'}
        return []
    ctx.readarr._get = catalog
    monkeypatch.setattr(slskd_backend, '_execute_search', lambda ctx, query, label: (searches.append(query) or [], 'search-id'))
    orchestrator = DownloadOrchestrator([backend], ctx)
    assert orchestrator.start_download(target()) is None
    assert orchestrator.last_start_failure == 'not_searched'
    assert orchestrator.last_start_kind == 'service_unavailable'
    assert searches == []
    assert not ctx.history.history
    assert backend.search(target()) == []
    assert len(catalog_calls) == 2
    assert searches
