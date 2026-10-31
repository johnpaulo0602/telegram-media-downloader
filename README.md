# Telegram Batch Media Downloader

[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![Telethon](https://img.shields.io/badge/telethon-1.38%2B-green.svg)](https://github.com/LonamiWebs/Telethon)
[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-orange.svg)](LICENSE)

Uma ferramenta de linha de comando em Python, poderosa e confiável, para baixar em lote mídias (vídeos, fotos, PDFs, arquivos compactados e todos os anexos) de canais, grupos e chats privados do Telegram.

Construída sobre o [Telethon](https://github.com/LonamiWebs/Telethon), com downloads simultâneos, sessões retomáveis, renomeação automática de arquivos e acompanhamento interativo do progresso.

---

## Principais Funcionalidades

- **Processamento em Lote Simultâneo**: baixa arquivos em paralelo usando um semáforo assíncrono, aproveitando ao máximo a banda sem desrespeitar os limites da API do Telegram.
- **Downloads Retomáveis (`download_state.json`)**: registra automaticamente os downloads concluídos. Ao reiniciar o script, os arquivos já baixados são ignorados, sem serem baixados novamente.
- **Resolução Flexível de Canal / Chat**: aceita nomes de usuário de canais (`@canal`), links diretos (`https://t.me/canal`), IDs positivos e IDs completos de canais do Telegram (`-100...`).
- **Renomeação Inteligente de Arquivos**:
  - Usa a legenda da mensagem como nome do arquivo, quando disponível.
  - Na falta dela, usa o nome original do documento no Telegram ou a extensão correspondente ao tipo MIME.
  - Remove automaticamente caracteres inválidos e quebras de linha problemáticas, e trunca títulos muito longos para garantir compatibilidade com os sistemas de arquivos do macOS, Linux e Windows.
  - Prefixa os arquivos com o ID da mensagem para evitar colisões de nome quando várias postagens têm a mesma legenda.
- **Filtragem por Tipo de Mídia**:
  1. **Imagens** (`.jpg`, `.png` etc.)
  2. **Vídeos** (`.mp4`, `.mkv` etc.)
  3. **Documentos PDF** (`.pdf`)
  4. **ZIP e Arquivos Compactados** (`.zip`, `.rar`, `.7z`, `.tar`, `.gz` etc.)
  5. **Todos os Tipos de Mídia** (baixa qualquer mensagem que contenha anexos de mídia, ignorando mensagens vazias ou só de texto)
- **Tratamento de Limite de Requisições e Erros**: lida com o limite de requisições do Telegram (`FloodWaitError`) aguardando exatamente o tempo de penalidade e retomando automaticamente.

---

## Requisitos

- Python 3.8, 3.9, 3.10, 3.11, 3.12, 3.13 ou 3.14+
- Credenciais de uma conta do Telegram (`API_ID` e `API_HASH`)

---

## Início Rápido e Instalação

### 1. Clone o repositório

```bash
git clone https://github.com/mdic/telegram-batch-media-downloader.git
cd telegram-batch-media-downloader
```

### 2. Crie e ative um ambiente virtual

- **macOS / Linux:**
  ```bash
  python3 -m venv venv
  source venv/bin/activate
  ```

- **Windows (Prompt de Comando / PowerShell):**
  ```cmd
  python -m venv venv
  venv\Scripts\activate
  ```

### 3. Instale as dependências

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Configure as variáveis de ambiente

Copie `.env.example` para `.env`:

```bash
cp .env.example .env
```

Abra o `.env` em um editor de texto e preencha suas credenciais da API do Telegram:

```env
API_ID=1234567
API_HASH=0123456789abcdef0123456789abcdef
SESSION_NAME=default_session
DOWNLOAD_DIR=downloads
BATCH_SIZE=5
MESSAGE_LIMIT=2000
```

> **Como obter seu `API_ID` e `API_HASH` do Telegram:**
> 1. Acesse [https://my.telegram.org](https://my.telegram.org) com o número de telefone da sua conta do Telegram.
> 2. Clique em **API development tools**.
> 3. Preencha o formulário curto (título do app e nome curto).
> 4. Copie o `api_id` e o `api_hash` para o arquivo `.env`.

### 5. Execute o downloader

```bash
python src/tbmd.py
```

Na primeira execução, o Telethon vai pedir no terminal:
1. Seu número de telefone (com código do país, ex.: `+55...` ou `+1...`).
2. O código de confirmação de login enviado ao seu app do Telegram.
3. Sua senha de verificação em duas etapas (se estiver ativada na sua conta).

Depois de autenticado, a sessão fica salva localmente em `<SESSION_NAME>.session`, então você não precisará fazer login novamente.

---

## Guia de Uso

### Modo interativo

Execute sem argumentos:

```bash
python src/tbmd.py
```

Os menus são navegados com as setas ↑/↓ e Enter.

1. **Escolha do chat**: há três formas:
   - **Listar meus canais e grupos**: mostra uma tabela com nome, tipo, username, ID e mensagens não lidas. Basta digitar o número da linha.
   - **Buscar pelo nome**: digite parte do nome ou do username. A busca ignora maiúsculas e acentos (`musica` encontra "Música"), e também inclui chats privados e bots.
   - **Informar username, link ou ID**: `@canalexemplo`, `canalexemplo`, `https://t.me/canalexemplo`, `-1001234567890` ou `1234567890`.
2. **Tipo de mídia**: Imagens, Vídeos, PDFs, ZIP e arquivos compactados, ou Todos os tipos de mídia.
3. **Pasta de destino**: o padrão é `<DOWNLOAD_DIR>/<tipo>` (ex.: `downloads/videos`). Você pode digitar qualquer outro caminho, inclusive com `~`, e a tecla Tab completa nomes de pastas.
4. **Resumo e confirmação**: aparece um painel com o chat, o tipo, a pasta e quantas mídias foram encontradas, antes de começar.
5. **Progresso**: há uma barra para cada arquivo em andamento (com tamanho, velocidade e tempo restante) e uma barra geral. No final, uma tabela mostra quantos arquivos foram baixados, quantos já existiam e quantos falharam.

### Linha de comando (sem perguntas)

Todas as escolhas também podem ser passadas como argumentos. O que não for informado é perguntado no modo interativo.

```bash
# Lista seus canais e grupos
python src/tbmd.py --list

# Busca chats pelo nome
python src/tbmd.py --search "inglês"

# Baixa todos os vídeos de um canal para uma pasta específica, sem confirmações
python src/tbmd.py -c @canalexemplo -t videos -o ~/Videos/curso -y
```

| Opção | Descrição |
|---|---|
| `-c`, `--chat` | Username, link ou ID do chat |
| `-t`, `--type` | `images`, `videos`, `pdfs`, `archives` ou `all` |
| `-o`, `--output` | Pasta de destino (padrão: `<DOWNLOAD_DIR>/<tipo>`) |
| `-l`, `--limit` | Máximo de mensagens analisadas (`0` = ilimitado); substitui o `MESSAGE_LIMIT` |
| `-b`, `--batch-size` | Downloads simultâneos; substitui o `BATCH_SIZE` |
| `-y`, `--yes` | Usa a pasta padrão e não pede confirmação |
| `--list` | Lista seus canais e grupos e sai |
| `--search TERMO` | Busca chats pelo nome e sai |
| `--web` | Inicia a interface Web (Homelab UI) |
| `--host` | Host do servidor Web (padrão: `0.0.0.0`) |
| `--port` | Porta do servidor Web (padrão: `8000`) |

### Interface Web (Homelab UI)

Para usar o painel visual em página única (com seleção de canais, explorador de pastas do servidor/volumes e progresso em tempo real via WebSocket):

```bash
python src/tbmd.py --web
# ou especificar porta:
python src/tbmd.py --web --port 8080
```

Acesse `http://localhost:8000` (ou o IP do seu servidor no homelab).

#### Executando com Docker Compose no Homelab

1. Autentique sua sessão na primeira vez via terminal (`python src/tbmd.py --list`) para gerar o arquivo `default_session.session`.
2. Suba o container:
   ```bash
   docker compose up -d --build
   ```
3. Os volumes mapeados no `docker-compose.yml` (como `/downloads`, `/media`, `/mnt`) aparecem automaticamente nos atalhos do seletor de pastas da interface web.

---

## Referência de Configuração

| Variável | Padrão | Descrição |
|---|---|---|
| `API_ID` | *Obrigatório* | ID da API do Telegram (número inteiro) obtido em my.telegram.org |
| `API_HASH` | *Obrigatório* | Hash da API do Telegram (texto) obtido em my.telegram.org |
| `SESSION_NAME` | `default_session` | Nome do arquivo de sessão do Telethon |
| `DOWNLOAD_DIR` | `downloads` | Pasta base dos downloads; os arquivos vão para `<DOWNLOAD_DIR>/<tipo>` |
| `BATCH_SIZE` | `5` | Número máximo de downloads simultâneos |
| `MESSAGE_LIMIT` | `2000` | Máximo de mensagens analisadas no canal (use `0` para ilimitado) |

---

## Retomando Downloads Interrompidos

O downloader registra os downloads concluídos em `download_state.json`:
- Se a internet cair ou você encerrar o script com `Ctrl+C`, basta executar `python src/tbmd.py` novamente.
- Os arquivos já baixados serão reconhecidos e ignorados, e o download continua de onde parou.

---

## Testes

Execute a suíte de testes automatizados com o `unittest` nativo do Python:

```bash
python -m unittest discover tests
```

---

## Solução de Problemas

- **`Erro de configuração: API_ID não definido no .env`**:
  Verifique se você copiou `.env.example` para `.env` e definiu tanto `API_ID` quanto `API_HASH`.
- **`Não foi possível encontrar o chat`**:
  Se o canal for privado, confirme que sua conta do Telegram já entrou no canal antes de baixar. Use `--search` ou a opção "Buscar pelo nome" para encontrar o chat sem precisar do ID.
- **`Limite de requisições do Telegram. Aguardando...`**:
  O Telegram limita temporariamente os downloads de mídia quando muitas requisições são feitas em pouco tempo. O downloader aguarda automaticamente o fim do tempo de espera e retoma. Para reduzir esse limite, diminua o `BATCH_SIZE` para `2` ou `3` no `.env`.

---

## Licença

Este projeto está licenciado sob a [GNU General Public License v3.0](LICENSE).
