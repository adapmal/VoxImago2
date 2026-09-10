"""
Módulo de banco de dados do VoxImago.MB

Responsável por:
- Gerenciar o banco SQLite do aplicativo
- Indexar, buscar e atualizar arquivos e metadados
- Fornecer a classe FileIndexer para operações CRUD, filtros e favoritos
"""

import unicodedata
import sqlite3
import logging
from src.database.search import SearchEngine
import os
import time
import shutil
import tempfile
import mimetypes

from src.database.snapshot import (
    SCHEMA_VERSION,
    build_root_mapping,
    create_manifest,
    infer_local_roots,
    publish_generation,
    rebase_local_paths,
    select_snapshot_database,
    sqlite_has_files_schema,
    sqlite_is_healthy,
)

THUMBNAIL_CACHE_DIR = "thumbnail_cache"


def to_canonical_id(fid: str | None) -> str | None:
    if not fid or not isinstance(fid, str):
        return fid
    if os.path.isabs(fid) or '\\' in fid or '/' in fid or ':' in fid:
        return os.path.normcase(os.path.normpath(fid))
    return fid


class FileIndexer:

    def buscar_drive_por_metadados(self, termo):
        query = "SELECT file_id, name, path, description, starred, mimeType, createdTime FROM files WHERE source = 'drive' AND (name LIKE ? OR description LIKE ?)"
        like_term = f"%{termo}%"
        self.cursor.execute(query, (like_term, like_term))
        resultados = []
        for row in self.cursor.fetchall():
            arquivo = {
                'id': row[0],
                'name': row[1],
                'local_path': row[2],
                'description': row[3],
                'starred': bool(row[4]),
                'mimeType': row[5],
                'createdTime': row[6],
            }
            arquivo['is_local'] = os.path.exists(
                arquivo['local_path']) if arquivo['local_path'] else False
            resultados.append(arquivo)
        return resultados

    @staticmethod
    def get_database_results(consulta, pagina, itens_por_pagina, termo_busca, tipo_filtro, db_path="data/file_index.db"):
        offset = (pagina - 1) * itens_por_pagina
        conn = open_db_for_thread(db_path)
        cur = conn.cursor()
        resultados = []
        for row in cur.fetchall():
            arquivo = dict(row)
            arquivo['file_exists'] = os.path.exists(
                arquivo['local_path']) if arquivo['local_path'] else False
            resultados.append(arquivo)
        conn.close()
        return resultados

    def __init__(self, db_name=None):
        from src.utils.config_manager import ConfigManager
        config_mgr = ConfigManager()
        if db_name is None:
            self.db_name = config_mgr.get_db_path()
        else:
            self.db_name = db_name

        db_dir = os.path.dirname(self.db_name)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            logging.info("Pasta de banco criada automaticamente: %s", db_dir)

        # Recuperacao estrutural. A decisao continua conservadora: um banco
        # apenas desatualizado nunca e sobrescrito automaticamente.
        needs_restore = False
        confirmed_corrupt = False
        restored_manifest = None
        restored_snapshot = None
        if not os.path.exists(self.db_name) or os.path.getsize(self.db_name) == 0:
            needs_restore = True
        else:
            test_conn = None
            try:
                test_conn = sqlite3.connect(self.db_name, timeout=5.0)
                test_cur = test_conn.cursor()
                test_cur.execute("PRAGMA integrity_check")
                res = test_cur.fetchone()
                if not res or res[0] != 'ok':
                    needs_restore = True
                    confirmed_corrupt = True
            except sqlite3.Error as exc:
                message = str(exc).lower()
                if 'locked' in message or 'busy' in message:
                    logging.warning(
                        "Banco temporariamente ocupado durante a verificacao: %s",
                        exc,
                    )
                else:
                    needs_restore = True
                    confirmed_corrupt = True
            except Exception as exc:
                logging.warning(
                    "Nao foi possivel verificar o banco local: %s", exc
                )
            finally:
                if test_conn is not None:
                    test_conn.close()

        if needs_restore and not config_mgr.is_sandbox():
            s_path, manifest, selection_reason = select_snapshot_database(
                config_mgr.get_shared_cache_candidates(SCHEMA_VERSION),
                max_schema_version=SCHEMA_VERSION,
            )
            if not s_path:
                logging.warning(
                    "[AUTORECOVERY] Restauracao compartilhada indisponivel: %s",
                    selection_reason,
                )
            else:
                try:
                    healthy, reason = sqlite_is_healthy(s_path)
                    if not healthy or not sqlite_has_files_schema(s_path):
                        raise RuntimeError(
                            'Snapshot nao e um banco VoxImago utilizavel: '
                            f'{reason}'
                        )

                    logging.info(
                        "[AUTORECOVERY] Banco local corrompido, vazio ou ausente. "
                        "Restaurando snapshot verificado (%s)...", s_path,
                    )
                    for ext in ["-wal", "-shm"]:
                        wal_path = self.db_name + ext
                        if os.path.exists(wal_path):
                            try:
                                os.remove(wal_path)
                            except OSError:
                                pass

                    if os.path.isfile(self.db_name) and os.path.getsize(self.db_name) > 0:
                        corrupt_copy = (
                            f"{self.db_name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
                        )
                        shutil.copy2(self.db_name, corrupt_copy)

                    restore_tmp = self.db_name + f'.restore-{os.getpid()}.tmp'
                    shutil.copy2(s_path, restore_tmp)
                    copied_ok, copied_reason = sqlite_is_healthy(restore_tmp)
                    if not copied_ok:
                        raise RuntimeError(
                            f'Copia local do snapshot falhou na validacao: {copied_reason}'
                        )
                    os.replace(restore_tmp, self.db_name)
                    restored_snapshot = s_path
                    restored_manifest = manifest
                    logging.info(
                        "[OK] Banco local restaurado a partir do snapshot verificado."
                    )
                except Exception as e_copy:
                    logging.warning(
                        "[AVISO] Nao foi possivel restaurar snapshot %s: %s",
                        s_path, e_copy,
                    )
                    restore_tmp = self.db_name + f'.restore-{os.getpid()}.tmp'
                    if os.path.exists(restore_tmp):
                        try:
                            os.remove(restore_tmp)
                        except OSError:
                            pass

        if confirmed_corrupt and not restored_snapshot and os.path.isfile(self.db_name):
            # Manter o arquivo integralmente recuperavel e permitir que o app
            # abra um banco novo para posterior scan/sync. Nunca apagar a unica
            # copia corrompida.
            corrupt_copy = (
                f"{self.db_name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
            )
            os.replace(self.db_name, corrupt_copy)
            for extension in ('-wal', '-shm'):
                sidecar = self.db_name + extension
                if os.path.exists(sidecar):
                    try:
                        os.replace(sidecar, corrupt_copy + extension)
                    except OSError:
                        pass
            logging.error(
                "[AUTORECOVERY] Snapshot indisponivel. Banco corrompido "
                "preservado em %s; um indice local novo sera criado.",
                corrupt_copy,
            )

        self.conn = sqlite3.connect(self.db_name, check_same_thread=False, timeout=30.0)
        self.conn.create_function("py_lower", 1, lambda s: s.lower() if s else s)
        self.cursor = self.conn.cursor()
        self.cursor.execute("PRAGMA journal_mode=WAL")
        self.cursor.execute("PRAGMA busy_timeout=30000")
        self._create_tables()
        if restored_snapshot:
            target_roots = config_mgr.get_resolved_scan_paths(persist=True)
            source_roots = list((restored_manifest or {}).get('scan_roots') or [])
            if not source_roots:
                source_roots = infer_local_roots(self.conn)
            for source_root, target_root in build_root_mapping(
                    source_roots, target_roots):
                changed = rebase_local_paths(
                    self.conn, source_root, target_root
                )
                logging.info(
                    "[AUTORECOVERY] %d caminhos remapeados: %s -> %s",
                    changed, source_root, target_root,
                )
            if restored_manifest and restored_manifest.get('generation'):
                config_mgr.set(
                    'shared_snapshot_generation',
                    str(restored_manifest['generation']),
                )
        self._count_cache = {}
        self._paged_cache = {}
        self._auto_rebuild_search_index()

    def _auto_rebuild_search_index(self):
        try:
            self.cursor.execute("PRAGMA table_info(search_index)")
            columns = [row[1] for row in self.cursor.fetchall()]
            expected = {'name', 'description', 'normalized_name',
                        'normalized_description', 'file_id', 'source'}
            if not expected.issubset(set(columns)):
                logging.info(
                    "Recriando indice de busca para compatibilidade hibrida.")
                self.rebuild_search_index_with_normalization()

            self._populate_normalized_columns()
            consistent, reason = self.search_index_is_consistent()
            if not consistent:
                logging.warning(
                    "Indice de busca inconsistente (%s). Use Sistema / "
                    "Avancado > Saude e reparo do banco.",
                    reason,
                )
        except Exception as e:
            logging.warning("Erro ao verificar/recriar indice de busca: %s", e)

    def search_index_is_consistent(self):
        """Valida cardinalidade e IDs do FTS sem alterar o banco."""
        self.ensure_conn()
        try:
            files_count = self.cursor.execute(
                'SELECT COUNT(*) FROM files'
            ).fetchone()[0]
            index_count = self.cursor.execute(
                'SELECT COUNT(*) FROM search_index'
            ).fetchone()[0]
            if files_count != index_count:
                return False, (
                    f'arquivos={files_count}, indice={index_count}'
                )
            missing = self.cursor.execute(
                'SELECT file_id FROM files '
                'EXCEPT SELECT file_id FROM search_index LIMIT 1'
            ).fetchone()
            extra = self.cursor.execute(
                'SELECT file_id FROM search_index '
                'EXCEPT SELECT file_id FROM files LIMIT 1'
            ).fetchone()
            if missing or extra:
                return False, 'IDs ausentes ou extras no indice'
            return True, 'ok'
        except Exception as exc:
            return False, str(exc)

    def _populate_normalized_columns(self):
        try:
            self.cursor.execute("PRAGMA table_info(files)")
            columns = [row[1] for row in self.cursor.fetchall()]

            if 'name_normalized' not in columns or 'name_aggressive' not in columns:
                logging.info(
                    "Colunas normalizadas serao criadas automaticamente.")
                return

            self.cursor.execute(
                "SELECT COUNT(*) FROM files WHERE name_normalized IS NULL OR name_aggressive IS NULL")
            null_count = self.cursor.fetchone()[0]

            if null_count > 0:
                logging.info(
                    "Populando %d registros normalizados para matching.",
                    null_count,
                )

                self.cursor.execute(
                    "SELECT file_id, name FROM files WHERE name_normalized IS NULL OR name_aggressive IS NULL")
                files_to_update = self.cursor.fetchall()

                from src.drive.match import normalize_aggressive
                updates = []

                for file_id, name in files_to_update:
                    if name:
                        name_normalized = SearchEngine(
                            None).normalize_text(name)
                        name_aggressive = normalize_aggressive(name)
                        updates.append(
                            (name_normalized, name_aggressive, file_id))

                if updates:
                    self.cursor.executemany(
                        "UPDATE files SET name_normalized = ?, name_aggressive = ? WHERE file_id = ?",
                        updates
                    )
                    self.conn.commit()
                    logging.info(
                        "%d registros normalizados atualizados.", len(updates)
                    )
        except Exception as e:
            logging.warning("Erro ao popular colunas normalizadas: %s", e)

    def ensure_conn(self):
        if self.conn is None:
            self.conn = sqlite3.connect(self.db_name, check_same_thread=False, timeout=30.0)
            self.conn.create_function("py_lower", 1, lambda s: s.lower() if s else s)
            self.cursor = self.conn.cursor()
            self.cursor.execute("PRAGMA busy_timeout=30000")
        try:
            self.cursor.execute("SELECT 1")
        except sqlite3.ProgrammingError as e:
            if "closed" in str(e) or "thread" in str(e):
                self.conn = sqlite3.connect(self.db_name, check_same_thread=False, timeout=30.0)
                self.conn.create_function("py_lower", 1, lambda s: s.lower() if s else s)
                self.cursor = self.conn.cursor()
                self.cursor.execute("PRAGMA busy_timeout=30000")

    def _create_tables(self):
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS files (
                file_id TEXT PRIMARY KEY,
                name TEXT,
                path TEXT,
                mimeType TEXT,
                source TEXT,
                description TEXT,
                thumbnailLink TEXT,
                thumbnailPath TEXT,
                size INTEGER,
                modifiedTime INTEGER,
                createdTime INTEGER,
                parentId TEXT,
                webContentLink TEXT,
                starred INTEGER DEFAULT 0,
                thumbnailRotation INTEGER DEFAULT 0
            )
        ''')

        self._migrate_add_normalized_columns()
        from src.drive.link_validation import ensure_validation_table
        ensure_validation_table(self.cursor)
        self.cursor.execute('''
                            CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
                                name,
                                description,
                                normalized_name,
                                normalized_description,
                                file_id UNINDEXED,
                                source UNINDEXED,
                                tokenize="trigram"
                            )
                            ''')
        self.cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_files_source ON files(source)')
        self.cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_files_parentId ON files(parentId)')
        self.cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_files_mimeType ON files(mimeType)')
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_starred ON files(starred)")
        self.cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_files_name ON files(name COLLATE NOCASE)')
        self.cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_files_modifiedTime ON files(modifiedTime)')
        self.cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_files_parent_source_created ON files(parentId, source, createdTime DESC)')
        self.cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_files_parent_source_name ON files(parentId, source, name COLLATE NOCASE)')

        self._create_performance_indices()

        self.cursor.execute("PRAGMA mmap_size=268435456")
        self.cursor.execute("PRAGMA journal_mode=WAL")
        self.cursor.execute("PRAGMA synchronous=NORMAL")
        self.cursor.execute("PRAGMA temp_store=MEMORY")
        self.cursor.execute("PRAGMA cache_size=5000")
        self.conn.commit()
        logging.info("Indice de resultados trigram verificado.")

    def _migrate_add_normalized_columns(self):
        try:
            self.cursor.execute("PRAGMA table_info(files)")
            columns = [row[1] for row in self.cursor.fetchall()]

            if 'name_normalized' not in columns:
                logging.info("Adicionando coluna name_normalized.")
                self.cursor.execute(
                    "ALTER TABLE files ADD COLUMN name_normalized TEXT")

            if 'name_aggressive' not in columns:
                logging.info("Adicionando coluna name_aggressive.")
                self.cursor.execute(
                    "ALTER TABLE files ADD COLUMN name_aggressive TEXT")

            if 'thumbnailRotation' not in columns:
                logging.info("Adicionando coluna thumbnailRotation.")
                self.cursor.execute(
                    "ALTER TABLE files ADD COLUMN thumbnailRotation INTEGER DEFAULT 0")

            self.cursor.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.conn.commit()
            logging.info("Migracao de colunas concluida.")

        except Exception as e:
            logging.warning("Erro na migracao de colunas: %s", e)
            self.conn.rollback()

    def _create_performance_indices(self):
        try:
            self.cursor.execute("PRAGMA table_info(files)")
            columns = [row[1] for row in self.cursor.fetchall()]

            if 'name_normalized' in columns:
                self.cursor.execute(
                    'CREATE INDEX IF NOT EXISTS idx_files_name_normalized ON files(name_normalized) WHERE source="local"')
                logging.debug("Indice name_normalized verificado.")

            if 'name_aggressive' in columns:
                self.cursor.execute(
                    'CREATE INDEX IF NOT EXISTS idx_files_name_aggressive ON files(name_aggressive) WHERE source="local"')
                logging.debug("Indice name_aggressive verificado.")

            self.cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_files_name_lower_local ON files(LOWER(name)) WHERE source="local"')
            self.cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_files_source_name ON files(source, name)')

            self.conn.commit()

        except Exception as e:
            logging.warning("Erro ao criar indices de performance: %s", e)
            self.conn.rollback()

    def save_files_in_batch(self, files_list, source, simulate_error=False):
        self.ensure_conn()
        try:
            with self.conn:
                file_ids = []
                for item in files_list:
                    candidate_id = item.get('id')
                    if item.get('source') == 'local' and item.get('path'):
                        candidate_id = to_canonical_id(item.get('path'))
                    file_ids.append((candidate_id,))

                existing_state = {}
                if file_ids:
                    placeholders = ','.join('?' for _ in file_ids)
                    try:
                        self.cursor.execute(
                            f"SELECT file_id, description, webContentLink, "
                            f"thumbnailRotation, starred FROM files "
                            f"WHERE file_id IN ({placeholders})",
                            [fid for (fid,) in file_ids]
                        )
                        existing_state = {
                            row[0]: (row[1], row[2], row[3], row[4])
                            for row in self.cursor.fetchall()
                        }
                    except Exception:
                        existing_state = {}

                if file_ids:
                    self.cursor.executemany(
                        "DELETE FROM files WHERE file_id = ?", file_ids)
                    self.cursor.executemany(
                        "DELETE FROM search_index WHERE file_id = ?", file_ids)

                from src.drive.match import normalize_aggressive
                data_files = []
                for item in files_list:
                    fid = item.get('id')
                    raw_path = item.get('path')
                    if item.get('source') == 'local' and raw_path:
                        raw_path = os.path.normcase(os.path.normpath(raw_path))
                        fid = raw_path

                    incoming_desc = item.get('description')
                    if (item.get('source') == 'local') and (not incoming_desc):
                        effective_desc = (existing_state.get(fid) or ('', '', 0, 0))[0] or ''
                    else:
                        effective_desc = incoming_desc

                    incoming_web_link = item.get('webContentLink')
                    if item.get('source') == 'local' and not incoming_web_link:
                        effective_web_link = (
                            (existing_state.get(fid) or ('', '', 0, 0))[1] or None
                        )
                    else:
                        effective_web_link = incoming_web_link

                    previous_state = existing_state.get(fid) or ('', '', 0, 0)
                    incoming_rotation = item.get('thumbnailRotation')
                    effective_rotation = (
                        int(previous_state[2] or 0)
                        if incoming_rotation is None
                        else int(incoming_rotation or 0) % 360
                    )
                    effective_starred = int(
                        item.get('starred', previous_state[3] or 0) or 0
                    )

                    name = item.get('name', '')
                    name_normalized = SearchEngine(
                        None).normalize_text(name) if name else ''
                    name_aggressive = normalize_aggressive(
                        name) if name else ''

                    data_files.append((
                        fid,
                        name,
                        raw_path,
                        item.get('mimeType'),
                        item.get('source'),
                        effective_desc,

                        item.get('thumbnailLink'),
                        item.get('thumbnailPath'),
                        item.get('size', 0),
                        item.get('modifiedTime'),
                        item.get('createdTime'),
                        item.get('parentId'),
                        effective_web_link,
                        effective_starred,
                        name_normalized,
                        name_aggressive,
                        effective_rotation,
                    ))
                if data_files:
                    self.cursor.executemany(
                        "INSERT OR REPLACE INTO files ("
                        "file_id,name,path,mimeType,source,description,"
                        "thumbnailLink,thumbnailPath,size,modifiedTime,createdTime,"
                        "parentId,webContentLink,starred,name_normalized,"
                        "name_aggressive,thumbnailRotation"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        data_files,
                    )

                data_search_index = []
                for item in files_list:
                    fid = item.get('id')
                    raw_path = item.get('path')
                    if item.get('source') == 'local' and raw_path:
                        fid = os.path.normcase(os.path.normpath(raw_path))

                    name_val = item.get('name', '')
                    incoming_desc = item.get('description', '')
                    if (item.get('source') == 'local') and (not incoming_desc):
                        effective_desc = (existing_state.get(fid) or ('', '', 0, 0))[0] or ''
                    else:
                        effective_desc = incoming_desc
                    data_search_index.append((
                        name_val,
                        effective_desc,
                        SearchEngine(None).normalize_text(name_val),
                        SearchEngine(None).normalize_text(effective_desc),
                        fid,
                        item.get('source')
                    ))
                if data_search_index:
                    self.cursor.executemany(
                        "INSERT OR REPLACE INTO search_index VALUES (?, ?, ?, ?, ?, ?)", data_search_index)

                if simulate_error:
                    raise ValueError("Simulating an error for rollback")

        except Exception as e:
            logging.error("Erro ao salvar lote; rollback acionado: %s", e)
            raise

    def count_files(self, source, search_term=None, filter_type=None, folder_id=None, advanced_filters=None):
        self.ensure_conn()
        cache_key = (source, search_term, filter_type,
                     folder_id, str(advanced_filters))
        if cache_key in self._count_cache:
            return self._count_cache[cache_key]
        params = []
        where_clauses = []
        file_ids = None
        if search_term:
            quoted_search_term = search_term.strip().replace('"', '""')
            query_term = f'"{quoted_search_term}*"'
            query = "SELECT DISTINCT file_id FROM search_index WHERE search_index MATCH ?"
            self.cursor.execute(query, (query_term,))
            file_ids = [row[0] for row in self.cursor.fetchall()]
            if not file_ids:
                self._count_cache[cache_key] = 0
                return 0
        if file_ids is not None:
            placeholders = ','.join('?' for _ in file_ids)
            where_clauses.append(f"file_id IN ({placeholders})")
            params.extend(file_ids)
        if source:
            where_clauses.append("source=?")
            params.append(source)
        if folder_id:
            where_clauses.append("parentId=?")
            params.append(folder_id)
        elif not search_term:
            where_clauses.append("(parentId IS NULL OR parentId = '')")
        if filter_type == 'image':
            where_clauses.append("mimeType LIKE 'image/%'")
        elif filter_type == 'document':
            where_clauses.append(
                "(mimeType LIKE 'application/vnd.google-apps.document' OR mimeType LIKE 'application/pdf' OR mimeType LIKE '%wordprocessingml.document%')")
        elif filter_type == 'spreadsheet':
            where_clauses.append(
                "(mimeType LIKE 'application/vnd.google-apps.spreadsheet' OR mimeType LIKE '%spreadsheetml.sheet%')")
        elif filter_type == 'presentation':
            where_clauses.append(
                "(mimeType LIKE 'application/vnd.google-apps.presentation' OR mimeType LIKE '%presentationml.presentation%')")
        elif filter_type == 'folder':
            where_clauses.append(
                "mimeType = 'folder' OR mimeType = 'application/vnd.google-apps.folder'")
        if advanced_filters:
            if 'size_min' in advanced_filters and advanced_filters['size_min']:
                where_clauses.append("size >= ?")
                params.append(advanced_filters['size_min'] * 1024 * 1024)
            if 'size_max' in advanced_filters and advanced_filters['size_max']:
                where_clauses.append("size <= ?")
                params.append(advanced_filters['size_max'] * 1024 * 1024)
            if 'modified_after' in advanced_filters and advanced_filters['modified_after']:
                where_clauses.append("modifiedTime >= ?")
                params.append(
                    int(time.mktime(advanced_filters['modified_after'].timetuple())))
            if 'created_after' in advanced_filters and advanced_filters['created_after']:
                where_clauses.append("createdTime >= ?")
                params.append(
                    int(time.mktime(advanced_filters['created_after'].timetuple())))
            if 'created_before' in advanced_filters and advanced_filters['created_before']:
                where_clauses.append("createdTime <= ?")
                params.append(
                    int(time.mktime(advanced_filters['created_before'].timetuple())))
            if 'extension' in advanced_filters and advanced_filters['extension']:
                where_clauses.append("name LIKE ?")
                params.append(f"%{advanced_filters['extension']}")
                where_clauses.append("mimeType != 'folder'")
                where_clauses.append(
                    "mimeType != 'application/vnd.google-apps.folder'")

            if 'category' in advanced_filters and advanced_filters['category']:
                category = advanced_filters['category']
                if category != '':
                    category_extensions = {
                        'images': ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg', '.ico', '.tiff', '.heic', '.arw', '.cr2', '.nef', '.dng', '.raf', '.orf', '.srw'],
                        'videos': ['.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv', '.webm', '.m4v', '.3gp'],
                        'media': ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg', '.ico', '.tiff', '.heic', '.arw', '.cr2', '.nef', '.dng', '.raf', '.orf', '.srw', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv', '.webm', '.m4v', '.3gp'],
                        'documents': ['.pdf', '.doc', '.docx', '.txt', '.rtf', '.odt', '.xls', '.xlsx', '.ppt', '.pptx'],
                        'audios': ['.mp3', '.wav', '.flac', '.aac', '.ogg', '.wma', '.m4a'],
                        'others': []
                    }
                    if category in category_extensions and category != 'others':
                        extensions = category_extensions[category]
                        ext_conditions = []
                        for ext in extensions:
                            ext_conditions.append("name LIKE ?")
                            params.append(f"%{ext}")
                        if ext_conditions:
                            where_clauses.append(
                                f"({' OR '.join(ext_conditions)})")
        query = "SELECT COUNT(*) FROM files"
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
        self.cursor.execute(query, params)
        result = self.cursor.fetchone()[0]
        self._count_cache[cache_key] = result
        return result

    def get_file_count(self, source=None):
        self.ensure_conn()
        if source:
            self.cursor.execute(
                "SELECT COUNT(*) FROM files WHERE source = ?", (source,))
        else:
            self.cursor.execute("SELECT COUNT(*) FROM files")
        return self.cursor.fetchone()[0]

    def get_breadcrumb(self, folder_id, source):
        self.ensure_conn()
        if not folder_id:
            return [{'id': None, 'name': 'Root' if source == 'local' else 'Drive'}]

        breadcrumb = []
        current_id = folder_id
        while current_id:
            self.cursor.execute(
                "SELECT file_id, name, path, parentId FROM files WHERE file_id = ?", (current_id,))
            row = self.cursor.fetchone()
            if row:
                breadcrumb.append(
                    {'id': row[0], 'name': row[1], 'path': row[2]})
                current_id = row[3]
            else:
                break
        breadcrumb.append(
            {'id': None, 'name': 'Root' if source == 'local' else 'Drive'})
        return list(reversed(breadcrumb))

    def _build_file_objects_from_search(self, rows):
        files = []
        for row in rows:
            file = {
                'id': row[0],
                'name': row[1],
                'path': row[2],
                'mimeType': row[3],
                'source': row[4],
                'description': row[5],
                'thumbnailLink': row[6],
                'thumbnailPath': row[7],
                'size': row[8],
                'modifiedTime': row[9],
                'createdTime': row[10],
                'parentId': row[11],
                'starred': bool(row[12]) if len(row) > 12 else False,
                'webContentLink': row[13] if len(row) > 13 else '',
                'thumbnailRotation': int(row[14] or 0) if len(row) > 14 else 0,
            }
            files.append(file)
        return files

    def set_starred(self, file_id, starred=True):
        self.ensure_conn()
        self.cursor.execute(
            "UPDATE files SET starred = ? WHERE file_id = ?", (1 if starred else 0, file_id))
        self.conn.commit()

    def rebuild_search_index_with_normalization(self):
        self.ensure_conn()

        try:
            logging.info("Iniciando reconstrucao do indice FTS5 com normalizacao...")
            self.cursor.execute('DROP TABLE IF EXISTS search_index')
            self.cursor.execute('''
                    CREATE VIRTUAL TABLE search_index USING fts5(
                        name,
                        description,
                        normalized_name,
                        normalized_description,
                        file_id UNINDEXED,
                        source UNINDEXED,
                        tokenize="trigram"
                    )
                    ''')

            self.cursor.execute(
                'SELECT file_id, name, description, source FROM files')
            all_files = self.cursor.fetchall()

            batch_data = []
            norm_engine = SearchEngine(None)
            for i, (file_id, name, description, source) in enumerate(all_files):
                name = name or ""
                description = description or ""

                batch_data.append((
                    name,
                    description,
                    norm_engine.normalize_text(name),
                    norm_engine.normalize_text(description),
                    file_id,
                    source
                ))

                if len(batch_data) >= 1000:
                    self.cursor.executemany(
                        "INSERT INTO search_index VALUES (?, ?, ?, ?, ?, ?)",
                        batch_data
                    )
                    batch_data = []

            if batch_data:
                self.cursor.executemany(
                    "INSERT INTO search_index VALUES (?, ?, ?, ?, ?, ?)",
                    batch_data
                )

            self.conn.commit()
            logging.info("Indice FTS5 reconstruido com sucesso!")

        except Exception as e:
            logging.error(f"Erro ao reconstruir o indice: {e}")
            self.conn.rollback()
            raise

    def close(self):
        if self.conn is not None:
            self.conn.close()
        self.conn = None
        self.cursor = None

    def add_file(self, file_path, source):
        if not os.path.exists(file_path):
            return
        normalized_path = os.path.normcase(os.path.normpath(file_path))
        self.save_files_in_batch(
            [{
                'id': normalized_path,
                'name': os.path.basename(file_path),
                'path': normalized_path,
                'mimeType': mimetypes.guess_type(file_path)[0]
                or 'application/octet-stream',
                'source': source,
                'description': '',
                'thumbnailLink': '',
                'thumbnailPath': '',
                'size': os.path.getsize(file_path),
                'modifiedTime': int(os.path.getmtime(file_path)),
                'createdTime': int(os.path.getctime(file_path)),
                'parentId': os.path.normcase(
                    os.path.normpath(os.path.dirname(file_path))
                ),
                'webContentLink': None,
            }],
            source=source,
        )

    def toggle_starred(self, file_id):
        self.ensure_conn()
        self.cursor.execute(
            "SELECT starred FROM files WHERE file_id = ?", (file_id,))
        row = self.cursor.fetchone()
        if row:
            current = row[0]
            new_status = 0 if current else 1
            self.cursor.execute(
                "UPDATE files SET starred = ? WHERE file_id = ?", (new_status, file_id))
            self.conn.commit()
            return new_status
        return None

    def clear_query_cache(self):
        """Descarta apenas resultados calculados; thumbnails permanecem."""
        self._count_cache.clear()
        self._paged_cache.clear()

    def clear_thumbnail_cache(self):
        """Apaga thumbnails somente quando o usuario pede explicitamente."""
        self.ensure_conn()
        try:
            assets_cache = os.path.join('assets', 'thumbnail_cache')
            if os.path.exists(assets_cache):
                shutil.rmtree(assets_cache)
        except Exception:
            pass
        try:
            legacy_cache = 'thumbnail_cache'
            if os.path.exists(legacy_cache):
                shutil.rmtree(legacy_cache)
        except Exception:
            pass
        os.makedirs(os.path.join('assets', 'thumbnail_cache'), exist_ok=True)
        self.cursor.execute(
            "UPDATE files SET thumbnailPath = NULL WHERE source = 'drive'")
        self.clear_query_cache()
        self.conn.commit()

    def clear_cache(self):
        """Compatibilidade: o antigo nome agora e seguro e nao apaga imagens."""
        self.clear_query_cache()

    def clear_source(self, source: str):
        self.ensure_conn()
        try:
            self.cursor.execute(
                "DELETE FROM files WHERE source = ?", (source,))
            self.cursor.execute(
                "DELETE FROM search_index WHERE source = ?", (source,))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def update_thumbnail_path(self, file_id: str, thumbnail_path: str | None):
        self.ensure_conn()
        self.cursor.execute(
            "UPDATE files SET thumbnailPath = ? WHERE file_id = ?",
            (thumbnail_path, file_id),
        )
        self.conn.commit()

    def update_thumbnail_rotation(self, file_id, path, degrees):
        """Persiste a orientacao de exibicao sem modificar o arquivo original."""
        self.ensure_conn()
        canon_id = to_canonical_id(file_id or path)
        canon_path = to_canonical_id(path)
        rotation = int(degrees or 0) % 360
        if rotation not in (0, 90, 180, 270):
            raise ValueError('Rotacao deve ser 0, 90, 180 ou 270 graus.')
        self.cursor.execute(
            "UPDATE files SET thumbnailRotation=? "
            "WHERE source='local' AND (file_id=? OR path=?)",
            (rotation, canon_id, canon_path),
        )
        if self.cursor.rowcount != 1:
            self.conn.rollback()
            raise RuntimeError('Arquivo local nao encontrado para salvar a rotacao.')
        self.conn.commit()
        return rotation

    def rotate_thumbnail_rotations(self, file_refs, quarter_turns=1):
        """Gira uma selecao usando o angulo vivo do banco em uma transacao.

        ``file_refs`` contem pares ``(file_id, path)``. O retorno preserva a
        ordem dos registros unicos e informa a identidade persistida e o novo
        angulo. Assim a interface nao depende de dicionarios possivelmente
        antigos mantidos pelo modelo Qt.
        """
        self.ensure_conn()
        try:
            turns = int(quarter_turns)
        except (TypeError, ValueError) as exc:
            raise ValueError('Quantidade de giros invalida.') from exc
        if turns == 0:
            return []

        delta = (turns * 90) % 360
        results = []
        seen = set()
        try:
            for file_id, path in file_refs:
                canon_id = to_canonical_id(file_id or path)
                canon_path = to_canonical_id(path)
                rows = self.cursor.execute(
                    "SELECT file_id,path FROM files WHERE source='local' "
                    "AND (file_id=? OR path=?) LIMIT 2",
                    (canon_id, canon_path),
                ).fetchall()
                if len(rows) != 1:
                    raise RuntimeError(
                        'Arquivo local nao encontrado de forma unica para '
                        'salvar a rotacao.'
                    )

                stored_id, stored_path = rows[0]
                if stored_id in seen:
                    continue
                seen.add(stored_id)
                self.cursor.execute(
                    "UPDATE files SET thumbnailRotation="
                    "((COALESCE(thumbnailRotation,0) + ?) % 360) "
                    "WHERE file_id=? AND source='local'",
                    (delta, stored_id),
                )
                rotation = self.cursor.execute(
                    "SELECT thumbnailRotation FROM files WHERE file_id=?",
                    (stored_id,),
                ).fetchone()[0]
                results.append({
                    'file_id': stored_id,
                    'path': stored_path,
                    'thumbnailRotation': int(rotation or 0),
                })
            self.conn.commit()
            return results
        except Exception:
            self.conn.rollback()
            raise

    def update_description(self, file_id: str, description: str | None, thumbnailLink: str | None = None, webContentLink: str | None = None, commit: bool = False, allow_empty_override: bool = True):
        self.ensure_conn()
        desc = (description or '').strip()
        canon_id = to_canonical_id(file_id)

        if not allow_empty_override and not desc:
            # Não sobrescrever descrição existente com vazio
            self.cursor.execute(
                "UPDATE files SET thumbnailLink = COALESCE(?, thumbnailLink), webContentLink = COALESCE(?, webContentLink) WHERE file_id = ? OR path = ?",
                (thumbnailLink, webContentLink, canon_id, canon_id),
            )
        else:
            self.cursor.execute(
                "UPDATE files SET description = ?, thumbnailLink = COALESCE(?, thumbnailLink), webContentLink = COALESCE(?, webContentLink) WHERE file_id = ? OR path = ?",
                (desc, thumbnailLink, webContentLink, canon_id, canon_id),
            )
            self.cursor.execute(
                "UPDATE search_index SET description = ?, normalized_description = ? WHERE file_id = ?",
                (desc, SearchEngine(None).normalize_text(desc), canon_id),
            )
        if commit:
            self.conn.commit()

    def export_to_shared_cache(self, target_path=None):
        """Publica uma geracao versionada; o manifesto e trocado por ultimo."""
        local_tmp_db = None
        local_tmp_csv = None
        dest_conn = None
        try:
            from src.utils.config_manager import ConfigManager
            config_mgr = ConfigManager()
            if target_path is None:
                target_path = config_mgr.get_shared_cache_publish_path(
                    SCHEMA_VERSION
                )
            target_dir = os.path.dirname(target_path)
            if target_dir and not os.path.exists(target_dir):
                os.makedirs(target_dir, exist_ok=True)

            self.ensure_conn()
            self.conn.commit()
            search_ok, search_reason = self.search_index_is_consistent()
            if not search_ok:
                raise RuntimeError(
                    'Snapshot nao publicado porque o indice de busca esta '
                    f'inconsistente ({search_reason}). Execute a ferramenta '
                    'Saude e reparo do banco.'
                )
            # 1. Executar checkpoint WAL para consolidar transações pendentes no arquivo .db principal
            try:
                self.cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as e_wal:
                logging.warning(f"Aviso no wal_checkpoint: {e_wal}")

            # 2. Copia consistente gerada em pasta temporaria local.
            local_tmp_dir = tempfile.gettempdir()
            generation = time.strftime('%Y%m%dT%H%M%S') + f'-{time.time_ns() % 1_000_000_000:09d}'
            local_tmp_db = os.path.join(
                local_tmp_dir, f"export_db_{os.getpid()}_{generation}.db"
            )
            dest_conn = sqlite3.connect(local_tmp_db)
            self.conn.backup(dest_conn)

            # 3. CSV acompanha a mesma geracao e serve para inspecao/recuperacao.
            local_tmp_csv = os.path.join(
                local_tmp_dir, f"export_csv_{os.getpid()}_{generation}.csv"
            )

            with open(local_tmp_csv, "w", newline="", encoding="utf-8-sig") as f:
                import csv
                writer = csv.writer(f)
                writer.writerow(["file_id", "name", "path", "mimeType", "source", "description", "size", "modifiedTime", "createdTime", "parentId", "webContentLink", "starred", "thumbnailRotation"])
                cur = dest_conn.cursor()
                cur.execute("SELECT file_id, name, path, mimeType, source, description, size, modifiedTime, createdTime, parentId, webContentLink, starred, thumbnailRotation FROM files")
                for row in cur:
                    writer.writerow(row)

            manifest = create_manifest(
                dest_conn,
                local_tmp_db,
                local_tmp_csv,
                scan_roots=config_mgr.get_resolved_scan_paths(persist=False),
                generation=generation,
            )
            dest_conn.close()
            dest_conn = None
            remote_db, _remote_csv, pointer = publish_generation(
                target_path,
                local_tmp_db,
                local_tmp_csv,
                manifest,
                expected_previous_generation=config_mgr.get(
                    'shared_snapshot_generation'
                ),
            )
            config_mgr.set('shared_snapshot_generation', generation)

            logging.info(
                "[OK] Snapshot compartilhado publicado: %s (manifesto: %s)",
                remote_db, pointer,
            )
            return True
        except Exception as e:
            logging.warning("Nao foi possivel exportar snapshot: %s", e)
            return False
        finally:
            if dest_conn:
                try: dest_conn.close()
                except Exception: pass
            if local_tmp_db and os.path.exists(local_tmp_db):
                try: os.remove(local_tmp_db)
                except Exception: pass
            if local_tmp_csv and os.path.exists(local_tmp_csv):
                try: os.remove(local_tmp_csv)
                except Exception: pass


def open_db_for_thread(db_name):
    conn = sqlite3.connect(db_name, check_same_thread=False, timeout=30.0)
    conn.create_function("py_lower", 1, lambda s: s.lower() if s else s)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA temp_store = MEMORY;")
    conn.execute("PRAGMA cache_size = 5000;")
    return conn
