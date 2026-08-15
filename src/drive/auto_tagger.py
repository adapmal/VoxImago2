'''
Motor de Sugestão Automática de Tags (Auto-Tagger) para o VoxImago v2.1
Extrai tags automaticamente a partir do caminho do diretório, metadados do arquivo
e prepara a integração para sugestão por IA com escopo fechado no Vocabulário Controlado.
'''

import os
import re
import datetime
from src.ui.vocab_panel import VocabManager


class AutoTagger:
    def __init__(self):
        self.vocab_mgr = VocabManager()

    def suggest_tags_from_path(self, file_path):
        if not file_path:
            return []

        suggested = set()
        norm_path = os.path.normpath(file_path)
        path_parts = norm_path.split(os.sep)

        all_vocab_tags = self.vocab_mgr.get_all_tags()
        vocab_map = {t.lower(): t for t in all_vocab_tags}

        for part in path_parts:
            part_clean = part.strip()
            if not part_clean:
                continue

            # 1. Correspondência Exata
            part_lower = part_clean.lower()
            if part_lower in vocab_map:
                suggested.add(vocab_map[part_lower])

            # 2. Correspondência de Sub-palavras no nome da pasta
            # Dividir pasta por espaço, hífen, underline ou mais
            tokens = re.split(r'[\s_\-\+\&\,\.]+', part_clean)
            for tok in tokens:
                tok_lower = tok.strip().lower()
                if len(tok_lower) >= 3 and tok_lower in vocab_map:
                    suggested.add(vocab_map[tok_lower])

        return sorted(list(suggested))

    def suggest_tags_from_metadata(self, file_item):
        if not file_item:
            return []

        suggested = set()

        # Sugerir por Ano de criação/modificação (Desativado a pedido do usuário, que prefere usar apenas a pasta)
        # created_ts = file_item.get('createdTime') or file_item.get('modifiedTime')
        # if created_ts and isinstance(created_ts, (int, float)) and created_ts > 0:
        #     try:
        #         dt = datetime.datetime.fromtimestamp(created_ts)
        #         year_str = str(dt.year)
        #         if year_str in self.vocab_mgr.all_tags:
                    suggested.add(year_str)
            except Exception:
                pass

        # Sugerir por tipo de mídia
        mime = file_item.get('mimeType', '')
        ext = os.path.splitext(file_item.get('name', ''))[1].lower()
        if 'video' in mime or ext in ['.mp4', '.avi', '.mov', '.mkv', '.wmv']:
            if 'Vídeo' in self.vocab_mgr.all_tags:
                suggested.add('Vídeo')

        # Sugerir orientação da imagem (Horizontal / Vertical)
        local_path = file_item.get('path')
        if local_path and os.path.exists(local_path) and ext in ['.jpg', '.jpeg', '.png', '.webp', '.heic']:
            try:
                from PIL import Image
                with Image.open(local_path) as img:
                    w, h = img.size
                    if w > h:
                        suggested.add('Horizontal')
                    elif h > w:
                        suggested.add('Vertical')
            except Exception:
                pass

        return sorted(list(suggested))

    def get_all_suggestions(self, file_item):
        fpath = file_item.get('path', '')
        tags_path = self.suggest_tags_from_path(fpath)
        tags_meta = self.suggest_tags_from_metadata(file_item)

        combined = set(tags_path + tags_meta)

        # Filtrar tags que o arquivo já possui na descrição
        existing_desc = file_item.get('description', '')
        if existing_desc:
            existing_tags = [t.strip().lower() for t in existing_desc.split(',') if t.strip()]
            combined = {t for t in combined if t.lower() not in existing_tags}

        return sorted(list(combined))

    def suggest_ai_tags(self, image_path, top_k=5):
        '''
        Interface de preparação para modelo leve de visão IA (CLIP / BLIP).
        Retorna sugestões de pontuação elevadas restritas ao Vocabulário Controlado.
        '''
        # Retorna uma lista de sugestões de modelo restritas ao vocabulário
        return []
