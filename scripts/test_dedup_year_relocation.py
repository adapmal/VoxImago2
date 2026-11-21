"""Regras de prioridade anual, discrepâncias (DateTaken / nome / até 2010) e recuperação."""
import os
import unittest
from unittest.mock import patch

from scripts import test_hash_dedup as fixtures
from scripts.test_hash_dedup import ROOT, SOURCE, REFERENCE
from src.database.database import FileIndexer, to_canonical_id
from src.services.hash_deduplicator import (
    DedupJournal,
    MODE_SELECTED_FOLDERS,
    evaluate_group_discrepancies,
    select_keeper_and_duplicates,
)


class DiscrepancyAndPriorityTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.HashDedupTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.drive = self.fx.drive

    def _register_pair(self, path_a, id_a, path_b, id_b, *, name_a=None, name_b=None,
                       desc_a='base', desc_b='herdada', exif_a=None, exif_b=None):
        name_a = name_a or os.path.basename(path_a)
        name_b = name_b or os.path.basename(path_b)
        parts_a = [p for p in os.path.relpath(path_a, ROOT).split('\\')[:-1]]
        parts_b = [p for p in os.path.relpath(path_b, ROOT).split('\\')[:-1]]

        def ensure_dir(parts):
            parent = 'src'
            for part in parts:
                existing = [
                    fid for fid, item in self.drive.items.items()
                    if item.get('mimeType') == 'application/vnd.google-apps.folder'
                    and item.get('name', '').casefold() == part.casefold()
                    and parent in item.get('parents', [])
                ]
                if existing:
                    parent = existing[0]
                else:
                    fid = f'dir_{parent}_{part}'
                    self.drive.folder(fid, part, parent, 'src')
                    parent = fid
            return parent

        pa = ensure_dir(parts_a)
        pb = ensure_dir(parts_b)
        self.drive.photo(id_a, name_a, pa, desc_a)
        self.drive.photo(id_b, name_b, pb, desc_b)
        if exif_a:
            self.drive.items[id_a]['imageMediaMetadata'] = {'time': exif_a}
        if exif_b:
            self.drive.items[id_b]['imageMediaMetadata'] = {'time': exif_b}

        idx = FileIndexer(str(self.fx.db))
        try:
            idx.save_files_in_batch([
                dict(id=path_a, path=path_a, name=name_a, source='local', size=123, mimeType='image/jpeg',
                     description=desc_a, modifiedTime=1, createdTime=1, parentId=os.path.dirname(path_a),
                     webContentLink=f'https://drive.google.com/file/d/{id_a}/view'),
                dict(id=path_b, path=path_b, name=name_b, source='local', size=123, mimeType='image/jpeg',
                     description=desc_b, modifiedTime=1, createdTime=1, parentId=os.path.dirname(path_b),
                     webContentLink=f'https://drive.google.com/file/d/{id_b}/view'),
            ], source='local')
        finally:
            idx.close()

    def test_oldest_root_year_wins_even_in_nested_subfolder_or_organizar_without_moving(self):
        p_old = ROOT + r'\2004\Organizar\Sub\foto.jpg'
        p_new = ROOT + r'\2012\Evento\foto_2012_detalhada.jpg'
        self._register_pair(p_old, 'old_k', p_new, 'new_d', exif_a='2012:05:10 12:00:00', exif_b='2012:05:10 12:00:00')

        groups = self.fx.scan(MODE_SELECTED_FOLDERS, [ROOT + r'\2004', ROOT + r'\2012'], [])
        grp = groups[0]
        self.assertEqual('old_k', grp.keeper['drive_id'])
        self.assertEqual('', grp.relocation_path)
        self.assertTrue(grp.checked)
        self.assertEqual('', grp.plan_error)
        self.assertTrue(grp.has_discrepancy)
        self.assertIn('DateTaken', grp.merged_description)
        self.assertIn('discrepância', grp.merged_description)

        orig_parents = list(self.drive.items['old_k']['parents'])
        result = self.fx.execute([grp])
        self.assertEqual([], result['errors'])
        self.assertEqual(0, result.get('keepers_relocated', 0))
        self.assertEqual(1, result['files_moved'])
        self.assertEqual(orig_parents, self.drive.items['old_k']['parents'])
        self.assertIn('DateTaken', self.drive.items['old_k']['description'])
        self.assertIn('discrepância', self.drive.items['old_k']['description'])
        self.assertEqual('dst', self.drive.items['new_d']['driveId'])

    def test_non_year_folder_always_loses_to_year_root_folder(self):
        p_year = ROOT + r'\2018\Organizar\Profunda\foto.jpg'
        p_org = ROOT + r'\Avulsas\foto.jpg'
        chosen, _ = select_keeper_and_duplicates(
            [dict(path=p_org, name='foto.jpg', description='tag1, tag2'),
             dict(path=p_year, name='foto.jpg', description='')],
            [ROOT],
        )
        self.assertEqual(p_year, chosen['path'])

    def test_datetaken_matching_root_year_has_no_discrepancy_and_is_not_red(self):
        p_a = ROOT + r'\2008\Brasil\festa_2008.jpg'
        p_b = ROOT + r'\2014\Outras\festa_2008.jpg'
        self._register_pair(p_a, 'k08', p_b, 'd14', exif_a='2008:06:15 09:00:00', exif_b='2008:06:15 09:00:00')

        groups = self.fx.scan(MODE_SELECTED_FOLDERS, [ROOT + r'\2008', ROOT + r'\2014'], [])
        grp = groups[0]
        self.assertEqual('k08', grp.keeper['drive_id'])
        self.assertFalse(grp.has_discrepancy)
        self.assertNotIn('DateTaken', grp.merged_description)
        self.assertNotIn('discrepância', grp.merged_description)

    def test_ate_2010_notation_allows_folders_up_to_2010_and_flags_later_folders(self):
        for ok_year in ('1999', '2005', '2010'):
            keeper = dict(path=ROOT + rf'\{ok_year}\Sub\fotos até 2010.jpg', name='fotos até 2010.jpg')
            dup = dict(path=ROOT + r'\2015\fotos ate 2010.jpg', name='fotos ate 2010.jpg')
            info = evaluate_group_discrepancies(keeper, [keeper, dup], [ROOT])
            self.assertEqual([], info['extra_tags'], f'falhou para {ok_year}')

        late_keeper = dict(path=ROOT + r'\2011\Sub\fotos até 2010.jpg', name='fotos até 2010.jpg')
        info_late = evaluate_group_discrepancies(late_keeper, [late_keeper], [ROOT])
        self.assertEqual(['discrepância'], info_late['extra_tags'])

        # Não confundir palavras terminadas em 'ate' (ex.: 'date_2012', 'chocolate_2012') com 'até 2012'.
        tricky = dict(path=ROOT + r'\2010\date_2012.jpg', name='date_2012.jpg')
        self.assertEqual(['discrepância'], evaluate_group_discrepancies(tricky, [tricky], [ROOT])['extra_tags'])

        # Combinação de 'até 2010' + ano exato divergente no mesmo nome deve acusar discrepância.
        mixed = dict(path=ROOT + r'\2009\fotos até 2010 evento 2015.jpg', name='fotos até 2010 evento 2015.jpg')
        self.assertEqual(['discrepância'], evaluate_group_discrepancies(mixed, [mixed], [ROOT])['extra_tags'])

    def test_duplicate_filename_year_triggers_discrepancy_and_manual_tags_override(self):
        p_a = ROOT + r'\2005\Brasil\img.jpg'
        p_b = ROOT + r'\2005\Organizar\img_2013.jpg'
        self._register_pair(p_a, 'k05', p_b, 'd05')

        groups = self.fx.scan(MODE_SELECTED_FOLDERS, [ROOT + r'\2005\Brasil', ROOT + r'\2005\Organizar'], [])
        grp = groups[0]
        self.assertTrue(grp.has_discrepancy)
        self.assertIn('discrepância', grp.merged_description)
        self.assertNotIn('DateTaken', grp.merged_description)

        grp.edited_description = 'tags manuais limpas'
        result = self.fx.execute([grp])
        self.assertEqual([], result['errors'])
        self.assertEqual('tags manuais limpas', self.drive.items['k05']['description'])

    def test_legacy_keeper_relocation_recovery_reconciles_without_repeating_move(self):
        # Garante compatibilidade de recuperação para entradas históricas no journal.db.
        self.drive.folder('y2017', '2017', 'src', 'src')
        self.drive.folder('org17', 'Organizar', 'y2017', 'src')
        self.drive.items['keep']['parents'] = ['org17']
        self.drive.items['keep']['description'] = 'base'
        old_path = self.fx.drive_path if hasattr(self.fx, 'drive_path') else REFERENCE + r'\keep.jpg'
        target_path = ROOT + r'\2017\Organizar\keep.jpg'

        journal = DedupJournal(str(self.fx.db))
        payload = dict(
            kind='keeper_relocation',
            file_id='keep',
            local_id=old_path,
            origin_path=old_path,
            original_parents=['b'],
            original_name='keep.jpg',
            destination_parent='org17',
            destination_drive='src',
            destination_name='keep.jpg',
            destination_path=target_path,
            md5=self.drive.items['keep']['md5Checksum'],
            size=int(self.drive.items['keep']['size']),
            description='base',
        )
        journal.begin_move('keep', payload)
        journal.close()

        writes_before = len(self.drive.writes)
        res = self.fx.execute([], recover=True)
        self.assertEqual(1, res['reconciled'])
        self.assertEqual(writes_before, len(self.drive.writes))

        idx = FileIndexer(str(self.fx.db))
        try:
            row = idx.conn.execute('SELECT path FROM files WHERE file_id=?',
                                   (to_canonical_id(target_path),)).fetchone()
            self.assertEqual((to_canonical_id(target_path),), tuple(row))
        finally:
            idx.close()

        # Nome divergente no remoto impede rebase automático.
        journal2 = DedupJournal(str(self.fx.db))
        payload2 = dict(payload, file_id='dup', local_id=SOURCE + r'\dup.jpg',
                        destination_path=ROOT + r'\2017\Organizar\dup.jpg', destination_name='dup.jpg')
        journal2.begin_move('dup', payload2)
        journal2.close()
        self.drive.items['dup']['parents'] = ['org17']
        self.drive.items['dup']['name'] = 'renomeado.jpg'
        res2 = self.fx.execute([], recover=True)
        self.assertEqual(0, res2['reconciled'])
        self.assertTrue(res2['errors'])


class ExifSourceValidationTests(unittest.TestCase):
    def test_invalid_earlier_fields_allow_later_source(self):
        from src.services.hash_deduplicator import _extract_exif_year
        for invalid in ('mascarado', '0000:00:00 00:00:00', '1899:01:01', '2101:01:01',
                        '2001:02:29', '2002:13:01', '2002:01:01 99:00:00', '2002:xx:01'):
            with self.subTest(invalid=invalid):
                self.assertEqual(2017, _extract_exif_year(dict(DateTimeOriginal=invalid,
                                  exif_date_taken='inválido', DateTimeDigitized='2017:05:23 12:30:00')))

    def test_valid_sources_keep_priority_and_allow_boundaries(self):
        from src.services.hash_deduplicator import _extract_exif_year
        keys = ['DateTimeOriginal', 'exif_date_taken', 'DateTimeDigitized', 'DateTime']
        item = dict(zip(keys, ('1999:01:01', '2000-01-01T12:00:00Z', '2001/01/01', '2100:01:01')))
        item['imageMediaMetadata'] = {'time': '1900:01:01 12:00:00'}
        for key, expected in zip(keys, (1999, 2000, 2001, 2100)):
            self.assertEqual(expected, _extract_exif_year(item))
            item[key] = 'inválido'
        self.assertEqual(1900, _extract_exif_year(item))

    def test_no_valid_date_returns_none_without_reading_original(self):
        from src.services.hash_deduplicator import _extract_exif_year
        with patch('builtins.open', side_effect=AssertionError('No originals')):
            self.assertIsNone(_extract_exif_year({'DateTimeOriginal': 'não informado', 'imageMediaMetadata': 'inválido'}))
            self.assertEqual(2000, _extract_exif_year({'DateTimeOriginal': b'2000:02:29 12:00:00\x00'}))


if __name__ == '__main__':
    unittest.main()
