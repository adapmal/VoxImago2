"""Audita e revalida vinculos historicos entre arquivos locais e Google Drive.

O modo padrao e conservador: cria backup, baixa um inventario completo do Drive,
gera CSV/JSON e nao altera o banco. ``--apply`` persiste a quarentena e somente
faz relinks quando um unico hash local coincide com o MD5 remoto.

Este script usa apenas a biblioteca padrao do Python para poder rodar mesmo sem
PyQt6/google-api-python-client instalados no interpretador de manutencao.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
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
REPORT_FIELDS = (
    'local_file_id', 'local_name', 'local_path', 'local_size', 'local_exists',
    'drive_id', 'remote_name', 'remote_size', 'remote_path', 'remote_md5',
    'local_md5', 'hierarchy_score', 'linked_local_count', 'status', 'action',
    'details',
)


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
    details: str = ''


class HashBudget:
    def __init__(self, total_bytes, max_file_bytes):
        self.total_bytes = total_bytes
        self.max_file_bytes = max_file_bytes
        self.used_bytes = 0
        self.cache = {}
        self.skip_reasons = Counter()

    def md5(self, path, expected_size):
        key = os.path.normcase(os.path.normpath(path))
        if key in self.cache:
            return self.cache[key]
        try:
            actual_size = os.path.getsize(path)
        except OSError:
            self.skip_reasons['local_missing'] += 1
            self.cache[key] = ('', 'local_missing')
            return self.cache[key]
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
        try:
            with open(path, 'rb') as stream:
                while True:
                    chunk = stream.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError as exc:
            self.skip_reasons['hash_read_error'] += 1
            self.cache[key] = ('', f'hash_read_error:{exc}')
            return self.cache[key]
        self.used_bytes += actual_size
        self.cache[key] = (digest.hexdigest().lower(), '')
        return self.cache[key]


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


def api_json(url, *, data=None, headers=None, attempts=4):
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers or {},
        method='POST' if data is not None else 'GET',
    )
    last_error = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 5))
    raise RuntimeError(f'Falha ao consultar API: {last_error}')


def refresh_access_token(token_path):
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
    response = api_json('https://oauth2.googleapis.com/token', data=body)
    access_token = response.get('access_token')
    if not access_token:
        raise RuntimeError('Google nao retornou um access_token.')
    return access_token


def list_drive_inventory(access_token, drive_id):
    items = []
    page_token = None
    page_number = 0
    fields = (
        'nextPageToken,files(id,name,size,md5Checksum,mimeType,parents,'
        'modifiedTime,webViewLink,trashed)'
    )
    while True:
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
        response = api_json(
            url,
            headers={'Authorization': f'Bearer {access_token}'},
        )
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


def backup_database(db_path, backup_path):
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(db_path), timeout=30)
    target = sqlite3.connect(str(backup_path))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    print(f'[Backup] {backup_path}', flush=True)


def remote_parent_paths(items):
    folders = {
        item['id']: (
            item.get('name', ''),
            (item.get('parents') or [''])[0],
        )
        for item in items
        if item.get('mimeType') == FOLDER_MIME
    }
    cache = {}

    def resolve(item):
        parents = item.get('parents') or []
        parent_id = parents[0] if parents else ''
        if parent_id in cache:
            return cache[parent_id]
        names = []
        visited = set()
        current = parent_id
        while current and current not in visited:
            visited.add(current)
            node = folders.get(current)
            if not node:
                break
            name, current = node
            if name:
                names.append(name)
        result = tuple(reversed(names))
        cache[parent_id] = result
        return result

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


def base_audit_rows(local_links, malformed, remote_by_id, resolve_parent):
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
        if remote:
            parent_parts = resolve_parent(remote)
            remote_path = '/'.join((*parent_parts, remote.get('name', '')))
        else:
            parent_parts = ()
            remote_path = ''
        for local in group:
            exists = os.path.isfile(local.path)
            remote_size = positive_int(remote.get('size')) if remote else 0
            score = (
                _common_suffix_score(_path_components(local.path), parent_parts)
                if remote else 0
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
                suspicious = (
                    len(group) != 1 or not same_size or not same_name or score <= 0
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
                    if score <= 0:
                        reasons.append('hierarquia sem sufixo comum')
                    details = '; '.join(reasons)
                    suspect_drive_ids.add(drive_id)
                else:
                    status = 'trusted_metadata'
                    details = 'ID unico, nome/extensao/tamanho e hierarquia coerentes.'
            rows.append(AuditRow(status=status, details=details, **common))
    return rows, suspect_drive_ids, grouped


def resolve_suspects(
    rows,
    suspect_drive_ids,
    grouped,
    remote_by_id,
    candidate_index,
    hash_budget,
):
    by_local = {row.local_file_id: row for row in rows}
    added_rows = []
    relinks = {}
    processed = 0
    total = len(suspect_drive_ids)

    for drive_id in sorted(suspect_drive_ids):
        processed += 1
        remote = remote_by_id[drive_id]
        remote_size = positive_int(remote.get('size'))
        remote_md5 = (remote.get('md5Checksum') or '').lower()
        linked = grouped[drive_id]
        identity_key = (
            normalized_name(remote.get('name', '')),
            extension(remote.get('name', '')),
            remote_size,
        )
        candidates = {candidate.file_id: candidate for candidate in linked}
        for candidate in candidate_index.get(identity_key, ()):
            candidate_drive_id = extract_drive_file_id(candidate.web_link)
            if candidate_drive_id and candidate_drive_id != drive_id:
                # Nao roubar automaticamente um arquivo que ainda pertence a
                # outro vinculo. Trocas cruzadas ficam em quarentena/revisao.
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
        if len(winners) == 1:
            winner = winners[0]
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
                    hierarchy_score=_common_suffix_score(
                        _path_components(winner.path),
                        tuple(part for part in remote_path.split('/')[:-1] if part),
                    ),
                    linked_local_count=len(linked),
                    status='valid_hash_relinked',
                    action='set_correct_link',
                    details='Candidato alternativo foi o unico vencedor por MD5.',
                )
                by_local[winner.file_id] = added
                added_rows.append(added)
        elif len(winners) > 1:
            for local in linked:
                current = by_local[local.file_id]
                by_local[local.file_id] = replace(
                    current,
                    local_md5=hashes.get(local.file_id, ''),
                    status='ambiguous_duplicate_hash',
                    details=f'{len(winners)} arquivos locais possuem o mesmo MD5 remoto.',
                )
        else:
            for local in linked:
                current = by_local[local.file_id]
                error = errors.get(local.file_id, '')
                if error:
                    status = error.split(':', 1)[0]
                    details = f'Hash nao calculado: {error}.'
                elif local.size != remote_size:
                    status = 'size_mismatch'
                    details = 'Nenhum candidato do tamanho remoto venceu por MD5.'
                else:
                    status = 'hash_mismatch'
                    details = 'MD5 local diverge do MD5 atual do Drive.'
                by_local[local.file_id] = replace(
                    current,
                    local_md5=hashes.get(local.file_id, ''),
                    status=status,
                    action='quarantine_link',
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


def write_csv(path, rows):
    with open(path, 'w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def stale_drive_cache_ids(cursor, live_ids):
    cursor.execute("SELECT file_id FROM files WHERE source = 'drive'")
    return [row[0] for row in cursor.fetchall() if row[0] not in live_ids]


def write_summary(path, rows, *, inventory_count, stale_count, hash_budget, applied):
    statuses = Counter(row.status for row in rows)
    actions = Counter(row.action for row in rows)
    payload = {
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'applied': applied,
        'inventory_count': inventory_count,
        'audited_rows': len(rows),
        'stale_drive_cache_rows': stale_count,
        'status_counts': dict(statuses),
        'action_counts': dict(actions),
        'hash_bytes_read': hash_budget.used_bytes,
        'hash_gib_read': round(hash_budget.used_bytes / (1024 ** 3), 3),
        'hash_skip_reasons': dict(hash_budget.skip_reasons),
    }
    with open(path, 'w', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    return payload


def apply_results(connection, rows, relinks, remote_by_id, stale_ids, prune_stale):
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
            for start in multiline_range(0, len(stale_ids), 500):
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


def multiline_range(start, stop, step):
    """Nome explicito para facilitar testes e evitar ranges gigantes acidentais."""
    return range(start, stop, step)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='data/file_index.db')
    parser.add_argument('--settings', default='config/settings.json')
    parser.add_argument('--token', default='config/token.json')
    parser.add_argument('--output-dir')
    parser.add_argument('--inventory', help='Reutiliza drive_inventory.json.gz existente.')
    parser.add_argument(
        '--hash-mode', choices=('none', 'suspects'), default='none',
        help='Calcula MD5 apenas dos vinculos suspeitos e candidatos equivalentes.',
    )
    parser.add_argument('--hash-budget-gib', type=float, default=25.0)
    parser.add_argument('--max-file-gib', type=float, default=2.0)
    parser.add_argument('--apply', action='store_true')
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

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    output_dir = Path(args.output_dir or f'config/backups/link_revalidation/{stamp}')
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = Path(args.db)
    backup_path = output_dir / f'{db_path.stem}-before-revalidation-{stamp}.db'
    backup_database(db_path, backup_path)

    if args.inventory:
        inventory_path = Path(args.inventory)
        items = load_inventory(inventory_path, drive_id)
        print(f'[Drive] inventario reutilizado: {len(items):,}', flush=True)
    else:
        access_token = refresh_access_token(args.token)
        items = list_drive_inventory(access_token, drive_id)
        inventory_path = output_dir / 'drive_inventory.json.gz'
        save_inventory(inventory_path, drive_id, items)
        print(f'[Drive] inventario salvo: {inventory_path}', flush=True)

    remote_by_id = {item['id']: item for item in items}
    resolve_parent = remote_parent_paths(items)
    connection = sqlite3.connect(str(db_path), timeout=30)
    connection.execute('PRAGMA busy_timeout=30000')
    try:
        cursor = connection.cursor()
        local_links, malformed = read_local_links(cursor)
        print(
            f'[Local] vinculos={len(local_links):,} invalidos={len(malformed):,}',
            flush=True,
        )
        rows, suspect_ids, grouped = base_audit_rows(
            local_links, malformed, remote_by_id, resolve_parent
        )
        suspect_bytes = sum(
            row.local_size for row in rows
            if row.status == 'needs_hash' and row.local_exists
        )
        print(
            f'[Auditoria] grupos suspeitos={len(suspect_ids):,} '
            f'volume vinculado={suspect_bytes / (1024 ** 3):.2f} GiB',
            flush=True,
        )

        hash_budget = HashBudget(
            int(args.hash_budget_gib * 1024 ** 3),
            int(args.max_file_gib * 1024 ** 3),
        )
        relinks = {}
        if args.hash_mode == 'suspects':
            candidate_index = build_local_candidate_index(cursor)
            rows, relinks = resolve_suspects(
                rows, suspect_ids, grouped, remote_by_id,
                candidate_index, hash_budget,
            )

        live_ids = set(remote_by_id)
        stale_ids = stale_drive_cache_ids(cursor, live_ids)
        report_path = output_dir / 'link_revalidation.csv'
        write_csv(report_path, rows)
        summary_path = output_dir / 'summary.json'

        if args.apply:
            if args.hash_mode != 'suspects':
                raise RuntimeError('--apply exige --hash-mode suspects.')
            apply_results(
                connection, rows, relinks, remote_by_id, stale_ids,
                args.prune_stale_drive_cache,
            )

        summary = write_summary(
            summary_path, rows,
            inventory_count=len(items),
            stale_count=len(stale_ids),
            hash_budget=hash_budget,
            applied=args.apply,
        )
        print('[Resultado] ' + json.dumps(summary, ensure_ascii=False), flush=True)
        print(f'[CSV] {report_path}', flush=True)
        print(f'[Resumo] {summary_path}', flush=True)
        if not args.apply:
            print('[Modo] SOMENTE LEITURA; nenhuma alteracao aplicada.', flush=True)
    finally:
        connection.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
