# Plano de Ação Arquitetural — VoxImago.MB v2.2

## Visão Geral
Este plano estabelece as diretrizes técnicas e as alterações concretas para a versão **v2.2** do VoxImago.MB, abordando os **22 achados de vulnerabilidade** identificados na auditoria técnica da branch `v2.1`. 

O objetivo primordial é **garantir risco ZERO de perda de fotos, metadados ou histórico de tags**, eliminando operações concorrentes destrutivas e estabelecendo regras estritas de integridade de dados no disco e no banco SQLite.

---

## Decisões Estratégicas de Negócio

1. **Rotação Não Destrutiva (P2 / F1, F2):**
   - O arquivo *master* original de alta resolução **nunca será sobrescrito nem modificado**.
   - A rotação operará **exclusivamente na miniatura (thumbnail) / visualização de tela**, preservando 100% da integridade física dos arquivos RAW/JPEG e seus metadados EXIF/IPTC originais.

2. **Desacoplamento de Responsabilidades Disco vs. Nuvem (P1 / F6):**
   - **Mover, Renomear e Excluir:** executados **apenas no disco local (Windows)**. A propagação para o Google Drive fica a cargo do agente de sincronização nativo instalado na máquina (InSync ou Google Drive para Desktop), eliminando conflitos e arquivos duplicados/corrompidos.
   - **Tags e Descrições:** enviadas via **API do Google Drive**, pois são metadados proprietários da nuvem não cobertos pelos agentes de sincronização de arquivos.

3. **Regra de Ouro da Preservação de Tags (P4 / F7, F8):**
   - **Tags locais nunca são zeradas por descrições vazias da nuvem.** O banco local só aceita atualização da nuvem se esta contiver texto válido e timestamp superior.

4. **Prevenção de Sobrescrita e Lixeira Segura (P5, P8 / F3, F4):**
   - Renomeação com checagem estrita de colisão (`os.path.exists`).
   - Exclusão com `send2trash` integrado; se o envio para a Lixeira falhar, a operação **aborta imediatamente com aviso**, nunca apagando em definitivo via `rmtree`/`remove` silencioso.

---

## Detalhamento das Alterações por Fase

### Fase 1 · Proteção Absoluta de Arquivos e Integridade Física

#### 1.1 Rotação 100% Visual e Não Destrutiva (F1, F2)
- **Arquivo:** `src/ui/details_panel.py`
- **Ação:** 
  - Remover completamente o bloco de código que invoca `rotated.save(fpath)` sobre o arquivo master.
  - Implementar a rotação apenas no cache de miniaturas (`assets/thumbnail_cache/`) ou rotação de exibição via `QPixmap.transformed()`.
  - O arquivo original no disco não sofre recompressão JPEG nem perda de EXIF.

#### 1.2 Verificação de Existência no Renomear (F3)
- **Arquivo:** `src/ui/staging_queue.py`
- **Ação:** 
  - Antes de executar `os.rename`/`safe_move_file`, verificar se `os.path.exists(new_path)` é verdadeiro.
  - Se o destino já existir, bloquear a operação e notificar o usuário (ou disparar `MoveConflictDialog`), impedindo que duas fotos virem uma.

#### 1.3 Eliminação da Exclusão Permanente Silenciosa (F4)
- **Arquivos:** `requirements.txt`, `src/utils/utils.py`, `src/ui/staging_queue.py`
- **Ação:** 
  - Adicionar formalmente `send2trash>=1.8.0` ao `requirements.txt`.
  - Atualizar `send_to_recycle_bin`: utilizar `SHFileOperationW` e fallback para `send2trash`.
  - Em `staging_queue.py` (`action_type == 'delete'`), se `send_to_recycle_bin` retornar `False`, **lançar exceção e abortar**. Remover os comandos `shutil.rmtree` e `os.remove` da fila de execução.

#### 1.4 Isolamento Real do Modo Sandbox e Somente Leitura (F5, F18)
- **Arquivos:** `src/utils/config_manager.py`, `src/ui/staging_queue.py`, `src/ui/folder_tree_widget.py`
- **Ação:** 
  - No Modo Sandbox: apontar a conexão SQLite para `data/sandbox_file_index.db` e travar qualquer operação de escrita em caminhos que não contenham `_TestesBanco`.
  - No Modo Somente Leitura: bloquear inclusive criação física de pastas (`create_new_folder`) e desativar qualquer rotina destrutiva.

#### 1.5 Eliminação de Palpites de Links na Nuvem (F6)
- **Arquivos:** `src/ui/details_panel.py`, `src/ui/staging_queue.py`
- **Ação:** 
  - Remover a gravação automática de `candidates[0]` no banco quando houver homônimos não desambiguados.
  - Eliminar chamadas de `files().update(trashed=True)` via API da fila de exclusão local.

---

### Fase 2 · Preservação Intransigente de Tags e Metadados

#### 2.1 Guarda contra Descrições Vazias da Nuvem (F7, F8)
- **Arquivos:** `src/drive/drive_sync.py`, `src/drive/incremental_sync.py`, `src/database/database.py`
- **Ação:** 
  - Em `update_description`, `fuse_page_data` e `IncrementalSyncWorker`: se `incoming_desc.strip() == ''`, **não sobrescrever** a descrição local existente no banco `files` ou `search_index`.
  - O banco local só aceita atualização da nuvem se houver texto real.

#### 2.2 Unificação da Chave Canônica Universal (F9, F10)
- **Arquivos:** `src/database/database.py`, `src/ui/staging_queue.py`
- **Ação:** 
  - Padronizar a chave de arquivos locais em todas as tabelas (`files`, `search_index`) como `os.path.normcase(os.path.normpath(path))`.
  - Garantir que `staging_queue.py` use rigorosamente essa chave canônica ao renomear e mover.
  - Incluir script de rebuild do `search_index` para unificar e limpar duplicatas históricas.

#### 2.3 Cálculo Dinâmico de Tags na Execução da Fila (F11)
- **Arquivo:** `src/ui/staging_queue.py`
- **Ação:** 
  - Ao executar `add_tags` ou `remove_tags`, consultar a descrição **atual e viva do banco SQLite no instante da execução**, em vez de aplicar sobre o snapshot estático `old_value`.
  - Permite encadear múltiplas adições/remoções no mesmo lote sem cancelamento mútuo.

#### 2.4 Escape de Caracteres Especiais em Consultas SQL (F12)
- **Arquivo:** `src/ui/staging_queue.py`
- **Ação:** 
  - Adicionar cláusula `ESCAPE` em consultas `LIKE` que envolvem caminhos com underscore `_`, protegendo nomes de pastas contra casamentos indevidos.

---

### Fase 3 · Integridade do Banco SQLite, WAL e Backups

#### 3.1 Exportação Atômica com Checkpoint WAL (F13)
- **Arquivo:** `src/database/database.py`
- **Ação:** 
  - Em `export_to_shared_cache`, executar `PRAGMA wal_checkpoint(TRUNCATE)` antes da cópia ou utilizar a API nativa `sqlite3.Connection.backup()`.
  - Gravar em um arquivo temporário no destino (`.db.tmp`) e realizar rename atômico.

#### 3.2 Correção da Ferramenta de Restauração de Emergência CSV (F14)
- **Arquivo:** `src/ui/advanced_settings_dialog.py`
- **Ação:** 
  - Corrigir a leitura da coluna de `file_id` (compatível com `export_to_shared_cache`).
  - Atualizar simultaneamente a tabela `files` e a tabela `search_index` (FTS5).

#### 3.3 Verificação Robusta de Integridade na Inicialização (F15, F16, F20)
- **Arquivos:** `src/database/database.py`, `src/drive/processing.py`
- **Ação:** 
  - Substituir a checagem cega de tamanho `< 1 MB` por `PRAGMA integrity_check` e `SELECT COUNT(*) FROM files`.
  - Remover a função insegura `import_from_shared_cache`.
  - Substituir `QThread.terminate()` por encerramento cooperativo com `requestInterruption()`.

#### 3.4 Estabilidade Transacional na Busca (F19)
- **Arquivo:** `src/database/search.py`
- **Ação:** 
  - Remover as chamadas `PRAGMA synchronous=OFF` da rotina `get_search_suggestions`.

---

### Fase 4 · Estabilidade de Diretórios e Correções Pontuais

#### 4.1 Correção de Imports e Detecção de Trava (F17, F21, F22)
- **Arquivos:** `src/drive/incremental_sync.py`, `src/utils/utils.py`, `src/ui/staging_queue.py`
- **Ação:** 
  - Garantir presença de `import os` em todas as rotinas.
  - Em `safe_move_file`, aplicar o teste de trava com `open('rb')` apenas se `os.path.isfile(path)`.
  - Separar a limpeza de cancelamento de pasta da conclusão de execução com sucesso na fila de staging.

---

## Plano de Verificação

### Testes Automatizados Unitários e de Integração:
1. `test_rotation_non_destructive.py`: Garantir que rotacionar uma foto não altera o arquivo master nem seu hash MD5/EXIF, afetando apenas o thumbnail.
2. `test_rename_collision_guard.py`: Tentar renomear um arquivo para um nome existente e verificar o bloqueio.
3. `test_delete_recycle_bin_abort.py`: Simular falha na lixeira e garantir que o código aborta sem apagar arquivos permanentemente.
4. `test_empty_drive_desc_guard.py`: Simular item no Drive com descrição vazia e certificar que a tag local permanece 100% preservada.
5. `test_key_normalization_sync.py`: Verificar que `files` e `search_index` usam rigorosamente `normcase(normpath)` sem gerar linhas fantasmas.
6. `test_wal_atomic_export.py`: Validar que a exportação do banco em modo WAL contém 100% dos dados recentes e passa em `integrity_check`.

### Verificação Manual no Ambiente Real:
- Testar a navegação, busca com acentuação e visualização de fotos.
- Validar operações da fila de staging no modo Somente Leitura e no modo Normal.
