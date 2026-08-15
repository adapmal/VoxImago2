r'''
Utilitário para gerenciamento do Ambiente de Testes Sandbox (L:\_TestesBanco)
'''

import os
import shutil
import logging
from src.utils.config_manager import ConfigManager


def ensure_sandbox_directory(sandbox_path=None):
    if sandbox_path is None:
        sandbox_path = ConfigManager().get_sandbox_path()
    try:
        os.makedirs(sandbox_path, exist_ok=True)
        return True, sandbox_path
    except Exception as e:
        logging.error(f"Erro ao criar diretório sandbox {sandbox_path}: {e}")
        return False, str(e)


def generate_sample_test_data(source_dir, sandbox_path=None, max_files=50):
    if sandbox_path is None:
        sandbox_path = ConfigManager().get_sandbox_path()

    ok, msg = ensure_sandbox_directory(sandbox_path)
    if not ok:
        return False, f"Não foi possível acessar a pasta de testes: {msg}", 0

    copied_count = 0
    valid_extensions = {
        '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.heic',
        '.mp4', '.avi', '.mov', '.mkv', '.cr2', '.nef', '.arw'
    }

    try:
        for root, _, files in os.walk(source_dir):
            if copied_count >= max_files:
                break
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in valid_extensions:
                    src_file = os.path.join(root, file)
                    rel_path = os.path.relpath(src_file, source_dir)
                    dest_file = os.path.join(sandbox_path, rel_path)

                    os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                    if not os.path.exists(dest_file):
                        shutil.copy2(src_file, dest_file)
                        copied_count += 1

                    if copied_count >= max_files:
                        break

        return True, f"Amostra de {copied_count} arquivos gerada em '{sandbox_path}'.", copied_count
    except Exception as e:
        logging.error(f"Erro ao gerar amostras para sandbox: {e}")
        return False, f"Erro ao copiar arquivos: {e}", copied_count
