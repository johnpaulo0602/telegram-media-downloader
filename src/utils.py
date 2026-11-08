import mimetypes
import os
from pathlib import Path
import re
import shutil
import unicodedata
from typing import Any
from fastapi import HTTPException
from telethon.tl.types import (
    InputMessagesFilterVideo,
    InputMessagesFilterPhotos,
    InputMessagesFilterDocument,
)

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
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp")

MEDIA_TYPES = {
    "images": {"label": "Imagens", "folder": "images", "filter": InputMessagesFilterPhotos},
    "videos": {"label": "Vídeos", "folder": "videos", "filter": InputMessagesFilterVideo},
    "pdfs": {"label": "PDFs", "folder": "pdfs", "filter": InputMessagesFilterDocument},
    "archives": {"label": "ZIP e arquivos compactados", "folder": "archives", "filter": InputMessagesFilterDocument},
    "audios": {"label": "Áudios e músicas", "folder": "audio", "filter": None},
    "all": {"label": "Todos os tipos de mídia", "folder": "all_media", "filter": None},
}


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


def normalize_text(text: str) -> str:
    """Lowercase and strip accents so 'Música' matches 'musica'."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def shorten(text: str, width: int = 40) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


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
    """Return dialogs whose name, @username, t.me URL, or ID matches the search term."""
    raw = (term or "").strip()
    if not raw:
        return []

    needle = normalize_text(raw.lstrip("@"))
    parsed = parse_chat_input(raw)
    parsed_needle = normalize_text(str(parsed).lstrip("@")) if parsed is not None else ""

    if not needle and not parsed_needle:
        return []

    results = []
    for dialog in dialogs:
        name_norm = normalize_text(getattr(dialog, "name", "") or "")
        username_norm = normalize_text(getattr(dialog.entity, "username", None) or "")
        dialog_id_str = str(getattr(dialog, "id", "") or "")
        entity_id_str = str(getattr(dialog.entity, "id", "") or "")

        matched = False
        for token in {t for t in (needle, parsed_needle) if t}:
            if (
                token in name_norm
                or token in username_norm
                or (dialog_id_str and token in dialog_id_str)
                or (entity_id_str and token in entity_id_str)
            ):
                matched = True
                break

        if matched:
            results.append(dialog)
    return results


def _matches_single_type(msg, media_type: str, strict_unfiltered: bool = False) -> bool:
    """Return True if a single message matches `media_type`."""
    if not getattr(msg, "media", None):
        return False

    doc = getattr(msg, "document", None)
    mime = (getattr(doc, "mime_type", None) or "").lower() if doc else ""
    file_obj = getattr(msg, "file", None)
    fname = (getattr(file_obj, "name", None) or "").lower() if file_obj else ""

    if media_type == "pdfs":
        return bool(doc and (mime == "application/pdf" or fname.endswith(".pdf")))

    if media_type == "archives":
        return bool(doc and (mime in ARCHIVE_MIMES or fname.endswith(ARCHIVE_EXTS)))

    if media_type == "audios":
        return bool(
            getattr(msg, "audio", None)
            or getattr(msg, "voice", None)
            or (doc and (mime.startswith("audio/") or fname.endswith(AUDIO_EXTS)))
        )

    if media_type == "videos":
        if getattr(msg, "video", None) or mime.startswith("video/") or fname.endswith(VIDEO_EXTS):
            return True
        return not strict_unfiltered and doc is None and not getattr(msg, "photo", None)

    if media_type == "images":
        if getattr(msg, "photo", None) or mime.startswith("image/") or fname.endswith(IMAGE_EXTS):
            return True
        return not strict_unfiltered and doc is None and not getattr(msg, "video", None)

    if media_type == "all":
        return file_obj is not None

    return True


def filter_media_messages(messages, media_type: str | list[str]) -> list:
    """Keep only the messages that match the chosen media type(s)."""
    if isinstance(media_type, (list, tuple, set)):
        types_list = [t for t in media_type if t in MEDIA_TYPES]
        if not types_list or "all" in types_list:
            return [msg for msg in messages if getattr(msg, "media", None) and getattr(msg, "file", None) is not None]
        strict = len(types_list) > 1
        return [
            msg for msg in messages
            if any(_matches_single_type(msg, t, strict_unfiltered=strict) for t in types_list)
        ]

    return [msg for msg in messages if _matches_single_type(msg, media_type, strict_unfiltered=False)]


def normalize_media_types(media_type: str | None, media_types: list[str] | None) -> list[str]:
    """Validate and normalize single or multiple media_type inputs into a non-empty list of keys."""
    raw_list: list[str] = []
    if media_types:
        raw_list = [m.strip() for m in media_types if m and m.strip()]
    elif media_type and media_type.strip():
        raw_list = [media_type.strip()]
    else:
        raw_list = ["videos"]

    valid: list[str] = []
    for item in raw_list:
        if item not in MEDIA_TYPES:
            raise HTTPException(status_code=400, detail=f"Tipo de mídia inválido: '{item}'.")
        if item not in valid:
            valid.append(item)

    if not valid:
        raise HTTPException(status_code=400, detail="Selecione ao menos um tipo de mídia válido.")

    if "all" in valid:
        return ["all"]
    return valid


def is_running_in_docker() -> bool:
    """Detect if the application is running inside a Docker/OCI container."""
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    try:
        if os.path.exists("/proc/1/cgroup"):
            with open("/proc/1/cgroup", "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                if "docker" in content or "kubepods" in content or "containerd" in content:
                    return True
    except OSError:
        pass
    return False


def channel_folder_name(title: str, chat_id: str = "") -> str:
    """Normalize a Telegram title into one portable snake_case path component."""
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")[:100].rstrip("_")
    if not slug:
        slug = "canal_" + (re.sub(r"[^a-z0-9]+", "_", str(chat_id).lower()).strip("_") or "telegram")
    if slug in {"con", "prn", "aux", "nul"} or re.fullmatch(r"(?:com|lpt)[1-9]", slug):
        slug = "canal_" + slug
    return slug


def get_filesystem_shortcuts(default_download_dir: str) -> list[dict[str, str]]:
    """Recommend only the configured persistent downloads volume."""
    return [{"label": "Downloads (Padrão)",
             "path": os.path.abspath(os.path.expanduser(default_download_dir)),
             "icon": "download"}]


def browse_directory(target_path: str | None, default_dir: str, show_hidden: bool = False) -> dict[str, Any]:
    """
    Inspect a directory on the host/container filesystem and return its subdirectories,
    breadcrumbs, and writability status.
    """
    raw = (target_path or "").strip() or default_dir
    expanded = os.path.abspath(os.path.expanduser(raw))

    probe = expanded
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent

    exists = os.path.isdir(expanded)
    browse_target = expanded if exists else probe
    if not os.path.isdir(browse_target):
        browse_target = os.path.abspath(".")

    writable = os.access(browse_target, os.W_OK)

    path_obj = Path(expanded)
    parts = path_obj.parts
    breadcrumbs = []
    accum = ""
    for index, part in enumerate(parts):
        if index == 0:
            accum = part
            label = part
        else:
            accum = os.path.join(accum, part)
            label = part
        breadcrumbs.append({"name": label, "path": accum})

    parent_dir = os.path.dirname(browse_target)
    if parent_dir == browse_target:
        parent_dir = None

    directories = []
    permission_error = False
    try:
        with os.scandir(browse_target) as entries:
            for entry in entries:
                try:
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                    if not show_hidden and entry.name.startswith("."):
                        continue
                    if entry.name in ("__pycache__", "node_modules", "venv", ".venv"):
                        continue
                    dir_writable = os.access(entry.path, os.W_OK)
                    directories.append({
                        "name": entry.name,
                        "path": os.path.abspath(entry.path),
                        "writable": dir_writable,
                    })
                except OSError:
                    continue
    except (PermissionError, OSError):
        permission_error = True

    directories.sort(key=lambda d: d["name"].casefold())

    disk_free = 0
    disk_total = 0
    try:
        usage = shutil.disk_usage(browse_target)
        disk_free = usage.free
        disk_total = usage.total
    except OSError:
        pass

    return {
        "requested_path": expanded,
        "current_path": browse_target,
        "exists": exists,
        "writable": writable,
        "permission_error": permission_error,
        "parent_path": parent_dir,
        "breadcrumbs": breadcrumbs,
        "directories": directories,
        "shortcuts": get_filesystem_shortcuts(default_dir),
        "is_docker": is_running_in_docker(),
        "disk_free_bytes": disk_free,
        "disk_total_bytes": disk_total,
    }
