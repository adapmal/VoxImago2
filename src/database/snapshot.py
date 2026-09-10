"""Snapshots compartilhados versionados e remapeamento de caminhos locais."""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import platform
import shutil
import sqlite3
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path


SCHEMA_VERSION = 2
SNAPSHOT_KEEP_GENERATIONS = 3


def sha256_file(path, chunk_size=8 * 1024 * 1024):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def manifest_path(base_path):
    root, _ext = os.path.splitext(base_path)
    return root + '.manifest.json'


def read_manifest(base_path):
    path = manifest_path(base_path)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def resolve_snapshot_database(base_path, max_schema_version=SCHEMA_VERSION):
    """Retorna ``(db_path, manifest)`` somente para snapshot verificavel."""
    manifest = read_manifest(base_path)
    if manifest:
        try:
            schema = int(manifest.get('schema_version', 0))
        except (TypeError, ValueError):
            return None, manifest
        if schema < 1 or schema > int(max_schema_version):
            return None, manifest
        filename = os.path.basename(str(manifest.get('database_file') or ''))
        if not filename:
            return None, manifest
        candidate = os.path.join(os.path.dirname(base_path), filename)
        if not os.path.isfile(candidate):
            return None, manifest
        expected = str(manifest.get('database_sha256') or '').lower()
        if not expected or sha256_file(candidate).lower() != expected:
            return None, manifest
        return candidate, manifest

    # Compatibilidade com o snapshot unico das versoes anteriores. Ele ainda e
    # aceito para a primeira migracao, mas os novos exports usam manifesto.
    if os.path.isfile(base_path):
        return base_path, None
    return None, None


def select_snapshot_database(base_paths, max_schema_version=SCHEMA_VERSION):
    """Seleciona um snapshot sem adivinhar entre copias divergentes.

    Snapshots versionados sempre vencem o formato legado. Duas montagens que
    apontam para a mesma geracao/hash sao equivalentes; geracoes ou hashes
    diferentes exigem intervencao do usuario.
    """
    manifested = []
    legacy = []
    seen = set()
    seen_files = []
    invalid_manifests = 0
    for base_path in base_paths:
        existing_manifest = read_manifest(base_path)
        snapshot_path, manifest = resolve_snapshot_database(
            base_path, max_schema_version=max_schema_version
        )
        if not snapshot_path:
            if existing_manifest:
                invalid_manifests += 1
            continue
        key = ntpath.normcase(ntpath.normpath(snapshot_path))
        same_file = False
        for previous in seen_files:
            try:
                if os.path.samefile(snapshot_path, previous):
                    same_file = True
                    break
            except OSError:
                continue
        if key in seen or same_file:
            continue
        seen.add(key)
        seen_files.append(snapshot_path)
        entry = (snapshot_path, manifest, base_path)
        if manifest:
            manifested.append(entry)
        else:
            legacy.append(entry)

    if manifested:
        signatures = {
            (
                str(manifest.get('generation') or ''),
                str(manifest.get('database_sha256') or '').lower(),
            )
            for _path, manifest, _base in manifested
        }
        if len(signatures) != 1:
            return None, None, (
                'Foram encontrados snapshots versionados divergentes em '
                'mais de uma unidade.'
            )
        path, manifest, _base = manifested[0]
        return path, manifest, 'snapshot versionado verificado'

    if len(legacy) == 1:
        path, manifest, _base = legacy[0]
        return path, manifest, 'snapshot legado unico'
    if len(legacy) > 1:
        # Algumas instalacoes do Google Drive expoem o mesmo compartilhamento
        # em duas letras. ``samefile`` nem sempre reconhece montagens virtuais;
        # hashes iguais permitem tratá-las como uma unica copia sem adivinhar.
        signatures = {
            (os.path.getsize(path), sha256_file(path))
            for path, _manifest, _base in legacy
        }
        if len(signatures) == 1:
            path, manifest, _base = legacy[0]
            return path, manifest, 'copias legadas equivalentes verificadas'
        return None, None, (
            'Foram encontrados snapshots legados em mais de uma unidade.'
        )
    if invalid_manifests:
        return None, None, 'Manifesto encontrado, mas o snapshot nao passou na verificacao.'
    return None, None, 'Nenhum snapshot compartilhado foi encontrado.'


def sqlite_is_healthy(path, *, full=False):
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False, 'arquivo ausente ou vazio'
    connection = None
    try:
        connection = sqlite3.connect(f'file:{Path(path).as_posix()}?mode=ro', uri=True, timeout=10)
        pragma = 'integrity_check' if full else 'quick_check'
        result = connection.execute(f'PRAGMA {pragma}').fetchone()
        if not result or result[0] != 'ok':
            return False, str(result[0] if result else 'sem resultado')
        return True, 'ok'
    except Exception as exc:
        return False, str(exc)
    finally:
        if connection is not None:
            connection.close()


def sqlite_has_files_schema(path):
    """Confirma que o arquivo e um snapshot VoxImago, mesmo quando pequeno."""
    connection = None
    try:
        connection = sqlite3.connect(
            f'file:{Path(path).as_posix()}?mode=ro', uri=True, timeout=10
        )
        columns = {
            row[1] for row in connection.execute('PRAGMA table_info(files)')
        }
        required = {'file_id', 'name', 'path', 'source'}
        return required.issubset(columns)
    except Exception:
        return False
    finally:
        if connection is not None:
            connection.close()


def infer_local_roots(connection, root_names=('Banco de Imagens', 'Image Bank'), sample_limit=5000):
    """Infere as raizes historicas gravadas num snapshot legado."""
    wanted = {name.casefold() for name in root_names}
    counts = Counter()
    rows = connection.execute(
        "SELECT path FROM files WHERE source='local' AND path IS NOT NULL LIMIT ?",
        (int(sample_limit),),
    )
    for (raw_path,) in rows:
        if not raw_path:
            continue
        parts = list(ntpath.normpath(raw_path).split('\\'))
        for index, part in enumerate(parts):
            if part.casefold() in wanted:
                counts[ntpath.normpath('\\'.join(parts[:index + 1]))] += 1
                break
    if not counts:
        return []
    top_count = counts.most_common(1)[0][1]
    return [path for path, count in counts.items() if count == top_count]


def _path_is_under(path, root):
    if not path or not root:
        return False
    normalized = ntpath.normcase(ntpath.normpath(path))
    normalized_root = ntpath.normcase(ntpath.normpath(root))
    return normalized == normalized_root or normalized.startswith(normalized_root + '\\')


def build_root_mapping(source_roots, target_roots):
    """Casa raizes pelo ultimo diretorio, sem aceitar empates."""
    sources = [ntpath.normpath(path) for path in source_roots if path]
    targets = [ntpath.normpath(path) for path in target_roots if path]
    mapping = []
    used_targets = set()
    root_aliases = {
        'banco de imagens': 'image-bank',
        'image bank': 'image-bank',
    }

    def root_key(path):
        name = ntpath.basename(path).casefold()
        return root_aliases.get(name, name)

    for source in sources:
        source_name = root_key(source)
        matches = [
            target for target in targets
            if root_key(target) == source_name
        ]
        if len(matches) == 1:
            target = matches[0]
            target_key = ntpath.normcase(target)
            if target_key not in used_targets and ntpath.normcase(source) != target_key:
                used_targets.add(target_key)
                mapping.append((source, target))
    return mapping


def rebase_local_paths(connection, source_root, target_root):
    """Troca uma raiz nos identificadores locais e no FTS em uma transacao."""
    source = ntpath.normcase(ntpath.normpath(source_root))
    target = ntpath.normcase(ntpath.normpath(target_root))
    if not source or not target or source == target:
        return 0

    connection.execute('BEGIN IMMEDIATE')
    try:
        rows = connection.execute(
            "SELECT file_id,path,parentId FROM files WHERE source='local'"
        ).fetchall()
        updates = []
        id_mapping = {}
        for file_id, path, parent_id in rows:
            if not _path_is_under(path, source):
                continue

            def rebased(value):
                if not _path_is_under(value, source):
                    return value
                normalized = ntpath.normcase(ntpath.normpath(value))
                return target + normalized[len(source):]

            new_id = rebased(file_id)
            new_path = rebased(path)
            new_parent = rebased(parent_id)
            updates.append((new_id, new_path, new_parent, file_id))
            id_mapping[file_id] = new_id

        # IDs sao chaves primarias; o prefixo de destino vazio foi descartado e
        # uma instalacao restaurada nao deve conter simultaneamente as duas
        # raizes. Ainda assim, detectar conflito e mais seguro que substituir.
        new_ids = [row[0] for row in updates]
        if len(new_ids) != len(set(new_ids)):
            raise RuntimeError('Remapeamento produziria identificadores locais duplicados.')

        connection.executemany(
            "UPDATE files SET file_id=?, path=?, parentId=? WHERE file_id=?",
            updates,
        )
        # ``file_id`` e UNINDEXED no FTS5. Atualizar linha a linha faria uma
        # varredura completa para cada arquivo; uma unica atualizacao mantem a
        # migracao linear mesmo em acervos com mais de cem mil registros.
        source_length = len(source)
        connection.execute(
            "UPDATE search_index "
            "SET file_id = ? || substr(file_id, ?) "
            "WHERE source='local' AND ("
            "lower(file_id)=lower(?) OR ("
            "lower(substr(file_id,1,?))=lower(?) "
            "AND substr(file_id,?,1) IN (?, ?)))",
            (
                target, source_length + 1, source,
                source_length, source, source_length + 1, '\\', '/',
            ),
        )
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='drive_link_validation'"
        ).fetchone()
        if table_exists:
            connection.executemany(
                "UPDATE drive_link_validation SET local_file_id=? WHERE local_file_id=?",
                [(new, old) for old, new in id_mapping.items()],
            )
        connection.commit()
        return len(updates)
    except Exception:
        connection.rollback()
        raise


def create_manifest(connection, database_path, csv_path, *, scan_roots, generation):
    counts = {
        source: count
        for source, count in connection.execute(
            "SELECT source, COUNT(*) FROM files GROUP BY source"
        )
    }
    validation_count = 0
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='drive_link_validation'"
    ).fetchone():
        validation_count = connection.execute(
            'SELECT COUNT(*) FROM drive_link_validation'
        ).fetchone()[0]
    return {
        'format': 'voximago-shared-snapshot',
        'schema_version': SCHEMA_VERSION,
        'generation': generation,
        'created_at': int(time.time()),
        'source_machine': platform.node(),
        'scan_roots': list(scan_roots or []),
        'database_file': os.path.basename(database_path),
        'database_size': os.path.getsize(database_path),
        'database_sha256': sha256_file(database_path),
        'csv_file': os.path.basename(csv_path),
        'csv_size': os.path.getsize(csv_path),
        'csv_sha256': sha256_file(csv_path),
        'row_counts': counts,
        'validation_count': validation_count,
    }


@contextmanager
def _snapshot_publish_lock(base_path, stale_after_seconds=2 * 60 * 60):
    """Serializa publicacoes feitas por varias maquinas no mesmo destino."""
    lock_path = manifest_path(base_path) + '.lock'
    token = uuid.uuid4().hex
    payload = {
        'token': token,
        'machine': platform.node(),
        'pid': os.getpid(),
        'created_at': int(time.time()),
    }

    for attempt in range(2):
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(
                    descriptor,
                    json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                )
            finally:
                os.close(descriptor)
            break
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock_path)
            except OSError:
                age = 0
            if attempt == 0 and age > stale_after_seconds:
                try:
                    os.remove(lock_path)
                    continue
                except OSError:
                    pass
            raise RuntimeError(
                'Outro computador esta publicando o snapshot compartilhado. '
                'Tente novamente quando essa operacao terminar.'
            )

    try:
        yield
    finally:
        # Nao remover um lock que tenha sido substituido depois de uma falha
        # ou de uma expiracao excepcionalmente longa.
        try:
            with open(lock_path, 'r', encoding='utf-8') as stream:
                current = json.load(stream)
            if current.get('token') == token:
                os.remove(lock_path)
        except (OSError, ValueError, TypeError, AttributeError):
            pass


def publish_generation(
    base_path,
    local_database,
    local_csv,
    manifest,
    *,
    expected_previous_generation=None,
):
    """Publica uma geracao sob lock e troca o manifesto por ultimo."""
    destination_dir = os.path.dirname(base_path)
    os.makedirs(destination_dir, exist_ok=True)
    with _snapshot_publish_lock(base_path):
        current = read_manifest(base_path)
        current_generation = (
            str(current.get('generation')) if current else None
        )
        expected = (
            str(expected_previous_generation)
            if expected_previous_generation is not None else None
        )
        if current_generation != expected:
            raise RuntimeError(
                'O snapshot compartilhado mudou desde a ultima leitura deste '
                'computador. Execute a verificacao de saude antes de publicar.'
            )
        return _publish_generation_unlocked(
            base_path, local_database, local_csv, manifest
        )


def _publish_generation_unlocked(base_path, local_database, local_csv, manifest):
    destination_dir = os.path.dirname(base_path)
    generation = str(manifest['generation'])
    root_name = os.path.splitext(os.path.basename(base_path))[0]
    remote_db = os.path.join(destination_dir, f'{root_name}.{generation}.db')
    remote_csv = os.path.join(destination_dir, f'{root_name}.{generation}.csv')

    def copy_verified(source, destination, expected_hash):
        temporary = destination + f'.tmp-{os.getpid()}-{time.time_ns()}'
        try:
            shutil.copy2(source, temporary)
            if sha256_file(temporary).lower() != expected_hash.lower():
                raise RuntimeError(f'Hash divergente ao publicar {os.path.basename(destination)}.')
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError:
                    pass

    manifest = dict(manifest)
    manifest['database_file'] = os.path.basename(remote_db)
    manifest['csv_file'] = os.path.basename(remote_csv)
    copy_verified(local_database, remote_db, manifest['database_sha256'])
    copy_verified(local_csv, remote_csv, manifest['csv_sha256'])

    pointer = manifest_path(base_path)
    pointer_tmp = pointer + f'.tmp-{os.getpid()}-{time.time_ns()}'
    try:
        with open(pointer_tmp, 'w', encoding='utf-8') as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pointer_tmp, pointer)
    finally:
        if os.path.exists(pointer_tmp):
            try:
                os.remove(pointer_tmp)
            except OSError:
                pass

    _prune_generations(base_path, keep=SNAPSHOT_KEEP_GENERATIONS)
    return remote_db, remote_csv, pointer


def _prune_generations(base_path, keep=SNAPSHOT_KEEP_GENERATIONS):
    destination_dir = os.path.dirname(base_path)
    root_name = os.path.splitext(os.path.basename(base_path))[0]
    prefix = root_name + '.'
    db_files = sorted(
        (
            path for path in Path(destination_dir).glob(f'{root_name}.*.db')
            if path.name.startswith(prefix)
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    current = read_manifest(base_path) or {}
    protected_name = os.path.basename(str(current.get('database_file') or ''))
    retained = []
    if protected_name:
        retained.extend(path for path in db_files if path.name == protected_name)
    for path in db_files:
        if path not in retained and len(retained) < max(1, int(keep)):
            retained.append(path)

    for old_db in (path for path in db_files if path not in retained):
        old_csv = old_db.with_suffix('.csv')
        try:
            old_db.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            old_csv.unlink(missing_ok=True)
        except OSError:
            pass
