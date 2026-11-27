"""Testes unitários e de integração para Sincronização de Tags RAW ↔ Cópias (JPG/PNG/HEIC)."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.database.database import to_canonical_id
from src.services.hash_deduplicator import FOLDER_MIME
from src.services.raw_tag_sync import (
    RawTagSyncJournal,
    _is_copy_subfolder,
    compute_bidirectional_tags,
    execute_raw_tag_sync,
    is_old_filename_tag,
    normalize_photo_base_stem,
    scan_raw_tag_sync_groups,
)


class FakeCatalogApi:
    def __init__(self, items_by_id: dict[str, dict]):
        self.items_by_id = {k: dict(v) for k, v in items_by_id.items()}
        self.updates: list[tuple[str, str]] = []

    def get(self, fid: str) -> dict:
        item = dict(self.items_by_id[fid])
        item['_etag'] = f'etag-{fid}'
        return item

    def update(self, item: dict, **kwargs):
        fid = item['id']
        body = kwargs.get('body') or {}
        if 'description' in body:
            self.items_by_id[fid]['description'] = body['description']
            self.updates.append((fid, body['description']))
        return dict(self.items_by_id[fid])


class TestRawTagSync(unittest.TestCase):
    def test_raw_tag_is_added_only_to_raw_and_never_copied_to_jpg(self):
        # Mesmo se o RAW já tiver 'RAW' (ou 'raw'), a cópia JPG NUNCA deve receber 'RAW'!
        res = compute_bidirectional_tags(
            [
                '2024, Brasil, RAW, raw_antigo.ARW',
                '2024, brasil, acolhido, Gianpietro, f585306840.jpeg',
            ],
            is_raw_flags=[True, False],
            ignore_filename_tags=True,
            add_raw_tag=True,
        )
        # RAW recebe 'acolhido' e 'Gianpietro' (sem 'f585306840.jpeg'), sem duplicar 'RAW'
        self.assertEqual(res[0][1], ['acolhido', 'Gianpietro'])
        self.assertEqual(res[0][0], '2024, Brasil, RAW, raw_antigo.ARW, acolhido, Gianpietro')
        # JPG não recebe 'RAW' nem 'raw_antigo.ARW'
        self.assertEqual(res[1][1], [])
        self.assertEqual(res[1][0], '2024, brasil, acolhido, Gianpietro, f585306840.jpeg')

        # Agora com RAW sem a tag 'RAW' e JPG já com todas as tags de conteúdo:
        res2 = compute_bidirectional_tags(
            ['2024, Brasil', '2024, Brasil, Gianpietro'],
            is_raw_flags=[True, False],
            ignore_filename_tags=True,
            add_raw_tag=True,
        )
        self.assertEqual(res2[0][1], ['Gianpietro', 'RAW'])
        self.assertEqual(res2[0][0], '2024, Brasil, Gianpietro, RAW')
        self.assertEqual(res2[1][1], [])
        self.assertNotIn('RAW', res2[1][0])

    def test_is_old_filename_tag_and_accent_insensitive_copy_subfolder(self):
        self.assertTrue(is_old_filename_tag('f466929408.jpeg'))
        self.assertTrue(is_old_filename_tag('mb     SÁBADO BELÉM ESPECIAL       20221217pp0481     1718.JPG'))
        self.assertFalse(is_old_filename_tag('SãoPaulo'))
        self.assertFalse(is_old_filename_tag('&boa'))
        self.assertTrue(_is_copy_subfolder('seleçao tratadas'))
        self.assertTrue(_is_copy_subfolder('Seleção Tratadas'))
        self.assertTrue(_is_copy_subfolder('SELECAO TRATADAS'))

    def test_normalize_photo_base_stem_handles_derivatives(self):
        self.assertEqual(normalize_photo_base_stem('DSC00001-Enhanced-NR.jpg'), 'dsc00001')
        self.assertEqual(normalize_photo_base_stem('DSC00001.ARW'), 'dsc00001')
        self.assertEqual(normalize_photo_base_stem('JOSE CARLOS (2).jpg', strip_copy_numbers=True), 'jose carlos')
        self.assertEqual(normalize_photo_base_stem('Evento (2023).jpg', strip_copy_numbers=True), 'evento (2023)')

    def test_scan_pairs_format_subfolders_and_includes_unpaired_raws_for_raw_tag(self):
        drive_id = 'DRIVE_ROOT'
        items = [
            {'id': 'F_2023', 'name': '2023', 'mimeType': FOLDER_MIME, 'parents': [drive_id], 'driveId': drive_id},
            {'id': 'F_CAT', 'name': '1 - Eventos', 'mimeType': FOLDER_MIME, 'parents': ['F_2023'], 'driveId': drive_id},
            # Evento A e B com IMG_8344: NÃO pareiam entre si, mas RAW_B entra como 'raw_only' para ganhar tag RAW!
            {'id': 'F_EV_A', 'name': '0714_Missa', 'mimeType': FOLDER_MIME, 'parents': ['F_CAT'], 'driveId': drive_id},
            {'id': 'F_EV_B', 'name': '1118_DiaPobres', 'mimeType': FOLDER_MIME, 'parents': ['F_CAT'], 'driveId': drive_id},
            {
                'id': 'JPG_A', 'name': 'IMG_8344.jpg', 'mimeType': 'image/jpeg',
                'parents': ['F_EV_A'], 'driveId': drive_id, 'description': 'Missa, Julho',
            },
            {
                'id': 'RAW_B', 'name': 'IMG_8344.CR2', 'mimeType': 'image/x-canon-cr2',
                'parents': ['F_EV_B'], 'driveId': drive_id, 'description': 'DiaDosPobres',
            },
            # Evento C: par legítimo em subpastas de formato
            {'id': 'F_EV_C', 'name': '0419_Batismo', 'mimeType': FOLDER_MIME, 'parents': ['F_CAT'], 'driveId': drive_id},
            {'id': 'F_EV_C_RAW', 'name': 'Arquivos RAW originais', 'mimeType': FOLDER_MIME, 'parents': ['F_EV_C'], 'driveId': drive_id},
            {'id': 'F_EV_C_OUT', 'name': 'Outros', 'mimeType': FOLDER_MIME, 'parents': ['F_EV_C'], 'driveId': drive_id},
            {
                'id': 'RAW_C', 'name': 'DSC00001.ARW', 'mimeType': 'image/x-sony-arw',
                'parents': ['F_EV_C_RAW'], 'driveId': drive_id, 'description': '2023, Batismo',
                'imageMediaMetadata': {'time': '2023:04:19 10:00:00', 'cameraModel': 'ILCE-7SM3'},
            },
            {
                'id': 'DNG_C', 'name': 'DSC00001-Enhanced-NR.dng', 'mimeType': 'image/x-adobe-dng',
                'parents': ['F_EV_C_RAW'], 'driveId': drive_id, 'description': '',
                'imageMediaMetadata': {'time': '2023:04:19 10:00:00', 'cameraModel': 'ILCE-7SM3'},
            },
            {
                'id': 'JPG_C', 'name': 'DSC00001-Enhanced-NR.jpg', 'mimeType': 'image/jpeg',
                'parents': ['F_EV_C_OUT'], 'driveId': drive_id, 'description': '2023, Batismo, Gianpietro, f12345.jpeg',
                'imageMediaMetadata': {'time': '2023:04:19 10:00:00', 'cameraModel': 'ILCE-7SM3'},
            },
        ]

        with tempfile.TemporaryDirectory() as tmp:
            groups = scan_raw_tag_sync_groups(
                roots=[tmp],
                drive_id=drive_id,
                drive_items=items,
                ignore_filename_tags=True,
                add_raw_tag=True,
                include_unpaired_raws=True,
            )
            self.assertEqual(len(groups), 2)
            # Primeiro vem o par de DSC00001, depois o raw_only de IMG_8344.CR2
            pair_g = groups[0]
            self.assertEqual(pair_g.match_kind, 'format_subfolder')
            self.assertEqual(pair_g.status, 'eligible')
            raw_map = {m.drive_id: m for m in pair_g.raw_members}
            self.assertEqual(raw_map['RAW_C'].added_tags, ['Gianpietro', 'RAW'])
            self.assertEqual(raw_map['DNG_C'].added_tags, ['2023', 'Batismo', 'Gianpietro', 'RAW'])
            # JPG_C NÃO recebe 'RAW'!
            self.assertEqual(pair_g.copy_members[0].added_tags, [])

            solo_g = groups[1]
            self.assertEqual(solo_g.match_kind, 'raw_only')
            self.assertEqual(solo_g.status, 'eligible')
            self.assertEqual(solo_g.raw_members[0].drive_id, 'RAW_B')
            self.assertEqual(solo_g.raw_members[0].added_tags, ['RAW'])
            self.assertEqual(solo_g.copy_members, [])

    def test_splits_exact_exif_shot_and_gives_orphan_raw_only_raw_tag(self):
        drive_id = 'DRIVE_ROOT'
        items = [
            {'id': 'F_2025', 'name': '2025', 'mimeType': FOLDER_MIME, 'parents': [drive_id], 'driveId': drive_id},
            {'id': 'F_CAT', 'name': '1 - Eventos', 'mimeType': FOLDER_MIME, 'parents': ['F_2025'], 'driveId': drive_id},
            {'id': 'F_EV', 'name': '0419_Batismo', 'mimeType': FOLDER_MIME, 'parents': ['F_CAT'], 'driveId': drive_id},
            {'id': 'F_RAW', 'name': 'Arquivos RAW Originais', 'mimeType': FOLDER_MIME, 'parents': ['F_EV'], 'driveId': drive_id},
            {'id': 'F_SAC', 'name': 'Sacramentos', 'mimeType': FOLDER_MIME, 'parents': ['F_EV'], 'driveId': drive_id},
            {
                'id': 'ARW_A7S3', 'name': 'DSC00031.ARW', 'mimeType': 'image/x-sony-arw',
                'parents': ['F_RAW'], 'driveId': drive_id, 'description': 'TagExclusivaA7S3',
                'imageMediaMetadata': {'time': '2025:04:19 21:22:34', 'cameraModel': 'ILCE-7SM3'},
            },
            {
                'id': 'DNG_A6400', 'name': 'DSC00031-Enhanced-NR.dng', 'mimeType': 'image/x-adobe-dng',
                'parents': ['F_RAW'], 'driveId': drive_id, 'description': 'TagDNG',
                'imageMediaMetadata': {'time': '2025:04:19 21:50:53', 'cameraModel': 'ILCE-6400'},
            },
            {
                'id': 'JPG_A6400', 'name': 'DSC00031-Enhanced-NR.jpg', 'mimeType': 'image/jpeg',
                'parents': ['F_SAC'], 'driveId': drive_id, 'description': 'TagJPG',
                'imageMediaMetadata': {'time': '2025:04:19 21:50:53', 'cameraModel': 'ILCE-6400'},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            groups = scan_raw_tag_sync_groups(
                roots=[tmp], drive_id=drive_id, drive_items=items, add_raw_tag=True, include_unpaired_raws=True
            )
            self.assertEqual(len(groups), 2)
            # Grupo 1: par DNG_A6400 + JPG_A6400
            self.assertEqual(groups[0].match_kind, 'format_subfolder')
            self.assertEqual(groups[0].raw_members[0].drive_id, 'DNG_A6400')
            self.assertEqual(groups[0].raw_members[0].added_tags, ['TagJPG', 'RAW'])
            self.assertEqual(groups[0].copy_members[0].added_tags, ['TagDNG'])
            # Grupo 2: ARW_A7S3 da outra câmera entra isoladamente apenas para ganhar 'RAW' (sem misturar tags!)
            self.assertEqual(groups[1].match_kind, 'raw_only')
            self.assertEqual(groups[1].raw_members[0].drive_id, 'ARW_A7S3')
            self.assertEqual(groups[1].raw_members[0].added_tags, ['RAW'])

    def test_blocked_pair_still_allows_solo_raw_to_get_raw_tag_without_mixing_copy_tags(self):
        drive_id = 'DRIVE_ROOT'
        items = [
            {'id': 'F_EV', 'name': 'Evento', 'mimeType': FOLDER_MIME, 'parents': [drive_id], 'driveId': drive_id},
            {
                'id': 'R1', 'name': 'DSC0010.ARW', 'mimeType': 'image/x-sony-arw',
                'parents': ['F_EV'], 'driveId': drive_id, 'description': 'TagA',
                'imageMediaMetadata': {'time': '2024:05:01 10:00:00', 'cameraModel': 'ILCE-7SM3'},
            },
            {
                'id': 'J1', 'name': 'DSC0010.JPG', 'mimeType': 'image/jpeg',
                'parents': ['F_EV'], 'driveId': drive_id, 'description': 'TagB',
                'imageMediaMetadata': {'time': '2024:05:02 15:30:00', 'cameraModel': 'ILCE-6400'},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            groups = scan_raw_tag_sync_groups(
                roots=[tmp], drive_id=drive_id, drive_items=items, add_raw_tag=True, include_unpaired_raws=True
            )
            by_kind = {g.match_kind: g for g in groups}
            # O pareamento fica bloqueado (não mistura TagA e TagB)
            self.assertEqual(by_kind['same_folder'].status, 'blocked')
            self.assertIn('câmera', by_kind['same_folder'].reason.lower())
            # Mas o R1 pode receber isoladamente apenas a tag 'RAW' (sem TagB!)
            self.assertEqual(by_kind['raw_only'].status, 'eligible')
            self.assertEqual(by_kind['raw_only'].raw_members[0].added_tags, ['RAW'])
            self.assertEqual(by_kind['raw_only'].raw_members[0].proposed_description, 'TagA, RAW')

    def test_execute_updates_drive_journal_and_local_sqlite_with_raw_tag_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'Banco de Imagens'
            ev_dir = root / '2024' / 'Crisma'
            ev_dir.mkdir(parents=True)
            raw_file = ev_dir / 'IMG_0001.CR2'
            jpg_file = ev_dir / 'IMG_0001.JPG'
            solo_raw = ev_dir / 'IMG_0002.CR2'
            raw_file.write_bytes(b'rawdata')
            jpg_file.write_bytes(b'jpgdata')
            solo_raw.write_bytes(b'raw2')

            db_path = Path(tmp) / 'data' / 'file_index.db'
            db_path.parent.mkdir(parents=True)
            conn = sqlite3.connect(str(db_path))
            conn.execute('PRAGMA user_version = 1')
            conn.execute(
                'CREATE TABLE files (file_id TEXT PRIMARY KEY, name TEXT, path TEXT, mimeType TEXT, source TEXT, description TEXT, thumbnailRotation INTEGER DEFAULT 0)'
            )
            conn.execute(
                'CREATE TABLE search_index (name TEXT, description TEXT, normalized_name TEXT, normalized_description TEXT, file_id TEXT, source TEXT)'
            )
            conn.execute(
                'CREATE TABLE drive_link_validation (local_file_id TEXT PRIMARY KEY, drive_id TEXT, status TEXT)'
            )
            raw_cid = to_canonical_id(str(raw_file))
            jpg_cid = to_canonical_id(str(jpg_file))
            solo_cid = to_canonical_id(str(solo_raw))
            conn.execute('INSERT INTO files VALUES (?,?,?,?,?,?,0)', (raw_cid, 'IMG_0001.CR2', raw_cid, 'file', 'local', 'Crisma'))
            conn.execute('INSERT INTO files VALUES (?,?,?,?,?,?,0)', (jpg_cid, 'IMG_0001.JPG', jpg_cid, 'file', 'local', '2024, DomCíceroAlves'))
            conn.execute('INSERT INTO files VALUES (?,?,?,?,?,?,0)', (solo_cid, 'IMG_0002.CR2', solo_cid, 'file', 'local', ''))
            conn.execute('INSERT INTO drive_link_validation VALUES (?,?,?)', (raw_cid, 'DRV_RAW', 'trusted_metadata'))
            conn.execute('INSERT INTO drive_link_validation VALUES (?,?,?)', (jpg_cid, 'DRV_JPG', 'trusted_metadata'))
            conn.execute('INSERT INTO drive_link_validation VALUES (?,?,?)', (solo_cid, 'DRV_SOLO', 'trusted_metadata'))
            conn.commit()
            conn.close()

            drive_id = 'DRIVE_ROOT'
            drive_items = [
                {'id': 'F_2024', 'name': '2024', 'mimeType': FOLDER_MIME, 'parents': [drive_id], 'driveId': drive_id},
                {'id': 'F_CRISMA', 'name': 'Crisma', 'mimeType': FOLDER_MIME, 'parents': ['F_2024'], 'driveId': drive_id},
                {
                    'id': 'DRV_RAW', 'name': 'IMG_0001.CR2', 'mimeType': 'image/x-canon-cr2',
                    'parents': ['F_CRISMA'], 'driveId': drive_id, 'description': 'Crisma', 'version': '1',
                },
                {
                    'id': 'DRV_JPG', 'name': 'IMG_0001.JPG', 'mimeType': 'image/jpeg',
                    'parents': ['F_CRISMA'], 'driveId': drive_id, 'description': '2024, DomCíceroAlves', 'version': '1',
                },
                {
                    'id': 'DRV_SOLO', 'name': 'IMG_0002.CR2', 'mimeType': 'image/x-canon-cr2',
                    'parents': ['F_CRISMA'], 'driveId': drive_id, 'description': '', 'version': '1',
                },
            ]

            groups = scan_raw_tag_sync_groups(
                roots=[str(root)], drive_id=drive_id, drive_items=drive_items, add_raw_tag=True, include_unpaired_raws=True
            )
            self.assertEqual(len(groups), 2)

            fake_api = FakeCatalogApi({it['id']: it for it in drive_items if 'version' in it})
            stats = execute_raw_tag_sync(groups, db_name=str(db_path), catalog_api=fake_api, add_raw_tag=True)

            self.assertEqual(stats['groups_completed'], 2)
            self.assertEqual(stats['files_updated'], 3)
            self.assertTrue(Path(stats['backup_path']).exists())
            self.assertEqual(fake_api.items_by_id['DRV_RAW']['description'], 'Crisma, 2024, DomCíceroAlves, RAW')
            self.assertEqual(fake_api.items_by_id['DRV_JPG']['description'], '2024, DomCíceroAlves, Crisma')
            self.assertEqual(fake_api.items_by_id['DRV_SOLO']['description'], 'RAW')

            conn = sqlite3.connect(str(db_path))
            r_desc = conn.execute('SELECT description FROM files WHERE file_id=?', (raw_cid,)).fetchone()[0]
            j_desc = conn.execute('SELECT description FROM files WHERE file_id=?', (jpg_cid,)).fetchone()[0]
            s_desc = conn.execute('SELECT description FROM files WHERE file_id=?', (solo_cid,)).fetchone()[0]
            s_cnt = conn.execute('SELECT COUNT(*) FROM search_index').fetchone()[0]
            conn.close()
            self.assertEqual(r_desc, 'Crisma, 2024, DomCíceroAlves, RAW')
            self.assertEqual(j_desc, '2024, DomCíceroAlves, Crisma')
            self.assertEqual(s_desc, 'RAW')
            self.assertEqual(s_cnt, 3)

            journal = RawTagSyncJournal(str(db_path))
            with journal._connect() as jconn:
                u_cnt = jconn.execute("SELECT COUNT(*) FROM updates WHERE state='completed'").fetchone()[0]
                run_st = jconn.execute('SELECT status FROM runs').fetchone()[0]
            self.assertEqual(u_cnt, 3)
            self.assertEqual(run_st, 'completed')


if __name__ == '__main__':
    unittest.main()
