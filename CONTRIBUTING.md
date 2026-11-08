# Contributing to Telegram Media Downloader

Thank you for considering contributing to this project!

## Development Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/mdic/telegram-batch-media-downloader.git
   cd telegram-batch-media-downloader
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
   Edit `.env` and fill in your `API_ID` and `API_HASH` obtained from [my.telegram.org](https://my.telegram.org).

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
