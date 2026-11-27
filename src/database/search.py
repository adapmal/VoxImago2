"""
Módulo de busca do VoxImago.MB

Responsável por:
- Implementar o mecanismo de busca full-text (FTS5) no banco SQLite
- Normalizar e interpretar termos de busca
- Fornecer a classe SearchEngine para consultas, filtros e paginação
"""

import re
import unicodedata
import sqlite3
import time
import json
import logging
from src.services.name_history import (
    build_drive_file_index,
    historical_file_ids,
    historical_file_ids_short,
    history_path,
    history_revision,
)


class SearchEngine:
    def __init__(self, indexer):
        self.indexer = indexer
        self._paged_cache = {}

    def clear_cache(self):
        self._paged_cache.clear()

    @staticmethod
    def _extract_inline_filters(search_term):
        filters = {}
        if 'is:starred' in search_term:
            filters['is_starred'] = True
            search_term = search_term.replace('is:starred', '')
        match_before = re.search(r'createdbefore:(\d{4}-\d{2}-\d{2})', search_term)
        match_after = re.search(r'createdafter:(\d{4}-\d{2}-\d{2})', search_term)
        import datetime
        if match_before:
            filters['created_before'] = datetime.datetime.strptime(
                match_before.group(1), '%Y-%m-%d')
            search_term = search_term.replace(match_before.group(0), '')
        if match_after:
            filters['created_after'] = datetime.datetime.strptime(
                match_after.group(1), '%Y-%m-%d')
            search_term = search_term.replace(match_after.group(0), '')
        return search_term, filters

    def parse_search_query(self, search_term):
        filters = {}
        terms = []
        exclude_terms = []
        or_groups = []
        quoted = re.findall(r'"([^"]+)"', search_term)
        for q in quoted:
            terms.append(q)
        search_term = re.sub(r'"[^"]+"', '', search_term)
        search_term, filters = self._extract_inline_filters(search_term)
        symbol_tag_pattern = r'([<&#@][\w\d]+)'
        symbol_tags = re.findall(symbol_tag_pattern, search_term)
        for tag in symbol_tags:
            terms.append(tag)
        search_term = re.sub(symbol_tag_pattern, '', search_term)
        exclude_terms += re.findall(r'(?<!\S)-([\w<&#@][\w\d]+)', search_term)
        search_term = re.sub(r'(?<!\S)-([\w<&#@][\w\d]+)', '', search_term)
        if ' or ' in search_term.lower():
            or_groups = [t.strip() for t in re.split(
                r'(?i)\s+or\s+', search_term) if t.strip()]
        elif ' and ' in search_term.lower():
            terms += [t.strip()
                      for t in re.split(r'(?i)\s+and\s+', search_term) if t.strip()]
        else:
            terms += [t for t in re.split(r'\s+', search_term) if t]
        terms = list(dict.fromkeys([t for t in terms if t]))
        exclude_terms = list(dict.fromkeys([t for t in exclude_terms if t]))
        return terms, exclude_terms, or_groups, filters

    @staticmethod
    def _find_grouping_close(text, open_idx):
        depth = 0
        in_quotes = False
        n = len(text)
        has_group_marker = False
        j = open_idx
        while j < n:
            ch = text[j]
            if ch == '"':
                in_quotes = not in_quotes
                has_group_marker = True
            elif not in_quotes:
                if ch == '(':
                    depth += 1
                    if j > open_idx:
                        has_group_marker = True
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        at_boundary = (j + 1 == n or text[j + 1].isspace() or text[j + 1] == ')')
                        inner = text[open_idx + 1:j]
                        if not at_boundary:
                            return None
                        if has_group_marker or any(c.isspace() for c in inner) or inner.startswith('-'):
                            return j
                        return None
            j += 1
        return None

    @classmethod
    def _tokenize_query(cls, text):
        tokens = []
        group_closes = set()
        i, n = 0, len(text)
        while i < n:
            if text[i].isspace():
                i += 1
                continue
            if i in group_closes:
                group_closes.remove(i)
                tokens.append(('RPAREN', ')'))
                i += 1
                continue
            if text[i] == '(':
                close_idx = cls._find_grouping_close(text, i)
                if close_idx is not None:
                    group_closes.add(close_idx)
                    tokens.append(('LPAREN', '('))
                    i += 1
                    continue
            if text[i] == '-' and i + 1 < n and (
                    text[i + 1] in ('"', '(') or re.match(r'[\w<&#@]', text[i + 1])):
                tokens.append(('MINUS', '-'))
                i += 1
                continue
            if text[i] == '"':
                end = text.find('"', i + 1)
                if end == -1:
                    inner = text[i + 1:].strip()
                    i = n
                else:
                    inner = text[i + 1:end].strip()
                    i = end + 1
                if inner:
                    tokens.append(('TERM', (inner, True)))
                continue
            start = i
            while i < n and not text[i].isspace() and text[i] != '"' and not (text[i] == ')' and i in group_closes):
                i += 1
            word = text[start:i]
            upper = word.upper()
            if upper in ('OR', 'AND', 'NOT'):
                tokens.append(('OP', upper))
            elif word:
                tokens.append(('TERM', (word, False)))
        return tokens

    @classmethod
    def _parse_query_ast(cls, text):
        tokens = cls._tokenize_query(text)
        pos = [0]

        def peek():
            return tokens[pos[0]] if pos[0] < len(tokens) else None

        def consume():
            tok = tokens[pos[0]]
            pos[0] += 1
            return tok

        def parse_primary():
            tok = peek()
            if not tok:
                return None
            if tok[0] == 'LPAREN':
                consume()
                inner = parse_or()
                if peek() and peek()[0] == 'RPAREN':
                    consume()
                return inner
            if tok[0] == 'TERM':
                consume()
                return ('TERM', tok[1][0], tok[1][1])
            return None

        def parse_not():
            tok = peek()
            if not tok:
                return None
            if tok[0] == 'MINUS' or tok == ('OP', 'NOT'):
                consume()
                child = parse_not()
                return ('NOT', child) if child is not None else None
            return parse_primary()

        def parse_and():
            while peek() and peek()[0] == 'OP' and peek()[1] in ('OR', 'AND'):
                consume()
            left = parse_not()
            if left is None:
                return None
            while True:
                tok = peek()
                if not tok or tok[0] == 'RPAREN' or tok == ('OP', 'OR'):
                    break
                if tok == ('OP', 'AND'):
                    consume()
                rhs = parse_not()
                if rhs is None:
                    break
                left = ('AND', left, rhs)
            return left

        def parse_or():
            left = parse_and()
            while peek() == ('OP', 'OR'):
                consume()
                rhs = parse_and()
                if rhs is None:
                    break
                left = rhs if left is None else ('OR', left, rhs)
            return left

        ast = None
        while pos[0] < len(tokens):
            part = parse_or()
            if part is not None:
                ast = part if ast is None else ('AND', ast, part)
            elif pos[0] < len(tokens):
                pos[0] += 1
        return ast

    @staticmethod
    def _fts_atom(term, quoted):
        stripped = term.rstrip('*')
        if (not quoted and len(stripped) >= 3 and re.fullmatch(r'\w+\*?', term)
                and stripped.upper() not in ('AND', 'OR', 'NOT', 'NEAR')):
            return term
        return '"' + term.replace('"', '""') + '"'

    def build_search_clause(self, search_term, source=None, explorer_special=False):
        """Constrói SQL e parâmetros consistentes entre listagem paginada e contagem."""
        norm_search_term = self.normalize_text(search_term)
        remaining_text, extra_filters = self._extract_inline_filters(norm_search_term)
        ast = self._parse_query_ast(remaining_text)
        if ast is None and not extra_filters:
            return None, [], extra_filters, True
        if ast is None:
            return None, [], extra_filters, False

        sql_norm_name = "replace(replace(replace(replace(replace(replace(replace(replace(COALESCE(name_normalized, ''), '_', ' '), '-', ' '), '.', ' '), ',', ' '), '(', ' '), ')', ' '), '[', ' '), ']', ' ')"
        sql_norm_desc = "replace(replace(replace(replace(replace(replace(replace(replace(COALESCE(description, ''), '_', ' '), '-', ' '), '.', ' '), ',', ' '), '(', ' '), ')', ' '), '[', ' '), ']', ' ')"
        has_history = bool(getattr(self.indexer, 'db_name', None)) and history_path(self.indexer.db_name).exists()
        drive_index_cache = [None]

        def get_drive_index():
            if drive_index_cache[0] is None:
                drive_index_cache[0] = build_drive_file_index(self.indexer)
            return drive_index_cache[0]

        def eval_node(node):
            kind = node[0]
            if kind == 'AND':
                l_sql, l_params = eval_node(node[1])
                r_sql, r_params = eval_node(node[2])
                return f'({l_sql} AND {r_sql})', l_params + r_params
            if kind == 'OR':
                l_sql, l_params = eval_node(node[1])
                r_sql, r_params = eval_node(node[2])
                return f'({l_sql} OR {r_sql})', l_params + r_params
            if kind == 'NOT':
                c_sql, c_params = eval_node(node[1])
                return f'NOT ({c_sql})', c_params

            _, val, quoted = node
            clean_alpha = re.sub(r'^[<&#@]', '', val)
            if len(clean_alpha) >= 3:
                atom = self._fts_atom(val, quoted)
                if explorer_special:
                    cur_sql = "file_id IN (SELECT file_id FROM search_index WHERE (search_index MATCH ? OR normalized_name MATCH ? OR normalized_description MATCH ?) AND source = 'local')"
                    cur_params = [atom, atom, atom]
                elif source:
                    cur_sql = "file_id IN (SELECT file_id FROM search_index WHERE (search_index MATCH ? OR normalized_name MATCH ? OR normalized_description MATCH ?) AND source = ?)"
                    cur_params = [atom, atom, atom, source]
                else:
                    cur_sql = "file_id IN (SELECT file_id FROM search_index WHERE search_index MATCH ? OR normalized_name MATCH ? OR normalized_description MATCH ?)"
                    cur_params = [atom, atom, atom]
                aliases = []
                if has_history:
                    try:
                        aliases = historical_file_ids(self.indexer, atom, drive_index=get_drive_index())
                    except (sqlite3.Error, ValueError, OSError) as exc:
                        logging.warning('Histórico de nomes indisponível na busca: %s', exc)
            else:
                cur_sql = f"(' ' || {sql_norm_name} || ' ' LIKE ? OR ' ' || {sql_norm_desc} || ' ' LIKE ?)"
                cur_params = [f'% {val} %', f'% {val} %']
                if explorer_special:
                    cur_sql = f"({cur_sql} AND source = 'local')"
                aliases = []
                if has_history:
                    try:
                        aliases = historical_file_ids_short(self.indexer, val, drive_index=get_drive_index())
                    except (sqlite3.Error, ValueError, OSError) as exc:
                        logging.warning('Histórico de nomes indisponível na busca curta: %s', exc)

            if aliases:
                alias_sql = 'file_id IN (SELECT value FROM json_each(?))'
                if explorer_special:
                    alias_sql = f"({alias_sql} AND source = 'local')"
                return f'({cur_sql} OR {alias_sql})', cur_params + [json.dumps(aliases)]
            return cur_sql, cur_params

        sql, params = eval_node(ast)
        return sql, params, extra_filters, False

    def normalize_text(self, text):
        if not text:
            return ""
        allowed_symbols = {'<', '>', '&', '#', '@'}
        normalized = ''.join(
            c for c in unicodedata.normalize('NFD', text.lower().strip())
            if unicodedata.category(c) != 'Mn' or c in allowed_symbols
        )
        return normalized

    def remove_accents(self, text):
        return self.normalize_text(text)

    def get_search_suggestions(self, search_term, search_all_sources, limit=10):
        self.indexer.ensure_conn()
        quoted_term = search_term.strip().replace('"', '""')
        query_term = f'"{quoted_term}*"'

        if search_all_sources:
            query = "SELECT name FROM search_index WHERE search_index MATCH ? OR normalized_name MATCH ? OR normalized_description MATCH ? ORDER BY rank LIMIT ?"
            self.indexer.cursor.execute(
                query, (query_term, query_term, query_term, limit))
        else:
            query = "SELECT name FROM search_index WHERE (search_index MATCH ? OR normalized_name MATCH ? OR normalized_description MATCH ?) AND source = 'local' ORDER BY rank LIMIT ?"
            self.indexer.cursor.execute(
                query, (query_term, query_term, query_term, limit))

        suggestions = [row[0] for row in self.indexer.cursor.fetchall()]
        return list(dict.fromkeys(suggestions))

    def load_files_paged(self, source, page, page_size, search_term=None, sort_by='name_asc', filter_type='all', folder_id=None, advanced_filters=None, explorer_special=False):
        self.indexer.ensure_conn()
        if self.indexer.conn is None:
            return []
        cache_key = (source, page, page_size, search_term, sort_by,
                     filter_type, folder_id, str(advanced_filters), explorer_special,
                     history_revision(self.indexer.db_name) if search_term else None)
        if cache_key in self._paged_cache:
            return self._paged_cache[cache_key]
        offset = page * page_size
        sort_map = {
            'name_asc': 'name ASC',
            'name_desc': 'name DESC',
            'size_asc': 'size ASC',
            'size_desc': 'size DESC',
            'created_desc': 'createdTime DESC',
            'created_asc': 'createdTime ASC'
        }
        order_by_clause = sort_map.get(sort_by, 'name ASC')
        files_where_clauses = []
        files_params = []
        if search_term:
            norm_search_term = self.normalize_text(search_term)
            print(
                f"Termo original: '{search_term}' -> Normalizado: '{norm_search_term}'")
            search_sql, search_params, extra_filters, empty_result = self.build_search_clause(
                search_term, source=source, explorer_special=explorer_special)
            if empty_result:
                self._paged_cache[cache_key] = []
                return []

            where_parts = []
            filter_params = []

            if search_sql:
                where_parts.append(search_sql)
                filter_params.extend(search_params)

            if source:
                where_parts.append("source = ?")
                filter_params.append(source)

            where_parts.append("(path IS NOT NULL AND path != '')")

            if extra_filters.get('is_starred'):
                where_parts.append("starred = 1")
            if extra_filters.get('created_before'):
                where_parts.append("createdTime <= ?")
                filter_params.append(int(time.mktime(extra_filters['created_before'].timetuple())))
            if extra_filters.get('created_after'):
                where_parts.append("createdTime >= ?")
                filter_params.append(int(time.mktime(extra_filters['created_after'].timetuple())))

            if advanced_filters:
                if advanced_filters.get('category'):
                    category = advanced_filters['category']
                    if category != '':
                        category_extensions = {
                            'images': ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg', '.ico', '.tif', '.tiff', '.heic', '.arw', '.cr2', '.nef', '.dng', '.raf', '.orf', '.srw'],
                            'videos': ['.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv', '.webm', '.m4v', '.3gp', '.mts', '.m2ts', '.ts', '.mpg', '.mpeg', '.vob'],
                            'media': ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg', '.ico', '.tif', '.tiff', '.heic', '.heif', '.arw', '.cr2', '.nef', '.dng', '.raf', '.orf', '.srw', '.rw2', '.pef', '.raw', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv', '.webm', '.m4v', '.3gp', '.mts', '.m2ts', '.ts', '.mpg', '.mpeg', '.vob'],
                            'documents': ['.pdf', '.doc', '.docx', '.txt', '.rtf', '.odt', '.xls', '.xlsx', '.ppt', '.pptx'],
                            'audios': ['.mp3', '.wav', '.flac', '.aac', '.ogg', '.wma', '.m4a']
                        }
                        if category in category_extensions:
                            extensions = category_extensions[category]
                            ext_conditions = []
                            for ext in extensions:
                                ext_conditions.append("name LIKE ?")
                                filter_params.append(f"%{ext}")
                            if ext_conditions:
                                where_parts.append(f"({' OR '.join(ext_conditions)})")

                if advanced_filters.get('extension'):
                    where_parts.append("name LIKE ?")
                    filter_params.append(f"%{advanced_filters['extension']}")

                if advanced_filters.get('is_starred'):
                    where_parts.append("starred = 1")

                if advanced_filters.get('created_before'):
                    where_parts.append("createdTime <= ?")
                    filter_params.append(int(time.mktime(advanced_filters['created_before'].timetuple())))

                if advanced_filters.get('created_after'):
                    where_parts.append("createdTime >= ?")
                    filter_params.append(int(time.mktime(advanced_filters['created_after'].timetuple())))

                if advanced_filters.get('path_filter'):
                    pf = advanced_filters['path_filter'].replace('\\', '/')
                    pf_win = pf.replace('/', '\\')
                    if search_term and search_term.strip():
                        # Modo 2: Busca recursiva dentro da pasta selecionada e de todas as suas subpastas
                        where_parts.append("(parentId IN (?, ?, ?, ?) OR path LIKE ? OR path LIKE ? OR parentId LIKE ? OR parentId LIKE ?)")
                        filter_params.extend([pf, pf.lower(), pf_win, pf_win.lower(), f"{pf}/%", f"{pf_win}\\%", f"{pf}/%", f"{pf_win}\\%"])
                    else:
                        # Modo 3: Navegação de pasta direta sem busca (apenas o nível direto)
                        where_parts.append("(parentId IN (?, ?, ?, ?) OR ((path LIKE ? OR path LIKE ?) AND (path NOT LIKE ? AND path NOT LIKE ?)))")
                        filter_params.extend([pf, pf.lower(), pf_win, pf_win.lower(), f"{pf}/%", f"{pf_win}\\%", f"{pf}/%/%", f"{pf_win}\\%\\%"])

                if advanced_filters.get('sandbox_filter'):
                    sf = advanced_filters['sandbox_filter'].replace('\\', '/')
                    sf_win = sf.replace('/', '\\')
                    where_parts.append("(path = ? OR path = ? OR path LIKE ? OR path LIKE ?)")
                    filter_params.extend([sf, sf_win, f"{sf}/%", f"{sf_win}\\%"])

                if advanced_filters.get('tag_filter'):
                    tf = advanced_filters['tag_filter']
                    if tf == 'tagged':
                        where_parts.append("(description IS NOT NULL AND TRIM(description) != '')")
                    elif tf == 'untagged':
                        where_parts.append("(description IS NULL OR TRIM(description) = '')")

            details_query = f"SELECT file_id, name, path, mimeType, source, description, thumbnailLink, thumbnailPath, size, modifiedTime, createdTime, parentId, starred, webContentLink, thumbnailRotation FROM files"
            
            # Remover o bloqueio global de pastas na busca normal
            # where_parts.append("mimeType != 'folder'")
            # where_parts.append("mimeType != 'application/vnd.google-apps.folder'")
            
            if where_parts:
                details_query += " WHERE " + " AND ".join(where_parts)

            self.indexer.cursor.execute(details_query, filter_params)
            rows = self.indexer.cursor.fetchall()
            result = self.indexer._build_file_objects_from_search(rows)
            if sort_by == 'name_asc':
                result = sorted(
                    result, key=lambda x: x.get('name', '').lower())
            elif sort_by == 'name_desc':
                result = sorted(result, key=lambda x: x.get(
                    'name', '').lower(), reverse=True)
            elif sort_by == 'created_desc':
                result = sorted(result, key=lambda x: x.get(
                    'createdTime', 0), reverse=True)
            elif sort_by == 'created_asc':
                result = sorted(result, key=lambda x: x.get('createdTime', 0))
            elif sort_by == 'modified_desc':
                result = sorted(result, key=lambda x: x.get(
                    'modifiedTime', 0), reverse=True)
            elif sort_by == 'modified_asc':
                result = sorted(result, key=lambda x: x.get('modifiedTime', 0))
            elif sort_by == 'size_asc':
                result = sorted(result, key=lambda x: x.get('size', 0))
            elif sort_by == 'size_desc':
                result = sorted(result, key=lambda x: x.get(
                    'size', 0), reverse=True)
            start = offset
            end = offset + page_size
            paged_result = result[start:end]
            self._paged_cache[cache_key] = paged_result
            return paged_result
        else:
            if source:
                files_where_clauses.append("source=?")
                files_params.append(source)
            if folder_id:
                files_where_clauses.append("parentId=?")
                files_params.append(folder_id)
            if advanced_filters and advanced_filters.get('is_root'):
                files_where_clauses.append(
                    "(parentId IS NULL OR parentId = '')")
            
            # Remove global ban of folders, so explorer works as intended
            # files_where_clauses.append("mimeType NOT IN ('folder', 'application/vnd.google-apps.folder')")
            if filter_type == 'image':
                files_where_clauses.append("mimeType LIKE 'image/%'")
            elif filter_type == 'document':
                files_where_clauses.append(
                    "(mimeType LIKE 'application/vnd.google-apps.document' OR mimeType LIKE 'application/pdf' OR mimeType LIKE '%wordprocessingml.document%')")
            elif filter_type == 'spreadsheet':
                files_where_clauses.append(
                    "(mimeType LIKE 'application/vnd.google-apps.spreadsheet' OR mimeType LIKE '%spreadsheetml.sheet%')")
            elif filter_type == 'presentation':
                files_where_clauses.append(
                    "(mimeType LIKE 'application/vnd.google-apps.presentation' OR mimeType LIKE '%presentationml.presentation%')")
            elif filter_type == 'folder':
                files_where_clauses.append(
                    "mimeType = 'folder' OR mimeType = 'application/vnd.google-apps.folder'")
            if advanced_filters:
                if 'size_min' in advanced_filters and advanced_filters['size_min']:
                    files_where_clauses.append("size >= ?")
                    files_params.append(
                        advanced_filters['size_min'] * 1024 * 1024)
                if 'size_max' in advanced_filters and advanced_filters['size_max']:
                    files_where_clauses.append("size <= ?")
                    files_params.append(
                        advanced_filters['size_max'] * 1024 * 1024)
                if 'modified_after' in advanced_filters and advanced_filters['modified_after']:
                    files_where_clauses.append("modifiedTime >= ?")
                    files_params.append(
                        int(time.mktime(advanced_filters['modified_after'].timetuple())))
                if 'created_after' in advanced_filters and advanced_filters['created_after']:
                    files_where_clauses.append("createdTime >= ?")
                    files_params.append(
                        int(time.mktime(advanced_filters['created_after'].timetuple())))
                if 'created_before' in advanced_filters and advanced_filters['created_before']:
                    files_where_clauses.append("createdTime <= ?")
                    files_params.append(
                        int(time.mktime(advanced_filters['created_before'].timetuple())))
                if 'extension' in advanced_filters and advanced_filters['extension']:
                    files_where_clauses.append("name LIKE ?")
                    files_params.append(f"%{advanced_filters['extension']}")
                    files_where_clauses.append("(mimeType != 'folder' OR mimeType IS NULL)")
                    files_where_clauses.append("(mimeType != 'application/vnd.google-apps.folder' OR mimeType IS NULL)")
                if 'is_starred' in advanced_filters and advanced_filters['is_starred']:
                    files_where_clauses.append("starred = 1")
                if advanced_filters and advanced_filters.get('category'):
                    category = advanced_filters['category']
                    if category != '':
                        category_extensions = {
                            'images': ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg', '.ico', '.tif', '.tiff', '.heic', '.arw', '.cr2', '.nef', '.dng', '.raf', '.orf', '.srw'],
                            'videos': ['.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv', '.webm', '.m4v', '.3gp', '.mts', '.m2ts', '.ts', '.mpg', '.mpeg', '.vob'],
                            'media': ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg', '.ico', '.tif', '.tiff', '.heic', '.heif', '.arw', '.cr2', '.nef', '.dng', '.raf', '.orf', '.srw', '.rw2', '.pef', '.raw', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv', '.webm', '.m4v', '.3gp', '.mts', '.m2ts', '.ts', '.mpg', '.mpeg', '.vob'],
                            'documents': ['.pdf', '.doc', '.docx', '.txt', '.rtf', '.odt', '.xls', '.xlsx', '.ppt', '.pptx'],
                            'audios': ['.mp3', '.wav', '.flac', '.aac', '.ogg', '.wma', '.m4a']
                        }
                        if category in category_extensions:
                            extensions = category_extensions[category]
                            ext_conditions = []
                            for ext in extensions:
                                ext_conditions.append("name LIKE ?")
                                files_params.append(f"%{ext}")
                            if ext_conditions:
                                files_where_clauses.append(
                                    f"({' OR '.join(ext_conditions)})")

                if advanced_filters.get('path_filter'):
                    pf = advanced_filters['path_filter'].replace('\\', '/')
                    pf_win = pf.replace('/', '\\')
                    files_where_clauses.append("(parentId IN (?, ?, ?, ?) OR ((path LIKE ? OR path LIKE ?) AND (path NOT LIKE ? AND path NOT LIKE ?)))")
                    files_params.extend([pf, pf.lower(), pf_win, pf_win.lower(), f"{pf}/%", f"{pf_win}\\%", f"{pf}/%/%", f"{pf_win}\\%\\%"])

                if advanced_filters.get('sandbox_filter'):
                    sf = advanced_filters['sandbox_filter'].replace('\\', '/')
                    sf_win = sf.replace('/', '\\')
                    files_where_clauses.append("(path = ? OR path = ? OR path LIKE ? OR path LIKE ?)")
                    files_params.extend([sf, sf_win, f"{sf}/%", f"{sf_win}\\%"])

                if advanced_filters.get('tag_filter'):
                    tf = advanced_filters['tag_filter']
                    if tf == 'tagged':
                        files_where_clauses.append("(description IS NOT NULL AND TRIM(description) != '')")
                    elif tf == 'untagged':
                        files_where_clauses.append("(description IS NULL OR TRIM(description) = '')")
            query = f"SELECT file_id, name, path, mimeType, source, description, thumbnailLink, thumbnailPath, size, modifiedTime, createdTime, parentId, starred, webContentLink, thumbnailRotation FROM files WHERE {' AND '.join(files_where_clauses)} ORDER BY {order_by_clause} LIMIT ? OFFSET ?"
            if explorer_special:
                query = query.replace("WHERE", "WHERE source = 'local' AND")
            files_params.extend([page_size, offset])
            self.indexer.cursor.execute(query, files_params)
            rows = self.indexer.cursor.fetchall()
            files = [
                {
                    'id': row[0],
                    'name': row[1],
                    'path': row[2],
                    'mimeType': row[3],
                    'source': row[4],
                    'description': row[5],
                    'thumbnailLink': row[6],
                    'thumbnailPath': row[7],
                    'size': row[8],
                    'modifiedTime': row[9],
                    'createdTime': row[10],
                    'parentId': row[11],
                    'starred': bool(row[12]),
                    'webContentLink': row[13] if len(row) > 13 else '',
                    'thumbnailRotation': int(row[14] or 0) if len(row) > 14 else 0,
                } for row in rows
            ]
            self._paged_cache[cache_key] = files
            return files

    def debug_search_normalization(self, search_term):
        if not search_term:
            print("\n⚠️ DEBUG: Nenhum termo de busca fornecido")
            return

        print(f"\n" + "="*60)
        print(f"🔍 DEBUG BUSCA NORMALIZADA - {search_term}")
        print(f"="*60)

        normalized = self.normalize_text(search_term)
        print(f"📝 Termo original: '{search_term}'")
        print(f"📝 Termo normalizado: '{normalized}'")
        print(
            f"📝 Mudança detectada: {'Sim' if search_term.lower() != normalized else 'Não'}")

        try:
            self.indexer.cursor.execute('SELECT COUNT(*) FROM search_index')
            total_indexed = self.indexer.cursor.fetchone()[0]
            print(f"💾 Total de arquivos no índice: {total_indexed:,}")
        except Exception as e:
            print(f"❌ Erro ao acessar índice: {e}")
            return

        print(f"\n🔍 TESTE 1: Busca com termo original")
        original_results = self.load_files_paged(
            source=None, page=0, page_size=8, search_term=search_term,
            sort_by='name_asc', filter_type='all'
        )
        print(f"   Resultados encontrados: {len(original_results)}")

        print(f"\n🔍 TESTE 2: Busca com termo normalizado")
        normalized_results = self.load_files_paged(
            source=None, page=0, page_size=8, search_term=normalized,
            sort_by='name_asc', filter_type='all'
        )
        print(f"   Resultados encontrados: {len(normalized_results)}")

        consistent = len(original_results) == len(normalized_results)
        status = "✅ CONSISTENTE" if consistent else "⚠️ INCONSISTENTE"
        print(f"\n🎯 STATUS DA NORMALIZAÇÃO: {status}")
        if original_results:
            print(f"\n📋 AMOSTRAS (máximo 5):")
            for i, result in enumerate(original_results[:5]):
                name = result.get('name', 'N/A')
                source = result.get('source', 'N/A')
                print(f"   {i+1}. [{source}] {name}")
        else:
            print(f"\n📋 Nenhum resultado encontrado")

        print(f"\n🔧 DEBUG AVANÇADO FTS5:")
        try:
            self.indexer.cursor.execute(
                'SELECT name FROM search_index WHERE search_index MATCH ? LIMIT 3',
                (f'"{normalized}"',)
            )
            fts_results = self.indexer.cursor.fetchall()
            print(
                f"   FTS5 direto com \"{normalized}\": {len(fts_results)} resultados")
            if normalized != search_term.lower():
                accent_pattern = f'%{search_term.lower()}%'
                self.indexer.cursor.execute(
                    'SELECT name FROM files WHERE LOWER(name) LIKE ? LIMIT 3',
                    (accent_pattern,)
                )
                accent_files = self.indexer.cursor.fetchall()
                print(
                    f"   Arquivos com acentos similares: {len(accent_files)}")
                for file in accent_files:
                    original_name = file[0]
                    normalized_name = self.normalize_text(original_name)
                    print(f"     '{original_name}' -> '{normalized_name}'")

        except Exception as e:
            print(f"   ❌ Erro no debug avançado: {e}")

        print(f"\n" + "="*60)
        print(f"💡 DICA: Use termos como 'formação', 'ação', 'coração' para testar")
        print(f"="*60)
