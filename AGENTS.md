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
│   └── tbmd.py               # Core application logic, CLI interface, and workers
├── tests/
│   ├── __init__.py
│   └── test_tbmd.py          # Unit test suite
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

### `src/tbmd.py`
The primary module contains:
- **`load_config()`**: Reads and validates `API_ID`, `API_HASH`, `SESSION_NAME`, `DOWNLOAD_DIR`, `BATCH_SIZE`, and `MESSAGE_LIMIT` from environment variables.
- **`safe_filename(name, default_ext, max_length)`**: Sanitizes filenames across macOS, Linux, and Windows: removes illegal characters (`:`, `/`, `\`, `*`, `?`, `"`, `<`, `>`, `|`), collapses whitespace/newlines, handles Unicode normalization, and truncates safely while preserving file extensions.
- **`get_media_filename(message, default_ext)`**: Derives meaningful filenames from message captions, document attributes, or MIME fallbacks, prefixing with `message.id` to prevent collisions.
- **`load_state()` / `save_state()`**: Manages `download_state.json` to persist downloaded message IDs per chat/channel, enabling idempotent, resumable downloads.
- **`parse_chat_input(input_val)`**: Resolves user inputs into usable entities (handles `@username`, channel links `https://t.me/...`, numeric IDs, and `-100...` supergroup/channel IDs).
- **Concurrency & Semaphores**: Uses `asyncio.Semaphore(batch_size)` to control concurrent downloads without triggering Telegram rate limits (`FloodWaitError`).
- **Interactive UI**: Dialog selection menus, progress monitoring per file worker, overall batch statistics table.

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
3. In unit tests involving `tbmd.load_config()`, always mock or patch `load_dotenv` and `os.environ` so tests do not inadvertently read local `.env` files.

---

## 6. Coding & Contribution Conventions

- **Cross-Platform Compatibility**: File paths must use `os.path` or `pathlib.Path`. Filename sanitization must always account for Windows forbidden characters, even when developed on macOS or Linux.
- **Error Resilience**: Any Telegram API calls subject to rate limits must catch `telethon.errors.FloodWaitError` and handle backoff appropriately (`await asyncio.sleep(e.seconds)`).
- **Async Safety**: Telethon uses `asyncio`. Avoid blocking synchronous I/O within async loops where possible.
- **Rich Output**: User messages, errors, warnings, and tables should maintain Rich styling consistency.
- **Unit Tests**: Whenever modifying `safe_filename`, entity resolution, or state management, add or update corresponding unit tests in `tests/test_tbmd.py`.
