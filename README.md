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

### Deduplicação por hash — análise e revisão antes de movimentar

Em **Diagnóstico > Deduplicar Fotos por Hash**, o padrão é **comparar pastas
selecionadas entre si**. Adicione duas ou mais pastas, sem definir origem ou
referência; as subpastas também participam. Pastas sobrepostas são percorridas
uma única vez e cada ID só participa uma vez. Outras pastas `Organizar` ficam
fora desse modo. Permanecem disponíveis os escopos
`Organizar × acervo`, `pastas escolhidas × acervo` e `acervo × acervo`.
O conjunto de comparação nunca amplia sozinho a autorização de movimentação.

A análise consulta os checksums e descrições **do Drive** e confere os caminhos
do catálogo por uma hierarquia sem ambiguidades. Não abre os originais locais,
não baixa mídias para calcular hashes e não usa busca global por nome/tamanho
para decidir qual ID mover. Confere tamanho + MD5 e SHA-256 quando disponível;
arquivos sem checksum, vínculos divergentes, caminhos ambíguos e arquivos não
indexados localmente não são movidos. Aguarde o cliente InSync/Drive concluir
suas alterações antes de analisar. Isso identifica cópias de conteúdo binário,
não fotos apenas visualmente parecidas. O relatório de ignorados fica em
`data/dedup/last_scan_skipped.json`.

No modo de pastas selecionadas, a consulta carrega metadados completos apenas
dessas árvores. Para localizar as pastas com segurança, lista também IDs, nomes
e tipos dos filhos diretos dos ancestrais; não percorre outras árvores.
Não há cache persistente: cada análise consulta novamente esse escopo.
Modos que comparam com todo o acervo ainda consultam todo o Drive Compartilhado.

Os grupos começam **marcados**, mas nada é movido sem confirmação final.
Desmarque individualmente ou em lote o que não quiser executar.
A tabela usa somente thumbnails já em cache; mudar o escopo invalida a revisão.
O painel de inspeção também usa somente miniaturas em cache, sem gerar novas
abrindo originais automaticamente. EXIF é lido em segundo plano, com apenas uma
leitura por painel e a seleção mais recente aguardando; resultados antigos são
descartados. Arquivos marcados pelo Windows como offline/recall não são abertos
para EXIF. Espaço/duplo clique continuam sendo pedidos explícitos de abertura.
Durante a execução, o painel de inspeção fica bloqueado e o executor recebe uma
cópia independente do plano confirmado, incluindo quem fica e as tags manuais.
O ano preferido é determinado pelo grupo inteiro: ano no nome de qualquer
cópia (1997 até o ano atual), depois data EXIF disponível nos metadados e,
por último, menor pasta anual raiz válida. A busca continua sem baixar originais:
usa a data de imagem fornecida pelo Drive, não uma nova varredura de EXIF local.
Nos campos já disponíveis no item, tenta a primeira data válida na ordem
DateTimeOriginal, exif_date_taken, DateTimeDigitized, DateTime e imageMediaMetadata.time.
Datas mascaradas, calendários/horários inválidos e anos fora de 1900–2100 não
impedem a tentativa da fonte seguinte.
Anos conflitantes nos nomes só são resolvidos se o EXIF apontar unicamente para
um deles; conflitos não resolvidos ficam desmarcados e bloqueados para revisão.
Depois da preferência anual, os desempates são: fora de Organizar, menor
profundidade, mais tags, caminho mais curto, criação mais antiga no Drive e
ordem alfabética do caminho.

Se nenhuma cópia analisada estiver no ano identificado, fica o nome mais longo
(sem extensão), com os demais critérios como desempate. A revisão mostra o
destino `Banco de Imagens/ANO/Organizar/nome`. Após confirmar as tags, a execução
realoca esse preservado no mesmo Drive e confirma o resultado antes de arquivar
os duplicados. Pastas ausentes são criadas; pastas ambíguas ou nomes ocupados
bloqueiam o grupo, sem sobrescrita nem renomeação automática do preservado.
Preservados protegidos pelo escopo não são movidos; o grupo pede revisão.
O diário permite reconciliar uma realocação confirmada com o índice local,
preservando rotação e dados do registro, sem repetir o movimento na nuvem.

Cada grupo exibe no máximo 12 cartões de duplicados por página, com botões
Anteriores/Próximas e navegação por setas entre páginas. Todas as cópias continuam
na análise e na execução: a paginação é somente visual. A consulta de miniaturas
também é limitada às cópias em inspeção/página, reaproveitando caches já encontrados
no grupo. Quando a rotação da cópia doadora difere, a diferença angular é aplicada
somente ao QPixmap em memória, tanto na coluna de miniaturas quanto no inspetor;
o original e o cache doador não são modificados. Shift usa a mesma âncora entre
cartões e teclado; o foco por célula respeita a página atual de cópias da linha.
O botão de inversão alterna o arquivo preservado entre os membros do grupo e
marca o grupo para execução. Nenhuma escolha autoriza mover arquivos fora do escopo.

Dê duplo clique na coluna **Tags Finais** para editar o texto que será gravado.
A edição manual substitui a união automática (permite remover tags e nomes);
separadores são normalizados e repetições são removidas. Uma descrição vazia
é permitida e destacada na confirmação. Se alguma descrição no Drive mudou
desde a análise, o grupo manual é bloqueado para nova revisão, sem sobrescrevê-la.
A edição é preservada ao inverter quem fica. **Restaurar tags automáticas**
descarta a edição dos grupos selecionados e volta à união automática.

A execução exige fila vazia, produção, permissão de escrita e backup local do
catálogo. Coordena-se com a sincronização/varredura para evitar concorrência na
mesma instalação. Reconsulta IDs, conteúdo, pais, permissões e descrições atuais;
une tags e todos os nomes diferentes antes dos movimentos, sem truncar texto.
Nomes que contêm separadores de tags exigem revisão manual. Escritas usam
`If-Match` com o ETag remoto. Quando a API v3 não fornece ETag, a execução
consulta a representação v2 do mesmo ID, confere versão e metadados e usa
`files.patch` v2 com o ETag dessa representação. Não reutiliza ETags entre
versões da API e não faz escrita desprotegida. Se a v2 também não fornecer
ETag ou as leituras divergirem, bloqueia o grupo. Evite edições
simultâneas por outras máquinas: não há transação única entre vários arquivos
do Google Drive e o SQLite.

O destino deve estar sob `_TestesBanco/Banco_Apagado` no outro Drive configurado.
Toda a subpasta escolhida é respeitada. Pastas homônimas ambíguas bloqueiam o
processamento; colisões de arquivos recebem sufixo por ID, sem sobrescrita.
Não há fallback de movimentação pelo disco local.

Miniaturas TIFF numéricas respeitam o tipo/faixa dos pixels (inclusive float
normalizado). Apenas as chaves de cache de TIFF local foram versionadas para
substituir miniaturas antigas incorretas quando forem geradas novamente; o
cache antigo não é apagado e as preferências de rotação são preservadas.

O diário durável `data/dedup/journal.db` registra pedidos antes de mover e
confirmações antes de limpar o índice local. Os relatórios JSON ficam nessa
pasta; após movimentos, tenta-se enviar uma cópia ao destino via API. Falha no
envio não apaga o relatório local. **Conferir operações interrompidas** consulta
o resultado remoto e conclui apenas a limpeza local de movimentos confirmados;
não repete movimentos incertos. Casos ainda não confirmados permanecem
bloqueados para revisão manual. Depois de mover, aguarde o cliente sincronizar:
uma varredura local antecipada pode exibir temporariamente caminhos antigos.

A revisão do deduplicador é salva automaticamente em
`data/dedup/review_plan.json` (ao lado do banco em instalações com outro caminho).
Ela é retomada ao reabrir o painel/aplicativo, incluindo seleção, escopo e tags
manuais; não é publicada no Drive nem executada automaticamente. O arquivo é
substituído atomicamente, com escrita em segundo plano. Antes de executar,
salva-se também um marcador dos grupos em andamento: após uma queda, esses
grupos voltam bloqueados para conferência, sem repetir movimentos às cegas.

Após executar, saem da revisão apenas os grupos concluídos e as cópias cujo
movimento e atualização local foram confirmados. Falhas, cancelamentos e grupos
não executados permanecem. Use **Conferir operações interrompidas** e **Refazer
busca** para reavaliar os bloqueados. **Limpar revisão** apaga somente a lista
salva e suas edições, nunca os arquivos do acervo. Alterar escopo/destino mantém
a revisão visível, mas exige refazer a busca antes de executar. Uma busca que
falhar/cancelar não apaga a revisão anterior.

O fundo vermelho identifica grupos cujo preservado precisa mudar para
`ANO\Organizar`, porque nenhuma cópia está na pasta anual inferida. O destino
aparece na célula do preservado. Cancelamento é informado separadamente de
falhas; as etapas já concluídas não são desfeitas e o relatório fica local.

### Incorporar Meu Drive mantendo atalhos

Em **Ferramentas → Utilitários → Incorporar Meu Drive mantendo atalhos**, informe
o link/ID de uma pasta específica do Meu Drive e de uma pasta de destino no
Drive compartilhado Banco de Imagens. A conta conectada precisa conseguir ler
a origem, criar atalhos nas pastas originais e mover os arquivos aos Drives
configurados. Não há acesso automático às contas dos outros usuários.

**Analisar pastas** pagina os metadados de todo o Banco de Imagens e percorre a
origem recursivamente, sem baixar mídias e sem seguir atalhos existentes.
Compara MD5 e tamanho, rejeitando divergências de SHA-256 quando disponível.
Arquivos sem checksum verificável (incluindo documentos nativos Google) ficam
bloqueados. A tabela é paginada em 100 linhas; duplo clique no alvo abre o Drive.

Na execução confirmada, uma pasta com o nome da origem e suas subpastas é criada
no destino. Arquivos novos são movidos com o mesmo ID e recebem atalhos em sua
pasta original. Se já houver conteúdo igual no banco, o atalho aponta à cópia
preservada e o original vai para `_TestesBanco/Banco_Apagado/APAGAR`. Entre
várias cópias, escolhe a mais antiga por data de criação, com ID como desempate.
Repetições dentro da importação usam o primeiro arquivo incorporado; se esse
arquivo não for selecionado/concluído, os dependentes não serão arquivados.
Colisões de nomes recebem sufixo por ID; pastas ambíguas de arquivamento bloqueiam.
As descrições originais acompanham os arquivos; não se fundem tags na cópia
preservada do banco. Não há exclusão definitiva nem alterações de permissões.

**Os usuários precisam de acesso ao alvo: o atalho não concede esse acesso.**
A movimentação para um Drive compartilhado muda a propriedade do arquivo e
pode alterar permissões herdadas. Confirme os acessos antes de executar.

Plano e etapas ficam em `data/shortcut_migration/journal.db`, ao lado do banco
nas instalações com outro caminho. **Abrir / retomar revisão** recupera os
planos após reiniciar. Criações têm marcadores remotos, movimentos são
condicionais e verificados por releitura. Resposta incerta interrompe o lote;
retomar confere as etapas já efetuadas. Uma criação incerta ainda não localizada
permanece bloqueada, sem repetir cegamente. Cancelar preserva etapas concluídas;
não existe rollback automático entre várias chamadas à API.

Os hashes do banco são consultados novamente antes de executar uma revisão
salva. Uma nova duplicata não prevista exige reanálise. Evite alterações
simultâneas nos arquivos envolvidos. A ferramenta atua diretamente no Drive;
InSync/Drive Desktop e o catálogo local podem levar mais tempo para refletir os
novos caminhos. Esta funcionalidade não substitui a revisão do sincronizador.

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
