import asyncio
import os
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from telethon import TelegramClient, utils

# Ensure `src.tbmd` is importable whether invoked as `python src/web.py` or `uvicorn src.web:app`
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import src.tbmd as tbmd


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


def get_filesystem_shortcuts(default_download_dir: str) -> list[dict[str, str]]:
    """Return helpful root/volume shortcuts depending on Host vs Docker environment."""
    shortcuts = []
    seen = set()

    def add_shortcut(label: str, raw_path: str, icon: str = "folder"):
        abs_path = os.path.abspath(os.path.expanduser(raw_path))
        if abs_path not in seen and (os.path.isdir(abs_path) or abs_path == os.path.abspath(default_download_dir)):
            seen.add(abs_path)
            shortcuts.append({"label": label, "path": abs_path, "icon": icon})

    add_shortcut("Downloads (Padrão)", default_download_dir, "download")
    add_shortcut("Pasta do Projeto", PROJECT_ROOT, "code")
    add_shortcut("Home (~)", "~", "home")

    # Common Docker/Homelab mount points and macOS/Linux media roots
    candidate_mounts = [
        ("/downloads", "Volume /downloads", "hard-drive"),
        ("/media", "Volume /media", "hard-drive"),
        ("/mnt", "Volume /mnt", "hard-drive"),
        ("/data", "Volume /data", "hard-drive"),
        ("/storage", "Volume /storage", "hard-drive"),
        ("/Volumes", "Volumes (macOS)", "hard-drive"),
    ]
    for mount_path, label, icon in candidate_mounts:
        if os.path.isdir(mount_path):
            add_shortcut(label, mount_path, icon)

    root_path = os.path.abspath(os.sep)
    add_shortcut(f"Raiz ({root_path})", root_path, "server")
    return shortcuts


def browse_directory(target_path: str | None, default_dir: str, show_hidden: bool = False) -> dict[str, Any]:
    """
    Inspect a directory on the host/container filesystem and return its subdirectories,
    breadcrumbs, and writability status.
    """
    raw = (target_path or "").strip() or default_dir
    expanded = os.path.abspath(os.path.expanduser(raw))

    # If the target directory does not exist yet (e.g. default `downloads/`), walk up to existing parent
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

    # Build breadcrumbs from filesystem root to `expanded`
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
    except PermissionError:
        permission_error = True
    except OSError:
        permission_error = True

    directories.sort(key=lambda d: d["name"].casefold())

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
    }


class MkdirRequest(BaseModel):
    parent_path: str
    folder_name: str


class PreviewRequest(BaseModel):
    chat: str
    media_type: str = "videos"
    message_limit: int | None = None
    dest_dir: str | None = None
    append_type_folder: bool = True


class StartDownloadRequest(BaseModel):
    chat: str
    media_type: str = "videos"
    message_limit: int | None = None
    batch_size: int | None = None
    dest_dir: str | None = None
    append_type_folder: bool = True


class WebDownloadManager:
    """Manages the TelegramClient connection, active download job, and WebSocket subscribers."""

    def __init__(self):
        self.config: dict | None = None
        self.config_error: str | None = None
        self.client: TelegramClient | None = None
        self.connected: bool = False
        self.authorized: bool = False
        self.user_info: dict | None = None
        self.dialogs_cache: list = []
        self.dialogs_updated_at: float = 0.0

        # Active or most recent job state
        self.job_status: str = "idle"  # idle, scanning, downloading, cancelling, completed, cancelled, error
        self.job_error: str | None = None
        self.job_info: dict[str, Any] = {}
        self.active_Session: tbmd.DownloadSession | None = None
        self.active_task: asyncio.Task | None = None
        self.active_workers: dict[int, dict[str, Any]] = {}
        self.recent_logs: deque = deque(maxlen=150)
        self.flood_wait_until: float = 0.0
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.total_files: int = 0

        # WebSocket clients
        self.ws_clients: set[WebSocket] = set()
        self._last_broadcast: float = 0.0
        self._broadcast_pending: bool = False

    async def initialize(self):
        """Load config and connect TelegramClient if credentials exist."""
        self.config, self.config_error = tbmd.load_config()
        if self.config_error or not self.config:
            self.connected = False
            self.authorized = False
            return

        session_path = os.path.join(PROJECT_ROOT, self.config["session_name"])
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass

        self.client = TelegramClient(
            session_path,
            self.config["api_id"],
            self.config["api_hash"],
        )
        try:
            await self.client.connect()
            self.connected = True
            self.authorized = await self.client.is_user_authorized()
            if self.authorized:
                me = await self.client.get_me()
                self.user_info = {
                    "id": me.id,
                    "name": utils.get_display_name(me),
                    "username": getattr(me, "username", None),
                    "phone": getattr(me, "phone", None),
                }
            else:
                self.user_info = None
        except Exception as e:
            self.connected = False
            self.authorized = False
            self.config_error = f"Falha ao conectar ao Telegram: {str(e)}"

    async def shutdown(self):
        if self.active_Session:
            self.active_Session.cancelled = True
        if self.active_task and not self.active_task.done():
            self.active_task.cancel()
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    def resolve_destination(self, custom_dir: str | None, media_type: str, append_type_folder: bool) -> str:
        base_default = (self.config or {}).get("download_dir", "downloads")
        media_folder = tbmd.MEDIA_TYPES.get(media_type, tbmd.MEDIA_TYPES["all"])["folder"]

        if custom_dir and custom_dir.strip():
            base = os.path.abspath(os.path.expanduser(custom_dir.strip()))
        else:
            base = os.path.abspath(os.path.expanduser(base_default))

        if append_type_folder:
            # Avoid duplicating the subfolder if user already selected `<path>/<folder>`
            if os.path.basename(base).lower() != media_folder.lower():
                return os.path.join(base, media_folder)
        return base

    def get_snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the server & job state."""
        session = self.active_Session
        downloaded = session.downloaded if session else self.job_info.get("downloaded", 0)
        skipped = session.skipped if session else self.job_info.get("skipped", 0)
        failed_count = len(session.failed) if session else self.job_info.get("failed_count", 0)
        processed = downloaded + skipped + failed_count

        now = time.time()
        elapsed = 0.0
        if self.started_at:
            end_ref = self.finished_at or now
            elapsed = max(0.0, end_ref - self.started_at)

        flood_remaining = max(0, int(self.flood_wait_until - now)) if self.flood_wait_until > now else 0

        default_dir = os.path.abspath(os.path.expanduser((self.config or {}).get("download_dir", "downloads")))

        return {
            "telegram": {
                "connected": self.connected,
                "authorized": self.authorized,
                "user": self.user_info,
                "error": self.config_error,
                "session_name": (self.config or {}).get("session_name", "default_session"),
            },
            "environment": {
                "is_docker": is_running_in_docker(),
                "default_download_dir": default_dir,
                "batch_size": (self.config or {}).get("batch_size", 5),
                "message_limit": (self.config or {}).get("message_limit", 2000),
                "media_types": [
                    {"key": k, "label": v["label"], "folder": v["folder"]}
                    for k, v in tbmd.MEDIA_TYPES.items()
                ],
            },
            "job": {
                "status": self.job_status,
                "error": self.job_error,
                "info": self.job_info,
                "total_files": self.total_files,
                "downloaded": downloaded,
                "skipped": skipped,
                "failed_count": failed_count,
                "processed": processed,
                "percent": round((processed / self.total_files) * 100, 1) if self.total_files > 0 else 0.0,
                "elapsed_seconds": round(elapsed, 1),
                "flood_wait_seconds": flood_remaining,
                "active_workers": list(self.active_workers.values()),
                "recent_logs": list(self.recent_logs),
            },
        }

    async def broadcast_state(self, force: bool = False):
        """Send current state snapshot to all connected WebSocket clients, throttled to ~10Hz."""
        if not self.ws_clients:
            return
        now = time.time()
        if not force and (now - self._last_broadcast) < 0.12:
            if not self._broadcast_pending:
                self._broadcast_pending = True
                asyncio.create_task(self._delayed_broadcast())
            return

        self._last_broadcast = now
        self._broadcast_pending = False
        payload = self.get_snapshot()
        stale = []
        for ws in list(self.ws_clients):
            try:
                await ws.send_json(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.ws_clients.discard(ws)

    async def _delayed_broadcast(self):
        await asyncio.sleep(0.12)
        if self._broadcast_pending:
            await self.broadcast_state(force=True)

    def _add_log(self, kind: str, filename: str, message: str, size: int | None = None):
        self.recent_logs.appendleft({
            "time": time.strftime("%H:%M:%S"),
            "kind": kind,  # success, skipped, error, warning, info
            "filename": filename,
            "message": message,
            "size": size,
        })

    def _on_download_event(self, event_type: str, payload: dict[str, Any]):
        """Callback invoked by `tbmd.download_file` during batch execution."""
        msg_id = payload.get("message_id")
        filename = payload.get("filename", "")
        now = time.time()

        if event_type == "file_start":
            total = payload.get("total") or 0
            self.active_workers[msg_id] = {
                "message_id": msg_id,
                "filename": filename,
                "current": 0,
                "total": total,
                "percent": 0.0,
                "speed_bps": 0.0,
                "started_at": now,
                "last_update": now,
                "status": "downloading",
            }
            asyncio.create_task(self.broadcast_state(force=True))

        elif event_type == "file_progress":
            worker = self.active_workers.get(msg_id)
            current = payload.get("current") or 0
            total = payload.get("total") or (worker["total"] if worker else 0)
            if worker:
                elapsed_file = max(0.001, now - worker["started_at"])
                speed = current / elapsed_file
                percent = round((current / total) * 100, 1) if total else 0.0
                worker.update({
                    "current": current,
                    "total": total,
                    "percent": percent,
                    "speed_bps": round(speed),
                    "last_update": now,
                    "status": "downloading",
                })
            asyncio.create_task(self.broadcast_state(force=False))

        elif event_type == "flood_wait":
            seconds = payload.get("seconds", 0)
            self.flood_wait_until = max(self.flood_wait_until, now + seconds)
            if msg_id in self.active_workers:
                self.active_workers[msg_id]["status"] = "flood_wait"
            self._add_log("warning", filename, f"Limite do Telegram (FloodWait): aguardando {seconds}s")
            asyncio.create_task(self.broadcast_state(force=True))

        elif event_type == "file_complete":
            self.active_workers.pop(msg_id, None)
            self._add_log("success", filename, "Baixado com sucesso", size=payload.get("size"))
            asyncio.create_task(self.broadcast_state(force=True))

        elif event_type == "file_skipped":
            self.active_workers.pop(msg_id, None)
            self._add_log("skipped", filename, "Já existia no destino (pulado)")
            asyncio.create_task(self.broadcast_state(force=False))

        elif event_type == "file_cancelled":
            self.active_workers.pop(msg_id, None)
            self._add_log("warning", filename, "Cancelado")
            asyncio.create_task(self.broadcast_state(force=True))

        elif event_type == "file_error":
            self.active_workers.pop(msg_id, None)
            err = payload.get("error", "Erro desconhecido")
            self._add_log("error", filename, f"Erro: {err}")
            asyncio.create_task(self.broadcast_state(force=True))

    async def get_dialogs_list(self, query: str = "", refresh: bool = False) -> list[dict[str, Any]]:
        if not self.client or not self.authorized:
            raise HTTPException(status_code=400, detail="Sessão do Telegram não está autenticada.")

        now = time.time()
        if refresh or not self.dialogs_cache or (now - self.dialogs_updated_at) > 120:
            self.dialogs_cache = [d async for d in self.client.iter_dialogs()]
            self.dialogs_updated_at = now

        if query and query.strip():
            filtered = tbmd.search_dialogs(self.dialogs_cache, query)
        else:
            filtered = [d for d in self.dialogs_cache if d.is_channel or d.is_group]

        results = []
        for d in filtered:
            username = getattr(d.entity, "username", None)
            results.append({
                "id": d.id,
                "name": d.name or "Sem nome",
                "kind": tbmd.dialog_kind(d),
                "username": f"@{username}" if username else None,
                "unread_count": d.unread_count or 0,
                "input_value": f"@{username}" if username else str(d.id),
            })
        return results

    async def preview_media(self, req: PreviewRequest) -> dict[str, Any]:
        if not self.client or not self.authorized:
            raise HTTPException(status_code=400, detail="Sessão do Telegram não está autenticada.")
        if req.media_type not in tbmd.MEDIA_TYPES:
            raise HTTPException(status_code=400, detail="Tipo de mídia inválido.")

        try:
            channel = await tbmd.resolve_channel(self.client, req.chat)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"Não foi possível encontrar o chat '{req.chat}': {e}")

        if channel is None:
            raise HTTPException(status_code=400, detail="Informe um canal, grupo, username ou ID válido.")

        media_info = tbmd.MEDIA_TYPES[req.media_type]
        limit = req.message_limit if (req.message_limit and req.message_limit > 0) else None
        filter_class = media_info["filter"]

        messages = await self.client.get_messages(
            channel,
            filter=filter_class() if filter_class else None,
            limit=limit,
        )
        media_messages = tbmd.filter_media_messages(messages, req.media_type)
        dest_dir = self.resolve_destination(req.dest_dir, req.media_type, req.append_type_folder)
        state = tbmd.load_state()
        chat_id = str(getattr(channel, "id", req.chat))
        chat_title = utils.get_display_name(channel) or str(req.chat)

        already_count = 0
        total_bytes = 0
        samples = []

        for msg in media_messages:
            fname, _ = tbmd.get_media_filename(msg)
            dest_path = os.path.join(dest_dir, fname)
            fsize = msg.file.size if msg.file and msg.file.size else 0
            total_bytes += fsize
            already = tbmd.is_already_downloaded(state, chat_id, msg.id, dest_path)
            if already:
                already_count += 1
            if len(samples) < 8:
                samples.append({
                    "id": msg.id,
                    "filename": fname,
                    "size": fsize,
                    "already_downloaded": already,
                })

        return {
            "chat_title": chat_title,
            "chat_id": chat_id,
            "media_type": req.media_type,
            "media_label": media_info["label"],
            "dest_dir": dest_dir,
            "total_found": len(media_messages),
            "already_downloaded": already_count,
            "to_download": max(0, len(media_messages) - already_count),
            "total_size_bytes": total_bytes,
            "samples": samples,
        }

    async def start_download_job(self, req: StartDownloadRequest) -> dict[str, Any]:
        if not self.client or not self.authorized:
            raise HTTPException(status_code=400, detail="Sessão do Telegram não está autenticada.")
        if self.job_status in ("scanning", "downloading"):
            raise HTTPException(status_code=409, detail="Já existe um download em andamento.")
        if req.media_type not in tbmd.MEDIA_TYPES:
            raise HTTPException(status_code=400, detail="Tipo de mídia inválido.")

        self.job_status = "scanning"
        self.job_error = None
        self.active_workers.clear()
        self.recent_logs.clear()
        self.started_at = time.time()
        self.finished_at = None
        self.total_files = 0
        self.flood_wait_until = 0.0

        dest_dir = self.resolve_destination(req.dest_dir, req.media_type, req.append_type_folder)
        batch_size = max(1, req.batch_size or (self.config or {}).get("batch_size", 5))
        limit = req.message_limit if req.message_limit is not None else (self.config or {}).get("message_limit", 2000)
        effective_limit = limit if (limit and limit > 0) else None

        self.job_info = {
            "chat_input": req.chat,
            "chat_title": req.chat,
            "chat_id": None,
            "media_type": req.media_type,
            "media_label": tbmd.MEDIA_TYPES[req.media_type]["label"],
            "dest_dir": dest_dir,
            "batch_size": batch_size,
            "message_limit": effective_limit,
            "downloaded": 0,
            "skipped": 0,
            "failed_count": 0,
        }
        await self.broadcast_state(force=True)

        self.active_task = asyncio.create_task(
            self._run_download_pipeline(req.chat, req.media_type, dest_dir, batch_size, effective_limit)
        )
        return self.get_snapshot()

    async def _run_download_pipeline(
        self,
        chat_input: str,
        media_type: str,
        dest_dir: str,
        batch_size: int,
        effective_limit: int | None,
    ):
        try:
            channel = await tbmd.resolve_channel(self.client, chat_input)
            if channel is None:
                raise ValueError(f"Chat '{chat_input}' não encontrado.")

            chat_title = utils.get_display_name(channel) or str(chat_input)
            chat_id = str(getattr(channel, "id", chat_input))
            self.job_info["chat_title"] = chat_title
            self.job_info["chat_id"] = chat_id
            self._add_log("info", chat_title, f"Buscando mensagens no chat (limite: {effective_limit or 'ilimitado'})...")
            await self.broadcast_state(force=True)

            media_info = tbmd.MEDIA_TYPES[media_type]
            filter_class = media_info["filter"]
            messages = await self.client.get_messages(
                channel,
                filter=filter_class() if filter_class else None,
                limit=effective_limit,
            )
            media_messages = tbmd.filter_media_messages(messages, media_type)
            self.total_files = len(media_messages)

            if not media_messages:
                self.job_status = "completed"
                self.finished_at = time.time()
                self._add_log("warning", chat_title, "Nenhuma mídia encontrada para o filtro selecionado.")
                await self.broadcast_state(force=True)
                return

            os.makedirs(dest_dir, exist_ok=True)
            state = tbmd.load_state()
            self.active_Session = tbmd.DownloadSession(
                dest_dir=dest_dir,
                chat_key=chat_id,
                state=state,
                batch_size=batch_size,
                event_callback=self._on_download_event,
            )
            self.job_status = "downloading"
            self._add_log("info", chat_title, f"Iniciando download de {len(media_messages)} mídias para {dest_dir}")
            await self.broadcast_state(force=True)

            await asyncio.gather(*[
                tbmd.download_file(msg, self.active_Session)
                for msg in media_messages
            ])

            self.job_info["downloaded"] = self.active_Session.downloaded
            self.job_info["skipped"] = self.active_Session.skipped
            self.job_info["failed_count"] = len(self.active_Session.failed)
            self.active_workers.clear()
            self.finished_at = time.time()

            if self.active_Session.cancelled:
                self.job_status = "cancelled"
                self._add_log("warning", chat_title, "Download interrompido pelo usuário.")
            else:
                self.job_status = "completed"
                self._add_log(
                    "info",
                    chat_title,
                    f"Lote finalizado: {self.active_Session.downloaded} baixados, "
                    f"{self.active_Session.skipped} pulados, {len(self.active_Session.failed)} falhas.",
                )
            await self.broadcast_state(force=True)

        except asyncio.CancelledError:
            self.active_workers.clear()
            self.finished_at = time.time()
            self.job_status = "cancelled"
            self._add_log("warning", chat_input, "Job cancelado pelo usuário.")
            await self.broadcast_state(force=True)
        except Exception as e:
            self.active_workers.clear()
            self.finished_at = time.time()
            self.job_status = "error"
            self.job_error = str(e)
            self._add_log("error", chat_input, f"Falha no job: {e}")
            await self.broadcast_state(force=True)

    async def cancel_job(self) -> dict[str, Any]:
        if self.active_Session:
            self.active_Session.cancelled = True
        if self.job_status in ("scanning", "downloading"):
            self.job_status = "cancelling"
        if self.active_task and not self.active_task.done():
            self.active_task.cancel()
        await self.broadcast_state(force=True)
        return self.get_snapshot()


manager = WebDownloadManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.initialize()
    yield
    await manager.shutdown()


app = FastAPI(
    title="Telegram Batch Media Downloader — Homelab UI",
    version="1.0.0",
    lifespan=lifespan,
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(index_path):
        return HTMLResponse("<h1>Interface não encontrada</h1>", status_code=404)
    return FileResponse(index_path)


@app.get("/api/status")
async def api_status():
    return manager.get_snapshot()


@app.post("/api/reconnect")
async def api_reconnect():
    await manager.initialize()
    return manager.get_snapshot()


@app.get("/api/dialogs")
async def api_dialogs(q: str = Query(default=""), refresh: bool = Query(default=False)):
    dialogs = await manager.get_dialogs_list(query=q, refresh=refresh)
    return {"dialogs": dialogs}


@app.get("/api/fs")
async def api_browse_fs(
    path: str | None = Query(default=None),
    show_hidden: bool = Query(default=False),
):
    default_dir = (manager.config or {}).get("download_dir", "downloads")
    return browse_directory(path, default_dir=default_dir, show_hidden=show_hidden)


@app.post("/api/fs/mkdir")
async def api_mkdir(req: MkdirRequest):
    safe_name = tbmd.safe_filename(req.folder_name.strip())
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(status_code=400, detail="Nome de pasta inválido.")

    parent = os.path.abspath(os.path.expanduser(req.parent_path.strip() or "."))
    new_dir = os.path.join(parent, safe_name)
    try:
        os.makedirs(new_dir, exist_ok=True)
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Sem permissão para criar pasta em '{parent}'.")
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"Erro ao criar pasta: {e}")

    default_dir = (manager.config or {}).get("download_dir", "downloads")
    return browse_directory(new_dir, default_dir=default_dir)


@app.post("/api/downloads/preview")
async def api_preview_downloads(req: PreviewRequest):
    return await manager.preview_media(req)


@app.post("/api/downloads/start")
async def api_start_downloads(req: StartDownloadRequest):
    return await manager.start_download_job(req)


@app.post("/api/downloads/cancel")
async def api_cancel_downloads():
    return await manager.cancel_job()


@app.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    manager.ws_clients.add(websocket)
    try:
        await websocket.send_json(manager.get_snapshot())
        while True:
            # Keep connection alive and respond to optional client pings
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_json(manager.get_snapshot())
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        manager.ws_clients.discard(websocket)


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("WEB_HOST", "0.0.0.0")
    port = int(os.getenv("WEB_PORT", "8000"))
    uvicorn.run("src.web:app", host=host, port=port, reload=False)
