"""
CLI entrypoint for Telegram Media Downloader.

Ensures the project root is importable, then hands control to `src.cli.main`.
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.cli.main import run_cli

if __name__ == "__main__":
    run_cli()
