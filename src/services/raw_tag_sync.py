"""Sincronização bidirecional de tags entre arquivos RAW e suas cópias (JPG/PNG/HEIC/TIFF/etc.).

Fluxo seguro (com Diário próprio e ETag no Google Drive):
1. Análise (somente leitura):
   - Baixa o inventário atual do Google Drive (com `description` e `imageMediaMetadata(time,cameraMake,cameraModel)`).
   - Agrupa arquivos RAW e suas cópias convertidas dentro do mesmo evento:
     * Nível 1 ('same_folder'): mesma pasta e mesmo nome-base de câmera/edição;
     * Nível 2 ('format_subfolder'): subpasta dedicada de formato (ex.: `RAW`, `Bruto`, `Arquivo Bruto Original`,
       `Arquivos RAW originais`, `Originais`, `JPG`, `JPGs`, `Convertidas`, `Select_JPG`, `Seleção Tratadas`)
       conectada à pasta-mãe do evento ou às subpastas temáticas irmãs do mesmo evento.
   - Separa automaticamente disparos distintos com o mesmo nome-base quando o horário EXIF (`imageMediaMetadata.time`)
     ou o modelo da câmera (`cameraModel`) desambiguam perfeitamente qual RAW gerou qual cópia (ex.: câmera A7S3 vs.
     A6400 no mesmo evento), bloqueando qualquer grupo que ainda permaneça ambíguo.
   - Calcula a soma bidirecional das tags entre todos os arquivos do grupo:
     * Ignora tags que sejam nomes de arquivos antigos deixados por deduplicações anteriores (quando marcado);
     * Nunca propaga a tag `"RAW"` de um arquivo bruto para uma cópia (JPG/PNG/HEIC/etc.);
     * Quando `add_raw_tag=True` (padrão), garante a tag `"RAW"` exclusivamente nos arquivos brutos (tanto os
       pareados quanto, opcionalmente via `include_unpaired_raws=True`, os RAWs sem cópia JPG/PNG ou cujo par
       esteja bloqueado — nesses casos aplicando apenas a tag `"RAW"` no arquivo bruto sem copiar tags do par!).
2. Execução (somente após aprovação explícita):
   - Exige Modo Edição e fila vazia, faz backup local do catálogo (`local_backup`), grava no Diário SQLite
     (`data/raw_tag_sync/journal.db`), releitura ao vivo no Google Drive com escrita condicional via ETag
     (`DriveCatalog.update`), e atualiza imediatamente `files` + `search_index` no banco local (`file_index.db`).
"""
from __future__ import annotations

import json
import ntpath
import os
import re
import sqlite3
import time
import unicodedata
import uuid
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from PyQt6.QtCore import QLockFile, QObject, pyqtSignal

from src.database.database import to_canonical_id
from src.database.search import SearchEngine
from src.services.hash_deduplicator import (
    DedupCancelled,
    DriveCatalog,
    FOLDER_MIME,
    parse_tags,
    private_service,
)
from src.services.insync_suffix_repair import (
    _extract_rel_parts,
    _fold,
    _resolve_active_roots_and_drive,
    build_drive_folder_index,
)
from src.services.shortcut_migration import MigrationAPI
from src.services.snapshot_management import assert_queue_empty, local_backup
from src.utils.config_manager import ConfigManager

RAW_EXTENSIONS = frozenset({
    '.arw', '.cr2', '.cr3', '.nef', '.dng', '.raf', '.orf', '.srw', '.rw2', '.pef', '.raw',
})
COPY_EXTENSIONS = frozenset({
    '.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif', '.tif', '.tiff', '.bmp',
})
ALL_MEDIA_EXTENSIONS = RAW_EXTENSIONS | COPY_EXTENSIONS

RAW_TAG_LABEL = 'RAW'
RAW_ONLY_TAGS_FOLDED = frozenset({'raw'})

# Subpastas dedicadas a guardar os arquivos RAW ou as cópias exportadas dentro de um evento
RAW_SUBFOLDER_NAMES = frozenset({
    'raw', 'raws', 'bruto', 'brutos', 'originais', 'original',
    'arquivo bruto original', 'arquivos brutos originais',
    'arquivos raw originais', 'arquivo raw original', 'fotos raw',
})
COPY_SUBFOLDER_NAMES = frozenset({
    'jpg', 'jpgs', 'jpeg', 'jpegs', 'png', 'pngs',
    'convertidas', 'convertidos', 'jpg - convertido', 'jpg convertido',
    'select_jpg', 'selecao tratadas', 'seleção tratadas', 'tratadas', 'tratados',
    'editadas', 'editados', 'exportadas', 'exportados', 'workflow',
})

# Sufixos comuns gerados por Lightroom / Photoshop / Camera Raw / Darktable / InSync / Desambiguador
_DERIVATIVE_SUFFIX_RE = re.compile(
    r'(?:[-_\s]+(?:enhanced(?:-nr|-sr)?|editar|editada|editado|edit|tratada|tratado|hdr|pano|nr|denoise))+$',
    re.IGNORECASE,
)
_TRAILING_PAREN_COPY_RE = re.compile(r'^(.*?)(\s*\((\d+)\))$')
_TRAILING_DISAMBIG_RE = re.compile(r'^(.*?)(_(\d+))$')

# Tag que é claramente um nome de arquivo antigo com extensão de mídia (herança de deduplicação)
_FILENAME_TAG_RE = re.compile(
    r'^.+\.(?:jpg|jpeg|png|webp|heic|heif|tif|tiff|bmp|gif|arw|cr2|cr3|nef|dng|raf|orf|srw|rw2|pef|raw|mp4|mov|avi|mts)$',
    re.IGNORECASE,
)

RAW_SYNC_SCAN_FIELDS = (
    'id,name,mimeType,parents,driveId,trashed,size,md5Checksum,'
    'description,version,webViewLink,imageMediaMetadata(time,cameraMake,cameraModel),capabilities(canEdit)'
)


def is_old_filename_tag(tag: str) -> bool:
    """Retorna True se a tag parecer um nome de arquivo antigo com extensão de mídia."""
    clean = str(tag or '').strip()
    if not clean:
        return False
    return bool(_FILENAME_TAG_RE.match(clean))


def normalize_photo_base_stem(
    filename: str,
    *,
    strip_copy_numbers: bool = False,
) -> str:
    """Extrai o nome-base da foto para parear RAW e cópias exportadas (ex.: DSC00001-Enhanced-NR.jpg -> dsc00001)."""
    stem, _ext = ntpath.splitext(str(filename or '').strip())
    cur = unicodedata.normalize('NFC', stem).strip()
    if strip_copy_numbers:
        while True:
            m_par = _TRAILING_PAREN_COPY_RE.match(cur)
            if m_par and m_par.group(1).strip():
                num = int(m_par.group(3))
                if not (1900 <= num <= 2100):
                    cur = m_par.group(1).rstrip()
                    continue
            break
    # Remove sufixos de revelação (ex.: -Enhanced-NR, _editada)
    while True:
        new_cur = _DERIVATIVE_SUFFIX_RE.sub('', cur).rstrip()
        if new_cur and new_cur != cur:
            cur = new_cur
        else:
            break
    return cur.casefold()


def compute_bidirectional_tags(
    descriptions: list[str],
    *,
    is_raw_flags: list[bool] | None = None,
    ignore_filename_tags: bool = True,
    add_raw_tag: bool = True,
) -> dict[int, tuple[str, list[str]]]:
    """Calcula para cada membro (índice i) a nova descrição somada e a lista de tags adicionadas.

    Regras de segurança:
    - Preserva na frente todas as tags já existentes no próprio arquivo (sem apagar nada).
    - Ignora nomes de arquivos antigos (`is_old_filename_tag`) ao transferir entre parceiros quando
      `ignore_filename_tags=True`.
    - **Tag `"RAW"` exclusiva de arquivos brutos**:
      * Nunca entra no pool compartilhado para cópias (JPG/PNG/HEIC/etc.), então um JPG/PNG jamais recebe `"RAW"`;
      * Quando `add_raw_tag=True` e `is_raw_flags[i]` é `True`, adiciona `"RAW"` caso ainda não exista (case-insensitive)
        naquele arquivo bruto.
    """
    parsed_per_member: list[list[str]] = [parse_tags(desc) for desc in descriptions]
    if is_raw_flags is None:
        flags = [False] * len(descriptions)
    else:
        if len(is_raw_flags) != len(descriptions):
            raise ValueError('is_raw_flags deve ter o mesmo tamanho que descriptions.')
        flags = list(is_raw_flags)

    # Coleta o conjunto ordenado global de tags transferíveis do grupo (excluindo RAW_ONLY_TAGS_FOLDED!)
    shared_pool: list[str] = []
    seen_pool: set[str] = set()
    for tags in parsed_per_member:
        for tag in tags:
            if ignore_filename_tags and is_old_filename_tag(tag):
                continue
            key = _fold(tag)
            if key in RAW_ONLY_TAGS_FOLDED:
                continue
            if key not in seen_pool:
                seen_pool.add(key)
                shared_pool.append(tag)

    result: dict[int, tuple[str, list[str]]] = {}
    for idx, own_tags in enumerate(parsed_per_member):
        own_seen = {_fold(t) for t in own_tags}
        added: list[str] = []
        for candidate in shared_pool:
            ckey = _fold(candidate)
            if ckey not in own_seen:
                own_seen.add(ckey)
                added.append(candidate)
        if add_raw_tag and flags[idx]:
            if _fold(RAW_TAG_LABEL) not in own_seen:
                own_seen.add(_fold(RAW_TAG_LABEL))
                added.append(RAW_TAG_LABEL)
        merged = [*own_tags, *added]
        result[idx] = (', '.join(merged), added)
    return result


@dataclass
class RawSyncMember:
    drive_id: str
    name: str
    ext: str
    is_raw: bool
    rel_folder: str
    local_path: str  # vazio se não existir localmente
    current_description: str
    proposed_description: str = ''
    added_tags: list[str] = field(default_factory=list)
    exif_time: str = ''
    camera_model: str = ''
    web_link: str = ''
    can_edit: bool = True


@dataclass
class RawTagSyncGroup:
    group_id: str
    event_folder: str
    base_stem: str
    match_kind: str  # 'same_folder' | 'format_subfolder' | 'raw_only'
    raw_members: list[RawSyncMember] = field(default_factory=list)
    copy_members: list[RawSyncMember] = field(default_factory=list)
    status: str = 'eligible'  # 'eligible' | 'blocked' | 'completed' | 'failed'
    reason: str = ''

    @property
    def all_members(self) -> list[RawSyncMember]:
        return [*self.raw_members, *self.copy_members]

    @property
    def members_needing_update(self) -> list[RawSyncMember]:
        return [m for m in self.all_members if m.added_tags]


class RawTagSyncJournal:
    """Diário durável SQLite em `data/raw_tag_sync/journal.db` para auditoria das atualizações."""

    def __init__(self, db_name: str):
        base = Path(db_name).resolve().parent / 'raw_tag_sync'
        base.mkdir(parents=True, exist_ok=True)
        self.path = base / 'journal.db'
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=15)
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=FULL')
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                'CREATE TABLE IF NOT EXISTS runs ('
                'run_id TEXT PRIMARY KEY, started_at REAL, backup_path TEXT, '
                'scope_folder TEXT, ignore_filename_tags INTEGER, status TEXT)'
            )
            conn.execute(
                'CREATE TABLE IF NOT EXISTS updates ('
                'seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, group_id TEXT, '
                'drive_id TEXT, local_path TEXT, old_description TEXT, new_description TEXT, '
                'added_tags TEXT, state TEXT, error TEXT, updated_at REAL)'
            )

    def start_run(self, run_id: str, backup_path: str, scope_folder: str, ignore_filename_tags: bool):
        with self._connect() as conn:
            conn.execute(
                'INSERT INTO runs VALUES (?,?,?,?,?,?)',
                (run_id, time.time(), backup_path, scope_folder, int(ignore_filename_tags), 'running'),
            )

    def finish_run(self, run_id: str, status: str):
        with self._connect() as conn:
            conn.execute('UPDATE runs SET status=? WHERE run_id=?', (status, run_id))

    def record_update(
        self,
        *,
        run_id: str,
        group_id: str,
        drive_id: str,
        local_path: str,
        old_description: str,
        new_description: str,
        added_tags: list[str],
        state: str,
        error: str = '',
    ):
        with self._connect() as conn:
            conn.execute(
                'INSERT INTO updates(run_id, group_id, drive_id, local_path, old_description, '
                'new_description, added_tags, state, error, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (
                    run_id,
                    group_id,
                    drive_id,
                    local_path,
                    old_description,
                    new_description,
                    json.dumps(added_tags, ensure_ascii=False),
                    state,
                    error,
                    time.time(),
                ),
            )


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize('NFKD', str(text or ''))
    return ''.join(ch for ch in decomposed if not unicodedata.combining(ch))


def _normalize_folder_label(name: str) -> str:
    return _strip_accents(name).casefold().strip()


def is_raw_filename(filename_or_path: str | None) -> bool:
    """Retorna True se o nome ou caminho possuir uma extensão RAW conhecida."""
    if not filename_or_path:
        return False
    _stem, ext = ntpath.splitext(str(filename_or_path).strip())
    return ext.lower() in RAW_EXTENSIONS


def is_copy_filename(filename_or_path: str | None) -> bool:
    """Retorna True se o nome ou caminho possuir uma extensão de cópia (JPG/PNG/HEIC/etc.)."""
    if not filename_or_path:
        return False
    _stem, ext = ntpath.splitext(str(filename_or_path).strip())
    return ext.lower() in COPY_EXTENSIONS


def ensure_raw_tag_in_description(description: str, filename_or_path: str | None) -> str:
    """Se o arquivo for RAW e ainda não tiver a tag 'RAW' (case-insensitive), acrescenta 'RAW'."""
    if not is_raw_filename(filename_or_path):
        return description
    tags = parse_tags(description)
    if any(_fold(t) in RAW_ONLY_TAGS_FOLDED for t in tags):
        return ', '.join(tags)
    tags.append(RAW_TAG_LABEL)
    return ', '.join(tags)


def _get_excluded_drive_letters() -> frozenset[str]:
    try:
        return frozenset(ConfigManager().get_excluded_drive_letters())
    except Exception:
        return frozenset()


def _is_excluded_drive_path(path: str, excluded_drives: frozenset[str] | set[str] | None = None) -> bool:
    if not path:
        return False
    drives = _get_excluded_drive_letters() if excluded_drives is None else excluded_drives
    if not drives:
        return False
    drive = ntpath.splitdrive(ntpath.normpath(str(path)))[0].upper()
    return bool(drive and drive in drives)


def _read_local_exif_signature(path: str, excluded_drives: frozenset[str] | set[str] | None = None) -> tuple[str, str]:
    """Lê (DateTimeOriginal, Model) de metadados EXIF de forma leve sem bloquear em arquivos ausentes/online.

    Retorna ('', '') se o arquivo não existir, estiver em unidade excluída (ex.: O:),
    for somente-online ou não possuir EXIF legível.
    """
    if not path or _is_excluded_drive_path(path, excluded_drives):
        return '', ''
    try:
        st = os.stat(path)
    except OSError:
        return '', ''
    # Não hidratar arquivos somente-online do OneDrive/InSync/Drive Desktop
    if getattr(st, 'st_file_attributes', 0) & (0x1000 | 0x40000 | 0x400000):
        return '', ''
    ext = ntpath.splitext(path)[1].lower()
    if ext in ('.jpg', '.jpeg', '.tif', '.tiff', '.webp'):
        try:
            from PIL import Image
            with Image.open(path) as img:
                exif = img.getexif()
                if not exif:
                    return '', ''
                model = str(exif.get(272) or '').strip()
                dt = ''
                try:
                    exif_ifd = exif.get_ifd(0x8769)
                    dt = str(exif_ifd.get(36867) or exif_ifd.get(36868) or '').strip()
                except Exception:
                    pass
                if not dt:
                    dt = str(exif.get(306) or '').strip()
                return dt, model
        except Exception:
            return '', ''
    # Para arquivos RAW locais (ARW, CR2, DNG, RAF, NEF...), tenta Pillow (TIFF-based) e fallback leve no cabeçalho
    try:
        from PIL import Image
        with Image.open(path) as img:
            exif = img.getexif()
            if exif:
                model = str(exif.get(272) or '').strip()
                dt = ''
                try:
                    exif_ifd = exif.get_ifd(0x8769)
                    dt = str(exif_ifd.get(36867) or '').strip()
                except Exception:
                    pass
                if not dt:
                    dt = str(exif.get(306) or '').strip()
                if dt or model:
                    return dt, model
    except Exception:
        pass
    try:
        with open(path, 'rb') as stream:
            head = stream.read(65536)
        m_dt = re.search(rb'(?:19|20)\d{2}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}', head)
        dt = m_dt.group(0).decode('ascii', 'ignore') if m_dt else ''
        m_cam = re.search(rb'(?:ILCE-[0-9A-Za-z-]+|Canon EOS [0-9A-Za-z -]{2,20}|NIKON [0-9A-Za-z -]{2,20}|X-[A-Za-z0-9-]{2,12})', head)
        model = m_cam.group(0).decode('ascii', 'ignore').strip() if m_cam else ''
        return dt, model
    except Exception:
        return '', ''


class LocalRawPairFinder:
    """Localiza rapidamente pares RAW ↔ Cópias no catálogo SQLite local (`file_index.db`) sem varrer o disco inteiro.

    Usa os índices `idx_files_parentId` e cache de pastas em memória para responder em milissegundos,
    tanto na edição individual quanto em lotes de milhares de arquivos.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._has_parent_col = False
        self._has_source_col = False
        self._has_mime_col = False
        self._has_name_col = False
        self._has_path_col = False
        cols = {row[1] for row in conn.execute('PRAGMA table_info(files)')}
        self._has_parent_col = 'parentId' in cols
        self._has_source_col = 'source' in cols
        self._has_mime_col = 'mimeType' in cols
        self._has_name_col = 'name' in cols
        self._has_path_col = 'path' in cols
        self._dir_files_cache: dict[str, list[dict]] = {}
        self._dir_subfolders_cache: dict[str, list[tuple[str, str]]] = {}
        self._exif_cache: dict[str, tuple[str, str]] = {}
        self._excluded_drives: frozenset[str] = _get_excluded_drive_letters()

    @property
    def supported(self) -> bool:
        return self._has_parent_col and self._has_name_col and self._has_path_col

    def _list_files_in_dir(self, norm_dir: str) -> list[dict]:
        if not norm_dir or not self.supported:
            return []
        if norm_dir in self._dir_files_cache:
            return self._dir_files_cache[norm_dir]
        where_parts = ['parentId = ?']
        params: list[str] = [norm_dir]
        if self._has_source_col:
            where_parts.append("source = 'local'")
        if self._has_mime_col:
            where_parts.append("mimeType NOT IN ('folder', 'application/vnd.google-apps.folder')")
        sql = f"SELECT file_id, name, path, description FROM files WHERE {' AND '.join(where_parts)}"
        rows = []
        for fid, name, path, desc in self.conn.execute(sql, params):
            ext = ntpath.splitext(name or '')[1].lower()
            if ext not in ALL_MEDIA_EXTENSIONS:
                continue
            rows.append({
                'file_id': fid,
                'name': name or '',
                'path': path or fid,
                'description': desc or '',
                'ext': ext,
                'is_raw': ext in RAW_EXTENSIONS,
                'base_stem': normalize_photo_base_stem(name or '', strip_copy_numbers=False),
                'loose_stem': self._loose_stem(name or ''),
            })
        self._dir_files_cache[norm_dir] = rows
        return rows

    def _list_subfolders_in_dir(self, norm_dir: str) -> list[tuple[str, str]]:
        """Retorna lista de `(subfolder_file_id, subfolder_name)` filhas diretas de `norm_dir`."""
        if not norm_dir or not self.supported:
            return []
        if norm_dir in self._dir_subfolders_cache:
            return self._dir_subfolders_cache[norm_dir]
        where_parts = ['parentId = ?']
        params: list[str] = [norm_dir]
        if self._has_source_col:
            where_parts.append("source = 'local'")
        if self._has_mime_col:
            where_parts.append("mimeType IN ('folder', 'application/vnd.google-apps.folder')")
        sql = f"SELECT file_id, name FROM files WHERE {' AND '.join(where_parts)}"
        subs = [(row[0], row[1] or '') for row in self.conn.execute(sql, params)]
        self._dir_subfolders_cache[norm_dir] = subs
        return subs

    @staticmethod
    def _loose_stem(name: str) -> str:
        loose = normalize_photo_base_stem(name, strip_copy_numbers=True)
        m_dis = _TRAILING_DISAMBIG_RE.match(loose)
        if m_dis and m_dis.group(1).strip() and len(m_dis.group(3)) <= 2:
            loose = m_dis.group(1).strip()
        return loose

    def _get_exif(self, path: str) -> tuple[str, str]:
        key = ntpath.normcase(ntpath.normpath(path)) if path else ''
        if not key:
            return '', ''
        if key not in self._exif_cache:
            self._exif_cache[key] = _read_local_exif_signature(path, excluded_drives=self._excluded_drives)
        return self._exif_cache[key]

    def _compatible_exif(self, path_a: str, path_b: str) -> bool:
        """Retorna False se ambos tiverem EXIF legível e divergirem de modelo de câmera ou horário de disparo."""
        dt_a, cam_a = self._get_exif(path_a)
        dt_b, cam_b = self._get_exif(path_b)
        if cam_a and cam_b and cam_a.casefold() != cam_b.casefold():
            return False
        if dt_a and dt_b and dt_a != dt_b:
            return False
        return True

    def find_counterparts(self, file_id: str, name: str, path: str) -> list[dict]:
        """Encontra os arquivos correspondentes (JPG/PNG/HEIC se a origem for RAW, ou RAW se a origem for cópia).

        Segue as mesmas regras seguras de `scan_raw_tag_sync_groups`:
        1. Mesma pasta (nome-base exato/derivado `-Enhanced-NR`, ou variação `(2)`/`_0` quando há 1 único RAW).
        2. Subpasta técnica de formato (`RAW`, `Arquivo Bruto Original`, `Arquivos RAW originais`, `JPG`, `JPGs`,
           `Convertidas`, `Select_JPG`, `Seleção Tratadas`, etc.) dentro do mesmo evento (pasta-mãe ou subpasta
           temática irmã dentro do mesmo evento).
        3. Verifica EXIF (modelo de câmera + horário de disparo) para nunca vincular fotos de câmeras diferentes
           que compartilhem o mesmo contador de câmera.
        """
        if not self.supported:
            return []
        src_path = path or file_id or ''
        src_name = name or ntpath.basename(src_path)
        src_ext = ntpath.splitext(src_name)[1].lower()
        if src_ext not in ALL_MEDIA_EXTENSIONS:
            return []
        src_is_raw = src_ext in RAW_EXTENSIONS
        src_stem = normalize_photo_base_stem(src_name, strip_copy_numbers=False)
        src_loose = self._loose_stem(src_name)
        if not src_stem:
            return []

        norm_fid = to_canonical_id(file_id or src_path) if ntpath.isabs(file_id or src_path) else (file_id or src_path)
        norm_dir = to_canonical_id(ntpath.dirname(src_path))
        if not norm_dir:
            return []

        candidates: list[dict] = []
        seen_fids: set[str] = {norm_fid}

        def _is_valid_partner(candidate: dict) -> bool:
            if candidate['is_raw'] != src_is_raw:
                return True
            # Também sincronizar DNG derivado (-Enhanced-NR.dng) com o RAW nativo (.ARW/.CR2/...) do mesmo disparo
            if src_is_raw and candidate['is_raw']:
                return (src_ext == '.dng') != (candidate['ext'] == '.dng')
            return False

        def add_from_dir(target_dir: str, *, allow_loose: bool = False):
            dir_files = self._list_files_in_dir(target_dir)
            if not dir_files:
                return
            # Primeiro busca exata por base_stem
            matched = [
                f for f in dir_files
                if f['file_id'] not in seen_fids
                and _is_valid_partner(f)
                and f['base_stem'] == src_stem
            ]
            # Opcionalmente, na mesma pasta, busca por loose_stem ((2), _0) se não houver 2+ RAWs não-DNG homônimos
            if not matched and allow_loose and src_loose:
                non_dng_same_loose = [
                    f for f in dir_files
                    if f['is_raw'] and f['ext'] != '.dng' and f['loose_stem'] == src_loose
                ]
                raws_same_loose = non_dng_same_loose or [
                    f for f in dir_files if f['is_raw'] and f['loose_stem'] == src_loose
                ]
                if len(raws_same_loose) == 1:
                    matched = [
                        f for f in dir_files
                        if f['file_id'] not in seen_fids
                        and _is_valid_partner(f)
                        and f['loose_stem'] == src_loose
                    ]
            for m in matched:
                if self._compatible_exif(src_path, m['path']):
                    seen_fids.add(m['file_id'])
                    candidates.append(m)

        # Nível 1: Mesma pasta
        add_from_dir(norm_dir, allow_loose=True)

        # Nível 2: Subpastas técnicas de formato dentro do mesmo evento
        cur_folder_name = ntpath.basename(norm_dir)
        parent_dir = to_canonical_id(ntpath.dirname(norm_dir))
        grandparent_dir = to_canonical_id(ntpath.dirname(parent_dir)) if parent_dir else ''
        parent_is_event_candidate = (
            bool(parent_dir)
            and bool(grandparent_dir)
            and parent_dir != norm_dir
            and parent_dir != grandparent_dir
            and not re.fullmatch(r'(?:19|20)\d{2}', ntpath.basename(parent_dir))
        )

        if src_is_raw:
            if _is_raw_subfolder(cur_folder_name) and parent_dir and parent_dir != norm_dir:
                # RAW está em `.../Evento/RAW` -> procurar cópias (e DNGs derivados) em `.../Evento` e nas subpastas irmãs
                add_from_dir(parent_dir)
                for sub_id, _sub_name in self._list_subfolders_in_dir(parent_dir):
                    if sub_id != norm_dir:
                        add_from_dir(sub_id)
            else:
                # RAW está em `.../Evento` -> procurar cópias em subpastas técnicas de cópias (`JPG`, `Convertidas`, etc.)
                # e, se for .dng derivado solto, também procurar o RAW nativo em subpasta técnica de RAW
                for sub_id, sub_name in self._list_subfolders_in_dir(norm_dir):
                    if _is_copy_subfolder(sub_name) or (src_ext == '.dng' and _is_raw_subfolder(sub_name)):
                        add_from_dir(sub_id)
                if src_ext == '.dng' and parent_is_event_candidate:
                    for sub_id, sub_name in self._list_subfolders_in_dir(parent_dir):
                        if sub_id != norm_dir and _is_raw_subfolder(sub_name):
                            add_from_dir(sub_id)
        else:
            # Origem é cópia (JPG/PNG/HEIC) -> procurar RAW correspondente!
            if _is_copy_subfolder(cur_folder_name) and parent_dir and parent_dir != norm_dir:
                # Cópia está em `.../Evento/JPG` -> procurar RAW em `.../Evento` e em subpastas técnicas de RAW irmãs
                add_from_dir(parent_dir)
                for sub_id, sub_name in self._list_subfolders_in_dir(parent_dir):
                    if sub_id != norm_dir and _is_raw_subfolder(sub_name):
                        add_from_dir(sub_id)
            else:
                # Cópia está em `.../Evento` -> procurar RAW em subpastas técnicas de RAW filhas diretas
                for sub_id, sub_name in self._list_subfolders_in_dir(norm_dir):
                    if _is_raw_subfolder(sub_name):
                        add_from_dir(sub_id)
                # Ou cópia está em subpasta temática do evento (`.../Evento/Outros` ou `.../Evento/Sacramentos`)
                # -> procurar RAW em subpasta técnica de RAW irmã (`.../Evento/Arquivos RAW originais`)
                if parent_is_event_candidate:
                    for sub_id, sub_name in self._list_subfolders_in_dir(parent_dir):
                        if sub_id != norm_dir and _is_raw_subfolder(sub_name):
                            add_from_dir(sub_id)

        # Trava extra de segurança: se encontrarmos 2+ RAWs não-DNG diferentes para a origem (sem EXIF que desambigue),
        # bloqueia os RAWs ambíguos para nunca atualizar o RAW errado.
        non_dng_raws = [c for c in candidates if c['is_raw'] and c['ext'] != '.dng']
        if len(non_dng_raws) > 1:
            if not src_is_raw:
                return []
            candidates = [c for c in candidates if not (c['is_raw'] and c['ext'] != '.dng')]

        return candidates


_RAW_SUBFOLDER_FOLDED = frozenset(_normalize_folder_label(n) for n in RAW_SUBFOLDER_NAMES)
_COPY_SUBFOLDER_FOLDED = frozenset(_normalize_folder_label(n) for n in COPY_SUBFOLDER_NAMES)


def _is_raw_subfolder(folder_name: str) -> bool:
    return _normalize_folder_label(folder_name) in _RAW_SUBFOLDER_FOLDED


def _is_copy_subfolder(folder_name: str) -> bool:
    return _normalize_folder_label(folder_name) in _COPY_SUBFOLDER_FOLDED


def _event_anchor_for_parts(rel_folder_parts: tuple[str, ...]) -> tuple[tuple[str, ...], str]:
    """Determina a pasta-âncora do evento para um arquivo localizado em `rel_folder_parts`."""
    if not rel_folder_parts:
        return (), 'regular'
    last = rel_folder_parts[-1]
    if len(rel_folder_parts) >= 2 and _is_raw_subfolder(last):
        return rel_folder_parts[:-1], 'raw_sub'
    if len(rel_folder_parts) >= 2 and _is_copy_subfolder(last):
        return rel_folder_parts[:-1], 'copy_sub'
    return rel_folder_parts, 'regular'


def _split_members_by_exif_shot(members: list[RawSyncMember]) -> list[list[RawSyncMember]]:
    """Separa disparos distintos quando 2 câmeras geram o mesmo nome-base no mesmo evento."""
    exif_times = {m.exif_time for m in members if m.exif_time}
    if len(exif_times) <= 1:
        return [members]
    if any(not m.exif_time for m in members):
        return [members]

    by_shot: dict[tuple[str, str], list[RawSyncMember]] = defaultdict(list)
    for m in members:
        by_shot[(m.exif_time, m.camera_model.casefold())].append(m)

    valid_shots = [
        shot_members
        for shot_members in by_shot.values()
        if any(m.is_raw for m in shot_members) and any(not m.is_raw for m in shot_members)
    ]
    if valid_shots:
        return valid_shots
    return [members]


def scan_raw_tag_sync_groups(
    *,
    roots: list[str],
    drive_id: str,
    drive_items: list[dict],
    scope_folder: str = '',
    ignore_filename_tags: bool = True,
    add_raw_tag: bool = True,
    include_unpaired_raws: bool = True,
    cancel_check=None,
    progress_cb=None,
) -> list[RawTagSyncGroup]:
    """Analisa o inventário do Google Drive e monta os grupos RAW ↔ Cópias (e RAWs sem par, se habilitado)."""
    if progress_cb:
        progress_cb(15, 'Mapeando hierarquia de pastas do Google Drive...')

    folder_by_path, children_by_parent = build_drive_folder_index(drive_items, drive_id)
    path_by_folder_id: dict[str, tuple[str, ...]] = {}
    folder_real_name: dict[str, str] = {}
    folder_parent_id: dict[str, str] = {}
    for item in drive_items:
        if item.get('mimeType') == FOLDER_MIME and not item.get('trashed'):
            fid = item.get('id')
            parents = item.get('parents') or []
            if fid and len(parents) == 1:
                folder_real_name[fid] = item.get('name', '')
                folder_parent_id[fid] = parents[0]

    for rel_tuple, fid in folder_by_path.items():
        if fid:
            path_by_folder_id[fid] = rel_tuple

    def real_rel_parts(folder_id: str) -> tuple[str, ...]:
        parts: list[str] = []
        cur = folder_id
        seen = set()
        while cur and cur != drive_id and cur not in seen:
            seen.add(cur)
            nm = folder_real_name.get(cur)
            if not nm:
                break
            parts.append(nm)
            cur = folder_parent_id.get(cur, '')
        return tuple(reversed(parts))

    # Filtro opcional de escopo (pasta local escolhida pelo usuário)
    scope_rel_prefix: tuple[str, ...] | None = None
    if scope_folder:
        norm_scope = ntpath.normpath(scope_folder)
        for r in roots:
            norm_r = ntpath.normpath(r).rstrip('\\/')
            if _fold(norm_scope) == _fold(norm_r):
                scope_rel_prefix = ()
                break
            rel_p = _extract_rel_parts(norm_scope, [r])
            if rel_p is not None:
                scope_rel_prefix = rel_p
                break
        if scope_rel_prefix is None:
            raise ValueError(f'A pasta selecionada está fora da raiz configurada: {scope_folder}')

    primary_root = roots[0] if roots else ''

    if progress_cb:
        progress_cb(35, 'Classificando arquivos RAW e cópias convertidas...')

    same_folder_buckets: dict[tuple[tuple[str, ...], str], list[RawSyncMember]] = defaultdict(list)
    all_records: list[tuple[tuple[str, ...], str, RawSyncMember]] = []

    for parent_id, children in children_by_parent.items():
        if cancel_check and cancel_check():
            raise DedupCancelled('Análise cancelada.')
        if parent_id not in path_by_folder_id:
            continue
        rel_folded = path_by_folder_id[parent_id]
        if scope_rel_prefix is not None:
            if len(rel_folded) < len(scope_rel_prefix) or rel_folded[:len(scope_rel_prefix)] != scope_rel_prefix:
                continue
        rel_display_parts = real_rel_parts(parent_id)
        rel_folder_str = '\\'.join(rel_display_parts)

        for item in children:
            if item.get('mimeType') == FOLDER_MIME or item.get('trashed'):
                continue
            name = item.get('name') or ''
            _stem_raw, ext_raw = ntpath.splitext(name)
            ext = ext_raw.lower()
            if ext not in ALL_MEDIA_EXTENSIONS:
                continue
            is_raw = ext in RAW_EXTENSIONS
            local_candidate = os.path.join(primary_root, rel_folder_str, name) if primary_root else ''
            local_path = local_candidate if (local_candidate and os.path.exists(local_candidate)) else ''
            caps = item.get('capabilities') or {}
            img_meta = item.get('imageMediaMetadata') or {}
            member = RawSyncMember(
                drive_id=item['id'],
                name=name,
                ext=ext,
                is_raw=is_raw,
                rel_folder=rel_folder_str,
                local_path=local_path,
                current_description=(item.get('description') or ''),
                exif_time=str(img_meta.get('time') or '').strip(),
                camera_model=str(img_meta.get('cameraModel') or '').strip(),
                web_link=str(item.get('webViewLink') or ''),
                can_edit=bool(caps.get('canEdit', True)),
            )
            base_stem = normalize_photo_base_stem(name, strip_copy_numbers=False)
            if not base_stem:
                continue
            same_folder_buckets[(rel_folded, base_stem)].append(member)
            all_records.append((rel_folded, base_stem, member))

    if progress_cb:
        progress_cb(60, 'Formando duplas/grupos RAW ↔ JPG/PNG por pasta e evento...')

    paired_drive_ids: set[str] = set()
    candidate_groups: list[tuple[str, tuple[str, ...], str, list[RawSyncMember]]] = []

    # Passo 1: Pareamento Nível 1 — Mesma pasta + mesmo nome-base (exato ou derivado `-Enhanced-NR`)
    for (rel_folded, base_stem), members in same_folder_buckets.items():
        raws = [m for m in members if m.is_raw]
        copies = [m for m in members if not m.is_raw]
        if raws and copies:
            for m in members:
                paired_drive_ids.add(m.drive_id)
            candidate_groups.append(('same_folder', rel_folded, base_stem, members))

    # Passo 2: Na mesma pasta, verificar cópias com sufixo `(2)` ou `_0` quando o RAW está sem sufixo na mesma pasta
    unpaired_in_folder: dict[tuple[str, ...], list[RawSyncMember]] = defaultdict(list)
    for rel_folded, _bs, m in all_records:
        if m.drive_id not in paired_drive_ids:
            unpaired_in_folder[rel_folded].append(m)

    for rel_folded, members in unpaired_in_folder.items():
        by_loose_stem: dict[str, list[RawSyncMember]] = defaultdict(list)
        for m in members:
            loose = normalize_photo_base_stem(m.name, strip_copy_numbers=True)
            m_dis = _TRAILING_DISAMBIG_RE.match(loose)
            if m_dis and m_dis.group(1).strip() and len(m_dis.group(3)) <= 2:
                loose = m_dis.group(1).strip()
            if loose:
                by_loose_stem[loose].append(m)
        for loose_stem, lmembers in by_loose_stem.items():
            raws = [m for m in lmembers if m.is_raw]
            copies = [m for m in lmembers if not m.is_raw]
            if len(raws) == 1 and copies:
                for m in lmembers:
                    paired_drive_ids.add(m.drive_id)
                candidate_groups.append(('same_folder', rel_folded, loose_stem, lmembers))

    # Passo 3: Pareamento Nível 2 — Subpasta técnica de formato (`RAW`, `Arquivo Bruto Original`, `JPG`, etc.)
    event_buckets: dict[tuple[tuple[str, ...], str], list[tuple[str, RawSyncMember]]] = defaultdict(list)
    for rel_folded, base_stem, m in all_records:
        if m.drive_id in paired_drive_ids:
            continue
        anchor, role = _event_anchor_for_parts(rel_folded)
        if role in ('raw_sub', 'copy_sub'):
            event_buckets[(anchor, base_stem)].append((role, m))
        else:
            event_buckets[(rel_folded, base_stem)].append(('event_root', m))
            if len(rel_folded) >= 2:
                parent_event = rel_folded[:-1]
                # Nunca subir para a raiz de ano/categoria; exigir profundidade >= 3 (Ano / Categoria / Evento)
                if len(parent_event) >= 3:
                    event_buckets[(parent_event, base_stem)].append(('thematic_sub', m))

    for (anchor_folded, base_stem), tagged_members in event_buckets.items():
        has_tech_sub = any(role in ('raw_sub', 'copy_sub') for role, _m in tagged_members)
        if not has_tech_sub:
            continue
        unique_members: list[RawSyncMember] = []
        seen_ids: set[str] = set()
        for _role, m in tagged_members:
            if m.drive_id not in paired_drive_ids and m.drive_id not in seen_ids:
                seen_ids.add(m.drive_id)
                unique_members.append(m)
        raws = [m for m in unique_members if m.is_raw]
        copies = [m for m in unique_members if not m.is_raw]
        if raws and copies:
            for m in unique_members:
                paired_drive_ids.add(m.drive_id)
            candidate_groups.append(('format_subfolder', anchor_folded, base_stem, unique_members))

    if progress_cb:
        progress_cb(80, 'Calculando soma de tags e verificando segurança dos grupos...')

    results: list[RawTagSyncGroup] = []
    # Rastreia quais drive_ids de arquivos RAW já foram incluídos em algum grupo elegível pareado
    # (se um RAW ficar de fora por split EXIF ou porque seu grupo foi bloqueado ou não tinha par,
    # ele pode receber a tag "RAW" isoladamente quando `add_raw_tag and include_unpaired_raws`!)
    raw_ids_in_eligible_pair: set[str] = set()

    for match_kind, anchor_folded, base_stem, raw_candidate_members in candidate_groups:
        if cancel_check and cancel_check():
            raise DedupCancelled('Análise cancelada.')

        sub_groups = _split_members_by_exif_shot(raw_candidate_members)

        for members in sub_groups:
            raws = sorted([m for m in members if m.is_raw], key=lambda x: _fold(x.name))
            copies = sorted([m for m in members if not m.is_raw], key=lambda x: _fold(x.name))
            if not raws or not copies:
                continue

            raw_non_dng = [r for r in raws if r.ext != '.dng']
            exif_times = {m.exif_time for m in members if m.exif_time}
            camera_models = {m.camera_model.casefold() for m in members if m.camera_model}
            ambiguous_reason = ''
            if len(raw_non_dng) > 1:
                ambiguous_reason = f'Existem {len(raw_non_dng)} arquivos RAW originais com o mesmo nome-base neste evento.'
            elif len(camera_models) > 1:
                models_disp = ', '.join(sorted({m.camera_model for m in members if m.camera_model}))
                ambiguous_reason = f'Modelos de câmera divergentes entre RAW e cópia ({models_disp}).'
            elif len(exif_times) > 1:
                ambiguous_reason = f'Horários EXIF divergentes entre RAW e cópia ({", ".join(sorted(exif_times))}).'
            elif any(not m.can_edit for m in members):
                ambiguous_reason = 'Sem permissão de edição em um ou mais arquivos no Google Drive.'

            ordered = [*raws, *copies]
            descriptions = [m.current_description for m in ordered]
            is_raw_flags = [m.is_raw for m in ordered]
            merged_map = compute_bidirectional_tags(
                descriptions,
                is_raw_flags=is_raw_flags,
                ignore_filename_tags=ignore_filename_tags,
                add_raw_tag=add_raw_tag,
            )
            any_change = False
            for idx, member in enumerate(ordered):
                new_desc, added = merged_map[idx]
                member.proposed_description = new_desc
                member.added_tags = added
                if added:
                    any_change = True

            if not ambiguous_reason:
                for r in raws:
                    raw_ids_in_eligible_pair.add(r.drive_id)

            if not any_change:
                continue

            event_display = raws[0].rel_folder if match_kind == 'same_folder' else '\\'.join(anchor_folded)
            if match_kind == 'format_subfolder' and raws[0].rel_folder:
                parts_real = raws[0].rel_folder.split('\\')
                if len(parts_real) >= len(anchor_folded):
                    event_display = '\\'.join(parts_real[:len(anchor_folded)])

            gid = uuid.uuid5(
                uuid.NAMESPACE_OID,
                '|'.join(sorted(m.drive_id for m in ordered)),
            ).hex

            group = RawTagSyncGroup(
                group_id=gid,
                event_folder=event_display,
                base_stem=base_stem,
                match_kind=match_kind,
                raw_members=raws,
                copy_members=copies,
                status='blocked' if ambiguous_reason else 'eligible',
                reason=ambiguous_reason or (
                    'Mesma pasta' if match_kind == 'same_folder' else 'Subpasta de formato do evento'
                ),
            )
            results.append(group)

    # Passo 4: Incluir TODOS os arquivos RAW que não entraram em nenhum par elegível (RAWs sem cópia JPG/PNG,
    # RAWs de outra câmera separados pelo filtro EXIF ou RAWs cujo par foi bloqueado) apenas para adicionar a tag "RAW"!
    if add_raw_tag and include_unpaired_raws:
        for _rel_folded, base_stem, m in all_records:
            if cancel_check and cancel_check():
                raise DedupCancelled('Análise cancelada.')
            if not m.is_raw or m.drive_id in raw_ids_in_eligible_pair:
                continue
            # Criar uma instância independente do membro para não colidir com eventual grupo bloqueado
            solo = RawSyncMember(
                drive_id=m.drive_id,
                name=m.name,
                ext=m.ext,
                is_raw=True,
                rel_folder=m.rel_folder,
                local_path=m.local_path,
                current_description=m.current_description,
                exif_time=m.exif_time,
                camera_model=m.camera_model,
                web_link=m.web_link,
                can_edit=m.can_edit,
            )
            merged_solo = compute_bidirectional_tags(
                [solo.current_description],
                is_raw_flags=[True],
                ignore_filename_tags=ignore_filename_tags,
                add_raw_tag=True,
            )
            new_desc, added = merged_solo[0]
            if not added:
                continue
            solo.proposed_description = new_desc
            solo.added_tags = added
            gid = uuid.uuid5(uuid.NAMESPACE_OID, f'raw_only|{solo.drive_id}').hex
            results.append(
                RawTagSyncGroup(
                    group_id=gid,
                    event_folder=solo.rel_folder,
                    base_stem=base_stem,
                    match_kind='raw_only',
                    raw_members=[solo],
                    copy_members=[],
                    status='eligible' if solo.can_edit else 'blocked',
                    reason=(
                        'Apenas arquivo bruto (adicionar tag RAW)'
                        if solo.can_edit
                        else 'Sem permissão de edição no Google Drive.'
                    ),
                )
            )

    # Ordenação: pares elegíveis primeiro (mesma pasta / subpasta de formato), depois RAWs isolados, depois bloqueados
    kind_order = {'same_folder': 0, 'format_subfolder': 1, 'raw_only': 2}
    results.sort(
        key=lambda g: (
            0 if g.status == 'eligible' else 1,
            kind_order.get(g.match_kind, 9),
            _fold(g.event_folder),
            g.base_stem,
        )
    )
    return results


def _update_local_sqlite_descriptions(
    db_name: str,
    updates: list[tuple[str, str, str]],
):
    """Atualiza `files` e `search_index` no banco local `file_index.db`."""
    if not updates or not os.path.exists(db_name):
        return
    norm_fn = SearchEngine(None).normalize_text
    conn = sqlite3.connect(db_name, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        has_search = 'search_index' in tables
        has_dlv = 'drive_link_validation' in tables
        with conn:
            for drive_id, local_path, new_desc in updates:
                target_file_ids: set[str] = set()
                if local_path:
                    target_file_ids.add(to_canonical_id(local_path))
                if drive_id:
                    target_file_ids.add(drive_id)
                    if has_dlv:
                        for row in conn.execute(
                            'SELECT local_file_id FROM drive_link_validation WHERE drive_id=?',
                            (drive_id,),
                        ):
                            if row[0]:
                                target_file_ids.add(row[0])
                for fid in target_file_ids:
                    existing = conn.execute(
                        'SELECT file_id, name, source FROM files WHERE file_id=?',
                        (fid,),
                    ).fetchone()
                    if not existing:
                        continue
                    conn.execute(
                        'UPDATE files SET description=? WHERE file_id=?',
                        (new_desc, fid),
                    )
                    if has_search:
                        nm = existing['name'] or ''
                        src = existing['source'] or 'local'
                        conn.execute('DELETE FROM search_index WHERE file_id=?', (fid,))
                        conn.execute(
                            'INSERT INTO search_index(name, description, normalized_name, '
                            'normalized_description, file_id, source) VALUES (?,?,?,?,?,?)',
                            (nm, new_desc, norm_fn(nm), norm_fn(new_desc), fid, src),
                        )
    finally:
        conn.close()


def execute_raw_tag_sync(
    groups: list[RawTagSyncGroup],
    *,
    db_name: str,
    catalog_api: DriveCatalog,
    scope_folder: str = '',
    ignore_filename_tags: bool = True,
    add_raw_tag: bool = True,
    cancel_check=None,
    progress_cb=None,
) -> dict:
    """Executa a sincronização de tags diretamente no Google Drive (com ETag) e no `file_index.db` local."""
    eligible_groups = [g for g in groups if g.status == 'eligible']
    if not eligible_groups:
        return {'groups_completed': 0, 'files_updated': 0, 'failed': 0, 'cancelled': False, 'errors': []}

    if progress_cb:
        progress_cb(0, 'Criando backup de segurança do catálogo local antes de gravar...')
    backup_path = local_backup(db_name)
    journal = RawTagSyncJournal(db_name)
    run_id = uuid.uuid4().hex
    journal.start_run(run_id, str(backup_path or ''), scope_folder, ignore_filename_tags)

    groups_completed = 0
    files_updated = 0
    failed = 0
    cancelled = False
    errors: list[str] = []
    pending_db_updates: list[tuple[str, str, str]] = []

    total = len(eligible_groups)
    try:
        for idx, group in enumerate(eligible_groups, 1):
            if cancel_check and cancel_check():
                cancelled = True
                break
            if progress_cb:
                progress_cb(
                    int((idx - 1) * 100 / max(1, total)),
                    f'Sincronizando item {idx}/{total}: {group.base_stem} ({group.event_folder})...',
                )
            try:
                ordered_members = group.all_members
                live_items: list[dict] = []
                for m in ordered_members:
                    live = catalog_api.get(m.drive_id)
                    if live.get('trashed'):
                        raise ValueError(f'O arquivo {m.name} foi movido para a lixeira no Drive.')
                    live_items.append(live)

                live_descriptions = [(it.get('description') or '') for it in live_items]
                is_raw_flags = [m.is_raw for m in ordered_members]
                recomputed = compute_bidirectional_tags(
                    live_descriptions,
                    is_raw_flags=is_raw_flags,
                    ignore_filename_tags=ignore_filename_tags,
                    add_raw_tag=add_raw_tag,
                )

                group_updated_count = 0
                for m_idx, member in enumerate(ordered_members):
                    live_item = live_items[m_idx]
                    old_desc = live_descriptions[m_idx]
                    new_desc, added = recomputed[m_idx]
                    if not added or new_desc == old_desc:
                        member.current_description = old_desc
                        member.proposed_description = new_desc
                        member.added_tags = []
                        continue
                    catalog_api.update(live_item, body={'description': new_desc})
                    member.current_description = new_desc
                    member.proposed_description = new_desc
                    member.added_tags = []
                    journal.record_update(
                        run_id=run_id,
                        group_id=group.group_id,
                        drive_id=member.drive_id,
                        local_path=member.local_path,
                        old_description=old_desc,
                        new_description=new_desc,
                        added_tags=added,
                        state='completed',
                    )
                    pending_db_updates.append((member.drive_id, member.local_path, new_desc))
                    files_updated += 1
                    group_updated_count += 1

                if len(pending_db_updates) >= 25:
                    _update_local_sqlite_descriptions(db_name, pending_db_updates)
                    pending_db_updates.clear()

                group.status = 'completed'
                group.reason = (
                    f'Concluído ({group_updated_count} arquivo(s) atualizado(s)).'
                    if group_updated_count
                    else 'Concluído (tags já estavam sincronizadas no Drive).'
                )
                groups_completed += 1
            except DedupCancelled:
                cancelled = True
                break
            except Exception as exc:
                failed += 1
                msg = str(exc)
                group.status = 'failed'
                group.reason = msg
                errors.append(f'{group.base_stem} ({group.event_folder}): {msg}')
                journal.record_update(
                    run_id=run_id,
                    group_id=group.group_id,
                    drive_id=group.raw_members[0].drive_id if group.raw_members else '',
                    local_path=group.raw_members[0].local_path if group.raw_members else '',
                    old_description='',
                    new_description='',
                    added_tags=[],
                    state='failed',
                    error=msg,
                )
    finally:
        if pending_db_updates:
            _update_local_sqlite_descriptions(db_name, pending_db_updates)
            pending_db_updates.clear()
        journal.finish_run(run_id, 'cancelled' if cancelled else ('completed_with_errors' if failed else 'completed'))

    if progress_cb:
        progress_cb(100, 'Sincronização de tags RAW ↔ cópias finalizada.')

    return {
        'groups_completed': groups_completed,
        'files_updated': files_updated,
        'failed': failed,
        'cancelled': cancelled,
        'errors': errors,
        'backup_path': str(backup_path or ''),
    }


class RawTagSyncWorker(QObject):
    """Worker em QThread para analisar e executar a sincronização de tags RAW ↔ JPG/PNG."""

    progress = pyqtSignal(int, str)
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(
        self,
        db_name: str,
        service,
        *,
        mode: str = 'scan',  # 'scan' | 'execute'
        scope_folder: str = '',
        ignore_filename_tags: bool = True,
        add_raw_tag: bool = True,
        include_unpaired_raws: bool = True,
        groups: list[RawTagSyncGroup] | None = None,
    ):
        super().__init__()
        self.db_name = db_name
        self.service = service
        self.mode = mode
        self.scope_folder = scope_folder
        self.ignore_filename_tags = ignore_filename_tags
        self.add_raw_tag = add_raw_tag
        self.include_unpaired_raws = include_unpaired_raws
        self.groups = list(groups or [])
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _is_cancelled(self) -> bool:
        return self._cancelled

    def run(self):
        lock_path = Path(self.db_name).resolve().parent / 'raw_tag_sync' / 'op.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(lock_path))
        lock.setStaleLockTime(0)
        if not lock.tryLock(100):
            self.failed.emit('Já existe uma sincronização de tags RAW ↔ JPG/PNG em andamento.')
            return
        try:
            config = ConfigManager()
            roots, drive_id = _resolve_active_roots_and_drive(config)
            if not drive_id:
                raise ValueError('ID do Google Drive não configurado.')
            if not self.service:
                raise ValueError('Conecte-se ao Google Drive antes de continuar.')
            if not roots:
                raise ValueError('Nenhuma pasta local configurada para o modo atual.')

            svc = private_service(self.service)

            if self.mode == 'scan':
                api = MigrationAPI(
                    svc,
                    drive_id,
                    cancelled=self._is_cancelled,
                    on_wait=lambda msg: self.progress.emit(-1, msg),
                )
                self.progress.emit(5, 'Baixando inventário atual do Google Drive (somente leitura)...')
                drive_items = list(api.listing('trashed = false', drive=drive_id, fields=RAW_SYNC_SCAN_FIELDS))
                groups = scan_raw_tag_sync_groups(
                    roots=roots,
                    drive_id=drive_id,
                    drive_items=drive_items,
                    scope_folder=self.scope_folder,
                    ignore_filename_tags=self.ignore_filename_tags,
                    add_raw_tag=self.add_raw_tag,
                    include_unpaired_raws=self.include_unpaired_raws,
                    cancel_check=self._is_cancelled,
                    progress_cb=lambda pct, msg: self.progress.emit(pct, msg),
                )
                self.progress.emit(100, 'Análise concluída.')
                self.finished.emit({'mode': 'scan', 'groups': groups, 'cancelled': self._is_cancelled()})
                return

            if config.is_read_only():
                raise ValueError('Ative o Modo Edição para gravar tags no Google Drive.')
            assert_queue_empty(self.db_name)

            catalog_api = DriveCatalog(
                svc,
                drive_id,
                cancelled=self._is_cancelled,
                on_wait=lambda msg: self.progress.emit(-1, msg),
            )
            stats = execute_raw_tag_sync(
                self.groups,
                db_name=self.db_name,
                catalog_api=catalog_api,
                scope_folder=self.scope_folder,
                ignore_filename_tags=self.ignore_filename_tags,
                add_raw_tag=self.add_raw_tag,
                cancel_check=self._is_cancelled,
                progress_cb=lambda pct, msg: self.progress.emit(pct, msg),
            )
            self.finished.emit({'mode': 'execute', **stats})
        except DedupCancelled:
            self.finished.emit({'mode': self.mode, 'cancelled': True, 'groups': self.groups})
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            lock.unlock()
