"""Testes para propagação automática RAW ↔ Cópia e tag 'RAW' na fila de edição de tags."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database.database import to_canonical_id
from src.services.batch_tags import (
    build_single_edit_items,
    parse_delimited_tags,
    prepare_batch,
    prepare_batch_with_stats,
    sync_pending_queue_raw_items,
)
from src.services.raw_tag_sync import LocalRawPairFinder, _read_local_exif_signature
from src.utils.staging_schema import StagingItem


class TestQueueAutoRawTagging(unittest.TestCase):
    def _create_catalog(self, tmp_dir: Path, rows: list[tuple[str, str, str, str, str, str]]):
        """rows: (file_id, name, path, mimeType, parentId, description)"""
        db_path = tmp_dir / 'file_index.db'
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            'CREATE TABLE files (file_id TEXT PRIMARY KEY, name TEXT, path TEXT, mimeType TEXT, source TEXT, parentId TEXT, description TEXT)'
        )
        for fid, name, path, mime, parent_id, desc in rows:
            conn.execute(
                'INSERT INTO files VALUES (?,?,?,?,?,?,?)',
                (fid, name, path, mime, 'local', parent_id, desc),
            )
        conn.commit()
        conn.close()
        return db_path

    def test_single_edit_on_raw_adds_raw_tag_and_brings_jpg_counterpart_without_raw_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'Banco de Imagens' / '2025' / '1 - Eventos' / '0419_Batismo'
            raw_dir = root / 'Arquivos RAW Originais'
            jpg_dir = root / 'Outros'
            raw_dir.mkdir(parents=True)
            jpg_dir.mkdir(parents=True)

            raw_path = str(raw_dir / 'DSC00001.ARW')
            jpg_path = str(jpg_dir / 'DSC00001-Enhanced-NR.jpg')
            raw_fid = to_canonical_id(raw_path)
            jpg_fid = to_canonical_id(jpg_path)

            db_path = self._create_catalog(Path(tmp), [
                (to_canonical_id(str(raw_dir)), 'Arquivos RAW Originais', to_canonical_id(str(raw_dir)), 'folder', to_canonical_id(str(root)), ''),
                (to_canonical_id(str(jpg_dir)), 'Outros', to_canonical_id(str(jpg_dir)), 'folder', to_canonical_id(str(root)), ''),
                (raw_fid, 'DSC00001.ARW', raw_fid, 'file', to_canonical_id(str(raw_dir)), '2025, Batismo'),
                (jpg_fid, 'DSC00001-Enhanced-NR.jpg', jpg_fid, 'file', to_canonical_id(str(jpg_dir)), '2025, Batismo, f999.jpeg'),
            ])

            file_item = {'file_id': raw_fid, 'name': 'DSC00001.ARW', 'path': raw_fid, 'description': '2025, Batismo'}
            items, matching_ids, partner_count, final_desc, raw_auto = build_single_edit_items(
                str(db_path),
                file_item,
                '2025, Batismo, Gianpietro',
                [],
            )

            self.assertTrue(raw_auto)
            self.assertEqual(final_desc, '2025, Batismo, Gianpietro, RAW')
            self.assertEqual(partner_count, 1)
            self.assertEqual(len(items), 2)
            by_fid = {it.file_id: it for it in items}
            self.assertEqual(by_fid[raw_fid].new_value, '2025, Batismo, Gianpietro, RAW')
            self.assertEqual(by_fid[jpg_fid].new_value, '2025, Batismo, f999.jpeg, Gianpietro')
            self.assertNotIn('RAW', by_fid[jpg_fid].new_value)

    def test_single_edit_on_jpg_brings_raw_counterpart_with_raw_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev_dir = Path(tmp) / 'Banco de Imagens' / '2025' / '1 - Eventos' / 'Missa'
            ev_dir.mkdir(parents=True)
            raw_path = str(ev_dir / 'IMG_1000.CR2')
            jpg_path = str(ev_dir / 'IMG_1000.JPG')
            raw_fid = to_canonical_id(raw_path)
            jpg_fid = to_canonical_id(jpg_path)

            db_path = self._create_catalog(Path(tmp), [
                (raw_fid, 'IMG_1000.CR2', raw_fid, 'file', to_canonical_id(str(ev_dir)), '2025'),
                (jpg_fid, 'IMG_1000.JPG', jpg_fid, 'file', to_canonical_id(str(ev_dir)), '2025'),
            ])

            file_item = {'file_id': jpg_fid, 'name': 'IMG_1000.JPG', 'path': jpg_fid, 'description': '2025'}
            items, _mids, partner_count, final_desc, raw_auto = build_single_edit_items(
                str(db_path),
                file_item,
                '2025, Missa, Cacilda',
                [],
            )

            self.assertFalse(raw_auto)
            self.assertEqual(final_desc, '2025, Missa, Cacilda')
            self.assertEqual(partner_count, 1)
            by_fid = {it.file_id: it for it in items}
            self.assertEqual(by_fid[jpg_fid].new_value, '2025, Missa, Cacilda')
            self.assertEqual(by_fid[raw_fid].new_value, '2025, Missa, Cacilda, RAW')

    def test_single_multiword_tag_is_not_split_by_spaces_when_active_tags_provided(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev_dir = Path(tmp) / 'Evento'
            ev_dir.mkdir(parents=True)
            raw_path = str(ev_dir / 'IMG_2000.ARW')
            jpg_path = str(ev_dir / 'IMG_2000.JPG')
            raw_fid = to_canonical_id(raw_path)
            jpg_fid = to_canonical_id(jpg_path)

            db_path = self._create_catalog(Path(tmp), [
                (raw_fid, 'IMG_2000.ARW', raw_fid, 'file', to_canonical_id(str(ev_dir)), ''),
                (jpg_fid, 'IMG_2000.JPG', jpg_fid, 'file', to_canonical_id(str(ev_dir)), ''),
            ])

            file_item = {'file_id': jpg_fid, 'name': 'IMG_2000.JPG', 'path': jpg_fid, 'description': ''}
            items, _mids, partner_count, final_desc, _raw_auto = build_single_edit_items(
                str(db_path),
                file_item,
                'São Paulo',
                [],
                active_tags=['São Paulo'],
            )
            by_fid = {it.file_id: it for it in items}
            self.assertEqual(final_desc, 'São Paulo')
            self.assertEqual(by_fid[jpg_fid].new_value, 'São Paulo')
            self.assertEqual(by_fid[raw_fid].new_value, 'São Paulo, RAW')
            self.assertEqual(parse_delimited_tags('Dom Cícero'), ['Dom Cícero'])

    def test_existing_lowercase_raw_tag_does_not_trigger_raw_auto_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev_dir = Path(tmp) / 'Evento'
            ev_dir.mkdir(parents=True)
            raw_path = str(ev_dir / 'IMG_2001.ARW')
            raw_fid = to_canonical_id(raw_path)
            db_path = self._create_catalog(Path(tmp), [
                (raw_fid, 'IMG_2001.ARW', raw_fid, 'file', to_canonical_id(str(ev_dir)), '2025, raw'),
            ])
            file_item = {'file_id': raw_fid, 'name': 'IMG_2001.ARW', 'path': raw_fid, 'description': '2025, raw'}
            items, _mids, partner_count, final_desc, raw_auto = build_single_edit_items(
                str(db_path),
                file_item,
                '2025, raw, Missa',
                [],
                active_tags=['2025', 'raw', 'Missa'],
            )
            self.assertFalse(raw_auto)
            self.assertEqual(final_desc, '2025, raw, Missa')
            self.assertEqual(partner_count, 0)

    def test_protected_or_deleted_counterpart_is_safely_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev_dir = Path(tmp) / 'Evento'
            ev_dir.mkdir(parents=True)
            raw_path = str(ev_dir / 'IMG_3000.ARW')
            jpg_active_path = str(ev_dir / 'IMG_3000.jpg')
            jpg_del_path = str(ev_dir / 'IMG_3000-Enhanced-NR.jpg')
            raw_fid = to_canonical_id(raw_path)
            jpg_active_fid = to_canonical_id(jpg_active_path)
            jpg_del_fid = to_canonical_id(jpg_del_path)

            db_path = self._create_catalog(Path(tmp), [
                (raw_fid, 'IMG_3000.ARW', raw_fid, 'file', to_canonical_id(str(ev_dir)), '2025'),
                (jpg_active_fid, 'IMG_3000.jpg', jpg_active_fid, 'image/jpeg', to_canonical_id(str(ev_dir)), '2025'),
                (jpg_del_fid, 'IMG_3000-Enhanced-NR.jpg', jpg_del_fid, 'image/jpeg', to_canonical_id(str(ev_dir)), '2025'),
            ])

            active_item = StagingItem(
                jpg_active_fid, 'IMG_3000.jpg', jpg_active_fid, 'set_description',
                old_value='2025', new_value='2025, Outra'
            )
            active_item.execution_state = 'waiting_cloud'
            del_item = StagingItem(
                jpg_del_fid, 'IMG_3000-Enhanced-NR.jpg', jpg_del_fid, 'delete',
                old_value='IMG_3000-Enhanced-NR.jpg', new_value='EXCLUIR'
            )
            prior = [active_item, del_item]

            # 1) Edição individual: pula os dois parceiros sem incluí-los em matching_ids nem tocá-los!
            file_item = {'file_id': raw_fid, 'name': 'IMG_3000.ARW', 'path': raw_fid, 'description': '2025'}
            items, matching_ids, partner_count, final_desc, raw_auto = build_single_edit_items(
                str(db_path), file_item, '2025, Missa', prior, active_tags=['2025', 'Missa']
            )
            self.assertEqual(partner_count, 0)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].file_id, raw_fid)
            self.assertNotIn(jpg_active_fid, matching_ids)
            self.assertNotIn(jpg_del_fid, matching_ids)

            # 2) Edição em lote: preserva exatamente o item waiting_cloud e o item delete sem falhar
            records, b_partners, _changed = prepare_batch_with_stats(
                str(db_path),
                [file_item],
                [it.to_dict() for it in prior],
                added=['Missa'],
                removed=set(),
                cancelled=lambda: False,
                progress=lambda _d, _t: None,
            )
            self.assertEqual(b_partners, 0)
            by_op = {r['operation_id']: r for r in records}
            self.assertEqual(by_op[active_item.operation_id], active_item.to_dict())
            self.assertEqual(by_op[del_item.operation_id], del_item.to_dict())

    def test_local_image_jpeg_mimetype_and_sandbox_depth_sibling_raw_folder_are_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Simula estrutura rasa do Sandbox: <tmp>/_TestesBanco/0419_Batismo/Outros + RAW
            ev_dir = Path(tmp) / '0419_Batismo'
            raw_dir = ev_dir / 'RAW'
            outros_dir = ev_dir / 'Outros'
            raw_dir.mkdir(parents=True)
            outros_dir.mkdir(parents=True)

            raw_path = str(raw_dir / 'DSC00100.ARW')
            dng_path = str(raw_dir / 'DSC00100-Enhanced-NR.dng')
            jpg_path = str(outros_dir / 'DSC00100-Enhanced-NR.jpg')
            raw_fid = to_canonical_id(raw_path)
            dng_fid = to_canonical_id(dng_path)
            jpg_fid = to_canonical_id(jpg_path)

            db_path = self._create_catalog(Path(tmp), [
                (to_canonical_id(str(raw_dir)), 'RAW', to_canonical_id(str(raw_dir)), 'folder', to_canonical_id(str(ev_dir)), ''),
                (to_canonical_id(str(outros_dir)), 'Outros', to_canonical_id(str(outros_dir)), 'folder', to_canonical_id(str(ev_dir)), ''),
                (raw_fid, 'DSC00100.ARW', raw_fid, 'file', to_canonical_id(str(raw_dir)), '2025'),
                (dng_fid, 'DSC00100-Enhanced-NR.dng', dng_fid, 'file', to_canonical_id(str(raw_dir)), '2025'),
                # mimeType='image/jpeg' (como os 19 arquivos locais reais em file_index.db!)
                (jpg_fid, 'DSC00100-Enhanced-NR.jpg', jpg_fid, 'image/jpeg', to_canonical_id(str(outros_dir)), '2025'),
            ])

            conn = sqlite3.connect(str(db_path))
            try:
                finder = LocalRawPairFinder(conn)
                # JPG em subpasta temática rasa acha BOTH .ARW e .dng em RAW/
                cps_from_jpg = {c['file_id'] for c in finder.find_counterparts(jpg_fid, 'DSC00100-Enhanced-NR.jpg', jpg_fid)}
                self.assertEqual(cps_from_jpg, {raw_fid, dng_fid})
                # Editar .ARW acha BOTH .dng derivado e .jpg!
                cps_from_arw = {c['file_id'] for c in finder.find_counterparts(raw_fid, 'DSC00100.ARW', raw_fid)}
                self.assertEqual(cps_from_arw, {dng_fid, jpg_fid})
            finally:
                conn.close()

    def test_prepare_batch_does_not_stage_untouched_raw_counterpart_when_jpg_did_not_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev_dir = Path(tmp) / 'Evento'
            ev_dir.mkdir(parents=True)
            raw_path = str(ev_dir / 'IMG_4000.ARW')
            jpg_path = str(ev_dir / 'IMG_4000.JPG')
            raw_fid = to_canonical_id(raw_path)
            jpg_fid = to_canonical_id(jpg_path)

            db_path = self._create_catalog(Path(tmp), [
                (raw_fid, 'IMG_4000.ARW', raw_fid, 'file', to_canonical_id(str(ev_dir)), '2025, Missa'),
                (jpg_fid, 'IMG_4000.JPG', jpg_fid, 'file', to_canonical_id(str(ev_dir)), '2025, Missa'),
            ])

            # Remover tag inexistente no JPG: nenhum dos dois deve entrar na fila!
            records, partners, changed = prepare_batch_with_stats(
                str(db_path),
                [{'file_id': jpg_fid, 'name': 'IMG_4000.JPG', 'path': jpg_fid}],
                [],
                added=[],
                removed={'tag_inexistente'},
                cancelled=lambda: False,
                progress=lambda _d, _t: None,
            )
            self.assertEqual(records, [])
            self.assertEqual(partners, 0)
            self.assertEqual(changed, 0)

    def test_excluded_drive_letter_is_never_statted_by_exif_reader(self):
        with patch('os.stat', side_effect=AssertionError('Nunca deve dar stat em unidade excluída!')):
            dt, cam = _read_local_exif_signature(r'O:\Drives Compartilhados\Banco\foto.jpg', excluded_drives={'O:'})
        self.assertEqual((dt, cam), ('', ''))

    def test_prepare_batch_adds_raw_tag_to_raws_and_stages_counterparts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev_dir = Path(tmp) / 'Banco de Imagens' / '2026' / '1 - Eventos' / 'Votos'
            jpg_sub = ev_dir / 'JPG'
            jpg_sub.mkdir(parents=True)

            raw_path = str(ev_dir / 'A6407836.ARW')
            jpg_path = str(jpg_sub / 'a6407836.jpg')
            solo_raw_path = str(ev_dir / 'A6407837.ARW')
            raw_fid = to_canonical_id(raw_path)
            jpg_fid = to_canonical_id(jpg_path)
            solo_fid = to_canonical_id(solo_raw_path)

            db_path = self._create_catalog(Path(tmp), [
                (to_canonical_id(str(jpg_sub)), 'JPG', to_canonical_id(str(jpg_sub)), 'folder', to_canonical_id(str(ev_dir)), ''),
                (raw_fid, 'A6407836.ARW', raw_fid, 'file', to_canonical_id(str(ev_dir)), '2026'),
                (jpg_fid, 'a6407836.jpg', jpg_fid, 'file', to_canonical_id(str(jpg_sub)), '2026, &boa'),
                (solo_fid, 'A6407837.ARW', solo_fid, 'file', to_canonical_id(str(ev_dir)), '2026'),
            ])

            selected = [
                {'file_id': raw_fid, 'name': 'A6407836.ARW', 'path': raw_fid},
                {'file_id': solo_fid, 'name': 'A6407837.ARW', 'path': solo_fid},
            ]
            records = prepare_batch(
                str(db_path),
                selected,
                [],
                added=['Votos', 'HélioFerreira'],
                removed=set(),
                cancelled=lambda: False,
                progress=lambda _d, _t: None,
            )
            by_fid = {r['file_id']: r for r in records}
            self.assertEqual(set(by_fid.keys()), {raw_fid, solo_fid, jpg_fid})
            self.assertEqual(by_fid[raw_fid]['new_value'], '2026, Votos, HélioFerreira, RAW')
            self.assertEqual(by_fid[solo_fid]['new_value'], '2026, Votos, HélioFerreira, RAW')
            self.assertEqual(by_fid[jpg_fid]['new_value'], '2026, &boa, Votos, HélioFerreira')

    def test_cross_event_same_filename_never_pairs_in_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            cat_dir = Path(tmp) / 'Banco de Imagens' / '2023' / '1 - Eventos'
            ev_a = cat_dir / '0714_Missa'
            ev_b = cat_dir / '1118_DiaPobres'
            ev_a.mkdir(parents=True)
            ev_b.mkdir(parents=True)

            raw_path = str(ev_b / 'IMG_8344.CR2')
            jpg_path = str(ev_a / 'IMG_8344.jpg')
            raw_fid = to_canonical_id(raw_path)
            jpg_fid = to_canonical_id(jpg_path)

            db_path = self._create_catalog(Path(tmp), [
                (to_canonical_id(str(ev_a)), '0714_Missa', to_canonical_id(str(ev_a)), 'folder', to_canonical_id(str(cat_dir)), ''),
                (to_canonical_id(str(ev_b)), '1118_DiaPobres', to_canonical_id(str(ev_b)), 'folder', to_canonical_id(str(cat_dir)), ''),
                (raw_fid, 'IMG_8344.CR2', raw_fid, 'file', to_canonical_id(str(ev_b)), 'DiaDosPobres'),
                (jpg_fid, 'IMG_8344.jpg', jpg_fid, 'file', to_canonical_id(str(ev_a)), 'Missa'),
            ])

            conn = sqlite3.connect(str(db_path))
            try:
                finder = LocalRawPairFinder(conn)
                self.assertEqual(finder.find_counterparts(raw_fid, 'IMG_8344.CR2', raw_fid), [])
                self.assertEqual(finder.find_counterparts(jpg_fid, 'IMG_8344.jpg', jpg_fid), [])
            finally:
                conn.close()

    def test_staging_queue_replace_descriptions_and_add_batch_tags_invalidate_tag_index(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        from src.ui.staging_queue import StagingQueue

        self.__class__._qt_app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            prev_cwd, prev_instance = os.getcwd(), StagingQueue._instance
            os.chdir(tmp)
            StagingQueue._instance = None
            queue = None
            try:
                ev_dir = Path(tmp) / 'Evento'
                ev_dir.mkdir(parents=True)
                raw_path = str(ev_dir / 'DSC09000.ARW')
                jpg_path = str(ev_dir / 'DSC09000.jpg')
                raw_fid = to_canonical_id(raw_path)
                jpg_fid = to_canonical_id(jpg_path)

                db_path = self._create_catalog(Path(tmp), [
                    (raw_fid, 'DSC09000.ARW', raw_fid, 'file', to_canonical_id(str(ev_dir)), '2026'),
                    (jpg_fid, 'DSC09000.jpg', jpg_fid, 'file', to_canonical_id(str(ev_dir)), '2026'),
                ])

                queue = StagingQueue()
                # Popula _tag_index inicialmente
                jpg_item = {'file_id': jpg_fid, 'name': 'DSC09000.jpg', 'path': jpg_fid, 'description': '2026'}
                raw_item = {'file_id': raw_fid, 'name': 'DSC09000.ARW', 'path': raw_fid, 'description': '2026'}
                self.assertEqual(queue.effective_description(jpg_item), '2026')

                # add_batch_tags via BatchEditorDialog preserva tag composta com espaço e traz o par JPG
                count = queue.add_batch_tags([raw_item], tags_to_add='Dom Cícero', db_path=str(db_path))
                self.assertEqual(count, 2)
                self.assertEqual(queue.effective_description(raw_item), '2026, Dom Cícero, RAW')
                self.assertEqual(queue.effective_description(jpg_item), '2026, Dom Cícero')

                # Edição individual subsequente atualiza ambos e mantém _tag_index coerente
                items, matching_ids, partner_count, final_desc, raw_auto = build_single_edit_items(
                    str(db_path), raw_item, '2026, Dom Cícero, Missa, RAW', queue.items,
                    active_tags=['2026', 'Dom Cícero', 'Missa', 'RAW'],
                )
                self.assertFalse(raw_auto)
                self.assertEqual(partner_count, 1)
                self.assertTrue(queue.replace_descriptions(items, matching_ids))
                self.assertEqual(queue.effective_description(raw_item), '2026, Dom Cícero, Missa, RAW')
                self.assertEqual(queue.effective_description(jpg_item), '2026, Dom Cícero, Missa')
            finally:
                if queue:
                    queue.timer.stop()
                StagingQueue._instance = prev_instance
                os.chdir(prev_cwd)

    def test_sync_pending_queue_raw_items_adds_raw_tag_pulls_missing_partners_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev_dir = Path(tmp) / 'Evento'
            raw_dir = ev_dir / 'RAW'
            jpg_dir = ev_dir / 'JPG'
            raw_dir.mkdir(parents=True)
            jpg_dir.mkdir(parents=True)

            # 1) RAW já na fila sem tag 'RAW' e cujo JPG está FORA da fila
            r1_path = str(raw_dir / 'DSC00010.ARW')
            j1_path = str(jpg_dir / 'DSC00010.jpg')
            # 2) Par onde AMBOS já estão na fila com tags diferentes
            r2_path = str(raw_dir / 'DSC00020.ARW')
            j2_path = str(jpg_dir / 'DSC00020.jpg')
            # 3) RAW solitário já na fila (sem JPG)
            r3_path = str(raw_dir / 'DSC00030.ARW')
            # 4) Item protegido (waiting_cloud) que NÃO deve ser tocado
            j4_path = str(jpg_dir / 'DSC00040.jpg')
            r4_path = str(raw_dir / 'DSC00040.ARW')

            r1, j1 = to_canonical_id(r1_path), to_canonical_id(j1_path)
            r2, j2 = to_canonical_id(r2_path), to_canonical_id(j2_path)
            r3 = to_canonical_id(r3_path)
            r4, j4 = to_canonical_id(r4_path), to_canonical_id(j4_path)

            db_path = self._create_catalog(Path(tmp), [
                (to_canonical_id(str(raw_dir)), 'RAW', to_canonical_id(str(raw_dir)), 'folder', to_canonical_id(str(ev_dir)), ''),
                (to_canonical_id(str(jpg_dir)), 'JPG', to_canonical_id(str(jpg_dir)), 'folder', to_canonical_id(str(ev_dir)), ''),
                (r1, 'DSC00010.ARW', r1, 'file', to_canonical_id(str(raw_dir)), '2026'),
                (j1, 'DSC00010.jpg', j1, 'file', to_canonical_id(str(jpg_dir)), '2026, &boa, old_dup.jpg'),
                (r2, 'DSC00020.ARW', r2, 'file', to_canonical_id(str(raw_dir)), '2026'),
                (j2, 'DSC00020.jpg', j2, 'file', to_canonical_id(str(jpg_dir)), '2026'),
                (r3, 'DSC00030.ARW', r3, 'file', to_canonical_id(str(raw_dir)), '2026'),
                (r4, 'DSC00040.ARW', r4, 'file', to_canonical_id(str(raw_dir)), '2026'),
                (j4, 'DSC00040.jpg', j4, 'file', to_canonical_id(str(jpg_dir)), '2026'),
            ])

            active_j4 = StagingItem(j4, 'DSC00040.jpg', j4, 'set_description', old_value='2026', new_value='2026, Ativo')
            active_j4.execution_state = 'waiting_cloud'
            prior = [
                StagingItem(r1, 'DSC00010.ARW', r1, 'set_description', old_value='2026', new_value='2026, Gianpietro'),
                StagingItem(r2, 'DSC00020.ARW', r2, 'set_description', old_value='2026', new_value='2026, Missa'),
                StagingItem(j2, 'DSC00020.jpg', j2, 'set_description', old_value='2026', new_value='2026, Cacilda'),
                StagingItem(r3, 'DSC00030.ARW', r3, 'set_description', old_value='2026', new_value='2026, Votos'),
                StagingItem(r4, 'DSC00040.ARW', r4, 'set_description', old_value='2026', new_value='2026, Teste'),
                active_j4,
            ]

            records, stats = sync_pending_queue_raw_items(str(db_path), [it.to_dict() for it in prior])
            by_fid = {r['file_id']: r for r in records}

            # r1 ganha '&boa' (do j1) + 'RAW' (e NÃO copia 'old_dup.jpg')!
            self.assertEqual(by_fid[r1]['new_value'], '2026, Gianpietro, &boa, RAW')
            # j1 foi trazido para a fila com 'Gianpietro', preservando '&boa' e 'old_dup.jpg', SEM 'RAW'!
            self.assertEqual(by_fid[j1]['new_value'], '2026, &boa, old_dup.jpg, Gianpietro')
            # r2 e j2 (ambos já na fila) somaram 'Missa' + 'Cacilda' (com 'RAW' só no r2)!
            self.assertEqual(by_fid[r2]['new_value'], '2026, Missa, Cacilda, RAW')
            self.assertEqual(by_fid[j2]['new_value'], '2026, Cacilda, Missa')
            # r3 solitário ganhou 'RAW'!
            self.assertEqual(by_fid[r3]['new_value'], '2026, Votos, RAW')
            # r4 ganhou 'RAW', mas j4 (waiting_cloud) ficou 100% intocado!
            self.assertEqual(by_fid[r4]['new_value'], '2026, Teste, RAW')
            self.assertEqual(by_fid[j4], active_j4.to_dict())
            self.assertEqual(stats['partners_added'], 1)
            self.assertEqual(stats['raw_tags_added'], 4)
            self.assertEqual(stats['skipped_protected'], 1)

            # Segunda execução deve ser 100% idempotente (0 alterações!)
            records2, stats2 = sync_pending_queue_raw_items(str(db_path), records)
            self.assertEqual(stats2['total_changed'], 0)
            self.assertEqual(records2, records)

    def test_staging_queue_dialog_sync_raw_button_end_to_end(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from types import SimpleNamespace
        from PyQt6.QtCore import QEventLoop, QTimer
        from PyQt6.QtWidgets import QApplication, QMessageBox
        from src.ui.staging_queue import StagingQueue, StagingQueueDialog

        self.__class__._qt_app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            prev_cwd, prev_instance = os.getcwd(), StagingQueue._instance
            os.chdir(tmp)
            StagingQueue._instance = None
            dialog, queue = None, None
            try:
                ev_dir = Path(tmp) / 'Evento'
                ev_dir.mkdir(parents=True)
                raw_path = str(ev_dir / 'IMG_7777.CR2')
                jpg_path = str(ev_dir / 'IMG_7777.jpg')
                raw_fid, jpg_fid = to_canonical_id(raw_path), to_canonical_id(jpg_path)

                db_path = self._create_catalog(Path(tmp), [
                    (raw_fid, 'IMG_7777.CR2', raw_fid, 'file', to_canonical_id(str(ev_dir)), '2026'),
                    (jpg_fid, 'IMG_7777.jpg', jpg_fid, 'file', to_canonical_id(str(ev_dir)), '2026'),
                ])

                queue = StagingQueue()
                # Simula item antigo já na fila antes da funcionalidade existir
                queue.items = [
                    StagingItem(raw_fid, 'IMG_7777.CR2', raw_fid, 'set_description', old_value='2026', new_value='2026, Missa')
                ]
                queue._save_to_disk()

                dialog = StagingQueueDialog(db_indexer=SimpleNamespace(db_name=str(db_path)))
                self.assertTrue(dialog.btn_sync_raw.isEnabled())

                with patch.object(QMessageBox, 'information') as info_mock:
                    loop = QEventLoop()
                    deadline = QTimer()
                    deadline.setSingleShot(True)
                    deadline.timeout.connect(loop.quit)
                    deadline.start(10000)

                    dialog._sync_raw_in_queue()
                    self.assertIsNotNone(dialog._raw_sync_worker)
                    dialog._raw_sync_worker.finished.connect(loop.quit)
                    loop.exec()
                    deadline.stop()
                    self.__class__._qt_app.processEvents()

                    self.assertEqual(info_mock.call_count, 1)

                by_fid = {it.file_id: it for it in queue.items}
                self.assertEqual(set(by_fid.keys()), {raw_fid, jpg_fid})
                self.assertEqual(by_fid[raw_fid].new_value, '2026, Missa, RAW')
                self.assertEqual(by_fid[jpg_fid].new_value, '2026, Missa')
                self.assertEqual(len(queue.store.load()), 2)
            finally:
                if dialog:
                    dialog.close()
                    dialog.deleteLater()
                if queue:
                    queue.timer.stop()
                self.__class__._qt_app.processEvents()
                StagingQueue._instance = prev_instance
                os.chdir(prev_cwd)


if __name__ == '__main__':
    unittest.main()
