"""Regras determinísticas de nomes; não consulta nem altera arquivos locais."""
import ntpath
import unicodedata

from src.utils.path_validation import validate_path_component


def name_key(name):
    return unicodedata.normalize('NFC', name).casefold()


def numbered_name(name, occupied):
    stem, extension = ntpath.splitext(name)
    number = 0
    while name_key(f'{stem}_{number}{extension}') in occupied:
        number += 1
    result = f'{stem}_{number}{extension}'
    occupied.add(name_key(result))
    return result


def safe_windows_name(name):
    valid, _ = validate_path_component(name)
    return valid and len(name) <= 240
