"""Prepare a tag batch without touching media paths or Qt widgets.

Regras automáticas para arquivos RAW e suas cópias (JPG/PNG/HEIC/etc.):
- Sempre que um arquivo RAW recebe/edita tags na fila, a tag 'RAW' é somada automaticamente
  (caso ainda não esteja presente) — e nunca é propagada para cópias JPG/PNG.
- Sempre que um arquivo RAW ou sua cópia (JPG/PNG/HEIC) tem tags editadas/adicionadas/removidas,
  o catálogo local é consultado pelo `LocalRawPairFinder` para trazer automaticamente o(s)
  arquivo(s) correspondente(s) (JPG/PNG ↔ RAW) para a fila com as mesmas edições de tags
  (preservando tags próprias de cada arquivo e sem copiar nomes antigos de duplicatas).
"""
import ntpath
import re
import sqlite3
from pathlib import Path
from PyQt6.QtCore import QThread, pyqtSignal
from src.database.database import to_canonical_id
from src.utils.staging_schema import StagingItem, physical_item_path
from src.services.raw_tag_sync import (
    LocalRawPairFinder,
    RAW_ONLY_TAGS_FOLDED,
    RAW_TAG_LABEL,
    is_old_filename_tag,
    is_raw_filename,
)

TAG_ACTIONS = ('set_description', 'add_tags', 'remove_tags')
ACTIVE_STATES = frozenset({'applying', 'local_applied', 'waiting_cloud', 'uncertain'})


def _identity_key(value):
    """Normaliza caminhos locais Windows (case-insensitive) preservando case de IDs remotos do Drive."""
    if not value:
        return ''
    text = str(value).strip()
    return ntpath.normcase(ntpath.normpath(text)) if ntpath.isabs(text) else text


def parse_tags(text):
    if not text:
        return []
    parts = text.split(',') if ',' in text else text.split()
    result, seen = [], set()
    for part in parts:
        tag = part.strip()
        if tag and tag.lower() not in seen:
            result.append(tag)
            seen.add(tag.lower())
    return result


def parse_delimited_tags(text):
    """Divide entrada explícita de tags separadas por vírgula/ponto-e-vírgula/linha (preservando espaços internos)."""
    if not text:
        return []
    result, seen = [], set()
    for part in re.split(r'[,;\n]+', str(text)):
        tag = part.strip()
        if tag and tag.lower() not in seen:
            result.append(tag)
            seen.add(tag.lower())
    return result


def _dedupe_tag_list(tags):
    result, seen = [], set()
    for raw in tags or ():
        tag = str(raw or '').strip()
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


def _apply_tag_delta(
    base_desc,
    added,
    removed,
    *,
    filename_or_path='',
    is_counterpart=False,
    ensure_raw_if_unchanged=True,
):
    """Aplica adições e remoções de tags respeitando as regras da tag RAW exclusiva e nomes antigos.

    Retorna `(new_description, content_changed)` onde `content_changed` indica se alguma tag
    (excluindo o acréscimo automático de 'RAW') foi efetivamente adicionada ou removida.
    """
    target_is_raw = is_raw_filename(filename_or_path)
    removed_lower = {t.lower() for t in removed}
    # Quando propagando para um parceiro RAW, nunca remover 'RAW' por efeito colateral do JPG
    if is_counterpart and target_is_raw:
        removed_lower = removed_lower - set(RAW_ONLY_TAGS_FOLDED)

    base_tags = parse_tags(base_desc)
    tags = [tag for tag in base_tags if tag.lower() not in removed_lower]
    content_changed = len(tags) != len(base_tags)

    known = {tag.lower() for tag in tags}
    for tag in added:
        tlow = tag.lower()
        if tlow in known:
            continue
        if is_counterpart and is_old_filename_tag(tag):
            continue
        if not target_is_raw and tlow in RAW_ONLY_TAGS_FOLDED:
            continue
        tags.append(tag)
        known.add(tlow)
        content_changed = True

    if (
        target_is_raw
        and (content_changed or ensure_raw_if_unchanged)
        and RAW_TAG_LABEL.lower() not in known
        and RAW_TAG_LABEL.lower() not in removed_lower
    ):
        tags.append(RAW_TAG_LABEL)

    # Preserva exatamente o texto original (ex.: tags legadas separadas por espaço) se nada mudou
    if not content_changed and [t.lower() for t in tags] == [t.lower() for t in base_tags]:
        return base_desc, False

    return ', '.join(tags), content_changed


def build_single_edit_items(db_path, file_item, new_desc, prior_records, *, active_tags=None):
    """Prepara os itens da fila para a edição individual de tags de um arquivo.

    Retorna `(items_to_stage, matching_ids_to_replace, partner_count, final_new_desc, raw_was_auto_added)` onde:
    - Se `file_item` for RAW e estiver recebendo/editando tags, garante a tag `RAW` no próprio arquivo
      (a menos que o usuário tenha acabado de removê-la explicitamente nesta edição);
    - Procura automaticamente correspondentes RAW ↔ JPG/PNG (e DNGs derivados do mesmo disparo) no catálogo local
      e inclui os parceiros na fila com as mesmas edições de tags (sem propagar a tag `RAW` para JPG/PNG,
      sem copiar nomes antigos de duplicatas e sem tocar em parceiros com operação ativa ou exclusão na fila).
    """
    raw_fid = file_item.get('file_id') or file_item.get('id') or ''
    raw_path = physical_item_path(file_item)
    fname = file_item.get('name', '') or ntpath.basename(raw_path or raw_fid)
    src_is_raw = is_raw_filename(raw_path or fname)
    src_key = _identity_key(raw_fid or raw_path)

    prior = [StagingItem.from_dict(r) if isinstance(r, dict) else r for r in prior_records]
    operations = {}
    protected_keys = set()
    deleted_keys = set()
    for it in prior:
        keys = {_identity_key(v) for v in (it.file_id, it.path) if v}
        if it.execution_state in ACTIVE_STATES:
            protected_keys.update(keys)
        if it.action_type == 'delete':
            deleted_keys.update(keys)
        if it.action_type in TAG_ACTIONS:
            for k in keys:
                operations.setdefault(k, []).append(it)

    old_desc = file_item.get('description', '') or ''
    counterparts = []
    if db_path and Path(db_path).exists():
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            canon_fid = to_canonical_id(raw_path or raw_fid) if ntpath.isabs(raw_path or raw_fid) else (raw_fid or raw_path)
            row = conn.execute(
                'SELECT description FROM files WHERE file_id = ? OR file_id = ? OR path = ? LIMIT 1',
                (raw_fid, canon_fid, raw_path),
            ).fetchone()
            if row:
                old_desc = row[0] or ''
            finder = LocalRawPairFinder(conn)
            if finder.supported and (raw_path or raw_fid):
                counterparts = finder.find_counterparts(raw_fid, fname, raw_path)
        finally:
            conn.close()

    # Descobrir o delta efetivo desta edição comparando o estado anterior (efetivo na fila) com as tags novas
    prev_effective = effective_description(old_desc, operations.get(src_key, []))
    prev_tags = parse_tags(prev_effective)
    prev_lower = {t.lower() for t in prev_tags}

    # Se o caller (TagChipsWidget) já entregou a lista exata de chips ativos, preserve tags com espaço (ex.: 'São Paulo')
    if active_tags is not None:
        new_tags = _dedupe_tag_list(active_tags)
    else:
        new_tags = parse_tags(new_desc)
    new_lower = {t.lower() for t in new_tags}

    # Se o usuário está editando um arquivo RAW e NÃO removeu explicitamente a tag 'RAW' que já estava lá,
    # garantimos que a tag 'RAW' seja somada automaticamente!
    explicitly_removed_raw = any(t.lower() in RAW_ONLY_TAGS_FOLDED for t in prev_tags) and not (new_lower & set(RAW_ONLY_TAGS_FOLDED))
    raw_was_auto_added = False
    if src_is_raw and not explicitly_removed_raw and not (new_lower & set(RAW_ONLY_TAGS_FOLDED)):
        new_tags = [*new_tags, RAW_TAG_LABEL]
        new_lower = {t.lower() for t in new_tags}
        raw_was_auto_added = True

    final_new_desc = ', '.join(new_tags) if (raw_was_auto_added or active_tags is not None) else new_desc

    added_delta = [t for t in new_tags if t.lower() not in prev_lower]
    removed_delta = {t.lower() for t in prev_tags if t.lower() not in new_lower}

    # Também considerar as tags totais presentes no arquivo editado para somar no parceiro (caso RAW e JPG
    # tenham sido editados separadamente antes), sem nunca apagar tags extras que já existem só no parceiro!
    tags_to_propagate_add = []
    seen_prop = set()
    for t in [*new_tags, *added_delta]:
        tl = t.lower()
        if tl in seen_prop or tl in RAW_ONLY_TAGS_FOLDED or is_old_filename_tag(t):
            continue
        seen_prop.add(tl)
        tags_to_propagate_add.append(t)

    matching_ids = {_identity_key(val) for val in (raw_fid, raw_path) if val}

    items_to_stage = []
    if final_new_desc != old_desc:
        items_to_stage.append(
            StagingItem(raw_fid or raw_path, fname, raw_path, 'set_description', old_value=old_desc, new_value=final_new_desc)
        )

    partner_count = 0
    for cp in counterparts:
        cp_fid = cp['file_id']
        cp_path = cp['path']
        cp_name = cp['name']
        cp_keys = {_identity_key(val) for val in (cp_fid, cp_path) if val}
        # Nunca sobrescrever/tocar em parceiro que já esteja em confirmação na nuvem ou marcado para exclusão
        if cp_keys & (protected_keys | deleted_keys):
            continue

        cp_orig = cp['description'] or ''
        cp_key = _identity_key(cp_fid or cp_path)
        cp_eff = effective_description(cp_orig, operations.get(cp_key, []))
        cp_new, cp_changed = _apply_tag_delta(
            cp_eff,
            tags_to_propagate_add,
            removed_delta,
            filename_or_path=cp_path or cp_name,
            is_counterpart=True,
            ensure_raw_if_unchanged=True,
        )
        matching_ids.update(cp_keys)
        if cp_new != cp_orig:
            items_to_stage.append(
                StagingItem(cp_fid, cp_name, cp_path, 'set_description', old_value=cp_orig, new_value=cp_new)
            )
            partner_count += 1

    return items_to_stage, matching_ids, partner_count, final_new_desc, raw_was_auto_added


def prepare_batch_with_stats(db_path, files, records, added, removed, cancelled, progress):
    """Versão interna de `prepare_batch` que retorna `(records_list, partner_count, changed_count)`."""
    prior = [StagingItem.from_dict(record) for record in records]
    operations = {}
    protected_keys = set()
    deleted_keys = set()
    for item in prior:
        keys = {_identity_key(v) for v in (item.file_id, item.path) if v}
        if item.execution_state in ACTIVE_STATES:
            protected_keys.update(keys)
        if item.action_type == 'delete':
            deleted_keys.update(keys)
        if item.action_type in TAG_ACTIONS:
            for k in keys:
                operations.setdefault(k, []).append(item)

    selected = {}
    selected_keys = set()
    for item in files:
        fid = item.get('file_id') or item.get('id')
        if not fid:
            raise ValueError('Seleção contém arquivo sem identificador.')
        selected[fid] = item
        selected_keys.add(_identity_key(fid))
        fpath = physical_item_path(item)
        if fpath:
            selected_keys.add(_identity_key(fpath))

    # Indexed primary-key lookups in bounded groups, on a private read connection.
    descriptions = {}
    auto_counterparts = {}  # cp_key -> dict(cp, source_fids=set)
    connection = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        ids = list(selected)
        connection.execute('BEGIN')
        for offset in range(0, len(ids), 400):
            if cancelled():
                return None, 0, 0
            group = ids[offset:offset + 400]
            for row_fid, row_desc in connection.execute(
                'SELECT file_id,description FROM files WHERE file_id IN (' + ','.join('?' for _ in group) + ')', group
            ):
                descriptions[_identity_key(row_fid)] = row_desc
            # Fallback para caminhos com diferença de caixa (ex.: 'L:\...' vs 'l:\...')
            missing_canon = []
            for fid in group:
                if _identity_key(fid) not in descriptions and ntpath.isabs(fid):
                    missing_canon.append(to_canonical_id(fid))
            if missing_canon:
                for row_fid, row_desc in connection.execute(
                    'SELECT file_id,description FROM files WHERE file_id IN (' + ','.join('?' for _ in missing_canon) + ')',
                    missing_canon,
                ):
                    descriptions[_identity_key(row_fid)] = row_desc

        # Buscar automaticamente pares correspondentes (RAW ↔ JPG/PNG) para qualquer item do lote
        finder = LocalRawPairFinder(connection)
        if finder.supported:
            for fid, file_item in selected.items():
                if cancelled():
                    return None, 0, 0
                fpath = physical_item_path(file_item)
                fname = file_item.get('name', '') or ntpath.basename(fpath or fid)
                for cp in finder.find_counterparts(fid, fname, fpath):
                    cp_fid = cp['file_id']
                    cp_key = _identity_key(cp_fid)
                    cp_path_key = _identity_key(cp['path']) if cp.get('path') else ''
                    if cp_key in selected_keys or (cp_path_key and cp_path_key in selected_keys):
                        continue
                    # Pular parceiros que estejam ativos na nuvem ou marcados para exclusão
                    if cp_key in protected_keys or cp_key in deleted_keys or (
                        cp_path_key and (cp_path_key in protected_keys or cp_path_key in deleted_keys)
                    ):
                        continue
                    if cp_key not in auto_counterparts:
                        auto_counterparts[cp_key] = {'cp': cp, 'sources': set()}
                        descriptions[cp_key] = cp['description']
                    auto_counterparts[cp_key]['sources'].add(_identity_key(fid))
    finally:
        connection.close()

    affected_keys = selected_keys | set(auto_counterparts)
    # Remover itens antigos de tags apenas para arquivos afetados que NÃO sejam operações ativas de parceiros
    result = [
        item for item in prior
        if not (
            item.action_type in TAG_ACTIONS
            and item.execution_state not in ACTIVE_STATES
            and (
                _identity_key(item.file_id) in affected_keys
                or (item.path and _identity_key(item.path) in affected_keys)
            )
        )
    ]

    # Se algum item selecionado explicitamente já estiver ACTIVE, preserve comportamento seguro (erro claro)
    if selected_keys & protected_keys:
        raise ValueError('Há operações aplicadas ou incertas em um ou mais arquivos selecionados. Confira a sincronização antes de editar.')

    total_targets = len(selected) + len(auto_counterparts)
    done_count = 0
    changed_sources = set()
    changed_count = 0
    partner_count = 0

    for fid, file_item in selected.items():
        if cancelled():
            return None, 0, 0
        fkey = _identity_key(fid)
        if fkey not in descriptions:
            raise ValueError(f'Arquivo saiu do catálogo: {file_item.get("name", fid)}. Atualize a seleção e tente novamente.')
        original = descriptions[fkey] or ''
        effective = effective_description(original, operations.get(fkey, []))
        fpath = physical_item_path(file_item)
        fname = file_item.get('name', '') or ntpath.basename(fpath or fid)
        description, content_changed = _apply_tag_delta(
            effective,
            added,
            removed,
            filename_or_path=fpath or fname,
            is_counterpart=False,
            ensure_raw_if_unchanged=True,
        )
        if content_changed or description != effective:
            changed_sources.add(fkey)
        if description != original:
            result.append(StagingItem(fid, file_item.get('name', ''), fpath,
                'set_description', old_value=original, new_value=description))
            changed_count += 1
        done_count += 1
        if done_count % 100 == 0 or done_count == total_targets:
            progress(done_count, total_targets)

    # Adicionar automaticamente os pares correspondentes encontrados (JPG/PNG ↔ RAW)
    for cp_key, entry in auto_counterparts.items():
        if cancelled():
            return None, 0, 0
        cp = entry['cp']
        cp_fid = cp['file_id']
        original = descriptions.get(cp_key) or ''
        effective = effective_description(original, operations.get(cp_key, []))
        source_changed = bool(entry['sources'] & changed_sources)
        description, content_changed = _apply_tag_delta(
            effective,
            added,
            removed,
            filename_or_path=cp['path'] or cp['name'],
            is_counterpart=True,
            ensure_raw_if_unchanged=source_changed,
        )
        if description != original:
            result.append(StagingItem(cp_fid, cp['name'], cp['path'],
                'set_description', old_value=original, new_value=description))
            changed_count += 1
            partner_count += 1
        done_count += 1
        if done_count % 100 == 0 or done_count == total_targets:
            progress(done_count, total_targets)

    return [item.to_dict() for item in result], partner_count, changed_count


def prepare_batch(db_path, files, records, added, removed, cancelled, progress):
    result, _partner_count, _changed_count = prepare_batch_with_stats(
        db_path, files, records, added, removed, cancelled, progress
    )
    return result


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
        self.partner_count = 0
        self.changed_count = 0

    def run(self):
        try:
            records, partner_count, changed_count = prepare_batch_with_stats(
                *self.arguments, self.isInterruptionRequested, self.progress.emit
            )
            if records is None or self.isInterruptionRequested():
                self.cancelled.emit()
                return
            self.partner_count = partner_count
            self.changed_count = changed_count
            self.saving.emit()
            # Cancellation is no longer accepted once the atomic commit starts.
            self.store.save(records)
            self.succeeded.emit(records)
        except Exception as exc:
            self.failed.emit(str(exc))


def sync_pending_queue_raw_items(db_path, records, cancelled=None, progress=None):
    """Aplica retroativamente a regra RAW ↔ Cópias e a tag 'RAW' aos itens de tags já pendentes na fila.

    - Garante a tag 'RAW' em todos os arquivos brutos já presentes na fila (exceto se o usuário removeu
      explicitamente a tag 'RAW' naquele item).
    - Localiza pares correspondentes (RAW ↔ JPG/PNG/DNG derivado) no catálogo local:
      * se o parceiro ainda não estiver na fila, traz o parceiro para a fila com as tags sincronizadas;
      * se o parceiro já estiver na fila, soma as tags transferíveis entre ambos (preservando tags próprias,
        sem copiar nomes antigos de duplicatas e sem jamais copiar 'RAW' para JPG/PNG).
    - Nunca altera itens que estejam em confirmação na nuvem (`ACTIVE_STATES`), nem itens de mover/renomear/excluir,
      nem parceiros marcados para exclusão.

    Retorna `(new_records, stats)` ou `(None, {})` se cancelado.
    """
    cancelled = cancelled or (lambda: False)
    progress = progress or (lambda _done, _total: None)

    prior = [StagingItem.from_dict(r) if isinstance(r, dict) else r for r in records]
    protected_keys = set()
    deleted_keys = set()
    for it in prior:
        keys = {_identity_key(v) for v in (it.file_id, it.path) if v}
        if it.execution_state in ACTIVE_STATES:
            protected_keys.update(keys)
        if it.action_type == 'delete':
            deleted_keys.update(keys)

    # Agrupar itens de tags pendentes (elegíveis) por arquivo
    queued_files = {}  # key -> dict(file_id, name, path, ops=[StagingItem, ...], first_idx=int)
    key_by_item_idx = {}
    for idx, it in enumerate(prior):
        if it.action_type not in TAG_ACTIONS or it.execution_state in ACTIVE_STATES:
            continue
        keys = {_identity_key(v) for v in (it.file_id, it.path) if v}
        if keys & (protected_keys | deleted_keys):
            continue
        primary_key = _identity_key(it.file_id or it.path)
        if not primary_key:
            continue
        key_by_item_idx[idx] = primary_key
        if primary_key not in queued_files:
            queued_files[primary_key] = {
                'key': primary_key,
                'file_id': it.file_id,
                'name': it.file_name or ntpath.basename(it.path or it.file_id),
                'path': it.path or '',
                'ops': [],
                'first_idx': idx,
            }
        queued_files[primary_key]['ops'].append(it)

    empty_stats = {
        'raw_tags_added': 0,
        'partners_added': 0,
        'existing_updated': 0,
        'total_changed': 0,
        'skipped_protected': 0,
    }
    if not queued_files:
        return [it.to_dict() for it in prior], empty_stats

    db_descriptions = {}
    external_counterparts = {}  # cp_key -> dict(file_id, name, path, orig_desc)
    adjacency = {k: set() for k in queued_files}
    skipped_protected_keys = set()

    total_scan = len(queued_files)
    if db_path and Path(db_path).exists():
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            # Buscar descrições originais reais do catálogo para todos os itens de tags da fila
            fids = [info['file_id'] for info in queued_files.values() if info['file_id']]
            canon_fids = [
                to_canonical_id(info['path'] or info['file_id'])
                for info in queued_files.values()
                if ntpath.isabs(info['path'] or info['file_id'])
            ]
            lookup_ids = list(dict.fromkeys([*fids, *canon_fids]))
            for offset in range(0, len(lookup_ids), 400):
                if cancelled():
                    return None, {}
                group = lookup_ids[offset:offset + 400]
                for row_fid, row_desc in conn.execute(
                    'SELECT file_id, description FROM files WHERE file_id IN (' + ','.join('?' for _ in group) + ')',
                    group,
                ):
                    db_descriptions[_identity_key(row_fid)] = row_desc or ''

            finder = LocalRawPairFinder(conn)
            if finder.supported:
                for step, (qkey, info) in enumerate(queued_files.items(), 1):
                    if cancelled():
                        return None, {}
                    cps = finder.find_counterparts(info['file_id'], info['name'], info['path'])
                    for cp in cps:
                        cp_fid = cp['file_id']
                        cp_path = cp['path']
                        cp_keys = {_identity_key(v) for v in (cp_fid, cp_path) if v}
                        if cp_keys & (protected_keys | deleted_keys):
                            skipped_protected_keys.add(_identity_key(cp_fid or cp_path))
                            continue
                        cp_key = _identity_key(cp_fid or cp_path)
                        if cp_key == qkey:
                            continue
                        if cp_key not in queued_files and cp_key not in external_counterparts:
                            external_counterparts[cp_key] = {
                                'key': cp_key,
                                'file_id': cp_fid,
                                'name': cp['name'],
                                'path': cp_path,
                                'orig_desc': cp['description'] or '',
                            }
                            adjacency.setdefault(cp_key, set())
                        adjacency.setdefault(qkey, set()).add(cp_key)
                        adjacency.setdefault(cp_key, set()).add(qkey)
                    if step % 25 == 0 or step == total_scan:
                        progress(step, total_scan)
        finally:
            conn.close()

    # Calcular estado atual (original e efetivo) de cada membro envolvido
    member_state = {}
    for qkey, info in queued_files.items():
        orig_desc = db_descriptions.get(qkey)
        if orig_desc is None:
            orig_desc = info['ops'][0].old_value or ''
        eff_desc = effective_description(orig_desc, info['ops'])
        orig_tags = parse_tags(orig_desc)
        eff_tags = parse_tags(eff_desc)
        orig_lower = {t.lower() for t in orig_tags}
        eff_lower = {t.lower() for t in eff_tags}
        explicitly_removed_raw = bool(orig_lower & set(RAW_ONLY_TAGS_FOLDED)) and not bool(eff_lower & set(RAW_ONLY_TAGS_FOLDED))
        member_state[qkey] = {
            'queued': True,
            'file_id': info['file_id'],
            'name': info['name'],
            'path': info['path'],
            'is_raw': is_raw_filename(info['path'] or info['name']),
            'orig_desc': orig_desc,
            'eff_desc': eff_desc,
            'eff_tags': eff_tags,
            'eff_lower': eff_lower,
            'removed_lower': orig_lower - eff_lower,
            'explicitly_removed_raw': explicitly_removed_raw,
        }

    for cp_key, cp in external_counterparts.items():
        orig_desc = cp['orig_desc']
        orig_tags = parse_tags(orig_desc)
        member_state[cp_key] = {
            'queued': False,
            'file_id': cp['file_id'],
            'name': cp['name'],
            'path': cp['path'],
            'is_raw': is_raw_filename(cp['path'] or cp['name']),
            'orig_desc': orig_desc,
            'eff_desc': orig_desc,
            'eff_tags': orig_tags,
            'eff_lower': {t.lower() for t in orig_tags},
            'removed_lower': set(),
            'explicitly_removed_raw': False,
        }

    # Componentes conectados (cada grupo RAW ↔ Cópias ou RAW solitário)
    visited = set()
    updated_queued_desc = {}  # qkey -> (orig_desc, new_desc)
    new_partner_items = []
    raw_tags_added = 0
    existing_updated = 0
    partners_added = 0

    for start_key in list(member_state):
        if start_key in visited:
            continue
        comp = []
        stack = [start_key]
        visited.add(start_key)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nxt in adjacency.get(cur, ()):
                if nxt not in visited and nxt in member_state:
                    visited.add(nxt)
                    stack.append(nxt)

        queued_members = [member_state[k] for k in comp if member_state[k]['queued']]
        if not queued_members:
            continue

        # Tags removidas pelos itens em fila que NÃO permaneceram ativas em nenhum outro item da fila no grupo
        active_in_queued_lower = set()
        removed_by_queued_lower = set()
        for m in queued_members:
            active_in_queued_lower.update(m['eff_lower'])
            removed_by_queued_lower.update(m['removed_lower'])
        group_removed_lower = removed_by_queued_lower - active_in_queued_lower

        # Pool de tags transferíveis do grupo (priorizando primeiro os membros que já estão na fila)
        ordered_members = [*queued_members, *(member_state[k] for k in comp if not member_state[k]['queued'])]
        shared_pool = []
        seen_pool = set()
        for m in ordered_members:
            for tag in m['eff_tags']:
                tl = tag.lower()
                if tl in seen_pool or tl in RAW_ONLY_TAGS_FOLDED or tl in group_removed_lower or is_old_filename_tag(tag):
                    continue
                seen_pool.add(tl)
                shared_pool.append(tag)

        for k in comp:
            m = member_state[k]
            # Remove apenas tags removidas do grupo em parceiros externos; itens na fila já refletem suas próprias remoções
            removed_for_member = group_removed_lower if not m['queued'] else set()
            if m['is_raw']:
                removed_for_member = removed_for_member - set(RAW_ONLY_TAGS_FOLDED)

            had_raw_before = bool(m['eff_lower'] & set(RAW_ONLY_TAGS_FOLDED))
            allow_auto_raw = m['is_raw'] and not m['explicitly_removed_raw']

            if len(comp) == 1:
                # Arquivo solitário na fila (sem par): só acrescentar 'RAW' se for bruto
                added_for_member = []
            else:
                added_for_member = shared_pool

            new_desc, content_changed = _apply_tag_delta(
                m['eff_desc'],
                added_for_member,
                removed_for_member,
                filename_or_path=m['path'] or m['name'],
                is_counterpart=not m['queued'],
                ensure_raw_if_unchanged=allow_auto_raw,
            )
            # Se o usuário removeu 'RAW' explicitamente no arquivo da fila, não recolocar
            if m['explicitly_removed_raw'] and m['is_raw']:
                new_desc, content_changed = _apply_tag_delta(
                    m['eff_desc'],
                    added_for_member,
                    removed_for_member | set(RAW_ONLY_TAGS_FOLDED),
                    filename_or_path=m['path'] or m['name'],
                    is_counterpart=False,
                    ensure_raw_if_unchanged=False,
                )

            got_raw_now = allow_auto_raw and not had_raw_before and any(
                t.lower() in RAW_ONLY_TAGS_FOLDED for t in parse_tags(new_desc)
            )

            if m['queued']:
                if new_desc != m['eff_desc']:
                    updated_queued_desc[k] = (m['orig_desc'], new_desc)
                    if got_raw_now:
                        raw_tags_added += 1
                    if content_changed:
                        existing_updated += 1
            else:
                if new_desc != m['orig_desc']:
                    new_partner_items.append(
                        StagingItem(
                            m['file_id'],
                            m['name'],
                            m['path'],
                            'set_description',
                            old_value=m['orig_desc'],
                            new_value=new_desc,
                        )
                    )
                    partners_added += 1
                    if got_raw_now:
                        raw_tags_added += 1

    # Reconstruir a lista preservando a posição exata dos itens não alterados e dos itens atualizados
    result = []
    emitted_updated = set()
    for idx, it in enumerate(prior):
        qkey = key_by_item_idx.get(idx)
        if qkey and qkey in updated_queued_desc:
            if qkey in emitted_updated:
                continue
            emitted_updated.add(qkey)
            orig_desc, new_desc = updated_queued_desc[qkey]
            if new_desc != orig_desc:
                info = queued_files[qkey]
                result.append(
                    StagingItem(
                        info['file_id'],
                        info['name'],
                        info['path'],
                        'set_description',
                        old_value=orig_desc,
                        new_value=new_desc,
                    )
                )
        else:
            result.append(it)

    result.extend(new_partner_items)

    stats = {
        'raw_tags_added': raw_tags_added,
        'partners_added': partners_added,
        'existing_updated': existing_updated,
        'total_changed': len(updated_queued_desc) + partners_added,
        'skipped_protected': len(skipped_protected_keys),
    }
    return [it.to_dict() for it in result], stats


class QueueRawSyncWorker(QThread):
    progress = pyqtSignal(int, int)
    saving = pyqtSignal()
    succeeded = pyqtSignal(object, object)  # (records, stats)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, db_path, records, store, parent=None):
        super().__init__(parent)
        self.db_path = db_path
        self.records = records
        self.store = store

    def run(self):
        try:
            new_records, stats = sync_pending_queue_raw_items(
                self.db_path,
                self.records,
                cancelled=self.isInterruptionRequested,
                progress=self.progress.emit,
            )
            if new_records is None or self.isInterruptionRequested():
                self.cancelled.emit()
                return
            if stats.get('total_changed', 0) > 0:
                self.saving.emit()
                self.store.save(new_records)
            self.succeeded.emit(new_records, stats)
        except Exception as exc:
            self.failed.emit(str(exc))
