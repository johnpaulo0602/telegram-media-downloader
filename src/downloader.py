import asyncio
import os
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
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
from telethon.errors import FloodWaitError
from telethon.tl.types import PeerChannel

from src.models import QueueStatus
from src.queue_db import DEFAULT_DB_FILE, QueueDatabase
from src.utils import get_media_filename, parse_chat_input, shorten

console = Console()
_queue_db: QueueDatabase | None = None


def get_queue_db(db_path: str | None = None) -> QueueDatabase:
    """Return the global QueueDatabase instance or create one with a custom path."""
    global _queue_db
    if db_path is not None:
        if _queue_db is not None and os.path.abspath(_queue_db.db_path) == os.path.abspath(db_path):
            return _queue_db
        _queue_db = QueueDatabase(db_path)
        return _queue_db
    if _queue_db is None:
        db_path_env = os.getenv("DB_PATH", os.getenv("DB_FILE", DEFAULT_DB_FILE)).strip() or DEFAULT_DB_FILE
        _queue_db = QueueDatabase(db_path_env)
    return _queue_db


def set_queue_db(db: QueueDatabase | None):
    """Set or reset the global QueueDatabase instance (useful for testing)."""
    global _queue_db
    _queue_db = db


def is_already_downloaded(chat_key: str, message_id: int, dest_path: str, db: QueueDatabase | None = None) -> bool:
    """Check the SQLite history and destination file for an existing download."""
    target_db = db if db is not None else get_queue_db()
    return target_db.is_downloaded(chat_key, message_id, dest_path)


class DownloadSession:
    """Shared state for one batch of downloads with SQLite queue tracking."""

    def __init__(
        self,
        dest_dir: str,
        chat_key: str,
        batch_size: int,
        event_callback=None,
        db: QueueDatabase | None = None,
    ):
        self.dest_dir = dest_dir
        self.chat_key = str(chat_key)
        self.batch_size = batch_size
        self.semaphore = asyncio.Semaphore(batch_size)
        self.downloaded = 0
        self.skipped = 0
        self.failed = []
        self.cancelled = False
        self.event_callback = event_callback
        self.db = db if db is not None else get_queue_db()

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
    item_id: int | None = None,
):
    """Download a single media message with progress tracking and SQLite queue updates."""
    if session.cancelled:
        return

    async with session.semaphore:
        if session.cancelled:
            return

        filename, _ = get_media_filename(message)
        dest_path = os.path.join(session.dest_dir, filename)
        file_size = (
            message.file.size
            if getattr(message, "file", None) and getattr(message.file, "size", None)
            else 0
        )

        # Enqueue or retrieve item from SQLite
        if session.db is not None:
            if item_id is None:
                item_id, _ = session.db.enqueue_item(
                    chat_id=session.chat_key,
                    message_id=message.id,
                    file_name=filename,
                    dest_path=dest_path,
                    file_size=file_size,
                )

        # Check for resuming / skipping
        if is_already_downloaded(session.chat_key, message.id, dest_path, db=session.db):
            session.skipped += 1
            if session.db is not None and item_id:
                session.db.mark_skipped(item_id, "Already exists on disk")
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

        if session.db is not None and item_id:
            session.db.mark_started(item_id)

        task = (
            files_progress.add_task(escape(shorten(filename)), total=file_size or None)
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

            final_size = os.path.getsize(dest_path) if os.path.exists(dest_path) else (file_size or 0)
            if session.db is not None and item_id:
                session.db.mark_completed(item_id, dest_path, final_size)
            session.downloaded += 1

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
            if session.db is not None and item_id:
                session.db.mark_cancelled(item_id)
            session.emit(
                "file_cancelled",
                message_id=message.id,
                filename=filename,
            )
        except Exception as e:
            session.failed.append((message.id, str(e)))
            if session.db is not None and item_id:
                session.db.mark_failed(item_id, str(e), auto_retry=True)
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
    """Enqueue media messages in SQLite first, then download concurrently with progress tracking."""
    if session.db is not None and messages:
        items_payload = []
        for msg in messages:
            fn, _ = get_media_filename(msg)
            dp = os.path.join(session.dest_dir, fn)
            fs = (
                msg.file.size
                if getattr(msg, "file", None) and getattr(msg.file, "size", None)
                else 0
            )
            items_payload.append({
                "chat_id": session.chat_key,
                "message_id": msg.id,
                "file_name": fn,
                "dest_path": dp,
                "file_size": fs,
            })
        if items_payload:
            stats = session.db.enqueue_batch(items_payload)
            console.print(
                f"[dim]Fila SQLite:[/] {stats['enqueued']} novas mídias enfileiradas "
                f"({stats['existing']} já registradas)."
            )

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
