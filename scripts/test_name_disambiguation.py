"""Somente Drive simulado, diretórios e bancos temporários."""
import copy
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from scripts.test_shortcut_migration import Drive, Config
from scripts.test_hash_dedup import Request
from src.services.name_disambiguation import NameDisambiguation, NameJournal
from src.services.name_history import NameHistory, historical_file_ids, history_revision
from src.services.name_safety import name_key, numbered_name, safe_windows_name
from src.services.shortcut_migration import SHORTCUT


class RenameDrive(Drive):
    fail_after = False
    fail_before = False
    fail_on_op = None  # optional filter: 'rename', 'tags', 'move'

    def photo(self, fid, name, parent, md5='a' * 32):
        super().photo(fid, name, parent, md5=md5)
        self.items[fid]['capabilities']['canMoveItemOutOfDrive'] = True

    def update(self, fileId, body=None, addParents=None, removeParents=None, **kwargs):
        body = dict(body or {})
        if addParents:
            op = 'move'
        elif 'description' in body and 'name' not in body:
            op = 'tags'
        else:
            op = 'rename'

        def perform(request):
            item = self.items[fileId]
            should_fail_before = self.fail_before and (self.fail_on_op is None or self.fail_on_op == op)
            should_fail_after = self.fail_after and (self.fail_on_op is None or self.fail_on_op == op)
            if should_fail_before:
                raise TimeoutError('Sem confirmação')
            if request.headers.get('If-Match') != '"' + item['version'] + '"':
                raise RuntimeError('412')
            if body:
                item.update(body)
            if addParents:
                item['parents'] = [addParents]
                item['driveId'] = self.items[addParents].get('driveId')
            item['version'] = str(int(item['version']) + 1)
            self.writes.append((op, fileId, copy.deepcopy(body), addParents))
            if should_fail_after:
                self.fail_after = False
                raise TimeoutError('Gravado, resposta perdida')
            if self.cancel_hook:
                self.cancel_hook()
            return copy.deepcopy(item)
        return Request(perform)


class NameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'file_index.db')
        self.drive, self.cfg = RenameDrive(), Config()
        self.drive.photo('first', 'Festa.jpg', 'destination', md5='c' * 32)
        self.drive.photo('second', 'Festa.jpg', 'destination', md5='d' * 32)
        self.cancelled = False
        self.engine = NameDisambiguation(self.drive, self.db, self.cfg, lambda: self.cancelled)

    def run_plan(self, plan, ids=None):
        return self.engine.execute(plan['id'], ids if ids is not None else [r['source']['id'] for r in plan['items'] if r['state'] != 'blocked'])

    def load(self, plan):
        journal = NameJournal(self.db)
        try:
            return journal.load(plan['id'])
        finally:
            journal.close()

    def test_scan_reads_only_and_plans_all_copies(self):
        plan = self.engine.scan()
        self.assertEqual([], self.drive.writes)
        self.assertEqual(['Festa_0.jpg', 'Festa_1.jpg'], [r['new_name'] for r in plan['items']])
        self.assertTrue(all(r['folder_path'] == 'Banco de Imagens/Recebidos' for r in plan['items']))

    def test_reserved_suffix_and_case_collision(self):
        self.drive.items['second']['name'] = 'FESTA.JPG'
        self.drive.photo('reserved', 'Festa_0.jpg', 'destination', md5='e' * 32)
        plan = self.engine.scan()
        self.assertEqual(['Festa_1.jpg', 'FESTA_2.JPG'], [r['new_name'] for r in plan['items']])

    def test_same_name_different_parent_is_not_conflict(self):
        self.drive.items['second']['parents'] = ['bank']
        self.assertEqual([], self.engine.scan()['items'])

    def test_same_hash_deduplicates_in_cloud_preserving_name_merging_tags_and_cleaning_local_by_id(self):
        from src.database.database import FileIndexer, to_canonical_id
        from src.services.hash_deduplicator import DedupJournal

        self.drive.folder('y2020', '2020', 'bank', 'bank')
        self.drive.folder('sub', 'Italia_1', 'y2020', 'bank')
        self.drive.photo('keep', 'Festa.jpg', 'sub', md5='e' * 32)
        self.drive.photo('dup', 'Festa.jpg', 'sub', md5='e' * 32)
        self.drive.items['keep'].update(
            description='2020, familia, igreja, missa',
            createdTime='2021-01-01T00:00:00Z',
            imageMediaMetadata={'time': '2020:05:10 12:00:00'},
        )
        self.drive.items['dup'].update(
            description='2019, roma, familia',
            createdTime='2020-01-01T00:00:00Z',
            imageMediaMetadata={'time': '2020:05:10 12:00:00'},
        )

        # Popula índice local para confirmar atualização/limpeza estritamente por drive_id, nunca por nome.
        indexer = FileIndexer(self.db)
        try:
            keep_local = r'L:\Drives Compartilhados\Banco de Imagens\2020\Italia_1\Festa.jpg'
            dup_local = r'L:\Drives Compartilhados\Banco de Imagens\2020\Italia_1\Festa (2).jpg'
            other_local = r'L:\Drives Compartilhados\Banco de Imagens\Outro\Festa.jpg'
            indexer.save_files_in_batch([
                dict(id='k', name='Festa.jpg', path=keep_local, source='local', size=100,
                     description='2020, familia, igreja, missa',
                     webContentLink='https://drive.google.com/uc?id=keep&export=download'),
                dict(id='d', name='Festa (2).jpg', path=dup_local, source='local', size=100,
                     description='2019, roma, familia',
                     webContentLink='https://drive.google.com/uc?id=dup&export=download'),
                dict(id='o', name='Festa.jpg', path=other_local, source='local', size=100,
                     description='intacto',
                     webContentLink='https://drive.google.com/uc?id=other_drive_id&export=download'),
            ], source='local')
        finally:
            indexer.close()

        # Remove o par padrão diferente de self.drive em 'destination' para isolar este grupo.
        self.drive.items.pop('first')
        self.drive.items.pop('second')

        plan = self.engine.scan()
        self.assertEqual([], self.drive.writes)
        actions = {r['source']['id']: r for r in plan['items']}
        self.assertEqual('dedup_keep', actions['keep']['action'])
        self.assertEqual('Festa.jpg', actions['keep']['new_name'])
        self.assertEqual('dedup_move', actions['dup']['action'])
        self.assertEqual('_TestesBanco/Banco_Apagado/2020/Italia_1/Festa.jpg', actions['dup']['archive_path'])
        # Ano divergente '2019' da cópia duplicada é descartado em pasta 2020; tag 'roma' é herdada.
        self.assertEqual('2020, familia, igreja, missa, roma', actions['keep']['merged_description'])

        res = self.run_plan(plan)
        self.assertEqual([], res['errors'])
        self.assertEqual(2, res['completed'])
        self.assertEqual(1, res['dedup_moved'])
        self.assertEqual(1, res['tags_updated'])
        self.assertTrue(res.get('dedup_log_path'))

        # Preservado manteve o nome original na mesma pasta e absorveu as tags no Drive.
        self.assertEqual('Festa.jpg', self.drive.items['keep']['name'])
        self.assertEqual(['sub'], self.drive.items['keep']['parents'])
        self.assertEqual('2020, familia, igreja, missa, roma', self.drive.items['keep']['description'])
        # Duplicata foi movida via API para _TestesBanco/Banco_Apagado/2020/Italia_1.
        self.assertEqual('archive', self.drive.items['dup']['driveId'])
        moved_parent = self.drive.items['dup']['parents'][0]
        self.assertEqual('Italia_1', self.drive.items[moved_parent]['name'])
        y_parent = self.drive.items[moved_parent]['parents'][0]
        self.assertEqual('2020', self.drive.items[y_parent]['name'])
        ba_parent = self.drive.items[y_parent]['parents'][0]
        self.assertEqual('Banco_Apagado', self.drive.items[ba_parent]['name'])

        # DedupJournal registra 'cleaned' para a cópia movida.
        dj = DedupJournal(self.db)
        try:
            self.assertEqual(('cleaned',), dj.conn.execute('SELECT state FROM moves WHERE file_id=?', ('dup',)).fetchone())
        finally:
            dj.close()

        # Índice local: atualizado em 'keep', removido em 'dup' e intacto em 'other' (mesmo nome!).
        check_idx = FileIndexer(self.db)
        try:
            k_row = check_idx.conn.execute('SELECT description FROM files WHERE file_id=?', (to_canonical_id(keep_local),)).fetchone()
            d_row = check_idx.conn.execute('SELECT 1 FROM files WHERE file_id=?', (to_canonical_id(dup_local),)).fetchone()
            o_row = check_idx.conn.execute('SELECT description FROM files WHERE file_id=?', (to_canonical_id(other_local),)).fetchone()
            self.assertEqual(('2020, familia, igreja, missa, roma',), k_row)
            self.assertIsNone(d_row)
            self.assertEqual(('intacto',), o_row)
        finally:
            check_idx.close()

        # Nova varredura não vê mais conflito.
        self.assertEqual([], self.engine.scan()['items'])

    def test_mixed_group_deduplicates_identical_first_then_renames_distinct(self):
        self.drive.items['first'].update(description='vip', createdTime='2020-01-01T00:00:00Z')
        self.drive.photo('first_dup', 'Festa.jpg', 'destination', md5='c' * 32)
        self.drive.items['first_dup'].update(description='extra', createdTime='2019-01-01T00:00:00Z')
        self.drive.items['second'].update(description='outra', createdTime='2021-01-01T00:00:00Z')

        plan = self.engine.scan()
        self.assertEqual([], self.drive.writes)
        rows = {r['source']['id']: r for r in plan['items']}
        # Embora 'first_dup' seja mais antigo, 'first' tem mais tags (desempate por tags -> createdTime -> id)?
        # Ambos têm 1 tag -> createdTime mais antigo vence: 'first_dup' (2019) é o preservado!
        self.assertEqual('dedup_keep_and_rename', rows['first_dup']['action'])
        self.assertEqual('dedup_move', rows['first']['action'])
        self.assertEqual('rename', rows['second']['action'])
        self.assertEqual('Festa_0.jpg', rows['first_dup']['new_name'])
        self.assertEqual('Festa_1.jpg', rows['second']['new_name'])

        res = self.run_plan(plan)
        self.assertEqual([], res['errors'])
        self.assertEqual(3, res['completed'])
        self.assertEqual(1, res['dedup_moved'])
        self.assertEqual(1, res['tags_updated'])
        # O preservado fundiu 'vip' + 'extra' e foi renomeado sem colisão depois da remoção da cópia idêntica.
        self.assertEqual('Festa_0.jpg', self.drive.items['first_dup']['name'])
        self.assertEqual('extra, vip', self.drive.items['first_dup']['description'])
        self.assertEqual('archive', self.drive.items['first']['driveId'])
        self.assertEqual('Festa_1.jpg', self.drive.items['second']['name'])
        self.assertEqual('outra', self.drive.items['second']['description'])
        self.assertEqual([], self.engine.scan()['items'])

        hist = NameHistory(self.db)
        try:
            records = {json.loads(p)['file_id']: json.loads(p) for p, in hist.conn.execute('SELECT payload FROM records').fetchall()}
            self.assertEqual('Festa_0.jpg', records['first_dup']['new_name'])
            self.assertEqual('Festa_1.jpg', records['second']['new_name'])
        finally:
            hist.close()

    def test_mixed_group_rename_timeout_reconciles_tags_done_and_smallest_id_tiebreak(self):
        # Mesmo número de tags e mesmo createdTime -> menor ID ('a1' < 'a2') vence como preservado.
        self.drive.items.pop('first')
        self.drive.items.pop('second')
        self.drive.photo('a2', 'Festa.jpg', 'destination', md5='c' * 32)
        self.drive.photo('a1', 'Festa.jpg', 'destination', md5='c' * 32)
        self.drive.photo('b1', 'Festa.jpg', 'destination', md5='d' * 32)
        self.drive.items['a1'].update(description='t1', createdTime='2020-01-01T00:00:00Z')
        self.drive.items['a2'].update(description='t2', createdTime='2020-01-01T00:00:00Z')
        self.drive.items['b1'].update(description='tb', createdTime='2021-01-01T00:00:00Z')

        plan = self.engine.scan()
        rows = {r['source']['id']: r for r in plan['items']}
        self.assertEqual('dedup_keep_and_rename', rows['a1']['action'])
        self.assertEqual('dedup_move', rows['a2']['action'])
        self.assertEqual('rename', rows['b1']['action'])

        # Fase 1 conclui (tags de a1 gravadas e a2 movido), mas Fase 2 sofre timeout ANTES de renomear a1.
        self.drive.fail_before = True
        self.drive.fail_on_op = 'rename'
        res_fail = self.run_plan(plan)
        self.assertTrue(res_fail['errors'])
        self.drive.fail_before = False
        self.drive.fail_on_op = None

        states = {r['source']['id']: r['state'] for r in self.load(plan)['items']}
        self.assertEqual('completed', states['a2'])
        self.assertEqual('uncertain', states['a1'])
        self.assertEqual('t1, t2', self.drive.items['a1']['description'])
        self.assertEqual('Festa.jpg', self.drive.items['a1']['name'])

        # Reconciliação libera a1 para 'tags_done' (sem regravar tags nem renomear).
        rec = self.engine.reconcile_uncertain(plan['id'], ['a1'])
        self.assertEqual(1, rec['released'])
        self.assertEqual('tags_done', {r['source']['id']: r['state'] for r in self.load(plan)['items']}['a1'])

        # Reexecução aproveita 'tags_done' e conclui apenas os renomeios restantes.
        writes_before = len(self.drive.writes)
        res_ok = self.run_plan(plan)
        self.assertEqual([], res_ok['errors'])
        self.assertEqual(2, res_ok['completed'])
        self.assertEqual(0, res_ok['dedup_moved'])
        self.assertEqual(0, res_ok['tags_updated'])
        self.assertEqual(writes_before + 2, len(self.drive.writes))
        self.assertEqual('Festa_0.jpg', self.drive.items['a1']['name'])
        self.assertEqual('Festa_1.jpg', self.drive.items['b1']['name'])
        self.assertEqual([], self.engine.scan()['items'])

    def test_same_md5_with_divergent_sha256_remains_blocked(self):
        self.drive.items['second'].update(md5Checksum='c' * 32, sha256Checksum='f' * 64)
        plan = self.engine.scan()
        self.assertTrue(all(r['state'] == 'blocked' and 'SHA-256' in r['error'] for r in plan['items']))
        res = self.run_plan(plan, ['first', 'second'])
        self.assertTrue(res['errors'])
        self.assertEqual([], self.drive.writes)

    def test_same_hash_missing_move_permission_or_archive_drive_blocks_group(self):
        self.drive.items['second'].update(md5Checksum='c' * 32, sha256Checksum='c' * 64)
        self.drive.items['second']['capabilities']['canMoveItemOutOfDrive'] = False
        plan = self.engine.scan()
        self.assertTrue(all(r['state'] == 'blocked' and 'mover' in r['error'] for r in plan['items']))

        self.drive.items['second']['capabilities']['canMoveItemOutOfDrive'] = True
        no_sandbox_cfg = SimpleNamespace(
            get=lambda k, d=None: {'production_drive_id': 'bank'}.get(k, d),
            is_sandbox=lambda: False,
            is_read_only=lambda: False,
        )
        engine2 = NameDisambiguation(self.drive, self.db, no_sandbox_cfg, lambda: False)
        plan2 = engine2.scan()
        self.assertTrue(all(r['state'] == 'blocked' and '_TestesBanco' in r['error'] for r in plan2['items']))

    def test_dedup_move_timeout_before_and_after_reconcile_safely(self):
        self.drive.items['first'].update(description='tag1', createdTime='2019-01-01T00:00:00Z')
        self.drive.items['second'].update(md5Checksum='c' * 32, sha256Checksum='c' * 64, description='tag2', createdTime='2020-01-01T00:00:00Z')
        plan = self.engine.scan()

        # 1) Timeout ANTES de mover no Drive -> bloqueia em 'uncertain'; reconciliação libera sem mover.
        self.drive.fail_before = True
        self.drive.fail_on_op = 'move'
        res_fail = self.run_plan(plan)
        self.assertTrue(res_fail['errors'])
        self.drive.fail_before = False
        self.drive.fail_on_op = None
        loaded = self.load(plan)
        states = {r['source']['id']: r['state'] for r in loaded['items']}
        self.assertEqual('completed', states['first'])
        self.assertEqual('uncertain', states['second'])
        self.assertEqual(['destination'], self.drive.items['second']['parents'])

        # Execução cega mantém o bloqueio sem repetir movimentação.
        self.assertTrue(self.run_plan(plan, ['second'])['errors'])
        rec = self.engine.reconcile_uncertain(plan['id'], ['second'])
        self.assertEqual(1, rec['released'])
        self.assertEqual('pending', {r['source']['id']: r['state'] for r in self.load(plan)['items']}['second'])
        self.assertEqual(['destination'], self.drive.items['second']['parents'])

        # 2) Agora simula timeout APÓS mover no Drive -> reconciliação confirma sem segunda movimentação.
        self.drive.fail_after = True
        self.drive.fail_on_op = 'move'
        res_after = self.run_plan(plan, ['second'])
        self.assertTrue(res_after['errors'])
        self.drive.fail_on_op = None
        self.assertEqual('archive', self.drive.items['second']['driveId'])
        self.assertEqual('uncertain', {r['source']['id']: r['state'] for r in self.load(plan)['items']}['second'])

        writes_before = len(self.drive.writes)
        rec2 = self.engine.reconcile_uncertain(plan['id'], ['second'])
        self.assertEqual(1, rec2['confirmed'])
        self.assertEqual(writes_before, len(self.drive.writes))
        self.assertEqual('completed', {r['source']['id']: r['state'] for r in self.load(plan)['items']}['second'])
        self.assertEqual([], self.engine.scan()['items'])

    def test_audit1_keeper_changed_md5_or_size_without_sha256_between_validate_and_dedup_aborts(self):
        self.drive.items['first'].pop('sha256Checksum', None)
        self.drive.items['second'].update(md5Checksum='c' * 32)
        self.drive.items['second'].pop('sha256Checksum', None)
        self.drive.items['first'].update(description='t1', createdTime='2019-01-01T00:00:00Z')
        self.drive.items['second'].update(description='t2', createdTime='2020-01-01T00:00:00Z')
        plan = self.engine.scan()

        # Simula alteração concorrente do MD5 e tamanho do preservado logo após _validate_group.
        orig_get = self.engine.api.get
        get_calls = {'first': 0}

        def mutating_get(fid):
            if fid == 'first':
                get_calls['first'] += 1
                if get_calls['first'] == 2:
                    self.drive.items['first']['md5Checksum'] = '9' * 32
                    self.drive.items['first']['size'] = '999'
            return orig_get(fid)

        with patch.object(self.engine.api, 'get', side_effect=mutating_get):
            res = self.run_plan(plan)

        self.assertTrue(res['errors'])
        self.assertEqual([], self.drive.writes)
        self.assertEqual(['destination'], self.drive.items['second']['parents'])

    def test_audit2_external_description_change_on_completed_keeper_before_resume_blocks(self):
        self.drive.items['first'].update(description='t1', createdTime='2019-01-01T00:00:00Z')
        self.drive.items['second'].update(md5Checksum='c' * 32, sha256Checksum='c' * 64, description='t2', createdTime='2020-01-01T00:00:00Z')
        plan = self.engine.scan()

        # Fase 1 grava tags no preservado e marca 'completed', então falha antes de mover a cópia.
        self.drive.fail_before = True
        self.drive.fail_on_op = 'move'
        res = self.run_plan(plan)
        self.assertTrue(res['errors'])
        self.drive.fail_before = False
        self.drive.fail_on_op = None

        # Libera a cópia incerta; nesse meio tempo, descrição do preservado muda externamente na nuvem.
        self.engine.reconcile_uncertain(plan['id'], ['second'])
        self.drive.items['first']['description'] = 'editado externamente'

        writes_before = len(self.drive.writes)
        res_resume = self.run_plan(plan, ['second'])
        self.assertTrue(res_resume['errors'])
        self.assertEqual(writes_before, len(self.drive.writes))
        self.assertEqual(['destination'], self.drive.items['second']['parents'])

    def test_audit3_split_crash_between_dedup_journal_and_name_journal_recovers_in_ui_and_reconcile(self):
        from PyQt6.QtWidgets import QApplication
        from src.services.hash_deduplicator import DedupJournal
        from src.services.name_disambiguation import uncertain_operation_ids
        from src.ui.name_disambiguation_dialog import NameDisambiguationDialog

        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)

        self.drive.items['first'].update(description='t1', createdTime='2019-01-01T00:00:00Z')
        self.drive.items['second'].update(md5Checksum='c' * 32, sha256Checksum='c' * 64, description='t2', createdTime='2020-01-01T00:00:00Z')
        plan = self.engine.scan()

        # Simula queda após gravar DedupJournal.begin_move e antes da movimentação ou gravação em NameJournal.
        dj = DedupJournal(self.db)
        try:
            payload = dict(
                file_id='second', local_id='', origin_path='Banco de Imagens/Recebidos/Festa.jpg',
                original_parents=['destination'], original_name='Festa.jpg',
                destination_parent='dest_sub', destination_drive='archive', destination_name='Festa.jpg',
                relative_path=r'Recebidos\Festa.jpg', keeper_id='first',
                md5='c' * 32, size=100, source='name_disambiguation', job=plan['id'],
            )
            dj.begin_move('second', payload)
        finally:
            dj.close()

        # Mesmo com NameJournal em 'pending', o helper e a UI enxergam como conferência incerta.
        self.assertIn('second', uncertain_operation_ids(self.db, plan))
        dlg = NameDisambiguationDialog(indexer=SimpleNamespace(db_name=self.db))
        try:
            dlg._set_plan(plan)
            self.assertEqual('Conferir resposta anterior', dlg.table.item(1, 5).text())
            dlg._select(True)
            self.assertTrue(dlg.reconcile.isEnabled())
        finally:
            dlg.close()

        # Reconciliação libera e limpa o registro 'uncertain' órfão no DedupJournal sem mover nada.
        rec = self.engine.reconcile_uncertain(plan['id'], ['second'])
        self.assertEqual(1, rec['released'])
        self.assertEqual(0, rec['blocked'])
        dj_check = DedupJournal(self.db)
        try:
            self.assertIsNone(dj_check.conn.execute('SELECT state FROM moves WHERE file_id=?', ('second',)).fetchone())
        finally:
            dj_check.close()
        self.assertEqual([], self.drive.writes)

    def test_audit4_hash_dedup_recover_cleans_local_db_by_drive_id_even_with_empty_local_id(self):
        from src.database.database import FileIndexer, to_canonical_id
        from src.services.hash_deduplicator import DedupJournal, HashDedupExecuteWorker

        self.drive.items['first'].update(description='t1', createdTime='2019-01-01T00:00:00Z')
        self.drive.items['second'].update(md5Checksum='c' * 32, sha256Checksum='c' * 64, description='t2', createdTime='2020-01-01T00:00:00Z')

        dup_local = r'L:\Drives Compartilhados\Banco de Imagens\Recebidos\Festa (2).jpg'
        other_local = r'L:\Drives Compartilhados\Banco de Imagens\Outro\Festa.jpg'
        idx = FileIndexer(self.db)
        try:
            idx.save_files_in_batch([
                dict(id='d', name='Festa (2).jpg', path=dup_local, source='local', size=100,
                     webContentLink='https://drive.google.com/uc?id=second&export=download'),
                dict(id='o', name='Festa.jpg', path=other_local, source='local', size=100,
                     webContentLink='https://drive.google.com/uc?id=unrelated&export=download'),
            ], source='local')
        finally:
            idx.close()

        plan = self.engine.scan()
        self.drive.fail_after = True
        self.drive.fail_on_op = 'move'
        res_fail = self.run_plan(plan)
        self.assertTrue(res_fail['errors'])
        self.drive.fail_on_op = None
        self.assertEqual('archive', self.drive.items['second']['driveId'])

        class RecoverConfig:
            def get(self, k, d=None):
                return {'production_drive_id': 'bank', 'sandbox_drive_id': 'archive',
                        'scan_paths': [r'L:\Drives Compartilhados\Banco de Imagens']}.get(k, d)
            def get_sandbox_path(self):
                return r'L:\Drives Compartilhados\_TestesBanco'
            def get_excluded_drive_letters(self):
                return {'O:'}
            def is_read_only(self):
                return False
            def is_sandbox(self):
                return False

        worker = HashDedupExecuteWorker(self.db, [], r'L:\Drives Compartilhados\_TestesBanco\Banco_Apagado', self.drive, recover_only=True)
        outputs = []
        errors = []
        worker.execution_finished.connect(outputs.append)
        worker.execution_failed.connect(errors.append)
        with patch('src.services.hash_deduplicator.ConfigManager', return_value=RecoverConfig()):
            worker.run()
        self.assertFalse(errors, errors)
        self.assertEqual(1, outputs[0]['reconciled'])

        idx_after = FileIndexer(self.db)
        try:
            self.assertIsNone(idx_after.conn.execute('SELECT 1 FROM files WHERE file_id=?', (to_canonical_id(dup_local),)).fetchone())
            self.assertIsNotNone(idx_after.conn.execute('SELECT 1 FROM files WHERE file_id=?', (to_canonical_id(other_local),)).fetchone())
        finally:
            idx_after.close()

        dj = DedupJournal(self.db)
        try:
            self.assertEqual(('cleaned',), dj.conn.execute('SELECT state FROM moves WHERE file_id=?', ('second',)).fetchone())
        finally:
            dj.close()
        self.assertEqual('completed', {r['source']['id']: r['state'] for r in self.load(plan)['items']}['second'])

    def test_missing_hash_and_permission_block(self):
        self.drive.items['first'].pop('md5Checksum')
        self.assertTrue(all(r['error'] for r in self.engine.scan()['items']))
        self.drive.items['first']['md5Checksum'] = 'c' * 32
        self.drive.items['first']['capabilities']['canEdit'] = False
        self.assertTrue(all(r['error'] for r in self.engine.scan()['items']))

    def test_folder_shortcut_reported_not_renamed(self):
        self.drive.folder('f1', 'Pasta', 'destination', 'bank')
        self.drive.folder('f2', 'Pasta', 'destination', 'bank')
        self.drive.items['s1'] = dict(id='s1', name='Festa.jpg', parents=['destination'], driveId='bank', mimeType=SHORTCUT)
        plan = self.engine.scan()
        self.assertTrue(all(r['error'] for r in plan['items']))
        self.run_plan(plan, [r['source']['id'] for r in plan['items']])
        self.assertEqual([], self.drive.writes)

    def test_rename_by_id_preserves_description_and_content(self):
        plan = self.engine.scan()
        result = self.run_plan(plan)
        self.assertEqual([], result['errors'])
        self.assertEqual(2, result['completed'])
        self.assertEqual('Festa_0.jpg', self.drive.items['first']['name'])
        self.assertEqual('tags origem', self.drive.items['first']['description'])
        self.assertEqual('c' * 32, self.drive.items['first']['md5Checksum'])
        self.assertTrue(all(r['state'] == 'completed' for r in self.load(plan)['items']))
        self.assertEqual([], self.engine.scan()['items'])
        self.assertEqual(0, self.run_plan(plan)['completed'])

    def test_partial_selection_never_renames_group(self):
        result = self.run_plan(self.engine.scan(), ['first'])
        self.assertTrue(result['errors'])
        self.assertEqual([], self.drive.writes)

    def test_changed_description_blocks_entire_group(self):
        plan = self.engine.scan()
        self.drive.items['second']['description'] = 'Outra pessoa editou'
        self.assertTrue(self.run_plan(plan)['errors'])
        self.assertEqual([], self.drive.writes)

    def test_changed_ancestor_blocks_stale_path_preview(self):
        plan = self.engine.scan()
        self.drive.items['destination']['name'] = 'Outro destino'
        self.assertTrue(self.run_plan(plan)['errors'])
        self.assertEqual([], self.drive.writes)

    def test_new_sibling_or_occupied_planned_name_blocks(self):
        plan = self.engine.scan()
        self.drive.photo('third', 'Festa.jpg', 'destination', md5='e' * 32)
        self.assertTrue(self.run_plan(plan)['errors'])
        self.drive.items['third']['name'] = 'Festa_0.jpg'
        self.assertTrue(self.run_plan(plan)['errors'])
        self.assertEqual([], self.drive.writes)

    def test_timeout_after_write_reconciles_without_repeating(self):
        plan = self.engine.scan()
        self.drive.fail_after = True
        self.assertTrue(self.run_plan(plan)['errors'])
        self.assertEqual('uncertain', self.load(plan)['items'][0]['state'])
        self.assertEqual([], self.run_plan(plan)['errors'])
        self.assertEqual(2, len(self.drive.writes))

    def test_timeout_before_write_never_retries_blindly(self):
        plan = self.engine.scan()
        self.drive.fail_before = True
        self.run_plan(plan)
        self.drive.fail_before = False
        self.assertTrue(self.run_plan(plan)['errors'])
        self.assertEqual([], self.drive.writes)
        self.assertTrue(all(r['error'] for r in self.engine.scan()['items']))

    def test_cancel_after_write_confirms_then_resumes(self):
        plan = self.engine.scan()
        self.drive.cancel_hook = lambda: setattr(self, 'cancelled', True)
        result = self.run_plan(plan)
        self.assertTrue(result['cancelled'])
        self.assertEqual(0, result['completed'])  # Cancelou a última conferência dos irmãos, sem repetir a escrita.
        self.assertEqual('writing', self.load(plan)['items'][0]['state'])
        self.cancelled, self.drive.cancel_hook = False, None
        self.assertEqual(2, self.run_plan(plan)['completed'])
        self.assertEqual(2, len(self.drive.writes))

    def test_readonly_and_pending_queue_block(self):
        plan = self.engine.scan()
        self.cfg.readonly = True
        with self.assertRaises(ValueError):
            self.run_plan(plan)
        self.cfg.readonly = False
        with sqlite3.connect(Path(self.db).parent / 'staging_queue.db') as conn:
            conn.execute('CREATE TABLE items(id TEXT)')
            conn.execute("INSERT INTO items VALUES('pending')")
        conn.close()
        with self.assertRaises(ValueError):
            self.run_plan(plan)
        self.assertEqual([], self.drive.writes)

    def test_incomplete_listing_aborts(self):
        self.drive.incomplete = True
        with self.assertRaises(ValueError):
            self.engine.scan()

    def test_historical_names_portable_and_only_link_by_id(self):
        self.run_plan(self.engine.scan())
        history = NameHistory(self.db)
        path = Path(self.tmp.name) / 'history.json'
        try:
            history.export(path)
        finally:
            history.close()
        other_db = str(Path(self.tmp.name) / 'other' / 'file_index.db')
        other = NameHistory(other_db)
        try:
            self.assertEqual(2, other.import_file(path))
            other.import_file(path)
            self.assertEqual(2, other.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0])
        finally:
            other.close()
        conn = sqlite3.connect(':memory:')
        try:
            conn.execute('CREATE TABLE files(file_id TEXT, webContentLink TEXT)')
            conn.executemany('INSERT INTO files VALUES (?,?)', [('local1', 'https://drive.google.com/file/d/first/view'),
                ('local2', 'https://drive.google.com/file/d/first_OTHER/view'), ('second', '')])
            ids = historical_file_ids(SimpleNamespace(db_name=other_db, conn=conn), 'festa')
            self.assertEqual({'local1', 'second'}, set(ids))
        finally:
            conn.close()

    def test_normal_search_includes_aliases_and_honors_source(self):
        from src.database.search import SearchEngine
        self.run_plan(self.engine.scan())
        conn = sqlite3.connect(':memory:')
        try:
            conn.execute('CREATE TABLE files(file_id TEXT, name TEXT, name_normalized TEXT, path TEXT, mimeType TEXT, source TEXT, description TEXT, thumbnailLink TEXT, thumbnailPath TEXT, size INT, modifiedTime INT, createdTime INT, parentId TEXT, starred INT, webContentLink TEXT, thumbnailRotation INT)')
            conn.execute('CREATE VIRTUAL TABLE search_index USING fts5(name,description,normalized_name,normalized_description,file_id UNINDEXED,source UNINDEXED,tokenize="trigram")')
            conn.execute("INSERT INTO files VALUES('local1','NOVO.jpg','novo.jpg','L:/NOVO.jpg','image/jpeg','local','','','',100,0,0,'',0,'https://drive.google.com/file/d/first/view',0)")
            idx = SimpleNamespace(db_name=self.db, conn=conn, cursor=conn.cursor(), ensure_conn=lambda: None)
            engine = SearchEngine(idx)
            # The actual search path builds records through the indexer's helper.
            idx._build_file_objects_from_search = lambda rows: [dict(file_id=r[0], name=r[1], path=r[2], source=r[4]) for r in rows]
            result = engine.load_files_paged('local', 0, 100, search_term='festa')
            self.assertEqual(1, len(result))
            self.assertEqual(1, len(engine.load_files_paged('local', 0, 100, search_term='Festa.jpg')))
            self.assertEqual(1, len(engine.load_files_paged('local', 0, 100, search_term='"Festa.jpg"')))
            self.assertEqual([], engine.load_files_paged('drive', 0, 100, search_term='festa'))
        finally:
            conn.close()

    def test_numbering_preserves_extension_and_does_not_strip_parentheses(self):
        self.assertEqual('foto (1)_0.jpg', numbered_name('foto (1).jpg', set()))
        self.assertEqual('foto_0', numbered_name('foto', set()))
        self.assertEqual(name_key('Pará.JPG'), name_key('Para\u0301.jpg'))

    def test_history_import_invalid_record_rolls_back_entire_batch(self):
        history = NameHistory(self.db)
        try:
            record = dict(event_id='test', file_id='first', drive_id='bank', old_name='Antigo.jpg',
                          new_name='Novo.jpg', old_path='', new_path='')
            path = Path(self.tmp.name) / 'invalid.json'
            from src.utils.queue_store import atomic_json
            atomic_json(path, dict(schema=1, records=[record, {'file_id': 'bad'}]))
            with self.assertRaises(ValueError):
                history.import_file(path)
            self.assertEqual(0, history.conn.execute('SELECT COUNT(*) FROM records').fetchone()[0])
        finally:
            history.close()

    def test_missing_etag_blocks_without_any_write(self):
        plan = self.engine.scan()
        with patch.object(self.engine.api, 'prepare_write', side_effect=ValueError('Sem ETag')):
            self.assertTrue(self.run_plan(plan)['errors'])
        self.assertEqual([], self.drive.writes)

    def test_another_local_process_lock_blocks_execution(self):
        from PyQt6.QtCore import QLockFile
        plan = self.engine.scan()
        lock = QLockFile(str(Path(self.tmp.name) / 'name_disambiguation' / 'execution.lock'))
        self.assertTrue(lock.tryLock(0))
        try:
            with self.assertRaisesRegex(ValueError, 'execução'):
                self.run_plan(plan)
            self.assertEqual([], self.drive.writes)
        finally:
            lock.unlock()

    def test_reconcile_releases_unapplied_uncertain_with_audit_and_no_drive_writes(self):
        plan = self.engine.scan()
        self.drive.fail_before = True
        self.run_plan(plan)
        self.drive.fail_before = False
        self.assertEqual('uncertain', self.load(plan)['items'][0]['state'])
        self.assertEqual([], self.drive.writes)

        # Conferência e liberação não escrevem no Drive nem executam renomeação automática.
        res = self.engine.reconcile_uncertain(plan['id'], ['first'])
        self.assertEqual(dict(confirmed=0, released=1, blocked=0, errors=[], cancelled=False), res)
        self.assertEqual([], self.drive.writes)
        self.assertEqual('Festa.jpg', self.drive.items['first']['name'])

        loaded = self.load(plan)
        self.assertEqual('pending', loaded['items'][0]['state'])
        journal = NameJournal(self.db)
        try:
            self.assertEqual(set(), journal.uncertain_ids())
            events = journal.events(job=plan['id'], fid='first')
            self.assertTrue(any(e['action'] == 'uncertain_released' for e in events))
        finally:
            journal.close()

        # Nova análise não fica mais presa e aprovação explícita posterior conclui.
        fresh_plan = self.engine.scan()
        self.assertTrue(all(r['state'] == 'pending' for r in fresh_plan['items']))
        done = self.run_plan(fresh_plan)
        self.assertEqual(2, done['completed'])
        self.assertEqual(2, len(self.drive.writes))

    def test_reconcile_confirms_already_applied_uncertain_without_extra_write(self):
        plan = self.engine.scan()
        self.drive.fail_after = True
        self.run_plan(plan)
        self.assertEqual(1, len(self.drive.writes))
        self.assertEqual('uncertain', self.load(plan)['items'][0]['state'])

        res = self.engine.reconcile_uncertain(plan['id'], ['first'])
        self.assertEqual(1, res['confirmed'])
        self.assertEqual(0, res['blocked'])
        self.assertEqual(1, len(self.drive.writes))
        self.assertEqual('completed', self.load(plan)['items'][0]['state'])
        journal = NameJournal(self.db)
        try:
            self.assertEqual(set(), journal.uncertain_ids())
            events = journal.events(job=plan['id'], fid='first')
            self.assertTrue(any(e['action'] == 'uncertain_confirmed_completed' for e in events))
        finally:
            journal.close()

    def test_reconcile_failure_keeps_block_and_never_writes(self):
        scenarios = ('api_error', 'moved_parent', 'changed_md5', 'unexpected_name', 'occupied_destination')
        for mode in scenarios:
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as sub_tmp:
                    db = str(Path(sub_tmp) / 'file_index.db')
                    drive, cfg = RenameDrive(), Config()
                    drive.photo('first', 'Festa.jpg', 'destination', md5='c' * 32)
                    drive.photo('second', 'Festa.jpg', 'destination', md5='d' * 32)
                    engine = NameDisambiguation(drive, db, cfg, lambda: False)
                    plan = engine.scan()
                    drive.fail_before = True
                    engine.execute(plan['id'], ['first', 'second'])
                    drive.fail_before = False

                    if mode == 'api_error':
                        patcher = patch.object(engine.api, 'get', side_effect=TimeoutError('falha na leitura'))
                    else:
                        patcher = patch('builtins.id', wraps=id)  # no-op context
                    if mode == 'moved_parent':
                        drive.items['first']['parents'] = ['bank']
                    elif mode == 'changed_md5':
                        drive.items['first']['md5Checksum'] = 'f' * 32
                    elif mode == 'unexpected_name':
                        drive.items['first']['name'] = 'OutroNome.jpg'
                    elif mode == 'occupied_destination':
                        drive.photo('intruder', 'Festa_0.jpg', 'destination', md5='e' * 32)

                    with patcher:
                        res = engine.reconcile_uncertain(plan['id'], ['first'])
                    self.assertEqual(0, res['confirmed'])
                    self.assertEqual(0, res['released'])
                    self.assertEqual(1, res['blocked'])
                    self.assertTrue(res['errors'])
                    self.assertEqual([], drive.writes)

                    journal = NameJournal(db)
                    try:
                        self.assertIn('first', journal.uncertain_ids())
                        self.assertEqual('uncertain', journal.load(plan['id'])['items'][0]['state'])
                        events = journal.events(job=plan['id'], fid='first')
                        self.assertTrue(any(e['action'] == 'uncertain_check_failed' for e in events))
                    finally:
                        journal.close()
                    self.assertTrue(all(r['state'] == 'blocked' for r in engine.scan()['items']))
                    self.assertEqual([], drive.writes)

    def test_scan_isolates_broken_branch_without_aborting_healthy_groups(self):
        self.drive.folder('broken_parent', 'Orfa', 'missing_grandparent', 'bank')
        self.drive.photo('b1', 'Foto.jpg', 'broken_parent', md5='a' * 32)
        self.drive.photo('b2', 'Foto.jpg', 'broken_parent', md5='b' * 32)
        plan = self.engine.scan()
        self.assertEqual([], self.drive.writes)
        broken = [r for r in plan['items'] if r['source']['id'] in ('b1', 'b2')]
        healthy = [r for r in plan['items'] if r['source']['id'] in ('first', 'second')]
        self.assertEqual(2, len(broken))
        self.assertTrue(all(r['state'] == 'blocked' and 'Hierarquia incompleta' in r['error'] for r in broken))
        self.assertEqual(2, len(healthy))
        self.assertTrue(all(r['state'] == 'pending' for r in healthy))

    def test_leading_space_names_rejected_uniformly(self):
        self.assertFalse(safe_windows_name('   DOENTES FOTOS DELICADAS'))
        self.drive.photo('s1', '   foto.jpg', 'destination', md5='a' * 32)
        self.drive.photo('s2', '   FOTO.jpg', 'destination', md5='b' * 32)
        plan = self.engine.scan()
        space_rows = [r for r in plan['items'] if r['source']['id'] in ('s1', 's2')]
        self.assertEqual(2, len(space_rows))
        self.assertTrue(all(r['state'] == 'blocked' for r in space_rows))
        self.assertEqual([], self.drive.writes)

    def test_unified_search_and_count_with_history_boolean_quotes_and_exclusions(self):
        from src.database.database import FileIndexer
        from src.database.search import SearchEngine

        with tempfile.TemporaryDirectory() as db_tmp:
            db_path = str(Path(db_tmp) / 'file_index.db')
            indexer = FileIndexer(db_path)
            try:
                rows = [
                    ('local1', 'NOVO_0.jpg', 'novo_0.jpg', 'L:/2020/NOVO_0.jpg', 'image/jpeg', 'local',
                     'brasil, 2021', 100, 1, 1, 'L:/2020', 0, 'https://drive.google.com/file/d/first/view'),
                    ('local2', 'Outro.jpg', 'outro.jpg', 'L:/2020/Outro.jpg', 'image/jpeg', 'local',
                     'peru, 2020', 100, 1, 1, 'L:/2020', 0, 'https://drive.google.com/file/d/outro_drive/view'),
                    ('local3', 'SemTag.jpg', 'semtag.jpg', 'L:/2020/SemTag.jpg', 'image/jpeg', 'local',
                     None, 100, 1, 1, 'L:/2020', 0, 'https://drive.google.com/file/d/semtag_drive/view'),
                    ('local4', 'Img_0.jpg', 'img_0.jpg', 'L:/2020/Img_0.jpg', 'image/jpeg', 'local',
                     'missa', 100, 1, 1, 'L:/2020', 0, 'https://drive.google.com/file/d/short_drive/view'),
                ]
                indexer.conn.executemany(
                    'INSERT INTO files(file_id,name,name_normalized,path,mimeType,source,description,size,modifiedTime,createdTime,parentId,starred,webContentLink) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    rows,
                )
                fts_rows = [
                    ('NOVO_0.jpg', 'brasil, 2021', 'novo_0.jpg', 'brasil, 2021', 'local1', 'local'),
                    ('Outro.jpg', 'peru, 2020', 'outro.jpg', 'peru, 2020', 'local2', 'local'),
                    ('SemTag.jpg', '', 'semtag.jpg', '', 'local3', 'local'),
                    ('Img_0.jpg', 'missa', 'img_0.jpg', 'missa', 'local4', 'local'),
                ]
                indexer.conn.executemany(
                    'INSERT INTO search_index(name,description,normalized_name,normalized_description,file_id,source) VALUES (?,?,?,?,?,?)',
                    fts_rows,
                )
                indexer.conn.commit()

                nh = NameHistory(db_path)
                try:
                    nh.record('e1', {'id': 'first', 'name': 'Festa (1).jpg', 'driveId': 'bank', 'parents': ['p']},
                              'NOVO_0.jpg', 'Banco/2020/Festa (1).jpg', 'Banco/2020/NOVO_0.jpg')
                    nh.record('e2', {'id': 'short_drive', 'name': 'Pe Gilson.jpg', 'driveId': 'bank', 'parents': ['p']},
                              'Img_0.jpg', 'Banco/2020/Pe Gilson.jpg', 'Banco/2020/Img_0.jpg')
                    nh.record('e3', {'id': 'semtag_drive', 'name': 'Festa Antiga.jpg', 'driveId': 'bank', 'parents': ['p']},
                              'SemTag.jpg', 'Banco/2020/Festa Antiga.jpg', 'Banco/2020/SemTag.jpg')
                finally:
                    nh.close()

                se = SearchEngine(indexer)
                expectations = {
                    '"Festa (1).jpg"': {'local1'},
                    'Festa brasil': {'local1'},
                    'Festa -2021': {'local3'},  # SemTag tem description=NULL e NÃO é derrubada por NULL em NOT.
                    'Festa NOT 2021': {'local3'},
                    '(Festa OR Outro) NOT 2021': {'local2', 'local3'},
                    '(Festa AND brasil) OR peru': {'local1', 'local2'},
                    'fes* brasil': {'local1'},
                    'pe missa': {'local4'},
                }
                for query, expected_ids in expectations.items():
                    with self.subTest(query=query):
                        found = {r['id'] for r in se.load_files_paged('local', 0, 50, search_term=query)}
                        counted = indexer.count_files('local', search_term=query)
                        self.assertEqual(expected_ids, found)
                        self.assertEqual(len(expected_ids), counted)
            finally:
                indexer.close()

    def test_scan_retries_403_user_rate_limit_exceeded_with_light_fields(self):
        from googleapiclient.errors import HttpError
        from httplib2 import Response

        attempts = {'count': 0, 'fields': []}
        wait_messages = []
        real_list = self.drive.list
        self.engine.progress = lambda _pct, msg: wait_messages.append(msg)
        self.engine.api.on_wait = lambda msg: self.engine.progress(-1, msg)

        def flaky_list(**kwargs):
            attempts['fields'].append(kwargs.get('fields', ''))
            def perform(req):
                attempts['count'] += 1
                if attempts['count'] <= 2:
                    raise HttpError(
                        Response({'status': 403}),
                        b'{"error":{"errors":[{"reason":"userRateLimitExceeded","message":"User rate limit exceeded."}]}}',
                    )
                return real_list(**kwargs).execute()
            return Request(perform)

        with patch.object(self.drive, 'list', side_effect=flaky_list), \
                patch('src.services.drive_retry.time.sleep'):
            plan = self.engine.scan()

        self.assertGreaterEqual(attempts['count'], 3)
        self.assertEqual(['Festa_0.jpg', 'Festa_1.jpg'], [r['new_name'] for r in plan['items']])
        self.assertTrue(all('appProperties' not in f and 'webViewLink' not in f for f in attempts['fields']))
        self.assertTrue(any('Google limitou as consultas; nova tentativa em ' in m for m in wait_messages))
        self.assertEqual([], self.drive.writes)

    def test_prepare_write_compatibility_etag_read_retries_rate_limit(self):
        from googleapiclient.errors import HttpError
        from httplib2 import Response

        attempts = {'count': 0}

        class FakeV2Files:
            def get(self, fileId, supportsAllDrives=True, fields=''):
                def perform(_):
                    attempts['count'] += 1
                    if attempts['count'] == 1:
                        raise HttpError(
                            Response({'status': 403}),
                            b'{"error":{"errors":[{"reason":"userRateLimitExceeded","message":"User rate limit exceeded."}]}}',
                        )
                    return {
                        'id': fileId,
                        'version': '1',
                        'title': 'Festa.jpg',
                        'parents': [{'id': 'destination'}],
                        'driveId': 'bank',
                        'labels': {'trashed': False},
                        'fileSize': '100',
                        'md5Checksum': 'c' * 32,
                        'description': 'tags origem',
                        'etag': '"v2-etag-1"',
                    }
                return Request(perform)

        class FakeV2Service:
            def files(self):
                return FakeV2Files()

        item = copy.deepcopy(self.drive.items['first'])
        self.engine.api._conditional_service = FakeV2Service()
        with patch('src.services.drive_retry.time.sleep'):
            guarded = self.engine.api.prepare_write(item)
        self.assertEqual(2, attempts['count'])
        self.assertEqual('"v2-etag-1"', guarded['_etag'])
        self.assertEqual('v2', guarded['_write_api'])
        self.assertEqual([], self.drive.writes)

    def test_drive_read_does_not_retry_403_permission_error(self):
        from googleapiclient.errors import HttpError
        from httplib2 import Response

        attempts = {'count': 0}

        def denied_list(**kwargs):
            def perform(_):
                attempts['count'] += 1
                raise HttpError(
                    Response({'status': 403}),
                    b'{"error":{"errors":[{"reason":"insufficientFilePermissions","message":"Forbidden"}]}}',
                )
            return Request(perform)

        with patch.object(self.drive, 'list', side_effect=denied_list), \
                patch('src.services.drive_retry.time.sleep'):
            with self.assertRaises(HttpError):
                self.engine.scan()
        self.assertEqual(1, attempts['count'])

    def test_drive_read_stops_promptly_if_cancelled_during_rate_limit_backoff(self):
        from googleapiclient.errors import HttpError
        from httplib2 import Response
        from src.services.hash_deduplicator import DedupCancelled

        attempts = {'count': 0}

        def limited_list(**kwargs):
            def perform(_):
                attempts['count'] += 1
                raise HttpError(
                    Response({'status': 403}),
                    b'{"error":{"errors":[{"reason":"userRateLimitExceeded","message":"User rate limit exceeded."}]}}',
                )
            return Request(perform)

        def fake_sleep(_):
            self.cancelled = True

        with patch.object(self.drive, 'list', side_effect=limited_list), \
                patch('src.services.drive_retry.time.sleep', side_effect=fake_sleep):
            with self.assertRaises(DedupCancelled):
                self.engine.scan()
        self.assertEqual(1, attempts['count'])

    def test_scan_wait_message_distinguishes_5xx_unavailability_from_rate_limit(self):
        from googleapiclient.errors import HttpError
        from httplib2 import Response

        attempts = {'count': 0}
        wait_messages = []
        real_list = self.drive.list
        self.engine.progress = lambda _pct, msg: wait_messages.append(msg)

        def flaky_list(**kwargs):
            def perform(_req):
                attempts['count'] += 1
                if attempts['count'] == 1:
                    raise HttpError(
                        Response({'status': 503}),
                        b'{"error":{"message":"Backend Error"}}',
                    )
                if attempts['count'] == 2:
                    raise HttpError(
                        Response({'status': 500}),
                        b'{"error":{"message":"Internal Error"}}',
                    )
                if attempts['count'] == 3:
                    raise HttpError(
                        Response({'status': 429}),
                        b'{"error":{"message":"Too Many Requests"}}',
                    )
                return real_list(**kwargs).execute()
            return Request(perform)

        with patch.object(self.drive, 'list', side_effect=flaky_list), \
                patch('src.services.drive_retry.time.sleep'):
            plan = self.engine.scan()

        self.assertEqual(5, attempts['count'])
        self.assertEqual(['Festa_0.jpg', 'Festa_1.jpg'], [r['new_name'] for r in plan['items']])
        retry_messages = [m for m in wait_messages if 'nova tentativa em ' in m]
        self.assertEqual(3, len(retry_messages))
        self.assertIn('Google Drive temporariamente indisponível (HTTP 503)', retry_messages[0])
        self.assertIn('Google Drive temporariamente indisponível (HTTP 500)', retry_messages[1])
        self.assertIn('Google limitou as consultas; nova tentativa em ', retry_messages[2])
        self.assertNotIn('limitou', retry_messages[0])
        self.assertNotIn('limitou', retry_messages[1])

    def test_drive_view_version_bump_without_content_change_allows_execution(self):
        self.drive.items.pop('first')
        self.drive.items.pop('second')
        self.drive.photo('keep', 'COLDIP.mp4', 'destination', md5='e' * 32)
        self.drive.photo('dup', 'COLDIP.mp4', 'destination', md5='e' * 32)
        self.drive.items['keep'].update(description='t1', createdTime='2020-01-01T00:00:00Z')
        self.drive.items['dup'].update(description='t1, t2', createdTime='2021-01-01T00:00:00Z')
        plan = self.engine.scan()

        # Simula apenas visualização no Drive Web (viewedByMeTime incrementa `version` sem alterar conteúdo/tags/pasta).
        self.drive.items['keep']['version'] = '7'
        self.drive.items['dup']['version'] = '6'
        res = self.run_plan(plan)
        self.assertEqual([], res['errors'])
        self.assertEqual(2, res['completed'])
        self.assertEqual(1, res['dedup_moved'])

    def test_error_message_includes_group_filename_on_real_metadata_change(self):
        plan = self.engine.scan()
        self.drive.items['first']['description'] = 'editado fora'
        res = self.run_plan(plan)
        self.assertTrue(res['errors'])
        self.assertIn('Festa.jpg', res['errors'][0])
        self.assertEqual([], self.drive.writes)


class SelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_space_checks_selected_rows_but_never_blocked(self):
        from src.ui.review_table import ReviewTable
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QTableWidgetItem
        from PyQt6.QtTest import QTest
        table = ReviewTable(3, 2)
        try:
            for row in range(3):
                item = QTableWidgetItem()
                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if row != 1:
                    flags |= Qt.ItemFlag.ItemIsUserCheckable
                item.setFlags(flags)
                item.setCheckState(Qt.CheckState.Unchecked)
                table.setItem(row, 0, item)
            table.selectAll()
            QTest.keyClick(table, Qt.Key.Key_Space)
            self.assertEqual([Qt.CheckState.Checked, Qt.CheckState.Unchecked, Qt.CheckState.Checked],
                             [table.item(i, 0).checkState() for i in range(3)])
            QTest.keyClick(table, Qt.Key.Key_Space)
            self.assertTrue(all(table.item(i, 0).checkState() == Qt.CheckState.Unchecked for i in range(3)))
        finally:
            table.close()

    def test_dialog_pagination_and_busy_close(self):
        from src.ui.name_disambiguation_dialog import NameDisambiguationDialog
        from PyQt6.QtCore import Qt
        with tempfile.TemporaryDirectory() as tmp:
            dialog = NameDisambiguationDialog(indexer=SimpleNamespace(db_name=str(Path(tmp) / 'db')))
            try:
                rows = [dict(source=dict(id=str(i), name='foto.jpg'), folder_path='Banco', new_name=f'foto_{i}.jpg', state='pending', error='') for i in range(220)]
                dialog._set_plan(dict(id='test', items=rows))
                self.assertEqual(100, dialog.table.rowCount())
                dialog._select(True)
                dialog._page(1)
                self.assertEqual(Qt.CheckState.Checked, dialog.table.item(0, 0).checkState())
                self.assertEqual(220, len(dialog.selected))
                dialog.busy = True
                self.assertFalse(dialog.close())
            finally:
                dialog.busy = False
                dialog.close()

    def test_shift_ctrl_and_whole_apply_cell(self):
        from src.ui.review_table import ReviewTable
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QTableWidgetItem
        from PyQt6.QtTest import QTest
        table = ReviewTable(4, 2)
        try:
            for row in range(4):
                item = QTableWidgetItem()
                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if row != 1:
                    flags |= Qt.ItemFlag.ItemIsUserCheckable
                item.setFlags(flags)
                item.setCheckState(Qt.CheckState.Unchecked)
                table.setItem(row, 0, item)
                table.setItem(row, 1, QTableWidgetItem(str(row)))
            table.resize(400, 240)
            table.show()
            self.app.processEvents()
            def click(row, col, modifier=Qt.KeyboardModifier.NoModifier):
                QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, modifier,
                                 table.visualItemRect(table.item(row, col)).center())
            click(0, 1)
            click(2, 1, Qt.KeyboardModifier.ShiftModifier)
            self.assertEqual({0, 1, 2}, {i.row() for i in table.selectionModel().selectedRows()})
            click(1, 1, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual({0, 2}, {i.row() for i in table.selectionModel().selectedRows()})
            click(3, 0)
            self.assertEqual(Qt.CheckState.Checked, table.item(3, 0).checkState())
            click(1, 0)
            self.assertEqual(Qt.CheckState.Unchecked, table.item(1, 0).checkState())
        finally:
            table.close()

    def test_dialog_reconcile_button_tracks_uncertain_selection(self):
        from src.ui.name_disambiguation_dialog import NameDisambiguationDialog
        from PyQt6.QtCore import Qt
        with tempfile.TemporaryDirectory() as tmp:
            dialog = NameDisambiguationDialog(indexer=SimpleNamespace(db_name=str(Path(tmp) / 'db')))
            try:
                rows = [
                    dict(source=dict(id='1', name='a.jpg'), folder_path='Banco', new_name='a_0.jpg', state='pending', error=''),
                    dict(source=dict(id='2', name='b.jpg'), folder_path='Banco', new_name='b_0.jpg', state='uncertain', error='timeout'),
                ]
                dialog._set_plan(dict(id='test', items=rows))
                self.assertFalse(dialog.reconcile.isEnabled())
                dialog.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
                self.assertFalse(dialog.reconcile.isEnabled())
                dialog.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
                self.assertTrue(dialog.reconcile.isEnabled())
            finally:
                dialog.close()

    def test_dialog_renders_cloud_dedup_and_mixed_actions(self):
        from src.ui.name_disambiguation_dialog import NameDisambiguationDialog
        from PyQt6.QtCore import Qt
        with tempfile.TemporaryDirectory() as tmp:
            dialog = NameDisambiguationDialog(indexer=SimpleNamespace(db_name=str(Path(tmp) / 'db')))
            try:
                rows = [
                    dict(source=dict(id='k1', name='foto.jpg', md5Checksum='a' * 32, size='10'),
                         folder_path='Banco/2020', action='dedup_keep', new_name='foto.jpg',
                         merged_description='t1, t2', discrepancy_reasons=[], state='pending', error=''),
                    dict(source=dict(id='d1', name='foto.jpg', md5Checksum='a' * 32, size='10'),
                         folder_path='Banco/2020', action='dedup_move', new_name='',
                         archive_path='_TestesBanco/Banco_Apagado/2020/foto.jpg', state='pending', error=''),
                    dict(source=dict(id='m1', name='x.jpg', md5Checksum='b' * 32, size='20'),
                         folder_path='Banco/2020', action='dedup_keep_and_rename', new_name='x_0.jpg',
                         merged_description='tm', discrepancy_reasons=[], state='tags_done', error=''),
                ]
                dialog._set_plan(dict(id='test', items=rows))
                self.assertIn('Manter nome (foto.jpg)', dialog.table.item(0, 3).text())
                self.assertIn('t1, t2', dialog.table.item(0, 3).text())
                self.assertIn('Mover → _TestesBanco/Banco_Apagado/2020/foto.jpg', dialog.table.item(1, 3).text())
                self.assertIn('Fundir tags → x_0.jpg', dialog.table.item(2, 3).text())
                self.assertEqual('Tags gravadas; falta renomear', dialog.table.item(2, 5).text())
                dialog._select(True)
                self.assertEqual({'k1', 'd1', 'm1'}, dialog.selected)
                self.assertEqual(Qt.CheckState.Checked, dialog.table.item(2, 0).checkState())
            finally:
                dialog.close()

    def test_dialog_shows_warning_popup_when_execute_returns_errors(self):
        from src.ui.name_disambiguation_dialog import NameDisambiguationDialog
        with tempfile.TemporaryDirectory() as tmp:
            dialog = NameDisambiguationDialog(indexer=SimpleNamespace(db_name=str(Path(tmp) / 'db')))
            try:
                with patch('src.ui.name_disambiguation_dialog.QMessageBox.warning') as warning:
                    dialog._finished(dict(completed=0, dedup_moved=0, tags_updated=0, errors=['COLDIP.mp4: erro simulado']))
                warning.assert_called_once()
                self.assertIn('COLDIP.mp4: erro simulado', dialog.status.text())
            finally:
                dialog.close()


if __name__ == '__main__':
    unittest.main()
