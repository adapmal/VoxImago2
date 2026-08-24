"""
Módulo match

Este módulo implementa algoritmos e funções para comparação e correspondência de arquivos
entre o sistema local e o Google Drive no VoxImago.MB. Fornece utilitários para identificar
arquivos duplicados, verificar similaridade, e auxiliar nos processos de sincronização e fusão.
"""

from src.database.search import SearchEngine
import os
import string
import unicodedata

DRIVE_SHORTCUT_EXTENSIONS = [
    '.gdoc', '.gsheet', '.gslides', '.gdraw', '.gform']


def normalize_aggressive(name):
    if not name:
        return ''

    name_wo_ext, ext = os.path.splitext(name)
    base_name = name_wo_ext

    name_norm = unicodedata.normalize('NFD', base_name)
    name_no_accents = ''.join(
        c for c in name_norm if unicodedata.category(c) != 'Mn')
    table = str.maketrans('', '', string.punctuation + string.whitespace)
    return name_no_accents.translate(table).lower()


def normalize_name_only(name):
    if not name:
        return ''

    name_base = os.path.splitext(name)[0]

    name_norm = unicodedata.normalize('NFD', name_base)
    name_clean = ''.join(
        c for c in name_norm if unicodedata.category(c) != 'Mn')
    table = str.maketrans('', '', string.punctuation + string.whitespace)
    return name_clean.translate(table).lower()


def find_local_matches(drive_file, local_files_cursor, drive_service=None):
    import logging
    import time

    matches = []
    drive_name = drive_file.get('name', '')
    drive_size = int(drive_file.get('size') or 0)
    drive_id = drive_file.get('id', '')
    drive_parent_name = drive_file.get('parent_name', '')

    if not drive_name:
        return matches

    # Se não temos o nome da pasta pai do Drive mas temos o parentId e o drive_service, tenta obter
    if not drive_parent_name and drive_service and drive_file.get('parentId'):
        try:
            p_info = drive_service.files().get(fileId=drive_file['parentId'], supportsAllDrives=True, fields='name').execute()
            drive_parent_name = p_info.get('name', '')
        except Exception:
            pass

    # 1. Buscar todos os candidatos locais com o mesmo nome (exato ou normalizado)
    local_files_cursor.execute(
        "SELECT file_id, name, path, size FROM files WHERE source='local' AND LOWER(name)=LOWER(?)",
        (drive_name,)
    )
    candidates = local_files_cursor.fetchall()

    if not candidates:
        search_engine = SearchEngine(None)
        drive_name_norm = search_engine.normalize_text(drive_name)
        if drive_name_norm:
            local_files_cursor.execute(
                "SELECT file_id, name, path, size FROM files WHERE source='local' AND name_normalized=?",
                (drive_name_norm,)
            )
            candidates = local_files_cursor.fetchall()

    if not candidates:
        return []

    # 2. Se há apenas 1 candidato local, valida tamanho e pasta para evitar falso positivo em nomes comuns
    if len(candidates) == 1:
        cand_id, cand_name, cand_path, cand_size = candidates[0]
        # Se for um nome muito genérico de câmera (ex: IMG_0091.jpg, DSC07402.arw) e tivermos info de pasta/tamanho
        is_generic_name = any(cand_name.upper().startswith(p) for p in ('IMG_', 'DSC', 'DSC0', 'DSC_', 'C00', '_MG_', 'SAM_', '2024', '2023', '2022', '2021', '2020', '2026'))
        
        if is_generic_name and drive_parent_name and cand_path:
            parent_folder = os.path.basename(os.path.dirname(cand_path)).lower()
            if drive_parent_name.lower() not in parent_folder and parent_folder not in drive_parent_name.lower():
                # Pastas totalmente diferentes em nome genérico -> não associar aleatoriamente
                return []
                
        return [cand_id]

    # 3. Desambiguação de múltiplos arquivos homônimos:
    # A. Prioridade 1: Match exato por nome da pasta pai
    if drive_parent_name:
        for cand_id, cand_name, cand_path, cand_size in candidates:
            if cand_path:
                local_pname = os.path.basename(os.path.dirname(cand_path)).lower()
                if drive_parent_name.lower() in local_pname or local_pname in drive_parent_name.lower():
                    return [cand_id]

    # B. Prioridade 2: Match por tamanho exato (tolerância de até 512 bytes)
    if drive_size > 0:
        for cand_id, cand_name, cand_path, cand_size in candidates:
            c_size = int(cand_size or 0)
            if c_size > 0 and abs(c_size - drive_size) <= 512:
                return [cand_id]

    # Se não foi possível desambiguar com segurança, NÃO retorna match aleatório para não contaminar
    return []
