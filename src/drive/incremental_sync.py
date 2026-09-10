import time
import logging
from collections import Counter
from datetime import datetime, timezone
from PyQt6.QtCore import QObject, pyqtSignal

from src.drive.match import (
    DriveHierarchyResolver,
    build_local_link_index,
    match_drive_to_local,
)
from src.utils.path_validation import configured_operation_roots

class IncrementalSyncWorker(QObject):
    sync_finished = pyqtSignal(int)  # number of updated files
    sync_failed = pyqtSignal(str)
    sync_warning = pyqtSignal(str)
    # Valor negativo indica uma etapa de duracao indeterminada (consulta da API).
    progress_update = pyqtSignal(int, str)

    def __init__(self, service, config_mgr, indexer=None, force_window_days=None):
        super().__init__()
        self.service = service
        self.config_mgr = config_mgr
        self.indexer = indexer
        self.force_window_days = force_window_days

    def run(self):
        local_indexer = None
        try:
            self.progress_update.emit(
                -1, "Consultando alterações no Google Drive..."
            )
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
                'fields': "nextPageToken, files(id, name, mimeType, description, size, md5Checksum, modifiedTime, createdTime, parents, thumbnailLink, webViewLink)",
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
                self.progress_update.emit(
                    -1,
                    f"Consultando o Drive: {len(updated_files):,} item(ns) localizado(s)...",
                )
                
                page_token = response.get('nextPageToken')
                if not page_token:
                    break

            if not updated_files:
                logging.info("Nenhum arquivo novo ou modificado encontrado no Drive.")
                self.config_mgr.set('last_sync_timestamp', int(time.time()))
                self.progress_update.emit(100, "Google Drive já está atualizado.")
                self.sync_finished.emit(0)
                return

            logging.info(f"🔄 Sincronização Incremental encontrou {len(updated_files)} arquivos alterados. Processando...")
            total_updated = len(updated_files)
            self.progress_update.emit(
                0,
                f"Sincronizando: 0/{total_updated:,} arquivos (0%)...",
            )

            local_indexer.ensure_conn()
            from src.database.search import SearchEngine
            norm_engine = SearchEngine(None)
            self.progress_update.emit(
                1, f"Preparando correspondências para {total_updated:,} arquivos..."
            )
            match_resolver = DriveHierarchyResolver(self.service)
            match_roots = configured_operation_roots(self.config_mgr)
            link_index = build_local_link_index(local_indexer.cursor, match_roots)
            match_outcomes = Counter()

            for position, file in enumerate(updated_files, start=1):
                fid = file.get('id')
                fname = file.get('name')
                desc = (file.get('description') or '').strip()
                norm_desc = norm_engine.normalize_text(desc)
                wlink = file.get('webViewLink', '')
                parents = file.get('parents', [])
                parent_id = parents[0] if parents else None
                mod_time = int(datetime.strptime(file.get('modifiedTime'), "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()) if file.get('modifiedTime') else int(time.time())
                
                # 1. Atualizar registro no banco onde file_id = fid (drive) com proteção de descrição vazia (F8)
                if desc:
                    local_indexer.cursor.execute(
                        "UPDATE files SET description = ?, modifiedTime = ?, webContentLink = ? WHERE file_id = ?",
                        (desc, mod_time, wlink, fid)
                    )
                    drive_updated = (local_indexer.cursor.rowcount > 0)
                    local_indexer.cursor.execute(
                        "UPDATE search_index SET description = ?, normalized_description = ? WHERE file_id = ?",
                        (desc, norm_desc, fid)
                    )
                else:
                    local_indexer.cursor.execute(
                        "UPDATE files SET modifiedTime = ?, webContentLink = COALESCE(?, webContentLink) WHERE file_id = ?",
                        (mod_time, wlink, fid)
                    )
                    drive_updated = (local_indexer.cursor.rowcount > 0)

                # 2. So atualiza o registro local quando tamanho/hierarquia produzem
                # um unico vencedor. Empates permanecem como itens separados do Drive.
                match = match_drive_to_local(
                    file,
                    local_indexer.cursor,
                    self.service,
                    hierarchy_resolver=match_resolver,
                    allowed_roots=match_roots,
                    link_index=link_index,
                )
                match_outcomes[match.status] += 1
                target_local_fid = match.local_id if match.matched else None

                local_updated = False
                if target_local_fid:
                    # ``modifiedTime`` do registro local descreve os bytes no
                    # disco e faz parte da chave da thumbnail. Uma alteracao de
                    # tag no Drive nao pode fingir que a imagem local mudou.
                    if desc:
                        local_indexer.cursor.execute(
                            "UPDATE files SET description = ?, webContentLink = ? WHERE file_id = ?",
                            (desc, wlink, target_local_fid)
                        )
                        local_indexer.cursor.execute(
                            "UPDATE search_index SET description = ?, normalized_description = ? WHERE file_id = ?",
                            (desc, norm_desc, target_local_fid)
                        )
                    else:
                        local_indexer.cursor.execute(
                            "UPDATE files SET webContentLink = COALESCE(?, webContentLink) WHERE file_id = ?",
                            (wlink, target_local_fid)
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

                if (position == 1 or position % 25 == 0
                        or position == total_updated):
                    percent = min(96, max(1, int(position * 96 / total_updated)))
                    self.progress_update.emit(
                        percent,
                        f"Sincronizando: {position:,}/{total_updated:,} "
                        f"arquivos ({percent}%)...",
                    )

                # Evita reter o unico lock de escrita do SQLite durante todo um
                # inventario grande. O coordenador da interface ainda impede a
                # fila de iniciar ao mesmo tempo; este commit limita o impacto
                # de processos externos ou de uma interrupcao inesperada.
                if position % 100 == 0:
                    local_indexer.conn.commit()

            local_indexer.conn.commit()
            logging.info(
                "Matching incremental Drive/local: seguros=%d, ambiguos=%d, "
                "conflitos=%d, sem correspondencia=%d.",
                match_outcomes['matched'],
                match_outcomes['ambiguous'],
                match_outcomes['conflict'],
                match_outcomes['no_match'],
            )
            
            # Exportar cache compartilhado (.db e .csv)
            self.progress_update.emit(
                98, "Finalizando e exportando o índice compartilhado..."
            )
            snapshot_exported = local_indexer.export_to_shared_cache()

            # Atualizar Timestamp
            self.config_mgr.set('last_sync_timestamp', int(time.time()))

            self.progress_update.emit(
                100,
                f"Sincronização concluída: {total_updated:,} arquivo(s) processado(s).",
            )
            self.sync_finished.emit(len(updated_files))
            if not snapshot_exported:
                self.sync_warning.emit(
                    'Os dados locais foram sincronizados, mas o snapshot '
                    'compartilhado nao foi publicado. Abra Sistema / '
                    'Avancado > Saude e reparo do banco.'
                )

        except Exception as e:
            logging.error(f"❌ Erro na Sincronização Incremental: {e}", exc_info=True)
            self.sync_failed.emit(str(e))
        finally:
            if local_indexer is not None and hasattr(local_indexer, 'close'):
                try:
                    local_indexer.close()
                except Exception:
                    logging.debug(
                        "Falha ao fechar banco da sincronizacao incremental.",
                        exc_info=True,
                    )
