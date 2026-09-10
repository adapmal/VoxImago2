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

1. **Snapshot automático** - Em produção, um banco ausente, vazio ou
   corrompido é restaurado do snapshot compartilhado verificado, quando ele
   estiver disponível.
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
máquinas de publicar ao mesmo tempo, e uma instalação baseada numa geração
antiga não pode substituir silenciosamente uma geração mais nova.

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
execute o diagnóstico/reparo e faça uma sincronização. Isso cria a primeira
geração v2. Depois atualize as demais máquinas; bancos novos podem nascer do
snapshot, enquanto bancos já existentes devem ser diagnosticados individualmente.

## 🔧 Troubleshooting

### Problemas Comuns
- **Token expirado:** Use menu Ferramentas > Logout/Login
- **Arquivos não aparecem:** Force rescan local no menu
- **Performance lenta:** Verifique índices do banco (F11)

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
