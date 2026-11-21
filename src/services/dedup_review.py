"""Revisão local durável. Não executa operações nem acessa mídias/Drive."""
import json
from dataclasses import asdict, fields
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from src.services.hash_deduplicator import DuplicateGroup
from src.utils.queue_store import atomic_json

_WRITER = ThreadPoolExecutor(max_workers=1, thread_name_prefix='dedup-review')


def review_path(db_name):
    return Path(db_name).resolve().parent / 'dedup' / 'review_plan.json'


def save_review(db_name, groups, settings, stale=False, in_flight=()):
    # Snapshot independente na thread da interface; JSON e disco em um escritor
    # serial para nunca uma gravação antiga ultrapassar uma revisão mais recente.
    records = []
    for group in groups:
        record = asdict(group)
        record['eligible_ids'] = sorted(group.eligible_ids)
        records.append(record)
    payload = dict(version=1, database=str(Path(db_name).resolve()), groups=records,
                   settings=dict(settings), stale=bool(stale), in_flight=list(in_flight))
    return _WRITER.submit(atomic_json, review_path(db_name), payload)


def load_review(db_name):
    path = review_path(db_name)
    if not path.exists():
        return None
    if path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError('Revisão muito grande; arquivo preservado para investigação.')
    data = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(data, dict) or data.get('version') != 1
            or data.get('database') != str(Path(db_name).resolve())):
        raise ValueError('Revisão incompatível com este banco.')
    if not isinstance(data.get('groups'), list) or not isinstance(data.get('settings'), dict):
        raise ValueError('Estrutura da revisão inválida.')
    settings = data['settings']
    if (type(settings.get('mode')) is not int or settings['mode'] not in range(4)
            or not isinstance(settings.get('folders'), list)
            or any(not isinstance(p, str) for p in settings['folders'])
            or not isinstance(settings.get('destination'), str)
            or type(settings.get('videos')) is not bool
            or settings.get('drive') is not None and not isinstance(settings['drive'], str)):
        raise ValueError('Configuração da revisão inválida.')
    names = {f.name for f in fields(DuplicateGroup)}
    groups = []
    for record in data['groups']:
        if not isinstance(record, dict) or set(record) - names:
            raise ValueError('Grupo inválido na revisão.')
        if not isinstance(record.get('size'), int) or record['size'] <= 0:
            raise ValueError('Tamanho inválido na revisão.')
        if not isinstance(record.get('md5'), str) or not isinstance(record.get('keeper'), dict):
            raise ValueError('Identidade inválida na revisão.')
        for key in ('duplicates', 'year_context', 'eligible_ids', 'source_folders', 'reference_folders'):
            if not isinstance(record.get(key), list):
                raise ValueError('Lista inválida na revisão: ' + key)
        for entry in [record['keeper'], *record['duplicates'], *record['year_context']]:
            if not isinstance(entry, dict) or any(not isinstance(entry.get(k), str) for k in ('drive_id', 'name', 'path')):
                raise ValueError('Arquivo inválido na revisão.')
            if entry.get('description') is not None and not isinstance(entry['description'], str):
                raise ValueError('Descrição inválida na revisão.')
        for key in ('merged_description', 'plan_error', 'plan_root', 'year_source', 'scope_mode'):
            if not isinstance(record.get(key), str):
                raise ValueError('Campo inválido na revisão: ' + key)
        if record.get('edited_description') is not None and not isinstance(record['edited_description'], str):
            raise ValueError('Tags manuais inválidas na revisão.')
        if type(record.get('checked')) is not bool:
            raise ValueError('Seleção inválida na revisão.')
        if record.get('target_year') is not None and (not isinstance(record['target_year'], int)
                                                    or not 1900 <= record['target_year'] <= 2100):
            raise ValueError('Ano inválido na revisão.')
        if any(not isinstance(v, str) for k in ('eligible_ids', 'source_folders', 'reference_folders') for v in record[k]):
            raise ValueError('Identificadores inválidos na revisão.')
        record['eligible_ids'] = set(record['eligible_ids'])
        groups.append(DuplicateGroup(**record))
    data['groups'] = groups
    if (not isinstance(data.get('in_flight', []), list)
            or any(not isinstance(v, str) for v in data.get('in_flight', []))):
        raise ValueError('Estado de execução inválido.')
    return data
