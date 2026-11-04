"""
Web package for Telegram Media Downloader Homelab UI.
Exports FastAPI app, WebDownloadManager, routes and utilities.
"""
import os
import sys

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.models import (
    MkdirRequest,
    PreviewRequest,
    QueuePauseRequest,
    StartDownloadRequest,
)
from src.utils import (
    browse_directory,
    get_filesystem_shortcuts,
    is_running_in_docker,
    normalize_media_types,
)
from src.web.app import STATIC_DIR, app, main
from src.web.manager import JOBS_FILE, WebDownloadManager, manager

__all__ = [
    "app",
    "manager",
    "WebDownloadManager",
    "browse_directory",
    "get_filesystem_shortcuts",
    "is_running_in_docker",
    "normalize_media_types",
    "JOBS_FILE",
    "STATIC_DIR",
    "MkdirRequest",
    "PreviewRequest",
    "StartDownloadRequest",
    "QueuePauseRequest",
    "main",
]
