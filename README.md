# 📸 VoxImago.MB - Documentação

> Galeria de imagens com busca unificada Local + Google Drive

## 🚀 Instalação Rápida

### Pré-requisitos
- Python 3.12 x64
- Windows 10 ou 11 x64

### Passos
1. **Clone o repositório:**
   ```bash
   git clone https://github.com/giorgiani29/VoxImago.MB.git
   cd VoxImago.MB
   ```

2. **Instale dependências:**
   ```bash
   py -3.12 -m pip install -r requirements.txt
   ```

3. **Configure Google Drive API:**
   - Copie `config/credentials.json.example` → `config/credentials.json`
   - Adicione suas credenciais da Google Cloud Console
   - Copie `config/settings.json.example` → `config/settings.json`
   - Configure seus caminhos de scan

4. **Execute:**
   ```bash
   py -3.12 app.py
   ```

## 📋 Como Usar

### Primeira Execução

1. **Snapshot manual (opcional)** - Uma instalação nova oferece a escolha de
   um snapshot. Nenhum banco é substituído automaticamente. Um banco corrompido
   é preservado e a ferramenta de restauração manual é oferecida.
2. **Login Google Drive** - Autorize acesso aos seus arquivos.
3. **Sincronização** - Confirme as atualizações recentes do Drive. A varredura
   local só é necessária quando não houver snapshot ou quando você quiser
   reconciliar mudanças feitas diretamente no disco.

### Funcionalidades Principais
- **🔍 Busca Unificada:** Encontre arquivos locais e do Drive simultaneamente
- **🖼️ Visualização:** Grid view com thumbnails inteligentes
- **📁 Filtros:** Por tipo, extensão, data, favoritos
- **⚡ Performance:** Algoritmo O(1) para matching de arquivos
- **🌙 Interface:** Tema escuro, system tray

### Atalhos Úteis
- **F10/F11/F12** - Ferramentas de debug e status
- **Ctrl+F** - Busca rápida
- **Duplo clique** - Abrir arquivo
- **Botão direito** - Menu contextual (Explorer, copiar caminho)

## ⚙️ Configuração

### Caminhos de Scan (`settings.json`)
```json
{
  "scan_paths": [
    "C:/Users/User/Pictures",
    "D:/Fotos"
  ],
  "drive_folders": [
    "root",
    "1ABC123..." 
  ]
}
```

Se a mesma pasta estiver montada sob outra letra, o VoxImago remapeia os
caminhos somente quando encontra uma correspondência única. Unidades que não
devem ser consultadas podem ser excluídas localmente:

```json
{
  "excluded_drive_letters": ["O:"]
}
```

### Formatos Suportados Por Enquanto
- **Imagens:** JPG, PNG, HEIC, RAW (ARW, CR2, etc.)
- **Vídeos:** MP4, AVI, MOV
- **Documentos:** PDF, DOCX (visualização limitada)

### Correspondência Drive/local

Um novo vínculo automático exige nome e tamanho exatos. Vínculos já conhecidos
também têm o tamanho revalidado. Quando existem arquivos homônimos com o mesmo
tamanho, a hierarquia de pastas precisa indicar um único vencedor; empates e
divergências ficam como registros separados e não sobrescrevem os metadados locais.

Para auditar vínculos criados por versões antigas, feche o VoxImago e execute:

```powershell
py -3.12 scripts/revalidate_drive_links.py --hash-mode suspects --hash-budget-gib 120 --max-file-gib 2 --apply --prune-stale-drive-cache
```

Antes de qualquer alteração, o utilitário cria uma cópia do banco e um inventário
do Drive em `config/backups/link_revalidation`. Vínculos confirmados por metadados
ou MD5 continuam aptos à sincronização; divergências e empates são colocados em
quarentena e não podem sobrescrever descrições. O CSV e o `summary.json` da mesma
pasta registram todas as decisões.

### Snapshot compartilhado e saúde do banco

O snapshot v2 é uma geração imutável formada por banco SQLite, CSV e manifesto
com hashes. O manifesto só é trocado depois que os dois arquivos foram copiados
e verificados. As três gerações mais recentes são mantidas. Um lock impede duas
máquinas de publicar ao mesmo tempo. Se o destino mudar depois da confirmação,
a publicação falha e exige nova conferência.

Em **Sistema / Avançado / Snapshots**, use **Comparar / verificar snapshot**
para conferir origem, data, versão, contagens e raízes de pastas. Essa comparação
não mescla registros nem considera a diferença de contagens um erro do banco.
**Publicar snapshot desta máquina** é a única ação que distribui uma geração:
sincronizações (inclusive a automática de 15 minutos) e commits da fila não
publicam mais snapshots. Atualize todas as máquinas: versões antigas ainda
podem continuar publicando automaticamente.

**Preparar adoção ao reiniciar** verifica o snapshot e pede o mapeamento das
pastas de origem para as pastas desta máquina, sem consultar mídias individuais.
Exige fila vazia e versão de banco compatível. A cópia é preparada localmente;
a troca só ocorre após confirmação na próxima abertura, antes de iniciar os
workers. Feche também versões antigas e ferramentas externas que acessem esse
banco. Um backup do catálogo anterior é obrigatório; sua falha impede a troca.
Não há mesclagem: tags locais, favoritos, rotações e vínculos passam a ser os do
snapshot. A fila e o cache de thumbnails não são substituídos. A primeira
sincronização após a adoção consulta todo o inventário não excluído do Drive,
sem a janela curta de alterações recentes; depois faça o diagnóstico local.

Backups automáticos são independentes e **locais**, em
`data/catalog_backups/<nome-do-banco>/`. Enquanto o app está aberto e ocioso,
ele verifica após um minuto e depois a cada hora se cabe uma cópia: no máximo
uma por 24 horas, mantendo três backups `auto-*.db`. Backups `before-restore-*`
e arquivos danificados preservados não entram nessa rotação. Eles não protegem
contra perda do disco inteiro; mantenha uma cópia externa quando necessário.

O snapshot inclui caminhos, tags, vínculos com o Drive, favoritos e a orientação
manual das miniaturas. Os arquivos de thumbnail não são copiados; o cache local
existente é preservado e uma instalação nova gera apenas as miniaturas que usar.

Em **Sistema / Avançado / Saúde e reparo do banco** é possível, sob demanda:

- verificar a integridade física e a estrutura do SQLite;
- comparar a tabela principal com o índice de busca;
- conferir a existência dos caminhos locais;
- auditar ou reparar de forma conservadora vínculos antigos com o Drive.

Reparos criam um backup antes de escrever. Ambiguidades continuam em quarentena
e não são “corrigidas” por escolha arbitrária.

Para atualizar várias máquinas, atualize primeiro a instalação principal,
execute o diagnóstico/reparo e faça uma sincronização. Publique manualmente uma
geração quando precisar inicializar ou recuperar outra instalação. Nas demais
máquinas, atualizar o código não exige adotar outro snapshot: mantenha o banco
local saudável e sincronize normalmente.

## 🔧 Troubleshooting

### Problemas Comuns
- **Token expirado:** Use menu Ferramentas > Logout/Login
- **Arquivos não aparecem:** Force rescan local no menu
- **Performance lenta:** Verifique índices do banco (F11)

### Fila de alterações e recuperação

A fila é salva transacionalmente em `data/staging_queue.db`, separada do catálogo
e dos snapshots compartilhados. Na primeira abertura desta versão, a fila antiga
de `config/staging_queue.json` é importada integralmente; o JSON original fica
preservado, mas deixa de ser atualizado. Não use versões antigas simultaneamente
na mesma instalação. Exportar/importar JSON continua disponível para transporte.

Adicionar tags a uma seleção grande prepara um único lote em segundo plano,
usando somente metadados do catálogo, sem abrir ou baixar as mídias. A barra
inferior mostra o andamento e permite cancelar antes da gravação final. Cancelar
preserva toda a fila anterior. Enquanto o lote está em preparação, outras edições
da fila e a sincronização aguardam; a navegação continua disponível.

Ao abrir **Fila**, a lista é montada progressivamente, com uma barra indicando
a preparação e a quantidade de operações exibidas. Os controles da fila ficam
indisponíveis até a montagem terminar; a janela pode ser fechada durante esse
processo sem alterar as operações salvas. Reabrir carrega a fila atual. Essa
montagem não acessa as mídias nem executa alterações no Drive.

A cada cinco minutos, havendo mudanças, são mantidas no máximo três cópias JSON
em `data/queue_autosave/queue-1.json` até `queue-3.json`. A mais antiga é substituída
de forma atômica. O autosave periódico aguarda a preparação, execução e manutenção
da fila terminarem. As edições confirmadas já estão salvas no SQLite mesmo antes
do próximo autosave. Na janela da Fila, **Recuperar autosave** permite escolher uma
cópia pelo horário. Falhas de leitura ou gravação são mostradas, sem substituir
silenciosamente uma fila ilegível por uma vazia.

A execução registra cada operação antes de iniciá-la e retira cada sucesso de
forma durável. Resultados incertos após erro/interrupção exigem conferência antes
de remover/recriar a operação; cópias antigas não reintroduzem operações com
sucesso registrado. Se o banco da fila estiver corrompido e o histórico de execução
for ilegível, a recuperação guarda o arquivo danificado e considera todas as
operações recuperadas incertas. O limite é de 100.000 operações e 64 MB, validado
tanto na entrada quanto na persistência. Estes arquivos contêm nomes/caminhos e
tags e devem permanecer na pasta local da instalação.

### Logs
O log ativo fica em `logs/voximago.log`. Ele é arquivado ao atingir 25 MB
ou na mudança do dia; os arquivos anteriores ficam comprimidos em
`logs/archive` por 45 dias.

Por padrão, eventos repetitivos por arquivo não poluem o log. Para investigar
um problema específico com todos os detalhes, inicie temporariamente pelo
PowerShell com o nível de diagnóstico:

```powershell
$env:VOXIMAGO_LOG_LEVEL = "DEBUG"
py -3.12 app.py
```

O modo `DEBUG` aumenta o volume e deve ser usado apenas durante a investigação.

---

*Desenvolvido com PyQt6 e Google Drive API*
