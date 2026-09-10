"""Correspondencia conservadora entre arquivos locais e do Google Drive."""

from __future__ import annotations

import logging
import os
import re
import string
import unicodedata
from dataclasses import dataclass, replace
from urllib.parse import parse_qs, urlparse

from src.database.search import SearchEngine
from src.drive.link_validation import load_validations, validation_blocks


DRIVE_SHORTCUT_EXTENSIONS = [
    '.gdoc', '.gsheet', '.gslides', '.gdraw', '.gform']
_DRIVE_ID_IN_PATH = re.compile(r"/(?:d|folders)/([A-Za-z0-9_-]+)(?:/|$)")


@dataclass(frozen=True)
class LocalCandidate:
    file_id: str
    name: str
    path: str
    size: int
    mime_type: str
    web_link: str
    validation: object | None = None


@dataclass(frozen=True)
class MatchResult:
    """Resultado explicavel; somente ``matched`` autoriza sobrescrever metadados."""

    status: str
    reason: str
    local_id: str | None = None
    candidate_count: int = 0
    hierarchy_score: int = 0

    @property
    def matched(self) -> bool:
        return self.status == 'matched' and bool(self.local_id)


def normalize_aggressive(name):
    if not name:
        return ''

    name_wo_ext, _ext = os.path.splitext(name)
    name_norm = unicodedata.normalize('NFD', name_wo_ext)
    name_no_accents = ''.join(
        c for c in name_norm if unicodedata.category(c) != 'Mn')
    table = str.maketrans('', '', string.punctuation + string.whitespace)
    return name_no_accents.translate(table).lower()


def normalize_name_only(name):
    if not name:
        return ''

    name_base = os.path.splitext(name)[0]
    name_norm = unicodedata.normalize('NFD', name_base)
    name_clean = ''.join(
        c for c in name_norm if unicodedata.category(c) != 'Mn')
    table = str.maketrans('', '', string.punctuation + string.whitespace)
    return name_clean.translate(table).lower()


def _normalize_component(value):
    if not value:
        return ''
    normalized = unicodedata.normalize('NFD', str(value).strip().casefold())
    return ''.join(char for char in normalized if unicodedata.category(char) != 'Mn')


def _positive_size(value):
    try:
        size = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return size if size > 0 else 0


def extract_drive_file_id(link):
    """Extrai um ID somente de formatos conhecidos de URL do Google Drive."""
    if not isinstance(link, str) or not link.strip():
        return None
    try:
        parsed = urlparse(link.strip())
    except ValueError:
        return None
    if parsed.scheme not in ('http', 'https'):
        return None
    host = (parsed.hostname or '').casefold()
    if host not in ('drive.google.com', 'docs.google.com'):
        return None

    path_match = _DRIVE_ID_IN_PATH.search(parsed.path)
    if path_match:
        return path_match.group(1)
    query_id = parse_qs(parsed.query).get('id', [])
    if query_id and re.fullmatch(r'[A-Za-z0-9_-]+', query_id[0]):
        return query_id[0]
    return None


def select_unique_drive_candidate_by_size(candidates, expected_size):
    """Retorna candidato Drive apenas quando o tamanho exato produz um unico vencedor."""
    local_size = _positive_size(expected_size)
    if not local_size:
        return None
    size_matches = [
        candidate for candidate in candidates
        if _positive_size(candidate.get('size')) == local_size
    ]
    return size_matches[0] if len(size_matches) == 1 else None


class DriveHierarchyResolver:
    """Resolve e memoriza a cadeia de pastas Drive, da raiz ate a pasta pai."""

    def __init__(self, service=None, max_depth=64):
        self.service = service
        self.max_depth = max_depth
        self._node_cache = {}
        self._path_cache = {}

    def _node_from_database(self, folder_id, cursor):
        if cursor is None:
            return None
        cursor.execute(
            "SELECT name, parentId FROM files WHERE file_id = ? AND source = 'drive' LIMIT 1",
            (folder_id,),
        )
        row = cursor.fetchone()
        return (row[0] or '', row[1] or '') if row else None

    def _node_from_service(self, folder_id):
        if not self.service:
            return None
        try:
            info = self.service.files().get(
                fileId=folder_id,
                supportsAllDrives=True,
                fields='id,name,parents',
            ).execute()
            parents = info.get('parents') or []
            return info.get('name', ''), (parents[0] if parents else '')
        except Exception as exc:
            logging.debug("Nao foi possivel resolver a pasta Drive %s: %s", folder_id, exc)
            return None

    def _get_node(self, folder_id, cursor):
        if folder_id in self._node_cache:
            return self._node_cache[folder_id]
        node = self._node_from_database(folder_id, cursor)
        if node is None:
            node = self._node_from_service(folder_id)
        self._node_cache[folder_id] = node
        return node

    def resolve(self, drive_file, cursor=None):
        supplied_path = drive_file.get('parent_path') or drive_file.get('drive_parent_path')
        if supplied_path:
            if isinstance(supplied_path, (list, tuple)):
                return tuple(str(part) for part in supplied_path if str(part).strip())
            return tuple(
                part for part in re.split(r'[\\/]+', str(supplied_path)) if part
            )

        supplied_names = drive_file.get('parent_names')
        if isinstance(supplied_names, (list, tuple)) and supplied_names:
            return tuple(str(part) for part in supplied_names if str(part).strip())

        parents = drive_file.get('parents') or []
        parent_id = drive_file.get('parentId') or (parents[0] if parents else '')
        if not parent_id:
            parent_name = drive_file.get('parent_name')
            return (parent_name,) if parent_name else ()
        if parent_id in self._path_cache:
            return self._path_cache[parent_id]

        names_leaf_first = []
        visited = set()
        current_id = parent_id
        for _depth in range(self.max_depth):
            if not current_id or current_id in visited:
                break
            visited.add(current_id)
            node = self._get_node(current_id, cursor)
            if not node:
                break
            name, next_parent_id = node
            if name:
                names_leaf_first.append(name)
            current_id = next_parent_id

        resolved = tuple(reversed(names_leaf_first))
        if not resolved and drive_file.get('parent_name'):
            resolved = (drive_file['parent_name'],)
        self._path_cache[parent_id] = resolved
        return resolved


def _candidate_from_row(row):
    return LocalCandidate(
        file_id=row[0],
        name=row[1] or '',
        path=row[2] or '',
        size=_positive_size(row[3]),
        mime_type=row[4] or '',
        web_link=row[5] or '',
    )


def _fetch_candidates(cursor, where_clause, params):
    cursor.execute(
        "SELECT file_id, name, path, size, mimeType, webContentLink "
        "FROM files WHERE source = 'local' AND " + where_clause,
        params,
    )
    seen = set()
    candidates = []
    for row in cursor.fetchall():
        candidate = _candidate_from_row(row)
        if candidate.file_id not in seen:
            candidates.append(candidate)
            seen.add(candidate.file_id)
    validations = load_validations(cursor, [candidate.file_id for candidate in candidates])
    if validations:
        candidates = [
            replace(candidate, validation=validations.get(candidate.file_id))
            for candidate in candidates
        ]
    return candidates


def _filter_allowed_roots(candidates, allowed_roots):
    if allowed_roots is None:
        return candidates
    normalized_roots = [
        os.path.normcase(os.path.abspath(os.path.normpath(root)))
        for root in allowed_roots if isinstance(root, str) and root.strip()
    ]
    filtered = []
    for candidate in candidates:
        if not candidate.path or not os.path.isabs(candidate.path):
            continue
        candidate_path = os.path.normcase(
            os.path.abspath(os.path.normpath(candidate.path))
        )
        for root in normalized_roots:
            try:
                if os.path.commonpath((candidate_path, root)) == root:
                    filtered.append(candidate)
                    break
            except ValueError:
                continue
    return filtered


def _filter_unlinked_or_same_drive(candidates, drive_id):
    eligible = []
    for candidate in candidates:
        if validation_blocks(candidate.validation, drive_id):
            continue
        if not candidate.web_link:
            eligible.append(candidate)
            continue
        if drive_id and extract_drive_file_id(candidate.web_link) == drive_id:
            eligible.append(candidate)
    return eligible


def build_local_link_index(cursor, allowed_roots=None):
    """Indexa uma vez os links locais para evitar um ``LIKE`` por arquivo Drive."""
    candidates = _fetch_candidates(
        cursor,
        "COALESCE(webContentLink, '') <> ''",
        (),
    )
    candidates = _filter_allowed_roots(candidates, allowed_roots)
    link_index = {}
    for candidate in candidates:
        drive_id = extract_drive_file_id(candidate.web_link)
        if drive_id:
            link_index.setdefault(drive_id, []).append(candidate)
    return link_index


def _path_components(path):
    if not path:
        return ()
    parent = os.path.dirname(os.path.normpath(path)).replace('\\', '/')
    return tuple(part for part in parent.split('/') if part and not part.endswith(':'))


def _common_suffix_score(local_parts, drive_parts):
    score = 0
    for local_part, drive_part in zip(reversed(local_parts), reversed(drive_parts)):
        if _normalize_component(local_part) != _normalize_component(drive_part):
            break
        score += 1
    return score


def _select_by_hierarchy(candidates, drive_file, cursor, resolver):
    drive_parts = resolver.resolve(drive_file, cursor)
    if not drive_parts:
        return None, 0
    scored = [
        (candidate, _common_suffix_score(_path_components(candidate.path), drive_parts))
        for candidate in candidates
    ]
    best_score = max((score for _candidate, score in scored), default=0)
    if best_score <= 0:
        return None, 0
    winners = [candidate for candidate, score in scored if score == best_score]
    return (winners[0], best_score) if len(winners) == 1 else (None, best_score)


def _linked_candidates(drive_id, cursor, allowed_roots, link_index=None):
    if not drive_id:
        return []
    if link_index is not None:
        return list(link_index.get(drive_id, ()))
    escaped = drive_id.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    candidates = _fetch_candidates(
        cursor,
        "webContentLink LIKE ? ESCAPE '\\'",
        (f'%{escaped}%',),
    )
    exact = [
        candidate for candidate in candidates
        if extract_drive_file_id(candidate.web_link) == drive_id
    ]
    return _filter_allowed_roots(exact, allowed_roots)


def _result_from_size_and_hierarchy(
    candidates,
    drive_file,
    drive_size,
    cursor,
    resolver,
    *,
    unique_reason,
    hierarchy_reason,
):
    size_matches = [candidate for candidate in candidates if candidate.size == drive_size]
    if not size_matches:
        return MatchResult('conflict', 'size_mismatch', candidate_count=len(candidates))
    if len(size_matches) == 1:
        return MatchResult(
            'matched', unique_reason, size_matches[0].file_id, len(candidates)
        )

    winner, score = _select_by_hierarchy(size_matches, drive_file, cursor, resolver)
    if winner:
        return MatchResult(
            'matched', hierarchy_reason, winner.file_id, len(candidates), score
        )
    return MatchResult(
        'ambiguous', 'ambiguous_same_name_size', candidate_count=len(size_matches),
        hierarchy_score=score,
    )


def match_drive_to_local(
    drive_file,
    local_files_cursor,
    drive_service=None,
    *,
    hierarchy_resolver=None,
    allowed_roots=None,
    link_index=None,
):
    """Decide um unico match seguro ou explica por que a fusao foi recusada."""
    drive_name = str(drive_file.get('name') or '').strip()
    drive_id = str(drive_file.get('id') or '').strip()
    drive_size = _positive_size(drive_file.get('size'))
    drive_mime = str(drive_file.get('mimeType') or '')
    resolver = hierarchy_resolver or DriveHierarchyResolver(drive_service)

    if not drive_name:
        return MatchResult('no_match', 'missing_name')
    if drive_mime == 'application/vnd.google-apps.folder':
        return MatchResult('no_match', 'drive_folder')
    if not drive_size:
        return MatchResult('no_match', 'missing_drive_size')

    linked_all = _linked_candidates(
        drive_id, local_files_cursor, allowed_roots, link_index=link_index
    )
    linked = [
        candidate for candidate in linked_all
        if not validation_blocks(candidate.validation, drive_id)
    ]
    if linked:
        return _result_from_size_and_hierarchy(
            linked,
            drive_file,
            drive_size,
            local_files_cursor,
            resolver,
            unique_reason='verified_existing_link',
            hierarchy_reason='verified_existing_link_hierarchy',
        )
    if linked_all:
        return MatchResult(
            'conflict', 'quarantined_existing_link',
            candidate_count=len(linked_all),
        )

    exact_name = _fetch_candidates(
        local_files_cursor,
        "LOWER(name) = LOWER(?)",
        (drive_name,),
    )
    exact_name = _filter_allowed_roots(exact_name, allowed_roots)
    exact_name = _filter_unlinked_or_same_drive(exact_name, drive_id)
    if exact_name:
        return _result_from_size_and_hierarchy(
            exact_name,
            drive_file,
            drive_size,
            local_files_cursor,
            resolver,
            unique_reason='exact_name_size',
            hierarchy_reason='exact_name_size_hierarchy',
        )

    normalized_name = SearchEngine(None).normalize_text(drive_name)
    if not normalized_name:
        return MatchResult('no_match', 'name_not_found')
    normalized = _fetch_candidates(
        local_files_cursor,
        "name_normalized = ?",
        (normalized_name,),
    )
    normalized = _filter_allowed_roots(normalized, allowed_roots)
    normalized = _filter_unlinked_or_same_drive(normalized, drive_id)
    drive_extension = os.path.splitext(drive_name)[1].casefold()
    normalized = [
        candidate for candidate in normalized
        if os.path.splitext(candidate.name)[1].casefold() == drive_extension
        and candidate.size == drive_size
    ]
    if not normalized:
        return MatchResult('no_match', 'name_or_size_not_found')

    winner, score = _select_by_hierarchy(
        normalized, drive_file, local_files_cursor, resolver
    )
    if winner:
        return MatchResult(
            'matched', 'normalized_name_size_hierarchy', winner.file_id,
            len(normalized), score,
        )
    return MatchResult(
        'ambiguous', 'normalized_match_requires_hierarchy',
        candidate_count=len(normalized), hierarchy_score=score,
    )


def find_local_matches(
    drive_file,
    local_files_cursor,
    drive_service=None,
    *,
    hierarchy_resolver=None,
    allowed_roots=None,
    link_index=None,
):
    """API compativel: retorna zero ou um ID; jamais escolhe o primeiro empate."""
    result = match_drive_to_local(
        drive_file,
        local_files_cursor,
        drive_service,
        hierarchy_resolver=hierarchy_resolver,
        allowed_roots=allowed_roots,
        link_index=link_index,
    )
    return [result.local_id] if result.matched else []
