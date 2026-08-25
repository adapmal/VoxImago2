import os
import time
import logging
from datetime import datetime, timezone
from PyQt6.QtCore import QObject, pyqtSignal, QThread

class IncrementalSyncWorker(QObject):
    sync_finished = pyqtSignal(int)  # number of updated files
    sync_failed = pyqtSignal(str)

    def __init__(self, service, config_mgr, indexer=None, force_window_days=None):
        super().__init__()
        self.service = service
        self.config_mgr = config_mgr
        self.indexer = indexer
        self.force_window_days = force_window_days

    def run(self):
        try:
            from src.database.database import FileIndexer
            local_indexer = FileIndexer()
            
            if self.force_window_days:
                last_sync = int(time.time() - self.force_window_days * 86400)
            else:
                last_sync = self.config_mgr.get('last_sync_timestamp')
                if not last_sync:
                    # Se não houver timestamp anterior, olhar os últimos 30 dias para pegar qualquer alteração recente
                    last_sync = int(time.time() - 30 * 86400)
                    self.config_mgr.set('last_sync_timestamp', last_sync)

            last_sync_dt = datetime.fromtimestamp(last_sync, timezone.utc)
            formatted_time = last_sync_dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
            
            q = f"modifiedTime > '{formatted_time}' and trashed = false"
            
            kwargs = {
                'q': q,
                'pageSize': 1000,
                'includeItemsFromAllDrives': True,
                'supportsAllDrives': True,
                'fields': "nextPageToken, files(id, name, mimeType, description, size, modifiedTime, createdTime, parents, thumbnailLink, webViewLink)",
            }
            
            current_drive = self.config_mgr.get_current_drive_id()
            if current_drive:
                kwargs['corpora'] = 'drive'
                kwargs['driveId'] = current_drive
            else:
                kwargs['corpora'] = 'allDrives'

            logging.info(f"🔄 Iniciando Sincronização Incremental (Drive: {current_drive}): {q}")
            
            updated_files = []
            page_token = None
            
            while True:
                if page_token:
                    kwargs['pageToken'] = page_token
                response = self.service.files().list(**kwargs).execute()
                items = response.get('files', [])
                updated_files.extend(items)
                
                page_token = response.get('nextPageToken')
                if not page_token:
                    break

            if not updated_files:
                logging.info("Nenhum arquivo novo ou modificado encontrado no Drive.")
                self.config_mgr.set('last_sync_timestamp', int(time.time()))
                self.sync_finished.emit(0)
                return

            logging.info(f"🔄 Sincronização Incremental encontrou {len(updated_files)} arquivos alterados. Processando...")

            local_indexer.ensure_conn()
            is_sb = self.config_mgr.is_sandbox()

            from src.database.search import SearchEngine
            norm_engine = SearchEngine(None)
            parent_name_cache = {}

            for file in updated_files:
                fid = file.get('id')
                fname = file.get('name')
                desc = (file.get('description') or '').strip()
                norm_desc = norm_engine.normalize_text(desc)
                wlink = file.get('webViewLink', '')
                parents = file.get('parents', [])
                parent_id = parents[0] if parents else None
                mod_time = int(datetime.strptime(file.get('modifiedTime'), "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()) if file.get('modifiedTime') else int(time.time())
                
                # 1. Atualizar registro no banco onde file_id = fid (drive)
                local_indexer.cursor.execute(
                    "UPDATE files SET description = ?, modifiedTime = ?, webContentLink = ? WHERE file_id = ?",
                    (desc, mod_time, wlink, fid)
                )
                drive_updated = (local_indexer.cursor.rowcount > 0)
                
                # 2. Localizar o arquivo local correspondente com isolamento estrito de pasta e ID
                # 2a. Primeiro tenta por vínculo direto gravado
                local_indexer.cursor.execute(
                    "SELECT file_id, path FROM files WHERE (webContentLink LIKE ? OR file_id = ?) AND source = 'local'",
                    (f"%{fid}%", fid)
                )
                direct_matches = local_indexer.cursor.fetchall()
                
                target_local_fid = None
                if direct_matches:
                    target_local_fid = direct_matches[0][0]
                elif parent_id:
                    # 2b. Se não está vinculado, resolve o nome da pasta pai no Drive
                    if parent_id not in parent_name_cache:
                        try:
                            p_info = self.service.files().get(fileId=parent_id, supportsAllDrives=True, fields='name').execute()
                            parent_name_cache[parent_id] = p_info.get('name', '')
                        except Exception:
                            parent_name_cache[parent_id] = ''
                    drive_parent_name = parent_name_cache.get(parent_id, '')

                    # Buscar candidatos locais com o mesmo nome de arquivo
                    local_indexer.cursor.execute(
                        "SELECT file_id, path FROM files WHERE (name = ? OR name_normalized = ?) AND source = 'local'",
                        (fname, fname.lower())
                    )
                    candidates = local_indexer.cursor.fetchall()

                    matched_candidates = []
                    for c_fid, c_path in candidates:
                        if is_sb and '_TestesBanco' not in c_fid:
                            continue
                        if not is_sb and '_TestesBanco' in c_fid:
                            continue
                        if c_path and drive_parent_name:
                            p_folder = os.path.basename(os.path.dirname(c_path))
                            if norm_engine.normalize_text(p_folder) == norm_engine.normalize_text(drive_parent_name):
                                matched_candidates.append(c_fid)

                    if len(matched_candidates) == 1:
                        target_local_fid = matched_candidates[0]

                local_updated = False
                if target_local_fid:
                    if desc:
                        local_indexer.cursor.execute(
                            "UPDATE files SET description = ?, modifiedTime = ?, webContentLink = ? WHERE file_id = ?",
                            (desc, mod_time, wlink, target_local_fid)
                        )
                        local_indexer.cursor.execute(
                            "UPDATE search_index SET description = ?, normalized_description = ? WHERE file_id = ?",
                            (desc, norm_desc, target_local_fid)
                        )
                    else:
                        local_indexer.cursor.execute(
                            "UPDATE files SET modifiedTime = ?, webContentLink = COALESCE(?, webContentLink) WHERE file_id = ?",
                            (mod_time, wlink, target_local_fid)
                        )
                    local_updated = True

                # Se o arquivo não existia nem no Drive nem localmente, inserir novo registro do Drive
                if not drive_updated and not local_updated:
                    try:
                        created_time_raw = file.get('createdTime')
                        created_time = int(datetime.strptime(created_time_raw, "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()) if created_time_raw else mod_time
                    except Exception:
                        created_time = mod_time

                    new_item = {
                        'id': fid,
                        'name': fname,
                        'mimeType': file.get('mimeType'),
                        'source': 'drive',
                        'description': desc,
                        'thumbnailLink': file.get('thumbnailLink', ''),
                        'thumbnailPath': '',
                        'size': int(file.get('size', 0)) if file.get('size') else 0,
                        'modifiedTime': mod_time,
                        'createdTime': created_time,
                        'parentId': file.get('parents', [''])[0] if file.get('parents') else '',
                        'path': None,
                        'webContentLink': wlink,
                    }
                    local_indexer.save_files_in_batch([new_item], source='drive')

            local_indexer.conn.commit()
            
            # Exportar cache compartilhado (.db e .csv)
            local_indexer.export_to_shared_cache()

            # Atualizar Timestamp
            self.config_mgr.set('last_sync_timestamp', int(time.time()))
            
            self.sync_finished.emit(len(updated_files))

        except Exception as e:
            logging.error(f"❌ Erro na Sincronização Incremental: {e}", exc_info=True)
            self.sync_failed.emit(str(e))
