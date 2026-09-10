"""Valida nomes e limita operacoes locais as raizes configuradas."""

import os


INVALID_WINDOWS_NAME_CHARS = set('<>:"/\\|?*')
RESERVED_WINDOWS_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{i}' for i in range(1, 10)),
    *(f'LPT{i}' for i in range(1, 10)),
}


def validate_path_component(name, label='nome'):
    """Retorna ``(valido, mensagem)`` para um unico nome de arquivo/pasta."""
    if not isinstance(name, str):
        return False, f'O {label} deve ser um texto.'
    if not name:
        return False, f'O {label} nao pode ficar vazio.'
    if name in ('.', '..'):
        return False, f'O {label} nao pode ser "." ou "..".'
    if name != name.strip():
        return False, f'O {label} nao pode comecar ou terminar com espacos.'
    if name.endswith('.'):
        return False, f'O {label} nao pode terminar com ponto.'
    if len(name) > 255:
        return False, f'O {label} excede o limite de 255 caracteres.'
    if any(ord(char) < 32 for char in name):
        return False, f'O {label} contem caracteres de controle.'

    invalid_found = sorted(set(name).intersection(INVALID_WINDOWS_NAME_CHARS))
    if invalid_found:
        return False, f'O {label} contem caractere(s) invalido(s): {" ".join(invalid_found)}'

    # O trecho anterior ao primeiro ponto tambem nao pode ser um dispositivo
    # reservado do Windows (CON.txt, NUL.jpg etc.).
    device_name = name.split('.', 1)[0].upper()
    if device_name in RESERVED_WINDOWS_NAMES:
        return False, f'O {label} usa um nome reservado do Windows: {device_name}.'

    if os.path.basename(name) != name or os.path.isabs(name):
        return False, f'O {label} deve conter somente o nome, sem caminho.'
    return True, ''


def looks_like_local_path(value):
    if not isinstance(value, str) or not value:
        return False
    return os.path.isabs(value) or any(char in value for char in ('\\', '/', ':'))


def canonical_local_path(path):
    """Normaliza ``..`` e resolve links/juncoes existentes no caminho."""
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.normpath(path))))


def configured_operation_roots(config_mgr):
    """Obtem as raizes onde a fila pode alterar arquivos locais."""
    candidates = []
    if config_mgr.is_sandbox():
        candidates.append(config_mgr.get_sandbox_path())
    else:
        # ``scan_paths`` e o nome historico ``scan_folders`` coexistem no app.
        for key in ('scan_paths', 'scan_folders', 'local_folders'):
            value = config_mgr.get(key, [])
            if isinstance(value, str):
                candidates.append(value)
            elif isinstance(value, (list, tuple)):
                candidates.extend(value)

    roots = []
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        root = canonical_local_path(candidate)
        if root not in seen:
            roots.append(root)
            seen.add(root)
    return roots


def validate_path_in_roots(path, roots, label='caminho', allow_root=False):
    """Confirma que um caminho absoluto permanece dentro de uma raiz permitida."""
    if not isinstance(path, str) or not path.strip():
        return False, f'O {label} nao foi informado.'
    if not os.path.isabs(path):
        return False, f'O {label} deve ser absoluto.'
    if not roots:
        return False, 'Nenhuma pasta local autorizada esta configurada para a operacao.'

    candidate = canonical_local_path(path)
    for raw_root in roots:
        root = canonical_local_path(raw_root)
        try:
            if os.path.commonpath((candidate, root)) != root:
                continue
        except ValueError:
            continue
        if candidate == root and not allow_root:
            return False, f'O {label} nao pode ser a propria raiz configurada.'
        return True, ''

    return False, f'O {label} esta fora das pastas configuradas no aplicativo.'
