"""Prepare a tag batch without touching media paths or Qt widgets."""
import sqlite3
from pathlib import Path
from PyQt6.QtCore import QThread, pyqtSignal
from src.utils.staging_schema import StagingItem

TAG_ACTIONS = ('set_description', 'add_tags', 'remove_tags')


def parse_tags(text):
    parts = text.split(',') if ',' in text else text.split()
    result, seen = [], set()
    for part in parts:
        tag = part.strip()
        if tag and tag.lower() not in seen:
            result.append(tag)
            seen.add(tag.lower())
    return result


def effective_description(description, operations):
    for item in operations:
        if item.action_type == 'set_description':
            description = item.new_value
        elif item.action_type == 'add_tags':
            tags = parse_tags(description)
            known = {t.lower() for t in tags}
            for tag in parse_tags(item.new_value):
                if tag.lower() not in known:
                    tags.append(tag)
                    known.add(tag.lower())
            description = ', '.join(tags)
        elif item.action_type == 'remove_tags':
            removed = {t.lower() for t in parse_tags(item.new_value)}
            description = ', '.join(t for t in parse_tags(description) if t.lower() not in removed)
    return description


def prepare_batch(db_path, files, records, added, removed, cancelled, progress):
    prior = [StagingItem.from_dict(record) for record in records]
    operations = {}
    for item in prior:
        if item.action_type in TAG_ACTIONS:
            operations.setdefault(item.file_id, []).append(item)
    selected = {item.get('file_id') or item.get('id'): item for item in files}
    if None in selected or '' in selected:
        raise ValueError('Seleção contém arquivo sem identificador.')
    # Indexed primary-key lookups in bounded groups, on a private read connection.
    descriptions = {}
    connection = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        ids = list(selected)
        connection.execute('BEGIN')
        for offset in range(0, len(ids), 400):
            if cancelled():
                return None
            group = ids[offset:offset + 400]
            descriptions.update(connection.execute(
                'SELECT file_id,description FROM files WHERE file_id IN (' + ','.join('?' for _ in group) + ')', group))
    finally:
        connection.close()
    result = [item for item in prior if not (item.file_id in selected and item.action_type in TAG_ACTIONS)]
    for position, (fid, file_item) in enumerate(selected.items(), 1):
        if cancelled():
            return None
        if fid not in descriptions:
            raise ValueError(f'Arquivo saiu do catálogo: {file_item.get("name", fid)}. Atualize a seleção e tente novamente.')
        original = descriptions[fid] or ''
        effective = effective_description(original, operations.get(fid, []))
        tags = [tag for tag in parse_tags(effective) if tag.lower() not in removed]
        known = {tag.lower() for tag in tags}
        for tag in added:
            if tag.lower() not in known:
                tags.append(tag)
                known.add(tag.lower())
        description = ', '.join(tags)
        if description != original:
            result.append(StagingItem(fid, file_item.get('name', ''), file_item.get('path') or '',
                'set_description', old_value=original, new_value=description))
        if position % 100 == 0 or position == len(selected):
            progress(position, len(selected))
    return [item.to_dict() for item in result]


class BatchTagsWorker(QThread):
    progress = pyqtSignal(int, int)
    saving = pyqtSignal()
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, db_path, files, records, added, removed, store, parent=None):
        super().__init__(parent)
        self.arguments = (db_path, files, records, added, removed)
        self.store = store

    def run(self):
        try:
            records = prepare_batch(*self.arguments, self.isInterruptionRequested, self.progress.emit)
            if records is None or self.isInterruptionRequested():
                self.cancelled.emit()
                return
            self.saving.emit()
            # Cancellation is no longer accepted once the atomic commit starts.
            self.store.save(records)
            self.succeeded.emit(records)
        except Exception as exc:
            self.failed.emit(str(exc))
