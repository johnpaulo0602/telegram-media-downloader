import os
from typing import Any
import questionary
from questionary import Choice
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import IntPrompt
from rich.table import Table

from src.downloader import DownloadSession, fetch_dialogs, resolve_or_report
from src.queue_db import QueueDatabase
from src.utils import dialog_kind, search_dialogs

console = Console()


def print_queue_stats(db: QueueDatabase, chat_id: str | int | None = None):
    """Display current SQLite queue items status breakdown."""
    stats = db.get_stats(chat_id)
    table = Table(title="Estatísticas da Fila (SQLite)", header_style="bold cyan", expand=False)
    table.add_column("Status", style="bold")
    table.add_column("Quantidade", justify="right")
    table.add_row("[yellow]Pendentes[/]", str(stats["pending"]))
    table.add_row("[blue]Baixando[/]", str(stats["downloading"]))
    table.add_row("[green]Concluídos[/]", str(stats["completed"]))
    table.add_row("[cyan]Pulados[/]", str(stats["skipped"]))
    table.add_row("[red]Falhas[/]", str(stats["failed"]))
    table.add_row("[magenta]Cancelados[/]", str(stats["cancelled"]))
    table.add_row("[bold white]Total[/]", f"[bold]{stats['total']}[/]")
    console.print(table)


def print_summary(chat_title: str, chat_id: Any, media_label: str, dest_dir: str, found: int, config: dict):
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


def dialogs_table(dialogs: list, title: str) -> Table:
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


def pick_dialog(dialogs: list, title: str):
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
