import os
from src.utils.utils import filter_existing_files


class list_update:
    @staticmethod
    def _sort_files(files, sort_order):
        if sort_order == "created_desc":
            return sorted(files, key=lambda x: x.get("createdTime", 0), reverse=True)
        elif sort_order == "created_asc":
            return sorted(files, key=lambda x: x.get("createdTime", 0))
        elif sort_order == "modified_desc":
            return sorted(files, key=lambda x: x.get("modifiedTime", 0), reverse=True)
        elif sort_order == "modified_asc":
            return sorted(files, key=lambda x: x.get("modifiedTime", 0))
        elif sort_order == "name_asc":
            return sorted(files, key=lambda x: x.get("name", "").lower())
        elif sort_order == "name_desc":
            return sorted(files, key=lambda x: x.get("name", "").lower(), reverse=True)
        return files

    @staticmethod
    def _apply_virtual_staging_overlay(app, files, norm_filter):
        """
        Aplica a camada virtual da Fila de Staging (Proxy Virtual):
        - Oculta arquivos que possuem 'move' pendente saindo da pasta atual.
        - Injeta arquivos que possuem 'move' pendente entrando na pasta atual.
        - Sobrepõe descrições/tags pendentes na memória para busca e exibição consistentes.
        """
        from src.ui.staging_queue import StagingQueue
        staging_queue = StagingQueue()
        if not staging_queue.items:
            return files

        moved_away_ids = set()
        moved_in_staged_items = []
        deleted_ids = set()
        desc_overlay = {}  # {file_id / path_lower: updated_description}

        for it in staging_queue.items:
            fid_key = it.file_id
            path_key = os.path.normpath(it.path).lower() if it.path else None

            if it.action_type == 'move':
                moved_away_ids.add(it.file_id)
                if it.path:
                    moved_away_ids.add(os.path.normpath(it.path).lower())
                if it.old_value:
                    moved_away_ids.add(os.path.normpath(it.old_value).lower())

                if norm_filter:
                    dst_folder = os.path.normpath(os.path.dirname(it.new_value)).lower()
                    if dst_folder == norm_filter:
                        moved_in_staged_items.append(it)

            elif it.action_type == 'delete':
                deleted_ids.add(it.file_id)
                if path_key:
                    deleted_ids.add(path_key)

            elif it.action_type in ('add_tags', 'remove_tags', 'set_description'):
                curr_desc = desc_overlay.get(fid_key, it.old_value or '')
                if it.action_type == 'set_description':
                    new_desc = it.new_value
                elif it.action_type == 'add_tags':
                    new_desc = f"{curr_desc} {it.new_value}".strip()
                elif it.action_type == 'remove_tags':
                    new_desc = curr_desc
                desc_overlay[fid_key] = new_desc
                if path_key:
                    desc_overlay[path_key] = new_desc

        # 1. Filtrar e atualizar arquivos existentes
        filtered_files = []
        for f in files:
            fid = f.get('file_id') or f.get('id')
            fpath = os.path.normpath(f.get('path', '')).lower() if f.get('path') else None

            # Se foi movido para fora desta pasta, não exibe aqui
            if fid in moved_away_ids or (fpath and fpath in moved_away_ids):
                continue

            # Sobrepor tags/descrição pendentes da fila
            if fid in desc_overlay:
                f['description'] = desc_overlay[fid]
            elif fpath and fpath in desc_overlay:
                f['description'] = desc_overlay[fpath]

            if fid in deleted_ids or (fpath and fpath in deleted_ids):
                f['is_staged_delete'] = True

            filtered_files.append(f)

        # 2. Injetar arquivos que foram movidos para dentro desta pasta
        if norm_filter and moved_in_staged_items:
            existing_ids = {f.get('file_id') or f.get('id') for f in filtered_files}
            for it in moved_in_staged_items:
                if it.file_id not in existing_ids:
                    # Buscar dados originais no banco SQLite se disponível
                    file_row = None
                    if hasattr(app, 'indexer') and app.indexer:
                        try:
                            app.indexer.ensure_conn()
                            app.indexer.cursor.execute(
                                "SELECT file_id, name, path, mimeType, source, description, thumbnailLink, thumbnailPath, size, modifiedTime, createdTime, parentId, starred, webContentLink FROM files WHERE file_id = ? OR path = ? LIMIT 1",
                                (it.file_id, it.path)
                            )
                            r = app.indexer.cursor.fetchone()
                            if r:
                                fid_val = r[0]
                                desc_val = desc_overlay.get(fid_val, r[5] or '')
                                file_row = {
                                    'id': r[0],
                                    'name': r[1],
                                    'path': it.new_value,
                                    'physical_path': it.old_value or it.path,
                                    'orig_path': it.old_value or it.path,
                                    'mimeType': r[3],
                                    'source': r[4],
                                    'description': desc_val,
                                    'thumbnailLink': r[6],
                                    'thumbnailPath': r[7],
                                    'size': r[8],
                                    'modifiedTime': r[9],
                                    'createdTime': r[10],
                                    'parentId': os.path.dirname(it.new_value),
                                    'starred': bool(r[12]),
                                    'webContentLink': r[13],
                                    'is_staged_move': True
                                }
                        except Exception:
                            file_row = None

                    if not file_row:
                        file_row = {
                            'id': it.file_id,
                            'name': it.file_name,
                            'path': it.new_value,
                            'physical_path': it.old_value or it.path,
                            'orig_path': it.old_value or it.path,
                            'mimeType': 'image/jpeg',
                            'source': 'local',
                            'description': desc_overlay.get(it.file_id, ''),
                            'thumbnailLink': '',
                            'thumbnailPath': '',
                            'size': 0,
                            'modifiedTime': int(it.timestamp),
                            'createdTime': int(it.timestamp),
                            'parentId': os.path.dirname(it.new_value),
                            'starred': False,
                            'webContentLink': None,
                            'is_staged_move': True
                        }
                    filtered_files.append(file_row)
                    existing_ids.add(it.file_id)

        return filtered_files

    @staticmethod
    def _load_files_for_filters(app, source):
        folder_id = app.current_folder_id
        filter_type = app.current_filter

        from src.utils.config_manager import ConfigManager
        config_mgr = ConfigManager()
        norm_filter = os.path.normpath(app.current_folder_path_filter).lower() if getattr(app, 'current_folder_path_filter', None) else None
        norm_sandbox = os.path.normpath(config_mgr.get_sandbox_path()).lower() if config_mgr.is_sandbox() else None

        # O sandbox filter deve ser absoluto sempre que estiver ativo
        if norm_sandbox:
            app.advanced_filters['sandbox_filter'] = norm_sandbox
        else:
            app.advanced_filters.pop('sandbox_filter', None)

        # Se há um filtro de pasta ativo selecionado na árvore e a busca está vazia:
        if norm_filter and not app.search_term:
            app.advanced_filters['path_filter'] = norm_filter
            filter_type = 'all'
            files = app.search_engine.load_files_paged(
                'local', app.current_page, app.page_size, None, app.current_sort, filter_type, None, app.advanced_filters, explorer_special=app.explorer_special_active
            )
            files = list_update._apply_virtual_staging_overlay(app, files, norm_filter)
            return list_update._sort_files(files, app.current_sort)

        if not app.search_term and folder_id is None:
            app.advanced_filters['path_filter'] = norm_filter
            
            if app.advanced_filters.get('is_starred') or app.advanced_filters.get('extension') not in [None, '']:
                filter_type = 'all'
                local_files = app.search_engine.load_files_paged(
                    'local', app.current_page, app.page_size, None, app.current_sort, filter_type, None, app.advanced_filters, explorer_special=app.explorer_special_active
                )
                drive_files = []
                if app.is_authenticated and app.show_drive_metadata:
                    drive_files = app.search_engine.load_files_paged(
                        'drive', app.current_page, app.page_size, None, app.current_sort, filter_type, None, app.advanced_filters, explorer_special=app.explorer_special_active
                    )
                all_files = local_files + drive_files
            else:
                local_files = app.search_engine.load_files_paged(
                    'local', app.current_page, app.page_size, None, app.current_sort, filter_type, None, app.advanced_filters, explorer_special=app.explorer_special_active
                )
                drive_files = []
                if app.is_authenticated and app.show_drive_metadata:
                    drive_files = app.search_engine.load_files_paged(
                        'drive', app.current_page, app.page_size, None, app.current_sort, filter_type, None, app.advanced_filters, explorer_special=app.explorer_special_active
                    )
                all_files = local_files + drive_files

            all_files = list_update._apply_virtual_staging_overlay(app, all_files, norm_filter)
            return list_update._sort_files(all_files, app.current_sort)
        else:
            app.advanced_filters['path_filter'] = norm_filter
            
            if app.advanced_filters.get('extension'):
                filter_type = 'all'
            files = app.search_engine.load_files_paged(
                source, app.current_page, app.page_size, app.search_term,
                app.current_sort, filter_type, folder_id, app.advanced_filters, explorer_special=app.explorer_special_active
            )
            if not app.show_drive_metadata:
                files = [f for f in files if not (
                    f.get('source') == 'drive' and not f.get('path'))]

            files = list_update._apply_virtual_staging_overlay(app, files, norm_filter)
            return list_update._sort_files(files, app.current_sort)

    @staticmethod
    def clear_display(app):
        app.all_loaded_label.hide()
        app.file_list_model.setFiles([])

        if hasattr(app.details_panel, 'thumbnail_thread') and app.details_panel.thumbnail_thread:
            try:
                if app.details_panel.thumbnail_thread.isRunning():
                    app.details_panel.thumbnail_thread.quit()
                    if not app.details_panel.thumbnail_thread.wait(2000):
                        app.details_panel.thumbnail_thread.terminate()
            except Exception:
                pass

        app.details_panel.thumbnail_worker = None
        app.details_panel.thumbnail_thread = None

    @staticmethod
    def load_next_batch(app):
        if app.is_loading or app.all_files_loaded:
            return

        app.is_loading = True
        app.loading_label.show()
        if hasattr(app, 'progress_bar') and app.progress_bar:
            app.progress_bar.setVisible(True)
            app.progress_bar.setRange(0, 0)
        if hasattr(app, 'status_bar') and app.status_bar:
            app.status_bar.showMessage("Carregando arquivos...", 0)

        try:
            if hasattr(app, 'indexer') and app.indexer:
                app.indexer.ensure_conn()
                
            # Sincronização rápida "on-the-fly" da pasta selecionada (apenas para a primeira página)
            if app.current_page == 0 and getattr(app, 'current_folder_path_filter', None):
                folder_path = app.current_folder_path_filter
                if os.path.isdir(folder_path):
                    try:
                        list_update._sync_single_folder_on_the_fly(app, folder_path)
                    except Exception as sf_err:
                        print(f"Erro no sync on-the-fly da pasta: {sf_err}")

            search_all_sources = bool(
                app.search_term and app.is_authenticated)
            source = app.current_view if not search_all_sources else None

            files_raw = list_update._load_files_for_filters(app, source)
            
            # Manter arquivos que existem fisicamente OU que são virtuais (staged move) OU do Drive
            files_to_add = [
                f for f in files_raw 
                if f.get('is_staged_move') or f.get('source') == 'drive' or (f.get('path') and os.path.exists(f['path']))
            ]

            if not files_to_add:
                app.all_files_loaded = True
                if not app.file_list_model.rowCount():
                    app.loading_label.setText("Nenhum arquivo encontrado.")
                    app.loading_label.show()
                else:
                    app.loading_label.hide()
                    app.all_loaded_label.show()
            else:
                app.file_list_model.addFiles(files_to_add)
                if len(files_to_add) < app.page_size:
                    app.all_files_loaded = True
                    app.all_loaded_label.show()
                app.current_page += 1
                app.loading_label.hide()

        except Exception as e:
            print(f"Erro ao carregar arquivos: {e}")
            import traceback
            traceback.print_exc()
            app.loading_label.setText("Erro ao carregar arquivos.")
            app.loading_label.show()
        finally:
            app.is_loading = False
            if hasattr(app, 'scroll_loading'):
                app.scroll_loading = False
            if hasattr(app, 'progress_bar') and app.progress_bar:
                app.progress_bar.setVisible(False)
            if hasattr(app, 'status_bar') and app.status_bar:
                app.status_bar.showMessage("Arquivos carregados.", 3000)

    @staticmethod
    def update_file_list(app, source=None):
        list_update.clear_display(app)
        if source is None:
            source = app.current_view
        files = list_update._load_files_for_filters(app, source)
        app.file_list_model.setFiles(files)
        app.all_files_loaded = len(files) < app.page_size
        app.current_page = 1 if files else 0

    @staticmethod
    def _sync_single_folder_on_the_fly(app, folder_path):
        import logging
        try:
            entries = os.listdir(folder_path)
        except Exception:
            return

        valid_entries = [e for e in entries if e.lower() != 'desktop.ini']
        app.indexer.ensure_conn()
        norm_folder = os.path.normpath(folder_path)

        app.indexer.cursor.execute(
            "SELECT file_id, name, path, mimeType, modifiedTime, size FROM files WHERE parentId = ? AND source = 'local'",
            (norm_folder,)
        )
        db_entries = app.indexer.cursor.fetchall()
        db_by_name = {row[1]: row for row in db_entries}

        items_to_save = []
        ids_to_delete = []
        actual_names = set()

        from src.utils.utils import extrair_ano_banco_imagens
        from datetime import datetime, timezone

        for name in valid_entries:
            entry_path = os.path.join(norm_folder, name)
            actual_names.add(name)

            try:
                is_dir = os.path.isdir(entry_path)
                modified = int(os.path.getmtime(entry_path))
                size = 0 if is_dir else os.path.getsize(entry_path)
            except Exception:
                continue

            db_row = db_by_name.get(name)

            if not db_row or db_row[4] != modified or db_row[5] != size:
                try:
                    created = int(os.path.getctime(entry_path))
                    data_mais_antiga = min(created, modified)
                    ano_caminho = extrair_ano_banco_imagens(entry_path)
                    if ano_caminho and ano_caminho < datetime.fromtimestamp(data_mais_antiga).year:
                        data_final = int(datetime(ano_caminho, 1, 1, tzinfo=timezone.utc).timestamp())
                    else:
                        data_final = data_mais_antiga
                except Exception:
                    data_final = modified

                item = {
                    'id': entry_path,
                    'name': name,
                    'path': entry_path,
                    'mimeType': 'folder' if is_dir else 'image/jpeg',
                    'source': 'local',
                    'description': '',
                    'thumbnailLink': '',
                    'thumbnailPath': '',
                    'size': size,
                    'modifiedTime': modified,
                    'createdTime': data_final,
                    'parentId': norm_folder,
                    'webContentLink': None
                }
                if db_row:
                    app.indexer.cursor.execute(
                        "SELECT description, thumbnailLink, thumbnailPath, webContentLink FROM files WHERE file_id = ?",
                        (db_row[0],)
                    )
                    old_data = app.indexer.cursor.fetchone()
                    if old_data:
                        item['description'] = old_data[0] or ''
                        item['thumbnailLink'] = old_data[1] or ''
                        item['thumbnailPath'] = old_data[2] or ''
                        item['webContentLink'] = old_data[3]

                items_to_save.append(item)

        for db_name, db_row in db_by_name.items():
            if db_name not in actual_names:
                ids_to_delete.append(db_row[0])

        if items_to_save:
            app.indexer.save_files_in_batch(items_to_save, source='local')
        if ids_to_delete:
            placeholders = ','.join('?' for _ in ids_to_delete)
            app.indexer.cursor.execute(f"DELETE FROM files WHERE file_id IN ({placeholders})", ids_to_delete)
            app.indexer.cursor.execute(f"DELETE FROM search_index WHERE file_id IN ({placeholders})", ids_to_delete)

        if items_to_save or ids_to_delete:
            app.indexer.conn.commit()
            logging.info(
                f"⚡ [ON-THE-FLY SYNC] {len(items_to_save)} adicionados/atualizados, {len(ids_to_delete)} removidos na pasta {norm_folder}"
            )
