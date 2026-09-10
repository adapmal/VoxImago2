"""Formato persistente e validacao dos itens da fila de revisao."""

import os
import time

from src.utils.path_validation import validate_path_component


ALLOWED_ACTION_TYPES = {
    'add_tags', 'remove_tags', 'set_description', 'rename', 'move',
    'delete', 'create_folder', 'rotate_90',
}
MAX_QUEUE_ITEMS = 10000
MAX_QUEUE_FILE_BYTES = 10 * 1024 * 1024
MAX_QUEUE_TEXT_LENGTH = 1024 * 1024


class StagingItem:
    def __init__(self, file_id, file_name, path, action_type, old_value="", new_value=""):
        self.file_id = file_id
        self.file_name = file_name
        self.path = path
        self.action_type = action_type
        self.old_value = old_value
        self.new_value = new_value
        self.timestamp = time.time()

    def to_dict(self):
        # A lista e os nomes dos campos permanecem compativeis com as versoes
        # anteriores. Versoes antigas simplesmente ignoram ``schema_version``.
        return {
            'schema_version': 1,
            'file_id': self.file_id,
            'file_name': self.file_name,
            'path': self.path,
            'action_type': self.action_type,
            'old_value': self.old_value,
            'new_value': self.new_value,
            'timestamp': self.timestamp,
        }

    @classmethod
    def from_dict(cls, data):
        """Le tanto o formato legado quanto o formato atual da fila."""
        if not isinstance(data, dict):
            raise ValueError('Item da fila deve ser um objeto JSON.')

        values = {}
        for field in ('file_id', 'file_name', 'path', 'action_type', 'old_value', 'new_value'):
            value = data.get(field, '')
            if value is None:
                value = ''
            if not isinstance(value, str):
                raise ValueError(f'Campo "{field}" deve ser texto.')
            if len(value) > MAX_QUEUE_TEXT_LENGTH:
                raise ValueError(f'Campo "{field}" excede o tamanho permitido.')
            values[field] = value

        item = cls(
            values['file_id'], values['file_name'], values['path'],
            values['action_type'], values['old_value'], values['new_value'],
        )
        timestamp = data.get('timestamp', time.time())
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise ValueError('Campo "timestamp" deve ser numerico.')
        item.timestamp = float(timestamp)
        item.validate()
        return item

    def validate(self):
        for field in ('file_id', 'file_name', 'path', 'action_type', 'old_value', 'new_value'):
            value = getattr(self, field)
            if value is None:
                value = ''
                setattr(self, field, value)
            if not isinstance(value, str):
                raise ValueError(f'Campo "{field}" deve ser texto.')
            if len(value) > MAX_QUEUE_TEXT_LENGTH:
                raise ValueError(f'Campo "{field}" excede o tamanho permitido.')

        if self.action_type not in ALLOWED_ACTION_TYPES:
            raise ValueError(f'Acao de fila nao permitida: {self.action_type!r}.')

        if self.action_type in ('add_tags', 'remove_tags', 'set_description'):
            if not (self.file_id or self.path):
                raise ValueError('Edicao de tags exige file_id ou path.')
        elif self.action_type == 'rename':
            if not (self.path or self.old_value):
                raise ValueError('Renomeacao exige um caminho de origem.')
            valid, error = validate_path_component(self.new_value, 'novo nome')
            if not valid:
                raise ValueError(error)
        elif self.action_type == 'move':
            if not (self.old_value or self.path) or not self.new_value:
                raise ValueError('Movimentacao exige origem e destino.')
            valid, error = validate_path_component(
                os.path.basename(os.path.normpath(self.new_value)), 'nome no destino')
            if not valid:
                raise ValueError(error)
        elif self.action_type == 'delete':
            if not (self.path or self.old_value):
                raise ValueError('Exclusao exige um caminho local.')
        elif self.action_type == 'create_folder':
            target = self.new_value or self.path
            if not target:
                raise ValueError('Criacao de pasta exige um caminho de destino.')
            valid, error = validate_path_component(
                os.path.basename(os.path.normpath(target)), 'nome da pasta')
            if not valid:
                raise ValueError(error)
        elif self.action_type == 'rotate_90' and not self.path:
            raise ValueError('Rotacao exige um caminho local.')
        return True

    def get_description_summary(self):
        if self.action_type == 'add_tags':
            return f"[+] Adicionar tags: '{self.new_value}'"
        if self.action_type == 'remove_tags':
            return f"[-] Remover tags: '{self.new_value}'"
        if self.action_type == 'set_description':
            return f"[=] Nova descrição: '{self.new_value}'"
        if self.action_type == 'rename':
            return f"[R] Renomear para: '{self.new_value}'"
        if self.action_type == 'move':
            dst_folder = os.path.basename(os.path.dirname(self.new_value))
            return f"📦 Mover para a pasta: '{dst_folder}'"
        if self.action_type == 'delete':
            return "🗑️ Excluir arquivo"
        if self.action_type == 'create_folder':
            folder_name = os.path.basename(self.new_value)
            return f"📁🟢 Criar nova pasta (Fila): '{folder_name}'"
        return f"[{self.action_type}] {self.new_value}"
