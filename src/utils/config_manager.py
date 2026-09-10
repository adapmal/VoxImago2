'''
Gerenciador centralizado de configurações e modos do VoxImago v2.1
'''

import os
import json
import ntpath
import threading
from PyQt6.QtCore import QObject, pyqtSignal

from src.utils.portable_paths import (
    available_drive_letters,
    discover_shared_folder,
    relocation_candidates,
    resolve_relocated_path,
)

SETTINGS_FILE = os.path.join('config', 'settings.json')
_SETTINGS_LOCK = threading.RLock()

DEFAULT_SETTINGS = {
    'read_only_mode': False,
    'sandbox_mode': False,
    'sandbox_path': r'L:\Drives Compartilhados\_TestesBanco',
    'sandbox_drive_id': '0AIME28tN4AEHUk9PVA',
    'production_drive_id': '0AOB-ISqqs76_Uk9PVA',
    'shared_cache_path': r'L:\Drives Compartilhados\zRecursos_VoxImago\file_index_shared.db',
    'sheets_vocab_url': 'https://docs.google.com/spreadsheets/d/1_etBf2z9sqmdR74j4ADso47XYGgYQXljdjtBo3X74Jw/export?format=csv',
    'sheets_doc_url': 'https://docs.google.com/document/d/1huecd2o-vcoapmPcjguQwjsQAMnaAbxYw4qSXmgOo0Y/export?format=txt',
    'last_selected_folder': '',
    'current_sort': 'created_desc'
}


class _ConfigSignals(QObject):
    modeChanged = pyqtSignal(str, object)  # (key_name, new_value)


class ConfigManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.signals = _ConfigSignals()
            cls._instance.modeChanged = cls._instance.signals.modeChanged
            cls._instance.settings = DEFAULT_SETTINGS.copy()
            cls._instance.load_settings()
        return cls._instance

    def __init__(self, *args, **kwargs):
        pass

    def load_settings(self):
        try:
            with _SETTINGS_LOCK:
                if os.path.exists(SETTINGS_FILE):
                    with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        self.settings.update(data)
        except Exception as e:
            print(f"Erro ao carregar configurações: {e}")

    def save_settings(self):
        temporary = None
        try:
            with _SETTINGS_LOCK:
                os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
                temporary = (
                    SETTINGS_FILE
                    + f'.tmp-{os.getpid()}-{threading.get_ident()}'
                )
                with open(temporary, 'w', encoding='utf-8') as f:
                    json.dump(self.settings, f, indent=4, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary, SETTINGS_FILE)
        except Exception as e:
            print(f"Erro ao salvar configurações: {e}")
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError:
                    pass

    def get(self, key, default=None):
        return self.settings.get(key, default if default is not None else DEFAULT_SETTINGS.get(key))

    def set(self, key, value):
        changed = False
        with _SETTINGS_LOCK:
            old_value = self.settings.get(key)
            if old_value != value:
                self.settings[key] = value
                self.save_settings()
                changed = True
        if changed:
            self.modeChanged.emit(key, value)

    def is_read_only(self):
        return bool(self.get('read_only_mode', False))

    def is_sandbox(self):
        return bool(self.get('sandbox_mode', False))

    def get_sandbox_path(self):
        return self.get('sandbox_path', r'L:\Drives Compartilhados\_TestesBanco')

    def get_db_path(self):
        if self.is_sandbox():
            return os.path.join('data', 'sandbox_file_index.db')
        return os.path.join('data', 'file_index.db')

    def get_excluded_drive_letters(self):
        excluded = self.get('excluded_drive_letters', []) or []
        if isinstance(excluded, str):
            excluded = [excluded]
        return {
            str(letter).strip().rstrip('\\/').upper()
            for letter in excluded if str(letter).strip()
        }

    def get_allowed_drive_letters(self):
        """Unidades que podem participar da descoberta automática local."""
        excluded = self.get_excluded_drive_letters()
        return [
            drive for drive in available_drive_letters(excluded=excluded)
        ]

    def get_resolved_scan_paths(self, persist=False):
        """Retorna raizes locais existentes, remapeando apenas casos unicos.

        O caso principal e uma instalacao cujo snapshot contem ``L:`` mas o
        Google Drive Desktop esta montado, por exemplo, em ``G:``. Se duas
        copias do mesmo caminho existirem, nenhuma escolha automatica e feita.
        """
        configured = self.get('scan_paths', []) or self.get('scan_folders', [])
        if isinstance(configured, str):
            configured = [configured]

        resolved = []
        allowed_drives = self.get_allowed_drive_letters()
        for path in configured or []:
            configured_drive = ntpath.splitdrive(ntpath.normpath(path))[0].upper()
            if configured_drive in self.get_excluded_drive_letters():
                continue
            candidate = (
                path if os.path.isdir(path)
                else resolve_relocated_path(
                    path, drive_letters=allowed_drives
                )
            )
            if candidate and candidate not in resolved:
                resolved.append(candidate)

        if not resolved:
            discovered = discover_shared_folder(
                ("Banco de Imagens", "Image Bank"),
                drive_letters=allowed_drives,
            )
            if discovered:
                resolved.append(discovered)

        if persist and resolved:
            normalized = [ntpath.normpath(path) for path in resolved]
            if (self.settings.get('scan_paths') != normalized
                    or self.settings.get('scan_folders') != normalized):
                self.settings['scan_paths'] = normalized
                self.settings['scan_folders'] = normalized
                self.save_settings()
        return resolved

    def get_shared_cache_candidates(self, schema_version=None):
        """Caminhos-base candidatos ao snapshot, incluindo outras unidades."""
        configured = self.get(
            'shared_cache_path',
            r'L:\Drives Compartilhados\zRecursos_VoxImago\file_index_shared.db',
        )
        allowed_drives = self.get_allowed_drive_letters()
        configured_drive = ntpath.splitdrive(ntpath.normpath(configured))[0].upper()
        if configured_drive in self.get_excluded_drive_letters():
            configured_paths = []
        else:
            configured_paths = relocation_candidates(
                configured, drive_letters=allowed_drives
            )
        versioned_paths = []
        if schema_version:
            for path in configured_paths:
                root, ext = ntpath.splitext(path)
                versioned_paths.append(
                    f"{root}_v{int(schema_version)}{ext or '.db'}"
                )
        bases = [*versioned_paths, *configured_paths]

        # ``.voximago_system`` era um destino experimental. Ele so participa
        # quando nenhum artefato existe no local configurado; caso contrario,
        # dois backups historicos distintos pareceriam uma ambiguidade de
        # montagem e impediriam uma restauracao valida.
        def artifact_exists(path):
            root, _ext = ntpath.splitext(path)
            return os.path.isfile(path) or os.path.isfile(root + '.manifest.json')

        if not any(artifact_exists(path) for path in bases):
            legacy = r'L:\.voximago_system\shared_index.db'
            bases.extend(relocation_candidates(
                legacy, drive_letters=allowed_drives
            ))

        unique = []
        seen = set()
        for path in bases:
            key = ntpath.normcase(ntpath.normpath(path))
            if key not in seen:
                seen.add(key)
                unique.append(ntpath.normpath(path))
        return unique

    def get_shared_cache_publish_path(self, schema_version):
        configured = self.get(
            'shared_cache_path',
            r'L:\Drives Compartilhados\zRecursos_VoxImago\file_index_shared.db',
        )
        allowed_drives = self.get_allowed_drive_letters()
        configured_drive = ntpath.splitdrive(ntpath.normpath(configured))[0].upper()
        if configured_drive in self.get_excluded_drive_letters():
            raise RuntimeError(
                f'A unidade {configured_drive} esta excluida da configuracao.'
            )
        resolved = resolve_relocated_path(
            configured,
            drive_letters=allowed_drives,
            allow_existing_parent=True,
        ) or ntpath.normpath(configured)
        root, ext = ntpath.splitext(resolved)
        return f"{root}_v{int(schema_version)}{ext or '.db'}"

    def get_current_drive_id(self):
        if self.is_sandbox():
            return self.get('sandbox_drive_id', '0AIME28tN4AEHUk9PVA')
        return self.get('production_drive_id', '0AOB-ISqqs76_Uk9PVA')

    def set_read_only(self, value):
        self.set('read_only_mode', bool(value))

    def set_sandbox(self, value):
        self.set('sandbox_mode', bool(value))
