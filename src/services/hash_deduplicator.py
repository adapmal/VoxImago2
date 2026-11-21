"""Deduplicação de fotos por hash MD5 com herança de tags e movimentação segura para Banco_Apagado."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import sqlite3
import unicodedata
import uuid
import ntpath
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field
from PyQt6.QtCore import QObject, pyqtSignal

from src.database.database import FileIndexer, to_canonical_id
from src.drive.match import extract_drive_file_id
from src.utils.config_manager import ConfigManager
from src.utils.path_validation import validate_path_component
from src.utils.queue_store import atomic_json
from src.services.snapshot_management import assert_queue_empty, local_backup

PHOTO_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif',
    '.heic', '.heif', '.arw', '.cr2', '.nef', '.dng', '.raf', '.orf',
    '.srw', '.rw2', '.pef', '.raw',
}
VIDEO_EXTENSIONS = {
    '.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv', '.webm', '.m4v',
    '.3gp', '.mts', '.m2ts', '.ts', '.mpg', '.mpeg', '.vob',
}

MODE_ORGANIZAR_VS_ALL = 'organizar_vs_all'
MODE_CUSTOM_VS_ALL = 'custom_vs_all'
MODE_ALL_VS_ALL = 'all_vs_all'
MODE_FOLDER_VS_FOLDER = 'folder_vs_folder'
MODE_SELECTED_FOLDERS = 'selected_folders'


def parse_tags(text: str | None) -> list[str]:
    """Divide uma descrição em lista de tags únicas preservando a ordem."""
    if not text:
        return []
    raw_parts = re.split(r'[,;\n]+', str(text))
    tags: list[str] = []
    seen: set[str] = set()
    for part in raw_parts:
        clean = part.strip()
        if not clean:
            continue
        key = clean.casefold()
        if key not in seen:
            seen.add(key)
            tags.append(clean)
    return tags


_YEAR_TAG_RE = re.compile(r'(?:19|20)\d{2}')


def _extract_root_year(
    rel_path: str | None = None,
    file_path: str | None = None,
    roots: list[str] | None = None,
) -> str | None:
    """Retorna o ano (4 dígitos) da pasta raiz do arquivo, se houver."""
    parts: list[str] = []
    if file_path and roots:
        norm_file = os.path.normcase(os.path.normpath(str(file_path)))
        if any(
            norm_file == os.path.normcase(os.path.normpath(r))
            or norm_file.startswith(os.path.normcase(os.path.normpath(r)) + os.sep)
            for r in roots
        ):
            parts = _relative_parts_from_roots(str(file_path), roots)
    if not parts and rel_path:
        parts = [p for p in re.split(r'[\\/]+', str(rel_path)) if p and p != '.']
    elif not parts and file_path:
        parts = _relative_parts_from_roots(str(file_path), roots or [])
    if len(parts) > 1 and _YEAR_TAG_RE.fullmatch(parts[0].strip()):
        return parts[0].strip()
    return None


def compute_merged_tags(
    keeper_desc: str,
    keeper_name: str,
    duplicates: list[dict],
    *,
    keeper_root_year: str | int | None = None,
    keeper_path: str | None = None,
    keeper_rel_path: str | None = None,
    roots: list[str] | None = None,
    extra_tags: list[str] | tuple[str, ...] = (),
) -> str:
    """Funde as tags do arquivo que fica + tags dos duplicados + nomes diferentes com extensão.

    Quando o diretório raiz do arquivo mantido é conhecido, tags de ano (4 dígitos) vindas
    dos duplicados que não coincidem com o ano raiz do mantido não são acrescentadas.
    """
    has_keeper_context = (
        keeper_root_year is not None
        or bool(keeper_rel_path)
        or bool(keeper_path)
    )
    resolved_root_year = (
        str(keeper_root_year).strip()
        if keeper_root_year is not None
        else _extract_root_year(keeper_rel_path, keeper_path, roots)
    )

    merged = parse_tags(keeper_desc)
    seen = {t.casefold() for t in merged}
    keeper_name_cf = (keeper_name or '').strip().casefold()

    for dup in duplicates:
        for tag in parse_tags(dup.get('description', '')):
            if has_keeper_context and _YEAR_TAG_RE.fullmatch(tag):
                if resolved_root_year is None or tag != resolved_root_year:
                    continue
            key = tag.casefold()
            if key not in seen:
                seen.add(key)
                merged.append(tag)

        dup_name = (dup.get('name') or '').strip()
        if dup_name and dup_name.casefold() != keeper_name_cf:
            key = dup_name.casefold()
            if key not in seen:
                seen.add(key)
                merged.append(dup_name)

    for extra in extra_tags:
        clean = (extra or '').strip()
        if clean and clean.casefold() not in seen:
            seen.add(clean.casefold())
            merged.append(clean)

    return ', '.join(merged)


def _relative_parts_from_roots(file_path: str, roots: list[str]) -> list[str]:
    norm_file = os.path.normcase(os.path.normpath(file_path))
    for root in roots:
        norm_root = os.path.normcase(os.path.normpath(root))
        if norm_file == norm_root or norm_file.startswith(norm_root + os.sep):
            rel = os.path.relpath(file_path, root)
            return [p for p in rel.split(os.sep) if p and p != '.']
    parts = [p for p in os.path.normpath(file_path).split(os.sep) if p]
    for idx, part in enumerate(parts):
        if part.casefold() in ('banco de imagens', 'image bank'):
            return parts[idx + 1:]
    return parts


def _is_in_organizar(rel_parts: list[str]) -> bool:
    """Retorna True se alguma pasta ancestral do arquivo se chama 'Organizar'."""
    folder_parts = rel_parts[:-1] if len(rel_parts) > 1 else []
    return any(p.casefold() == 'organizar' for p in folder_parts)


_FILENAME_UPTO_YEAR_RE = re.compile(
    r'(?i)(?<![a-z0-9áàâãéêíóôõúç])at[eé][_\s.-]*(199[7-9]|20\d{2})(?!\d)'
)
_FILENAME_DELIM_YEAR_RE = re.compile(r'(?<!\d)(199[7-9]|20\d{2})(?!\d)')
_FILENAME_PACKED_DATE_RE = re.compile(
    r'(?<!\d)(199[7-9]|20\d{2})(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?=(?:\d{4,6})?(?:\D|$))'
)


def _inspect_filename_years(filename: str | None, *, max_year: int | None = None) -> tuple[set[int], set[int]]:
    """Extrai (anos_exatos, anos_ate_limite) entre 1997 e o ano atual no nome do arquivo."""
    if not filename:
        return set(), set()
    stem = unicodedata.normalize('NFC', os.path.splitext(str(filename))[0])
    upper = max_year if max_year is not None else datetime.now().year
    upto_years: set[int] = set()
    upto_spans: list[tuple[int, int]] = []
    for match in _FILENAME_UPTO_YEAR_RE.finditer(stem):
        yr = int(match.group(1))
        if 1997 <= yr <= upper:
            upto_years.add(yr)
            upto_spans.append((match.start(1), match.end(1)))

    exact_years: set[int] = set()
    for pattern in (_FILENAME_DELIM_YEAR_RE, _FILENAME_PACKED_DATE_RE):
        for match in pattern.finditer(stem):
            if any(u_start <= match.start(1) < u_end for u_start, u_end in upto_spans):
                continue
            yr = int(match.group(1))
            if 1997 <= yr <= upper:
                exact_years.add(yr)
    return exact_years, upto_years


def _extract_filename_years(filename: str | None, *, max_year: int | None = None) -> set[int]:
    """Extrai anos (entre 1997 e o ano atual) presentes no nome do arquivo."""
    exact_years, upto_years = _inspect_filename_years(filename, max_year=max_year)
    return exact_years | upto_years


def _extract_exif_year(item: dict) -> int | None:
    """Extrai o ano (4 dígitos) do Date Taken vindo do EXIF/Drive metadata."""
    explicit_year = item.get('exif_year')
    if isinstance(explicit_year, int) and 1900 <= explicit_year <= 2100:
        return explicit_year
    media = item.get('imageMediaMetadata')
    sources = [item.get(key) for key in
               ('DateTimeOriginal', 'exif_date_taken', 'DateTimeDigitized', 'DateTime')]
    sources.append(media.get('time') if isinstance(media, dict) else None)
    for raw in sources:
        if not raw:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', errors='replace')
        text = str(raw).strip().rstrip('\x00').strip()
        match = re.fullmatch(r'(\d{4})[:/\-](\d{2})[:/\-](\d{2})(.*)', text)
        if not match:
            continue
        year, month, day = map(int, match.group(1, 2, 3))
        if not 1900 <= year <= 2100:
            continue
        try:
            # Validar também calendário/horário: 0000:00:00 e datas mascaradas não são evidência.
            datetime.fromisoformat(f'{year:04d}-{month:02d}-{day:02d}' + match.group(4))
        except ValueError:
            continue
        return year
    return None


def _parse_valid_root_year(item: dict, roots: list[str] | None) -> int | None:
    """Retorna o ano válido (1900..atual) da pasta raiz do item, ou None."""
    raw = _extract_root_year(
        rel_path=item.get('rel_path'),
        file_path=item.get('path'),
        roots=roots,
    )
    if not raw:
        return None
    yr = int(raw)
    return yr if 1900 <= yr <= datetime.now().year else None


def evaluate_group_discrepancies(
    keeper: dict,
    members: list[dict],
    roots: list[str] | None,
) -> dict:
    """Detecta discrepâncias de DateTaken e de ano no nome em relação à pasta raiz do preservado."""
    keeper_year = _parse_valid_root_year(keeper, roots)
    if keeper_year is None:
        return {
            'keeper_root_year': None,
            'datetaken_discrepancy': False,
            'name_year_discrepancy': False,
            'extra_tags': [],
            'reasons': [],
        }

    bad_exif = sorted({
        yr for m in members
        if (yr := _extract_exif_year(m)) is not None and yr != keeper_year
    })

    exact_years: set[int] = set()
    upto_years: set[int] = set()
    for m in members:
        ex, up = _inspect_filename_years(m.get('name'))
        exact_years.update(ex)
        upto_years.update(up)

    bad_exact = sorted(yr for yr in exact_years if yr != keeper_year)
    bad_upto = sorted(yr for yr in upto_years if keeper_year > yr)

    datetaken_disc = bool(bad_exif)
    name_disc = bool(bad_exact or bad_upto)
    extra_tags: list[str] = []
    reasons: list[str] = []
    if datetaken_disc:
        extra_tags.append('DateTaken')
        reasons.append(
            f"DateTaken ({', '.join(str(y) for y in bad_exif)}) ≠ pasta {keeper_year} (+tag DateTaken)"
        )
    if name_disc:
        extra_tags.append('discrepância')
        parts = []
        if bad_exact:
            parts.append('nome ' + ', '.join(str(y) for y in bad_exact))
        if bad_upto:
            parts.append('até ' + ', '.join(str(y) for y in bad_upto))
        reasons.append(f"{'; '.join(parts)} ≠ pasta {keeper_year} (+tag discrepância)")
    return {
        'keeper_root_year': keeper_year,
        'datetaken_discrepancy': datetaken_disc,
        'name_year_discrepancy': name_disc,
        'extra_tags': extra_tags,
        'reasons': reasons,
    }


def select_keeper_and_duplicates(
    group: list[dict],
    roots: list[str],
    *,
    context_items: list[dict] | None = None,
) -> tuple[dict, list[dict]]:
    """Prioriza sempre a pasta raiz mais antiga; desempates em cadeia padrão."""
    del context_items
    enriched = []
    for item in group:
        rel_parts = _relative_parts_from_roots(item['path'], roots)
        in_org = 1 if _is_in_organizar(rel_parts) else 0
        depth = max(0, len(rel_parts) - 1)  # nº de subpastas abaixo da raiz
        root_year_num = _parse_valid_root_year(item, roots)
        exif_year = _extract_exif_year(item)
        tag_cnt = len(parse_tags(item.get('description', '')))
        item_copy = dict(item)
        item_copy['rel_path'] = os.path.join(*rel_parts) if rel_parts else item['name']
        item_copy['in_organizar'] = bool(in_org)
        item_copy['depth'] = depth
        if exif_year is not None:
            item_copy['exif_year'] = exif_year
        sort_key = (
            (0, root_year_num) if root_year_num is not None else (1, 0),
            in_org,
            depth,
            -tag_cnt,
            len(item['path']),
            item.get('drive_created') or str(item.get('createdTime') or ''),
            item['path'].casefold(),
        )
        enriched.append((sort_key, item_copy))

    enriched.sort(key=lambda pair: pair[0])
    keeper = enriched[0][1]
    duplicates = [pair[1] for pair in enriched[1:]]
    return keeper, duplicates


def default_banco_apagado_path(config_mgr: ConfigManager | None = None) -> str:
    cfg = config_mgr or ConfigManager()
    return ntpath.join(archive_base(cfg), 'Banco_Apagado')


def archive_base(cfg):
    base = cfg.get_sandbox_path()
    try:
        roots = catalog_roots(cfg)
        base = ntpath.join(ntpath.dirname(roots[0]), ntpath.basename(base))
    except ValueError:
        pass
    return allowed_path(base, cfg)


@dataclass
class DuplicateGroup:
    md5: str
    size: int
    keeper: dict
    duplicates: list[dict] = field(default_factory=list)
    merged_description: str = ''
    checked: bool = True
    eligible_ids: set[str] = field(default_factory=set)
    scope_mode: str = MODE_ALL_VS_ALL
    source_folders: list[str] = field(default_factory=list)
    reference_folders: list[str] = field(default_factory=list)
    edited_description: str | None = None
    target_year: int | None = None
    year_source: str = ''
    plan_error: str = ''
    plan_root: str = ''
    year_context: list[dict] = field(default_factory=list)

    @property
    def relocation_path(self):
        return ''

    @property
    def discrepancy_info(self) -> dict:
        roots = [self.plan_root] if self.plan_root else None
        context = self.year_context or [self.keeper, *self.duplicates]
        return evaluate_group_discrepancies(self.keeper, context, roots)

    @property
    def has_discrepancy(self) -> bool:
        return bool(self.discrepancy_info['extra_tags'])

    @property
    def discrepancy_reasons(self) -> list[str]:
        return self.discrepancy_info['reasons']

    @property
    def proposed_description(self):
        return self.merged_description if self.edited_description is None else self.edited_description

    def recompute_merged(self):
        info = self.discrepancy_info
        self.target_year = info['keeper_root_year']
        self.year_source = 'pasta' if info['keeper_root_year'] is not None else ''
        self.merged_description = compute_merged_tags(
            self.keeper.get('description', ''),
            self.keeper.get('name', ''),
            self.duplicates,
            keeper_rel_path=self.keeper.get('rel_path'),
            keeper_path=self.keeper.get('path'),
            roots=[self.plan_root] if self.plan_root else None,
            extra_tags=info['extra_tags'],
        )


FOLDER_MIME = 'application/vnd.google-apps.folder'
META_FIELDS = ('id,name,mimeType,parents,driveId,trashed,size,md5Checksum,sha256Checksum,'
               'description,createdTime,version,capabilities,imageMediaMetadata(time)')
CONDITIONAL_FIELDS = 'id,etag,version,title,parents(id),driveId,labels(trashed),fileSize,md5Checksum,description'


def conditional_service(service):
    """Cliente v2 somente para obter ETag e escrever na mesma representação.

    Reutiliza o transporte privado do worker, nunca o cliente da UI.
    """
    http = getattr(service, '_http', None)
    if http is None:
        raise ValueError('Não foi possível obter o ETag pela API de compatibilidade. Nenhuma escrita autorizada.')
    from googleapiclient.discovery import build
    return build('drive', 'v2', http=http, cache_discovery=False, static_discovery=True)


def private_service(service):
    """Não compartilhar httplib2.Http entre a UI e workers Qt."""
    http = getattr(service, '_http', None)
    credentials = getattr(http, 'credentials', None)
    if credentials is None:
        if http is not None:
            raise ValueError('Não foi possível criar uma conexão isolada; refaça o login no Drive.')
        return service  # Transportes injetados nos testes.
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    return build('drive', 'v3', http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=30)), cache_discovery=False)


def norm(path):
    return ntpath.normcase(ntpath.normpath(path))


def under(path, root):
    return norm(path) == norm(root) or norm(path).startswith(norm(root).rstrip('\\') + '\\')


def allowed_path(path, cfg):
    if not ntpath.isabs(path) or '..' in path.replace('/', '\\').split('\\'):
        raise ValueError('Informe um caminho absoluto sem "..".')
    excluded = set(cfg.get_excluded_drive_letters()) | {'O:'}
    if ntpath.splitdrive(path)[0].upper() in excluded:
        raise ValueError('Unidade excluída: ' + path)
    return path


def catalog_roots(cfg):
    # Somente configuração; não procurar unidades ou abrir mídias online.
    roots = []
    for key in ('scan_paths', 'scan_folders', 'local_folders'):
        values = cfg.get(key, []) or []
        for path in ([values] if isinstance(values, str) else values):
            allowed_path(path, cfg)
            parts = ntpath.normpath(path).split('\\')
            anchors = [i for i, part in enumerate(parts) if part.casefold() in ('banco de imagens', 'image bank')]
            if anchors:
                root = '\\'.join(parts[:anchors[-1] + 1])
                if norm(root) not in {norm(r) for r in roots}:
                    roots.append(root)
    if len(roots) != 1:
        raise ValueError('Configure uma única raiz local do Banco de Imagens para esta análise.')
    return roots


def relative_parts(path, root):
    if not under(path, root) or norm(path) == norm(root):
        raise ValueError('Arquivo fora do Banco de Imagens autorizado: ' + path)
    parts = ntpath.relpath(path, root).split('\\')
    for part in parts:
        if not part or part in ('.', '..'):
            raise ValueError('Caminho relativo inválido.')
    return parts


def validate_scope(mode, folders, references, roots, cfg):
    if mode not in (MODE_ALL_VS_ALL, MODE_ORGANIZAR_VS_ALL, MODE_CUSTOM_VS_ALL, MODE_FOLDER_VS_FOLDER, MODE_SELECTED_FOLDERS):
        raise ValueError('Escopo inválido.')
    for path in [*folders, *references]:
        allowed_path(path, cfg)
        if not under(path, roots[0]):
            raise ValueError('Pasta fora do Banco de Imagens: ' + path)
    if mode in (MODE_CUSTOM_VS_ALL, MODE_FOLDER_VS_FOLDER) and not folders:
        raise ValueError('Escolha a pasta de origem.')
    if mode == MODE_SELECTED_FOLDERS:
        if len({norm(path) for path in folders}) < 2 or references:
            raise ValueError('Escolha ao menos duas pastas distintas para comparar entre si.')
    if mode == MODE_FOLDER_VS_FOLDER:
        if len(folders) != 1 or len(references) != 1:
            raise ValueError('Escolha exatamente uma pasta de origem e uma pasta de comparação.')
        if under(folders[0], references[0]) or under(references[0], folders[0]):
            raise ValueError('As duas pastas devem ser distintas e não podem conter uma à outra.')


def eligible(item, mode, folders, roots):
    if mode == MODE_ALL_VS_ALL:
        return True
    if mode == MODE_ORGANIZAR_VS_ALL:
        return _is_in_organizar(relative_parts(item['path'], roots[0]))
    return any(under(item['path'], folder) for folder in folders)


def escape_query(text):
    return text.replace('\\', '\\\\').replace("'", "\\'")


class DedupCancelled(RuntimeError):
    """Pedido explícito de parada, distinto de falha da API."""


class DriveCatalog:
    """Paginação completa e resolução por hierarquia, nunca busca global por nome."""
    def __init__(self, service, drive_id, cancelled=lambda: False, progress=None):
        self.service, self.drive_id, self.cancelled = service, drive_id, cancelled
        self.children = defaultdict(list)
        self.progress = progress
        self._loaded_parents = set()
        self._located_parents = set()
        self._parent_items = {}
        self._received = 0
        self._conditional_service = None

    def list(self, query, fields=META_FIELDS, drive_id=None):
        result, token, seen = [], None, set()
        while True:
            if self.cancelled():
                raise DedupCancelled('Operação cancelada.')
            kwargs = dict(q=query, corpora='drive', driveId=drive_id or self.drive_id,
                          supportsAllDrives=True, includeItemsFromAllDrives=True, pageSize=1000,
                          fields=f'nextPageToken,incompleteSearch,files({fields})')
            if token:
                kwargs['pageToken'] = token
            page = self.service.files().list(**kwargs).execute()
            if page.get('incompleteSearch'):
                raise RuntimeError('Busca incompleta do Drive; não é seguro escolher candidatos.')
            result.extend(page.get('files', []))
            self._received += len(page.get('files', []))
            if self.progress:
                self.progress(self._received)
            token = page.get('nextPageToken')
            if not token:
                return result
            if token in seen:
                raise RuntimeError('Paginação repetida; análise cancelada.')
            seen.add(token)

    def load(self):
        for item in self.list('trashed = false'):
            for parent in item.get('parents', []):
                self.children[(parent, item['name'].casefold())].append(item)

    def load_children(self, parent, *, metadata=True):
        """Lista uma pasta, reutilizando páginas já carregadas neste levantamento."""
        if parent in self._loaded_parents or (not metadata and parent in self._located_parents):
            return
        fields = META_FIELDS if metadata else 'id,name,mimeType,parents'
        items = self.list(f"'{escape_query(parent)}' in parents and trashed = false", fields=fields)
        for old in self._parent_items.get(parent, []):
            self.children.pop((parent, old['name'].casefold()), None)
        for item in items:
            self.children[(parent, item['name'].casefold())].append(item)
        self._parent_items[parent] = items
        self._located_parents.add(parent)
        if metadata:
            self._loaded_parents.add(parent)

    def load_selected(self, folder_paths):
        """Resolve ancestrais por nomes e percorre apenas as árvores selecionadas."""
        selected_ids = []
        for parts in folder_paths:
            parent = self.drive_id
            for name in parts:
                self.load_children(parent, metadata=False)
                found = self.children.get((parent, name.casefold()), [])
                if len(found) != 1 or found[0].get('mimeType') != FOLDER_MIME:
                    raise ValueError('Pasta selecionada inexistente ou ambígua no Drive: ' + '\\'.join(parts))
                parent = found[0]['id']
            selected_ids.append(parent)
        pending, visited = list(selected_ids), set()
        while pending:
            if self.cancelled():
                raise RuntimeError('Operação cancelada.')
            parent = pending.pop()
            if parent in visited:
                continue
            visited.add(parent)
            self.load_children(parent)
            pending.extend(item['id'] for item in self._parent_items[parent]
                           if item.get('mimeType') == FOLDER_MIME)

    def resolve(self, parts):
        parent = self.drive_id
        for index, name in enumerate(parts):
            found = self.children.get((parent, name.casefold()), [])
            if len(found) != 1:
                raise ValueError('Caminho inexistente ou ambíguo no Drive: ' + '\\'.join(parts))
            item = found[0]
            if index < len(parts) - 1 and item.get('mimeType') != FOLDER_MIME:
                raise ValueError('Um componente da hierarquia não é uma pasta.')
            parent = item['id']
        return item

    def get(self, file_id):
        request = self.service.files().get(fileId=file_id, supportsAllDrives=True, fields=META_FIELDS)
        headers = {}
        if hasattr(request, 'add_response_callback'):
            request.add_response_callback(lambda response: headers.update(response))
        item = request.execute()
        item['_etag'] = headers.get('etag') or headers.get('ETag')
        return item

    def prepare_write(self, item):
        if item.get('_etag'):
            return item
        if self._conditional_service is None:
            self._conditional_service = conditional_service(self.service)
        legacy = self._conditional_service.files().get(
            fileId=item['id'], supportsAllDrives=True, fields=CONDITIONAL_FIELDS).execute()
        comparable = dict(id=legacy.get('id'), version=legacy.get('version'), name=legacy.get('title'),
                          parents=sorted(p['id'] for p in legacy.get('parents', [])),
                          driveId=legacy.get('driveId'), trashed=legacy.get('labels', {}).get('trashed', False),
                          size=str(legacy.get('fileSize', '')), md5Checksum=legacy.get('md5Checksum'),
                          description=legacy.get('description', ''))
        expected = {key: item.get(key) for key in comparable}
        expected.update(parents=sorted(item.get('parents', [])), size=str(item.get('size', '')),
                        description=item.get('description', ''), trashed=item.get('trashed', False))
        if not legacy.get('version') or not item.get('version') or comparable != expected:
            raise ValueError('Arquivo mudou entre as consultas de segurança; refaça a análise.')
        etag = legacy.get('etag')
        if not isinstance(etag, str) or not etag.strip() or etag == '*':
            raise ValueError('Drive não forneceu ETag nem pela API de compatibilidade; grupo bloqueado sem alterações.')
        return dict(item, _etag=etag, _write_api='v2')

    def update(self, item, **kwargs):
        guarded = self.prepare_write(item)
        if guarded.get('_write_api') == 'v2':
            body = dict(kwargs.pop('body', {}))
            if 'name' in body:
                body['title'] = body.pop('name')
            request = self._conditional_service.files().patch(
                fileId=item['id'], supportsAllDrives=True, fields='id', body=body, **kwargs)
        else:
            request = self.service.files().update(fileId=item['id'], supportsAllDrives=True,
                                                   fields=META_FIELDS, **kwargs)
        request.headers['If-Match'] = guarded['_etag']
        return request.execute()  # Não repetir automaticamente após timeout.


def form_groups(items, mode, folders, references, roots):
    buckets = defaultdict(list)
    for item in items:
        if mode == MODE_SELECTED_FOLDERS and not eligible(item, mode, folders, roots):
            continue
        buckets[(item['size'], item['md5Checksum'])].append(item)
    # Descarta chaves sem duplicatas antes de construir os grupos para reduzir pico de RAM.
    dup_buckets = [(k, m) for k, m in buckets.items() if len(m) >= 2]
    buckets.clear()
    groups = []
    for (size, checksum), members in dup_buckets:
        sha = {item.get('sha256Checksum') for item in members if item.get('sha256Checksum')}
        if len(sha) > 1:
            continue  # Mesmo MD5, mas conteúdo divergente por SHA-256.
        sources = [item for item in members if eligible(item, mode, folders, roots)]
        if not sources:
            continue
        if mode == MODE_FOLDER_VS_FOLDER:
            protected = [item for item in members if any(under(item['path'], ref) for ref in references)]
            if not protected:
                continue
        else:
            protected = [item for item in members if item not in sources]
        keeper, _ = select_keeper_and_duplicates(protected or sources, roots, context_items=members)
        duplicates = [dict(item) for item in sources if item['drive_id'] != keeper['drive_id']]
        if not duplicates:
            continue
        for item in [keeper, *duplicates]:
            item['rel_path'] = '\\'.join(relative_parts(item['path'], roots[0]))
            item['depth'] = len(relative_parts(item['path'], roots[0])) - 1
        group = DuplicateGroup(checksum, size, keeper, duplicates,
                               eligible_ids={item['drive_id'] for item in sources},
                               scope_mode=mode, source_folders=list(folders), reference_folders=list(references))
        group.plan_root = roots[0]
        group.year_context = members
        group.recompute_merged()
        groups.append(group)
    return groups


class HashScanWorker(QObject):
    progress_update = pyqtSignal(int, str)
    scan_finished = pyqtSignal(list)
    scan_failed = pyqtSignal(str)

    def __init__(self, db_name, scope_mode, custom_folders=None, include_videos=False,
                 reference_folders=None, drive_service=None):
        super().__init__()
        self.db_name, self.scope_mode = db_name, scope_mode
        self.custom_folders, self.reference_folders = custom_folders or [], reference_folders or []
        self.include_videos, self.drive_service = include_videos, drive_service
        self._cancelled = False
        self.skipped = []

    def cancel(self):
        self._cancelled = True

    def run(self):
        conn = None
        try:
            if not self.drive_service:
                raise ValueError('Conecte-se ao Drive. A análise usa checksums da nuvem, sem baixar as mídias.')
            cfg = ConfigManager()
            if cfg.is_sandbox():
                raise ValueError('Use o modo Produção para analisar o Banco de Imagens.')
            self.drive_service = private_service(self.drive_service)
            roots = catalog_roots(cfg)
            validate_scope(self.scope_mode, self.custom_folders, self.reference_folders, roots, cfg)
            cloud = DriveCatalog(self.drive_service, cfg.get('production_drive_id'), lambda: self._cancelled,
                lambda count: self.progress_update.emit(2, f'Consultando metadados: {count:,} itens (sem baixar originais)...'))
            self.progress_update.emit(1, 'Consultando metadados/checksums no Drive (sem baixar fotos)...')
            if self.scope_mode == MODE_SELECTED_FOLDERS:
                cloud.load_selected([[] if norm(folder) == norm(roots[0]) else relative_parts(folder, roots[0])
                                     for folder in self.custom_folders])
            else:
                cloud.load()
            conn = sqlite3.connect(Path(self.db_name).resolve().as_uri() + '?mode=ro', uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT file_id,name,path,size,webContentLink,modifiedTime,thumbnailRotation,thumbnailPath "
                "FROM files WHERE source='local' AND path IS NOT NULL"
            ).fetchall()
            allowed_ext = PHOTO_EXTENSIONS | (VIDEO_EXTENSIONS if self.include_videos else set())
            items, seen_ids = [], set()
            for position, row in enumerate(rows):
                if self._cancelled:
                    raise RuntimeError('Busca cancelada pelo usuário.')
                item = dict(row)
                path = item['path']
                if not under(path, roots[0]) or ntpath.splitext(item['name'] or '')[1].lower() not in allowed_ext:
                    continue
                if self.scope_mode in (MODE_FOLDER_VS_FOLDER, MODE_SELECTED_FOLDERS) and not any(
                        under(path, folder) for folder in self.custom_folders + self.reference_folders):
                    continue
                try:
                    meta = cloud.resolve(relative_parts(path, roots[0]))
                    if meta.get('mimeType') == FOLDER_MIME or meta.get('trashed'):
                        continue
                    size, checksum = int(meta.get('size') or 0), meta.get('md5Checksum')
                    if not size or not checksum:
                        raise ValueError('Checksum/tamanho indisponível no Drive.')
                    if size != int(item.get('size') or 0):
                        raise ValueError('Tamanho do índice local diverge do Drive; sincronize e analise novamente.')
                    linked_id = extract_drive_file_id(item.get('webContentLink') or '')
                    if linked_id and linked_id != meta['id']:
                        raise ValueError('Vínculo local aponta para outro ID; revalide o vínculo.')
                    if meta['id'] in seen_ids:
                        continue
                    seen_ids.add(meta['id'])
                    item.update(name=meta['name'], size=size, description=meta.get('description', ''),
                                md5Checksum=checksum, sha256Checksum=meta.get('sha256Checksum'),
                                drive_id=meta['id'], drive_parent=meta.get('parents', []),
                                createdTime=0, drive_created=meta.get('createdTime', ''),
                                imageMediaMetadata=meta.get('imageMediaMetadata') or {},
                                mimeType=meta.get('mimeType'), source='local')
                    items.append(item)
                except ValueError as exc:
                    self.skipped.append({'path': path, 'reason': str(exc)})
                if position % 100 == 0:
                    self.progress_update.emit(10 + int(85 * position / max(1, len(rows))),
                                              f'Conferindo caminhos: {position:,}/{len(rows):,}; ignorados: {len(self.skipped):,}')
            cloud.children.clear()
            cloud._parent_items.clear()
            rows.clear()
            groups = form_groups(items, self.scope_mode, self.custom_folders, self.reference_folders, roots)
            self.progress_update.emit(100, f'{len(groups)} grupos; {len(self.skipped)} arquivos ignorados por segurança. Nenhum original baixado.')
            self.scan_finished.emit(groups)
        except Exception as exc:
            logging.exception('Análise de duplicatas interrompida')
            self.scan_failed.emit(str(exc))
        finally:
            if conn is not None:
                conn.close()


class DedupJournal:
    """Registro durável anterior a cada efeito externo; resultados incertos nunca repetem movimentos."""
    def __init__(self, db_name):
        self.folder = Path(db_name).resolve().parent / 'dedup'
        self.folder.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.folder / 'journal.db'), timeout=10)
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, run TEXT, event TEXT, payload TEXT)')
        self.conn.execute('CREATE TABLE IF NOT EXISTS moves (file_id TEXT PRIMARY KEY, state TEXT, payload TEXT)')
        self.conn.commit()
        self.run_id = uuid.uuid4().hex

    def event(self, event, payload):
        self.conn.execute('INSERT INTO events(run,event,payload) VALUES(?,?,?)',
                          (self.run_id, event, json.dumps(payload, ensure_ascii=False)))
        self.conn.commit()

    def begin_move(self, file_id, payload):
        if self.conn.execute('SELECT 1 FROM moves WHERE file_id=?', (file_id,)).fetchone():
            raise ValueError('Arquivo já consta no histórico de deduplicação; confira o relatório antes de repetir.')
        self.conn.execute('INSERT INTO moves VALUES(?,?,?)', (file_id, 'uncertain', json.dumps(payload, ensure_ascii=False)))
        self.conn.commit()
        self.event('move_requested', payload)

    def state(self, file_id, state):
        self.conn.execute('UPDATE moves SET state=? WHERE file_id=?', (state, file_id))
        self.conn.commit()

    def report(self, result):
        result = dict(result, events=[{'event': e, 'data': json.loads(p)} for e, p in
            self.conn.execute('SELECT event,payload FROM events WHERE run=? ORDER BY seq', (self.run_id,))])
        path = self.folder / ('dedup_log_' + self.run_id + '.json')
        atomic_json(path, result)
        return str(path)

    def close(self):
        self.conn.close()


def assert_metadata(item, fresh, checksum, size, source_drive):
    if (fresh.get('trashed') or fresh.get('driveId') != source_drive or
            fresh.get('id') != item['drive_id'] or fresh.get('name') != item['name'] or
            fresh.get('parents') != item['drive_parent'] or
            fresh.get('md5Checksum') != checksum or int(fresh.get('size') or 0) != size or
            (item.get('sha256Checksum') and fresh.get('sha256Checksum') != item['sha256Checksum'])):
        raise ValueError('Arquivo mudou de conteúdo, nome ou pasta; refaça a análise: ' + item['path'])


def metadata_stable(before, after):
    return all(before.get(key) == after.get(key) for key in
               ('id', 'version', 'name', 'parents', 'driveId', 'md5Checksum', 'sha256Checksum', 'size', 'description', 'trashed', 'imageMediaMetadata'))


def relocate_local(indexer, payload):
    """Rebase transacional do índice, sem tocar no disco sincronizado. Idempotente."""
    old_id = to_canonical_id(payload['local_id'])
    new_id = to_canonical_id(payload['destination_path'])
    conn = indexer.conn
    conn.execute('BEGIN IMMEDIATE')
    try:
        old = conn.execute('SELECT file_id FROM files WHERE file_id=?', (old_id,)).fetchone()
        dest = conn.execute('SELECT webContentLink FROM files WHERE file_id=?', (new_id,)).fetchone()
        if dest:
            if old or extract_drive_file_id(dest[0] or '') != payload['file_id']:
                raise ValueError('Conflito no índice de destino; revisão necessária.')
        elif not old:
            raise ValueError('Registro local do preservado ausente; revisão necessária.')
        else:
            conn.execute('UPDATE files SET file_id=?,path=?,parentId=? WHERE file_id=?',
                         (new_id, new_id, to_canonical_id(ntpath.dirname(new_id)), old_id))
            conn.execute('UPDATE search_index SET file_id=? WHERE file_id=?', (new_id, old_id))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='drive_link_validation'").fetchone():
            conn.execute('DELETE FROM drive_link_validation WHERE local_file_id=? OR drive_id=?',
                         (old_id, payload['file_id']))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def clean_local(indexer, payload):
    # Nunca operar por nome; remover somente os IDs registrados após confirmação remota.
    ids = {payload['local_id'], to_canonical_id(payload['local_id']), payload['file_id']}
    indexer.conn.execute('BEGIN IMMEDIATE')
    try:
        for fid in ids:
            indexer.conn.execute('DELETE FROM files WHERE file_id=?', (fid,))
            indexer.conn.execute('DELETE FROM search_index WHERE file_id=?', (fid,))
            if indexer.conn.execute("SELECT 1 FROM sqlite_master WHERE name='drive_link_validation'").fetchone():
                indexer.conn.execute('DELETE FROM drive_link_validation WHERE local_file_id=? OR drive_id=?',
                                     (fid, payload['file_id']))
        indexer.conn.commit()
    except Exception:
        indexer.conn.rollback()
        raise


class HashDedupExecuteWorker(QObject):
    progress_update = pyqtSignal(int, str)
    execution_finished = pyqtSignal(dict)
    execution_failed = pyqtSignal(str)

    def __init__(self, db_name, groups, dest_root, drive_service, recover_only=False):
        super().__init__()
        self.db_name = db_name
        self.groups = deepcopy([g for g in groups if g.checked and g.duplicates])
        self.dest_root, self.drive_service = ntpath.normpath(dest_root), drive_service
        self.recover_only = recover_only
        self._cancelled = False
        self._folder_cache = {}

    def cancel(self):
        logging.info('Deduplicação: pedido de cancelamento recebido pelo executor.')
        self._cancelled = True

    def folder(self, parent, name, dest_drive, journal):
        valid, reason = validate_path_component(name, 'pasta de destino')
        if not valid:
            raise ValueError(reason)
        query = f"'{escape_query(parent)}' in parents and trashed = false"
        found = [item for item in self.cloud.list(query, drive_id=dest_drive)
                 if item.get('name', '').casefold() == name.casefold()]
        if len(found) > 1 or (found and found[0].get('mimeType') != FOLDER_MIME):
            raise ValueError('Pasta de destino ambígua ou ocupada por um arquivo: ' + name)
        if found:
            result = found[0]['id']
        else:
            journal.event('folder_create_requested', {'parent': parent, 'name': name})
            result = self.drive_service.files().create(body={'name': name, 'mimeType': FOLDER_MIME, 'parents': [parent]},
                                                       supportsAllDrives=True, fields='id').execute()['id']
            journal.event('folder_created', {'parent': parent, 'name': name, 'id': result})
        fresh = self.cloud.get(result)
        if fresh.get('trashed') or fresh.get('driveId') != dest_drive or not fresh.get('capabilities', {}).get('canAddChildren'):
            raise ValueError('Sem permissão para adicionar arquivos à pasta de destino.')
        return result

    def destination_segments(self, cfg):
        base = archive_base(cfg)
        allowed_path(self.dest_root, cfg)
        if not under(self.dest_root, base) or norm(self.dest_root) == norm(base):
            raise ValueError('O destino deve estar dentro de _TestesBanco, em Banco_Apagado ou subpasta.')
        segments = ntpath.relpath(self.dest_root, base).split('\\')
        if segments[0].casefold() != 'banco_apagado':
            raise ValueError('O destino autorizado começa em _TestesBanco\\Banco_Apagado.')
        for segment in segments:
            valid, reason = validate_path_component(segment)
            if not valid:
                raise ValueError(reason)
        return segments

    def destination(self, rel_parts, cfg, journal):
        segments = self.destination_segments(cfg)
        parent = cfg.get('sandbox_drive_id')
        for part in segments + rel_parts:
            parent = self.folder(parent, part, cfg.get('sandbox_drive_id'), journal)
        return parent

    def unique_name(self, parent, name, dest_drive, file_id):
        valid, reason = validate_path_component(name, 'nome do arquivo')
        if not valid:
            raise ValueError(reason)
        names = {item.get('name', '').casefold() for item in
                 self.cloud.list(f"'{escape_query(parent)}' in parents and trashed = false", drive_id=dest_drive)}
        if name.casefold() not in names:
            return name
        stem, ext = ntpath.splitext(name)
        # Sufixo determinístico por ID; não depender apenas de uma sequência local.
        candidate = stem[:180] + '_dup_' + file_id + ext
        valid, reason = validate_path_component(candidate, 'nome de arquivamento')
        if not valid or candidate.casefold() in names:
            raise ValueError('Colisão no destino; revisão manual necessária. ' + reason)
        return candidate

    def recover(self, indexer, journal):
        recovered = 0
        unresolved = 0
        for file_id, state, raw in journal.conn.execute("SELECT file_id,state,payload FROM moves WHERE state!='cleaned'").fetchall():
            payload = json.loads(raw)
            fresh = self.cloud.get(file_id)
            if (not fresh.get('trashed') and fresh.get('parents') == [payload['destination_parent']]
                    and fresh.get('driveId') == payload['destination_drive']
                    and fresh.get('md5Checksum') == payload['md5'] and int(fresh.get('size') or 0) == payload['size']
                    and fresh.get('name') == payload['destination_name']
                    and (payload.get('kind') != 'keeper_relocation'
                         or fresh.get('description', '') == payload['description'])):
                journal.state(file_id, 'confirmed')
                if payload.get('kind') == 'keeper_relocation':
                    relocate_local(indexer, payload)
                else:
                    clean_local(indexer, payload)
                journal.state(file_id, 'cleaned')
                journal.event('reconciled', payload)
                recovered += 1
            else:
                journal.event('manual_review_required', payload)
                unresolved += 1
        return recovered, unresolved

    def check_keeper_destination(self, parent, name, cfg, keeper_id):
        entries = self.cloud.list(f"'{escape_query(parent)}' in parents and trashed = false",
                                  drive_id=cfg.get('production_drive_id'))
        if any(e.get('name', '').casefold() == name.casefold() and e['id'] != keeper_id for e in entries):
            raise ValueError('Nome já ocupado na pasta anual; grupo bloqueado, sem sobrescrever.')

    def prepare_keeper_relocation(self, grp, fresh, indexer, journal, roots, cfg):
        target = grp.relocation_path
        if not target:
            return None
        if grp.keeper['drive_id'] not in grp.eligible_ids or not eligible(
                grp.keeper, grp.scope_mode, grp.source_folders, roots):
            raise ValueError('Realocação do preservado fora do escopo autorizado.')
        expected = ntpath.join(roots[0], str(grp.target_year), 'Organizar', fresh['name'])
        if norm(target) != norm(expected):
            raise ValueError('Destino anual inválido; refaça a análise.')
        allowed_path(target, cfg)
        valid, reason = validate_path_component(fresh['name'])
        if not valid:
            raise ValueError(reason)
        if not fresh.get('capabilities', {}).get('canMoveItemWithinDrive'):
            raise ValueError('Sem permissão para realocar o preservado dentro do Drive.')
        if journal.conn.execute('SELECT 1 FROM moves WHERE file_id=?', (fresh['id'],)).fetchone():
            raise ValueError('Preservado com movimento anterior registrado; confira as operações interrompidas.')
        if indexer.conn.execute('SELECT 1 FROM files WHERE file_id=? OR path=?',
                                (to_canonical_id(target), to_canonical_id(target))).fetchone():
            raise ValueError('Caminho anual já ocupado no índice local; revisão necessária.')
        parent = cfg.get('production_drive_id')
        for part in (str(grp.target_year), 'Organizar'):
            parent = self.folder(parent, part, cfg.get('production_drive_id'), journal)
        self.check_keeper_destination(parent, fresh['name'], cfg, fresh['id'])
        return dict(kind='keeper_relocation', file_id=fresh['id'], local_id=grp.keeper['file_id'],
                    origin_path=grp.keeper['path'], original_parents=fresh['parents'], original_name=fresh['name'],
                    destination_parent=parent, destination_drive=cfg.get('production_drive_id'),
                    destination_name=fresh['name'], destination_path=target, md5=grp.md5, size=grp.size)

    def run(self):
        indexer, journal = None, None
        result = dict(tags_updated=0, files_moved=0, keepers_relocated=0, errors=[], cancelled=False,
                      completed_groups=[], completed_moves=[], review_groups={}, keeper_updates={}, notes=[])
        self.last_result = result
        try:
            cfg = ConfigManager()
            if not self.drive_service or cfg.is_read_only() or cfg.is_sandbox():
                raise ValueError('Execução requer conexão ao Drive, modo Produção e permissão de escrita.')
            self.drive_service = private_service(self.drive_service)
            if cfg.get('production_drive_id') == cfg.get('sandbox_drive_id'):
                raise ValueError('Origem e destino precisam ser Drives diferentes.')
            if not cfg.get('production_drive_id') or not cfg.get('sandbox_drive_id'):
                raise ValueError('Configure os IDs dos dois Drives.')
            self.destination_segments(cfg)
            assert_queue_empty(self.db_name)
            backup = local_backup(self.db_name)
            journal = DedupJournal(self.db_name)
            journal.event('started', {'backup': backup, 'destination': self.dest_root})
            indexer = FileIndexer(self.db_name)
            roots = catalog_roots(cfg)
            self.cloud = DriveCatalog(self.drive_service, cfg.get('production_drive_id'), lambda: self._cancelled)
            if self.recover_only:
                result['reconciled'], unresolved = self.recover(indexer, journal)
                if unresolved:
                    result['errors'].append(f'{unresolved} operação(ões) permanecem bloqueadas para revisão manual; consulte o diário.')
            else:
                for position, grp in enumerate(self.groups):
                    if self._cancelled:
                        break
                    self.progress_update.emit(int(100 * position / max(1, len(self.groups))),
                                              f'Conferindo grupo {position + 1}/{len(self.groups)}...')
                    try:
                        complete = self.execute_group(grp, indexer, journal, roots, cfg, result)
                        if complete:
                            result['completed_groups'].append(grp.keeper['drive_id'])
                            journal.event('group_completed', {'keeper': grp.keeper['drive_id']})
                        else:
                            result['review_groups'][grp.keeper['drive_id']] = 'Grupo interrompido; confira o diário e refaça a análise antes de repetir.'
                            break
                    except DedupCancelled:
                        self._cancelled = True
                        result['review_groups'][grp.keeper['drive_id']] = 'Grupo cancelado durante a execução; confira as operações interrompidas e refaça a análise.'
                        journal.event('group_cancelled', {'keeper': grp.keeper['drive_id']})
                        break
                    except Exception as exc:
                        keeper_name = grp.keeper.get('name') or grp.keeper.get('drive_id') or '?'
                        result['errors'].append(f"[{keeper_name}] {exc}")
                        result['review_groups'][grp.keeper['drive_id']] = str(exc)
                        journal.event('group_interrupted', {'keeper': grp.keeper.get('drive_id'), 'name': keeper_name, 'error': str(exc)})
                        # Uma resposta incerta ou alteração concorrente exige nova análise.
                        break
            result['cancelled'] = self._cancelled
            result['log_path'] = journal.report(result)
            # Cópia remota do relatório é secundária; o diário local já é durável.
            if self._cancelled:
                result['notes'].append('Cancelamento solicitado. As etapas já concluídas não foram desfeitas. '
                                       'Relatório salvo localmente; envio à nuvem não foi iniciado.')
                result['log_path'] = journal.report(result)
            elif result['files_moved']:
                try:
                    import io
                    from googleapiclient.http import MediaIoBaseUpload
                    folder = self.destination([], cfg, journal)
                    media = MediaIoBaseUpload(io.BytesIO(Path(result['log_path']).read_bytes()),
                                              mimetype='application/json', resumable=False)
                    self.drive_service.files().create(body={'name': Path(result['log_path']).name, 'parents': [folder]},
                                                       media_body=media, supportsAllDrives=True, fields='id').execute()
                except DedupCancelled:
                    result['cancelled'] = True
                    result['notes'].append('Envio do relatório cancelado. A cópia local e as etapas concluídas foram preservadas.')
                    result['log_path'] = journal.report(result)
                except Exception as exc:
                    result['errors'].append('Relatório preservado localmente; envio ao Drive falhou: ' + str(exc))
                    result['log_path'] = journal.report(result)
            self.progress_update.emit(100, 'Operação encerrada; confira o relatório.')
            self.execution_finished.emit(result)
        except Exception as exc:
            logging.exception('Deduplicação interrompida')
            if journal:
                try:
                    journal.event('failed', {'error': str(exc)})
                    journal.report(dict(result, fatal_error=str(exc)))
                except Exception:
                    logging.exception('Não foi possível finalizar o relatório; conferir diário antes de repetir.')
            self.execution_failed.emit(str(exc))
        finally:
            if indexer:
                indexer.close()
            if journal:
                journal.close()

    def execute_group(self, grp, indexer, journal, roots, cfg, result):
        if grp.plan_error:
            raise ValueError(grp.plan_error)
        validate_scope(grp.scope_mode, grp.source_folders, grp.reference_folders, roots, cfg)
        members = [grp.keeper, *grp.duplicates]
        ids = [item['drive_id'] for item in members]
        if len(set(ids)) != len(ids):
            raise ValueError('O mesmo ID aparece como arquivo mantido e duplicado.')
        if any(item['drive_id'] not in grp.eligible_ids or
               not eligible(item, grp.scope_mode, grp.source_folders, roots) for item in grp.duplicates):
            raise ValueError('Movimento fora do escopo de origem autorizado.')
        if grp.scope_mode == MODE_FOLDER_VS_FOLDER and not under(grp.keeper['path'], grp.reference_folders[0]):
            raise ValueError('O arquivo preservado precisa estar na pasta de comparação.')
        if grp.scope_mode == MODE_SELECTED_FOLDERS and not eligible(grp.keeper, grp.scope_mode, grp.source_folders, roots):
            raise ValueError('O arquivo preservado precisa estar nas pastas selecionadas.')
        live = [self.cloud.get(item['drive_id']) for item in members]
        for item, fresh in zip(members, live):
            assert_metadata(item, fresh, grp.md5, grp.size, cfg.get('production_drive_id'))
        context = []
        if grp.year_context:
            by_id = {m['id']: m for m in live}
            for item in grp.year_context:
                fresh = by_id.get(item['drive_id']) or self.cloud.get(item['drive_id'])
                assert_metadata(item, fresh, grp.md5, grp.size, cfg.get('production_drive_id'))
                context.append(dict(item, name=fresh['name'], imageMediaMetadata=fresh.get('imageMediaMetadata') or {}))
        live = [self.cloud.prepare_write(item) for item in live]
        if not live[0].get('capabilities', {}).get('canEdit') or any(
                not item.get('capabilities', {}).get('canMoveItemOutOfDrive') for item in live[1:]):
            raise ValueError('Permissões insuficientes para editar o preservado ou mover os duplicados.')
        for item in grp.duplicates:
            if journal.conn.execute('SELECT 1 FROM moves WHERE file_id=?', (item['drive_id'],)).fetchone():
                raise ValueError('Arquivo com execução anterior registrada. Use Conferir operações interrompidas.')
        keeper_live = dict(grp.keeper, name=live[0]['name'], imageMediaMetadata=live[0].get('imageMediaMetadata') or {})
        members_live = [
            dict(item, name=fresh['name'], imageMediaMetadata=fresh.get('imageMediaMetadata') or {})
            for item, fresh in zip(members, live)
        ]
        live_disc = evaluate_group_discrepancies(keeper_live, context or members_live, roots)
        # Usar descrições atuais na nuvem, não cópias do SQLite ou o preview antigo.
        if grp.edited_description is not None:
            if any((item.get('description') or '') != (fresh.get('description') or '')
                   for item, fresh in zip(members, live)):
                raise ValueError('As tags no Drive mudaram após a análise. Revise novamente sua edição manual; grupo não alterado.')
            merged = ', '.join(parse_tags(grp.edited_description))
        else:
            for item in live[1:]:
                if item['name'].casefold() != live[0]['name'].casefold() and any(c in item['name'] for c in '\r\n'):
                    raise ValueError(f"Nome '{item['name']}' contém quebra de linha; revisão manual necessária.")
            merged = compute_merged_tags(
                live[0].get('description', ''),
                live[0]['name'],
                live[1:],
                keeper_rel_path=grp.keeper.get('rel_path'),
                keeper_path=grp.keeper.get('path'),
                roots=roots,
                extra_tags=live_disc['extra_tags'],
            )
        if len(merged.encode('utf-8')) > 30000:
            raise ValueError('Descrição resultante excede o limite seguro; nenhuma tag foi truncada.')
        journal.event('group_prepared', {'md5': grp.md5, 'size': grp.size, 'members': live,
                                         'description_after': merged, 'manual_tags': grp.edited_description is not None})
        for item in live:
            if not metadata_stable(item, self.cloud.get(item['id'])):
                raise ValueError('Metadados mudaram durante a conferência. Analise novamente.')
        if merged != live[0].get('description', ''):
            self.cloud.update(live[0], body={'description': merged})
            result['tags_updated'] += 1
        keeper = self.cloud.get(live[0]['id'])
        assert_metadata(grp.keeper, keeper, grp.md5, grp.size, cfg.get('production_drive_id'))
        if keeper.get('description', '') != merged:
            raise ValueError('A gravação das tags não foi confirmada; nenhum duplicado será movido.')
        indexer.update_description(grp.keeper['file_id'], merged, commit=True, allow_empty_override=True)
        journal.event('tags_confirmed', {'keeper': keeper['id'], 'description': merged})
        result.setdefault('keeper_updates', {})[keeper['id']] = {'description': merged}
        if self._cancelled:
            return False
        for dup, before in zip(grp.duplicates, live[1:]):
            if self._cancelled:
                return False
            fresh = self.cloud.get(dup['drive_id'])
            if not metadata_stable(before, fresh):
                raise ValueError('Duplicado alterado após a fusão das tags. Movimento bloqueado.')
            rel = relative_parts(dup['path'], roots[0])
            parent = self.destination(rel[:-1], cfg, journal)
            name = self.unique_name(parent, fresh['name'], cfg.get('sandbox_drive_id'), fresh['id'])
            if not metadata_stable(keeper, self.cloud.get(keeper['id'])):
                raise ValueError('Arquivo preservado mudou durante a operação. Movimento bloqueado.')
            payload = dict(file_id=fresh['id'], local_id=dup['file_id'], origin_path=dup['path'],
                           original_parents=fresh['parents'], original_name=fresh['name'],
                           destination_parent=parent, destination_drive=cfg.get('sandbox_drive_id'),
                           destination_name=name, relative_path='\\'.join(rel), keeper_id=keeper['id'],
                           md5=grp.md5, size=grp.size)
            journal.begin_move(fresh['id'], payload)
            self.cloud.update(fresh, addParents=parent, removeParents=','.join(fresh['parents']),
                              body={'name': name})
            moved = self.cloud.get(fresh['id'])
            if (moved.get('parents') != [parent] or moved.get('driveId') != cfg.get('sandbox_drive_id')
                    or moved.get('trashed') or moved.get('md5Checksum') != grp.md5
                    or int(moved.get('size') or 0) != grp.size or moved.get('name') != name):
                raise ValueError('Destino remoto não confirmado; registro local preservado para conferência.')
            journal.state(fresh['id'], 'confirmed')
            result['files_moved'] += 1
            journal.event('move_confirmed', payload)
            clean_local(indexer, payload)
            journal.state(fresh['id'], 'cleaned')
            result.setdefault('completed_moves', []).append(fresh['id'])
        return True
