"""Diagnostico offline e reparos deterministas do banco VoxImago."""

from __future__ import annotations

import argparse
import json
import ntpath
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database.search import SearchEngine
from src.database.snapshot import (
    SCHEMA_VERSION,
    select_snapshot_database,
)
from src.utils.config_manager import ConfigManager
from src.utils.staging_schema import StagingItem


REQUIRED_FILE_COLUMNS = {
    'file_id', 'name', 'path', 'mimeType', 'source', 'description',
    'size', 'modifiedTime', 'parentId', 'webContentLink',
    'thumbnailRotation',
}


def _add(report, severity, check, message, **data):
    report.append({
        'severity': severity,
        'check': check,
        'message': message,
        **data,
    })
    print(f'[{severity.upper()}] {check}: {message}', flush=True)


def _backup_database(connection, db_path):
    backup_dir = PROJECT_ROOT / 'config' / 'backups' / 'database_health'
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup_path = backup_dir / f'{Path(db_path).stem}-before-repair-{stamp}.db'
    destination = sqlite3.connect(str(backup_path))
    try:
        connection.backup(destination)
    finally:
        destination.close()
    return backup_path


def rebuild_search_index(connection):
    normalizer = SearchEngine(None)
    connection.execute('DROP TABLE IF EXISTS search_index')
    connection.execute(
        '''
        CREATE VIRTUAL TABLE search_index USING fts5(
            name, description, normalized_name, normalized_description,
            file_id UNINDEXED, source UNINDEXED, tokenize="trigram"
        )
        '''
    )
    cursor = connection.execute(
        'SELECT name,description,file_id,source FROM files'
    )
    while True:
        rows = cursor.fetchmany(2000)
        if not rows:
            break
        connection.executemany(
            'INSERT INTO search_index VALUES (?,?,?,?,?,?)',
            [
                (
                    name or '', description or '',
                    normalizer.normalize_text(name or ''),
                    normalizer.normalize_text(description or ''),
                    file_id, source,
                )
                for name, description, file_id, source in rows
            ],
        )
    connection.commit()


def diagnose(db_path, *, full=False, repair_indexes=False):
    report = []
    if not os.path.isfile(db_path):
        _add(report, 'error', 'arquivo', f'Banco ausente: {db_path}')
        return report

    connection = sqlite3.connect(db_path, timeout=30)
    connection.execute('PRAGMA busy_timeout=30000')
    try:
        pragma = 'integrity_check' if full else 'quick_check'
        integrity = connection.execute(f'PRAGMA {pragma}').fetchone()
        integrity_ok = bool(integrity and integrity[0] == 'ok')
        _add(
            report,
            'ok' if integrity_ok else 'error',
            pragma,
            'Banco fisicamente integro.' if integrity_ok else str(integrity),
        )
        if not integrity_ok:
            return report

        schema_version = connection.execute('PRAGMA user_version').fetchone()[0]
        _add(
            report,
            'ok' if schema_version == SCHEMA_VERSION else 'warning',
            'schema',
            f'Versao {schema_version}; esperada {SCHEMA_VERSION}.',
            value=schema_version,
        )

        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        columns = {
            row[1] for row in connection.execute('PRAGMA table_info(files)')
        } if 'files' in tables else set()
        missing = sorted(REQUIRED_FILE_COLUMNS - columns)
        _add(
            report,
            'ok' if not missing else 'error',
            'estrutura',
            'Tabelas e colunas obrigatorias presentes.' if not missing
            else 'Colunas ausentes: ' + ', '.join(missing),
            missing=missing,
        )
        if missing:
            return report

        counts = dict(connection.execute(
            'SELECT source,COUNT(*) FROM files GROUP BY source'
        ))
        total = sum(counts.values())
        _add(
            report, 'ok', 'registros',
            f"Total={total:,}; local={counts.get('local', 0):,}; Drive={counts.get('drive', 0):,}.",
            counts=counts,
        )

        fts_exists = 'search_index' in tables
        fts_count = connection.execute(
            'SELECT COUNT(*) FROM search_index'
        ).fetchone()[0] if fts_exists else 0
        duplicate_fts_ids = 0
        missing_fts_ids = 0
        extra_fts_ids = 0
        if fts_exists:
            file_ids = {
                row[0] for row in connection.execute('SELECT file_id FROM files')
            }
            fts_ids = {
                row[0] for row in connection.execute(
                    'SELECT file_id FROM search_index'
                )
            }
            duplicate_fts_ids = max(0, fts_count - len(fts_ids))
            missing_fts_ids = len(file_ids - fts_ids)
            extra_fts_ids = len(fts_ids - file_ids)
        fts_ok = (
            fts_exists and fts_count == total
            and not duplicate_fts_ids
            and not missing_fts_ids
            and not extra_fts_ids
        )
        _add(
            report,
            'ok' if fts_ok else 'warning',
            'indice_busca',
            f'Indice={fts_count:,}; arquivos={total:,}; '
            f'ausentes={missing_fts_ids:,}; extras={extra_fts_ids:,}; '
            f'duplicados={duplicate_fts_ids:,}.',
            index_count=fts_count,
        )
        if repair_indexes and not fts_ok:
            backup = _backup_database(connection, db_path)
            print(f'[BACKUP] {backup}', flush=True)
            rebuild_search_index(connection)
            _add(report, 'ok', 'reparo_indice', 'Indice de busca reconstruido.')

        if 'drive_link_validation' in tables:
            validation = dict(connection.execute(
                'SELECT status,COUNT(*) FROM drive_link_validation GROUP BY status'
            ))
            severity = 'warning' if any(
                key.startswith(('ambiguous', 'hash_mismatch', 'size_mismatch'))
                for key in validation
            ) else 'ok'
            _add(
                report, severity, 'vinculos_drive',
                f"{sum(validation.values()):,} vinculos validados.",
                statuses=validation,
            )
        else:
            _add(
                report, 'warning', 'vinculos_drive',
                'Tabela de validacao ainda nao existe.',
            )

        config = ConfigManager()
        roots = config.get_resolved_scan_paths(persist=False)
        missing_roots = [path for path in roots if not os.path.isdir(path)]
        _add(
            report,
            'ok' if roots and not missing_roots else 'warning',
            'pastas_locais',
            ', '.join(roots) if roots else 'Nenhuma raiz local encontrada.',
            roots=roots,
        )

        if full:
            local_rows = connection.execute(
                "SELECT path FROM files WHERE source='local' AND path IS NOT NULL"
            )
            checked_paths = 0
            missing_paths = 0
            excluded_paths = 0
            excluded_drives = config.get_excluded_drive_letters()
            for (local_path,) in local_rows:
                drive = ntpath.splitdrive(
                    ntpath.normpath(str(local_path))
                )[0].upper()
                if drive and drive in excluded_drives:
                    excluded_paths += 1
                    continue
                checked_paths += 1
                if not os.path.exists(local_path):
                    missing_paths += 1
                if checked_paths % 10000 == 0:
                    print(
                        f'[PROGRESSO] caminhos locais: {checked_paths:,}',
                        flush=True,
                    )
            path_severity = (
                'ok' if missing_paths == 0 else
                ('warning' if missing_paths < max(100, checked_paths // 100)
                 else 'error')
            )
            _add(
                report, path_severity, 'arquivos_no_disco',
                f'Verificados={checked_paths:,}; ausentes={missing_paths:,}; '
                f'ignorados_por_unidade={excluded_paths:,}.',
                checked=checked_paths,
                missing=missing_paths,
                excluded=excluded_paths,
            )

        queue_path = PROJECT_ROOT / 'config' / 'staging_queue.json'
        queue_count = 0
        invalid_queue = 0
        if queue_path.is_file():
            try:
                data = json.loads(queue_path.read_text(encoding='utf-8'))
                records = data if isinstance(data, list) else data.get('items', [])
                for record in records:
                    try:
                        StagingItem.from_dict(record)
                        queue_count += 1
                    except Exception:
                        invalid_queue += 1
            except Exception:
                invalid_queue = 1
        _add(
            report,
            'warning' if invalid_queue else 'ok',
            'fila',
            f'Pendentes={queue_count}; invalidos={invalid_queue}.',
            pending=queue_count,
            invalid=invalid_queue,
        )

        snapshot_path, manifest, snapshot_reason = select_snapshot_database(
            config.get_shared_cache_candidates(SCHEMA_VERSION)
        )
        if snapshot_path:
            generation = (manifest or {}).get('generation')
            known_generation = config.get('shared_snapshot_generation')
            adopted = bool(
                generation and str(generation) == str(known_generation)
            )
            severity = 'ok' if manifest and adopted else 'warning'
            message = f'Snapshot encontrado: {snapshot_path}'
            if manifest:
                message += f'; geracao={generation}'
                if not adopted:
                    message += '; esta instalacao ainda nao adotou essa geracao'
                shared_counts = manifest.get('row_counts') or {}
                if shared_counts != counts:
                    message += '; contagens local/compartilhada diferem'
            _add(
                report, severity, 'snapshot_compartilhado', message,
                path=snapshot_path,
                generation=generation,
                known_generation=known_generation,
                local_counts=counts,
                shared_counts=(manifest or {}).get('row_counts'),
            )
        else:
            _add(
                report, 'warning', 'snapshot_compartilhado',
                snapshot_reason,
            )
    finally:
        connection.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='data/file_index.db')
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--repair-indexes', action='store_true')
    args = parser.parse_args()
    os.chdir(PROJECT_ROOT)
    report = diagnose(
        args.db, full=args.full, repair_indexes=args.repair_indexes
    )
    summary = Counter(item['severity'] for item in report)
    print('[RESUMO] ' + json.dumps(dict(summary), ensure_ascii=False), flush=True)
    if summary['error']:
        return 2
    if summary['warning']:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
