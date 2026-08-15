'''
Gerenciador centralizado de configurações e modos do VoxImago v2.1
'''

import os
import json
from PyQt6.QtCore import QObject, pyqtSignal

SETTINGS_FILE = os.path.join('config', 'settings.json')

DEFAULT_SETTINGS = {
    'read_only_mode': False,
    'sandbox_mode': True,  # Ativo por padrão no desenvolvimento da v2.1
    'sandbox_path': r'L:\Drives Compartilhados\_TestesBanco',
    'shared_cache_path': r'L:\.voximago_system\shared_index.db',
    'sheets_vocab_url': 'https://docs.google.com/spreadsheets/d/1_etBf2z9sqmdR74j4ADso47XYGgYQXljdjtBo3X74Jw/export?format=csv',
    'sheets_doc_url': 'https://docs.google.com/document/d/1huecd2o-vcoapmPcjguQwjsQAMnaAbxYw4qSXmgOo0Y/export?format=txt',
    'last_selected_folder': '',
    'current_sort': 'created_desc'
}


class ConfigManager(QObject):
    modeChanged = pyqtSignal(str, object)  # (key_name, new_value)

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(ConfigManager, cls).__new__(cls)
            super(ConfigManager, cls._instance).__init__()
            cls._instance.settings = DEFAULT_SETTINGS.copy()
            cls._instance.load_settings()
        return cls._instance

    def __init__(self, *args, **kwargs):
        pass

    def load_settings(self):
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.settings.update(data)
        except Exception as e:
            print(f"Erro ao carregar configurações: {e}")

    def save_settings(self):
        try:
            os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Erro ao salvar configurações: {e}")

    def get(self, key, default=None):
        return self.settings.get(key, default if default is not None else DEFAULT_SETTINGS.get(key))

    def set(self, key, value):
        old_value = self.settings.get(key)
        if old_value != value:
            self.settings[key] = value
            self.save_settings()
            self.modeChanged.emit(key, value)

    def is_read_only(self):
        return bool(self.get('read_only_mode', False))

    def is_sandbox(self):
        return bool(self.get('sandbox_mode', True))

    def get_sandbox_path(self):
        return self.get('sandbox_path', r'L:\_TestesBanco')

    def set_read_only(self, value):
        self.set('read_only_mode', bool(value))

    def set_sandbox(self, value):
        self.set('sandbox_mode', bool(value))
