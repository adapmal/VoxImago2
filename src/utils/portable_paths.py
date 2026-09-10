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


def relocation_candidates(
    configured_path: str,
    *,
    drive_letters: Iterable[str] | None = None,
) -> list[str]:
    """Gera o mesmo caminho em outras letras e nos nomes PT/EN do Drive.

    A ordem sempre preserva o caminho configurado como primeira tentativa.
    Nenhuma existencia e presumida aqui, o que torna a funcao testavel e util
    tambem para escolher um destino de publicacao cujo arquivo ainda nao existe.
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

    return _unique_paths(
        ntpath.join(drive + "\\", candidate_tail.lstrip("\\/"))
        for drive in drives
        for candidate_tail in tails
    )


def resolve_relocated_path(
    configured_path: str,
    *,
    drive_letters: Iterable[str] | None = None,
    allow_existing_parent: bool = False,
) -> str | None:
    """Resolve um caminho mudado de unidade apenas quando ha vencedor unico."""
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


def discover_shared_folder(
    possible_names: Iterable[str],
    *,
    drive_letters: Iterable[str] | None = None,
    base_names: Iterable[str] = SHARED_DRIVE_DIR_NAMES,
) -> str | None:
    """Localiza uma pasta conhecida no nivel raiz de um Drive compartilhado.

    Retorna ``None`` quando nenhuma ou mais de uma correspondencia existe. Isso
    evita apontar o banco para o acervo errado em maquinas com montagens duplas.
    """
    drives = list(drive_letters) if drive_letters is not None else available_drive_letters()
    matches = _unique_existing_locations(
        ntpath.join(drive + "\\", base, name)
        for drive in drives
        for base in base_names
        for name in possible_names
        if os.path.isdir(ntpath.join(drive + "\\", base, name))
    )
    return matches[0] if len(matches) == 1 else None
