"""Resolucao conservadora de caminhos do Google Drive no Windows.

Os bancos compartilhados guardam caminhos absolutos por compatibilidade com o
restante do aplicativo. Este modulo permite localizar o mesmo caminho quando o
Google Drive esta montado em outra letra, sem fazer uma varredura recursiva dos
discos e sem escolher entre dois destinos existentes.
"""

from __future__ import annotations

import ntpath
import os
import string
from collections.abc import Iterable


SHARED_DRIVE_DIR_NAMES = ("Drives compartilhados", "Shared drives")
MY_DRIVE_DIR_NAMES = ("Meu Drive", "My Drive", "Google Drive")


def available_drive_letters(excluded: Iterable[str] | None = None) -> list[str]:
    """Retorna somente letras de unidade que existem nesta maquina."""
    if os.name != "nt":
        return []
    excluded_set = {
        str(drive).strip().rstrip('\\/').upper()
        for drive in (excluded or [])
    }
    return [
        f"{letter}:"
        for letter in string.ascii_uppercase
        if f"{letter}:" not in excluded_set
        if os.path.exists(f"{letter}:\\")
    ]


def _unique_paths(paths: Iterable[str]) -> list[str]:
    result = []
    seen = set()
    for path in paths:
        if not path:
            continue
        normalized = ntpath.normpath(path)
        key = ntpath.normcase(normalized)
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _unique_existing_locations(paths: Iterable[str]) -> list[str]:
    """Deduplica aliases PT/EN que apontam para o mesmo objeto no Drive."""
    result = []
    for path in _unique_paths(paths):
        duplicate = False
        for previous in result:
            try:
                if os.path.samefile(path, previous):
                    duplicate = True
                    break
            except OSError:
                continue
        if not duplicate:
            result.append(path)
    return result


def discover_insync_base_dirs(
    *,
    drive_letters: Iterable[str] | None = None,
) -> list[str]:
    """Descobre pastas-base sincronizadas pelo Insync ou Google Drive Desktop.

    Suporta padroes como:
    - ``C:\\Users\\<User>\\Insync\\<conta>\\Google Drive\\Drives compartilhados``
    - ``C:\\Users\\<User>\\Insync\\<conta>\\Google Drive``
    - ``<Drive>:\\Insync\\...``
    - ``C:\\Users\\<User>\\Google Drive\\...``
    """
    candidates: list[str] = []
    search_roots: list[str] = []

    user_home = os.path.expanduser("~")
    if user_home and os.path.isdir(user_home):
        for name in ("Insync", "Google Drive", "Meu Drive", "My Drive"):
            p = ntpath.join(user_home, name)
            if os.path.isdir(p):
                search_roots.append(p)

    drives = list(drive_letters) if drive_letters is not None else available_drive_letters()
    for drive in drives:
        for name in ("Insync", "Google Drive"):
            p = ntpath.join(drive + "\\", name)
            if os.path.isdir(p):
                search_roots.append(p)

    visited = set()
    for root in _unique_paths(search_roots):
        candidates.append(root)
        # Navegar ate 3 niveis de subpastas (conta -> Google Drive -> Drives compartilhados)
        queue = [(root, 0)]
        while queue:
            current, depth = queue.pop(0)
            norm_curr = ntpath.normcase(current)
            if norm_curr in visited:
                continue
            visited.add(norm_curr)
            for shared_name in (*SHARED_DRIVE_DIR_NAMES, *MY_DRIVE_DIR_NAMES):
                sub = ntpath.join(current, shared_name)
                if os.path.isdir(sub):
                    candidates.append(sub)
            if depth < 2:
                try:
                    with os.scandir(current) as it:
                        for entry in it:
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                            # Evitar descer em pastas de midia por engano (anos 19xx/20xx)
                            if entry.name.isdigit() and len(entry.name) == 4:
                                continue
                            if entry.name.startswith('.'):
                                continue
                            candidates.append(entry.path)
                            queue.append((entry.path, depth + 1))
                except OSError:
                    continue

    return _unique_existing_locations(candidates)


def _extract_shared_drive_tail(tail: str) -> str | None:
    """Extrai o sufixo apos ``Drives compartilhados`` / ``Shared drives``."""
    parts = [p for p in tail.split("\\") if p]
    shared_folded = {name.casefold() for name in SHARED_DRIVE_DIR_NAMES}
    for idx, part in enumerate(parts):
        if part.casefold() in shared_folded and idx + 1 < len(parts):
            return "\\".join(parts[idx + 1:])
    return None


def relocation_candidates(
    configured_path: str,
    *,
    drive_letters: Iterable[str] | None = None,
) -> list[str]:
    """Gera o mesmo caminho em outras letras, PT/EN do Drive e pastas Insync.

    A ordem sempre preserva o caminho configurado como primeira tentativa.
    Nenhuma existencia e presumida para as letras de unidade, o que torna a
    funcao testavel e util tambem para escolher um destino de publicacao cujo
    arquivo ainda nao existe.
    """
    if not configured_path:
        return []

    configured = ntpath.normpath(configured_path)
    configured_drive, tail = ntpath.splitdrive(configured)
    drives = list(drive_letters) if drive_letters is not None else available_drive_letters()
    if configured_drive:
        drives = [configured_drive, *drives]
    elif not drives:
        return [configured]

    tails = [tail]
    folded_parts = [part.casefold() for part in tail.split("\\")]
    for shared_name in SHARED_DRIVE_DIR_NAMES:
        for index, part in enumerate(folded_parts):
            if part in {name.casefold() for name in SHARED_DRIVE_DIR_NAMES}:
                original_parts = tail.split("\\")
                variant_parts = list(original_parts)
                variant_parts[index] = shared_name
                tails.append("\\".join(variant_parts))

    shared_subtail = _extract_shared_drive_tail(tail)
    if shared_subtail:
        # Tambem considerar montagem direta na raiz da letra (ex.: E:\Banco de Imagens)
        tails.append("\\" + shared_subtail)

    candidates = [
        ntpath.join(drive + "\\", candidate_tail.lstrip("\\/"))
        for drive in drives
        for candidate_tail in tails
    ]

    # Adicionar candidatos em pastas Insync existentes na maquina
    if shared_subtail:
        for insync_base in discover_insync_base_dirs(drive_letters=drives):
            candidates.append(ntpath.join(insync_base, shared_subtail))

    return _unique_paths(candidates)


def resolve_relocated_path(
    configured_path: str,
    *,
    drive_letters: Iterable[str] | None = None,
    allow_existing_parent: bool = False,
) -> str | None:
    """Resolve um caminho mudado de unidade ou Insync quando ha vencedor unico."""
    candidates = relocation_candidates(
        configured_path, drive_letters=drive_letters
    )
    existing = _unique_existing_locations(
        path for path in candidates if os.path.exists(path)
    )
    if len(existing) == 1:
        return existing[0]
    if len(existing) > 1:
        return None

    if allow_existing_parent:
        parent_matches = []
        parent_locations = []
        for path in candidates:
            parent = ntpath.dirname(path)
            if not os.path.isdir(parent):
                continue
            duplicate = False
            for previous_parent in parent_locations:
                try:
                    if os.path.samefile(parent, previous_parent):
                        duplicate = True
                        break
                except OSError:
                    continue
            if not duplicate:
                parent_matches.append(path)
                parent_locations.append(parent)
        if len(parent_matches) == 1:
            return parent_matches[0]
    return None


def discover_all_shared_folders(
    possible_names: Iterable[str],
    *,
    drive_letters: Iterable[str] | None = None,
    base_names: Iterable[str] = SHARED_DRIVE_DIR_NAMES,
) -> list[str]:
    """Retorna todas as localizacoes existentes (Google Drive Desktop + Insync)."""
    drives = list(drive_letters) if drive_letters is not None else available_drive_letters()
    names = list(possible_names)
    candidates: list[str] = []

    # 1. Google Drive for Desktop (<Drive>:\Drives compartilhados\<Nome>)
    for drive in drives:
        for base in base_names:
            for name in names:
                p = ntpath.join(drive + "\\", base, name)
                if os.path.isdir(p):
                    candidates.append(p)
        # 2. Montagem direta na raiz da letra (<Drive>:\<Nome>)
        for name in names:
            p = ntpath.join(drive + "\\", name)
            if os.path.isdir(p):
                candidates.append(p)

    # 3. Pastas do Insync / Google Drive em pastas de usuario ou outras unidades
    for insync_base in discover_insync_base_dirs(drive_letters=drives):
        base_leaf = ntpath.basename(insync_base).casefold()
        for name in names:
            if base_leaf == name.casefold() and os.path.isdir(insync_base):
                candidates.append(insync_base)
            p = ntpath.join(insync_base, name)
            if os.path.isdir(p):
                candidates.append(p)

    return _unique_existing_locations(candidates)


def discover_shared_folder(
    possible_names: Iterable[str],
    *,
    drive_letters: Iterable[str] | None = None,
    base_names: Iterable[str] = SHARED_DRIVE_DIR_NAMES,
) -> str | None:
    """Localiza uma pasta conhecida em Drive compartilhado ou Insync.

    Retorna ``None`` quando nenhuma ou mais de uma correspondencia existe. Isso
    evita apontar o banco para o acervo errado em maquinas com montagens duplas.
    """
    matches = discover_all_shared_folders(
        possible_names,
        drive_letters=drive_letters,
        base_names=base_names,
    )
    return matches[0] if len(matches) == 1 else None
