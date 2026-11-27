"""Testes offline do reparador de sufixos '(n)' deixados pelo InSync."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.insync_suffix_repair import (
    InsyncSuffixRepairWorker,
    PendingTempJournal,
    _resolve_active_roots_and_drive,
    execute_insync_suffix_repair,
    restore_pending_temp_names,
    scan_insync_suffix_candidates,
    strip_single_trailing_copy_suffix,
)
from src.utils.config_manager import ConfigManager


class InsyncSuffixRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'Banco de Imagens'
        self.root.mkdir(parents=True)
        self.db_path = str(Path(self.tmp.name) / 'file_index.db')
        self.drive_id = 'drv_root'

        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE files (
                file_id TEXT PRIMARY KEY, name TEXT, path TEXT, size INTEGER,
                modified_time INTEGER, created_time INTEGER, mime_type TEXT,
                parent_id TEXT, source TEXT, webContentLink TEXT,
                description TEXT, exif_date_taken TEXT
            );
            CREATE TABLE search_index (file_id TEXT, name TEXT, path TEXT, source TEXT);
            CREATE TABLE drive_link_validation (
                local_file_id TEXT PRIMARY KEY, drive_id TEXT, status TEXT,
                remote_name TEXT, remote_size INTEGER, remote_md5 TEXT,
                local_md5 TEXT, hierarchy_score INTEGER, details TEXT, validated_at INTEGER
            );
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _folder(fid, name, parent, drive_id='drv_root'):
        return {
            'id': fid, 'name': name, 'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent], 'driveId': drive_id, 'trashed': False,
        }

    @staticmethod
    def _file(fid, name, parent, content: bytes, drive_id='drv_root', can_edit=True):
        return {
            'id': fid, 'name': name, 'mimeType': 'image/jpeg',
            'parents': [parent], 'driveId': drive_id, 'trashed': False,
            'size': str(len(content)),
            'md5Checksum': hashlib.md5(content).hexdigest(),
            'webViewLink': f'https://drive.google.com/file/d/{fid}/view',
            'capabilities': {'canEdit': can_edit},
        }

    def test_strip_single_trailing_copy_suffix_rules(self):
        self.assertEqual(('foto.jpg', 2), strip_single_trailing_copy_suffix('foto (2).jpg'))
        self.assertEqual(('fernando nunes (2).jpg', 2), strip_single_trailing_copy_suffix('fernando nunes (2) (2).jpg'))
        self.assertEqual(('photo (3).JPG', 2), strip_single_trailing_copy_suffix('photo (3) (2).JPG'))
        self.assertIsNone(strip_single_trailing_copy_suffix('evento (2021).jpg'))
        self.assertIsNone(strip_single_trailing_copy_suffix('foto.jpg'))

    def test_scan_classifies_eligible_double_paren_homonym_local_conflict_and_md5_mismatch(self):
        b_ok = b'conteudo-igual-unico'
        b_dbl = b'conteudo-duplo-paren'
        b_hom = b'conteudo-homonimo'
        b_loc = b'conteudo-ja-existe-local'
        b_diff = b'conteudo-diferente-local'

        f_2020 = self.root / '2020'
        f_2020.mkdir(parents=True)
        (f_2020 / 'unica (2).jpg').write_bytes(b_ok)
        (f_2020 / 'fernando (2) (2).jpg').write_bytes(b_dbl)
        (f_2020 / 'homonima (2).jpg').write_bytes(b_hom)
        (f_2020 / 'conflito.jpg').write_bytes(b_loc)
        (f_2020 / 'conflito (2).jpg').write_bytes(b_loc)
        (f_2020 / 'md5diff (2).jpg').write_bytes(b_diff)

        drive_items = [
            self._folder('p2020', '2020', self.drive_id),
            self._file('d_ok', 'unica.jpg', 'p2020', b_ok),
            self._file('d_dbl', 'fernando (2).jpg', 'p2020', b_dbl),
            self._file('d_h1', 'homonima.jpg', 'p2020', b_hom),
            self._file('d_h2', 'homonima.jpg', 'p2020', b_hom),
            self._file('d_loc', 'conflito.jpg', 'p2020', b_loc),
            self._file('d_diff', 'md5diff.jpg', 'p2020', b'conteudo-diferente-drive'),
        ]

        results = scan_insync_suffix_candidates(
            roots=[str(self.root)],
            drive_id=self.drive_id,
            drive_items=drive_items,
        )
        by_name = {r.local_name: r for r in results}
        self.assertEqual('eligible', by_name['unica (2).jpg'].status)
        self.assertEqual('d_ok', by_name['unica (2).jpg'].drive_id)
        self.assertEqual('unica.jpg', by_name['unica (2).jpg'].drive_name)

        # Remove apenas UM (2) no caso '(2) (2)'
        self.assertEqual('eligible', by_name['fernando (2) (2).jpg'].status)
        self.assertEqual('fernando (2).jpg', by_name['fernando (2) (2).jpg'].drive_name)

        # 2 arquivos no Drive -> direciona para Desambiguar nomes
        self.assertEqual('needs_disambiguation', by_name['homonima (2).jpg'].status)
        # Base ja existe localmente -> bloqueado
        self.assertEqual('blocked', by_name['conflito (2).jpg'].status)
        # MD5 diferente -> bloqueado
        self.assertEqual('blocked', by_name['md5diff (2).jpg'].status)

    def test_execute_two_step_rename_syncs_and_updates_db(self):
        content = b'foto-teste-insync-ok'
        folder = self.root / '2010'
        folder.mkdir(parents=True)
        old_path = folder / 'img_101 (2).JPG'
        old_path.write_bytes(content)

        # Registrar no banco local com caminho antigo (2)
        conn = sqlite3.connect(self.db_path)
        old_cid = os.path.normcase(os.path.normpath(str(old_path)))
        conn.execute(
            "INSERT INTO files VALUES (?, ?, ?, ?, 100, 100, 'image/jpeg', '', 'local', '', '', '')",
            (old_cid, old_path.name, str(old_path), len(content)),
        )
        conn.execute(
            "INSERT INTO search_index VALUES (?, ?, ?, 'local')",
            (old_cid, old_path.name, str(old_path)),
        )
        conn.commit()
        conn.close()

        drive_state = {
            'p2010': self._folder('p2010', '2010', self.drive_id),
            'd1': self._file('d1', 'img_101.JPG', 'p2010', content),
        }
        results = scan_insync_suffix_candidates(
            roots=[str(self.root)],
            drive_id=self.drive_id,
            drive_items=list(drive_state.values()),
        )
        self.assertEqual(1, len(results))
        self.assertEqual('eligible', results[0].status)

        rename_log = []

        def fake_rename(fid, new_name):
            rename_log.append((fid, new_name))
            drive_state[fid]['name'] = new_name
            # Simula o InSync renomeando o arquivo local para refletir o Drive
            current_files = list(folder.iterdir())
            self.assertEqual(1, len(current_files))
            current_files[0].rename(folder / new_name)
            return drive_state[fid]

        stats = execute_insync_suffix_repair(
            results,
            db_name=self.db_path,
            drive_id=self.drive_id,
            get_item_fn=lambda fid: dict(drive_state[fid]),
            list_children_fn=lambda pid: [v for v in drive_state.values() if pid in (v.get('parents') or [])],
            rename_item_fn=fake_rename,
            wait_timeout_sec=5.0,
            poll_interval_sec=0.02,
            create_backup=False,
        )
        self.assertEqual(1, stats['completed'])
        self.assertEqual(0, stats['failed'])
        self.assertEqual(2, len(rename_log))
        self.assertIn('.__insync_fix__', rename_log[0][1])
        self.assertEqual('img_101.JPG', rename_log[1][1])
        self.assertTrue((folder / 'img_101.JPG').exists())
        self.assertFalse(old_path.exists())
        self.assertEqual({}, PendingTempJournal(self.db_path).load())

        # Confere atualizacao do SQLite local
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT name, path, webContentLink FROM files WHERE source='local'").fetchall()
        conn.close()
        self.assertEqual(1, len(rows))
        self.assertEqual('img_101.JPG', rows[0][0])
        self.assertIn('d1', rows[0][2])

    def test_timeout_in_step1_automatically_rolls_back_drive_name(self):
        content = b'foto-timeout-insync'
        folder = self.root / '2011'
        folder.mkdir(parents=True)
        old_path = folder / 'travada (2).jpg'
        old_path.write_bytes(content)

        drive_state = {
            'p2011': self._folder('p2011', '2011', self.drive_id),
            'd2': self._file('d2', 'travada.jpg', 'p2011', content),
        }
        results = scan_insync_suffix_candidates(
            roots=[str(self.root)],
            drive_id=self.drive_id,
            drive_items=list(drive_state.values()),
        )

        # InSync pausado: rename no Drive funciona, mas disco local NAO muda!
        def silent_drive_rename(fid, new_name):
            drive_state[fid]['name'] = new_name
            return drive_state[fid]

        stats = execute_insync_suffix_repair(
            results,
            db_name=self.db_path,
            drive_id=self.drive_id,
            get_item_fn=lambda fid: dict(drive_state[fid]),
            list_children_fn=lambda pid: [v for v in drive_state.values() if pid in (v.get('parents') or [])],
            rename_item_fn=silent_drive_rename,
            wait_timeout_sec=0.1,
            poll_interval_sec=0.02,
            create_backup=False,
        )
        self.assertEqual(0, stats['completed'])
        self.assertEqual(1, stats['rolled_back'])
        self.assertEqual('rolled_back', results[0].status)
        # Nome original DEVE ter sido restaurado no Drive e diario limpo
        self.assertEqual('travada.jpg', drive_state['d2']['name'])
        self.assertEqual({}, PendingTempJournal(self.db_path).load())

    def test_restore_pending_temp_names_recovers_interrupted_run(self):
        journal = PendingTempJournal(self.db_path)
        journal.add('d9', 'original.jpg', 'original.__insync_fix__.jpg', str(self.root))
        drive_state = {'d9': {'id': 'd9', 'name': 'original.__insync_fix__.jpg', 'trashed': False}}

        def fake_rename(fid, new_name):
            drive_state[fid]['name'] = new_name
            return drive_state[fid]

        res = restore_pending_temp_names(
            self.db_path,
            get_item_fn=lambda fid: dict(drive_state[fid]),
            rename_item_fn=fake_rename,
        )
        self.assertEqual(1, res['restored'])
        self.assertEqual(0, res['remaining'])
        self.assertEqual('original.jpg', drive_state['d9']['name'])

    def test_resolve_active_roots_and_drive_uses_real_config_manager(self):
        old_inst = ConfigManager._instance
        try:
            ConfigManager._instance = None
            cfg = ConfigManager()
            cfg.settings['sandbox_mode'] = True
            cfg.settings['sandbox_path'] = str(self.root)
            cfg.settings['sandbox_drive_id'] = 'sbx_123'
            roots, did = _resolve_active_roots_and_drive(cfg)
            self.assertEqual('sbx_123', did)
            self.assertEqual([os.path.normpath(str(self.root))], roots)
        finally:
            ConfigManager._instance = old_inst

    def test_worker_scan_and_execute_end_to_end_with_mock_service(self):
        from unittest.mock import patch

        content = b'worker-e2e-content'
        folder = self.root / '2012'
        folder.mkdir(parents=True)
        old_path = folder / 'e2e (2).jpg'
        old_path.write_bytes(content)

        drive_state = {
            'p2012': self._folder('p2012', '2012', 'sbx_e2e', drive_id='sbx_e2e'),
            'd_e2e': self._file('d_e2e', 'e2e.jpg', 'p2012', content, drive_id='sbx_e2e'),
        }

        class _Req:
            def __init__(self, fn):
                self._fn = fn

            def execute(self):
                return self._fn()

        class _FilesApi:
            def list(self, **kwargs):
                q = kwargs.get('q', '')
                def _do():
                    if 'in parents' in q:
                        pid = q.split("'")[1]
                        return {'files': [dict(v) for v in drive_state.values() if pid in (v.get('parents') or [])]}
                    return {'files': [dict(v) for v in drive_state.values()]}
                return _Req(_do)

            def get(self, **kwargs):
                fid = kwargs['fileId']
                return _Req(lambda: dict(drive_state[fid]))

            def update(self, **kwargs):
                fid = kwargs['fileId']
                new_name = kwargs['body']['name']
                def _do():
                    drive_state[fid]['name'] = new_name
                    cur = list(folder.iterdir())
                    cur[0].rename(folder / new_name)
                    return dict(drive_state[fid])
                return _Req(_do)

        class _FakeService:
            def files(self):
                return _FilesApi()

        old_inst = ConfigManager._instance
        try:
            ConfigManager._instance = None
            cfg = ConfigManager()
            cfg.settings['sandbox_mode'] = True
            cfg.settings['sandbox_path'] = str(self.root)
            cfg.settings['sandbox_drive_id'] = 'sbx_e2e'
            cfg.settings['read_only_mode'] = False

            fake_svc = _FakeService()
            scan_out, scan_err = [], []
            with patch('src.services.insync_suffix_repair.private_service', return_value=fake_svc):
                w_scan = InsyncSuffixRepairWorker(self.db_path, fake_svc, mode='scan')
                w_scan.finished.connect(scan_out.append)
                w_scan.failed.connect(scan_err.append)
                w_scan.run()
            self.assertEqual([], scan_err)
            self.assertEqual(1, len(scan_out))
            items = scan_out[0]['items']
            self.assertEqual(1, len(items))
            self.assertEqual('eligible', items[0].status)

            exec_out, exec_err = [], []
            with (
                patch('src.services.insync_suffix_repair.private_service', return_value=fake_svc),
                patch('src.services.insync_suffix_repair.assert_queue_empty'),
                patch('src.services.insync_suffix_repair.local_backup'),
            ):
                w_exec = InsyncSuffixRepairWorker(
                    self.db_path, fake_svc, mode='execute', items=items, wait_timeout_sec=5.0,
                )
                w_exec.finished.connect(exec_out.append)
                w_exec.failed.connect(exec_err.append)
                w_exec.run()
            self.assertEqual([], exec_err)
            self.assertEqual(1, exec_out[0]['completed'])
            self.assertTrue((folder / 'e2e.jpg').exists())
            self.assertFalse(old_path.exists())
        finally:
            ConfigManager._instance = old_inst


if __name__ == '__main__':
    unittest.main(verbosity=2)
