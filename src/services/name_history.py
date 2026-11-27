"""Histórico portátil de nomes. Não substitui o catálogo nem modifica mídias."""
import json
import re
import sqlite3
import time
import unicodedata
from pathlib import Path

from src.utils.queue_store import atomic_json


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFD', text.casefold()) if not unicodedata.combining(c))


def history_path(db_name):
    return Path(db_name).resolve().parent / 'name_history' / 'history.db'


def history_revision(db_name):
    path = history_path(db_name)
    result = []
    for p in (path, Path(str(path) + '-wal')):
        try:
            stat = p.stat()
            result.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            result.append(None)  # WAL pode desaparecer durante o checkpoint.
    return tuple(result)


class NameHistory:
    def __init__(self, db_name):
        self.path = history_path(db_name)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=10)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.execute('CREATE TABLE IF NOT EXISTS records(event_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        self.conn.execute('CREATE VIRTUAL TABLE IF NOT EXISTS aliases USING fts5(event_id UNINDEXED, text, tokenize="trigram")')

    def record(self, event_id, before, new_name, old_path='', new_path=''):
        record = dict(event_id=event_id, file_id=before['id'], drive_id=before.get('driveId', ''),
                      old_name=before['name'], new_name=new_name, old_path=old_path, new_path=new_path,
                      parent_ids=before.get('parents', []), md5=before.get('md5Checksum', ''),
                      size=before.get('size', ''), confirmed_at=time.time())
        self.insert(record)

    def insert(self, record):
        with self.conn:
            self._insert(record)

    def _insert(self, record):
        required = ('event_id', 'file_id', 'drive_id', 'old_name', 'new_name', 'old_path', 'new_path')
        if not isinstance(record, dict) or any(not isinstance(record.get(k), str) for k in required):
            raise ValueError('Registro de histórico inválido.')
        if (not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', record['file_id'])
                or any(len(record[k]) > 32768 for k in required)):
            raise ValueError('ID ou tamanho de campo inválido no histórico.')
        existing = self.conn.execute('SELECT payload FROM records WHERE event_id=?', (record['event_id'],)).fetchone()
        if existing:
            # Importar outra cópia não substitui o histórico local.
            old = json.loads(existing[0])
            if any(old.get(k) != record.get(k) for k in required):
                raise ValueError('Históricos conflitantes para a mesma operação.')
            return
        self.conn.execute('INSERT INTO records VALUES (?,?)', (record['event_id'], json.dumps(record, ensure_ascii=False)))
        text = normalized(' '.join(record[k] for k in ('old_name', 'old_path', 'new_name', 'new_path')))
        self.conn.execute('INSERT INTO aliases VALUES (?,?)', (record['event_id'], text))

    def export(self, path):
        atomic_json(Path(path), dict(schema=1, records=[json.loads(r[0]) for r in self.conn.execute('SELECT payload FROM records')]))

    def import_file(self, path):
        path = Path(path)
        if path.stat().st_size > 50_000_000:
            raise ValueError('Histórico excede 50 MB; divida a exportação.')
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('schema') != 1 or not isinstance(data.get('records'), list):
            raise ValueError('Formato de histórico incompatível.')
        # Somente metadados; este importador nunca executa operações ou caminhos do arquivo.
        with self.conn:
            for record in data['records']:
                self._insert(record)
        return len(data['records'])

    def close(self):
        self.conn.close()


def build_drive_file_index(indexer):
    """Mapeia ID do Drive -> lista de file_ids locais/remotos presentes no catálogo."""
    from src.drive.match import extract_drive_file_id
    mapping = {}
    for fid, link in indexer.conn.execute('SELECT file_id,webContentLink FROM files'):
        mapping.setdefault(fid, []).append(fid)
        drive_id = extract_drive_file_id(link or '')
        if drive_id and drive_id != fid:
            mapping.setdefault(drive_id, []).append(fid)
    return mapping


def _resolve_file_ids(indexer, records, drive_index=None):
    ids = {json.loads(payload)['file_id'] for payload, in records}
    if not ids:
        return []
    if drive_index is None:
        drive_index = build_drive_file_index(indexer)
    matched = []
    seen = set()
    for did in ids:
        for fid in drive_index.get(did, ()):
            if fid not in seen:
                seen.add(fid)
                matched.append(fid)
    return matched


def historical_file_ids(indexer, fts_query, drive_index=None):
    """IDs locais/Drive para a busca normal; preserva os filtros do SearchEngine."""
    path = history_path(indexer.db_name)
    if not path.exists():
        return []
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        records = conn.execute('SELECT r.payload FROM aliases a JOIN records r ON r.event_id=a.event_id WHERE aliases MATCH ?',
                               (fts_query,)).fetchall()
        return _resolve_file_ids(indexer, records, drive_index=drive_index)
    finally:
        conn.close()


def historical_file_ids_short(indexer, short_term, drive_index=None):
    """Correspondência de palavra inteira no histórico para termos com menos de 3 letras."""
    path = history_path(indexer.db_name)
    if not path.exists() or not short_term:
        return []
    term = normalized(short_term)
    if not term:
        return []
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        rows = conn.execute('SELECT r.payload, a.text FROM aliases a JOIN records r ON r.event_id=a.event_id').fetchall()
        matched_records = []
        pattern = re.compile(r'(?<![0-9a-z])' + re.escape(term) + r'(?![0-9a-z])')
        for payload, text in rows:
            if pattern.search(text or ''):
                matched_records.append((payload,))
        return _resolve_file_ids(indexer, matched_records, drive_index=drive_index)
    finally:
        conn.close()
