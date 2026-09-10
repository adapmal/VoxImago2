"""Testes dos sinais e da apresentacao do progresso de sincronizacao."""

from __future__ import annotations

import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.drive.incremental_sync import IncrementalSyncWorker
from src.drive.drive_sync import DriveSync
from src.drive.match import MatchResult
from src.services.local_scan import LocalScan
from src.ui.ui import DriveFileGalleryApp


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeFilesResource:
    def __init__(self, items):
        self.items = items

    def list(self, **_kwargs):
        return FakeRequest({'files': self.items})


class FakeService:
    def __init__(self, items):
        self.resource = FakeFilesResource(items)

    def files(self):
        return self.resource


class FakeConfig:
    def __init__(self):
        self.values = {'last_sync_timestamp': 1_700_000_000}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def get_current_drive_id(self):
        return 'drive-test'


class FakeCursor:
    def __init__(self):
        self.rowcount = 1
        self.calls = []

    def execute(self, query, params=()):
        self.calls.append((query, params))
        self.rowcount = 1
        return self


class FakeIndexer:
    def __init__(self):
        self.cursor = FakeCursor()
        self.conn = self
        self.exported = False
        self.closed = False

    def ensure_conn(self):
        return None

    def commit(self):
        return None

    def save_files_in_batch(self, *_args, **_kwargs):
        return None

    def export_to_shared_cache(self):
        self.exported = True
        return True

    def close(self):
        self.closed = True


class FakeProgressBar:
    def __init__(self):
        self.visible = False
        self.range = None
        self.value = None
        self.format = None

    def setVisible(self, value):
        self.visible = value

    def setRange(self, minimum, maximum):
        self.range = (minimum, maximum)

    def setValue(self, value):
        self.value = value

    def setFormat(self, value):
        self.format = value


class FakeStatusBar:
    def __init__(self):
        self.message = None

    def showMessage(self, message, timeout=0):
        self.message = (message, timeout)


class SyncProgressTests(unittest.TestCase):
    def test_local_scan_always_finishes_after_unexpected_error(self):
        worker = LocalScan('unused.db', [])
        worker._run_impl = Mock(side_effect=RuntimeError('disk failure'))
        finished = []
        worker.finished.connect(lambda: finished.append(True))

        worker.run()

        self.assertEqual([True], finished)
        self.assertEqual('disk failure', worker.failure_message)

    def test_local_scan_cancelled_during_count_releases_worker(self):
        worker = LocalScan('unused.db', [])

        def cancel_during_count():
            worker.terminate()
            return 0

        worker._count_total_files = cancel_during_count
        finished = []
        worker.finished.connect(lambda: finished.append(True))

        worker.run()

        self.assertEqual([True], finished)
        self.assertTrue(worker.cancelled)

    def test_full_sync_reports_failure_after_repeated_api_errors(self):
        with patch(
            'src.drive.drive_sync.configured_operation_roots', return_value=[]
        ):
            worker = DriveSync(
                service=object(),
                db_name='unused.db',
                selected_folders=['0AOB-ISqqs76_Uk9PVA'],
            )
        worker._count_total_files = Mock(return_value=1)
        worker.drive_service = SimpleNamespace(
            test_api_connection=Mock(return_value=True),
            list_files_paginated=Mock(side_effect=RuntimeError('API offline'))
        )
        failed = []
        finished = []
        worker.sync_failed.connect(failed.append)
        worker.sync_finished.connect(lambda: finished.append(True))
        indexer = FakeIndexer()

        with (
            patch('src.drive.drive_sync.FileIndexer', return_value=indexer),
            patch('src.drive.drive_sync.build_local_link_index', return_value={}),
            patch('src.drive.drive_sync.time.sleep'),
        ):
            worker.run()

        self.assertEqual([], finished)
        self.assertEqual(1, len(failed))
        self.assertIn('erros repetidos', failed[0])
        self.assertTrue(indexer.closed)

    def test_incremental_worker_reports_indeterminate_counts_and_completion(self):
        items = [
            {
                'id': f'drive-{index}',
                'name': f'foto-{index}.jpg',
                'mimeType': 'image/jpeg',
                'description': '',
                'size': '100',
                'modifiedTime': '2026-09-09T12:00:00.000Z',
                'createdTime': '2026-09-09T11:00:00.000Z',
                'parents': ['folder'],
                'webViewLink': f'https://drive.google.com/file/d/drive-{index}/view',
            }
            for index in range(3)
        ]
        indexer = FakeIndexer()
        worker = IncrementalSyncWorker(FakeService(items), FakeConfig())
        progress = []
        finished = []
        failed = []
        worker.progress_update.connect(lambda value, text: progress.append((value, text)))
        worker.sync_finished.connect(finished.append)
        worker.sync_failed.connect(failed.append)

        with (
            patch('src.database.database.FileIndexer', return_value=indexer),
            patch('src.drive.incremental_sync.build_local_link_index', return_value={}),
            patch('src.drive.incremental_sync.configured_operation_roots', return_value=[]),
            patch(
                'src.drive.incremental_sync.match_drive_to_local',
                return_value=MatchResult('no_match', 'test'),
            ),
        ):
            worker.run()

        self.assertEqual([], failed)
        self.assertEqual([3], finished)
        self.assertEqual(-1, progress[0][0])
        self.assertTrue(any(value == 0 for value, _text in progress))
        self.assertTrue(any('3/3' in text for _value, text in progress))
        self.assertEqual(100, progress[-1][0])
        self.assertTrue(indexer.exported)

    def test_status_bar_switches_between_busy_and_percentage_modes(self):
        target = SimpleNamespace(
            progress_bar=FakeProgressBar(),
            status_bar=FakeStatusBar(),
        )

        DriveFileGalleryApp.update_drive_sync_progress(
            target, -1, 'Consultando o Drive...'
        )
        self.assertTrue(target.progress_bar.visible)
        self.assertEqual((0, 0), target.progress_bar.range)
        self.assertEqual('Consultando...', target.progress_bar.format)

        DriveFileGalleryApp.update_drive_sync_progress(
            target, 37, 'Sincronizando: 37/100 arquivos (37%)...'
        )
        self.assertEqual((0, 100), target.progress_bar.range)
        self.assertEqual(37, target.progress_bar.value)
        self.assertEqual('37%', target.progress_bar.format)
        self.assertEqual(
            ('Sincronizando: 37/100 arquivos (37%)...', 0),
            target.status_bar.message,
        )

    def test_drive_metadata_does_not_replace_local_file_modified_time(self):
        item = {
            'id': 'drive-id',
            'name': 'foto.jpg',
            'mimeType': 'image/jpeg',
            'description': 'Brasil, Pará',
            'size': '100',
            'modifiedTime': '2026-09-09T12:00:00.000Z',
            'createdTime': '2026-09-09T11:00:00.000Z',
            'parents': ['folder'],
            'webViewLink': 'https://drive.google.com/file/d/drive-id/view',
        }
        indexer = FakeIndexer()
        worker = IncrementalSyncWorker(FakeService([item]), FakeConfig())

        with (
            patch('src.database.database.FileIndexer', return_value=indexer),
            patch('src.drive.incremental_sync.build_local_link_index', return_value={}),
            patch('src.drive.incremental_sync.configured_operation_roots', return_value=[]),
            patch(
                'src.drive.incremental_sync.match_drive_to_local',
                return_value=MatchResult('matched', 'test', local_id='local-id'),
            ),
        ):
            worker.run()

        local_updates = [
            (query, params) for query, params in indexer.cursor.calls
            if query.lstrip().startswith('UPDATE files')
            and params and params[-1] == 'local-id'
        ]
        self.assertEqual(1, len(local_updates))
        self.assertNotIn('modifiedTime', local_updates[0][0])

    def test_incremental_sync_is_deferred_while_queue_is_running(self):
        target = SimpleNamespace(
            is_authenticated=True,
            service=object(),
            queue_execution_running=True,
            maintenance_running=False,
            incremental_sync_deferred=False,
            incremental_sync_deferred_manual=False,
            status_bar=FakeStatusBar(),
        )

        DriveFileGalleryApp._run_incremental_sync(target, force_manual=True)

        self.assertTrue(target.incremental_sync_deferred)
        self.assertTrue(target.incremental_sync_deferred_manual)
        self.assertIn('aguardará', target.status_bar.message[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
