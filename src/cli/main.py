import argparse
import asyncio
import os
import sys

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import questionary
from dotenv import load_dotenv
from questionary import Choice
from rich.markup import escape
from rich.panel import Panel
from telethon import TelegramClient, utils

from src.cli.ui import (
    choose_chat,
    console,
    dialogs_table,
    print_queue_stats,
    print_results,
    print_summary,
)
from src.downloader import (
    DownloadSession,
    download_media_list,
    fetch_dialogs,
    get_queue_db,
    load_state,
    resolve_or_report,
)
from src.settings import load_settings
from src.session_store import SessionKeyError, open_client_from_store, save_client_session
from src.utils import MEDIA_TYPES, filter_media_messages, search_dialogs


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
    parser.add_argument("--queue-stats", action="store_true", help="mostra estatísticas da fila no banco SQLite e sai")
    parser.add_argument("--retry-failed", action="store_true", help="re-enfileira itens que falharam anteriormente no banco SQLite")
    parser.add_argument("--db", default=None, help="caminho do banco SQLite (padrão: tbmd.db ou $DB_PATH)")
    parser.add_argument("--web", action="store_true", help="inicia a interface web (homelab UI)")
    parser.add_argument("--host", default=os.getenv("WEB_HOST", "127.0.0.1"), help="host do servidor web (padrão: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.getenv("WEB_PORT", "8000")), help="porta do servidor web (padrão: 8000)")
    return parser


def apply_overrides(config: dict, args):
    """Command-line flags take precedence over settings values."""
    if getattr(args, "limit", None) is not None:
        config["message_limit"] = args.limit if args.limit > 0 else None
    if getattr(args, "batch_size", None) is not None:
        config["batch_size"] = max(1, args.batch_size)
    if getattr(args, "db", None) is not None:
        config["db_path"] = args.db


async def main():
    args = build_parser().parse_args()

    settings, error = load_settings()
    if error or not settings:
        console.print(f"[red]Erro de configuração:[/] {error}")
        sys.exit(1)

    config = settings.to_dict()
    apply_overrides(config, args)

    db = get_queue_db(config.get("db_path"))

    if args.queue_stats:
        print_queue_stats(db, args.chat)
        return

    if args.retry_failed:
        retried = db.retry_failed(args.chat)
        console.print(f"[green]✓[/] {retried} mídias resetadas para status 'pending' na fila do SQLite.")
        if not args.chat:
            return

    state = load_state(db=db)

    try:
        client = open_client_from_store(config, db)
    except SessionKeyError:
        console.print("[red]TBMD_SESSION_KEY não corresponde à sessão no banco. Corrija o .env.[/]")
        return

    if client is None:
        client = TelegramClient(config["session_name"], config["api_id"], config["api_hash"])

    async with client:
        if not await client.is_user_authorized():
            await client.start()
            save_client_session(client, db, config["session_key"])
            console.print("[green]✓[/] Sessão autenticada e guardada no banco SQLite (criptografada).")

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
        session = DownloadSession(dest_dir, str(chat_id), state, config["batch_size"], db=db)
        await download_media_list(media_messages, session)
        print_results(session)


def run_cli():
    try:
        load_dotenv()
        parsed_args = build_parser().parse_args()
        if parsed_args.web:
            import uvicorn
            uvicorn.run("src.web:app", host=parsed_args.host, port=parsed_args.port, reload=False)
        else:
            asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrompido pelo usuário.[/]")


if __name__ == "__main__":
    run_cli()
