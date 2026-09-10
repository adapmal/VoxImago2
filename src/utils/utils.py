
# utils.py - Utilidades gerais do Vox Imago
#
# Responsável por:
# - Carregar e salvar configurações do sistema
# - Formatar tamanhos de arquivos para exibição
# - Buscar correspondências entre arquivos locais e do Drive
# - Outras funções auxiliares genéricas usadas em todo o projeto

import os
from src.database.search import SearchEngine


def resolve_shared_folder_path(possible_names, base_paths=None, drive_letters=None):
    from src.utils.portable_paths import (
        SHARED_DRIVE_DIR_NAMES,
        available_drive_letters,
        discover_shared_folder,
    )
    if base_paths is None:
        base_paths = list(SHARED_DRIVE_DIR_NAMES)
    if drive_letters is None:
        try:
            from src.utils.config_manager import ConfigManager
            drive_letters = ConfigManager().get_allowed_drive_letters()
        except Exception:
            drive_letters = available_drive_letters()
        drive_letters = drive_letters or ["L:"]
    return discover_shared_folder(
        possible_names,
        drive_letters=drive_letters,
        base_names=base_paths,
    )


def filter_existing_files(file_records, path_key='caminho'):

    return [f for f in file_records if f.get(path_key) and os.path.exists(f[path_key])]


def get_existing_files(file_records):
    return [f for f in file_records if os.path.exists(f['path'])]


def load_settings():
    from src.utils.config_manager import ConfigManager, _SETTINGS_LOCK
    manager = ConfigManager()
    with _SETTINGS_LOCK:
        return dict(manager.settings)


def save_settings(settings):
    from src.utils.config_manager import ConfigManager, _SETTINGS_LOCK
    manager = ConfigManager()
    with _SETTINGS_LOCK:
        manager.settings.update(dict(settings or {}))
        manager.save_settings()


def format_size(size_in_bytes):
    if size_in_bytes < 1024:
        return f"{size_in_bytes} B"
    elif size_in_bytes < 1024**2:
        return f"{size_in_bytes / 1024:.2f} KB"
    elif size_in_bytes < 1024**3:
        return f"{size_in_bytes / 1024**2:.2f} MB"
    else:
        return f"{size_in_bytes / 1024**3:.2f} GB"

def extrair_ano_banco_imagens(path):
    partes = os.path.normpath(path).split(os.sep)
    try:
        idx = partes.index('Banco de Imagens')
        ano = int(partes[idx + 1])
        return ano
    except (ValueError, IndexError):
        return None


def send_to_recycle_bin(path):
    """
    Envia arquivos ou diretórios inteiros para a Lixeira do Windows nativa via Shell API (SHFileOperationW).
    Retorna True se foi movido com sucesso para a lixeira, ou False caso contrário.
    """
    if not path or not os.path.exists(path):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", wintypes.WORD),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", wintypes.LPVOID),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        FO_DELETE = 0x0003
        FOF_ALLOWUNDO = 0x0040
        FOF_NOCONFIRMATION = 0x0010
        FOF_SILENT = 0x0004
        FOF_NOERRORUI = 0x0400

        norm_path = os.path.abspath(path)
        pFrom = norm_path + '\0'

        fileop = SHFILEOPSTRUCTW()
        fileop.hwnd = 0
        fileop.wFunc = FO_DELETE
        fileop.pFrom = pFrom
        fileop.pTo = None
        fileop.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        fileop.fAnyOperationsAborted = False
        fileop.hNameMappings = None
        fileop.lpszProgressTitle = None

        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(fileop))
        if res == 0 and not fileop.fAnyOperationsAborted:
            return True
    except Exception:
        pass

    try:
        import send2trash
        send2trash.send2trash(path)
        return True
    except Exception:
        pass

    return False


def to_extended_path(path):
    """
    Retorna o caminho no formato estendido do Windows (\\\\?\\) se ultrapassar 240 caracteres,
    evitando erros de limite MAX_PATH (260 chars).
    """
    if not path or not isinstance(path, str):
        return path
    norm = os.path.abspath(path)
    if os.name == 'nt' and len(norm) >= 240 and not norm.startswith('\\\\?\\'):
        if norm.startswith('\\\\'):
            return '\\\\?\\UNC\\' + norm[2:]
        return '\\\\?\\' + norm
    return norm


def safe_move_file(src_path, dst_path, retries=3, delay=0.5):
    """
    Move um arquivo localmente de forma segura com tratamento de locks do Windows/InSync.
    Retorna (True, None) em caso de sucesso, ou (False, mensagem_de_erro) em caso de falha.
    """
    import shutil
    import time
    if not src_path or not os.path.exists(src_path):
        return False, f"Arquivo de origem não encontrado: {src_path}"

    dst_dir = os.path.dirname(dst_path)
    if dst_dir:
        os.makedirs(dst_dir, exist_ok=True)

    src_ext = to_extended_path(src_path)
    dst_ext = to_extended_path(dst_path)

    for attempt in range(retries):
        try:
            # Teste de lock: tentar abrir para leitura rápida antes de mover apenas se for arquivo
            if os.path.isfile(src_path):
                with open(src_ext, 'rb') as _:
                    pass
            shutil.move(src_ext, dst_ext)
            return True, None
        except PermissionError as pe:
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                return False, f"Arquivo em uso pelo Windows ou InSync: {pe}"
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                return False, str(e)

    return False, "Falha desconhecida ao mover arquivo"


def resolve_drive_folder_id_by_path(service, full_folder_path, drive_id=None, create_if_missing=False):
    """
    Resolve o fileId exato da pasta no Google Drive navegando nível por nível (Top-Down)
    a partir da raiz do Drive Compartilhado, evitando ambiguidades entre anos e pastas com nomes iguais.
    Se create_if_missing for True, cria as pastas intermediárias/finais faltantes no Drive.
    """
    if not service or not full_folder_path:
        return None

    from src.utils.config_manager import ConfigManager
    config_mgr = ConfigManager()
    if not drive_id:
        drive_id = config_mgr.get_current_drive_id() or '0AOB-ISqqs76_Uk9PVA'

    norm = os.path.normpath(full_folder_path)
    parts = norm.split(os.sep)

    try:
        idx = [p.lower() for p in parts].index('banco de imagens')
        rel_parts = parts[idx+1:]
    except ValueError:
        rel_parts = [p for p in parts if p and ':' not in p and p.lower() not in ('drives compartilhados', 'shared drives')]

    if not rel_parts:
        return drive_id

    current_parent_id = drive_id

    for seg in rel_parts:
        clean_seg = seg.replace("'", "\\'")
        q = f"'{current_parent_id}' in parents and name = '{clean_seg}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        kwargs = {
            'q': q,
            'supportsAllDrives': True,
            'includeItemsFromAllDrives': True,
            'fields': 'files(id, name)'
        }
        if drive_id:
            kwargs['corpora'] = 'drive'
            kwargs['driveId'] = drive_id
        else:
            kwargs['corpora'] = 'allDrives'

        res = service.files().list(**kwargs).execute()
        folders = res.get('files', [])

        # O Drive permite pastas irmas com o mesmo nome. Nesse caso nao existe
        # base segura para escolher uma delas automaticamente.
        if len(folders) > 1:
            return None

        if not folders:
            # Busca normalizada se nome tiver pequenas variações
            q_all = f"'{current_parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            res_all = service.files().list(q=q_all, supportsAllDrives=True, includeItemsFromAllDrives=True, fields='files(id, name)').execute()
            from src.database.search import SearchEngine
            norm_seg = SearchEngine(None).normalize_text(seg)
            folders = [
                cand for cand in res_all.get('files', [])
                if SearchEngine(None).normalize_text(cand.get('name', '')) == norm_seg
            ]
            if len(folders) > 1:
                return None

        if len(folders) == 1:
            current_parent_id = folders[0]['id']
        elif create_if_missing:
            try:
                folder_metadata = {
                    'name': seg,
                    'mimeType': 'application/vnd.google-apps.folder',
                    'parents': [current_parent_id]
                }
                res_create = service.files().create(body=folder_metadata, fields='id', supportsAllDrives=True).execute()
                current_parent_id = res_create.get('id')
            except Exception:
                return None
        else:
            return None

    return current_parent_id

