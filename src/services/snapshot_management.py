"""Distribuição manual do catálogo e backups locais, sem acesso às mídias."""
from __future__ import annotations

import json
import logging
import ntpath
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

from src.database.snapshot import (
    SCHEMA_VERSION, infer_local_roots, resolve_snapshot_database,
    rebase_local_paths, sha256_file, sqlite_is_healthy,
)
from src.utils.queue_store import atomic_json, read_queue_json


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)


def check_path(path):
    # Não sondar unidades excluídas, nem mesmo para verificar a existência.
    from src.utils.config_manager import ConfigManager
    excluded = set(ConfigManager().get_excluded_drive_letters()) | {'O:'}
    if ntpath.splitdrive(str(path))[0].upper() in excluded:
        raise ValueError('Esta unidade está excluída. Escolha outro caminho.')
    return Path(path).resolve()


def catalog_info(path):
    healthy, reason = sqlite_is_healthy(path, full=True)
    if not healthy:
        raise ValueError('Banco não verificado: ' + reason)
    conn = readonly(path)
    try:
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version != SCHEMA_VERSION:
            raise ValueError(f'Versão de banco {version}; esperada {SCHEMA_VERSION}. Atualize a instalação de origem e publique novamente.')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(files)')}
        if not {'file_id', 'name', 'path', 'source', 'description', 'thumbnailRotation'}.issubset(columns):
            raise ValueError('Estrutura do catálogo incompatível.')
        counts = dict(conn.execute('SELECT source, COUNT(*) FROM files GROUP BY source'))
        indexed = conn.execute('SELECT COUNT(*) FROM search_index').fetchone()[0]
        if indexed != sum(counts.values()) or conn.execute(
                'SELECT file_id FROM files EXCEPT SELECT file_id FROM search_index LIMIT 1').fetchone() or conn.execute(
                'SELECT file_id FROM search_index EXCEPT SELECT file_id FROM files LIMIT 1').fetchone():
            raise ValueError('Índice de busca inconsistente. Repare na instalação de origem antes de publicar/adotar.')
        roots = infer_local_roots(conn)
        validations = 0
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='drive_link_validation'").fetchone():
            validations = conn.execute('SELECT COUNT(*) FROM drive_link_validation').fetchone()[0]
        return dict(counts=counts, roots=roots, version=version, validations=validations)
    finally:
        conn.close()


def inspect_snapshot(base):
    base = check_path(base)
    if str(base).endswith('.manifest.json'):
        base = Path(str(base)[:-len('.manifest.json')] + '.db')
    path, manifest = resolve_snapshot_database(str(base))
    if not path:
        raise ValueError('Snapshot ausente, incompatível ou com hash divergente.')
    path = check_path(path)
    if manifest:
        from src.utils.config_manager import ConfigManager
        config = ConfigManager()
        mode = 'sandbox' if config.is_sandbox() else 'production'
        if manifest.get('catalog_mode') and manifest['catalog_mode'] != mode:
            raise ValueError('Snapshot de outro modo (produção/sandbox). Troque o modo antes de adotar.')
        if manifest.get('drive_id') and manifest['drive_id'] != config.get_current_drive_id():
            raise ValueError('O snapshot pertence a outro Drive. Confira a configuração desta instalação.')
    info = catalog_info(path)
    info.update(path=str(path), manifest=manifest or {}, sha256=sha256_file(path))
    if manifest and manifest.get('scan_roots'):
        info['roots'] = manifest['scan_roots']
    return info


def copy_database(source, destination):
    """Backup online SQLite: inclui WAL e nunca copia um .db ativo com shutil."""
    src = readonly(source)
    dst = sqlite3.connect(str(destination))
    deadline = time.monotonic() + 120
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError('Backup não concluiu em 120 segundos; tente com o banco ocioso.')
    try:
        src.backup(dst, pages=512, progress=progress, sleep=0.05)
        dst.execute('PRAGMA journal_mode=DELETE')
    finally:
        dst.close()
        src.close()


def backup_directory(db):
    return Path(db).resolve().parent / 'catalog_backups' / Path(db).stem


def local_backup(db, *, automatic=False):
    db = Path(db).resolve()
    folder = backup_directory(db)
    existing = sorted(folder.glob('auto-*.db'), key=lambda p: p.stat().st_mtime, reverse=True) if folder.exists() else []
    if automatic and existing and time.time() - existing[0].stat().st_mtime < 86400:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    name = ('auto-' if automatic else 'before-restore-') + time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.db'
    destination = folder / name
    temp = destination.with_suffix('.tmp')
    try:
        copy_database(db, temp)
        healthy, reason = sqlite_is_healthy(temp, full=True)
        if not healthy:
            raise ValueError('Backup inválido: ' + reason)
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    if automatic:
        # Rotação restrita a backups automáticos deste catálogo. Nunca elimina
        # backups anteriores a restaurações nem arquivos de outra pasta.
        for old in existing[2:]:
            old.unlink()
    return str(destination)


def assert_queue_empty(db):
    queue_path = Path(db).resolve().parent / 'staging_queue.db'
    if queue_path.exists():
        conn = readonly(queue_path)
        try:
            if conn.execute('SELECT COUNT(*) FROM items').fetchone()[0]:
                raise ValueError('A fila precisa estar vazia. Exporte e revise as operações antes de restaurar o catálogo.')
        finally:
            conn.close()
    else:
        legacy = Path(db).resolve().parent.parent / 'config' / 'staging_queue.json'
        if legacy.exists() and read_queue_json(legacy):
            raise ValueError('Existe uma fila legada pendente. Resolva-a antes da restauração.')


def pending_path(db):
    return Path(str(Path(db).resolve()) + '.adoption.json')


def prepare_adoption(db, info, mapping):
    """Prepara arquivo local sem tocar no catálogo atual; exige confirmação UI."""
    assert_queue_empty(db)
    original_roots = [ntpath.normcase(ntpath.normpath(root)) for root in info['roots']]
    mapped_roots = [ntpath.normcase(ntpath.normpath(old)) for old, _ in mapping]
    targets = [ntpath.normcase(ntpath.normpath(new)) for _, new in mapping]
    if sorted(original_roots) != sorted(mapped_roots) or len(targets) != len(set(targets)):
        raise ValueError('Mapeie cada raiz de origem para uma pasta de destino distinta.')
    for index, source_root in enumerate(mapped_roots):
        for other_index, other in enumerate(mapped_roots):
            if index != other_index and (source_root.startswith(other + '\\') or
                    targets[index] == other or targets[index].startswith(other + '\\')):
                raise ValueError('Raízes sobrepostas exigem revisão manual antes da adoção.')
    source = check_path(info['path'])
    wal = Path(str(source) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('A origem parece ser um banco ativo com WAL. Use um snapshot publicado ou backup consolidado.')
    if sha256_file(source) != info['sha256']:
        raise ValueError('Snapshot mudou após a comparação; compare novamente.')
    db = Path(db).resolve()
    if source == db:
        raise ValueError('O snapshot deve ser diferente do banco ativo.')
    marker = pending_path(db)
    if marker.exists():
        raise ValueError('Já existe uma adoção preparada. Reinicie ou cancele-a primeiro.')
    stage = db.with_name(db.name + '.adopt-' + uuid.uuid4().hex + '.db')
    db.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Snapshot é imutável; validar os bytes copiados antes de remapear.
        shutil.copy2(source, stage)
        if sha256_file(stage) != info['sha256']:
            raise ValueError('Cópia local diverge do snapshot conferido.')
        catalog_info(stage)
        conn = sqlite3.connect(str(stage))
        try:
            for old, new in mapping:
                check_path(new)
                if not os.path.isdir(new):
                    raise ValueError(f'Pasta de destino indisponível: {new}')
                if ntpath.normcase(old) != ntpath.normcase(new):
                    rebase_local_paths(conn, old, new)
            # Todo item local precisa estar coberto pelo mapeamento confirmado.
            roots = [ntpath.normcase(ntpath.normpath(new)) for _, new in mapping]
            for (path,) in conn.execute("SELECT path FROM files WHERE source='local'"):
                normalized = ntpath.normcase(ntpath.normpath(path or ''))
                if not any(normalized == root or normalized.startswith(root + '\\') for root in roots):
                    raise ValueError('Há caminhos locais fora das raízes mapeadas. Adoção cancelada.')
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            conn.execute('PRAGMA journal_mode=DELETE')
        finally:
            conn.close()
        catalog_info(stage)
        atomic_json(marker, dict(stage=stage.name, sha256=sha256_file(stage),
                                generation=info['manifest'].get('generation'),
                                scan_roots=[new for _, new in mapping]))
    except Exception:
        stage.unlink(missing_ok=True)
        raise
    return str(marker)


def cancel_adoption(db):
    # A cópia preparada fica preservada; somente a autorização é retirada.
    pending_path(db).unlink(missing_ok=True)


def persist_adoption_config(data):
    # ConfigManager.set registra falhas de gravação, mas não as propaga.
    # Aqui a falha precisa impedir que a autorização de adoção seja descartada.
    from src.utils.config_manager import ConfigManager, SETTINGS_FILE
    config = ConfigManager()
    settings = dict(config.settings)
    settings.update(scan_paths=data['scan_roots'], scan_folders=data['scan_roots'],
                    shared_snapshot_generation=data.get('generation'),
                    last_sync_timestamp=1, snapshot_reconcile_pending=True)
    atomic_json(SETTINGS_FILE, settings)
    config.settings.update(settings)


def apply_pending_adoption(db):
    """Somente na inicialização, antes de abrir FileIndexer e workers."""
    db = Path(db).resolve()
    marker = pending_path(db)
    data = json.loads(marker.read_text(encoding='utf-8'))
    stage_name = data['stage']
    if Path(stage_name).name != stage_name or not stage_name.startswith(db.name + '.adopt-'):
        raise ValueError('Caminho de adoção inválido.')
    stage = db.parent / stage_name
    if stage.resolve().parent != db.parent:
        raise ValueError('A cópia preparada deve estar na pasta do banco local.')
    if sha256_file(stage) != data['sha256']:
        raise ValueError('Cópia preparada foi alterada. O banco atual não foi substituído.')
    catalog_info(stage)
    for root in data['scan_roots']:
        check_path(root)
        if not os.path.isdir(root):
            raise ValueError(f'Pasta mapeada indisponível: {root}. Conecte a unidade e tente novamente.')
    assert_queue_empty(db)
    backup = None
    if db.exists() and db.stat().st_size:
        healthy, reason = sqlite_is_healthy(db)
        if healthy:
            backup = local_backup(db)
        else:
            if not any(word in reason.lower() for word in ('malformed', 'not a database', 'corrupt')):
                raise RuntimeError('Não foi possível validar o banco atual; restauração bloqueada: ' + reason)
            # Preservar banco danificado E journals antes de uma restauração explícita.
            folder = backup_directory(db) / ('damaged-' + uuid.uuid4().hex)
            folder.mkdir(parents=True)
            for suffix in ('', '-wal', '-shm', '-journal'):
                original = Path(str(db) + suffix)
                if original.exists():
                    shutil.copy2(original, folder / original.name)
            backup = str(folder)
    # Checkpoint só com conexões encerradas. Se o banco estiver ocupado, abortar.
    if db.exists() and db.stat().st_size and sqlite_is_healthy(db)[0]:
        conn = sqlite3.connect(str(db), timeout=2)
        try:
            busy, _, _ = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if busy:
                raise RuntimeError('Banco em uso por outro processo. Feche-o antes de restaurar.')
            conn.execute('BEGIN EXCLUSIVE')
            conn.rollback()
        finally:
            conn.close()
    # stage é mantido até o fim: falha antes da troca permite tentar de novo.
    temp = db.with_name(db.name + '.replacement-' + uuid.uuid4().hex)
    try:
        shutil.copy2(stage, temp)
        # Remover apenas sidecars do alvo exato, já preservados/checados acima.
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(db) + suffix).unlink(missing_ok=True)
        os.replace(temp, db)
    finally:
        temp.unlink(missing_ok=True)
    logging.info('Snapshot adotado manualmente: %s; backup anterior: %s', stage.name, backup)
    persist_adoption_config(data)
    marker.unlink()
    try:
        stage.unlink(missing_ok=True)
    except OSError:
        logging.warning('Adoção concluída; cópia temporária preservada em %s', stage)
    return backup
