"""
Módulo path_reconciler

Responsável por:
- Detectar e reconciliar arquivos e pastas que foram movidos no disco local
  (pelo Windows Explorer ou Google Drive Desktop), atualizando seus caminhos
  (`file_id`, `path`, `parentId`, `name`) no SQLite sem perder tags, descrições,
  vínculos com o Drive ou miniaturas em cache.
- Auto-curar caminhos obsoletos sob demanda (ex: ao pressionar Barra de Espaço no
  Quick Preview, ao abrir o Painel de Detalhes, ou ao navegar pelas pastas).
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import shutil
import time
from datetime import datetime, timezone
from PyQt6.QtCore import QObject, pyqtSignal

from src.database.database import FileIndexer, to_canonical_id
from src.database.search import SearchEngine
from src.drive.match import normalize_aggressive
from src.utils.utils import extrair_ano_banco_imagens


THUMBNAIL_CACHE_DIR = "assets/thumbnail_cache"
_YEAR_RE = re.compile(r"[\\/](19\d\d|20\d\d)(?:[\\/]|$)")


def _extract_year(path_str: str) -> str | None:
    if not path_str:
        return None
    m = _YEAR_RE.search(path_str)
    return m.group(1) if m else None


def _path_similarity_score(old_path: str, new_path: str) -> int:
    score = 0
    old_year = _extract_year(old_path)
    new_year = _extract_year(new_path)
    if old_year and new_year and old_year == new_year:
        score += 100
    old_parts = set(
        os.path.normcase(os.path.normpath(os.path.dirname(old_path))).split(os.sep)
    )
    new_parts = set(
        os.path.normcase(os.path.normpath(os.path.dirname(new_path))).split(os.sep)
    )
    score += len(old_parts & new_parts) * 10
    return score


def _compute_local_thumb_key(path: str, mtime: int | str, size: int | str, rotation: int = 0) -> str:
    rot = int(rotation or 0) % 360
    if rot not in (0, 90, 180, 270):
        rot = 0
    raw = f"{path}|{mtime}|{size}|rotation={rot}"
    return hashlib.sha1(raw.encode()).hexdigest()


def migrate_thumbnail_cache_file(
    old_path: str,
    old_mtime: int,
    old_size: int,
    new_path: str,
    new_mtime: int,
    new_size: int,
    rotation: int = 0,
    existing_thumbs: set[str] | None = None,
) -> str | None:
    """Copia a miniatura em cache da chave antiga para a chave do novo caminho."""
    try:
        if existing_thumbs is not None and not existing_thumbs:
            return None
        if existing_thumbs is None and not os.path.isdir(THUMBNAIL_CACHE_DIR):
            return None
        new_key = _compute_local_thumb_key(new_path, new_mtime, new_size, rotation)
        for old_p in {old_path, os.path.normpath(old_path), os.path.normcase(os.path.normpath(old_path))}:
            old_key = _compute_local_thumb_key(old_p, old_mtime, old_size, rotation)
            if old_key == new_key:
                continue
            for ext in ("png", "jpg"):
                old_fname = f"{old_key}.{ext}"
                if existing_thumbs is not None:
                    if old_fname not in existing_thumbs:
                        continue
                else:
                    if not os.path.exists(os.path.join(THUMBNAIL_CACHE_DIR, old_fname)):
                        continue
                src_thumb = os.path.join(THUMBNAIL_CACHE_DIR, old_fname)
                new_fname = f"{new_key}.{ext}"
                dst_thumb = os.path.join(THUMBNAIL_CACHE_DIR, new_fname)
                if existing_thumbs is not None:
                    if new_fname not in existing_thumbs:
                        shutil.copy2(src_thumb, dst_thumb)
                        existing_thumbs.add(new_fname)
                elif not os.path.exists(dst_thumb):
                    shutil.copy2(src_thumb, dst_thumb)
                return dst_thumb
    except Exception as exc:
        logging.debug("Falha ao migrar cache de thumbnail: %s", exc)
    return None


def heal_single_record_in_db(
    cursor,
    conn,
    old_file_id: str,
    new_disk_path: str,
    norm_engine: SearchEngine | None = None,
    commit: bool = True,
) -> bool:
    """Atualiza o registro local de `old_file_id` para `new_disk_path` preservando metadados."""
    try:
        if not os.path.exists(new_disk_path):
            return False
        canon_new_id = to_canonical_id(new_disk_path)
        new_name = os.path.basename(new_disk_path)
        new_parent = to_canonical_id(os.path.dirname(new_disk_path))
        st = os.stat(new_disk_path)
        new_size = int(st.st_size)
        new_mtime = int(st.st_mtime)

        cursor.execute(
            """SELECT file_id, name, path, mimeType, description, thumbnailLink,
                      thumbnailPath, size, modifiedTime, createdTime, parentId,
                      webContentLink, starred, thumbnailRotation
               FROM files WHERE file_id = ? LIMIT 1""",
            (old_file_id,),
        )
        old_row = cursor.fetchone()
        if not old_row:
            return False

        (
            _fid, _old_name, old_path, old_mime, old_desc, old_tlink,
            old_tpath, old_size, old_mtime, old_ctime, _old_parent,
            old_wlink, old_starred, old_rot
        ) = old_row

        migrate_thumbnail_cache_file(
            old_path or old_file_id,
            old_mtime or 0,
            old_size or 0,
            canon_new_id,
            new_mtime,
            new_size,
            old_rot or 0,
        )

        if norm_engine is None:
            norm_engine = SearchEngine(None)
        name_norm = norm_engine.normalize_text(new_name)
        name_agg = normalize_aggressive(new_name)
        desc_norm = norm_engine.normalize_text(old_desc or "")

        if canon_new_id == old_file_id:
            cursor.execute(
                """UPDATE files
                   SET name = ?, path = ?, parentId = ?, size = ?, modifiedTime = ?,
                       name_normalized = ?, name_aggressive = ?
                   WHERE file_id = ?""",
                (new_name, canon_new_id, new_parent, new_size, new_mtime, name_norm, name_agg, old_file_id),
            )
            if commit and conn:
                conn.commit()
            return True

        # Verificar se já existe um registro no destino
        cursor.execute(
            """SELECT description, thumbnailLink, thumbnailPath, webContentLink, starred, thumbnailRotation
               FROM files WHERE file_id = ? LIMIT 1""",
            (canon_new_id,),
        )
        existing_target = cursor.fetchone()
        if existing_target:
            t_desc, t_tlink, t_tpath, t_wlink, t_star, t_rot = existing_target
            merged_desc = t_desc if (t_desc and t_desc.strip()) else (old_desc or "")
            merged_tlink = t_tlink or old_tlink or ""
            merged_tpath = t_tpath or old_tpath or ""
            merged_wlink = t_wlink or old_wlink
            merged_star = max(int(t_star or 0), int(old_starred or 0))
            merged_rot = int(t_rot or 0) if int(t_rot or 0) else int(old_rot or 0)
            merged_desc_norm = norm_engine.normalize_text(merged_desc)

            cursor.execute(
                """UPDATE files
                   SET name = ?, path = ?, parentId = ?, size = ?, modifiedTime = ?,
                       description = ?, thumbnailLink = ?, thumbnailPath = ?,
                       webContentLink = ?, starred = ?, thumbnailRotation = ?,
                       name_normalized = ?, name_aggressive = ?
                   WHERE file_id = ?""",
                (
                    new_name, canon_new_id, new_parent, new_size, new_mtime,
                    merged_desc, merged_tlink, merged_tpath,
                    merged_wlink, merged_star, merged_rot,
                    name_norm, name_agg, canon_new_id,
                ),
            )
            cursor.execute(
                """UPDATE search_index
                   SET name = ?, description = ?, normalized_name = ?, normalized_description = ?
                   WHERE file_id = ?""",
                (new_name, merged_desc, name_norm, merged_desc_norm, canon_new_id),
            )
            cursor.execute("DELETE FROM files WHERE file_id = ?", (old_file_id,))
            cursor.execute("DELETE FROM search_index WHERE file_id = ?", (old_file_id,))
        else:
            cursor.execute(
                """UPDATE files
                   SET file_id = ?, name = ?, path = ?, parentId = ?,
                       size = ?, modifiedTime = ?,
                       name_normalized = ?, name_aggressive = ?
                   WHERE file_id = ?""",
                (
                    canon_new_id, new_name, canon_new_id, new_parent,
                    new_size, new_mtime, name_norm, name_agg, old_file_id,
                ),
            )
            cursor.execute(
                """UPDATE search_index
                   SET file_id = ?, name = ?, normalized_name = ?
                   WHERE file_id = ?""",
                (canon_new_id, new_name, name_norm, old_file_id),
            )

        try:
            cursor.execute(
                "UPDATE OR IGNORE drive_link_validation SET local_file_id = ? WHERE local_file_id = ?",
                (canon_new_id, old_file_id),
            )
        except Exception:
            pass

        if commit and conn:
            conn.commit()
        return True
    except Exception as exc:
        logging.debug("Falha em heal_single_record_in_db(%s -> %s): %s", old_file_id, new_disk_path, exc)
        return False


def resolve_and_heal_local_path(file_item: dict | None, indexer: FileIndexer | None = None) -> str | None:
    """
    Resolve o caminho real em disco para `file_item`.
    Se o caminho salvo no banco estiver desatualizado (arquivo movido de pasta),
    localiza o arquivo no disco, atualiza `file_item` em memória e cura o registro no SQLite.
    """
    if not file_item or not isinstance(file_item, dict):
        return None

    for key in ("physical_path", "orig_path", "path"):
        candidate = file_item.get(key)
        if candidate and os.path.exists(candidate):
            return candidate

    old_path = file_item.get("path") or file_item.get("id") or ""
    name = (file_item.get("name") or os.path.basename(old_path) or "").strip()
    if not name:
        return None

    try:
        expected_size = int(file_item.get("size") or 0)
    except (TypeError, ValueError):
        expected_size = 0

    own_indexer = False
    if indexer is None:
        try:
            indexer = FileIndexer()
            own_indexer = True
        except Exception:
            indexer = None

    try:
        # 1. Consultar se já existe outro registro válido no banco com mesmo nome
        if indexer is not None:
            indexer.ensure_conn()
            indexer.cursor.execute(
                "SELECT file_id, path, size FROM files WHERE LOWER(name) = LOWER(?) AND source = 'local'",
                (name,),
            )
            for fid, db_path, db_size in indexer.cursor.fetchall():
                if db_path and os.path.exists(db_path):
                    if not expected_size or int(db_size or 0) == expected_size:
                        old_fid = file_item.get("file_id") or file_item.get("id") or to_canonical_id(old_path)
                        if old_fid and old_fid != fid:
                            heal_single_record_in_db(indexer.cursor, indexer.conn, old_fid, db_path)
                        file_item["path"] = db_path
                        file_item["physical_path"] = db_path
                        file_item["id"] = to_canonical_id(db_path)
                        return db_path

        # 2. Busca rápida no disco: primeiro na pasta do mesmo Ano, depois nas raízes configuradas
        from src.utils.config_manager import ConfigManager
        scan_roots = ConfigManager().get_resolved_scan_paths(persist=False) or []
        search_dirs = []

        year_str = _extract_year(old_path)
        for root in scan_roots:
            if year_str:
                year_dir = os.path.join(root, year_str)
                if os.path.isdir(year_dir):
                    search_dirs.append(year_dir)
            if os.path.isdir(root):
                search_dirs.append(root)

        target_name_lower = name.lower()
        target_agg = normalize_aggressive(name)
        target_ext = os.path.splitext(name)[1].lower()

        visited_dirs = set()
        matches = []

        for base_dir in search_dirs:
            for root, _dirs, files in os.walk(base_dir):
                norm_root = os.path.normcase(os.path.normpath(root))
                if norm_root in visited_dirs:
                    continue
                visited_dirs.add(norm_root)

                for fname in files:
                    if fname.lower() == "desktop.ini":
                        continue
                    f_lower = fname.lower()
                    is_name_match = (f_lower == target_name_lower)
                    is_agg_match = False
                    if not is_name_match and os.path.splitext(fname)[1].lower() == target_ext:
                        if normalize_aggressive(fname) == target_agg:
                            is_agg_match = True

                    if is_name_match or is_agg_match:
                        full_p = os.path.join(root, fname)
                        try:
                            sz = os.path.getsize(full_p)
                        except OSError:
                            continue
                        if not expected_size or sz == expected_size:
                            score = _path_similarity_score(old_path, full_p) + (50 if is_name_match else 0)
                            matches.append((score, full_p))

            if matches:
                # Se já encontrou dentro da pasta do mesmo ano, não precisa varrer todos os outros anos
                break

        if matches:
            matches.sort(key=lambda x: x[0], reverse=True)
            best_path = matches[0][1]
            canon_best = to_canonical_id(best_path)
            if indexer is not None:
                old_fid = file_item.get("file_id") or file_item.get("id") or to_canonical_id(old_path)
                if old_fid:
                    heal_single_record_in_db(indexer.cursor, indexer.conn, old_fid, best_path)
            file_item["path"] = canon_best
            file_item["physical_path"] = best_path
            file_item["id"] = canon_best
            file_item["parentId"] = to_canonical_id(os.path.dirname(best_path))
            return best_path

    except Exception as exc:
        logging.debug("Erro em resolve_and_heal_local_path: %s", exc)
    finally:
        if own_indexer and indexer is not None:
            try:
                indexer.close()
            except Exception:
                pass

    return None


def find_relocated_record_for_disk_entry(cursor, name: str, size: int, entry_path: str):
    """
    Procura no SQLite um registro `source='local'` cujo `path` antigo não existe mais
    no disco e que corresponda ao arquivo recém-encontrado em `entry_path`.
    Retorna a tupla `(file_id, path, description, thumbnailLink, thumbnailPath, webContentLink, starred, thumbnailRotation, modifiedTime, size)`
    ou `None`.
    """
    try:
        cursor.execute(
            """SELECT file_id, path, description, thumbnailLink, thumbnailPath,
                      webContentLink, starred, thumbnailRotation, modifiedTime, size
               FROM files
               WHERE (LOWER(name) = LOWER(?) OR name_aggressive = ?)
                 AND size = ?
                 AND source = 'local'
                 AND mimeType != 'folder'""",
            (name, normalize_aggressive(name), size),
        )
        candidates = []
        for row in cursor.fetchall():
            fid, old_p = row[0], row[1]
            if old_p and not os.path.exists(old_p):
                score = _path_similarity_score(old_p, entry_path)
                # Priorizar registros que possuem tags/descrição
                if (row[2] or "").strip():
                    score += 500
                candidates.append((score, row))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
    except Exception as exc:
        logging.debug("Erro em find_relocated_record_for_disk_entry: %s", exc)
    return None


def reconcile_moved_paths(
    db_name: str,
    scan_paths: list[str] | str,
    progress_callback=None,
    status_callback=None,
    cancel_check=None,
) -> dict:
    """
    Executa a reconciliação completa de caminhos locais movidos e pastas no banco SQLite.
    Usa mapeamento em memória do cache de thumbnails e `rowid` do FTS5 para máxima velocidade.
    """
    t0 = time.perf_counter()
    if isinstance(scan_paths, str):
        scan_paths = [scan_paths]

    valid_roots = [p for p in scan_paths if p and os.path.exists(p)]
    if not valid_roots:
        return {
            "relocated": 0,
            "merged": 0,
            "new_files": 0,
            "new_folders": 0,
            "removed_folders": 0,
            "removed_ghosts": 0,
            "elapsed_sec": 0.0,
        }

    # Criar backup rápido antes de alterar o banco
    if os.path.isfile(db_name):
        try:
            backup_dir = os.path.join(os.path.dirname(db_name) or "data", "backups")
            os.makedirs(backup_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = os.path.join(backup_dir, f"file_index_pre_reconcile_{stamp}.db")
            shutil.copy2(db_name, backup_file)
            existing_backups = sorted(
                [
                    os.path.join(backup_dir, f)
                    for f in os.listdir(backup_dir)
                    if f.startswith("file_index_pre_reconcile_") and f.endswith(".db")
                ]
            )
            for old_b in existing_backups[:-3]:
                try:
                    os.remove(old_b)
                except OSError:
                    pass
        except Exception as exc:
            logging.warning("Não foi possível criar backup pré-reconciliação: %s", exc)

    if status_callback:
        status_callback("Lendo estrutura de pastas e arquivos no disco...")
    if progress_callback:
        progress_callback(5, "Lendo estrutura de pastas e arquivos no disco...")

    disk_folders = {}  # canon_dir_path -> (name, canon_dir_path, parent_id, mtime, ctime)
    disk_files = {}    # canon_file_path -> (name, full_path, canon_file_path, parent_id, size, mtime, ctime)

    scanned_count = 0
    for scan_root in valid_roots:
        canon_root = to_canonical_id(scan_root)
        for root, dirs, files in os.walk(scan_root):
            if cancel_check and cancel_check():
                raise RuntimeError("Operação cancelada pelo usuário.")
            canon_curr = to_canonical_id(root)

            for dname in dirs:
                full_d = os.path.join(root, dname)
                canon_d = to_canonical_id(full_d)
                parent_d = "" if canon_curr == canon_root else canon_curr
                try:
                    st = os.stat(full_d)
                    disk_folders[canon_d] = (dname, canon_d, parent_d, int(st.st_mtime), int(st.st_ctime))
                except OSError:
                    continue

            for fname in files:
                if fname.lower() == "desktop.ini":
                    continue
                full_f = os.path.join(root, fname)
                canon_f = to_canonical_id(full_f)
                parent_f = "" if canon_curr == canon_root else canon_curr
                try:
                    st = os.stat(full_f)
                    disk_files[canon_f] = (
                        fname,
                        full_f,
                        canon_f,
                        parent_f,
                        int(st.st_size),
                        int(st.st_mtime),
                        int(st.st_ctime),
                    )
                except OSError:
                    continue

                scanned_count += 1
                if scanned_count % 5000 == 0 and progress_callback:
                    pct = min(35, 5 + int(scanned_count / 4000))
                    progress_callback(pct, f"Lendo disco: {scanned_count:,} arquivos encontrados...")

    if progress_callback:
        progress_callback(40, "Analisando registros do banco de dados...")

    existing_thumbs: set[str] = set()
    if os.path.isdir(THUMBNAIL_CACHE_DIR):
        try:
            existing_thumbs = set(os.listdir(THUMBNAIL_CACHE_DIR))
        except OSError:
            existing_thumbs = set()

    indexer = FileIndexer(db_name)
    norm_engine = SearchEngine(None)
    try:
        cursor = indexer.cursor
        # Mapear rowid do FTS5 em memória para atualizações/remoções O(1)
        cursor.execute("SELECT rowid, file_id FROM search_index")
        fts_rowid_by_fid = {fid: r_id for r_id, fid in cursor.fetchall()}

        cursor.execute(
            """SELECT file_id, name, path, mimeType, description, thumbnailLink,
                      thumbnailPath, size, modifiedTime, createdTime, parentId,
                      webContentLink, starred, thumbnailRotation
               FROM files WHERE source = 'local'"""
        )
        all_local_rows = cursor.fetchall()

        db_folders = {}
        existing_db_files = {}
        stale_db_files = []

        for row in all_local_rows:
            fid = row[0]
            mime = row[3]
            if mime == "folder":
                db_folders[fid] = row
            else:
                if fid in disk_files:
                    existing_db_files[fid] = row
                else:
                    stale_db_files.append(row)

        # 1. Reconciliar pastas (adicionar pastas novas e remover pastas inexistentes)
        new_folder_items = []
        for canon_d, (dname, _cd, parent_d, mtime, ctime) in disk_folders.items():
            if canon_d not in db_folders:
                data_mais_antiga = min(ctime, mtime)
                ano_caminho = extrair_ano_banco_imagens(canon_d)
                if ano_caminho and ano_caminho < datetime.fromtimestamp(data_mais_antiga).year:
                    data_final = int(datetime(ano_caminho, 1, 1, tzinfo=timezone.utc).timestamp())
                else:
                    data_final = data_mais_antiga
                new_folder_items.append({
                    "id": canon_d,
                    "name": dname,
                    "path": canon_d,
                    "mimeType": "folder",
                    "source": "local",
                    "description": "",
                    "thumbnailLink": "",
                    "thumbnailPath": "",
                    "size": 0,
                    "modifiedTime": mtime,
                    "createdTime": data_final,
                    "parentId": parent_d,
                    "webContentLink": None,
                })

        obsolete_folder_ids = [
            fid for fid in db_folders if fid not in disk_folders
        ]

        if new_folder_items:
            indexer.save_files_in_batch(new_folder_items, source="local")

        if obsolete_folder_ids:
            fts_rowids_to_del = [
                (fts_rowid_by_fid[fid],) for fid in obsolete_folder_ids if fid in fts_rowid_by_fid
            ]
            for i in range(0, len(obsolete_folder_ids), 500):
                batch = obsolete_folder_ids[i:i + 500]
                placeholders = ",".join("?" for _ in batch)
                cursor.execute(f"DELETE FROM files WHERE file_id IN ({placeholders})", batch)
            if fts_rowids_to_del:
                cursor.executemany("DELETE FROM search_index WHERE rowid = ?", fts_rowids_to_del)
            indexer.conn.commit()

        if progress_callback:
            progress_callback(55, f"Casando {len(stale_db_files):,} registros movidos com o disco...")

        # 2. Separar arquivos em disco ainda não vinculados a registros existentes
        unclaimed_disk = {
            canon_f: info
            for canon_f, info in disk_files.items()
            if canon_f not in existing_db_files
        }

        unclaimed_by_name_size = {}
        unclaimed_by_agg_size = {}
        unclaimed_by_name_year = {}

        for canon_f, info in unclaimed_disk.items():
            fname, _full_f, _cf, _pf, fsize, _mtime, _ctime = info
            ext = os.path.splitext(fname)[1].lower()
            unclaimed_by_name_size.setdefault((fname.lower(), fsize), []).append(canon_f)
            unclaimed_by_agg_size.setdefault((normalize_aggressive(fname), ext, fsize), []).append(canon_f)
            yr = _extract_year(canon_f)
            if yr:
                unclaimed_by_name_year.setdefault((fname.lower(), yr), []).append(canon_f)

        claimed_canon_paths = set()
        matched_stale_indices = set()
        relocations = []  # (stale_row, disk_info)

        # Passada 1: Nome exato (case-insensitive) + tamanho exato + score de caminho/ano
        stale_by_name_size = {}
        for idx, srow in enumerate(stale_db_files):
            key = ((srow[1] or "").lower(), int(srow[7] or 0))
            stale_by_name_size.setdefault(key, []).append(idx)

        for key, s_indices in stale_by_name_size.items():
            cands = [c for c in unclaimed_by_name_size.get(key, ()) if c not in claimed_canon_paths]
            if not cands:
                continue
            if len(s_indices) == 1 and len(cands) == 1:
                s_idx = s_indices[0]
                c_path = cands[0]
                matched_stale_indices.add(s_idx)
                claimed_canon_paths.add(c_path)
                relocations.append((stale_db_files[s_idx], disk_files[c_path]))
                continue

            pairs = []
            for s_idx in s_indices:
                old_p = stale_db_files[s_idx][2] or stale_db_files[s_idx][0]
                has_tags_bonus = 500 if (stale_db_files[s_idx][4] or "").strip() else 0
                for c_path in cands:
                    sc = _path_similarity_score(old_p, c_path) + has_tags_bonus
                    pairs.append((sc, s_idx, c_path))
            pairs.sort(key=lambda x: x[0], reverse=True)
            for _sc, s_idx, c_path in pairs:
                if s_idx not in matched_stale_indices and c_path not in claimed_canon_paths:
                    matched_stale_indices.add(s_idx)
                    claimed_canon_paths.add(c_path)
                    relocations.append((stale_db_files[s_idx], disk_files[c_path]))

        # Passada 2: Nome normalizado agressivo (ex: "(1)" -> "_1") + extensão + tamanho exato
        for idx, srow in enumerate(stale_db_files):
            if idx in matched_stale_indices:
                continue
            sname = srow[1] or ""
            ext = os.path.splitext(sname)[1].lower()
            ssize = int(srow[7] or 0)
            agg_key = (normalize_aggressive(sname), ext, ssize)
            cands = [c for c in unclaimed_by_agg_size.get(agg_key, ()) if c not in claimed_canon_paths]
            if not cands:
                continue
            old_p = srow[2] or srow[0]
            cands.sort(key=lambda c: _path_similarity_score(old_p, c), reverse=True)
            best_c = cands[0]
            matched_stale_indices.add(idx)
            claimed_canon_paths.add(best_c)
            relocations.append((srow, disk_files[best_c]))

        # Passada 3: Mesmo nome exato no mesmo Ano (quando houve pequena variação de metadados/EXIF)
        for idx, srow in enumerate(stale_db_files):
            if idx in matched_stale_indices:
                continue
            sname_lower = (srow[1] or "").lower()
            old_p = srow[2] or srow[0]
            yr = _extract_year(old_p)
            if not yr:
                continue
            cands = [c for c in unclaimed_by_name_year.get((sname_lower, yr), ()) if c not in claimed_canon_paths]
            if len(cands) == 1:
                best_c = cands[0]
                matched_stale_indices.add(idx)
                claimed_canon_paths.add(best_c)
                relocations.append((srow, disk_files[best_c]))

        # Passada 4: Verificar se algum registro obsoleto corresponde a um arquivo que JÁ está no banco no novo caminho (fusão de duplicatas)
        relocated_target_rows = {}
        for srow, dinfo in relocations:
            new_name, _full_f, canon_new_id, new_parent, new_size, new_mtime, _new_ctime = dinfo
            relocated_target_rows[canon_new_id] = (
                canon_new_id, new_name, canon_new_id, srow[3], srow[4], srow[5],
                srow[6], new_size, new_mtime, srow[9], new_parent, srow[11], srow[12], srow[13],
            )

        existing_by_name_size = {}
        for canon_f, erow in existing_db_files.items():
            key = ((erow[1] or "").lower(), int(erow[7] or 0))
            existing_by_name_size.setdefault(key, []).append((canon_f, erow))
        for canon_f, rrow in relocated_target_rows.items():
            key = ((rrow[1] or "").lower(), int(rrow[7] or 0))
            existing_by_name_size.setdefault(key, []).append((canon_f, rrow))

        merges_to_apply = []  # (stale_row, target_existing_row)
        ghosts_to_delete = []

        for idx, srow in enumerate(stale_db_files):
            if idx in matched_stale_indices:
                continue
            key = ((srow[1] or "").lower(), int(srow[7] or 0))
            existing_cands = existing_by_name_size.get(key, ())
            if existing_cands:
                old_p = srow[2] or srow[0]
                best_target_id, best_target_row = max(
                    existing_cands, key=lambda item: _path_similarity_score(old_p, item[0])
                )
                merges_to_apply.append((srow, best_target_row))
                matched_stale_indices.add(idx)
            else:
                if not (srow[4] or "").strip():
                    ghosts_to_delete.append(srow[0])

        if progress_callback:
            progress_callback(70, f"Atualizando {len(relocations):,} caminhos movidos no banco...")

        # 3. Aplicar realocações em lote
        files_updates = []
        fts_updates_by_rowid = []
        fts_inserts = []
        link_updates = []

        for pos, (srow, dinfo) in enumerate(relocations, start=1):
            old_fid = srow[0]
            old_path = srow[2] or old_fid
            old_desc = srow[4] or ""
            old_size = int(srow[7] or 0)
            old_mtime = int(srow[8] or 0)
            old_rot = int(srow[13] or 0)

            new_name, _full_f, canon_new_id, new_parent, new_size, new_mtime, _new_ctime = dinfo

            migrate_thumbnail_cache_file(
                old_path, old_mtime, old_size, canon_new_id, new_mtime, new_size, old_rot,
                existing_thumbs=existing_thumbs,
            )

            name_norm = norm_engine.normalize_text(new_name)
            name_agg = normalize_aggressive(new_name)

            files_updates.append((
                canon_new_id, new_name, canon_new_id, new_parent,
                new_size, new_mtime, name_norm, name_agg, old_fid,
            ))
            fts_rowid = fts_rowid_by_fid.get(old_fid)
            if fts_rowid is not None:
                fts_updates_by_rowid.append((
                    canon_new_id, new_name, name_norm, fts_rowid,
                ))
            else:
                fts_inserts.append((
                    new_name, old_desc, name_norm, norm_engine.normalize_text(old_desc), canon_new_id, "local",
                ))
            link_updates.append((canon_new_id, old_fid))

            if pos % 5000 == 0 and progress_callback:
                pct = min(88, 70 + int(18 * pos / max(1, len(relocations))))
                progress_callback(pct, f"Preparando atualização: {pos:,}/{len(relocations):,} arquivos movidos...")

        if files_updates:
            cursor.executemany(
                """UPDATE OR REPLACE files
                   SET file_id = ?, name = ?, path = ?, parentId = ?,
                       size = ?, modifiedTime = ?,
                       name_normalized = ?, name_aggressive = ?
                   WHERE file_id = ?""",
                files_updates,
            )
            if fts_updates_by_rowid:
                cursor.executemany(
                    """UPDATE search_index
                       SET file_id = ?, name = ?, normalized_name = ?
                       WHERE rowid = ?""",
                    fts_updates_by_rowid,
                )
            if fts_inserts:
                cursor.executemany(
                    """INSERT INTO search_index (name, description, normalized_name, normalized_description, file_id, source)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    fts_inserts,
                )
            try:
                cursor.executemany(
                    "UPDATE OR IGNORE drive_link_validation SET local_file_id = ? WHERE local_file_id = ?",
                    link_updates,
                )
            except Exception:
                pass
            indexer.conn.commit()

        # 4. Aplicar fusões de duplicatas (quando o destino já tinha um registro local)
        merged_count = 0
        for srow, trow in merges_to_apply:
            old_fid = srow[0]
            target_fid = trow[0]
            old_desc = (srow[4] or "").strip()
            target_desc = (trow[4] or "").strip()

            final_desc = target_desc if target_desc else old_desc
            final_tlink = trow[5] or srow[5] or ""
            final_tpath = trow[6] or srow[6] or ""
            final_wlink = trow[11] or srow[11]
            final_star = max(int(trow[12] or 0), int(srow[12] or 0))
            final_rot = int(trow[13] or 0) if int(trow[13] or 0) else int(srow[13] or 0)

            if final_desc != target_desc or final_wlink != trow[11] or final_star != int(trow[12] or 0):
                norm_desc = norm_engine.normalize_text(final_desc)
                cursor.execute(
                    """UPDATE files
                       SET description = ?, thumbnailLink = ?, thumbnailPath = ?,
                           webContentLink = ?, starred = ?, thumbnailRotation = ?
                       WHERE file_id = ?""",
                    (final_desc, final_tlink, final_tpath, final_wlink, final_star, final_rot, target_fid),
                )
                target_rowid = fts_rowid_by_fid.get(target_fid)
                if target_rowid is not None:
                    cursor.execute(
                        """UPDATE search_index
                           SET description = ?, normalized_description = ?
                           WHERE rowid = ?""",
                        (final_desc, norm_desc, target_rowid),
                    )
            ghosts_to_delete.append(old_fid)
            merged_count += 1

        if ghosts_to_delete:
            ghost_fts_rowids = [
                (fts_rowid_by_fid[fid],) for fid in ghosts_to_delete if fid in fts_rowid_by_fid
            ]
            for i in range(0, len(ghosts_to_delete), 500):
                batch = ghosts_to_delete[i:i + 500]
                placeholders = ",".join("?" for _ in batch)
                cursor.execute(f"DELETE FROM files WHERE file_id IN ({placeholders})", batch)
            if ghost_fts_rowids:
                cursor.executemany("DELETE FROM search_index WHERE rowid = ?", ghost_fts_rowids)
            indexer.conn.commit()

        # 5. Indexar arquivos genuinamente novos no disco que ainda não estavam no banco
        if progress_callback:
            progress_callback(92, "Indexando novos arquivos encontrados no disco...")

        new_file_items = []
        for canon_f, info in unclaimed_disk.items():
            if canon_f in claimed_canon_paths:
                continue
            fname, _full_f, _cf, parent_f, fsize, mtime, ctime = info
            data_mais_antiga = min(ctime, mtime)
            ano_caminho = extrair_ano_banco_imagens(canon_f)
            if ano_caminho and ano_caminho < datetime.fromtimestamp(data_mais_antiga).year:
                data_final = int(datetime(ano_caminho, 1, 1, tzinfo=timezone.utc).timestamp())
            else:
                data_final = data_mais_antiga
            mimetypes.add_type('video/mp2t', '.mts')
            mimetypes.add_type('video/mp2t', '.m2ts')
            mimetypes.add_type('video/mp2t', '.ts')
            mime_guess, _ = mimetypes.guess_type(fname)
            new_file_items.append({
                "id": canon_f,
                "name": fname,
                "path": canon_f,
                "mimeType": mime_guess or "image/jpeg",
                "source": "local",
                "description": "",
                "thumbnailLink": "",
                "thumbnailPath": "",
                "size": fsize,
                "modifiedTime": mtime,
                "createdTime": data_final,
                "parentId": parent_f,
                "webContentLink": None,
            })

        if new_file_items:
            for i in range(0, len(new_file_items), 500):
                indexer.save_files_in_batch(new_file_items[i:i + 500], source="local")

        indexer.conn.commit()

        elapsed = time.perf_counter() - t0
        if progress_callback:
            progress_callback(100, "Reconciliação concluída com sucesso!")

        result = {
            "relocated": len(relocations),
            "merged": merged_count,
            "new_files": len(new_file_items),
            "new_folders": len(new_folder_items),
            "removed_folders": len(obsolete_folder_ids),
            "removed_ghosts": len(ghosts_to_delete) - merged_count,
            "elapsed_sec": round(elapsed, 2),
        }
        logging.info("🧭 Reconciliação de caminhos concluída: %s", result)
        return result
    finally:
        try:
            indexer.close()
        except Exception:
            pass


class PathReconcileWorker(QObject):
    progress_update = pyqtSignal(int, str)
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, db_name: str, scan_paths: list[str] | str):
        super().__init__()
        self.db_name = db_name
        self.scan_paths = scan_paths
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            res = reconcile_moved_paths(
                self.db_name,
                self.scan_paths,
                progress_callback=lambda pct, msg: self.progress_update.emit(pct, msg),
                cancel_check=lambda: self._cancelled,
            )
            self.finished.emit(res)
        except Exception as exc:
            logging.exception("Erro na reconciliação de caminhos")
            self.failed.emit(str(exc))
