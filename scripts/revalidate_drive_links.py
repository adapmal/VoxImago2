"""Audita e revalida vinculos historicos entre arquivos locais e Google Drive.

O modo padrao e conservador: cria backup, baixa um inventario completo do Drive,
gera CSV/JSON (incluindo previa detalhada de reparos) e nao altera o banco.
``--apply`` e ``--apply-plan`` revalidam banco, disco e Drive antes de gravar
em transacao SQLite atomica, tratando trocas cruzadas em conjunto e mantendo
arquivos somente online pendentes por padrao.

Este script usa apenas a biblioteca padrao do Python (e modulos locais sem GUI)
para rodar mesmo sem PyQt6/google-api-python-client no ambiente de manutencao.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import ntpath
import os
import signal
import sqlite3
import stat
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.drive.link_validation import ensure_validation_table
from src.drive.match import (
    _common_suffix_score,
    _path_components,
    extract_drive_file_id,
)
from src.database.search import SearchEngine


FOLDER_MIME = 'application/vnd.google-apps.folder'
# Mesmos atributos Windows de src/utils/exif_utils.py: OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS
ONLINE_ONLY_ATTRIBUTES = 0x1000 | 0x40000 | 0x400000

REPORT_FIELDS = (
    'local_file_id', 'local_name', 'local_path', 'local_size', 'local_exists',
    'drive_id', 'remote_name', 'remote_size', 'remote_path', 'remote_md5',
    'local_md5', 'hierarchy_score', 'linked_local_count', 'status', 'action',
    'proposed_drive_id', 'proposed_remote_path', 'evidence', 'details',
)

PROPOSED_FIELDS = (
    'component_id', 'component_size', 'local_file_id', 'local_path',
    'old_drive_id', 'old_remote_path', 'proposed_drive_id', 'proposed_remote_path',
    'local_size', 'remote_size', 'local_md5', 'remote_md5', 'evidence', 'apply_status',
)


class RevalidationCancelled(RuntimeError):
    """Cancelamento explicito da revalidacao ou leitura de arquivos."""


class DriveApiError(RuntimeError):
    """Erro ao consultar a API do Google Drive com codigo HTTP opcional."""

    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class LocalLink:
    file_id: str
    name: str
    path: str
    size: int
    web_link: str


@dataclass(frozen=True)
class AuditRow:
    local_file_id: str
    local_name: str
    local_path: str
    local_size: int
    local_exists: bool
    drive_id: str
    remote_name: str = ''
    remote_size: int = 0
    remote_path: str = ''
    remote_md5: str = ''
    local_md5: str = ''
    hierarchy_score: int = 0
    linked_local_count: int = 0
    status: str = ''
    action: str = 'none'
    proposed_drive_id: str = ''
    proposed_remote_path: str = ''
    evidence: str = ''
    details: str = ''


@dataclass(frozen=True)
class RemoteNode:
    item_id: str
    verified: bool
    parts: tuple[str, ...]
    parts_fold: tuple[str, ...]
    tree_error: str = ''


@dataclass(frozen=True)
class RemoteTree:
    drive_id: str | None
    nodes: dict[str, RemoteNode]
    unique_by_path: dict[tuple[str, ...], dict]


@dataclass(frozen=True)
class RelinkProposal:
    local_file_id: str
    local_name: str
    local_path: str
    local_size: int
    old_drive_id: str
    old_remote_path: str
    old_web_link: str
    new_drive_id: str
    new_remote_path: str
    new_remote_name: str
    new_remote_size: int
    local_md5: str
    remote_md5: str
    hierarchy_score: int
    evidence: str
    new_parent_id: str = ''
    is_confirmation: bool = False
    disk_size: int = 0
    disk_mtime_ns: int = 0
    component_id: int = 0
    component_size: int = 1


def _path_fold(value: str) -> str:
    """Normaliza Unicode NFC e caixa sem remover espacos ou sufixos (2)."""
    return unicodedata.normalize('NFC', str(value or '')).casefold()


def positive_int(value):
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if parsed > 0 else 0


def normalized_name(value):
    return SearchEngine(None).normalize_text(value or '')


def extension(value):
    return os.path.splitext(value or '')[1].casefold()


def is_online_only(path):
    """Consulta atributos Windows via stat sem abrir o arquivo para hidratacao."""
    try:
        info = os.stat(path)
    except OSError:
        return False, None, 'local_missing'
    if not stat.S_ISREG(info.st_mode):
        return False, None, 'not_regular_file'
    attrs = getattr(info, 'st_file_attributes', 0)
    if attrs & ONLINE_ONLY_ATTRIBUTES:
        return True, info, 'online_only_pending'
    return False, info, ''


class HashBudget:
    def __init__(
        self,
        total_bytes,
        max_file_bytes,
        *,
        allow_online=False,
        cancel_check=None,
        progress_callback=None,
    ):
        self.total_bytes = total_bytes
        self.max_file_bytes = max_file_bytes
        self.allow_online = bool(allow_online)
        self.cancel_check = cancel_check or (lambda: False)
        self.progress_callback = progress_callback
        self.used_bytes = 0
        self.cache = {}
        self.file_stats = {}
        self.skip_reasons = Counter()

    def _check_cancelled(self):
        if self.cancel_check():
            raise RevalidationCancelled('Operacao cancelada pelo usuario.')

    def md5(self, path, expected_size):
        self._check_cancelled()
        key = os.path.normcase(os.path.normpath(path))
        if key in self.cache:
            return self.cache[key]

        online, info_before, stat_err = is_online_only(path)
        if stat_err and stat_err != 'online_only_pending':
            self.skip_reasons[stat_err] += 1
            self.cache[key] = ('', stat_err)
            return self.cache[key]
        if online and not self.allow_online:
            self.skip_reasons['online_only_pending'] += 1
            self.cache[key] = ('', 'online_only_pending')
            return self.cache[key]

        actual_size = info_before.st_size
        if expected_size >= 0 and actual_size != expected_size:
            self.skip_reasons['filesystem_size_changed'] += 1
            self.cache[key] = ('', 'filesystem_size_changed')
            return self.cache[key]
        if actual_size > self.max_file_bytes:
            self.skip_reasons['file_over_hash_limit'] += 1
            self.cache[key] = ('', 'file_over_hash_limit')
            return self.cache[key]
        if self.used_bytes + actual_size > self.total_bytes:
            self.skip_reasons['hash_budget_exceeded'] += 1
            self.cache[key] = ('', 'hash_budget_exceeded')
            return self.cache[key]

        digest = hashlib.md5()
        bytes_this_file = 0
        try:
            with open(path, 'rb') as stream:
                while True:
                    self._check_cancelled()
                    chunk = stream.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    bytes_this_file += len(chunk)
                    if self.progress_callback and online:
                        self.progress_callback(path, bytes_this_file, actual_size)
                info_fd = os.fstat(stream.fileno())
            info_after = os.stat(path)
        except RevalidationCancelled:
            raise
        except OSError as exc:
            self.skip_reasons['hash_read_error'] += 1
            self.cache[key] = ('', f'hash_read_error:{exc}')
            return self.cache[key]

        if (
            info_fd.st_size != info_before.st_size
            or info_fd.st_mtime_ns != info_before.st_mtime_ns
            or info_after.st_size != info_before.st_size
            or info_after.st_mtime_ns != info_before.st_mtime_ns
        ):
            self.skip_reasons['filesystem_changed_during_hash'] += 1
            return ('', 'filesystem_changed_during_hash')

        self.used_bytes += actual_size
        hexdigest = digest.hexdigest().lower()
        self.cache[key] = (hexdigest, '')
        self.file_stats[key] = (info_after.st_size, info_after.st_mtime_ns)
        if self.progress_callback and not online:
            self.progress_callback(path, actual_size, actual_size)
        return self.cache[key]


def _sleep_cancelable(seconds, cancel_check=None):
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if cancel_check and cancel_check():
            raise RevalidationCancelled('Operacao cancelada durante espera de nova tentativa na API.')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.2, remaining))


def api_json(url, *, data=None, headers=None, attempts=4, cancel_check=None, token_refresher=None):
    req_headers = dict(headers or {})
    last_error = None
    last_code = None
    refreshed_auth = False
    for attempt in range(attempts):
        if cancel_check and cancel_check():
            raise RevalidationCancelled('Operacao cancelada antes de consultar API do Drive.')
        request = urllib.request.Request(
            url,
            data=data,
            headers=req_headers,
            method='POST' if data is not None else 'GET',
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            last_error = exc
            last_code = exc.code
            if exc.code == 401 and token_refresher and not refreshed_auth:
                new_token = token_refresher()
                if new_token:
                    req_headers['Authorization'] = f'Bearer {new_token}'
                    refreshed_auth = True
                    continue
            if exc.code in (400, 401, 404):
                raise DriveApiError(f'Falha ao consultar API (HTTP {exc.code}): {exc}', status_code=exc.code) from exc
            if attempt + 1 < attempts:
                _sleep_cancelable(min(2 ** attempt, 10), cancel_check=cancel_check)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            last_code = None
            if attempt + 1 < attempts:
                _sleep_cancelable(min(2 ** attempt, 10), cancel_check=cancel_check)
    raise DriveApiError(f'Falha ao consultar API: {last_error}', status_code=last_code)


def refresh_access_token(token_path, *, cancel_check=None):
    with open(token_path, 'r', encoding='utf-8') as stream:
        token = json.load(stream)
    required = ('client_id', 'client_secret', 'refresh_token')
    if not all(token.get(key) for key in required):
        raise RuntimeError('Token OAuth nao contem dados suficientes para renovacao.')
    body = urllib.parse.urlencode({
        'client_id': token['client_id'],
        'client_secret': token['client_secret'],
        'refresh_token': token['refresh_token'],
        'grant_type': 'refresh_token',
    }).encode('ascii')
    response = api_json(
        'https://oauth2.googleapis.com/token',
        data=body,
        cancel_check=cancel_check,
    )
    access_token = response.get('access_token')
    if not access_token:
        raise RuntimeError('Google nao retornou um access_token.')
    return access_token


class TokenState:
    """Mantem o token OAuth atual e permite renovacao transparente em caso de HTTP 401."""

    def __init__(self, token_path, initial_token=None, *, cancel_check=None):
        self.token_path = token_path
        self.access_token = initial_token
        self.cancel_check = cancel_check

    def get(self):
        if not self.access_token and self.token_path and os.path.exists(self.token_path):
            self.access_token = refresh_access_token(self.token_path, cancel_check=self.cancel_check)
        return self.access_token

    def refresh(self):
        if not self.token_path or not os.path.exists(self.token_path):
            return None
        self.access_token = refresh_access_token(self.token_path, cancel_check=self.cancel_check)
        return self.access_token


def list_drive_inventory(access_token, drive_id, *, cancel_check=None, token_refresher=None):
    items = []
    page_token = None
    page_number = 0
    fields = (
        'nextPageToken,incompleteSearch,files(id,name,size,md5Checksum,mimeType,parents,'
        'driveId,modifiedTime,webViewLink,trashed)'
    )
    while True:
        if cancel_check and cancel_check():
            raise RevalidationCancelled('Operacao cancelada durante listagem do Drive.')
        params = {
            'corpora': 'drive',
            'driveId': drive_id,
            'includeItemsFromAllDrives': 'true',
            'supportsAllDrives': 'true',
            'q': 'trashed = false',
            'pageSize': '1000',
            'fields': fields,
        }
        if page_token:
            params['pageToken'] = page_token
        url = 'https://www.googleapis.com/drive/v3/files?' + urllib.parse.urlencode(params)
        current_token = access_token.get() if isinstance(access_token, TokenState) else access_token
        refresher = access_token.refresh if isinstance(access_token, TokenState) else token_refresher
        response = api_json(
            url,
            headers={'Authorization': f'Bearer {current_token}'},
            cancel_check=cancel_check,
            token_refresher=refresher,
        )
        if response.get('incompleteSearch'):
            raise RuntimeError('Inventario do Drive veio incompleto; abortado.')
        items.extend(response.get('files', ()))
        page_token = response.get('nextPageToken')
        page_number += 1
        if page_number == 1 or page_number % 10 == 0 or not page_token:
            print(
                f'[Drive] pagina={page_number} itens={len(items):,}',
                flush=True,
            )
        if not page_token:
            break
    return items


def fetch_remote_item(access_token, file_id, *, cancel_check=None, token_refresher=None):
    if cancel_check and cancel_check():
        raise RevalidationCancelled('Operacao cancelada durante consulta ao Drive.')
    fields = 'id,name,size,md5Checksum,mimeType,parents,driveId,modifiedTime,webViewLink,trashed'
    params = {'supportsAllDrives': 'true', 'fields': fields}
    url = (
        f'https://www.googleapis.com/drive/v3/files/'
        f'{urllib.parse.quote(file_id, safe="")}?' + urllib.parse.urlencode(params)
    )
    current_token = access_token.get() if isinstance(access_token, TokenState) else access_token
    refresher = access_token.refresh if isinstance(access_token, TokenState) else token_refresher
    return api_json(
        url,
        headers={'Authorization': f'Bearer {current_token}'},
        cancel_check=cancel_check,
        token_refresher=refresher,
    )


def list_folder_children(access_token, parent_id, drive_id, *, cancel_check=None, token_refresher=None):
    items = []
    page_token = None
    escaped = str(parent_id).replace('\\', '\\\\').replace("'", "\\'")
    fields = (
        'nextPageToken,incompleteSearch,files(id,name,size,md5Checksum,mimeType,parents,'
        'driveId,modifiedTime,webViewLink,trashed)'
    )
    while True:
        if cancel_check and cancel_check():
            raise RevalidationCancelled('Operacao cancelada durante conferencia de pasta no Drive.')
        params = {
            'corpora': 'drive',
            'driveId': drive_id,
            'includeItemsFromAllDrives': 'true',
            'supportsAllDrives': 'true',
            'q': f"'{escaped}' in parents and trashed = false",
            'pageSize': '1000',
            'fields': fields,
        }
        if page_token:
            params['pageToken'] = page_token
        url = 'https://www.googleapis.com/drive/v3/files?' + urllib.parse.urlencode(params)
        current_token = access_token.get() if isinstance(access_token, TokenState) else access_token
        refresher = access_token.refresh if isinstance(access_token, TokenState) else token_refresher
        response = api_json(
            url,
            headers={'Authorization': f'Bearer {current_token}'},
            cancel_check=cancel_check,
            token_refresher=refresher,
        )
        if response.get('incompleteSearch'):
            raise RuntimeError('Listagem de pasta incompleta no Drive.')
        items.extend(response.get('files', ()))
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    return items


def save_inventory(path, drive_id, items):
    payload = {
        'complete': True,
        'drive_id': drive_id,
        'created_at': int(time.time()),
        'items': items,
    }
    with gzip.open(path, 'wt', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, separators=(',', ':'))


def load_inventory(path, expected_drive_id):
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        payload = json.load(stream)
    if not payload.get('complete'):
        raise RuntimeError('Inventario informado nao foi concluido.')
    if payload.get('drive_id') != expected_drive_id:
        raise RuntimeError('Inventario pertence a outro Drive.')
    return payload.get('items', [])


def backup_database(db_path, backup_path, *, cancel_check=None):
    if cancel_check and cancel_check():
        raise RevalidationCancelled('Operacao cancelada antes de criar backup.')
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(db_path), timeout=30)
    target = sqlite3.connect(str(backup_path))
    aborted = False
    try:
        def _progress(_status, _remaining, _total):
            if cancel_check and cancel_check():
                raise RevalidationCancelled('Operacao cancelada durante criacao do backup.')
        source.backup(target, pages=256, progress=_progress if cancel_check else None)
    except RevalidationCancelled:
        aborted = True
        raise
    finally:
        target.close()
        source.close()
        if aborted and backup_path.exists():
            try:
                backup_path.unlink()
            except OSError:
                pass
    print(f'[Backup] {backup_path}', flush=True)


def resolve_local_roots(settings, cli_roots=None):
    """Retorna as raizes locais configuradas para o Banco de Imagens."""
    raw = []
    if cli_roots:
        raw.extend(cli_roots)
    elif isinstance(settings, dict):
        for key in ('scan_paths', 'scan_folders', 'local_folders'):
            val = settings.get(key) or []
            if isinstance(val, str):
                raw.append(val)
            else:
                raw.extend(val)
    roots = []
    seen = set()
    for path in raw:
        if not path:
            continue
        cleaned = ntpath.normpath(str(path)).rstrip('\\/')
        folded = _path_fold(cleaned)
        if folded and folded not in seen:
            seen.add(folded)
            roots.append(cleaned)
    return tuple(roots)


def extract_local_rel_parts(local_path, scan_roots):
    """Extrai o caminho relativo completo a partir da raiz local configurada."""
    if not local_path or not scan_roots:
        return None
    norm_local = ntpath.normpath(str(local_path))
    drive_letter = ntpath.splitdrive(norm_local)[0].upper()
    if drive_letter == 'O:':
        return None
    fold_local = _path_fold(norm_local)
    matched_roots = []
    for root in scan_roots:
        norm_root = ntpath.normpath(str(root)).rstrip('\\/')
        fold_root = _path_fold(norm_root)
        if fold_local.startswith(fold_root + '\\') or fold_local.startswith(fold_root + '/'):
            matched_roots.append((len(fold_root), fold_root))
    if not matched_roots:
        return None
    matched_roots.sort(key=lambda entry: entry[0], reverse=True)
    if len(matched_roots) > 1 and matched_roots[0][0] == matched_roots[1][0]:
        return None
    chosen_fold_root = matched_roots[0][1]
    rel = fold_local[len(chosen_fold_root) + 1:]
    parts = [p for p in rel.replace('/', '\\').split('\\') if p]
    if not parts or any(p in ('.', '..') for p in parts):
        return None
    return tuple(parts)


def build_remote_tree(items, drive_id=None):
    """Reconstroi caminhos completos no Drive exigindo raiz valida e unicidade."""
    by_id = {}
    folders = {}
    siblings = defaultdict(list)

    for item in items:
        fid = item.get('id')
        if not fid or item.get('trashed'):
            continue
        by_id[fid] = item
        if item.get('mimeType') == FOLDER_MIME:
            folders[fid] = item
        parents = item.get('parents') or []
        if len(parents) == 1:
            siblings[(parents[0], _path_fold(item.get('name', '')))].append(fid)

    nodes = {}
    path_buckets = defaultdict(list)

    for fid, item in by_id.items():
        if drive_id and item.get('driveId') not in (None, '', drive_id):
            nodes[fid] = RemoteNode(fid, False, (), (), 'wrong_drive')
            continue
        parents = item.get('parents') or []
        if len(parents) != 1:
            nodes[fid] = RemoteNode(fid, False, (), (), 'invalid_parents')
            continue
        parent_id = parents[0]
        if len(siblings[(parent_id, _path_fold(item.get('name', '')))]) > 1:
            nodes[fid] = RemoteNode(fid, False, (), (), 'homonymous_leaf')
            continue

        rev_names = [item.get('name', '')]
        visited = {fid}
        cur = parent_id
        error = ''
        verified = False

        while True:
            if drive_id is not None and cur == drive_id:
                verified = True
                break
            if cur in visited:
                error = 'cycle'
                break
            visited.add(cur)
            folder = folders.get(cur)
            if folder is None:
                if drive_id is None and cur not in by_id:
                    verified = True
                elif cur in by_id:
                    error = 'non_folder_ancestor'
                else:
                    error = 'missing_ancestor'
                break
            if folder.get('trashed'):
                error = 'trashed_ancestor'
                break
            if drive_id and folder.get('driveId') not in (None, '', drive_id):
                error = 'wrong_drive_ancestor'
                break
            f_parents = folder.get('parents') or []
            if len(f_parents) != 1:
                error = 'invalid_ancestor_parents'
                break
            f_parent = f_parents[0]
            if len(siblings[(f_parent, _path_fold(folder.get('name', '')))]) > 1:
                error = 'homonymous_ancestor'
                break
            rev_names.append(folder.get('name', ''))
            cur = f_parent

        parts = tuple(reversed(rev_names))
        parts_fold = tuple(_path_fold(p) for p in parts)
        if not verified:
            nodes[fid] = RemoteNode(fid, False, parts, parts_fold, error or 'unverified')
        else:
            nodes[fid] = RemoteNode(fid, True, parts, parts_fold, '')
            if item.get('mimeType') != FOLDER_MIME:
                path_buckets[parts_fold].append(fid)

    unique_by_path = {}
    for parts_fold, fids in path_buckets.items():
        if len(fids) == 1:
            unique_by_path[parts_fold] = by_id[fids[0]]
        else:
            for fid in fids:
                prev = nodes[fid]
                nodes[fid] = RemoteNode(
                    fid, False, prev.parts, prev.parts_fold, 'homonymous_path'
                )

    return RemoteTree(drive_id=drive_id, nodes=nodes, unique_by_path=unique_by_path)


def remote_parent_paths(items, drive_id=None):
    tree = build_remote_tree(items, drive_id=drive_id)
    cache = {}

    def resolve(item):
        fid = item.get('id')
        if fid in cache:
            return cache[fid]
        node = tree.nodes.get(fid)
        if node:
            res = node.parts[:-1]
            cache[fid] = res
            return res
        return ()

    resolve.tree = tree
    return resolve


def read_local_links(cursor):
    cursor.execute(
        """
        SELECT file_id, name, path, size, webContentLink
        FROM files
        WHERE source = 'local' AND COALESCE(webContentLink, '') <> ''
        """
    )
    links = []
    malformed = []
    for file_id, name, path, size, web_link in cursor.fetchall():
        row = LocalLink(
            file_id=file_id or '',
            name=name or '',
            path=path or '',
            size=positive_int(size),
            web_link=web_link or '',
        )
        drive_id = extract_drive_file_id(row.web_link)
        if drive_id:
            links.append((drive_id, row))
        else:
            malformed.append(row)
    return links, malformed


def read_all_local_links_map(cursor):
    cursor.execute(
        """
        SELECT file_id, COALESCE(webContentLink, '')
        FROM files WHERE source = 'local'
        """
    )
    mapping = {}
    for file_id, web_link in cursor.fetchall():
        mapping[file_id] = extract_drive_file_id(web_link) or ''
    return mapping


def read_unlinked_exact_candidates(
    cursor,
    remote_tree,
    scan_roots,
    contested_drive_ids,
    *,
    include_all_unlinked=False,
):
    """Carrega arquivos locais sem vinculo apenas quando disputam IDs ou explicitamente pedidos."""
    if not remote_tree or not scan_roots:
        return []
    cursor.execute(
        """
        SELECT file_id, name, path, size, COALESCE(webContentLink, '')
        FROM files
        WHERE source = 'local' AND COALESCE(webContentLink, '') = ''
        """
    )
    out = []
    for file_id, name, path, size, web_link in cursor.fetchall():
        rel = extract_local_rel_parts(path or '', scan_roots)
        if not rel:
            continue
        target = remote_tree.unique_by_path.get(rel)
        if not target:
            continue
        if include_all_unlinked or target['id'] in contested_drive_ids:
            out.append(LocalLink(
                file_id=file_id or '',
                name=name or '',
                path=path or '',
                size=positive_int(size),
                web_link=web_link or '',
            ))
    return out


def build_local_candidate_index(cursor):
    cursor.execute(
        """
        SELECT file_id, name, path, size, COALESCE(webContentLink, '')
        FROM files WHERE source = 'local' AND size > 0
        """
    )
    by_identity = defaultdict(list)
    for file_id, name, path, size, link in cursor.fetchall():
        local = LocalLink(
            file_id=file_id or '', name=name or '', path=path or '',
            size=positive_int(size), web_link=link or '',
        )
        key = (normalized_name(local.name), extension(local.name), local.size)
        by_identity[key].append(local)
    return by_identity


def base_audit_rows(
    local_links,
    malformed,
    remote_by_id,
    resolve_parent,
    *,
    remote_tree=None,
    scan_roots=(),
):
    if remote_tree is None:
        remote_tree = getattr(resolve_parent, 'tree', None)
    strict_tree = bool(remote_tree and remote_tree.drive_id is not None and scan_roots)

    grouped = defaultdict(list)
    for drive_id, local in local_links:
        grouped[drive_id].append(local)

    rows = []
    suspect_drive_ids = set()
    for local in malformed:
        rows.append(AuditRow(
            local.file_id, local.name, local.path, local.size,
            os.path.isfile(local.path), '', status='invalid_link',
            details='URL nao contem um ID Google Drive reconhecido.',
        ))

    for drive_id, group in grouped.items():
        remote = remote_by_id.get(drive_id)
        node = remote_tree.nodes.get(drive_id) if remote_tree else None
        if remote:
            parent_parts = node.parts[:-1] if node else resolve_parent(remote)
            remote_path = '/'.join((*parent_parts, remote.get('name', '')))
        else:
            parent_parts = ()
            remote_path = ''

        for local in group:
            exists = os.path.isfile(local.path)
            remote_size = positive_int(remote.get('size')) if remote else 0
            local_rel = extract_local_rel_parts(local.path, scan_roots) if scan_roots else None
            score = (
                len(node.parts)
                if (strict_tree and node and node.verified and local_rel and local_rel == node.parts_fold)
                else (_common_suffix_score(_path_components(local.path), parent_parts) if remote else 0)
            )
            common = dict(
                local_file_id=local.file_id,
                local_name=local.name,
                local_path=local.path,
                local_size=local.size,
                local_exists=exists,
                drive_id=drive_id,
                remote_name=remote.get('name', '') if remote else '',
                remote_size=remote_size,
                remote_path=remote_path,
                remote_md5=(remote.get('md5Checksum') or '').lower() if remote else '',
                hierarchy_score=score,
                linked_local_count=len(group),
            )
            if remote is None:
                status = 'remote_missing'
                details = 'ID nao apareceu no inventario completo do Drive selecionado.'
            elif remote.get('mimeType') == FOLDER_MIME:
                status = 'remote_folder'
                details = 'Vinculo local aponta para uma pasta Drive, nao para arquivo.'
            elif not exists:
                status = 'local_missing'
                details = 'Registro local nao existe mais no sistema de arquivos.'
            elif remote_size <= 0 or local.size <= 0:
                status = 'zero_size'
                details = 'Tamanho zero/ausente impede validar identidade pelo conteudo.'
            else:
                same_size = local.size == remote_size
                same_name = (
                    normalized_name(local.name) == normalized_name(remote.get('name', ''))
                    and extension(local.name) == extension(remote.get('name', ''))
                )
                if strict_tree:
                    hierarchy_ok = bool(node and node.verified)
                    full_path_ok = bool(hierarchy_ok and local_rel is not None and local_rel == node.parts_fold)
                else:
                    hierarchy_ok = True
                    full_path_ok = score > 0

                suspicious = (
                    len(group) != 1 or not same_size or not same_name or not full_path_ok
                )
                if suspicious:
                    status = 'needs_hash'
                    reasons = []
                    if len(group) != 1:
                        reasons.append('ID ligado a varios locais')
                    if not same_size:
                        reasons.append('tamanho divergente')
                    if not same_name:
                        reasons.append('nome divergente')
                    if not hierarchy_ok and node:
                        reasons.append(f'hierarquia Drive bloqueada ({node.tree_error})')
                    elif strict_tree and local_rel is None:
                        reasons.append('caminho fora da raiz configurada')
                    elif not full_path_ok:
                        reasons.append('caminho completo diverge do Drive' if strict_tree else 'hierarquia sem sufixo comum')
                    details = '; '.join(reasons)
                    suspect_drive_ids.add(drive_id)
                else:
                    status = 'trusted_metadata'
                    details = 'Caminho completo, raiz do Drive, ID, nome e tamanho coerentes.'
            rows.append(AuditRow(status=status, details=details, **common))
    return rows, suspect_drive_ids, grouped


def resolve_suspects(
    rows,
    suspect_drive_ids,
    grouped,
    remote_by_id,
    candidate_index,
    hash_budget,
    *,
    remote_tree=None,
    scan_roots=(),
):
    by_local = {row.local_file_id: row for row in rows}
    added_rows = []
    relinks = {}
    processed = 0
    total = len(suspect_drive_ids)
    strict_tree = bool(remote_tree and remote_tree.drive_id is not None and scan_roots)

    for drive_id in sorted(suspect_drive_ids):
        hash_budget._check_cancelled()
        processed += 1
        remote = remote_by_id[drive_id]
        node = remote_tree.nodes.get(drive_id) if remote_tree else None
        remote_size = positive_int(remote.get('size'))
        remote_md5 = (remote.get('md5Checksum') or '').lower()
        linked = grouped[drive_id]

        if strict_tree and (not node or not node.verified):
            reason = node.tree_error if node else 'unverified_tree'
            for local in linked:
                current = by_local[local.file_id]
                by_local[local.file_id] = replace(
                    current,
                    status='blocked_remote_hierarchy',
                    action='none',
                    details=f'Hierarquia no Drive bloqueada ({reason}); reparo impedido.',
                )
            continue

        identity_key = (
            normalized_name(remote.get('name', '')),
            extension(remote.get('name', '')),
            remote_size,
        )
        candidates = {candidate.file_id: candidate for candidate in linked}
        for candidate in candidate_index.get(identity_key, ()):
            candidate_drive_id = extract_drive_file_id(candidate.web_link)
            if candidate_drive_id and candidate_drive_id != drive_id:
                continue
            candidates.setdefault(candidate.file_id, candidate)

        if not remote_md5:
            for local in linked:
                current = by_local[local.file_id]
                by_local[local.file_id] = replace(
                    current,
                    status='unverified_no_remote_hash',
                    details=current.details + '; Drive nao forneceu MD5.',
                )
            continue

        hashes = {}
        errors = {}
        for candidate in candidates.values():
            if candidate.size != remote_size:
                continue
            digest, error = hash_budget.md5(candidate.path, candidate.size)
            if digest:
                hashes[candidate.file_id] = digest
            elif error:
                errors[candidate.file_id] = error

        winners = [
            candidate for candidate in candidates.values()
            if hashes.get(candidate.file_id) == remote_md5
        ]
        online_candidates = [
            candidate for candidate in candidates.values()
            if errors.get(candidate.file_id) == 'online_only_pending'
        ]

        if len(winners) == 1 and not online_candidates:
            winner = winners[0]
            winner_path_ok = True
            if strict_tree:
                winner_rel = extract_local_rel_parts(winner.path, scan_roots)
                winner_path_ok = bool(node and node.verified and winner_rel == node.parts_fold)

            if winner_path_ok:
                relinks[drive_id] = winner
                for local in linked:
                    current = by_local[local.file_id]
                    digest = hashes.get(local.file_id, '')
                    if local.file_id == winner.file_id:
                        status = (
                            'valid_hash'
                            if extract_drive_file_id(local.web_link) == drive_id
                            else 'valid_hash_relinked'
                        )
                        action = 'keep_link'
                        details = 'MD5 local coincide com o MD5 atual do Drive.'
                    else:
                        status = 'relinked_away'
                        action = 'clear_wrong_link'
                        details = 'Outro arquivo local foi o unico vencedor por MD5.'
                    by_local[local.file_id] = replace(
                        current, local_md5=digest, status=status,
                        action=action, details=details,
                        proposed_drive_id=drive_id if action == 'keep_link' else '',
                        proposed_remote_path=current.remote_path if action == 'keep_link' else '',
                        evidence='md5_unico_e_caminho_completo' if strict_tree else 'md5_unico',
                    )
                if winner.file_id not in by_local:
                    remote_path = next(
                        row.remote_path for row in rows if row.drive_id == drive_id
                    )
                    added = AuditRow(
                        local_file_id=winner.file_id,
                        local_name=winner.name,
                        local_path=winner.path,
                        local_size=winner.size,
                        local_exists=os.path.isfile(winner.path),
                        drive_id=drive_id,
                        remote_name=remote.get('name', ''),
                        remote_size=remote_size,
                        remote_path=remote_path,
                        remote_md5=remote_md5,
                        local_md5=hashes[winner.file_id],
                        hierarchy_score=(
                            len(node.parts)
                            if (strict_tree and node)
                            else _common_suffix_score(
                                _path_components(winner.path),
                                tuple(part for part in remote_path.split('/')[:-1] if part),
                            )
                        ),
                        linked_local_count=len(linked),
                        status='valid_hash_relinked',
                        action='set_correct_link',
                        proposed_drive_id=drive_id,
                        proposed_remote_path=remote_path,
                        evidence='md5_unico_e_caminho_completo' if strict_tree else 'md5_unico',
                        details='Candidato alternativo foi o unico vencedor por MD5.',
                    )
                    by_local[winner.file_id] = added
                    added_rows.append(added)
            else:
                # Strict mode: mesmo MD5 mas caminho completo diverge; deixa para fase de caminho exato.
                for local in linked:
                    current = by_local[local.file_id]
                    by_local[local.file_id] = replace(
                        current,
                        local_md5=hashes.get(local.file_id, ''),
                        status='path_mismatch_pending_exact',
                        action='none',
                        details='MD5 coincide, mas o caminho completo local diverge do Drive.',
                    )
        elif len(winners) > 1 or (len(winners) >= 1 and online_candidates):
            for local in linked:
                current = by_local[local.file_id]
                if errors.get(local.file_id) == 'online_only_pending':
                    status = 'pending_online_hash'
                    details = 'Arquivo somente online; deixado pendente sem download.'
                else:
                    status = 'ambiguous_duplicate_hash'
                    details = (
                        f'{len(winners)} arquivo(s) local(is) e {len(online_candidates)} '
                        f'online podem compartilhar o mesmo MD5.'
                        if online_candidates else
                        f'{len(winners)} arquivos locais possuem o mesmo MD5 remoto.'
                    )
                by_local[local.file_id] = replace(
                    current,
                    local_md5=hashes.get(local.file_id, ''),
                    status=status,
                    details=details,
                )
        else:
            for local in linked:
                current = by_local[local.file_id]
                error = errors.get(local.file_id, '')
                if error == 'online_only_pending':
                    by_local[local.file_id] = replace(
                        current,
                        status='pending_online_hash',
                        action='none',
                        details='Arquivo somente online; pendente sem abrir conteudo.',
                    )
                    continue
                if error:
                    status = error.split(':', 1)[0]
                    action = 'none' if status == 'filesystem_changed_during_hash' else 'quarantine_link'
                    details = f'Hash nao calculado: {error}.'
                elif local.size != remote_size:
                    status = 'size_mismatch'
                    action = 'quarantine_link'
                    details = 'Nenhum candidato do tamanho remoto venceu por MD5.'
                else:
                    status = 'hash_mismatch'
                    action = 'quarantine_link'
                    details = 'MD5 local diverge do MD5 atual do Drive.'
                by_local[local.file_id] = replace(
                    current,
                    local_md5=hashes.get(local.file_id, ''),
                    status=status,
                    action=action,
                    details=details,
                )

        if processed == 1 or processed % 100 == 0 or processed == total:
            print(
                f'[Hash] grupos={processed:,}/{total:,} '
                f'lidos={hash_budget.used_bytes / (1024 ** 3):.2f} GiB',
                flush=True,
            )

    ordered = [by_local[row.local_file_id] for row in rows]
    ordered.extend(
        row for row in added_rows
        if row.local_file_id not in {item.local_file_id for item in rows}
    )
    return ordered, relinks


def resolve_exact_paths(
    rows,
    remote_tree,
    scan_roots,
    hash_budget,
    all_local_links,
    *,
    extra_unlinked=(),
):
    """Propoe relinks seguros por caminho completo 1-para-1 + tamanho + MD5 real.

    Trata trocas cruzadas (A<->B), links obsoletos (remote_missing) e itens
    resolvidos ambiguamente por hash quando o caminho completo os diferencia.
    """
    if not remote_tree or not scan_roots:
        return rows, {}, {}

    by_local = {row.local_file_id: row for row in rows}
    candidates = []
    seen_lids = set()

    for row in rows:
        seen_lids.add(row.local_file_id)
        if row.status in ('trusted_metadata',):
            continue
        if row.action in ('keep_link', 'set_correct_link'):
            continue
        candidates.append(LocalLink(
            file_id=row.local_file_id,
            name=row.local_name,
            path=row.local_path,
            size=row.local_size,
            web_link='',
        ))

    for local in extra_unlinked:
        if local.file_id not in seen_lids:
            seen_lids.add(local.file_id)
            candidates.append(local)

    proposals = {}
    extra_rows = {}

    for local in candidates:
        hash_budget._check_cancelled()
        rel = extract_local_rel_parts(local.path, scan_roots)
        if not rel:
            continue
        target = remote_tree.unique_by_path.get(rel)
        if not target:
            continue
        t_id = target['id']
        old_row = by_local.get(local.file_id)
        old_drive_id = old_row.drive_id if old_row else (all_local_links.get(local.file_id) or '')
        is_confirm = (old_drive_id == t_id)

        node = remote_tree.nodes[t_id]
        t_path = '/'.join(node.parts)
        t_size = positive_int(target.get('size'))
        t_md5 = (target.get('md5Checksum') or '').lower()
        t_parent = (target.get('parents') or [''])[0]

        if not os.path.isfile(local.path):
            if old_row and old_row.status == 'remote_missing':
                by_local[local.file_id] = replace(old_row, status='local_missing', details='Arquivo local ausente no disco.')
            continue
        if t_size <= 0 or local.size <= 0:
            if old_row:
                by_local[local.file_id] = replace(old_row, status='zero_size', action='none', details='Tamanho zero/ausente no destino exato.')
            continue
        if local.size != t_size:
            if old_row:
                by_local[local.file_id] = replace(
                    old_row,
                    status='size_mismatch',
                    action='quarantine_link' if old_drive_id else 'none',
                    details=f'Tamanho local diverge do arquivo no Drive no mesmo caminho ({t_id}).',
                )
            continue
        if not t_md5:
            if old_row:
                by_local[local.file_id] = replace(
                    old_row,
                    status='unverified_no_remote_hash',
                    action='none',
                    details='Arquivo no caminho exato do Drive nao possui md5Checksum.',
                )
            continue

        digest, err = hash_budget.md5(local.path, local.size)
        if err == 'online_only_pending':
            upd = replace(
                old_row,
                status='pending_online_hash',
                action='none',
                proposed_drive_id=t_id,
                proposed_remote_path=t_path,
                details='Arquivo somente online; pendente sem download para validar caminho exato.',
            ) if old_row else AuditRow(
                local.file_id, local.name, local.path, local.size, True, old_drive_id,
                remote_name=target.get('name', ''), remote_size=t_size, remote_path=t_path,
                remote_md5=t_md5, hierarchy_score=len(node.parts),
                status='pending_online_hash', action='none',
                proposed_drive_id=t_id, proposed_remote_path=t_path,
                details='Arquivo somente online; pendente sem download para validar caminho exato.',
            )
            if old_row:
                by_local[local.file_id] = upd
            else:
                extra_rows[local.file_id] = upd
            continue
        if err:
            if old_row:
                by_local[local.file_id] = replace(
                    old_row,
                    status=err.split(':', 1)[0],
                    action='none',
                    details=f'Nao foi possivel validar hash no caminho exato: {err}.',
                )
            continue

        if digest != t_md5:
            if old_row:
                by_local[local.file_id] = replace(
                    old_row,
                    local_md5=digest,
                    status='hash_mismatch',
                    action='quarantine_link' if old_drive_id else 'none',
                    details=f'MD5 local diverge do arquivo no Drive no mesmo caminho ({t_id}).',
                )
            continue

        key = os.path.normcase(os.path.normpath(local.path))
        disk_size, disk_mtime_ns = hash_budget.file_stats.get(key, (local.size, 0))
        if is_confirm:
            evidence = 'confirmado_caminho_completo_1para1+tamanho+md5'
        elif old_row and old_row.status == 'remote_missing':
            evidence = 'remote_missing_recuperado+caminho_completo_1para1+tamanho+md5'
        elif old_drive_id:
            evidence = 'troca_ou_revinculo+caminho_completo_1para1+tamanho+md5'
        else:
            evidence = 'caminho_completo_1para1+tamanho+md5'

        proposals[local.file_id] = RelinkProposal(
            local_file_id=local.file_id,
            local_name=local.name,
            local_path=local.path,
            local_size=local.size,
            old_drive_id=old_drive_id,
            old_remote_path=old_row.remote_path if old_row else '',
            old_web_link='',
            new_drive_id=t_id,
            new_remote_path=t_path,
            new_remote_name=target.get('name', ''),
            new_remote_size=t_size,
            local_md5=digest,
            remote_md5=t_md5,
            hierarchy_score=len(node.parts),
            evidence=evidence,
            new_parent_id=t_parent,
            is_confirmation=is_confirm,
            disk_size=disk_size,
            disk_mtime_ns=disk_mtime_ns,
        )

    out_rows = [by_local[row.local_file_id] for row in rows]
    out_rows.extend(extra_rows.values())
    return out_rows, proposals, by_local


def build_atomic_relink_plan(
    rows,
    phase1_relinks,
    exact_proposals,
    remote_by_id,
    remote_tree,
    all_local_links,
    *,
    scan_roots=(),
    hash_budget=None,
):
    """Resolve conflitos e agrupa trocas cruzadas em componentes conexos atomicos."""
    by_local = {row.local_file_id: row for row in rows}
    combined_proposals = dict(exact_proposals)

    for drive_id, winner in phase1_relinks.items():
        if winner.file_id in combined_proposals:
            continue
        cur_did = all_local_links.get(winner.file_id, '')
        if cur_did == drive_id:
            continue
        remote = remote_by_id[drive_id]
        node = remote_tree.nodes.get(drive_id) if remote_tree else None
        r_path = '/'.join(node.parts) if node else ''
        row = by_local.get(winner.file_id)
        disk_size, disk_mtime_ns = winner.size, 0
        key = os.path.normcase(os.path.normpath(winner.path))
        if hash_budget is not None and key in hash_budget.file_stats:
            disk_size, disk_mtime_ns = hash_budget.file_stats[key]
        else:
            try:
                st_info = os.stat(winner.path)
                disk_size, disk_mtime_ns = st_info.st_size, st_info.st_mtime_ns
            except OSError:
                pass
        combined_proposals[winner.file_id] = RelinkProposal(
            local_file_id=winner.file_id,
            local_name=winner.name,
            local_path=winner.path,
            local_size=winner.size,
            old_drive_id=cur_did,
            old_remote_path='',
            old_web_link=winner.web_link,
            new_drive_id=drive_id,
            new_remote_path=r_path or (row.remote_path if row else ''),
            new_remote_name=remote.get('name', ''),
            new_remote_size=positive_int(remote.get('size')),
            local_md5=row.local_md5 if row else (remote.get('md5Checksum') or '').lower(),
            remote_md5=(remote.get('md5Checksum') or '').lower(),
            hierarchy_score=len(node.parts) if node else (row.hierarchy_score if row else 0),
            evidence='md5_unico_suspeito',
            new_parent_id=(remote.get('parents') or [''])[0],
            is_confirmation=False,
            disk_size=disk_size,
            disk_mtime_ns=disk_mtime_ns,
        )

    current_holders_by_drive = defaultdict(set)
    for lid, did in all_local_links.items():
        if did:
            current_holders_by_drive[did].add(lid)

    uncertain_statuses = {
        'pending_online_hash',
        'filesystem_changed_during_hash',
        'hash_budget_exceeded',
        'file_over_hash_limit',
        'hash_read_error',
    }
    auto_clears_by_target = defaultdict(set)
    for lid, prop in combined_proposals.items():
        for other_lid in current_holders_by_drive.get(prop.new_drive_id, ()):
            if other_lid == lid or other_lid in combined_proposals:
                continue
            other_row = by_local.get(other_lid)
            if other_row is None:
                continue
            if other_row.status in uncertain_statuses:
                continue
            if scan_roots and extract_local_rel_parts(other_row.local_path, scan_roots) is None:
                continue
            auto_clears_by_target[prop.new_drive_id].add(other_lid)

    phase1_clears = {
        row.local_file_id for row in rows
        if row.action == 'clear_wrong_link' and row.local_file_id not in combined_proposals
    }

    active = dict(combined_proposals)
    blocked_conflicts = {}

    while True:
        claimed_targets = {prop.new_drive_id for prop in active.values()}
        clear_ids = {
            cid for cid in phase1_clears
            if all_local_links.get(cid, '') in claimed_targets
        }
        for target_did in claimed_targets:
            clear_ids.update(auto_clears_by_target.get(target_did, ()))

        projected = dict(all_local_links)
        for lid in clear_ids:
            projected[lid] = ''
        for lid, prop in active.items():
            projected[lid] = prop.new_drive_id

        holders = defaultdict(list)
        for lid, did in projected.items():
            if did:
                holders[did].append(lid)

        rejected = []
        for lid, prop in active.items():
            h = holders.get(prop.new_drive_id, [])
            if len(h) != 1 or h[0] != lid:
                others = [o for o in h if o != lid]
                rejected.append((
                    lid,
                    f'Conflito: ID {prop.new_drive_id} permaneceria associado a {len(others)} outro(s) registro(s) local(is).',
                ))
        if not rejected:
            break
        for lid, reason in rejected:
            p = active.pop(lid)
            if not p.is_confirmation:
                blocked_conflicts[lid] = (p, reason)

    # Remove confirmacoes que nao precisaram de desocupacao nem mudanca de status.
    for lid in list(active.keys()):
        prop = active[lid]
        if not prop.is_confirmation:
            continue
        prev_holders = current_holders_by_drive.get(prop.new_drive_id, set())
        had_sibling_change = any(
            other != lid and (other in active or other in clear_ids)
            for other in prev_holders
        )
        old_row = by_local.get(lid)
        needs_status_upgrade = bool(old_row and old_row.status not in ('trusted_metadata', 'valid_hash'))
        if not had_sibling_change and not needs_status_upgrade:
            del active[lid]

    # Constroi componentes conexos para nunca aplicar metade de uma troca.
    ops_nodes = set(active.keys()) | clear_ids
    adj = defaultdict(set)
    for lid, prop in active.items():
        for prev_holder in current_holders_by_drive.get(prop.new_drive_id, ()):
            if prev_holder != lid and prev_holder in ops_nodes:
                adj[lid].add(prev_holder)
                adj[prev_holder].add(lid)

    visited = set()
    components = []
    final_proposals = {}
    final_clears = {}
    for start in sorted(ops_nodes):
        if start in visited:
            continue
        stack = [start]
        comp_nodes = []
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            comp_nodes.append(cur)
            stack.extend(adj[cur])
        comp_nodes.sort()
        comp_id = len(components) + 1
        comp_props = []
        comp_clears = []
        comp_clear_details = []
        for lid in comp_nodes:
            if lid in active:
                p = replace(active[lid], component_id=comp_id, component_size=len(comp_nodes))
                final_proposals[lid] = p
                comp_props.append(p)
            else:
                comp_clears.append(lid)
                c_row = by_local.get(lid)
                c_info = {
                    'local_file_id': lid,
                    'local_path': c_row.local_path if c_row else lid,
                    'local_size': c_row.local_size if c_row else 0,
                    'old_drive_id': all_local_links.get(lid, ''),
                    'old_remote_path': c_row.remote_path if c_row else '',
                    'component_id': comp_id,
                    'component_size': len(comp_nodes),
                }
                final_clears[lid] = c_info
                comp_clear_details.append(c_info)
        components.append({
            'component_id': comp_id,
            'size': len(comp_nodes),
            'relinks': comp_props,
            'clears': comp_clears,
            'clear_details': comp_clear_details,
        })

    updated_rows = []
    seen_row_ids = set()
    for row in rows:
        seen_row_ids.add(row.local_file_id)
        if row.local_file_id in final_proposals:
            p = final_proposals[row.local_file_id]
            updated_rows.append(replace(
                row,
                local_md5=p.local_md5,
                hierarchy_score=p.hierarchy_score,
                status='valid_hash' if p.is_confirmation else 'valid_hash_relinked',
                action='keep_link' if p.is_confirmation else 'set_correct_link',
                proposed_drive_id=p.new_drive_id,
                proposed_remote_path=p.new_remote_path,
                evidence=p.evidence,
                details=f'Vinculo {"confirmado" if p.is_confirmation else "proposto"} validado ({p.evidence}; componente #{p.component_id}, {p.component_size} item(ns)).',
            ))
        elif row.local_file_id in final_clears:
            c_info = final_clears[row.local_file_id]
            updated_rows.append(replace(
                row,
                status='relinked_away',
                action='clear_wrong_link',
                proposed_drive_id='',
                proposed_remote_path='',
                evidence='clear_wrong_link',
                details=f'Vinculo incorreto para {c_info["old_drive_id"]} sera removido (componente #{c_info["component_id"]}).',
            ))
        elif row.local_file_id in blocked_conflicts:
            p, reason = blocked_conflicts[row.local_file_id]
            updated_rows.append(replace(
                row,
                status='relink_conflict',
                action='none',
                proposed_drive_id=p.new_drive_id,
                proposed_remote_path=p.new_remote_path,
                evidence=p.evidence,
                details=reason,
            ))
        else:
            updated_rows.append(row)

    for lid, p in sorted(final_proposals.items()):
        if lid not in seen_row_ids:
            updated_rows.append(AuditRow(
                local_file_id=p.local_file_id,
                local_name=p.local_name,
                local_path=p.local_path,
                local_size=p.local_size,
                local_exists=True,
                drive_id=p.old_drive_id,
                remote_name=p.new_remote_name,
                remote_size=p.new_remote_size,
                remote_path=p.new_remote_path,
                remote_md5=p.remote_md5,
                local_md5=p.local_md5,
                hierarchy_score=p.hierarchy_score,
                linked_local_count=0,
                status='valid_hash' if p.is_confirmation else 'valid_hash_relinked',
                action='keep_link' if p.is_confirmation else 'set_correct_link',
                proposed_drive_id=p.new_drive_id,
                proposed_remote_path=p.new_remote_path,
                evidence=p.evidence,
                details=f'Vinculo {"confirmado" if p.is_confirmation else "proposto"} validado ({p.evidence}; componente #{p.component_id}, {p.component_size} item(ns)).',
            ))

    plan = {
        'schema': 2,
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'drive_id': remote_tree.drive_id if remote_tree else '',
        'scan_roots': list(scan_roots),
        'components': [
            {
                'component_id': comp['component_id'],
                'size': comp['size'],
                'relinks': [asdict(p) for p in comp['relinks']],
                'clears': list(comp['clears']),
                'clear_details': list(comp['clear_details']),
            }
            for comp in components
        ],
        'blocked_conflicts': [
            {'local_file_id': lid, 'proposed_drive_id': p.new_drive_id, 'reason': reason}
            for lid, (p, reason) in sorted(blocked_conflicts.items())
        ],
    }
    return updated_rows, plan


def select_components_within_limit(components, limit):
    """Seleciona componentes inteiros ate o limite sem jamais quebrar uma troca."""
    if not limit or limit <= 0:
        return list(components), []
    selected, deferred = [], []
    used = 0
    for comp in components:
        sz = int(comp.get('size', len(comp.get('relinks', [])) + len(comp.get('clears', []))))
        if used + sz <= limit:
            selected.append(comp)
            used += sz
        else:
            deferred.append(comp)
    return selected, deferred


def _verify_live_drive_proposal(
    prop,
    drive_id,
    scan_roots,
    *,
    access_token=None,
    remote_tree=None,
    item_fetcher=None,
    children_fetcher=None,
    cancel_check=None,
    _cache=None,
):
    """Revalida candidatos no Drive antes de gravar (prioriza consulta ao vivo sobre inventario inicial)."""
    if cancel_check and cancel_check():
        raise RevalidationCancelled('Operacao cancelada durante revalidacao no Drive.')

    has_live_drive = bool(access_token or item_fetcher or children_fetcher)
    if not has_live_drive and remote_tree is not None:
        node = remote_tree.nodes.get(prop['new_drive_id'])
        target = remote_tree.unique_by_path.get(node.parts_fold) if (node and node.verified) else None
        if not node or not node.verified or not target or target['id'] != prop['new_drive_id']:
            return False, 'destino_invalido_ou_ambiguo_no_drive'
        if positive_int(target.get('size')) != int(prop['local_size']):
            return False, 'tamanho_drive_divergente'
        if (target.get('md5Checksum') or '').lower() != prop['remote_md5'].lower():
            return False, 'md5_drive_divergente'
        rel = extract_local_rel_parts(prop['local_path'], scan_roots)
        if not rel or rel != node.parts_fold:
            return False, 'caminho_local_divergente'
        return True, ''

    if not has_live_drive:
        return False, 'sem_credencial_drive_para_revalidacao'

    raw_get = item_fetcher or (lambda fid: fetch_remote_item(access_token, fid, cancel_check=cancel_check))
    raw_list = children_fetcher or (lambda pid: list_folder_children(access_token, pid, drive_id, cancel_check=cancel_check))
    cache = _cache if _cache is not None else {'items': {}, 'children': {}}

    def get_fn(fid):
        if cancel_check and cancel_check():
            raise RevalidationCancelled('Operacao cancelada durante leitura de item no Drive.')
        if fid not in cache['items']:
            cache['items'][fid] = raw_get(fid)
        return cache['items'][fid]

    def list_fn(pid):
        if cancel_check and cancel_check():
            raise RevalidationCancelled('Operacao cancelada durante listagem de pasta no Drive.')
        if pid not in cache['children']:
            children = raw_list(pid)
            cache['children'][pid] = children
            for child in children:
                cid = child.get('id')
                if cid and 'size' in child and 'md5Checksum' in child and 'parents' in child:
                    cache['items'].setdefault(cid, child)
        return cache['children'][pid]

    hint_parent = prop.get('new_parent_id') or ''
    if hint_parent and prop['new_drive_id'] not in cache['items']:
        list_fn(hint_parent)

    fresh = get_fn(prop['new_drive_id'])
    if not fresh or fresh.get('trashed') or fresh.get('mimeType') == FOLDER_MIME:
        return False, 'destino_ausente_ou_na_lixeira'
    if fresh.get('driveId') not in (None, '', drive_id):
        return False, 'destino_em_outro_drive'
    if positive_int(fresh.get('size')) != int(prop['local_size']):
        return False, 'tamanho_drive_mudou'
    if (fresh.get('md5Checksum') or '').lower() != prop['remote_md5'].lower():
        return False, 'md5_drive_mudou'

    parents = fresh.get('parents') or []
    if len(parents) != 1:
        return False, 'pais_invalidos_no_drive'
    sibs = [c for c in list_fn(parents[0]) if _path_fold(c.get('name', '')) == _path_fold(fresh.get('name', ''))]
    if len(sibs) != 1 or sibs[0]['id'] != fresh['id']:
        return False, 'homonimo_no_drive'

    names = [fresh.get('name', '')]
    cur = parents[0]
    seen = {fresh['id']}
    while cur != drive_id:
        if cur in seen:
            return False, 'ciclo_no_drive'
        seen.add(cur)
        folder = get_fn(cur)
        if not folder or folder.get('trashed') or folder.get('mimeType') != FOLDER_MIME:
            return False, 'ancestral_ausente_no_drive'
        if folder.get('driveId') not in (None, '', drive_id):
            return False, 'ancestral_em_outro_drive'
        fp = folder.get('parents') or []
        if len(fp) != 1:
            return False, 'ancestral_com_multiplos_pais'
        fsibs = [c for c in list_fn(fp[0]) if _path_fold(c.get('name', '')) == _path_fold(folder.get('name', ''))]
        if len(fsibs) != 1 or fsibs[0]['id'] != folder['id']:
            return False, 'pasta_homonima_no_drive'
        names.append(folder.get('name', ''))
        cur = fp[0]

    remote_fold = tuple(_path_fold(n) for n in reversed(names))
    rel = extract_local_rel_parts(prop['local_path'], scan_roots)
    if not rel or rel != remote_fold:
        return False, 'caminho_drive_mudou_desde_previa'
    return True, ''


def _verify_live_disk_proposal(prop, *, allow_online=False, rehash=False, hash_budget=None):
    online, info, stat_err = is_online_only(prop['local_path'])
    if stat_err and stat_err != 'online_only_pending':
        return False, stat_err
    if online and not allow_online:
        return False, 'online_only_pending'
    if info.st_size != int(prop['local_size']):
        return False, 'filesystem_size_changed'
    recorded_mtime = int(prop.get('disk_mtime_ns') or 0)
    if recorded_mtime == 0:
        rehash = True
    elif info.st_mtime_ns != recorded_mtime:
        if not rehash:
            return False, 'filesystem_mtime_changed'
    if rehash:
        budget = hash_budget or HashBudget(100 * 1024 ** 3, 10 * 1024 ** 3, allow_online=allow_online)
        digest, err = budget.md5(prop['local_path'], int(prop['local_size']))
        if err:
            return False, err
        if digest != prop['remote_md5'].lower():
            return False, 'hash_divergente_na_rechecagem'
    return True, ''


def write_csv(path, rows):
    with open(path, 'w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_proposed_csv(path, plan, apply_statuses=None):
    apply_statuses = apply_statuses or {}
    with open(path, 'w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=PROPOSED_FIELDS)
        writer.writeheader()
        for comp in plan.get('components', []):
            for prop in comp.get('relinks', []):
                lid = prop['local_file_id']
                writer.writerow({
                    'component_id': comp['component_id'],
                    'component_size': comp['size'],
                    'local_file_id': lid,
                    'local_path': prop['local_path'],
                    'old_drive_id': prop.get('old_drive_id', ''),
                    'old_remote_path': prop.get('old_remote_path', ''),
                    'proposed_drive_id': prop['new_drive_id'],
                    'proposed_remote_path': prop['new_remote_path'],
                    'local_size': prop['local_size'],
                    'remote_size': prop['new_remote_size'],
                    'local_md5': prop['local_md5'],
                    'remote_md5': prop['remote_md5'],
                    'evidence': prop['evidence'],
                    'apply_status': apply_statuses.get(lid, 'preview_only'),
                })
            for c_info in comp.get('clear_details', []):
                lid = c_info['local_file_id']
                writer.writerow({
                    'component_id': comp['component_id'],
                    'component_size': comp['size'],
                    'local_file_id': lid,
                    'local_path': c_info.get('local_path', lid),
                    'old_drive_id': c_info.get('old_drive_id', ''),
                    'old_remote_path': c_info.get('old_remote_path', ''),
                    'proposed_drive_id': '',
                    'proposed_remote_path': '',
                    'local_size': c_info.get('local_size', 0),
                    'remote_size': 0,
                    'local_md5': '',
                    'remote_md5': '',
                    'evidence': 'clear_wrong_link',
                    'apply_status': apply_statuses.get(lid, 'preview_only'),
                })


def stale_drive_cache_ids(cursor, live_ids):
    cursor.execute("SELECT file_id FROM files WHERE source = 'drive'")
    return [row[0] for row in cursor.fetchall() if row[0] not in live_ids]


def write_summary(path, rows, *, inventory_count, stale_count, hash_budget, applied, plan=None, apply_stats=None):
    statuses = Counter(row.status for row in rows)
    actions = Counter(row.action for row in rows)
    payload = {
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'applied': bool(applied),
        'inventory_count': inventory_count,
        'audited_rows': len(rows),
        'stale_drive_cache_rows': stale_count,
        'status_counts': dict(statuses),
        'action_counts': dict(actions),
        'proposed_components_count': len(plan.get('components', [])) if plan else 0,
        'proposed_relinks_count': sum(len(c.get('relinks', [])) for c in plan.get('components', [])) if plan else 0,
        'blocked_conflicts_count': len(plan.get('blocked_conflicts', [])) if plan else 0,
        'online_only_pending_count': statuses.get('pending_online_hash', 0),
        'hash_bytes_read': hash_budget.used_bytes,
        'hash_gib_read': round(hash_budget.used_bytes / (1024 ** 3), 3),
        'hash_skip_reasons': dict(hash_budget.skip_reasons),
    }
    if apply_stats:
        payload.update(apply_stats)
    with open(path, 'w', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    return payload


def apply_plan_components(
    connection,
    plan,
    *,
    drive_id,
    scan_roots,
    remote_by_id=None,
    remote_tree=None,
    access_token=None,
    item_fetcher=None,
    children_fetcher=None,
    cancel_check=None,
    limit=0,
    allow_online=False,
    rehash_disk=False,
    stale_ids=(),
    prune_stale=False,
    audit_rows=(),
):
    """Aplica componentes atomicos apos revalidar Drive, disco e SQLite."""
    components = plan.get('components', [])
    selected_comps, deferred_comps = select_components_within_limit(components, limit)
    apply_statuses = {}
    for comp in deferred_comps:
        for prop in comp.get('relinks', []):
            apply_statuses[prop['local_file_id']] = 'deferred_by_limit'
        for cid in comp.get('clears', []):
            apply_statuses[cid] = 'deferred_by_limit'

    verified_comps = []
    skipped_reasons = {}
    hash_budget = HashBudget(
        100 * 1024 ** 3, 10 * 1024 ** 3,
        allow_online=allow_online,
        cancel_check=cancel_check,
    )
    drive_cache = {'items': {}, 'children': {}}

    total_selected = len(selected_comps)
    for comp_idx, comp in enumerate(selected_comps, 1):
        if cancel_check and cancel_check():
            raise RevalidationCancelled('Operacao cancelada durante revalidacao pre-aplicacao.')
        comp_ok = True
        fail_reason = ''
        for prop in comp.get('relinks', []):
            try:
                ok_drive, r_drive = _verify_live_drive_proposal(
                    prop, drive_id, scan_roots,
                    access_token=access_token, remote_tree=remote_tree,
                    item_fetcher=item_fetcher, children_fetcher=children_fetcher,
                    cancel_check=cancel_check,
                    _cache=drive_cache,
                )
            except RevalidationCancelled:
                raise
            except DriveApiError as exc:
                if exc.status_code == 404:
                    ok_drive, r_drive = False, 'destino_nao_encontrado_no_drive'
                else:
                    ok_drive, r_drive = False, f'erro_api_drive:{exc.status_code or "rede"}'
            except RuntimeError:
                ok_drive, r_drive = False, 'erro_api_drive'
            if not ok_drive:
                comp_ok, fail_reason = False, r_drive
                break
            ok_disk, r_disk = _verify_live_disk_proposal(
                prop, allow_online=allow_online, rehash=rehash_disk, hash_budget=hash_budget,
            )
            if not ok_disk:
                comp_ok, fail_reason = False, r_disk
                break
        if comp_ok:
            verified_comps.append(comp)
        else:
            skipped_reasons[comp['component_id']] = fail_reason
            for prop in comp.get('relinks', []):
                apply_statuses[prop['local_file_id']] = f'skipped_revalidation:{fail_reason}'
            for cid in comp.get('clears', []):
                apply_statuses[cid] = f'skipped_revalidation:{fail_reason}'
        if total_selected >= 50 and (comp_idx == 1 or comp_idx % 100 == 0 or comp_idx == total_selected):
            print(
                f'[Revalidacao] componentes={comp_idx:,}/{total_selected:,} '
                f'validados={len(verified_comps):,} pulados={len(skipped_reasons):,}',
                flush=True,
            )

    if cancel_check and cancel_check():
        raise RevalidationCancelled('Operacao cancelada antes de abrir transacao SQLite.')

    cursor = connection.cursor()
    ensure_validation_table(cursor)
    connection.commit()
    now = int(time.time())

    applied_components = 0
    applied_relinks = 0
    applied_confirmations = 0
    applied_clears = 0
    skipped_db_components = 0

    cursor.execute('BEGIN IMMEDIATE')
    try:
        # Grava linhas de auditoria nao-propostas apenas em execucao completa (sem limit parcial).
        if audit_rows and not limit:
            pending_lids = {
                prop['local_file_id']
                for comp in components
                for prop in comp.get('relinks', [])
            } | {
                cid
                for comp in components
                for cid in comp.get('clears', [])
            }
            cursor.executemany(
                """
                INSERT INTO drive_link_validation (
                    local_file_id, drive_id, status, remote_name, remote_size,
                    remote_md5, local_md5, hierarchy_score, details, validated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(local_file_id) DO UPDATE SET
                    drive_id=excluded.drive_id,
                    status=excluded.status,
                    remote_name=excluded.remote_name,
                    remote_size=excluded.remote_size,
                    remote_md5=excluded.remote_md5,
                    local_md5=excluded.local_md5,
                    hierarchy_score=excluded.hierarchy_score,
                    details=excluded.details,
                    validated_at=excluded.validated_at
                """,
                [
                    (
                        row.local_file_id, row.drive_id, row.status,
                        row.remote_name, row.remote_size, row.remote_md5,
                        row.local_md5, row.hierarchy_score, row.details, now,
                    )
                    for row in audit_rows
                    if row.drive_id and row.local_file_id not in pending_lids
                ],
            )

        for comp in verified_comps:
            if cancel_check and cancel_check():
                raise RevalidationCancelled('Operacao cancelada durante gravacao; transacao revertida.')
            comp_lids = {prop['local_file_id'] for prop in comp.get('relinks', [])} | set(comp.get('clears', []))
            expected_clear_did = {
                c['local_file_id']: (c.get('old_drive_id') or '')
                for c in comp.get('clear_details', [])
            }
            # 1) Conferencia do estado atual no SQLite antes de tocar neste componente.
            db_ok = True
            db_fail = ''
            for prop in comp.get('relinks', []):
                lid = prop['local_file_id']
                row_db = cursor.execute(
                    "SELECT size, COALESCE(webContentLink, ''), path FROM files WHERE file_id = ? AND source = 'local'",
                    (lid,),
                ).fetchone()
                if not row_db:
                    db_ok, db_fail = False, 'local_row_missing_in_db'
                    break
                cur_did = extract_drive_file_id(row_db[1]) or ''
                if positive_int(row_db[0]) != int(prop['local_size']) or cur_did != (prop.get('old_drive_id') or ''):
                    db_ok, db_fail = False, 'local_db_changed_since_plan'
                    break
                # Impede gravar destino ja ocupado por outro registro local fora deste componente.
                same_target = cursor.execute(
                    "SELECT file_id, COALESCE(webContentLink, '') FROM files WHERE source = 'local' AND webContentLink LIKE ?",
                    (f"%{prop['new_drive_id']}%",),
                ).fetchall()
                for other_lid, other_link in same_target:
                    if other_lid not in comp_lids and extract_drive_file_id(other_link) == prop['new_drive_id']:
                        db_ok, db_fail = False, 'target_id_occupied_in_db'
                        break
                if not db_ok:
                    break

            for clear_lid in comp.get('clears', []):
                row_db = cursor.execute(
                    "SELECT size, COALESCE(webContentLink, '') FROM files WHERE file_id = ? AND source = 'local'",
                    (clear_lid,),
                ).fetchone()
                if not row_db:
                    db_ok, db_fail = False, 'clear_row_missing_in_db'
                    break
                cur_clear_did = extract_drive_file_id(row_db[1]) or ''
                if clear_lid in expected_clear_did and cur_clear_did != expected_clear_did[clear_lid]:
                    db_ok, db_fail = False, 'clear_row_changed_in_db'
                    break

            if not db_ok:
                skipped_db_components += 1
                skipped_reasons[comp['component_id']] = db_fail
                for prop in comp.get('relinks', []):
                    apply_statuses[prop['local_file_id']] = f'skipped_db:{db_fail}'
                for cid in comp.get('clears', []):
                    apply_statuses[cid] = f'skipped_db:{db_fail}'
                continue

            for clear_lid in comp.get('clears', []):
                row_db = cursor.execute(
                    "SELECT size, COALESCE(webContentLink, '') FROM files WHERE file_id = ? AND source = 'local'",
                    (clear_lid,),
                ).fetchone()
                cur_clear_did = extract_drive_file_id(row_db[1]) or ''
                cursor.execute(
                    "UPDATE files SET webContentLink = NULL WHERE file_id = ? AND source = 'local'",
                    (clear_lid,),
                )
                if cur_clear_did:
                    cursor.execute(
                        """
                        INSERT INTO drive_link_validation (
                            local_file_id, drive_id, status, remote_name, remote_size,
                            remote_md5, local_md5, hierarchy_score, details, validated_at
                        ) VALUES (?, ?, 'relinked_away', '', 0, '', '', 0, 'Outro arquivo local venceu a correspondencia.', ?)
                        ON CONFLICT(local_file_id) DO UPDATE SET
                            drive_id=excluded.drive_id,
                            status=excluded.status,
                            details=excluded.details,
                            validated_at=excluded.validated_at
                        """,
                        (clear_lid, cur_clear_did, now),
                    )
                apply_statuses[clear_lid] = 'applied'
                applied_clears += 1

            for prop in comp.get('relinks', []):
                lid = prop['local_file_id']
                did = prop['new_drive_id']
                is_conf = bool(prop.get('is_confirmation'))
                live_item = drive_cache['items'].get(did) or {}
                remote = (remote_by_id or {}).get(did, {})
                link = (
                    live_item.get('webViewLink')
                    or remote.get('webViewLink')
                    or f'https://drive.google.com/file/d/{did}/view?usp=drivesdk'
                )
                if not is_conf:
                    cursor.execute(
                        "UPDATE files SET webContentLink = ? WHERE file_id = ? AND source = 'local'",
                        (link, lid),
                    )
                val_status = 'valid_hash' if is_conf else 'valid_hash_relinked'
                cursor.execute(
                    """
                    INSERT INTO drive_link_validation (
                        local_file_id, drive_id, status, remote_name, remote_size,
                        remote_md5, local_md5, hierarchy_score, details, validated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(local_file_id) DO UPDATE SET
                        drive_id=excluded.drive_id,
                        status=excluded.status,
                        remote_name=excluded.remote_name,
                        remote_size=excluded.remote_size,
                        remote_md5=excluded.remote_md5,
                        local_md5=excluded.local_md5,
                        hierarchy_score=excluded.hierarchy_score,
                        details=excluded.details,
                        validated_at=excluded.validated_at
                    """,
                    (
                        lid, did, val_status,
                        prop['new_remote_name'], int(prop['new_remote_size']),
                        prop['remote_md5'], prop['local_md5'], int(prop['hierarchy_score']),
                        prop['evidence'], now,
                    ),
                )
                apply_statuses[lid] = 'applied'
                if is_conf:
                    applied_confirmations += 1
                else:
                    applied_relinks += 1
            applied_components += 1

        if prune_stale and stale_ids and not limit:
            for start in range(0, len(stale_ids), 500):
                batch = stale_ids[start:start + 500]
                placeholders = ','.join('?' for _ in batch)
                cursor.execute(
                    f"DELETE FROM files WHERE source='drive' AND file_id IN ({placeholders})",
                    batch,
                )
                cursor.execute(
                    f"DELETE FROM search_index WHERE source='drive' AND file_id IN ({placeholders})",
                    batch,
                )
        if cancel_check and cancel_check():
            raise RevalidationCancelled('Operacao cancelada antes do commit; transacao revertida.')
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return apply_statuses, {
        'applied_components': applied_components,
        'applied_relinks': applied_relinks,
        'applied_confirmations': applied_confirmations,
        'applied_clears': applied_clears,
        'deferred_components': len(deferred_comps),
        'skipped_stale_components': len(skipped_reasons),
        'skipped_reasons': skipped_reasons,
        'revalidation_hash_gib': round(hash_budget.used_bytes / (1024 ** 3), 4),
    }


def apply_results(connection, rows, relinks, remote_by_id, stale_ids, prune_stale):
    """Compatibilidade com chamadas diretas e testes unitarios existentes."""
    cursor = connection.cursor()
    ensure_validation_table(cursor)
    connection.commit()
    now = int(time.time())
    cursor.execute('BEGIN IMMEDIATE')
    try:
        cursor.executemany(
            """
            INSERT INTO drive_link_validation (
                local_file_id, drive_id, status, remote_name, remote_size,
                remote_md5, local_md5, hierarchy_score, details, validated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(local_file_id) DO UPDATE SET
                drive_id=excluded.drive_id,
                status=excluded.status,
                remote_name=excluded.remote_name,
                remote_size=excluded.remote_size,
                remote_md5=excluded.remote_md5,
                local_md5=excluded.local_md5,
                hierarchy_score=excluded.hierarchy_score,
                details=excluded.details,
                validated_at=excluded.validated_at
            """,
            [
                (
                    row.local_file_id, row.drive_id, row.status,
                    row.remote_name, row.remote_size, row.remote_md5,
                    row.local_md5, row.hierarchy_score, row.details, now,
                )
                for row in rows if row.drive_id
            ],
        )

        linked_rows = defaultdict(list)
        for row in rows:
            if row.drive_id and row.action == 'clear_wrong_link':
                linked_rows[row.drive_id].append(row.local_file_id)
        for drive_id, winner in relinks.items():
            for local_id in linked_rows.get(drive_id, ()):
                if local_id != winner.file_id:
                    cursor.execute(
                        "UPDATE files SET webContentLink = NULL "
                        "WHERE file_id = ? AND source = 'local'",
                        (local_id,),
                    )
            remote = remote_by_id[drive_id]
            link = remote.get('webViewLink') or (
                f'https://drive.google.com/file/d/{drive_id}/view?usp=drivesdk'
            )
            cursor.execute(
                "UPDATE files SET webContentLink = ? "
                "WHERE file_id = ? AND source = 'local'",
                (link, winner.file_id),
            )

        if prune_stale and stale_ids:
            for start in range(0, len(stale_ids), 500):
                batch = stale_ids[start:start + 500]
                placeholders = ','.join('?' for _ in batch)
                cursor.execute(
                    f"DELETE FROM files WHERE source='drive' AND file_id IN ({placeholders})",
                    batch,
                )
                cursor.execute(
                    f"DELETE FROM search_index WHERE source='drive' AND file_id IN ({placeholders})",
                    batch,
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def run_audit_pipeline(
    connection,
    items,
    drive_id,
    scan_roots,
    *,
    hash_mode='none',
    hash_budget=None,
    include_unlinked=False,
):
    """Executa auditoria completa + resolucao por MD5/caminho exato + plano atomico."""
    cursor = connection.cursor()
    remote_by_id = {item['id']: item for item in items if not item.get('trashed')}
    resolve_parent = remote_parent_paths(items, drive_id=drive_id)
    remote_tree = resolve_parent.tree
    local_links, malformed = read_local_links(cursor)
    all_local_links = read_all_local_links_map(cursor)

    rows, suspect_ids, grouped = base_audit_rows(
        local_links, malformed, remote_by_id, resolve_parent,
        remote_tree=remote_tree, scan_roots=scan_roots,
    )
    suspect_bytes = sum(
        row.local_size for row in rows
        if row.status == 'needs_hash' and row.local_exists
    )
    print(
        f'[Local] vinculos={len(local_links):,} invalidos={len(malformed):,}',
        flush=True,
    )
    print(
        f'[Auditoria] grupos suspeitos={len(suspect_ids):,} '
        f'volume vinculado={suspect_bytes / (1024 ** 3):.2f} GiB',
        flush=True,
    )

    relinks = {}
    exact_proposals = {}
    if hash_budget is None:
        hash_budget = HashBudget(25 * 1024 ** 3, 2 * 1024 ** 3)

    if hash_mode == 'suspects':
        candidate_index = build_local_candidate_index(cursor)
        rows, relinks = resolve_suspects(
            rows, suspect_ids, grouped, remote_by_id,
            candidate_index, hash_budget,
            remote_tree=remote_tree, scan_roots=scan_roots,
        )
        contested_ids = set(suspect_ids) | {r.drive_id for r in rows if r.action == 'clear_wrong_link'}
        extra_unlinked = read_unlinked_exact_candidates(
            cursor, remote_tree, scan_roots, contested_ids,
            include_all_unlinked=include_unlinked,
        )
        rows, exact_proposals, _ = resolve_exact_paths(
            rows, remote_tree, scan_roots, hash_budget, all_local_links,
            extra_unlinked=extra_unlinked,
        )

    rows, plan = build_atomic_relink_plan(
        rows, relinks, exact_proposals, remote_by_id, remote_tree, all_local_links,
        scan_roots=scan_roots,
        hash_budget=hash_budget,
    )
    stale_ids = stale_drive_cache_ids(cursor, set(remote_by_id))
    return rows, plan, stale_ids, remote_by_id, remote_tree


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='data/file_index.db')
    parser.add_argument('--settings', default='config/settings.json')
    parser.add_argument('--token', default='config/token.json')
    parser.add_argument('--output-dir')
    parser.add_argument('--inventory', help='Reutiliza drive_inventory.json.gz existente.')
    parser.add_argument(
        '--hash-mode', choices=('none', 'suspects'), default='none',
        help='Calcula MD5 apenas dos vinculos suspeitos e divergencias de caminho.',
    )
    parser.add_argument('--hash-budget-gib', type=float, default=25.0)
    parser.add_argument('--max-file-gib', type=float, default=2.0)
    parser.add_argument('--allow-online-hashes', action='store_true',
                        help='Permite ler/baixar arquivos marcados como somente online para validar MD5.')
    parser.add_argument('--include-unlinked', action='store_true',
                        help='Tambem propoe vinculos para arquivos locais ainda sem webContentLink.')
    parser.add_argument('--scan-root', action='append', default=[],
                        help='Raiz local configurada (opcional; sobrescreve scan_paths do settings).')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--apply-plan', help='Aplica um relink_plan.json salvo apos revalidacao ao vivo.')
    parser.add_argument('--limit', type=int, default=0,
                        help='Maximo de registros/operacoes por execucao (respeita componentes inteiros).')
    parser.add_argument('--cancel-file', help='Caminho de arquivo-sentinela para cancelamento cooperativo.')
    parser.add_argument('--prune-stale-drive-cache', action='store_true')
    return parser.parse_args()


def main():
    args = parse_args()
    os.chdir(PROJECT_ROOT)
    with open(args.settings, 'r', encoding='utf-8') as stream:
        settings = json.load(stream)
    drive_id = (
        settings.get('sandbox_drive_id')
        if settings.get('sandbox_mode')
        else settings.get('production_drive_id')
    )
    if not drive_id:
        raise RuntimeError('Drive ID nao configurado.')
    scan_roots = resolve_local_roots(settings, args.scan_root or None)

    cancelled = {'flag': False}
    prev_sigint = signal.getsignal(signal.SIGINT)
    def _handle_sigint(signum, frame):
        cancelled['flag'] = True
    signal.signal(signal.SIGINT, _handle_sigint)

    def is_cancelled():
        return bool(cancelled['flag'] or (args.cancel_file and os.path.exists(args.cancel_file)))

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    output_dir = Path(args.output_dir or f'config/backups/link_revalidation/{stamp}')
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = Path(args.db)
    backup_path = output_dir / f'{db_path.stem}-before-revalidation-{stamp}.db'

    try:
        if args.apply_plan:
            plan = json.loads(Path(args.apply_plan).read_text(encoding='utf-8'))
            if plan.get('drive_id') and plan.get('drive_id') != drive_id:
                raise RuntimeError('Plano pertence a outro Drive ID.')
            plan_roots = tuple(plan.get('scan_roots') or ()) or scan_roots
            backup_database(db_path, backup_path, cancel_check=is_cancelled)
            remote_by_id = None
            remote_tree = None
            access_token = None
            if args.inventory:
                items = load_inventory(Path(args.inventory), drive_id)
                remote_by_id = {i['id']: i for i in items if not i.get('trashed')}
                remote_tree = build_remote_tree(items, drive_id=drive_id)
            if os.path.exists(args.token):
                access_token = TokenState(
                    args.token,
                    refresh_access_token(args.token, cancel_check=is_cancelled),
                    cancel_check=is_cancelled,
                )

            connection = sqlite3.connect(str(db_path), timeout=30)
            connection.execute('PRAGMA busy_timeout=30000')
            try:
                stale_ids = []
                if args.prune_stale_drive_cache and remote_by_id is not None:
                    stale_ids = stale_drive_cache_ids(connection.cursor(), set(remote_by_id))
                apply_statuses, apply_stats = apply_plan_components(
                    connection,
                    plan,
                    drive_id=drive_id,
                    scan_roots=plan_roots,
                    remote_by_id=remote_by_id,
                    remote_tree=None if access_token else remote_tree,
                    access_token=access_token,
                    cancel_check=is_cancelled,
                    limit=args.limit,
                    allow_online=args.allow_online_hashes,
                    rehash_disk=True,
                    stale_ids=stale_ids,
                    prune_stale=args.prune_stale_drive_cache,
                )
            finally:
                connection.close()
            proposed_path = output_dir / 'proposed_relinks.csv'
            write_proposed_csv(proposed_path, plan, apply_statuses)
            summary_path = output_dir / 'summary.json'
            with open(summary_path, 'w', encoding='utf-8') as s:
                json.dump(
                    dict(created_at=datetime.now().isoformat(timespec='seconds'),
                         applied=True, mode='apply_plan', source_plan=str(args.apply_plan),
                         **apply_stats),
                    s, ensure_ascii=False, indent=2,
                )
            print('[Resultado] ' + json.dumps(apply_stats, ensure_ascii=False), flush=True)
            print(f'[Propostos] {proposed_path}', flush=True)
            print(f'[Resumo] {summary_path}', flush=True)
            return 0

        backup_database(db_path, backup_path, cancel_check=is_cancelled)

        access_token = None
        if args.inventory:
            inventory_path = Path(args.inventory)
            items = load_inventory(inventory_path, drive_id)
            print(f'[Drive] inventario reutilizado: {len(items):,}', flush=True)
            if args.apply and os.path.exists(args.token):
                access_token = TokenState(
                    args.token,
                    refresh_access_token(args.token, cancel_check=is_cancelled),
                    cancel_check=is_cancelled,
                )
        else:
            access_token = TokenState(
                args.token,
                refresh_access_token(args.token, cancel_check=is_cancelled),
                cancel_check=is_cancelled,
            )
            items = list_drive_inventory(access_token, drive_id, cancel_check=is_cancelled)
            inventory_path = output_dir / 'drive_inventory.json.gz'
            save_inventory(inventory_path, drive_id, items)
            print(f'[Drive] inventario salvo: {inventory_path}', flush=True)

        connection = sqlite3.connect(str(db_path), timeout=30)
        connection.execute('PRAGMA busy_timeout=30000')
        try:
            hash_budget = HashBudget(
                int(args.hash_budget_gib * 1024 ** 3),
                int(args.max_file_gib * 1024 ** 3),
                allow_online=args.allow_online_hashes,
                cancel_check=is_cancelled,
                progress_callback=lambda p, done, total: (
                    print(f'[Online] {p}: {done}/{total} bytes', flush=True) if done < total else None
                ),
            )
            rows, plan, stale_ids, remote_by_id, remote_tree = run_audit_pipeline(
                connection, items, drive_id, scan_roots,
                hash_mode=args.hash_mode,
                hash_budget=hash_budget,
                include_unlinked=args.include_unlinked,
            )

            report_path = output_dir / 'link_revalidation.csv'
            proposed_path = output_dir / 'proposed_relinks.csv'
            plan_path = output_dir / 'relink_plan.json'
            summary_path = output_dir / 'summary.json'

            apply_statuses = {}
            apply_stats = None
            if args.apply:
                if args.hash_mode != 'suspects':
                    raise RuntimeError('--apply exige --hash-mode suspects.')
                print('[Revalidacao] Conferindo candidatos no Drive ao vivo antes de gravar...', flush=True)
                apply_statuses, apply_stats = apply_plan_components(
                    connection,
                    plan,
                    drive_id=drive_id,
                    scan_roots=scan_roots,
                    remote_by_id=remote_by_id,
                    remote_tree=None if access_token else remote_tree,
                    access_token=access_token,
                    cancel_check=is_cancelled,
                    limit=args.limit,
                    allow_online=args.allow_online_hashes,
                    rehash_disk=False,
                    stale_ids=stale_ids,
                    prune_stale=args.prune_stale_drive_cache,
                    audit_rows=rows,
                )

            write_csv(report_path, rows)
            write_proposed_csv(proposed_path, plan, apply_statuses)
            with open(plan_path, 'w', encoding='utf-8') as stream:
                json.dump(plan, stream, ensure_ascii=False, indent=2)
            summary = write_summary(
                summary_path, rows,
                inventory_count=len(items),
                stale_count=len(stale_ids),
                hash_budget=hash_budget,
                applied=args.apply,
                plan=plan,
                apply_stats=apply_stats,
            )
            print('[Resultado] ' + json.dumps(summary, ensure_ascii=False), flush=True)
            print(f'[CSV] {report_path}', flush=True)
            print(f'[Propostos] {proposed_path}', flush=True)
            print(f'[Plano] {plan_path}', flush=True)
            print(f'[Resumo] {summary_path}', flush=True)
            if not args.apply:
                print('[Modo] SOMENTE LEITURA; nenhuma alteracao aplicada.', flush=True)
        finally:
            connection.close()
        return 0
    except RevalidationCancelled as exc:
        print(f'[Cancelado] {exc}', flush=True)
        return 130
    finally:
        signal.signal(signal.SIGINT, prev_sigint)


if __name__ == '__main__':
    raise SystemExit(main())
