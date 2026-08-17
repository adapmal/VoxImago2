import time
import logging
from datetime import datetime, timezone
from PyQt6.QtCore import QObject, pyqtSignal, QThread

class IncrementalSyncWorker(QObject):
    sync_finished = pyqtSignal(int)  # number of updated files
    sync_failed = pyqtSignal(str)

    def __init__(self, service, config_mgr, indexer=None):
        super().__init__()
        self.service = service
        self.config_mgr = config_mgr
        self.indexer = indexer

    def run(self):
        try:
            from src.database.database import FileIndexer
            local_indexer = FileIndexer()
            
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

            for file in updated_files:
                fid = file.get('id')
                fname = file.get('name')
                desc = file.get('description', '')
                wlink = file.get('webViewLink', '')
                mod_time = int(datetime.strptime(file.get('modifiedTime'), "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()) if file.get('modifiedTime') else int(time.time())
                
                # 1. Atualizar registro no banco onde file_id = fid (drive)
                local_indexer.cursor.execute(
                    "UPDATE files SET description = ?, modifiedTime = ?, webContentLink = ? WHERE file_id = ?",
                    (desc, mod_time, wlink, fid)
                )
                
                # 2. Atualizar registro local correspondente respeitando o escopo do Sandbox
                if is_sb:
                    local_indexer.cursor.execute(
                        "UPDATE files SET description = ?, modifiedTime = ?, webContentLink = ? WHERE name = ? AND source = 'local' AND (path LIKE '%_TestesBanco%' OR file_id LIKE '%_TestesBanco%')",
                        (desc, mod_time, wlink, fname)
                    )
                    local_indexer.cursor.execute(
                        "UPDATE search_index SET description = ?, normalized_description = ? WHERE file_id = ? OR file_id IN (SELECT file_id FROM files WHERE name = ? AND (path LIKE '%_TestesBanco%' OR file_id LIKE '%_TestesBanco%'))",
                        (desc, desc.lower(), fid, fname)
                    )
                else:
                    local_indexer.cursor.execute(
                        "UPDATE files SET description = ?, modifiedTime = ?, webContentLink = ? WHERE name = ? AND source = 'local' AND NOT (path LIKE '%_TestesBanco%' OR file_id LIKE '%_TestesBanco%')",
                        (desc, mod_time, wlink, fname)
                    )
                    local_indexer.cursor.execute(
                        "UPDATE search_index SET description = ?, normalized_description = ? WHERE file_id = ? OR file_id IN (SELECT file_id FROM files WHERE name = ? AND NOT (path LIKE '%_TestesBanco%' OR file_id LIKE '%_TestesBanco%'))",
                        (desc, desc.lower(), fid, fname)
                    )

            local_indexer.conn.commit()
            
            # Exportar cache compartilhado (.db e .csv)
            local_indexer.export_to_shared_cache()

            # Atualizar Timestamp
            self.config_mgr.set('last_sync_timestamp', int(time.time()))
            
            self.sync_finished.emit(len(updated_files))

        except Exception as e:
            logging.error(f"❌ Erro na Sincronização Incremental: {e}", exc_info=True)
            self.sync_failed.emit(str(e))
