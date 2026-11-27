"""Testes offline do auditor de vinculos Drive/local."""

import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.revalidate_drive_links import (
    DriveApiError,
    HashBudget,
    ONLINE_ONLY_ATTRIBUTES,
    RevalidationCancelled,
    _verify_live_disk_proposal,
    api_json,
    apply_plan_components,
    apply_results,
    base_audit_rows,
    build_atomic_relink_plan,
    build_local_candidate_index,
    build_remote_tree,
    extract_local_rel_parts,
    read_local_links,
    remote_parent_paths,
    resolve_local_roots,
    resolve_suspects,
    run_audit_pipeline,
    stale_drive_cache_ids,
    write_proposed_csv,
)


class LinkRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.wrong = root / 'Antiga' / 'foto.jpg'
        self.correct = root / 'Evento' / 'foto.jpg'
        self.wrong.parent.mkdir(parents=True)
        self.correct.parent.mkdir(parents=True)
        self.wrong.write_bytes(b'conteudo-incorreto')
        self.correct.write_bytes(b'conteudo-correto--')
        self.assertEqual(self.wrong.stat().st_size, self.correct.stat().st_size)

        self.connection = sqlite3.connect(':memory:')
        self.cursor = self.connection.cursor()
        self.cursor.execute(
            """
            CREATE TABLE files (
                file_id TEXT PRIMARY KEY, name TEXT, path TEXT, size INTEGER,
                source TEXT, webContentLink TEXT
            )
            """
        )
        self.cursor.execute(
            """
            CREATE TABLE search_index (
                file_id TEXT, source TEXT
            )
            """
        )
        link = 'https://drive.google.com/file/d/drive_123/view'
        self.cursor.executemany(
            "INSERT INTO files VALUES (?, 'foto.jpg', ?, ?, 'local', ?)",
            [
                (str(self.wrong), str(self.wrong), self.wrong.stat().st_size, link),
                (str(self.correct), str(self.correct), self.correct.stat().st_size, ''),
            ],
        )
        self.cursor.execute(
            "INSERT INTO files VALUES ('stale_drive', 'velho.jpg', NULL, 10, 'drive', '')"
        )
        self.cursor.execute(
            "INSERT INTO search_index VALUES ('stale_drive', 'drive')"
        )

        digest = hashlib.md5(self.correct.read_bytes()).hexdigest()
        self.items = [
            {'id': 'folder_1', 'name': 'Evento', 'mimeType': 'application/vnd.google-apps.folder'},
            {
                'id': 'drive_123', 'name': 'foto.jpg',
                'size': str(self.correct.stat().st_size),
                'md5Checksum': digest, 'mimeType': 'image/jpeg',
                'parents': ['folder_1'],
                'webViewLink': 'https://drive.google.com/file/d/drive_123/view',
            },
        ]

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def test_unique_hash_repairs_wrong_legacy_link_and_prunes_cache(self):
        links, malformed = read_local_links(self.cursor)
        remote = {item['id']: item for item in self.items}
        resolver = remote_parent_paths(self.items)
        rows, suspects, grouped = base_audit_rows(
            links, malformed, remote, resolver
        )
        self.assertEqual(suspects, {'drive_123'})

        candidates = build_local_candidate_index(self.cursor)
        budget = HashBudget(1024 * 1024, 1024 * 1024)
        rows, relinks = resolve_suspects(
            rows, suspects, grouped, remote, candidates, budget
        )
        self.assertEqual(relinks['drive_123'].file_id, str(self.correct))

        stale = stale_drive_cache_ids(self.cursor, set(remote))
        self.assertEqual(stale, ['stale_drive'])
        apply_results(
            self.connection, rows, relinks, remote, stale, prune_stale=True
        )

        wrong_link = self.cursor.execute(
            'SELECT webContentLink FROM files WHERE file_id=?', (str(self.wrong),)
        ).fetchone()[0]
        correct_link = self.cursor.execute(
            'SELECT webContentLink FROM files WHERE file_id=?', (str(self.correct),)
        ).fetchone()[0]
        self.assertIsNone(wrong_link)
        self.assertIn('drive_123', correct_link)
        self.assertIsNone(self.cursor.execute(
            "SELECT 1 FROM files WHERE file_id='stale_drive'"
        ).fetchone())
        validation = self.cursor.execute(
            "SELECT drive_id,status FROM drive_link_validation WHERE local_file_id=?",
            (str(self.correct),),
        ).fetchone()
        self.assertEqual(validation, ('drive_123', 'valid_hash_relinked'))


class StrictPathSwapAndOnlineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / 'Banco de Imagens'
        self.root.mkdir(parents=True)
        self.drive_id = 'bank_root'
        self.scan_roots = resolve_local_roots({}, [str(self.root)])

        self.connection = sqlite3.connect(':memory:')
        self.cursor = self.connection.cursor()
        self.cursor.execute(
            """
            CREATE TABLE files (
                file_id TEXT PRIMARY KEY, name TEXT, path TEXT, size INTEGER,
                source TEXT, webContentLink TEXT
            )
            """
        )
        self.cursor.execute(
            """
            CREATE TABLE search_index (
                file_id TEXT, source TEXT
            )
            """
        )

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def _add_local(self, rel_path, content, drive_id=''):
        full = self.root / Path(rel_path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content)
        link = f'https://drive.google.com/file/d/{drive_id}/view?usp=drivesdk' if drive_id else ''
        key = str(full).lower()
        self.cursor.execute(
            "INSERT INTO files VALUES (?, ?, ?, ?, 'local', ?)",
            (key, full.name, str(full), len(content), link),
        )
        return key, full

    @staticmethod
    def _drive_folder(fid, name, parent, drive_id='bank_root'):
        return {
            'id': fid, 'name': name, 'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent], 'driveId': drive_id, 'trashed': False,
        }

    @staticmethod
    def _drive_file(fid, name, parent, content, drive_id='bank_root'):
        return {
            'id': fid, 'name': name, 'mimeType': 'image/jpeg',
            'parents': [parent], 'driveId': drive_id, 'trashed': False,
            'size': str(len(content)),
            'md5Checksum': hashlib.md5(content).hexdigest(),
            'webViewLink': f'https://drive.google.com/file/d/{fid}/view?usp=drivesdk',
        }

    def test_full_path_required_and_broken_cycle_homonymous_trees_block_repair(self):
        data = b'mesma-foto-id-diferente'
        items = [
            self._drive_folder('y20', '2020', self.drive_id),
            self._drive_folder('e20', 'Evento', 'y20'),
            self._drive_folder('y21', '2021', self.drive_id),
            self._drive_folder('e21', 'Evento', 'y21'),
            self._drive_file('d20', 'foto.jpg', 'e20', data),
            self._drive_file('d21', 'foto.jpg', 'e21', data),
            # Ancestral ausente
            self._drive_file('d_orphan', 'orfao.jpg', 'pasta_inexistente', data),
            # Ciclo entre pastas
            self._drive_folder('c1', 'CicloA', 'c2'),
            self._drive_folder('c2', 'CicloB', 'c1'),
            self._drive_file('d_cycle', 'ciclo.jpg', 'c1', data),
            # Pastas homonimas
            self._drive_folder('h1', 'Missa', self.drive_id),
            self._drive_folder('h2', 'missa', self.drive_id),
            self._drive_file('d_hom', 'santa.jpg', 'h1', data),
        ]
        tree = build_remote_tree(items, drive_id=self.drive_id)
        self.assertTrue(tree.nodes['d20'].verified)
        self.assertFalse(tree.nodes['d_orphan'].verified)
        self.assertEqual(tree.nodes['d_orphan'].tree_error, 'missing_ancestor')
        self.assertFalse(tree.nodes['d_cycle'].verified)
        self.assertEqual(tree.nodes['d_cycle'].tree_error, 'cycle')
        self.assertFalse(tree.nodes['d_hom'].verified)
        self.assertEqual(tree.nodes['d_hom'].tree_error, 'homonymous_ancestor')

        # Mesmo com sufixo "Evento/foto.jpg" e tamanho igual, 2020->d21 NAO pode virar trusted_metadata!
        lid_20, _ = self._add_local(r'2020\Evento\foto.jpg', data, drive_id='d21')
        lid_orp, _ = self._add_local(r'Orfao\orfao.jpg', data, drive_id='d_orphan')
        lid_hom, _ = self._add_local(r'Missa\santa.jpg', data, drive_id='d_hom')

        rows, plan, _, remote_by_id, _ = run_audit_pipeline(
            self.connection, items, self.drive_id, self.scan_roots, hash_mode='suspects',
        )
        by_id = {r.local_file_id: r for r in rows}
        self.assertNotEqual(by_id[lid_20].status, 'trusted_metadata')
        self.assertEqual(by_id[lid_20].status, 'valid_hash_relinked')
        self.assertEqual(by_id[lid_20].proposed_drive_id, 'd20')
        self.assertEqual(by_id[lid_orp].status, 'blocked_remote_hierarchy')
        self.assertEqual(by_id[lid_hom].status, 'blocked_remote_hierarchy')
        relinked_lids = [p['local_file_id'] for c in plan['components'] for p in c['relinks']]
        self.assertEqual(relinked_lids, [lid_20])

    def test_crossed_swap_and_three_way_cycle_resolve_atomically(self):
        same_bytes = b'duplicata-identica-troca-cruzada'
        items = [
            self._drive_folder('fA', 'Amigo Secreto', self.drive_id),
            self._drive_folder('fB', 'Amigo Secreto_b', self.drive_id),
            self._drive_folder('fC', 'Amigo Secreto_c', self.drive_id),
            self._drive_file('idA', 'f.jpg', 'fA', same_bytes),
            self._drive_file('idB', 'f.jpg', 'fB', same_bytes),
            self._drive_file('idC', 'f.jpg', 'fC', same_bytes),
        ]
        # Ciclo de 3 vias: A->idB, B->idC, C->idA
        lidA, _ = self._add_local(r'Amigo Secreto\f.jpg', same_bytes, drive_id='idB')
        lidB, _ = self._add_local(r'Amigo Secreto_b\f.jpg', same_bytes, drive_id='idC')
        lidC, _ = self._add_local(r'Amigo Secreto_c\f.jpg', same_bytes, drive_id='idA')

        rows, plan, _, remote_by_id, tree = run_audit_pipeline(
            self.connection, items, self.drive_id, self.scan_roots, hash_mode='suspects',
        )
        self.assertEqual(1, len(plan['components']))
        self.assertEqual(3, plan['components'][0]['size'])
        statuses, stats = apply_plan_components(
            self.connection, plan, drive_id=self.drive_id, scan_roots=self.scan_roots,
            remote_by_id=remote_by_id, remote_tree=tree, audit_rows=rows,
        )
        self.assertEqual(1, stats['applied_components'])
        self.assertEqual(3, stats['applied_relinks'])
        self.assertTrue(all(v == 'applied' for v in statuses.values()))

        cur_links = dict(self.cursor.execute("SELECT file_id, webContentLink FROM files WHERE source='local'").fetchall())
        self.assertIn('idA', cur_links[lidA])
        self.assertIn('idB', cur_links[lidB])
        self.assertIn('idC', cur_links[lidC])

    def test_online_only_stays_pending_blocks_half_swap_and_supports_opt_in_and_cancel(self):
        from unittest.mock import patch

        same_bytes = b'0123456789abcdef' * 4
        items = [
            self._drive_folder('f1', 'Pasta1', self.drive_id),
            self._drive_folder('f2', 'Pasta2', self.drive_id),
            self._drive_file('id1', 'foto.jpg', 'f1', same_bytes),
            self._drive_file('id2', 'foto.jpg', 'f2', same_bytes),
        ]
        lid1, path1 = self._add_local(r'Pasta1\foto.jpg', same_bytes, drive_id='id2')
        lid2, path2 = self._add_local(r'Pasta2\foto.jpg', same_bytes, drive_id='id1')

        real_stat = os.stat
        real_open = open

        for flag in (0x1000, 0x40000, 0x400000):
            with self.subTest(flag=hex(flag)):
                opened_paths = []

                def fake_stat(p, *args, **kwargs):
                    res = real_stat(p, *args, **kwargs)
                    if os.path.normcase(str(p)) == os.path.normcase(str(path2)):
                        return os.stat_result((
                            res.st_mode, res.st_ino, res.st_dev, res.st_nlink,
                            res.st_uid, res.st_gid, res.st_size,
                            res.st_atime, res.st_mtime, res.st_ctime,
                        ))
                    return res

                def guarded_is_online(p):
                    if os.path.normcase(str(p)) == os.path.normcase(str(path2)):
                        res = real_stat(p)
                        return True, type('StatObj', (), {'st_mode': res.st_mode, 'st_size': res.st_size, 'st_mtime_ns': res.st_mtime_ns, 'st_file_attributes': flag})(), 'online_only_pending'
                    res = real_stat(p)
                    return False, res, ''

                def spy_open(p, *args, **kwargs):
                    opened_paths.append(os.path.normcase(str(p)))
                    return real_open(p, *args, **kwargs)

                with patch('scripts.revalidate_drive_links.is_online_only', side_effect=guarded_is_online), \
                        patch('builtins.open', side_effect=spy_open):
                    rows, plan, _, _, _ = run_audit_pipeline(
                        self.connection, items, self.drive_id, self.scan_roots,
                        hash_mode='suspects', hash_budget=HashBudget(10**7, 10**6, allow_online=False),
                    )
                self.assertNotIn(os.path.normcase(str(path2)), opened_paths)
                by_lid = {r.local_file_id: r for r in rows}
                self.assertEqual('pending_online_hash', by_lid[lid2].status)
                # Como lid2 nao pode confirmar saida de id1, lid1 fica em conflito para evitar dois donos de id1!
                self.assertEqual('relink_conflict', by_lid[lid1].status)
                self.assertEqual([], plan['components'])

        # Agora testa opt-in (allow_online=True) + progresso + cancelamento
        progress_events = []
        cancel_flag = {'stop': False}

        def online_only_stat(p):
            res = real_stat(p)
            attrs = ONLINE_ONLY_ATTRIBUTES if os.path.normcase(str(p)) == os.path.normcase(str(path2)) else 0x20
            return (bool(attrs & ONLINE_ONLY_ATTRIBUTES),
                    type('StatObj', (), {'st_mode': res.st_mode, 'st_size': res.st_size, 'st_mtime_ns': res.st_mtime_ns, 'st_file_attributes': attrs})(),
                    'online_only_pending' if (attrs & ONLINE_ONLY_ATTRIBUTES) else '')

        def on_prog(p, done, total):
            progress_events.append((p, done, total))
            cancel_flag['stop'] = True

        with patch('scripts.revalidate_drive_links.is_online_only', side_effect=online_only_stat):
            budget_cancel = HashBudget(
                10**7, 10**6, allow_online=True,
                cancel_check=lambda: cancel_flag['stop'],
                progress_callback=on_prog,
            )
            with self.assertRaises(RevalidationCancelled):
                budget_cancel.md5(str(path2), len(same_bytes))
            self.assertTrue(progress_events)

            budget_ok = HashBudget(10**7, 10**6, allow_online=True)
            rows_ok, plan_ok, _, _, _ = run_audit_pipeline(
                self.connection, items, self.drive_id, self.scan_roots,
                hash_mode='suspects', hash_budget=budget_ok,
            )
            self.assertEqual(1, len(plan_ok['components']))
            self.assertEqual(2, plan_ok['components'][0]['size'])

    def test_remote_missing_recovered_exact_path_and_never_guesses_paren2(self):
        data = b'conteudo-com-copia-2-local'
        items = [
            self._drive_folder('f2005', '2005', self.drive_id),
            self._drive_file('id_live', 'foto.jpg', 'f2005', data),
        ]
        lid_real, _ = self._add_local(r'2005\foto.jpg', data, drive_id='id_antigo_apagado')
        lid_p2, _ = self._add_local(r'2005\foto (2).jpg', data, drive_id='id_outro_sumido')

        rows, plan, _, remote_by_id, tree = run_audit_pipeline(
            self.connection, items, self.drive_id, self.scan_roots, hash_mode='suspects',
        )
        by_lid = {r.local_file_id: r for r in rows}
        self.assertEqual('valid_hash_relinked', by_lid[lid_real].status)
        self.assertEqual('id_live', by_lid[lid_real].proposed_drive_id)
        # Nunca deve associar `foto (2).jpg` ao `id_live` por heuristicas de `(2)`.
        self.assertEqual('remote_missing', by_lid[lid_p2].status)
        self.assertEqual('', by_lid[lid_p2].proposed_drive_id)
        relink_lids = [p['local_file_id'] for c in plan['components'] for p in c['relinks']]
        self.assertEqual([lid_real], relink_lids)

    def test_hash_read_detects_local_file_mutation_during_read(self):
        from unittest.mock import patch

        data = b'arquivo-estavel-para-teste'
        _, path = self._add_local(r'2022\alterada.jpg', data, drive_id='x')
        real_stat = os.stat
        calls = {'n': 0}

        def mutating_stat(p):
            res = real_stat(p)
            if os.path.normcase(str(p)) == os.path.normcase(str(path)):
                calls['n'] += 1
                mtime = res.st_mtime_ns if calls['n'] == 1 else res.st_mtime_ns + 5000
                return type('StatObj', (), {'st_mode': res.st_mode, 'st_size': res.st_size, 'st_mtime_ns': mtime, 'st_file_attributes': 0x20})()
            return res

        with patch('scripts.revalidate_drive_links.os.stat', side_effect=mutating_stat):
            budget = HashBudget(10**7, 10**6)
            digest, err = budget.md5(str(path), len(data))
        self.assertEqual('', digest)
        self.assertEqual('filesystem_changed_during_hash', err)

    def test_apply_plan_limit_preserves_swap_units_and_rejects_stale_drive_disk_or_db(self):
        b1 = b'conteudo-1-swap'
        b2 = b'conteudo-2-single'
        items = [
            self._drive_folder('fa', 'A', self.drive_id),
            self._drive_folder('fb', 'B', self.drive_id),
            self._drive_folder('fc', 'C', self.drive_id),
            self._drive_file('idA', 'f.jpg', 'fa', b1),
            self._drive_file('idB', 'f.jpg', 'fb', b1),
            self._drive_file('idC', 'g.jpg', 'fc', b2),
        ]
        lidA, pathA = self._add_local(r'A\f.jpg', b1, drive_id='idB')
        lidB, pathB = self._add_local(r'B\f.jpg', b1, drive_id='idA')
        lidC, pathC = self._add_local(r'C\g.jpg', b2, drive_id='id_sumido')

        rows, plan, _, remote_by_id, tree = run_audit_pipeline(
            self.connection, items, self.drive_id, self.scan_roots, hash_mode='suspects',
        )
        # Preview CSV gerado sem erro e com colunas old -> proposed + evidencias
        csv_preview = Path(self.temp_dir.name) / 'preview.csv'
        write_proposed_csv(csv_preview, plan)
        preview_text = csv_preview.read_text(encoding='utf-8-sig')
        self.assertIn('idA', preview_text)
        self.assertIn('caminho_completo_1para1+tamanho+md5', preview_text)

        # 1) limit=1 nao deve quebrar a troca {A,B} (tamanho 2): aplica apenas {C} (tamanho 1).
        statuses_lim1, stats_lim1 = apply_plan_components(
            self.connection, plan, drive_id=self.drive_id, scan_roots=self.scan_roots,
            remote_by_id=remote_by_id, remote_tree=tree, limit=1, rehash_disk=True,
        )
        self.assertEqual(1, stats_lim1['applied_components'])
        self.assertEqual(1, stats_lim1['applied_relinks'])
        self.assertEqual('applied', statuses_lim1[lidC])
        self.assertEqual('deferred_by_limit', statuses_lim1[lidA])
        self.assertEqual('deferred_by_limit', statuses_lim1[lidB])

        # 2) Alteracao no Drive invalida o componente na revalidacao antes de aplicar!
        stale_items = [dict(it) for it in items]
        for it in stale_items:
            if it['id'] == 'idA':
                it['md5Checksum'] = '0' * 32
        stale_tree = build_remote_tree(stale_items, drive_id=self.drive_id)
        statuses_stale, stats_stale = apply_plan_components(
            self.connection, plan, drive_id=self.drive_id, scan_roots=self.scan_roots,
            remote_by_id={i['id']: i for i in stale_items}, remote_tree=stale_tree, rehash_disk=True,
        )
        self.assertEqual(0, stats_stale['applied_components'])
        self.assertTrue(statuses_stale[lidA].startswith('skipped_revalidation:'))

        # 3) Alteracao externa no banco SQLite antes do commit aborta apenas aquele componente.
        self.cursor.execute("UPDATE files SET webContentLink='https://drive.google.com/file/d/outro/view' WHERE file_id=?", (lidA,))
        statuses_db, stats_db = apply_plan_components(
            self.connection, plan, drive_id=self.drive_id, scan_roots=self.scan_roots,
            remote_by_id=remote_by_id, remote_tree=tree, rehash_disk=False,
        )
        self.assertEqual(0, stats_db['applied_components'])
        self.assertEqual('skipped_db:local_db_changed_since_plan', statuses_db[lidA])
        self.assertEqual('skipped_db:local_db_changed_since_plan', statuses_db[lidB])

        # 4) Alteracao do arquivo no disco apos analise antiga invalida na rechecagem MD5!
        self.cursor.execute("UPDATE files SET webContentLink='https://drive.google.com/file/d/idB/view' WHERE file_id=?", (lidA,))
        pathA.write_bytes(b'conteudo-modif!')  # mesmo tamanho de b1 (15 bytes), MD5 diferente!
        statuses_disk, stats_disk = apply_plan_components(
            self.connection, plan, drive_id=self.drive_id, scan_roots=self.scan_roots,
            remote_by_id=remote_by_id, remote_tree=tree, rehash_disk=True,
        )
        self.assertEqual(0, stats_disk['applied_components'])
        self.assertEqual('skipped_revalidation:hash_divergente_na_rechecagem', statuses_disk[lidA])

    def test_live_drive_revalidation_overrides_initial_remote_tree_and_detects_post_scan_change(self):
        data = b'conteudo-revalidado-ao-vivo'
        items = [
            self._drive_folder('f1', '2020', self.drive_id),
            self._drive_file('id1', 'foto.jpg', 'f1', data),
        ]
        lid, _ = self._add_local(r'2020\foto.jpg', data, drive_id='id_antigo')
        rows, plan, _, remote_by_id, initial_tree = run_audit_pipeline(
            self.connection, items, self.drive_id, self.scan_roots, hash_mode='suspects',
        )

        # Mesmo passando initial_tree, fornecer item_fetcher/children_fetcher obriga consulta ao vivo.
        live_calls = []
        live_items = {it['id']: dict(it) for it in items}
        live_items['id1']['md5Checksum'] = 'f' * 32  # mudou no Drive durante o calculo de hashes locais!

        def fetch_item(fid):
            live_calls.append(('get', fid))
            return live_items.get(fid)

        def fetch_children(pid):
            live_calls.append(('list', pid))
            return [v for v in live_items.values() if pid in (v.get('parents') or [])]

        statuses, stats = apply_plan_components(
            self.connection, plan, drive_id=self.drive_id, scan_roots=self.scan_roots,
            remote_by_id=remote_by_id, remote_tree=initial_tree,
            item_fetcher=fetch_item, children_fetcher=fetch_children,
            audit_rows=rows,
        )
        self.assertTrue(live_calls)
        self.assertEqual(0, stats['applied_components'])
        self.assertEqual('skipped_revalidation:md5_drive_mudou', statuses[lid])

    def test_old_wrong_link_cleared_and_staying_duplicate_twin_confirmed_atomically(self):
        same = b'mesmo-conteudo-em-duas-pastas'
        items = [
            self._drive_folder('f05', '2005', self.drive_id),
            self._drive_folder('f10', '2010', self.drive_id),
            self._drive_file('id05', 'agui.jpg', 'f05', same),
            self._drive_file('id10', 'agui.jpg', 'f10', same),
        ]
        # 2005 apontava errado para id10; 2010 ja apontava certo para id10; e uma copia (2) sem par no Drive tambem apontava para id10.
        lid05, _ = self._add_local(r'2005\agui.jpg', same, drive_id='id10')
        lid10, _ = self._add_local(r'2010\agui.jpg', same, drive_id='id10')
        lid_p2, _ = self._add_local(r'2010\agui (2).jpg', same, drive_id='id10')

        rows, plan, _, remote_by_id, tree = run_audit_pipeline(
            self.connection, items, self.drive_id, self.scan_roots, hash_mode='suspects',
        )
        by_lid = {r.local_file_id: r for r in rows}
        self.assertEqual('valid_hash_relinked', by_lid[lid05].status)
        self.assertEqual('valid_hash', by_lid[lid10].status)
        self.assertEqual('relinked_away', by_lid[lid_p2].status)
        self.assertEqual('clear_wrong_link', by_lid[lid_p2].action)
        self.assertEqual(1, len(plan['components']))
        self.assertEqual(3, plan['components'][0]['size'])

        statuses, stats = apply_plan_components(
            self.connection, plan, drive_id=self.drive_id, scan_roots=self.scan_roots,
            remote_by_id=remote_by_id, remote_tree=tree, audit_rows=rows,
        )
        self.assertEqual(1, stats['applied_components'])
        self.assertEqual(1, stats['applied_relinks'])
        self.assertEqual(1, stats['applied_confirmations'])
        self.assertEqual(1, stats['applied_clears'])
        self.assertEqual('applied', statuses[lid05])
        self.assertEqual('applied', statuses[lid10])
        self.assertEqual('applied', statuses[lid_p2])

        links = dict(self.cursor.execute("SELECT file_id, webContentLink FROM files WHERE source='local'").fetchall())
        self.assertIn('id05', links[lid05])
        self.assertIn('id10', links[lid10])
        self.assertIsNone(links[lid_p2])

        vals = dict(self.cursor.execute("SELECT local_file_id, status FROM drive_link_validation").fetchall())
        self.assertEqual('valid_hash_relinked', vals[lid05])
        self.assertEqual('valid_hash', vals[lid10])
        self.assertEqual('relinked_away', vals[lid_p2])

    def test_cancellation_during_pre_apply_leaves_database_unchanged(self):
        data = b'cancelamento-pre-commit'
        items = [
            self._drive_folder('f1', '2020', self.drive_id),
            self._drive_file('id1', 'foto.jpg', 'f1', data),
        ]
        lid, _ = self._add_local(r'2020\foto.jpg', data, drive_id='id_antigo')
        rows, plan, _, remote_by_id, tree = run_audit_pipeline(
            self.connection, items, self.drive_id, self.scan_roots, hash_mode='suspects',
        )
        with self.assertRaises(RevalidationCancelled):
            apply_plan_components(
                self.connection, plan, drive_id=self.drive_id, scan_roots=self.scan_roots,
                remote_by_id=remote_by_id, remote_tree=tree,
                cancel_check=lambda: True, audit_rows=rows,
            )
        cur_link = self.cursor.execute("SELECT webContentLink FROM files WHERE file_id=?", (lid,)).fetchone()[0]
        self.assertIn('id_antigo', cur_link)

    def test_single_drive_404_or_api_error_skips_only_failed_component(self):
        b1 = b'item-sumido-no-drive'
        b2 = b'item-valido-continua'
        items = [
            self._drive_folder('f1', '2020', self.drive_id),
            self._drive_file('id1', 'a.jpg', 'f1', b1),
            self._drive_file('id2', 'b.jpg', 'f1', b2),
        ]
        lid1, _ = self._add_local(r'2020\a.jpg', b1, drive_id='old1')
        lid2, _ = self._add_local(r'2020\b.jpg', b2, drive_id='old2')
        rows, plan, _, remote_by_id, _ = run_audit_pipeline(
            self.connection, items, self.drive_id, self.scan_roots, hash_mode='suspects',
        )
        self.assertEqual(2, len(plan['components']))

        def fetch_item(fid):
            if fid == 'id1':
                raise DriveApiError('HTTP 404 Not Found', status_code=404)
            return next(it for it in items if it['id'] == fid)

        def fetch_children(pid):
            if pid == 'f1':
                return [items[2]]  # id1 ausente na listagem, obriga get('id1') -> 404
            return [it for it in items if pid in (it.get('parents') or [])]

        statuses, stats = apply_plan_components(
            self.connection, plan, drive_id=self.drive_id, scan_roots=self.scan_roots,
            remote_by_id=remote_by_id,
            item_fetcher=fetch_item, children_fetcher=fetch_children,
            audit_rows=rows,
        )
        self.assertEqual(1, stats['applied_components'])
        self.assertEqual(1, stats['applied_relinks'])
        self.assertEqual(1, stats['skipped_stale_components'])
        self.assertEqual('skipped_revalidation:destino_nao_encontrado_no_drive', statuses[lid1])
        self.assertEqual('applied', statuses[lid2])
        links = dict(self.cursor.execute("SELECT file_id, webContentLink FROM files WHERE source='local'").fetchall())
        self.assertIn('old1', links[lid1])
        self.assertIn('id2', links[lid2])

    def test_api_json_refreshes_token_on_401_and_honors_cancel_during_retry(self):
        import io
        import urllib.error
        from unittest.mock import patch

        calls = []

        class _FakeResp:
            def __enter__(self):
                return io.StringIO('{"ok": true}')

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=90):
            calls.append(req.get_header('Authorization'))
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', {}, None)
            return _FakeResp()

        with patch('scripts.revalidate_drive_links.urllib.request.urlopen', side_effect=fake_urlopen):
            data = api_json(
                'https://example.invalid/test',
                headers={'Authorization': 'Bearer old-tok'},
                token_refresher=lambda: 'new-tok',
            )
        self.assertEqual({'ok': True}, data)
        self.assertEqual(['Bearer old-tok', 'Bearer new-tok'], calls)

        # Cancelamento durante retry de 503 deve interromper imediatamente.
        cancel_state = {'stop': False}

        def failing_503(req, timeout=90):
            cancel_state['stop'] = True
            raise urllib.error.HTTPError(req.full_url, 503, 'Unavailable', {}, None)

        with patch('scripts.revalidate_drive_links.urllib.request.urlopen', side_effect=failing_503):
            with self.assertRaises(RevalidationCancelled):
                api_json(
                    'https://example.invalid/test',
                    attempts=3,
                    cancel_check=lambda: cancel_state['stop'],
                )

    def test_phase1_proposal_records_mtime_and_missing_mtime_falls_back_to_rehash(self):
        data = b'123456'
        items = [
            self._drive_folder('f1', '2020', self.drive_id),
            self._drive_file('id1', 'foto.jpg', 'f1', data),
        ]
        lid, path = self._add_local(r'2020\foto.jpg', data, drive_id='id_velho')
        local_links, _ = read_local_links(self.cursor)
        winner = local_links[0][1]
        row = base_audit_rows(
            local_links, [], {i['id']: i for i in items}, remote_parent_paths(items, drive_id=self.drive_id),
            remote_tree=build_remote_tree(items, drive_id=self.drive_id), scan_roots=self.scan_roots,
        )[0][0]
        _, plan = build_atomic_relink_plan(
            [row], {'id1': winner}, {}, {i['id']: i for i in items},
            build_remote_tree(items, drive_id=self.drive_id), {lid: 'id_velho'},
            scan_roots=self.scan_roots,
        )
        prop = plan['components'][0]['relinks'][0]
        self.assertGreater(prop['disk_mtime_ns'], 0)

        # Mesmo que um plano antigo tenha disk_mtime_ns == 0, alterar o arquivo com mesmo tamanho deve falhar!
        prop_no_mtime = dict(prop, disk_mtime_ns=0)
        path.write_bytes(b'ABCDEF')
        ok, reason = _verify_live_disk_proposal(prop_no_mtime, allow_online=False, rehash=False)
        self.assertFalse(ok)
        self.assertEqual('hash_divergente_na_rechecagem', reason)

    def test_extract_local_rel_parts_unicode_nfd_and_auto_clear_skips_outside_root(self):
        root_nfd = r'L:\Drives Compartilhados\Sa' + '\u0303' + r'o Paulo'
        path_nfc = r'L:\Drives Compartilhados\São Paulo\foto.jpg'
        self.assertEqual(('foto.jpg',), extract_local_rel_parts(path_nfc, (root_nfd,)))

        # Registro fora da raiz configurada (ex.: unidade O:) nao deve ser limpo automaticamente; gera conflito seguro.
        data = b'arquivo-compartilhado-fora-raiz'
        items = [
            self._drive_folder('f1', '2020', self.drive_id),
            self._drive_file('id1', 'foto.jpg', 'f1', data),
        ]
        lid_in, _ = self._add_local(r'2020\foto.jpg', data, drive_id='id_velho')
        self.cursor.execute(
            "INSERT INTO files VALUES ('o_row', 'foto.jpg', 'O:\\Backup\\foto.jpg', ?, 'local', ?)",
            (len(data), 'https://drive.google.com/file/d/id1/view'),
        )
        rows, plan, _, _, _ = run_audit_pipeline(
            self.connection, items, self.drive_id, self.scan_roots, hash_mode='suspects',
        )
        by_lid = {r.local_file_id: r for r in rows}
        self.assertEqual('relink_conflict', by_lid[lid_in].status)
        self.assertEqual([], plan['components'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
