"""Persistencia e politica da revalidacao de vinculos Drive/local.

A tabela e separada de ``files`` para nao quebrar bancos antigos nem os inserts
posicionais ainda usados pelo indexador.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


VALIDATED_LINK_STATUSES = frozenset({
    'trusted_metadata',
    'valid_hash',
    'valid_hash_relinked',
})


@dataclass(frozen=True)
class LinkValidation:
    local_file_id: str
    drive_id: str
    status: str
    remote_md5: str = ''
    local_md5: str = ''

    @property
    def allows_metadata_sync(self) -> bool:
        return self.status in VALIDATED_LINK_STATUSES


def ensure_validation_table(connection_or_cursor):
    """Cria a tabela auxiliar sem alterar a estrutura da tabela ``files``."""
    cursor = (
        connection_or_cursor.cursor()
        if isinstance(connection_or_cursor, sqlite3.Connection)
        else connection_or_cursor
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS drive_link_validation (
            local_file_id TEXT PRIMARY KEY,
            drive_id TEXT NOT NULL,
            status TEXT NOT NULL,
            remote_name TEXT,
            remote_size INTEGER,
            remote_md5 TEXT,
            local_md5 TEXT,
            hierarchy_score INTEGER DEFAULT 0,
            details TEXT,
            validated_at INTEGER NOT NULL
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_drive_link_validation_drive_id "
        "ON drive_link_validation(drive_id)"
    )
    return cursor


def load_validations(cursor, local_file_ids):
    """Carrega validacoes em lotes compativeis com o limite de parametros SQLite."""
    ids = list(dict.fromkeys(str(value) for value in local_file_ids if value))
    if not ids:
        return {}

    result = {}
    try:
        for start in range(0, len(ids), 500):
            batch = ids[start:start + 500]
            placeholders = ','.join('?' for _ in batch)
            cursor.execute(
                "SELECT local_file_id, drive_id, status, "
                "COALESCE(remote_md5, ''), COALESCE(local_md5, '') "
                f"FROM drive_link_validation WHERE local_file_id IN ({placeholders})",
                batch,
            )
            for row in cursor.fetchall():
                validation = LinkValidation(*row)
                result[validation.local_file_id] = validation
    except sqlite3.OperationalError as exc:
        # Bancos que ainda nao passaram pela migracao continuam funcionando.
        if 'no such table' not in str(exc).casefold():
            raise
    return result


def validation_blocks(validation, drive_id):
    """Informa se uma decisao anterior proibe a fusao deste par especifico."""
    if validation is None or validation.drive_id != drive_id:
        return False
    return not validation.allows_metadata_sync
