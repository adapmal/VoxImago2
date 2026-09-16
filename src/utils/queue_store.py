"""Durable local queue, independently backed up from the media catalogue."""
import json
import os
import sqlite3
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from src.utils.staging_schema import MAX_QUEUE_FILE_BYTES, MAX_QUEUE_ITEMS, StagingItem


def validated_records(records):
    if not isinstance(records, list) or len(records) > MAX_QUEUE_ITEMS:
        raise ValueError(f'Fila deve conter no máximo {MAX_QUEUE_ITEMS:,} operações.')
    result = [StagingItem.from_dict(row).to_dict() for row in records]
    encoded = json.dumps(result, ensure_ascii=False).encode('utf-8')
    if len(encoded) > MAX_QUEUE_FILE_BYTES - 4096:
        raise ValueError('Fila excede o limite de tamanho permitido (64 MB).')
    ids = [row['operation_id'] for row in result]
    if len(set(ids)) != len(ids):
        raise ValueError('Identificadores de operações repetidos na fila.')
    return result


def read_queue_json(path):
    if Path(path).stat().st_size > MAX_QUEUE_FILE_BYTES:
        raise ValueError('Arquivo da fila excede o limite de 64 MB.')
    with open(path, encoding='utf-8') as stream:
        data = json.load(stream)
    return validated_records(data if isinstance(data, list) else data['items'])


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class QueueStore:
    def __init__(self, path, legacy_path):
        self.path = Path(path)
        self.legacy_path = Path(legacy_path)
        self.backup_dir = self.path.parent / 'queue_autosave'
        self.revision = None

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(str(self.path), timeout=10)
        try:
            connection.execute('PRAGMA synchronous=FULL')
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # An unsuccessful migration must never turn the legacy queue into an empty one.
        with self.connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)')
            connection.execute('CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, position INTEGER, payload TEXT)')
            connection.execute('CREATE TABLE IF NOT EXISTS execution (id TEXT PRIMARY KEY, state TEXT, updated REAL)')
            initialized = connection.execute("SELECT value FROM metadata WHERE key='initialized'").fetchone()
        if not initialized:
            records = read_queue_json(self.legacy_path) if self.legacy_path.exists() else []
            self.save(records)
        return self.load()

    def load(self):
        with self.connect() as connection:
            connection.execute('BEGIN')
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Banco da fila danificado. Use uma cópia de recuperação.')
            revision = connection.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()
            self.revision = revision[0] if revision else None
            return validated_records([json.loads(row[0]) for row in connection.execute(
                'SELECT payload FROM items ORDER BY position')])

    def check_revision(self, connection):
        row = connection.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()
        if (row[0] if row else None) != self.revision:
            raise ValueError('A fila foi alterada por outra instância. Feche as outras janelas e reabra o aplicativo.')

    def save(self, records, *, completed=None):
        records = validated_records(records)
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self.check_revision(connection)
            previous = dict(connection.execute('SELECT id,payload FROM items'))
            ids = set()
            for position, record in enumerate(records):
                key = record['operation_id']
                ids.add(key)
                payload = json.dumps(record, ensure_ascii=False)
                if previous.get(key) != payload:
                    connection.execute('INSERT OR REPLACE INTO items VALUES (?,?,?)', (key, position, payload))
                else:
                    connection.execute('UPDATE items SET position=? WHERE id=? AND position!=?', (position, key, position))
            connection.executemany('DELETE FROM items WHERE id=?', [(key,) for key in previous.keys() - ids])
            if completed:
                connection.execute('INSERT OR REPLACE INTO execution VALUES (?,?,?)', (completed, 'completed', time.time()))
            connection.execute("INSERT OR REPLACE INTO metadata VALUES ('initialized','1')")
            revision = str(time.time_ns())
            connection.execute("INSERT OR REPLACE INTO metadata VALUES ('revision',?)", (revision,))
        self.revision = revision

    def begin_execution(self, operation_id):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self.check_revision(connection)
            state = connection.execute('SELECT state FROM execution WHERE id=?', (operation_id,)).fetchone()
            if state:
                raise ValueError('Esta operação já foi executada ou tem resultado incerto. Confira o arquivo/Drive antes de recriar a operação.')
            connection.execute('INSERT INTO execution VALUES (?,?,?)', (operation_id, 'uncertain', time.time()))

    def complete_execution(self, operation_id):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self.check_revision(connection)
            connection.execute('DELETE FROM items WHERE id=?', (operation_id,))
            connection.execute('INSERT OR REPLACE INTO execution VALUES (?,?,?)', (operation_id, 'completed', time.time()))
            revision = str(time.time_ns())
            connection.execute("INSERT OR REPLACE INTO metadata VALUES ('revision',?)", (revision,))
        self.revision = revision

    def autosave(self):
        # One SQLite read transaction supplies a coherent revision and payload.
        with self.connect() as connection:
            connection.execute('BEGIN')
            revision = connection.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0]
            records = [json.loads(row[0]) for row in connection.execute('SELECT payload FROM items ORDER BY position')]
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        slots = [self.backup_dir / f'queue-{i}.json' for i in range(1, 4)]
        for slot in slots:
            try:
                with slot.open(encoding='utf-8') as stream:
                    if json.load(stream).get('revision') == revision:
                        return False
            except (OSError, ValueError, AttributeError):
                pass
        target = min(slots, key=lambda p: p.stat().st_mtime if p.exists() else -1)
        records = validated_records(records)
        atomic_json(target, {'schema_version': 1, 'saved_at': time.time(), 'revision': revision, 'items': records})
        return True

    def recovery_records(self, path):
        records = read_queue_json(path)
        with self.connect() as connection:
            states = dict(connection.execute('SELECT id,state FROM execution'))
        # Never replay successful operations from an older backup.
        return [record for record in records if states.get(record['operation_id']) != 'completed']

    def restore(self, path, records=None):
        records = validated_records(records) if records is not None else read_queue_json(path)
        try:
            with self.connect() as connection:
                states = dict(connection.execute('SELECT id,state FROM execution'))
            records = [row for row in records if states.get(row['operation_id']) != 'completed']
            self.save(records)
        except sqlite3.DatabaseError as exc:
            if getattr(exc, 'sqlite_errorcode', None) not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                raise
            # Preserve evidence before replacing a damaged store. Lost execution
            # history means restored operations require manual review before replay.
            archive = self.path.with_name(self.path.name + f'.damaged-{time.time_ns()}')
            shutil.copy2(self.path, archive)
            for suffix in ('-journal', '-wal', '-shm'):
                sidecar = Path(str(self.path) + suffix)
                if sidecar.exists():
                    raise RuntimeError(f'Banco possui arquivos auxiliares ativos. Backup preservado em {archive}; feche todas as instâncias antes de recuperar.')
            temporary = self.path.with_name(self.path.name + f'.{uuid.uuid4().hex}.recovery-tmp')
            replacement = QueueStore(temporary, temporary.with_suffix('.no-legacy'))
            replacement.initialize()
            replacement.save(records)
            with replacement.connect() as connection:
                connection.executemany('INSERT OR REPLACE INTO execution VALUES (?,?,?)',
                    [(row['operation_id'], 'uncertain', time.time()) for row in records])
            os.replace(temporary, self.path)
        return self.load()
