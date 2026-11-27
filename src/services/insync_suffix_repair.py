"""Correção segura de sufixos locais '(n)' deixados pelo InSync após conflitos antigos no Drive.

Fluxo por mini-lotes:
1. Análise (somente leitura): localiza arquivos locais terminando em ' (n)' antes da extensão
   (preservando anos 1900..2100), confirma que o nome base (sem o último '(n)') NÃO existe
   na pasta local, confirma no Google Drive ao vivo que existe EXATAMENTE 1 arquivo com o nome
   base naquela pasta (se houver 2+, indica o Desambiguador) e comprova MD5 + tamanho idênticos.
2. Execução (somente após aprovação explícita):
   - Etapa 1: renomeia no Drive para um nome temporário único (.__insync_fix__) e aguarda o InSync
     refletir no HD (com timeout e reversão automática para o nome original caso não sincronize).
   - Etapa 2: renomeia de volta no Drive para o nome original e aguarda o InSync confirmar no HD,
     verificando o MD5 final e atualizando o índice local SQLite.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import sqlite3
import stat
import time
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from PyQt6.QtCore import QLockFile, QObject, pyqtSignal

from src.database.database import FileIndexer, to_canonical_id
from src.services.drive_retry import execute_drive_read
from src.services.hash_deduplicator import (
    DedupCancelled,
    FOLDER_MIME,
    escape_query,
    private_service,
)
from src.services.shortcut_migration import MigrationAPI
from src.services.snapshot_management import assert_queue_empty, local_backup
from src.utils.config_manager import ConfigManager

ONLINE_ONLY_ATTRIBUTES = 0x1000 | 0x40000 | 0x400000
_SINGLE_TRAILING_COPY_RE = re.compile(r'^(.*?)(\s*\((\d+)\))$')
INSYNC_SCAN_FIELDS = (
    'id,name,mimeType,parents,driveId,trashed,size,md5Checksum,'
    'webViewLink,capabilities(canEdit)'
)
TEMP_MARKER = '.__insync_fix__'


@dataclass
class InsyncSuffixItem:
    local_path: str
    folder_path: str
    rel_folder: str
    local_name: str
    base_name: str
    drive_id: str = ''
    drive_name: str = ''
    drive_parent_id: str = ''
    drive_web_link: str = ''
    temp_name: str = ''
    size: int = 0
    local_md5: str = ''
    remote_md5: str = ''
    status: str = 'blocked'  # eligible | blocked | needs_disambiguation | completed | failed | rolled_back
    reason: str = ''


def _fold(text: str) -> str:
    return unicodedata.normalize('NFC', str(text or '')).casefold()


def strip_single_trailing_copy_suffix(filename: str | None) -> tuple[str, int] | None:
    """Remove apenas UM sufixo ' (n)' ao final do nome base (preservando anos 1900..2100).

    Exemplos:
      'foto (2).jpg' -> ('foto.jpg', 2)
      'fernando nunes (2) (2).jpg' -> ('fernando nunes (2).jpg', 2)
      'foto (2021).jpg' -> None
    """
    raw = str(filename or '')
    if not raw:
        return None
    stem, ext = ntpath.splitext(raw)
    match = _SINGLE_TRAILING_COPY_RE.match(stem)
    if not match:
        return None
    base_stem = match.group(1).rstrip()
    num = int(match.group(3))
    if not base_stem or num <= 0 or (1900 <= num <= 2100):
        return None
    return f'{base_stem}{ext}', num


def _is_online_only(path: str) -> bool:
    try:
        info = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    return bool(getattr(info, 'st_file_attributes', 0) & ONLINE_ONLY_ATTRIBUTES)


def compute_local_md5(path: str, cancel_check=None) -> tuple[str, int, str]:
    """Retorna (md5_hex, size, erro_ou_vazio) verificando se o arquivo mudou durante a leitura."""
    try:
        before = os.stat(path)
    except OSError as exc:
        return '', 0, f'Arquivo local inacessível: {exc}'
    if not stat.S_ISREG(before.st_mode):
        return '', 0, 'Não é um arquivo regular.'
    if getattr(before, 'st_file_attributes', 0) & ONLINE_ONLY_ATTRIBUTES:
        return '', before.st_size, 'Arquivo marcado como somente online; não aberto.'
    digest = hashlib.md5()
    try:
        with open(path, 'rb') as stream:
            while True:
                if cancel_check and cancel_check():
                    raise DedupCancelled('Operação cancelada.')
                chunk = stream.read(4 * 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            fd_info = os.fstat(stream.fileno())
        after = os.stat(path)
    except DedupCancelled:
        raise
    except OSError as exc:
        return '', before.st_size, f'Erro ao ler arquivo local: {exc}'
    if (
        fd_info.st_size != before.st_size
        or fd_info.st_mtime_ns != before.st_mtime_ns
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        return '', before.st_size, 'Arquivo local foi alterado durante a leitura.'
    return digest.hexdigest().lower(), before.st_size, ''


def make_safe_temp_name(drive_name: str, existing_folded_names: set[str]) -> str:
    stem, ext = ntpath.splitext(drive_name)
    candidate = f'{stem}{TEMP_MARKER}{ext}'
    counter = 1
    while _fold(candidate) in existing_folded_names:
        candidate = f'{stem}{TEMP_MARKER}{counter}{ext}'
        counter += 1
    return candidate


def _extract_rel_parts(local_path: str, roots: list[str]) -> tuple[str, ...] | None:
    norm_local = ntpath.normpath(str(local_path))
    fold_local = _fold(norm_local)
    matched = []
    for root in roots:
        norm_root = ntpath.normpath(str(root)).rstrip('\\/')
        fold_root = _fold(norm_root)
        if fold_local.startswith(fold_root + '\\') or fold_local.startswith(fold_root + '/'):
            matched.append((len(fold_root), fold_root))
    if not matched:
        return None
    matched.sort(key=lambda x: x[0], reverse=True)
    rel = fold_local[matched[0][0] + 1:]
    parts = [p for p in rel.replace('/', '\\').split('\\') if p]
    return tuple(parts) if parts else None


def build_drive_folder_index(items: list[dict], drive_id: str):
    """Constrói índice de pastas e filhos no Drive exigindo caminho único desde a raiz."""
    folders = {}
    children_by_parent: dict[str, list[dict]] = defaultdict(list)
    folder_siblings: dict[tuple[str, str], list[str]] = defaultdict(list)

    for item in items:
        fid = item.get('id')
        if not fid or item.get('trashed'):
            continue
        if drive_id and item.get('driveId') not in (None, '', drive_id):
            continue
        parents = item.get('parents') or []
        if len(parents) != 1:
            continue
        pid = parents[0]
        children_by_parent[pid].append(item)
        if item.get('mimeType') == FOLDER_MIME:
            folders[fid] = item
            folder_siblings[(pid, _fold(item.get('name', '')))].append(fid)

    folder_by_path: dict[tuple[str, ...], str | None] = {(): drive_id}
    for fid, folder in folders.items():
        rev = []
        cur = fid
        seen = set()
        valid = True
        while cur != drive_id:
            if cur in seen or cur not in folders:
                valid = False
                break
            seen.add(cur)
            f = folders[cur]
            parents = f.get('parents') or []
            if len(parents) != 1:
                valid = False
                break
            pid = parents[0]
            if len(folder_siblings[(pid, _fold(f.get('name', '')))]) > 1:
                valid = False
                break
            rev.append(_fold(f.get('name', '')))
            cur = pid
        if not valid:
            continue
        key = tuple(reversed(rev))
        if key in folder_by_path:
            folder_by_path[key] = None  # ambígua
        else:
            folder_by_path[key] = fid

    return folder_by_path, children_by_parent


def _resolve_active_roots_and_drive(config: ConfigManager) -> tuple[list[str], str]:
    if config.is_sandbox():
        root = config.get_sandbox_path()
        roots = [root] if root else []
    else:
        roots = config.get_resolved_scan_paths(persist=False)
    drive_id = config.get_current_drive_id() or ''
    excluded = config.get_excluded_drive_letters()
    filtered = []
    for r in roots:
        drv = ntpath.splitdrive(ntpath.normpath(r))[0].upper()
        if drv and drv in excluded:
            continue
        if os.path.isdir(r):
            filtered.append(ntpath.normpath(r))
    return filtered, drive_id


def _find_local_copy_suffix_files(roots: list[str], cancel_check=None) -> list[tuple[str, str, str, str]]:
    """Retorna lista de (local_path, folder_path, local_name, base_name) em todas as raízes."""
    found = []
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            if cancel_check and cancel_check():
                raise DedupCancelled('Análise cancelada.')
            for fname in filenames:
                stripped = strip_single_trailing_copy_suffix(fname)
                if stripped is None:
                    continue
                base_name, _num = stripped
                full_path = os.path.join(dirpath, fname)
                found.append((full_path, dirpath, fname, base_name))
    found.sort(key=lambda t: _fold(t[0]))
    return found


def scan_insync_suffix_candidates(
    *,
    roots: list[str],
    drive_id: str,
    drive_items: list[dict],
    cancel_check=None,
    progress_cb=None,
) -> list[InsyncSuffixItem]:
    """Analisa arquivos locais com '(n)' contra o inventário do Drive (somente leitura)."""
    if progress_cb:
        progress_cb(10, 'Procurando arquivos locais com sufixo (n)...')
    local_candidates = _find_local_copy_suffix_files(roots, cancel_check=cancel_check)
    if not local_candidates:
        if progress_cb:
            progress_cb(100, 'Nenhum arquivo com sufixo (n) encontrado.')
        return []

    if progress_cb:
        progress_cb(40, f'Conferindo {len(local_candidates)} arquivo(s) contra o Google Drive...')
    folder_by_path, children_by_parent = build_drive_folder_index(drive_items, drive_id)

    results: list[InsyncSuffixItem] = []
    total = len(local_candidates)
    for idx, (local_path, folder_path, local_name, base_name) in enumerate(local_candidates, 1):
        if cancel_check and cancel_check():
            raise DedupCancelled('Análise cancelada.')
        rel_parts = _extract_rel_parts(local_path, roots)
        rel_folder_parts = rel_parts[:-1] if rel_parts else ()
        rel_folder = '\\'.join(rel_folder_parts)
        item = InsyncSuffixItem(
            local_path=local_path,
            folder_path=folder_path,
            rel_folder=rel_folder,
            local_name=local_name,
            base_name=base_name,
        )

        # 1. Conferir se já existe arquivo com nome base na pasta local (case-insensitive)
        try:
            local_entries = os.listdir(folder_path)
        except OSError as exc:
            item.status = 'blocked'
            item.reason = f'Pasta local inacessível: {exc}'
            results.append(item)
            continue
        local_folded = {_fold(name) for name in local_entries}
        if _fold(base_name) in local_folded:
            item.status = 'blocked'
            item.reason = 'Já existe um arquivo com o nome base nesta pasta local.'
            results.append(item)
            continue

        # 2. Localizar pasta exata no Drive
        if rel_folder_parts not in folder_by_path or folder_by_path[rel_folder_parts] is None:
            item.status = 'blocked'
            item.reason = 'Pasta não encontrada ou duplicada no Google Drive.'
            results.append(item)
            continue
        parent_id = folder_by_path[rel_folder_parts]
        siblings = [
            child for child in children_by_parent.get(parent_id, ())
            if not child.get('trashed') and child.get('mimeType') != FOLDER_MIME
        ]

        # Se o próprio nome com '(n)' já existe no Drive, não é sufixo fantasma do InSync.
        if any(_fold(c.get('name', '')) == _fold(local_name) for c in siblings):
            item.status = 'blocked'
            item.reason = 'O arquivo com sufixo (n) existe no Google Drive; não é resíduo do InSync.'
            results.append(item)
            continue

        base_matches = [c for c in siblings if _fold(c.get('name', '')) == _fold(base_name)]
        if len(base_matches) > 1:
            item.status = 'needs_disambiguation'
            item.reason = (
                f'Há {len(base_matches)} arquivos com o nome "{base_name}" nesta pasta do Drive; '
                'use "Desambiguar nomes no Drive" primeiro.'
            )
            results.append(item)
            continue
        if len(base_matches) == 0:
            item.status = 'blocked'
            item.reason = 'Arquivo com o nome base não foi encontrado nesta pasta do Drive.'
            results.append(item)
            continue

        remote = base_matches[0]
        item.drive_id = remote['id']
        item.drive_name = remote.get('name', base_name)
        item.drive_parent_id = parent_id
        item.drive_web_link = remote.get('webViewLink') or f'https://drive.google.com/file/d/{remote["id"]}/view?usp=drivesdk'
        item.remote_md5 = (remote.get('md5Checksum') or '').lower()
        remote_size = int(remote.get('size') or 0)

        caps = remote.get('capabilities') or {}
        if caps.get('canEdit') is False:
            item.status = 'blocked'
            item.reason = 'Sem permissão de edição/renomeação no Google Drive.'
            results.append(item)
            continue

        if not item.remote_md5 or remote_size <= 0:
            item.status = 'blocked'
            item.reason = 'Arquivo no Drive sem tamanho ou MD5 disponível.'
            results.append(item)
            continue

        local_md5, local_size, md5_err = compute_local_md5(local_path, cancel_check=cancel_check)
        item.size = local_size
        item.local_md5 = local_md5
        if md5_err:
            item.status = 'blocked'
            item.reason = md5_err
            results.append(item)
            continue
        if local_size != remote_size or local_md5 != item.remote_md5:
            item.status = 'blocked'
            item.reason = 'Conteúdo (tamanho/MD5) do arquivo local difere do arquivo no Drive.'
            results.append(item)
            continue

        existing_names = local_folded | {_fold(c.get('name', '')) for c in siblings}
        item.temp_name = make_safe_temp_name(item.drive_name, existing_names)
        item.status = 'eligible'
        item.reason = 'Conteúdo idêntico (MD5) e único no Drive; pronto para ressincronizar no InSync.'
        results.append(item)
        if progress_cb and (idx == 1 or idx % 10 == 0 or idx == total):
            pct = 40 + int(60 * idx / max(1, total))
            progress_cb(pct, f'Analisados {idx}/{total} arquivo(s)...')

    return results


class PendingTempJournal:
    """Registra renomeios temporários em andamento para permitir restauração segura se interrompido."""

    def __init__(self, db_name: str):
        self.path = Path(db_name).resolve().parent / 'insync_suffix_repair' / 'pending_temp.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, data: dict[str, dict]) -> None:
        tmp = self.path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, self.path)

    def add(self, drive_id: str, orig_name: str, temp_name: str, folder_path: str) -> None:
        data = self.load()
        data[drive_id] = {
            'drive_id': drive_id,
            'orig_name': orig_name,
            'temp_name': temp_name,
            'folder_path': folder_path,
            'recorded_at': time.time(),
        }
        self.save(data)

    def remove(self, drive_id: str) -> None:
        data = self.load()
        if drive_id in data:
            del data[drive_id]
            self.save(data)


def _update_local_db_path_and_link(
    db_name: str,
    *,
    old_local_path: str,
    new_local_path: str,
    new_name: str,
    drive_id: str,
    drive_link: str,
    size: int,
    md5_hex: str,
) -> None:
    """Atualiza o registro local em files, search_index e drive_link_validation após a troca."""
    old_id = to_canonical_id(old_local_path)
    new_id = to_canonical_id(new_local_path)
    if not db_name or not os.path.exists(db_name):
        return
    conn = sqlite3.connect(db_name, timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
    try:
        cur = conn.cursor()
        cur.execute('BEGIN IMMEDIATE')
        old_row = cur.execute(
            "SELECT file_id, name, path, size, modified_time, created_time, mime_type, "
            "parent_id, description, exif_date_taken FROM files WHERE file_id = ? AND source = 'local'",
            (old_id,),
        ).fetchone()
        new_exists = cur.execute(
            "SELECT 1 FROM files WHERE file_id = ? AND source = 'local'",
            (new_id,),
        ).fetchone()
        if old_row and not new_exists:
            cur.execute(
                "UPDATE files SET file_id = ?, name = ?, path = ?, webContentLink = ? "
                "WHERE file_id = ? AND source = 'local'",
                (new_id, new_name, new_local_path, drive_link, old_id),
            )
            has_si = cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_index'"
            ).fetchone()
            if has_si:
                cur.execute(
                    "UPDATE search_index SET file_id = ?, name = ?, path = ? "
                    "WHERE file_id = ? AND source = 'local'",
                    (new_id, new_name, new_local_path, old_id),
                )
        elif new_exists:
            cur.execute(
                "UPDATE files SET name = ?, path = ?, webContentLink = ? "
                "WHERE file_id = ? AND source = 'local'",
                (new_name, new_local_path, drive_link, new_id),
            )
            if old_row and old_id != new_id:
                cur.execute("DELETE FROM files WHERE file_id = ? AND source = 'local'", (old_id,))
                has_si = cur.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_index'"
                ).fetchone()
                if has_si:
                    cur.execute("DELETE FROM search_index WHERE file_id = ? AND source = 'local'", (old_id,))

        has_val = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='drive_link_validation'"
        ).fetchone()
        if has_val:
            if old_id != new_id:
                cur.execute("DELETE FROM drive_link_validation WHERE local_file_id = ?", (old_id,))
            cur.execute(
                """
                INSERT INTO drive_link_validation (
                    local_file_id, drive_id, status, remote_name, remote_size,
                    remote_md5, local_md5, hierarchy_score, details, validated_at
                ) VALUES (?, ?, 'valid_hash', ?, ?, ?, ?, 1, 'insync_suffix_repair', ?)
                ON CONFLICT(local_file_id) DO UPDATE SET
                    drive_id=excluded.drive_id,
                    status=excluded.status,
                    remote_name=excluded.remote_name,
                    remote_size=excluded.remote_size,
                    remote_md5=excluded.remote_md5,
                    local_md5=excluded.local_md5,
                    details=excluded.details,
                    validated_at=excluded.validated_at
                """,
                (new_id, drive_id, new_name, size, md5_hex, md5_hex, int(time.time())),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _folder_has_name(folder_path: str, target_name: str) -> bool:
    try:
        entries = os.listdir(folder_path)
    except OSError:
        return False
    target_f = _fold(target_name)
    return any(_fold(e) == target_f for e in entries)


def _find_exact_name_in_folder(folder_path: str, target_name: str) -> str | None:
    try:
        entries = os.listdir(folder_path)
    except OSError:
        return None
    target_f = _fold(target_name)
    for e in entries:
        if _fold(e) == target_f:
            return os.path.join(folder_path, e)
    return None


def execute_insync_suffix_repair(
    items: list[InsyncSuffixItem],
    *,
    db_name: str,
    drive_id: str,
    get_item_fn,
    list_children_fn,
    rename_item_fn,
    wait_timeout_sec: float = 90.0,
    poll_interval_sec: float = 0.5,
    batch_size: int = 10,
    cancel_check=None,
    progress_cb=None,
    create_backup: bool = True,
) -> dict:
    """Executa a ressincronização em 2 etapas (orig -> temp -> orig) por mini-lotes com rollback."""
    journal = PendingTempJournal(db_name)
    if create_backup and db_name and os.path.exists(db_name):
        local_backup(db_name, 'insync_suffix_repair')

    stats = {
        'total_selected': len(items),
        'completed': 0,
        'rolled_back': 0,
        'failed': 0,
        'skipped': 0,
        'cancelled': False,
        'errors': [],
    }
    if not items:
        return stats

    total = len(items)
    done_so_far = 0
    for batch_start in range(0, total, max(1, batch_size)):
        if cancel_check and cancel_check():
            stats['cancelled'] = True
            break
        batch = items[batch_start:batch_start + max(1, batch_size)]

        # 1) Conferência ao vivo antes da Etapa 1 + renomear para temp no Drive
        step1_active: list[InsyncSuffixItem] = []
        for it in batch:
            if cancel_check and cancel_check():
                stats['cancelled'] = True
                break
            if not os.path.isfile(it.local_path):
                it.status = 'failed'
                it.reason = 'Arquivo local (n) não encontrado antes da execução.'
                stats['skipped'] += 1
                done_so_far += 1
                continue
            if _folder_has_name(it.folder_path, it.drive_name):
                it.status = 'failed'
                it.reason = 'Arquivo com nome base já existe na pasta local.'
                stats['skipped'] += 1
                done_so_far += 1
                continue
            try:
                live = get_item_fn(it.drive_id)
                sibs = list_children_fn(it.drive_parent_id)
            except DedupCancelled:
                stats['cancelled'] = True
                break
            except Exception as exc:
                it.status = 'failed'
                it.reason = f'Erro ao conferir item no Drive: {exc}'
                stats['failed'] += 1
                stats['errors'].append(f'{it.local_name}: {it.reason}')
                done_so_far += 1
                continue

            if not live or live.get('trashed'):
                it.status = 'failed'
                it.reason = 'Arquivo não encontrado ou na Lixeira do Drive.'
                stats['skipped'] += 1
                done_so_far += 1
                continue
            if _fold(live.get('name', '')) != _fold(it.drive_name):
                it.status = 'failed'
                it.reason = f'Nome no Drive mudou desde a análise ({live.get("name")}).'
                stats['skipped'] += 1
                done_so_far += 1
                continue
            if (live.get('md5Checksum') or '').lower() != it.remote_md5:
                it.status = 'failed'
                it.reason = 'Conteúdo (MD5) no Drive mudou desde a análise.'
                stats['skipped'] += 1
                done_so_far += 1
                continue
            same_name = [
                s for s in sibs
                if not s.get('trashed')
                and s.get('mimeType') != FOLDER_MIME
                and _fold(s.get('name', '')) == _fold(it.drive_name)
            ]
            if len(same_name) != 1 or same_name[0]['id'] != it.drive_id:
                it.status = 'needs_disambiguation'
                it.reason = 'Conflito de nomes surgiu na pasta do Drive; use Desambiguar nomes.'
                stats['skipped'] += 1
                done_so_far += 1
                continue
            existing_folded = {_fold(s.get('name', '')) for s in sibs if not s.get('trashed')}
            try:
                existing_folded |= {_fold(e) for e in os.listdir(it.folder_path)}
            except OSError:
                pass
            it.temp_name = make_safe_temp_name(it.drive_name, existing_folded)

            # Etapa 1: renomear no Drive -> temp_name
            journal.add(it.drive_id, it.drive_name, it.temp_name, it.folder_path)
            try:
                rename_item_fn(it.drive_id, it.temp_name)
            except DedupCancelled:
                journal.remove(it.drive_id)
                stats['cancelled'] = True
                break
            except Exception as exc:
                journal.remove(it.drive_id)
                it.status = 'failed'
                it.reason = f'Falha ao renomear para temporário no Drive: {exc}'
                stats['failed'] += 1
                stats['errors'].append(f'{it.local_name}: {it.reason}')
                done_so_far += 1
                continue
            step1_active.append(it)

        if not step1_active:
            if stats['cancelled']:
                break
            continue

        # 2) Espera 1: aguardar InSync refletir temp_name no HD
        if progress_cb:
            pct = int(100 * done_so_far / max(1, total))
            progress_cb(
                pct,
                f'Etapa 1/2: aguardando InSync sincronizar nome temporário ({len(step1_active)} arquivo(s))...',
            )
        pending_step1 = list(step1_active)
        synced_step1: list[InsyncSuffixItem] = []
        deadline1 = time.monotonic() + max(0.0, float(wait_timeout_sec))
        while pending_step1:
            still = []
            for it in pending_step1:
                if _folder_has_name(it.folder_path, it.temp_name) and not os.path.exists(it.local_path):
                    synced_step1.append(it)
                else:
                    still.append(it)
            pending_step1 = still
            if not pending_step1:
                break
            if cancel_check and cancel_check():
                stats['cancelled'] = True
                break
            if time.monotonic() >= deadline1:
                break
            time.sleep(min(poll_interval_sec, max(0.01, deadline1 - time.monotonic())))

        # Reverter imediatamente no Drive qualquer item que não concluiu a Etapa 1!
        for it in pending_step1:
            try:
                rename_item_fn(it.drive_id, it.drive_name)
                journal.remove(it.drive_id)
                it.status = 'rolled_back'
                it.reason = (
                    'Cancelado na etapa 1; nome original restaurado no Drive.'
                    if stats['cancelled']
                    else f'InSync não refletiu o nome temporário em {int(wait_timeout_sec)}s; nome original restaurado no Drive.'
                )
                stats['rolled_back'] += 1
            except Exception as exc:
                it.status = 'failed'
                it.reason = f'Timeout na etapa 1 e falha ao reverter no Drive ({it.temp_name}): {exc}'
                stats['failed'] += 1
                stats['errors'].append(f'{it.local_name}: {it.reason}')
            done_so_far += 1

        # 3) Etapa 2: renomear de volta no Drive -> drive_name original
        # (Mesmo se cancelado durante Espera 1, todos os que JÁ sincronizaram como temp precisam voltar ao nome original!)
        step2_active: list[InsyncSuffixItem] = []
        for it in synced_step1:
            try:
                rename_item_fn(it.drive_id, it.drive_name)
                journal.remove(it.drive_id)
                step2_active.append(it)
            except Exception as exc:
                it.status = 'failed'
                it.reason = f'Falha ao restaurar nome original no Drive ({it.temp_name} -> {it.drive_name}): {exc}'
                stats['failed'] += 1
                stats['errors'].append(f'{it.local_name}: {it.reason}')
                done_so_far += 1

        if not step2_active:
            if stats['cancelled']:
                break
            continue

        # 4) Espera 2: aguardar InSync refletir o nome original limpo no HD
        if progress_cb:
            pct = int(100 * done_so_far / max(1, total))
            progress_cb(
                pct,
                f'Etapa 2/2: aguardando InSync concluir nome final ({len(step2_active)} arquivo(s))...',
            )
        pending_step2 = list(step2_active)
        synced_step2: list[InsyncSuffixItem] = []
        deadline2 = time.monotonic() + max(0.0, float(wait_timeout_sec))
        while pending_step2:
            still = []
            for it in pending_step2:
                if _folder_has_name(it.folder_path, it.drive_name) and not _folder_has_name(it.folder_path, it.temp_name):
                    synced_step2.append(it)
                else:
                    still.append(it)
            pending_step2 = still
            if not pending_step2:
                break
            if time.monotonic() >= deadline2:
                break
            time.sleep(min(poll_interval_sec, max(0.01, deadline2 - time.monotonic())))

        for it in synced_step2:
            final_path = _find_exact_name_in_folder(it.folder_path, it.drive_name) or os.path.join(it.folder_path, it.drive_name)
            final_md5, final_size, err = compute_local_md5(final_path)
            if err or final_md5 != it.remote_md5:
                it.status = 'failed'
                it.reason = f'Nome final sincronizado, mas conferência MD5 falhou: {err or "MD5 divergente"}.'
                stats['failed'] += 1
                stats['errors'].append(f'{it.local_name}: {it.reason}')
            else:
                try:
                    _update_local_db_path_and_link(
                        db_name,
                        old_local_path=it.local_path,
                        new_local_path=final_path,
                        new_name=os.path.basename(final_path),
                        drive_id=it.drive_id,
                        drive_link=it.drive_web_link,
                        size=final_size,
                        md5_hex=final_md5,
                    )
                except Exception as exc:
                    it.status = 'completed'
                    it.reason = f'Sincronizado no HD com sucesso (reavalie o índice local: {exc}).'
                else:
                    it.status = 'completed'
                    it.reason = 'Corrigido com sucesso no Drive, InSync e índice local.'
                stats['completed'] += 1
            done_so_far += 1

        for it in pending_step2:
            it.status = 'failed'
            it.reason = (
                f'Nome original já foi restaurado no Drive, mas o InSync ainda não renomeou "{it.temp_name}" '
                f'em {int(wait_timeout_sec)}s. Aguarde o InSync concluir e reindexe.'
            )
            stats['failed'] += 1
            stats['errors'].append(f'{it.local_name}: {it.reason}')
            done_so_far += 1

        if progress_cb:
            progress_cb(int(100 * done_so_far / max(1, total)), f'Processados {done_so_far}/{total} arquivo(s)...')
        if stats['cancelled']:
            break

    return stats


def restore_pending_temp_names(
    db_name: str,
    *,
    get_item_fn,
    rename_item_fn,
) -> dict:
    """Restaura nomes originais no Drive caso uma execução anterior tenha sido interrompida no meio."""
    journal = PendingTempJournal(db_name)
    pending = journal.load()
    restored = 0
    errors = []
    for fid, info in list(pending.items()):
        orig_name = info.get('orig_name', '')
        temp_name = info.get('temp_name', '')
        try:
            live = get_item_fn(fid)
            if not live or live.get('trashed'):
                journal.remove(fid)
                continue
            if _fold(live.get('name', '')) == _fold(orig_name):
                journal.remove(fid)
                restored += 1
                continue
            if _fold(live.get('name', '')) == _fold(temp_name):
                rename_item_fn(fid, orig_name)
                journal.remove(fid)
                restored += 1
            else:
                errors.append(f'{fid}: nome atual inesperado ({live.get("name")}).')
        except Exception as exc:
            errors.append(f'{fid}: {exc}')
    return {'restored': restored, 'remaining': len(journal.load()), 'errors': errors}


class InsyncSuffixRepairWorker(QObject):
    """Worker em QThread para analisar, executar ou restaurar correções de sufixo '(n)'."""

    progress = pyqtSignal(int, str)
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(
        self,
        db_name: str,
        service,
        *,
        mode: str = 'scan',  # scan | execute | restore
        items: list[InsyncSuffixItem] | None = None,
        wait_timeout_sec: float = 90.0,
        batch_size: int = 10,
    ):
        super().__init__()
        self.db_name = db_name
        self.service = service
        self.mode = mode
        self.items = list(items or [])
        self.wait_timeout_sec = wait_timeout_sec
        self.batch_size = batch_size
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _is_cancelled(self) -> bool:
        return self._cancelled

    def run(self):
        lock_path = Path(self.db_name).resolve().parent / 'insync_suffix_repair' / 'op.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(lock_path))
        lock.setStaleLockTime(0)
        if not lock.tryLock(100):
            self.failed.emit('Já existe uma correção de sufixos do InSync em andamento.')
            return
        try:
            config = ConfigManager()
            roots, drive_id = _resolve_active_roots_and_drive(config)
            if not drive_id:
                raise ValueError('ID do Google Drive não configurado.')
            if not self.service:
                raise ValueError('Conecte-se ao Google Drive antes de continuar.')

            svc = private_service(self.service)
            api = MigrationAPI(
                svc,
                drive_id,
                cancelled=self._is_cancelled,
                on_wait=lambda msg: self.progress.emit(-1, msg),
            )

            def get_item(fid: str) -> dict:
                return execute_drive_read(
                    lambda: svc.files().get(
                        fileId=fid, supportsAllDrives=True, fields=INSYNC_SCAN_FIELDS,
                    ),
                    cancelled=self._is_cancelled,
                    cancel_error=DedupCancelled,
                )

            def list_children(pid: str) -> list[dict]:
                return list(api.listing(
                    f"'{escape_query(pid)}' in parents and trashed = false",
                    drive=drive_id,
                    fields=INSYNC_SCAN_FIELDS,
                ))

            def rename_item(fid: str, new_name: str) -> dict:
                return svc.files().update(
                    fileId=fid,
                    body={'name': new_name},
                    supportsAllDrives=True,
                    fields='id,name,parents,md5Checksum,size',
                ).execute()

            if self.mode == 'scan':
                if not roots:
                    raise ValueError('Nenhuma pasta local configurada para o modo atual.')
                self.progress.emit(5, 'Baixando lista atual do Google Drive (somente leitura)...')
                drive_items = list(api.listing('trashed = false', drive=drive_id, fields=INSYNC_SCAN_FIELDS))
                results = scan_insync_suffix_candidates(
                    roots=roots,
                    drive_id=drive_id,
                    drive_items=drive_items,
                    cancel_check=self._is_cancelled,
                    progress_cb=lambda pct, msg: self.progress.emit(pct, msg),
                )
                self.progress.emit(100, 'Análise concluída.')
                self.finished.emit({'mode': 'scan', 'items': results})
                return

            if config.is_read_only():
                raise ValueError('Ative o Modo Edição para executar alterações no Google Drive.')
            assert_queue_empty(self.db_name)

            if self.mode == 'restore':
                self.progress.emit(10, 'Restaurando nomes temporários pendentes no Google Drive...')
                res = restore_pending_temp_names(
                    self.db_name,
                    get_item_fn=get_item,
                    rename_item_fn=rename_item,
                )
                self.progress.emit(100, 'Restauração concluída.')
                self.finished.emit({'mode': 'restore', **res})
                return

            stats = execute_insync_suffix_repair(
                self.items,
                db_name=self.db_name,
                drive_id=drive_id,
                get_item_fn=get_item,
                list_children_fn=list_children,
                rename_item_fn=rename_item,
                wait_timeout_sec=self.wait_timeout_sec,
                batch_size=self.batch_size,
                cancel_check=self._is_cancelled,
                progress_cb=lambda pct, msg: self.progress.emit(pct, msg),
                create_backup=True,
            )
            self.progress.emit(100, 'Execução concluída.')
            self.finished.emit({'mode': 'execute', 'items': self.items, **stats})
        except DedupCancelled as exc:
            self.finished.emit({'mode': self.mode, 'cancelled': True, 'message': str(exc), 'items': self.items})
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            lock.unlock()
