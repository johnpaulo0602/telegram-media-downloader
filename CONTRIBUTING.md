# Contributing to Telegram Media Downloader

Thank you for considering contributing to this project!

## Development Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/johnpaulo0602/telegram-media-downloader.git
   cd telegram-media-downloader
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up environment variables:**
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and fill in `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org), plus a strong `TBMD_SESSION_KEY` to encrypt the Telegram session in SQLite. Authenticate through the web panel or CLI using the same database and key.

## Running Tests

Run the test suite using Python's built-in `unittest` runner:
```bash
python -m unittest discover tests
```

## Guidelines

- Keep code clean, readable, and typed where appropriate.
- Ensure all filenames and paths are sanitized across Windows, macOS, and Linux.
- Add or update unit tests in `tests/test_tbmd.py` when fixing bugs or introducing new features.
- Never commit `.env` or session files (`*.session`, `*.session-journal`) to version control.

Authentication changes must cover successful login adoption by the web manager, pending QR/2FA state, encrypted CLI persistence, and legacy migration. Never log session strings or passwords. Test with temporary databases and mocked Telegram clients.

## Persistence and Docker

- Use `DB_PATH` for encrypted authentication, web jobs, FIFO order, queue pause/counter and media records. Test state changes, restarts and one-time history imports using temporary databases.
- Keep `docker-compose.example.yml` as the tracked template. Copy it to the ignored `docker-compose.yml` for local deployment; never commit the local configuration, databases or runtime history.
- Docker stores the database in the `tbmd-data` volume at `/app/data/tbmd.db`. Keep the encryption key stable across restarts.
- Upgrade instructions for old web history are in [docs/UPGRADING.md](docs/UPGRADING.md). New installations require only the SQLite database and configured volumes.
