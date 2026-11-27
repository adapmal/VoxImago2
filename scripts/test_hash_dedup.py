"""Deduplicação: API simulada e bancos temporários; nunca toca no acervo real."""
import copy
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.database.database import FileIndexer
from src.services.hash_deduplicator import (
    HashScanWorker, HashDedupExecuteWorker, MODE_FOLDER_VS_FOLDER, MODE_ALL_VS_ALL,
    MODE_CUSTOM_VS_ALL, MODE_SELECTED_FOLDERS, DriveCatalog, DedupJournal, compute_merged_tags, validate_scope,
    select_keeper_and_duplicates,
)

ROOT = r'L:\Drives Compartilhados\Banco de Imagens'
SOURCE = ROOT + r'\Organizar\A'
REFERENCE = ROOT + r'\Selecionadas'
DEST = r'L:\Drives Compartilhados\_TestesBanco\Banco_Apagado'


class Config:
    readonly = False
    def get(self, key, default=None):
        return {'scan_paths': [ROOT], 'production_drive_id': 'src', 'sandbox_drive_id': 'dst'}.get(key, default)
    def get_excluded_drive_letters(self):
        return {'O:'}
    def get_sandbox_path(self):
        return r'L:\Drives Compartilhados\_TestesBanco'
    def is_read_only(self):
        return self.readonly
    def is_sandbox(self):
        return False


class Request:
    def __init__(self, fn, etag=None):
        self.fn, self.etag, self.callbacks, self.headers = fn, etag, [], {}
    def add_response_callback(self, fn):
        self.callbacks.append(fn)
    def execute(self):
        for fn in self.callbacks:
            fn({'etag': self.etag} if self.etag else {})
        return self.fn(self)


class FakeDrive:
    def __init__(self):
        self.items, self.writes, self.reads = {}, [], []
        self.fail_move_after = False
        self.fail_tags = False
        self.etags = True
        self.page_size = 2
        self.folder('src', 'Banco de Imagens', None, 'src')
        self.folder('dst', '_TestesBanco', None, 'dst')
        self.folder('org', 'Organizar', 'src', 'src')
        self.folder('a', 'A', 'org', 'src')
        self.folder('b', 'B', 'org', 'src')
        self.folder('ref', 'Selecionadas', 'src', 'src')
        self.photo('keep', 'keep.jpg', 'ref', 'base')
        self.photo('dup', 'dup.jpg', 'a', 'herdada')
        self.photo('other', 'other.jpg', 'b', 'fora do escopo')
    def files(self):
        return self
    def folder(self, fid, name, parent, drive):
        self.items[fid] = dict(id=fid, name=name, parents=[parent] if parent else [], driveId=drive,
                              mimeType='application/vnd.google-apps.folder', trashed=False,
                              version='1', capabilities={'canAddChildren': True})
    def photo(self, fid, name, parent, desc):
        self.items[fid] = dict(id=fid, name=name, parents=[parent], driveId='src', size='123',
            mimeType='image/jpeg', trashed=False, md5Checksum='a' * 32, sha256Checksum='b' * 64,
            description=desc, version='1', createdTime='2020-01-01T00:00:00Z',
            capabilities={'canEdit': True, 'canMoveItemOutOfDrive': True})
    def get(self, fileId, **kwargs):
        item = self.items[fileId]
        return Request(lambda _: copy.deepcopy(self.items[fileId]), f'"{item["version"]}"' if self.etags else None)
    def list(self, q, driveId, pageToken=None, **kwargs):
        self.reads.append(dict(q=q, driveId=driveId, pageToken=pageToken, **kwargs))
        parent = re.search(r"'([^']+)' in parents", q)
        entries = [v for v in self.items.values() if v['driveId'] == driveId and not v['trashed']
                   and (not parent or parent.group(1) in v.get('parents', []))]
        start = int(pageToken or 0)
        page = {'files': copy.deepcopy(entries[start:start+self.page_size])}
        if start + self.page_size < len(entries):
            page['nextPageToken'] = str(start+self.page_size)
        return Request(lambda _: page)
    def update(self, fileId, body=None, addParents=None, **kwargs):
        def perform(request):
            item = self.items[fileId]
            if request.headers.get('If-Match') != f'"{item["version"]}"':
                raise RuntimeError('412 Precondition failed')
            if body and 'description' in body and self.fail_tags:
                raise RuntimeError('tags refused')
            self.writes.append(('update', fileId, addParents, body))
            if body:
                item.update(body)
            if addParents:
                item['parents'] = [addParents]
                item['driveId'] = self.items[addParents]['driveId']
            item['version'] = str(int(item['version']) + 1)
            if addParents and self.fail_move_after:
                raise TimeoutError('response lost after remote move')
            return copy.deepcopy(item)
        return Request(perform)
    def create(self, body, **kwargs):
        def perform(_):
            fid = 'new-' + str(len(self.items))
            parent = body['parents'][0]
            self.writes.append(('create', fid, parent, body))
            self.folder(fid, body['name'], parent, self.items[parent]['driveId'])
            return copy.deepcopy(self.items[fid])
        return Request(perform)


class FakeConditionalDrive:
    """Representação v2 do mesmo servidor; valida If-Match no PATCH."""
    def __init__(self, drive):
        self.drive, self.missing_etag, self.patch_calls = drive, False, []
    def files(self):
        return self
    def get(self, fileId, **kwargs):
        def perform(_):
            item = self.drive.items[fileId]
            return dict(id=fileId, etag=None if self.missing_etag else f'"{item["version"]}"',
                        version=item['version'], title=item['name'], parents=[{'id': p} for p in item['parents']],
                        driveId=item['driveId'], labels={'trashed': item['trashed']}, fileSize=item['size'],
                        md5Checksum=item['md5Checksum'], description=item.get('description', ''))
        return Request(perform)
    def patch(self, fileId, body, **kwargs):
        self.patch_calls.append(dict(fileId=fileId, body=copy.deepcopy(body), **kwargs))
        translated = dict(body)
        if 'title' in translated:
            translated['name'] = translated.pop('title')
        return self.drive.update(fileId=fileId, body=translated, **kwargs)


class HashDedupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'data' / 'catalog.db'
        self.cfg, self.drive = Config(), FakeDrive()
        self.patch = patch('src.services.hash_deduplicator.ConfigManager', return_value=self.cfg)
        self.patch.start()
        indexer = FileIndexer(str(self.db))
        paths = {'keep': REFERENCE + r'\keep.jpg', 'dup': SOURCE + r'\dup.jpg',
                 'other': ROOT + r'\Organizar\B\other.jpg'}
        indexer.save_files_in_batch([dict(id=path, path=path, name=self.drive.items[fid]['name'],
            source='local', size=123, mimeType='image/jpeg', description='stale local', modifiedTime=1, createdTime=1,
            parentId=os.path.dirname(path), webContentLink=f'https://drive.google.com/file/d/{fid}/view')
            for fid, path in paths.items()], source='local')
        indexer.close()
    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
    def scan(self, mode=MODE_FOLDER_VS_FOLDER, folders=None, references=None):
        worker = HashScanWorker(str(self.db), mode, custom_folders=folders or [SOURCE],
            reference_folders=references if references is not None else [REFERENCE], drive_service=self.drive)
        outputs, errors = [], []
        worker.scan_finished.connect(outputs.append)
        worker.scan_failed.connect(errors.append)
        with patch('builtins.open', side_effect=AssertionError('must not read media')):
            worker.run()
        self.assertFalse(errors)
        return outputs[0]
    def execute(self, groups, recover=False):
        for g in groups:
            g.checked = True
        worker = HashDedupExecuteWorker(str(self.db), groups, DEST, self.drive, recover_only=recover)
        results, errors = [], []
        worker.execution_finished.connect(results.append)
        worker.execution_failed.connect(errors.append)
        worker.run()
        self.assertFalse(errors, errors)
        return results[0]
    def test_pair_protects_reference_and_excludes_other_organizar(self):
        groups = self.scan()
        self.assertEqual(1, len(groups))
        self.assertEqual('keep', groups[0].keeper['drive_id'])
        self.assertEqual(['dup'], [d['drive_id'] for d in groups[0].duplicates])
        self.assertTrue(groups[0].checked)
        self.assertEqual({'dup'}, groups[0].eligible_ids)
    def test_custom_vs_all_only_moves_selected_origin(self):
        groups = self.scan(MODE_CUSTOM_VS_ALL, references=[])
        self.assertEqual(['dup'], [d['drive_id'] for d in groups[0].duplicates])
    def test_pair_ignores_duplicates_only_within_origin(self):
        self.drive.items['keep']['md5Checksum'] = 'c' * 32
        self.assertEqual([], self.scan())
    def test_overlap_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_scope(MODE_FOLDER_VS_FOLDER, [SOURCE], [ROOT], [ROOT], self.cfg)
    def test_merge_preserves_all_distinct_names_and_tags(self):
        self.assertEqual('A, B, renamed.JPG, C, second.jpg', compute_merged_tags('A', 'keep.jpg', [
            {'name': 'renamed.JPG', 'description': 'a, B'}, {'name': 'second.jpg', 'description': 'C'},
            {'name': 'KEEP.jpg', 'description': 'B'}]))
    def test_cloud_tags_changed_after_scan_are_preserved(self):
        groups = self.scan()
        self.drive.items['keep']['description'] = 'base, recém adicionada'
        self.drive.items['dup']['description'] = 'herdada, nova na cópia'
        result = self.execute(groups)
        self.assertEqual(1, result['files_moved'], result)
        self.assertEqual([], result['errors'])
        desc = self.drive.items['keep']['description']
        self.assertIn('recém adicionada', desc)
        self.assertIn('nova na cópia', desc)
        self.assertIn('dup.jpg', desc)
        self.assertEqual('src', self.drive.items['other']['driveId'])
        self.assertEqual('dst', self.drive.items['dup']['driveId'])
    def test_hash_change_blocks_writes(self):
        groups = self.scan()
        self.drive.items['dup']['md5Checksum'] = 'f' * 32
        result = self.execute(groups)
        self.assertTrue(result['errors'])
        self.assertEqual([], self.drive.writes)
    def test_tag_write_failure_prevents_folders_and_moves(self):
        groups = self.scan()
        self.drive.fail_tags = True
        result = self.execute(groups)
        self.assertTrue(result['errors'])
        self.assertEqual([], self.drive.writes)
    def test_no_etag_blocks_writes(self):
        groups = self.scan()
        self.drive.etags = False
        self.assertTrue(self.execute(groups)['errors'])
        self.assertEqual([], self.drive.writes)

    def test_v2_fallback_without_v3_etag_updates_tags_and_moves(self):
        groups = self.scan()
        self.drive.etags = False
        legacy = FakeConditionalDrive(self.drive)
        with patch('src.services.hash_deduplicator.conditional_service', return_value=legacy):
            result = self.execute(groups)
        self.assertEqual([], result['errors'])
        self.assertEqual(1, result['files_moved'])
        self.assertEqual(1, result['tags_updated'])
        self.assertEqual('dst', self.drive.items['dup']['driveId'])
        self.assertTrue(any('title' in call['body'] and call.get('addParents') for call in legacy.patch_calls))

    def test_missing_v2_etag_also_blocks_without_writes(self):
        groups = self.scan()
        self.drive.etags = False
        legacy = FakeConditionalDrive(self.drive)
        legacy.missing_etag = True
        with patch('src.services.hash_deduplicator.conditional_service', return_value=legacy):
            result = self.execute(groups)
        self.assertTrue(result['errors'])
        self.assertEqual([], self.drive.writes)

    def test_v2_if_match_blocks_concurrent_update(self):
        self.drive.etags = False
        legacy = FakeConditionalDrive(self.drive)
        with patch('src.services.hash_deduplicator.conditional_service', return_value=legacy):
            catalog = DriveCatalog(self.drive, 'src')
            guarded = catalog.prepare_write(catalog.get('keep'))
            self.drive.items['keep']['version'] = '2'
            with self.assertRaisesRegex(RuntimeError, '412'):
                catalog.update(guarded, body={'description': 'old value'})
        self.assertEqual([], self.drive.writes)

    def test_v2_read_different_version_from_v3_blocks(self):
        self.drive.etags = False
        legacy = FakeConditionalDrive(self.drive)
        with patch('src.services.hash_deduplicator.conditional_service', return_value=legacy):
            catalog = DriveCatalog(self.drive, 'src')
            before = catalog.get('keep')
            self.drive.items['keep']['version'] = '2'
            with self.assertRaisesRegex(ValueError, 'mudou'):
                catalog.prepare_write(before)
        self.assertEqual([], self.drive.writes)

    def test_v2_real_sdk_serializes_conditional_patch_without_network(self):
        import json
        import httplib2
        from types import SimpleNamespace
        legacy = FakeConditionalDrive(self.drive).get('keep').execute()
        class OfflineHttp:
            def __init__(self):
                self.calls = []
            def request(self, uri, method='GET', body=None, headers=None, **kwargs):
                self.calls.append((uri, method, body, headers))
                payload = legacy if method == 'GET' else {'id': 'keep'}
                return httplib2.Response({'status': '200'}), json.dumps(payload).encode()
        http = OfflineHttp()
        catalog = DriveCatalog(SimpleNamespace(_http=http), 'src')
        before = dict(self.drive.items['keep'], _etag=None)
        catalog.update(before, body={'name': 'renamed.jpg', 'description': 'test'},
                       addParents='parent-new', removeParents='ref')
        self.assertEqual(2, len(http.calls))
        uri, method, body, headers = http.calls[-1]
        self.assertIn('/drive/v2/files/keep', uri)
        self.assertIn('supportsAllDrives=true', uri)
        self.assertIn('addParents=parent-new', uri)
        self.assertEqual('PATCH', method)
        self.assertEqual({'title': 'renamed.jpg', 'description': 'test'}, json.loads(body))
        self.assertEqual(legacy['etag'], headers['If-Match'])

    def test_v2_timeout_after_move_recovers_without_repeating(self):
        groups = self.scan()
        self.drive.etags = False
        self.drive.fail_move_after = True
        legacy = FakeConditionalDrive(self.drive)
        with patch('src.services.hash_deduplicator.conditional_service', return_value=legacy):
            result = self.execute(groups)
            self.assertTrue(result['errors'])
            writes = len(self.drive.writes)
            result = self.execute([], recover=True)
        self.assertEqual(1, result['reconciled'])
        self.assertEqual(writes, len(self.drive.writes))

    def test_manual_tags_replace_union_including_removals(self):
        groups = self.scan()
        groups[0].edited_description = 'Escolhida; nova, escolhida'
        result = self.execute(groups)
        self.assertEqual([], result['errors'])
        self.assertEqual('Escolhida, nova', self.drive.items['keep']['description'])

    def test_manual_tags_cloud_change_blocks_before_writing(self):
        groups = self.scan()
        groups[0].edited_description = 'manual'
        self.drive.items['dup']['description'] += ', edição externa'
        result = self.execute(groups)
        self.assertIn('edição manual', result['errors'][0])
        self.assertEqual([], self.drive.writes)

    def test_explicit_empty_manual_tags_are_respected(self):
        groups = self.scan()
        groups[0].edited_description = ''
        result = self.execute(groups)
        self.assertEqual([], result['errors'])
        self.assertEqual('', self.drive.items['keep']['description'])

    def test_year_root_priority_is_before_depth_and_organizar(self):
        def item(path):
            return dict(path=ROOT + path, name='photo.jpg', description='')
        cases = [
            (r'\1999\Deep\Nested\photo.jpg', r'\2010\photo.jpg'),
            (r'\9\photo.jpg', r'\10\photo.jpg'),
            (r'\2005\photo.jpg', r'\Outros\photo.jpg'),
            (r'\1999\Organizar\photo.jpg', r'\2020\photo.jpg'),
        ]
        for winner, loser in cases:
            chosen, _ = select_keeper_and_duplicates([item(loser), item(winner)], [ROOT])
            self.assertEqual(ROOT + winner, chosen['path'])

    def test_numeric_subfolder_is_not_a_numeric_root(self):
        items = [dict(path=ROOT + path, name='x.jpg', description='')
                 for path in (r'\A\1999\x.jpg', r'\B\1000\x.jpg')]
        chosen, _ = select_keeper_and_duplicates(items, [ROOT])
        self.assertEqual(items[0]['path'], chosen['path'])

    def test_exif_date_taken_year_beats_root_and_organizar(self):
        def item(path, exif_time='2002:05:10 14:30:00'):
            return dict(
                path=ROOT + path,
                name='photo.jpg',
                description='',
                imageMediaMetadata={'time': exif_time},
            )
        # Nova regra: pasta raiz mais antiga SEMPRE vence, mesmo quando DateTaken é diferente.
        chosen, _ = select_keeper_and_duplicates(
            [item(r'\2002\Evento\Sub\photo.jpg'), item(r'\1999\Evento\photo.jpg')],
            [ROOT],
        )
        self.assertEqual(ROOT + r'\1999\Evento\photo.jpg', chosen['path'])

        # Pasta raiz mais antiga vence também sobre estar fora de Organizar em ano mais recente.
        chosen_org, _ = select_keeper_and_duplicates(
            [item(r'\2002\Eventos\photo.jpg'), item(r'\1999\Organizar\photo.jpg')],
            [ROOT],
        )
        self.assertEqual(ROOT + r'\1999\Organizar\photo.jpg', chosen_org['path'])
    def test_duplicate_folders_on_later_page_are_ambiguous(self):
        self.drive.folder('ref2', 'Selecionadas', 'src', 'src')
        self.assertEqual([], self.scan())
    def test_missing_checksum_never_reads_local_file(self):
        del self.drive.items['dup']['md5Checksum']
        self.assertEqual([], self.scan())
    def test_timeout_after_move_is_reconciled_without_second_move(self):
        groups = self.scan()
        self.drive.fail_move_after = True
        result = self.execute(groups)
        self.assertTrue(result['errors'])
        self.assertEqual('dst', self.drive.items['dup']['driveId'])
        movements = len([w for w in self.drive.writes if w[0] == 'update' and w[2]])
        result = self.execute([], recover=True)
        self.assertEqual(1, result['reconciled'])
        self.assertEqual(movements, len([w for w in self.drive.writes if w[0] == 'update' and w[2]]))
        journal = DedupJournal(str(self.db))
        self.assertEqual('cleaned', journal.conn.execute("SELECT state FROM moves WHERE file_id='dup'").fetchone()[0])
        journal.close()
    def test_inversion_cannot_expand_move_scope(self):
        group = self.scan()[0]
        group.keeper, group.duplicates = group.duplicates[0], [group.keeper]
        result = self.execute([group])
        self.assertTrue(result['errors'])
        self.assertEqual([], self.drive.writes)
    def test_conflicting_sha256_is_not_a_duplicate_group(self):
        self.drive.items['dup']['sha256Checksum'] = 'c' * 64
        self.assertEqual([], self.scan())

    def test_identical_filename_is_not_added_as_a_tag(self):
        self.assertEqual('A, B', compute_merged_tags('A', 'foto.jpg', [{'name': 'FOTO.JPG', 'description': 'B'}]))

    def test_all_vs_all_keeps_shallow_non_organizar(self):
        group = self.scan(MODE_ALL_VS_ALL, references=[])[0]
        self.assertEqual('keep', group.keeper['drive_id'])
        self.assertEqual({'dup', 'other'}, {d['drive_id'] for d in group.duplicates})

    def test_multiple_duplicates_in_origin_contribute_all_names(self):
        self.drive.photo('dup2', 'second.jpg', 'a', 'segunda')
        idx = FileIndexer(str(self.db))
        path = SOURCE + r'\second.jpg'
        idx.save_files_in_batch([dict(id=path, path=path, source='local', name='second.jpg',
            size=123, mimeType='image/jpeg', description='', modifiedTime=1, createdTime=1)], source='local')
        idx.close()
        result = self.execute(self.scan())
        self.assertEqual(2, result['files_moved'], result)
        self.assertIn('dup.jpg', self.drive.items['keep']['description'])
        self.assertIn('second.jpg', self.drive.items['keep']['description'])

    def test_ambiguous_destination_does_not_choose_first_folder(self):
        groups = self.scan()
        self.drive.folder('dest1', 'Banco_Apagado', 'dst', 'dst')
        self.drive.folder('dest2', 'Banco_Apagado', 'dst', 'dst')
        result = self.execute(groups)
        self.assertTrue(result['errors'])
        self.assertEqual('src', self.drive.items['dup']['driveId'])

    def test_remote_move_then_sqlite_failure_is_recoverable(self):
        groups = self.scan()
        with patch('src.services.hash_deduplicator.clean_local', side_effect=RuntimeError('SQLite full')):
            result = self.execute(groups)
        self.assertEqual(1, result['files_moved'])
        self.assertTrue(result['errors'])
        writes = len(self.drive.writes)
        result = self.execute([], recover=True)
        self.assertEqual(1, result['reconciled'])
        self.assertEqual(writes, len(self.drive.writes))

    def test_queue_and_read_only_block_execution(self):
        groups = self.scan()
        worker = HashDedupExecuteWorker(str(self.db), groups, DEST, self.drive)
        errors = []
        worker.execution_failed.connect(errors.append)
        self.cfg.readonly = True
        worker.run()
        self.assertTrue(errors)
        self.cfg.readonly = False
        errors.clear()
        with patch('src.services.hash_deduplicator.assert_queue_empty', side_effect=ValueError('fila pendente')):
            worker.run()
        self.assertTrue(errors)
        self.assertEqual([], self.drive.writes)

    def test_conditional_update_rejects_new_version(self):
        catalog = DriveCatalog(self.drive, 'src')
        before = catalog.get('keep')
        self.drive.items['keep']['version'] = '2'
        with self.assertRaises(RuntimeError):
            catalog.update(before, body={'description': 'must not overwrite'})
        self.assertEqual('base', self.drive.items['keep']['description'])

    def test_selected_folders_excludes_third_tree_and_uses_scoped_requests(self):
        group = self.scan(MODE_SELECTED_FOLDERS, [SOURCE, REFERENCE], [])[0]
        self.assertTrue(group.checked)
        self.assertEqual({'dup', 'keep'}, group.eligible_ids)
        self.assertEqual('keep', group.keeper['drive_id'])
        self.assertEqual(['dup'], [d['drive_id'] for d in group.duplicates])
        for request in self.drive.reads:
            self.assertIn('in parents', request['q'])
            self.assertNotIn("'b' in parents", request['q'])
            if "'src' in parents" in request['q'] or "'org' in parents" in request['q']:
                self.assertNotIn('md5Checksum', request['fields'])

    def test_three_selected_folders_are_symmetric_and_order_independent(self):
        folders = [SOURCE, REFERENCE, ROOT + r'\Organizar\B']
        for ordered in (folders, folders[::-1]):
            group = self.scan(MODE_SELECTED_FOLDERS, ordered, [])[0]
            self.assertEqual('keep', group.keeper['drive_id'])
            self.assertEqual({'dup', 'other'}, {d['drive_id'] for d in group.duplicates})
            self.assertEqual({'dup', 'other', 'keep'}, group.eligible_ids)

    def test_selected_manual_keeper_can_be_in_any_selected_folder(self):
        group = self.scan(MODE_SELECTED_FOLDERS, [SOURCE, REFERENCE], [])[0]
        group.keeper, group.duplicates = group.duplicates[0], [group.keeper]
        result = self.execute([group])
        self.assertEqual([], result['errors'])
        self.assertEqual(1, result['files_moved'])
        self.assertEqual('dst', self.drive.items['keep']['driveId'])
        self.assertEqual('src', self.drive.items['dup']['driveId'])
        self.assertEqual('src', self.drive.items['other']['driveId'])

    def test_selected_overlap_loads_each_subtree_once(self):
        group = self.scan(MODE_SELECTED_FOLDERS, [ROOT + r'\Organizar', SOURCE, REFERENCE], [])[0]
        self.assertEqual({'dup', 'other'}, {d['drive_id'] for d in group.duplicates})
        a_requests = [r for r in self.drive.reads if "'a' in parents" in r['q']]
        self.assertEqual(1, len(a_requests))

    def test_selected_descendants_are_loaded_recursively(self):
        self.drive.folder('nested', 'Nested', 'a', 'src')
        self.drive.photo('nested-photo', 'nested.jpg', 'nested', 'nested tag')
        path = SOURCE + r'\Nested\nested.jpg'
        idx = FileIndexer(str(self.db))
        idx.save_files_in_batch([dict(id=path, path=path, source='local', name='nested.jpg',
            size=123, mimeType='image/jpeg', description='', modifiedTime=1, createdTime=1)], source='local')
        idx.close()
        group = self.scan(MODE_SELECTED_FOLDERS, [SOURCE, REFERENCE], [])[0]
        self.assertEqual({'dup', 'nested-photo'}, {d['drive_id'] for d in group.duplicates})

    def test_selected_requires_two_distinct_folders(self):
        for folders in ([SOURCE], [SOURCE, SOURCE.upper()]):
            with self.assertRaises(ValueError):
                validate_scope(MODE_SELECTED_FOLDERS, folders, [], [ROOT], self.cfg)

    def test_keeper_priority_chain(self):
        base = dict(name='a.jpg', description='', drive_created='2020-01-01T00:00:00Z')
        cases = [
            (dict(path=ROOT + r'\X\Y\a.jpg'), dict(path=ROOT + r'\Organizar\a.jpg', description='many, tags')),
            (dict(path=ROOT + r'\X\a.jpg'), dict(path=ROOT + r'\X\Y\a.jpg', description='many, tags')),
            (dict(path=ROOT + r'\Long\a.jpg', description='tag'), dict(path=ROOT + r'\X\a.jpg')),
            (dict(path=ROOT + r'\X\a.jpg'), dict(path=ROOT + r'\Long\a.jpg', drive_created='2010-01-01T00:00:00Z')),
            (dict(path=ROOT + r'\X\b.jpg', drive_created='2010-01-01T00:00:00Z'), dict(path=ROOT + r'\X\a.jpg')),
        ]
        for winner, loser in cases:
            with self.subTest(winner=winner):
                chosen, _ = select_keeper_and_duplicates([dict(base, **loser), dict(base, **winner)], [ROOT])
                self.assertEqual(winner['path'], chosen['path'])

    def test_selected_ambiguous_ancestor_blocks_loading_subtree(self):
        self.drive.folder('org2', 'Organizar', 'src', 'src')
        catalog = DriveCatalog(self.drive, 'src')
        with self.assertRaises(ValueError):
            catalog.load_selected([['Organizar', 'A'], ['Selecionadas']])
        self.assertFalse(any("'a' in parents" in r['q'] for r in self.drive.reads))

    def test_selected_root_is_allowed_and_no_id_is_repeated(self):
        group = self.scan(MODE_SELECTED_FOLDERS, [ROOT, SOURCE], [])[0]
        self.assertEqual({'dup', 'other'}, {d['drive_id'] for d in group.duplicates})
        self.assertEqual(2, len(group.duplicates))

    def test_compute_merged_tags_skips_unmatched_root_year_tags(self):
        merged = compute_merged_tags(
            '2001, base',
            'keep.jpg',
            [
                {'name': 'dup.jpg', 'description': '1999, Evento, 2002'},
                {'name': '1999.jpg', 'description': '1998, Viagem'},
            ],
            keeper_path=ROOT + r'\2002\Brasil\keep.jpg',
            roots=[ROOT],
        )
        # Mantém '2001' que já era do keeper e '2002' compatível com a raiz 2002;
        # preserva nome de arquivo '1999.jpg', mas NÃO acrescenta anos avulsos 1999/1998.
        self.assertEqual('2001, base, Evento, 2002, dup.jpg, Viagem, 1999.jpg', merged)

        # Keeper fora de pasta numérica de ano (ex: Organizar) também não herda anos avulsos
        merged_org = compute_merged_tags(
            'praia',
            'keep.jpg',
            [{'name': 'dup.jpg', 'description': '1999, sol'}],
            keeper_rel_path=r'Organizar\keep.jpg',
        )
        self.assertEqual('praia, sol, dup.jpg', merged_org)

    def test_execute_group_ignores_unmatched_year_tag_from_live_duplicate(self):
        self.drive.folder('y2002', '2002', 'src', 'src')
        self.drive.folder('y1999', '1999', 'src', 'src')
        self.drive.photo('keep_y', 'keep_y.jpg', 'y1999', 'Família')
        self.drive.items['keep_y']['imageMediaMetadata'] = {'time': '2002:05:10 10:00:00'}
        self.drive.photo('dup_y', 'dup_y.jpg', 'y2002', '2002, Verão, 1999')
        idx = FileIndexer(str(self.db))
        p_keep = ROOT + r'\1999\keep_y.jpg'
        p_dup = ROOT + r'\2002\dup_y.jpg'
        idx.save_files_in_batch([
            dict(id=p_keep, path=p_keep, name='keep_y.jpg', source='local', size=123, mimeType='image/jpeg',
                 description='', modifiedTime=1, createdTime=1, parentId=os.path.dirname(p_keep),
                 webContentLink='https://drive.google.com/file/d/keep_y/view'),
            dict(id=p_dup, path=p_dup, name='dup_y.jpg', source='local', size=123, mimeType='image/jpeg',
                 description='', modifiedTime=1, createdTime=1, parentId=os.path.dirname(p_dup),
                 webContentLink='https://drive.google.com/file/d/dup_y/view'),
        ], source='local')
        idx.close()

        groups = self.scan(MODE_SELECTED_FOLDERS, [ROOT + r'\2002', ROOT + r'\1999'], [])
        # Pasta raiz mais antiga (1999) vence; '2002' vindo de dup_y é ignorado, mas EXIF=2002 soma tag 'DateTaken'.
        target_grp = [g for g in groups if g.keeper['drive_id'] == 'keep_y'][0]
        self.assertEqual('Família, Verão, 1999, dup_y.jpg, DateTaken', target_grp.proposed_description)

        result = self.execute([target_grp])
        self.assertEqual([], result['errors'])
        self.assertEqual('Família, Verão, 1999, dup_y.jpg, DateTaken', self.drive.items['keep_y']['description'])

    def test_execute_group_allows_comma_and_semicolon_in_duplicate_filename(self):
        comma_name = 'Naim, Moinho; 2ºAno, casamento 036.jpg'
        self.drive.photo('dup_comma', comma_name, 'a', 'evento')
        idx = FileIndexer(str(self.db))
        p_dup = SOURCE + '\\' + comma_name
        idx.save_files_in_batch([
            dict(id=p_dup, path=p_dup, name=comma_name, source='local', size=123, mimeType='image/jpeg',
                 description='', modifiedTime=1, createdTime=1, parentId=SOURCE,
                 webContentLink='https://drive.google.com/file/d/dup_comma/view'),
        ], source='local')
        idx.close()

        groups = self.scan(MODE_FOLDER_VS_FOLDER, [SOURCE], [REFERENCE])
        self.assertEqual(1, len(groups))
        result = self.execute(groups)
        self.assertEqual([], result['errors'])
        self.assertIn(comma_name, self.drive.items['keep']['description'])
        self.assertEqual('dst', self.drive.items['dup_comma']['driveId'])
        self.assertEqual(comma_name, self.drive.items['dup_comma']['name'])

    def test_filename_year_between_1997_and_current_has_priority_before_organizar(self):
        from datetime import datetime
        from src.services.hash_deduplicator import _extract_filename_years, _inspect_filename_years

        cur_yr = datetime.now().year
        self.assertEqual({2011}, _extract_filename_years('mb haiti 2011 fotos boas.JPG'))
        self.assertEqual({2017}, _extract_filename_years('mb PAOLO VALLE 20170523d_eBR.mp4'))
        self.assertEqual({1997}, _extract_filename_years('1997-viagem.jpg'))
        self.assertEqual({cur_yr}, _extract_filename_years(f'evento_{cur_yr}.jpg'))
        self.assertEqual(set(), _extract_filename_years('1996_antiga.jpg'))
        self.assertEqual(set(), _extract_filename_years('2099_futuro.jpg'))
        self.assertEqual((set(), {2010}), _inspect_filename_years('fotos até 2010.jpg'))
        self.assertEqual(({2012}, set()), _inspect_filename_years('date_2012.jpg'))

        # Nova regra: raiz mais antiga (1999) SEMPRE permanece, mesmo que outra cópia cite ano no nome.
        c_1999 = dict(path=ROOT + r'\1999\Evento\IMG_001.jpg', name='IMG_001.jpg', description='')
        c_2017 = dict(path=ROOT + r'\2017\Outro\mb 20170523d_eBR.jpg', name='mb 20170523d_eBR.jpg', description='')
        chosen, _ = select_keeper_and_duplicates([c_2017, c_1999], [ROOT])
        self.assertEqual(c_1999['path'], chosen['path'])

        c_org = dict(path=ROOT + r'\Organizar\2011_haiti.jpg', name='2011_haiti.jpg', description='')
        c_out = dict(path=ROOT + r'\1999\foto.jpg', name='foto.jpg', description='')
        chosen3, _ = select_keeper_and_duplicates([c_org, c_out], [ROOT])
        self.assertEqual(c_out['path'], chosen3['path'])

    def test_same_name_copy_number_priority_after_year_organizar_and_depth(self):
        from src.services.hash_deduplicator import split_copy_name

        self.assertEqual(('foto.jpg', (0, ())), split_copy_name('foto.jpg'))
        self.assertEqual(('foto.jpg', (1, (2,))), split_copy_name('foto (2).JPG'))
        self.assertEqual(('foto.jpg', (1, (10,))), split_copy_name('foto (10).jpg'))
        self.assertEqual(('foto.jpg', (2, (1, 1))), split_copy_name('foto (1) (1).jpg'))
        self.assertEqual(('missas', (1, (2,))), split_copy_name('Missas (2)'))
        self.assertEqual(('natal (2020).jpg', (0, ())), split_copy_name('Natal (2020).jpg'))
        self.assertEqual(('natal (2020).jpg', (1, (2,))), split_copy_name('Natal (2020) (2).jpg'))
        self.assertEqual(('a(s) 11.02.28.jpg', (0, ())), split_copy_name('a(s) 11.02.28.jpg'))

        # Mesmo ano-raiz, mesmo não-Organizar e mesma profundidade, e mesmo nome-base:
        # sem (n) vence mesmo tendo menos tags, caminho mais longo e data mais recente.
        c_clean = dict(
            path=ROOT + r'\2010\EventoComNomeMuitoLongo\foto.jpg',
            name='foto.jpg',
            description='',
            drive_created='2025-01-01T00:00:00Z',
        )
        c_copy2 = dict(
            path=ROOT + r'\2010\A\foto (2).jpg',
            name='foto (2).jpg',
            description='a, b, c',
            drive_created='2010-01-01T00:00:00Z',
        )
        c_copy10 = dict(
            path=ROOT + r'\2010\B\foto (10).jpg',
            name='foto (10).jpg',
            description='a, b, c, d',
            drive_created='2009-01-01T00:00:00Z',
        )
        chosen, dups = select_keeper_and_duplicates([c_copy10, c_copy2, c_clean], [ROOT])
        self.assertEqual(c_clean['path'], chosen['path'])
        self.assertEqual([c_copy2['path'], c_copy10['path']], [d['path'] for d in dups])

        # Entre apenas cópias (n) no mesmo nível, menor número vence inclusive contra sufixo encadeado (1) (1).
        c_single1 = dict(path=ROOT + r'\2010\A\foto (1).jpg', name='foto (1).jpg', description='')
        c_chain = dict(path=ROOT + r'\2010\B\foto (1) (1).jpg', name='foto (1) (1).jpg', description='tag')
        chosen_n, _ = select_keeper_and_duplicates([c_chain, c_copy2, c_single1], [ROOT])
        self.assertEqual(c_single1['path'], chosen_n['path'])

        # Pasta raiz mais antiga continua vencendo.
        c_older_copy = dict(path=ROOT + r'\2009\Evento\foto (2).jpg', name='foto (2).jpg', description='')
        c_newer_clean = dict(path=ROOT + r'\2010\Evento\foto.jpg', name='foto.jpg', description='a, b')
        chosen_year, _ = select_keeper_and_duplicates([c_newer_clean, c_older_copy], [ROOT])
        self.assertEqual(c_older_copy['path'], chosen_year['path'])

        # Não-Organizar continua vencendo Organizar mesmo que a cópia fora de Organizar tenha (2).
        c_non_org_copy = dict(path=ROOT + r'\2010\Evento\foto (2).jpg', name='foto (2).jpg', description='')
        c_org_clean = dict(path=ROOT + r'\2010\Organizar\foto.jpg', name='foto.jpg', description='tag')
        chosen_org, _ = select_keeper_and_duplicates([c_org_clean, c_non_org_copy], [ROOT])
        self.assertEqual(c_non_org_copy['path'], chosen_org['path'])

        # Menor profundidade continua vencendo subpasta mais funda mesmo tendo (2).
        c_shallow_copy = dict(path=ROOT + r'\2010\Evento\foto (2).jpg', name='foto (2).jpg', description='')
        c_deep_clean = dict(path=ROOT + r'\2010\Evento\Sub\foto.jpg', name='foto.jpg', description='tag1, tag2')
        chosen_depth, _ = select_keeper_and_duplicates([c_deep_clean, c_shallow_copy], [ROOT])
        self.assertEqual(c_shallow_copy['path'], chosen_depth['path'])

        # Nomes-base distintos no mesmo nível comparam por mais tags antes do caminho/data.
        c_tagged_copy = dict(path=ROOT + r'\2010\Evento\Casamento Maria (2).jpg', name='Casamento Maria (2).jpg', description='t1, t2')
        c_untagged_other = dict(path=ROOT + r'\2010\A\IMG_001.jpg', name='IMG_001.jpg', description='')
        chosen_diff, _ = select_keeper_and_duplicates([c_untagged_other, c_tagged_copy], [ROOT])
        self.assertEqual(c_tagged_copy['path'], chosen_diff['path'])

    def test_scan_selected_folders_prefers_name_without_copy_number(self):
        self.drive.folder('y2010', '2010', 'src', 'src')
        self.drive.folder('y2010_ev_long', 'EventoComNomeLongo', 'y2010', 'src')
        self.drive.folder('y2010_ev_short', 'A', 'y2010', 'src')
        self.drive.photo('orig_id', 'festa.jpg', 'y2010_ev_long', 'tag1')
        self.drive.photo('copy_id', 'festa (2).jpg', 'y2010_ev_short', 'tag1, tag2, tag3')
        idx = FileIndexer(str(self.db))
        p_orig = ROOT + r'\2010\EventoComNomeLongo\festa.jpg'
        p_copy = ROOT + r'\2010\A\festa (2).jpg'
        idx.save_files_in_batch([
            dict(id=p_orig, path=p_orig, name='festa.jpg', source='local', size=123, mimeType='image/jpeg',
                 description='', modifiedTime=1, createdTime=1, parentId=os.path.dirname(p_orig),
                 webContentLink='https://drive.google.com/file/d/orig_id/view'),
            dict(id=p_copy, path=p_copy, name='festa (2).jpg', source='local', size=123, mimeType='image/jpeg',
                 description='', modifiedTime=1, createdTime=1, parentId=os.path.dirname(p_copy),
                 webContentLink='https://drive.google.com/file/d/copy_id/view'),
        ], source='local')
        idx.close()

        groups = self.scan(MODE_SELECTED_FOLDERS, [ROOT + r'\2010\EventoComNomeLongo', ROOT + r'\2010\A'], [])
        grp = [g for g in groups if {g.keeper['drive_id'], *(d['drive_id'] for d in g.duplicates)} == {'orig_id', 'copy_id'}][0]
        self.assertEqual('orig_id', grp.keeper['drive_id'])
        self.assertEqual(['copy_id'], [d['drive_id'] for d in grp.duplicates])


class HashDedupDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PyQt6.QtWidgets import QMainWindow
        from unittest.mock import Mock
        from src.ui.hash_dedup_dialog import HashDedupDialog
        self.fixture = HashDedupTests()
        self.fixture.setUp()
        self.config_patch = patch('src.ui.hash_dedup_dialog.ConfigManager', return_value=self.fixture.cfg)
        self.config_patch.start()
        self.cache_patch = patch('src.ui.hash_dedup_dialog.ThumbnailCache.get_existing_thumbnail_cache_path',
                                 return_value=str(Path(self.fixture.tmp.name) / 'missing-cache.png'))
        self.cache_patch.start()
        self.win = QMainWindow()
        self.win.begin_maintenance_operation = Mock(return_value=True)
        self.win.end_maintenance_operation = Mock()
        self.dialog = HashDedupDialog(self.win, indexer=type('Index', (), {'db_name': str(self.fixture.db)})(),
                                     drive_service=self.fixture.drive)
        self.dialog.custom_list.addItems([SOURCE, REFERENCE])
        self.dialog.show()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.win.deleteLater()
        self.app.processEvents()
        self.cache_patch.stop()
        self.config_patch.stop()
        self.fixture.tearDown()

    def wait(self):
        from PyQt6.QtCore import QEventLoop, QTimer
        loop = QEventLoop()
        timer, deadline = QTimer(), QTimer()
        timer.timeout.connect(lambda: loop.quit() if not self.dialog._busy else None)
        timer.start(1)
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        deadline.start(5000)
        loop.exec()
        timer.stop()
        deadline.stop()
        self.assertFalse(self.dialog._busy)

    def test_scan_defaults_to_selected_and_marks_groups_without_executing(self):
        self.assertTrue(self.dialog.radio_selected.isChecked())
        self.dialog._start_scan()
        self.assertTrue(self.dialog.btn_cancel_op.isEnabled())
        self.assertFalse(self.dialog.custom_list.isEnabled())
        self.wait()
        self.assertEqual(1, len(self.dialog.groups))
        self.assertTrue(self.dialog.btn_execute.isEnabled())
        self.assertTrue(self.dialog.groups[0].checked)
        self.assertEqual([], self.fixture.drive.writes)
        self.win.begin_maintenance_operation.assert_called_once()
        self.win.end_maintenance_operation.assert_called_once()

    def test_scope_change_invalidates_previous_plan(self):
        self.dialog._start_scan()
        self.wait()
        self.dialog._set_all_checked(True)
        self.dialog.custom_list.addItem(ROOT + r'\Outra')
        self.assertTrue(self.dialog.groups)
        self.assertTrue(self.dialog._plan_stale)
        self.assertFalse(self.dialog.btn_execute.isEnabled())

    def test_read_only_allows_scan_but_never_execution(self):
        self.fixture.cfg.readonly = True
        self.dialog._start_scan()
        self.wait()
        self.dialog._set_all_checked(True)
        self.assertFalse(self.dialog.btn_execute.isEnabled())
        self.assertFalse(self.dialog.btn_recover.isEnabled())

    def test_manual_swap_selects_group_and_preserves_scope(self):
        self.dialog._start_scan()
        self.wait()
        self.dialog._set_all_checked(False)
        self.dialog.table.selectRow(0)
        self.dialog._swap_selected_keeper()
        self.wait()
        self.assertEqual('dup', self.dialog.groups[0].keeper['drive_id'])
        self.assertTrue(self.dialog.groups[0].checked)
        self.assertEqual([], self.fixture.drive.writes)

    def test_folder_list_enabled_in_both_folder_modes(self):
        self.dialog.radio_all.setChecked(True)
        self.assertFalse(self.dialog.custom_list.isEnabled())
        self.dialog.radio_custom.setChecked(True)
        self.assertTrue(self.dialog.custom_list.isEnabled())
        self.dialog.radio_selected.setChecked(True)
        self.assertTrue(self.dialog.custom_list.isEnabled())

    def test_edit_tags_survives_keeper_swap_and_can_reset(self):
        from PyQt6.QtCore import Qt
        self.dialog._start_scan()
        self.wait()
        self.dialog.table.item(0, 5).setText('minhas tags')
        self.assertEqual('minhas tags', self.dialog.groups[0].edited_description)
        self.assertFalse(self.dialog.table.item(0, 2).flags() & Qt.ItemFlag.ItemIsEditable)
        self.assertTrue(self.dialog.table.item(0, 5).flags() & Qt.ItemFlag.ItemIsEditable)
        self.dialog.table.selectRow(0)
        self.dialog._swap_selected_keeper()
        self.wait()
        self.assertEqual('minhas tags', self.dialog.table.item(0, 5).text())
        self.dialog.table.selectRow(0)
        self.dialog._reset_selected_tags()
        self.wait()
        self.assertIsNone(self.dialog.groups[0].edited_description)
        self.assertEqual(self.dialog.groups[0].merged_description, self.dialog.table.item(0, 5).text())

    def test_warning_result_does_not_claim_success(self):
        with patch('src.ui.hash_dedup_dialog.QMessageBox.warning') as warn, \
                patch('src.ui.hash_dedup_dialog.QMessageBox.information') as info:
            self.dialog._on_execute_finished(dict(errors=['ETag ausente'], files_moved=0, tags_updated=0))
        warn.assert_called_once()
        info.assert_not_called()
        self.assertIn('interrompida', warn.call_args.args[1])
        self.wait()


if __name__ == '__main__':
    unittest.main()
