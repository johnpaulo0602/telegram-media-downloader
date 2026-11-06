# AGENTS.md — Telegram Media Downloader

This file provides architectural context, development guidelines, operational commands, and safety constraints for AI agents and coding assistants working on the **Telegram Media Downloader** repository.

---

## 1. Project Overview

**Telegram Media Downloader (TBMD)** is a robust, asynchronous Python CLI tool designed to batch-download media (videos, photos, PDFs, compressed archives, and general media attachments) from Telegram channels, groups, and private chats.

### Tech Stack
- **Language**: Python 3.8+ (tested up to 3.14+)
- **Core Engine**: [Telethon](https://github.com/LonamiWebs/Telethon) (MTProto Telegram client) + [cryptg](https://github.com/cher-nov/cryptg) (fast crypto acceleration)
- **UI & UX**: [Rich](https://github.com/Textualize/rich) (tables, layout, multi-worker progress bars, live updates) and [Questionary](https://github.com/tmbo/questionary) (interactive terminal selections)
- **Configuration**: [python-dotenv](https://github.com/theskumar/python-dotenv) (`.env`)
- **Testing**: Python's native `unittest` module

---

## 2. Directory Structure

```
telegram-media-downloader/
├── src/
│   ├── __init__.py
│   ├── settings.py           # Centralized configuration & environment loader
│   ├── models.py             # Enums, dataclasses (QueueItem, QueueStats) & Pydantic DTOs
│   ├── utils.py              # Pure utilities: filenames, MIME, search, fs browser
│   ├── queue_db.py           # SQLite persistent queue & history database manager
│   ├── session_store.py      # Encrypted Telegram session storage (AES-256-GCM)
│   ├── downloader.py         # Telegram engine, concurrent download workers & sessions
│   ├── tbmd.py               # Thin CLI entrypoint (`python src/tbmd.py`)
│   ├── cli/                  # Command-line interface package
│   │   ├── __init__.py
│   │   ├── main.py           # CLI argument parsing and main execution loop
│   │   └── ui.py             # Rich progress tables and Questionary interactive menus
│   └── web/                  # Web dashboard and FastAPI service
│       ├── __init__.py       # Web package re-exports
│       ├── app.py            # FastAPI application, lifespan & WebSocket router
│       ├── manager.py        # WebDownloadManager (FIFO multi-job coordinator)
│       ├── auth.py           # TelegramAuthService (QR / phone login, session storage)
│       └── routes/           # Modular endpoint routers
│           ├── __init__.py
│           ├── auth.py       # /api/auth (login, unlock, logout, access token)
│           ├── downloads.py  # /api/downloads, /api/jobs, /api/dialogs
│           ├── filesystem.py # /api/fs (host/docker filesystem browser)
│           └── queue.py      # /api/queue (SQLite queue stats, retry, items)
├── tests/
│   ├── __init__.py
│   ├── test_models.py        # Domain model and dataclass tests
│   ├── test_queue_db.py      # SQLite queue and state unit tests
│   ├── test_settings.py      # Environment configuration loading tests
│   ├── test_tbmd.py          # Core CLI and download unit test suite
│   └── test_web.py           # Web endpoints and queue API tests
├── .env.example              # Template for required environment variables
├── .gitignore                # Git exclusions (credentials, sessions, state, media)
├── CONTRIBUTING.md           # Contribution guidelines
├── LICENSE                   # GPL-3.0 License
├── README.md                 # User documentation in Portuguese
├── requirements.txt          # Python dependencies
└── AGENTS.md                 # Agent instructions (this file)
```

---

## 3. Core Architecture & Modules

### `src/settings.py`
Provides typed application configuration:
- **`Settings`**: Strongly-typed dataclass containing `api_id`, `api_hash`, `session_name`, `download_dir`, `batch_size`, `message_limit`, `db_path`, `web_host`, and `web_port`.
- **`load_settings()`**: Reads `.env` and validates credentials with helpful error messages.

### `src/models.py`
Domain and data transfer models:
- **`QueueStatus` & `JobStatus`**: Enums preventing typos in state transitions.
- **`QueueItem`**: Dataclass representing individual media items in the SQLite database.
- **`QueueStats`**: Aggregated queue counters (`pending`, `downloading`, `completed`, `failed`, `skipped`, `cancelled`, `total`).
- **Pydantic Request Models**: `PreviewRequest`, `StartDownloadRequest`, `QueuePauseRequest`, `MkdirRequest`.

### `src/utils.py`
Stateless utility functions:
- Filename sanitization across platforms (`safe_filename`).
- MIME & media filters (`filter_media_messages`).
- Channel identifier parsing (`parse_chat_input`).
- Docker & filesystem browser helpers (`browse_directory`, `get_filesystem_shortcuts`).

### `src/queue_db.py`
Manages the persistent SQLite database (`tbmd.db`) in WAL mode:
- **`QueueDatabase`**: Schema creation, bulk item pre-enqueueing (`enqueue_batch`), status transitions, attempt tracking with retries (`max_attempts`), manual batch retries (`retry_failed`), and legacy JSON migration (`import_from_json`).

### `src/downloader.py`
The core Telegram downloader:
- **`DownloadSession`**: Holds semaphore, progress callback, and state.
- **`download_file()`**: Downloads single media messages with event emissions and SQLite status updates.
- **`download_media_list()`**: Pre-enqueues all discovered media in SQLite first, then processes downloads concurrently.

### `src/cli/`
CLI user interface and entrypoint:
- **`src/cli/ui.py`**: Rich tables and Questionary interactive menus.
- **`src/cli/main.py`**: Argument parser (`--queue-stats`, `--retry-failed`, `--db`, `--web`) and execution loop.

### `src/tbmd.py`
The only entrypoint (`python src/tbmd.py`): makes the project root importable and delegates to `src.cli.main.run_cli()`. It intentionally re-exports nothing — import from the concrete modules (`src.utils`, `src.downloader`, `src.settings`, `src.queue_db`, `src.cli.main`) instead.

### `src/session_store.py`
Encrypted Telegram session storage:
- **`SessionStore`**: Reads and writes the session in the `telegram_session` table, encrypted with AES-256-GCM (key derived via scrypt).
- **`derive_key()` / `encrypt()` / `decrypt()`**: Pure crypto primitives; a wrong passphrase raises `SessionKeyError` through the GCM authentication tag.
- **`resolve_passphrase()`**: Explicit value first, then `TBMD_SESSION_KEY` from the environment.
- **`migrate_legacy_session()`**: Converts a legacy `*.session` file into a string session.

### `src/web/`
FastAPI web application and homelab dashboard:
- **`src/web/manager.py`**: `WebDownloadManager` coordinating FIFO jobs, background queue processor, and WebSocket live updates.
- **`src/web/auth.py`**: `TelegramAuthService` driving the QR and phone login flows and persisting the encrypted session.
- **`src/web/routes/`**: Cleanly divided routers for authentication, downloads, queue management, and filesystem browsing.
- **`src/web/app.py`**: Application factory, lifespan, static mounting and the `TBMD_WEB_TOKEN` access guard.

---

## 4. Setup & Running

### Environment Setup
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Running the CLI
```bash
python src/tbmd.py
```

### Running Tests
Always run the test suite to ensure non-regression:
```bash
python3 -m unittest discover tests
```

---

## 5. Critical Security & Safety Rules

1. **NEVER commit sensitive credentials or auth files**:
   - `.env` and `.env.*` (contains `API_ID`, `API_HASH`, phone numbers)
   - `*.session` and `*.session-journal` (SQLite session files containing active MTProto authentication keys and tokens)
   - `download_state.json` (local state)
   - `downloads/` directory (actual downloaded media)
2. **Never hardcode credentials** into tests or source files.
3. In unit tests involving `load_settings()`, call it with `load_env=False` and patch `os.environ` so tests do not inadvertently read local `.env` files.
4. **Never persist the Telegram session in cleartext.** Use `src/session_store.py` so the `auth_key` is always encrypted at rest. The 2FA password and the encryption passphrase are transient and must never be written to disk or logs.

---

## 6. Coding & Contribution Conventions

- **Cross-Platform Compatibility**: File paths must use `os.path` or `pathlib.Path`. Filename sanitization must always account for Windows forbidden characters, even when developed on macOS or Linux.
- **Error Resilience**: Any Telegram API calls subject to rate limits must catch `telethon.errors.FloodWaitError` and handle backoff appropriately (`await asyncio.sleep(e.seconds)`).
- **Async Safety**: Telethon uses `asyncio`. Avoid blocking synchronous I/O within async loops where possible.
- **Rich Output**: User messages, errors, warnings, and tables should maintain Rich styling consistency.
- **Unit Tests**: Whenever modifying `safe_filename`, entity resolution, or state management, add or update corresponding unit tests in `tests/test_tbmd.py`.

---

## 7. Git Workflow

**Commit locally only. Never push.**

- Create the commit with `git commit` and stop there. Do not run `git push`, and do not
  configure credentials, remotes, or branch upstream to make a push possible.
- The maintainer reviews the commit and pushes manually when ready.
- Never amend or rewrite existing commits; create a new one instead.
- Before committing, run the test suite and review `git status` to confirm no
  credentials, session files, or databases are staged.
