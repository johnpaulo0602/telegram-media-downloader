import argparse
import asyncio
import json
import mimetypes
import os
import re
import sys
import unicodedata
from dotenv import load_dotenv
import questionary
from questionary import Choice
from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.prompt import IntPrompt
from rich.table import Table
from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    InputMessagesFilterVideo,
    InputMessagesFilterPhotos,
    InputMessagesFilterDocument,
    PeerChannel,
)

console = Console()

STATE_FILE = "download_state.json"

ARCHIVE_MIMES = {
    "application/zip",
    "application/x-zip-compressed",
    "application/x-zip",
    "application/x-rar-compressed",
    "application/x-7z-compressed",
    "application/x-tar",
    "application/gzip",
}
ARCHIVE_EXTS = (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz")
AUDIO_EXTS = (".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac", ".wma")

# Media types: CLI key -> label shown to the user, output folder and Telegram filter
MEDIA_TYPES = {
    "images": {"label": "Imagens", "folder": "images", "filter": InputMessagesFilterPhotos},
    "videos": {"label": "Vídeos", "folder": "videos", "filter": InputMessagesFilterVideo},
    "pdfs": {"label": "PDFs", "folder": "pdfs", "filter": InputMessagesFilterDocument},
    "archives": {"label": "ZIP e arquivos compactados", "folder": "archives", "filter": InputMessagesFilterDocument},
    "audios": {"label": "Áudios e músicas", "folder": "audio", "filter": None},
    "all": {"label": "Todos os tipos de mídia", "folder": "all_media", "filter": None},
}


def load_config():
    """
    Load and validate environment variables.
    Returns (config_dict, error_message).
    """
    load_dotenv()
    api_id_raw = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    session_name = os.getenv("SESSION_NAME", "default_session").strip() or "default_session"
    download_dir = os.getenv("DOWNLOAD_DIR", "downloads").strip() or "downloads"
    batch_size_raw = os.getenv("BATCH_SIZE", "5")
    limit_raw = os.getenv("MESSAGE_LIMIT", "2000")

    if not api_id_raw or not api_id_raw.strip():
        return None, "API_ID não definido no .env. Copie o .env.example para .env e configure suas credenciais da API do Telegram."

    try:
        api_id = int(api_id_raw.strip())
    except ValueError:
        return None, "API_ID precisa ser um número inteiro válido no .env."

    if not api_hash or not api_hash.strip():
        return None, "API_HASH não definido no .env. Copie o .env.example para .env e configure suas credenciais da API do Telegram."

    try:
        batch_size = max(1, int(batch_size_raw))
    except ValueError:
        batch_size = 5

    try:
        message_limit = int(limit_raw) if limit_raw and int(limit_raw) > 0 else None
    except ValueError:
        message_limit = 2000

    return {
        "api_id": api_id,
        "api_hash": api_hash.strip(),
        "session_name": session_name,
        "download_dir": download_dir,
        "batch_size": batch_size,
        "message_limit": message_limit,
    }, None


def safe_filename(name: str, default_ext: str = "", max_length: int = 120) -> str:
    """
    Remove invalid characters from filenames, normalize whitespace,
    truncate safely and apply extension if missing.
    """
    if not name:
        return ""

    # Replace newlines, tabs, and carriage returns
    name = re.sub(r"[\r\n\t]+", " ", name).strip()
    # Replace illegal filesystem characters
    name = re.sub(r'[\\/*?:"<>|\0]', "_", name)
    # Collapse multiple whitespace characters
    name = re.sub(r"\s+", " ", name).strip(". ")

    if not name:
        return ""

    ext = default_ext if default_ext else ""
    if ext and not ext.startswith("."):
        ext = f".{ext}"

    # If name already ends with ext (case-insensitive)
    if ext and name.lower().endswith(ext.lower()):
        base = name[: -len(ext)].strip(". ")
    else:
        base = name

    # Truncate base name to avoid path/file length limits
    allowed_base_len = max(10, max_length - len(ext))
    if len(base) > allowed_base_len:
        base = base[:allowed_base_len].rstrip(". ")

    return f"{base}{ext}" if ext else base


def guess_extension(message) -> str:
    """Return the correct extension based on message metadata or MIME type."""
    if message.file and message.file.ext:
        return message.file.ext

    if message.document and getattr(message.document, "mime_type", None):
        ext = mimetypes.guess_extension(message.document.mime_type)
        if ext:
            return ext

    # Fallback based on media type
    if message.video:
        return ".mp4"
    if message.photo:
        return ".jpg"
    if message.audio or message.voice:
        return ".mp3"
    return ""


def get_media_filename(message, default_prefix: str = "media") -> tuple[str, str]:
    """
    Determines the best filename and extension for a media message.
    Returns (filename, extension).
    """
    file_name = None
    file_ext = guess_extension(message)

    if message.file and message.file.name:
        file_name = message.file.name

    # If caption is present, use caption as filename (core project feature)
    if message.text and message.text.strip():
        safe_caption = safe_filename(message.text.strip(), default_ext=file_ext)
        if safe_caption:
            return f"{message.id}_{safe_caption}", file_ext

    # If original filename is present, use it
    if file_name:
        safe_name = safe_filename(file_name, default_ext=file_ext)
        return f"{message.id}_{safe_name}", file_ext

    # Generic fallback with message ID
    return f"{message.id}_{default_prefix}{file_ext}", file_ext


def parse_chat_input(raw_input: str):
    """Normalize user input to channel username, link, or numeric integer ID."""
    if not raw_input:
        return None
    cleaned = raw_input.strip()

    # Extract username or invite from URL
    if "t.me/" in cleaned:
        match = re.search(r"t\.me/(?:joinchat/|\+)?([a-zA-Z0-9_+-]+)", cleaned)
        if match:
            cleaned = match.group(1)

    # Detect integer ID (supports negative IDs like -1001234567890)
    is_negative = cleaned.startswith("-")
    digits_part = cleaned[1:] if is_negative else cleaned
    if digits_part.isdigit():
        return int(cleaned)

    return cleaned


def filter_media_messages(messages, media_type: str) -> list:
    """Keep only the messages that match the chosen media type."""
    if media_type == "pdfs":
        return [
            msg for msg in messages
            if msg.document and (
                msg.document.mime_type == "application/pdf"
                or (msg.file and msg.file.name and msg.file.name.lower().endswith(".pdf"))
            )
        ]
    if media_type == "archives":
        return [
            msg for msg in messages
            if msg.document and (
                msg.document.mime_type in ARCHIVE_MIMES
                or (msg.file and msg.file.name and msg.file.name.lower().endswith(ARCHIVE_EXTS))
            )
        ]
    if media_type == "audios":
        return [
            msg for msg in messages
            if getattr(msg, "audio", None)
            or getattr(msg, "voice", None)
            or (
                msg.document
                and (
                    (getattr(msg.document, "mime_type", None) or "").startswith("audio/")
                    or (msg.file and msg.file.name and msg.file.name.lower().endswith(AUDIO_EXTS))
                )
            )
        ]
    if media_type == "all":
        # For all types: ensure message has actual downloadable media
        return [msg for msg in messages if msg.media and msg.file is not None]
    # Images and videos are already filtered by Telegram
    return [msg for msg in messages if msg.media]


def normalize_text(text: str) -> str:
    """Lowercase and strip accents so 'Música' matches 'musica'."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def dialog_kind(dialog) -> str:
    """Human-readable type of a Telethon dialog."""
    if dialog.is_group:
        return "Grupo"
    if dialog.is_channel:
        return "Canal"
    if getattr(dialog.entity, "bot", False):
        return "Bot"
    return "Usuário"


def search_dialogs(dialogs, term: str) -> list:
    """Return dialogs whose name or username contains the search term."""
    needle = normalize_text(term.strip())
    if not needle:
        return []
    results = []
    for dialog in dialogs:
        username = getattr(dialog.entity, "username", None) or ""
        if needle in normalize_text(dialog.name) or needle in normalize_text(username):
            results.append(dialog)
    return results


def shorten(text: str, width: int = 40) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def load_state() -> dict:
    """Load download history from STATE_FILE."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    """Save download history to STATE_FILE."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        console.print(f"[yellow]Aviso: não foi possível salvar o estado: {escape(str(e))}[/]")


def record_download(state: dict, chat_key: str, message_id: int, file_path: str):
    """Record completed download in state dictionary."""
    chat_str = str(chat_key)
    if chat_str not in state:
        state[chat_str] = {}
    state[chat_str][str(message_id)] = {
        "path": file_path,
        "size": os.path.getsize(file_path) if os.path.exists(file_path) else 0,
    }


def is_already_downloaded(state: dict, chat_key: str, message_id: int, dest_path: str) -> bool:
    """Check if message media was already downloaded and file exists."""
    chat_records = state.get(str(chat_key), {})
    if str(message_id) in chat_records:
        rec_path = chat_records[str(message_id)].get("path")
        if rec_path and os.path.exists(rec_path) and os.path.getsize(rec_path) > 0:
            return True

    # Check directly if target file exists and is not empty
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        return True

    return False


class DownloadSession:
    """Shared state for one batch of downloads."""

    def __init__(
        self,
        dest_dir: str,
        chat_key: str,
        state: dict,
        batch_size: int,
        event_callback=None,
    ):
        self.dest_dir = dest_dir
        self.chat_key = chat_key
        self.state = state
        self.semaphore = asyncio.Semaphore(batch_size)
        self.downloaded = 0
        self.skipped = 0
        self.failed = []
        self.cancelled = False
        self.event_callback = event_callback

    def emit(self, event_type: str, **payload):
        if self.event_callback:
            try:
                self.event_callback(event_type, payload)
            except Exception:
                pass


async def download_file(
    message,
    session: DownloadSession,
    files_progress: Progress | None = None,
    overall_progress: Progress | None = None,
    overall_task=None,
):
    """Download a single media message with progress tracking and state recording."""
    if session.cancelled:
        return

    async with session.semaphore:
        if session.cancelled:
            return

        filename, _ = get_media_filename(message)
        dest_path = os.path.join(session.dest_dir, filename)

        # Check for resuming / skipping
        if is_already_downloaded(session.state, session.chat_key, message.id, dest_path):
            session.skipped += 1
            session.emit(
                "file_skipped",
                message_id=message.id,
                filename=filename,
                dest_path=dest_path,
            )
            if files_progress is not None:
                console.print(f"[cyan]↷[/] [dim]Já baixado:[/] {escape(filename)}")
            if overall_progress is not None and overall_task is not None:
                overall_progress.advance(overall_task)
            return

        file_size = message.file.size if message.file and message.file.size else None
        task = (
            files_progress.add_task(escape(shorten(filename)), total=file_size)
            if files_progress is not None
            else None
        )
        session.emit(
            "file_start",
            message_id=message.id,
            filename=filename,
            total=file_size,
        )

        def on_progress(current, total):
            if session.cancelled:
                raise asyncio.CancelledError("Download cancelado pelo usuário.")
            if files_progress is not None and task is not None:
                files_progress.update(task, completed=current, total=total or None)
            session.emit(
                "file_progress",
                message_id=message.id,
                filename=filename,
                current=current,
                total=total or file_size,
            )

        try:
            try:
                await message.download_media(file=dest_path, progress_callback=on_progress)
            except FloodWaitError as e:
                session.emit(
                    "flood_wait",
                    message_id=message.id,
                    filename=filename,
                    seconds=e.seconds,
                )
                if files_progress is not None:
                    console.print(f"[yellow]Limite de requisições do Telegram. Aguardando {e.seconds}s...[/]")
                await asyncio.sleep(e.seconds)
                if session.cancelled:
                    raise asyncio.CancelledError("Download cancelado pelo usuário.")
                # Retry once after FloodWait
                await message.download_media(file=dest_path, progress_callback=on_progress)

            record_download(session.state, session.chat_key, message.id, dest_path)
            save_state(session.state)
            session.downloaded += 1
            final_size = os.path.getsize(dest_path) if os.path.exists(dest_path) else (file_size or 0)
            session.emit(
                "file_complete",
                message_id=message.id,
                filename=filename,
                dest_path=dest_path,
                size=final_size,
            )
            if files_progress is not None:
                console.print(f"[green]✓[/] {escape(filename)}")
        except asyncio.CancelledError:
            if os.path.exists(dest_path):
                try:
                    os.remove(dest_path)
                except OSError:
                    pass
            session.emit(
                "file_cancelled",
                message_id=message.id,
                filename=filename,
            )
        except Exception as e:
            session.failed.append((message.id, str(e)))
            session.emit(
                "file_error",
                message_id=message.id,
                filename=filename,
                error=str(e),
            )
            if files_progress is not None:
                console.print(f"[red]✗ Erro ao baixar a mensagem {message.id}:[/] {escape(str(e))}")
        finally:
            if files_progress is not None and task is not None:
                files_progress.remove_task(task)
            if overall_progress is not None and overall_task is not None:
                overall_progress.advance(overall_task)


async def download_media_list(messages, session: DownloadSession):
    """Download a list of media messages concurrently, showing per-file and overall progress."""
    files_progress = Progress(
        TextColumn("  {task.description}", style="blue"),
        BarColumn(bar_width=30),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    )
    overall_progress = Progress(
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=30, complete_style="green"),
        MofNCompleteColumn(),
        TextColumn("arquivos"),
        TimeElapsedColumn(),
        console=console,
    )
    overall_task = overall_progress.add_task("Total", total=len(messages))

    with Live(Group(files_progress, overall_progress), console=console, refresh_per_second=10):
        await asyncio.gather(*[
            download_file(message, session, files_progress, overall_progress, overall_task)
            for message in messages
        ])


async def resolve_channel(client, chat_input):
    """Safely resolve channel entity with helpful fallbacks."""
    target = parse_chat_input(chat_input)
    if target is None:
        return None

    try:
        return await client.get_entity(target)
    except ValueError:
        # If integer ID failed, try channel-specific prefixes
        if isinstance(target, int):
            try:
                return await client.get_entity(PeerChannel(target))
            except Exception:
                pass
            if not str(target).startswith("-100"):
                try:
                    return await client.get_entity(int(f"-100{abs(target)}"))
                except Exception:
                    pass
        raise


async def resolve_or_report(client, raw_input: str):
    """Resolve a chat typed by the user, printing a friendly error on failure."""
    try:
        with console.status("Procurando o chat..."):
            entity = await resolve_channel(client, raw_input)
    except Exception as e:
        console.print(f"[red]Não foi possível encontrar o chat '{escape(raw_input)}':[/] {escape(str(e))}")
        console.print("[yellow]Dica: confirme que você já entrou no canal e que o username/ID está correto.[/]")
        return None
    if entity is None:
        console.print("[red]Informe um username, link ou ID.[/]")
    return entity


async def fetch_dialogs(client) -> list:
    with console.status("Carregando seus chats..."):
        return [dialog async for dialog in client.iter_dialogs()]


def dialogs_table(dialogs, title: str) -> Table:
    table = Table(title=title, title_style="bold", header_style="bold cyan", expand=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Nome", overflow="fold", max_width=50)
    table.add_column("Tipo")
    table.add_column("Username", style="magenta")
    table.add_column("ID", justify="right", style="dim")
    table.add_column("Não lidas", justify="right")
    for index, dialog in enumerate(dialogs, start=1):
        username = getattr(dialog.entity, "username", None)
        table.add_row(
            str(index),
            escape(dialog.name or "—"),
            dialog_kind(dialog),
            f"@{username}" if username else "",
            str(dialog.id),
            str(dialog.unread_count) if dialog.unread_count else "",
        )
    return table


def pick_dialog(dialogs, title: str):
    """Show dialogs as a table and ask for a row number. Returns None to go back."""
    if not dialogs:
        console.print("[yellow]Nenhum chat encontrado.[/]")
        return None

    console.print(dialogs_table(dialogs, title))
    while True:
        number = IntPrompt.ask("Número do chat [dim](0 para voltar)[/]", console=console)
        if number == 0:
            return None
        if 1 <= number <= len(dialogs):
            return dialogs[number - 1]
        console.print(f"[red]Escolha um número entre 1 e {len(dialogs)}.[/]")


async def choose_chat(client):
    """Interactive chat selection: list, search by name or type it directly."""
    dialogs = None
    while True:
        mode = await questionary.select(
            "Como deseja escolher o chat?",
            choices=[
                Choice("Listar meus canais e grupos", "list"),
                Choice("Buscar pelo nome", "search"),
                Choice("Informar username, link ou ID", "manual"),
            ],
        ).unsafe_ask_async()

        if mode == "manual":
            raw_input = await questionary.text("Username, link ou ID:").unsafe_ask_async()
            entity = await resolve_or_report(client, raw_input)
            if entity is not None:
                return entity
            continue

        if dialogs is None:
            dialogs = await fetch_dialogs(client)

        if mode == "list":
            candidates = [d for d in dialogs if d.is_channel or d.is_group]
            title = "Seus canais e grupos"
        else:
            term = await questionary.text("Buscar por:").unsafe_ask_async()
            candidates = search_dialogs(dialogs, term)
            title = f'Resultados para "{escape(term.strip())}"'

        dialog = pick_dialog(candidates, title)
        if dialog is not None:
            return dialog.entity


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tbmd",
        description="Baixa mídias em lote de canais, grupos e chats do Telegram. "
                    "Sem argumentos, abre o modo interativo.",
    )
    parser.add_argument("-c", "--chat", help="username, link ou ID do chat")
    parser.add_argument("-t", "--type", choices=MEDIA_TYPES.keys(), help="tipo de mídia a baixar")
    parser.add_argument("-o", "--output", help="pasta de destino (padrão: DOWNLOAD_DIR/<tipo>)")
    parser.add_argument("-l", "--limit", type=int, help="máximo de mensagens analisadas (0 = ilimitado)")
    parser.add_argument("-b", "--batch-size", type=int, help="número de downloads simultâneos")
    parser.add_argument("-y", "--yes", action="store_true", help="não pergunta nada que tenha valor padrão")
    parser.add_argument("--list", action="store_true", help="lista seus canais e grupos e sai")
    parser.add_argument("--search", metavar="TERMO", help="busca chats pelo nome e sai")
    parser.add_argument("--web", action="store_true", help="inicia a interface web (homelab UI)")
    parser.add_argument("--host", default=os.getenv("WEB_HOST", "0.0.0.0"), help="host do servidor web (padrão: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(os.getenv("WEB_PORT", "8000")), help="porta do servidor web (padrão: 8000)")
    return parser


def apply_overrides(config: dict, args):
    """Command-line flags take precedence over .env values."""
    if args.limit is not None:
        config["message_limit"] = args.limit if args.limit > 0 else None
    if args.batch_size is not None:
        config["batch_size"] = max(1, args.batch_size)


def print_summary(chat_title, chat_id, media_label, dest_dir, found, config):
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan")
    grid.add_column()
    grid.add_row("Chat", f"{escape(chat_title)} [dim](ID {chat_id})[/]")
    grid.add_row("Tipo", media_label)
    grid.add_row("Destino", escape(os.path.abspath(dest_dir)))
    grid.add_row("Encontradas", f"{found} mensagens com mídia")
    grid.add_row("Simultâneos", str(config["batch_size"]))
    console.print(Panel(grid, title="Resumo", border_style="cyan", expand=False))


def print_results(session: DownloadSession):
    table = Table(title="Resultado", header_style="bold", expand=False)
    table.add_column("Baixados", justify="right", style="green")
    table.add_column("Já existiam", justify="right", style="cyan")
    table.add_column("Falhas", justify="right", style="red")
    table.add_row(str(session.downloaded), str(session.skipped), str(len(session.failed)))
    console.print(table)
    console.print(f"Arquivos em [bold]{escape(os.path.abspath(session.dest_dir))}[/]")


async def main():
    args = build_parser().parse_args()

    config, error = load_config()
    if error:
        console.print(f"[red]Erro de configuração:[/] {error}")
        sys.exit(1)
    apply_overrides(config, args)

    state = load_state()

    async with TelegramClient(config["session_name"], config["api_id"], config["api_hash"]) as client:
        if args.list or args.search:
            dialogs = await fetch_dialogs(client)
            if args.search:
                console.print(dialogs_table(search_dialogs(dialogs, args.search), f'Resultados para "{escape(args.search)}"'))
            else:
                console.print(dialogs_table([d for d in dialogs if d.is_channel or d.is_group], "Seus canais e grupos"))
            return

        me = await client.get_me()
        console.print(Panel.fit(
            f"[bold]Telegram Batch Media Downloader[/]\n[dim]Conectado como {escape(utils.get_display_name(me))}[/]",
            border_style="green",
        ))

        if args.chat:
            channel = await resolve_or_report(client, args.chat)
            if channel is None:
                sys.exit(1)
        else:
            channel = await choose_chat(client)

        chat_title = utils.get_display_name(channel) or "Chat privado"
        chat_id = getattr(channel, "id", args.chat)

        media_type = args.type or await questionary.select(
            "O que deseja baixar?",
            choices=[Choice(info["label"], key) for key, info in MEDIA_TYPES.items()],
        ).unsafe_ask_async()
        media_info = MEDIA_TYPES[media_type]

        default_dir = os.path.join(config["download_dir"], media_info["folder"])
        if args.output:
            dest_dir = args.output
        elif args.yes:
            dest_dir = default_dir
        else:
            dest_dir = await questionary.path(
                "Pasta de destino:", default=default_dir, only_directories=True
            ).unsafe_ask_async()
        dest_dir = os.path.expanduser(dest_dir.strip() or default_dir)

        limit_label = config["message_limit"] or "ilimitado"
        with console.status(f"Buscando mensagens (limite: {limit_label})..."):
            filter_class = media_info["filter"]
            messages = await client.get_messages(
                channel, filter=filter_class() if filter_class else None, limit=config["message_limit"]
            )
        media_messages = filter_media_messages(messages, media_type)

        print_summary(chat_title, chat_id, media_info["label"], dest_dir, len(media_messages), config)

        if not media_messages:
            console.print("[yellow]Nenhuma mídia encontrada para o tipo escolhido.[/]")
            return

        if not args.yes:
            proceed = await questionary.confirm("Iniciar o download?", default=True).unsafe_ask_async()
            if not proceed:
                return

        os.makedirs(dest_dir, exist_ok=True)
        session = DownloadSession(dest_dir, str(chat_id), state, config["batch_size"])
        await download_media_list(media_messages, session)
        print_results(session)


if __name__ == "__main__":
    try:
        parsed_args = build_parser().parse_args()
        if parsed_args.web:
            import uvicorn
            # Ensure project root is in sys.path when invoked as `python src/tbmd.py --web`
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if project_root not in sys.path:
                sys.path.insert(0, project_root)
            uvicorn.run("src.web:app", host=parsed_args.host, port=parsed_args.port, reload=False)
        else:
            asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrompido pelo usuário.[/]")
