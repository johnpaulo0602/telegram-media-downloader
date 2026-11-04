import asyncio
from collections import deque
import json
import os
import time
from typing import Any

from fastapi import HTTPException, WebSocket
from telethon import TelegramClient, utils

from src.downloader import (
    DownloadSession,
    download_file,
    get_queue_db,
    is_already_downloaded,
    load_state,
    resolve_channel,
)
from src.models import (
    PreviewRequest,
    StartDownloadRequest,
)
from src.settings import load_settings
from src.utils import (
    MEDIA_TYPES,
    dialog_kind,
    filter_media_messages,
    get_media_filename,
    is_running_in_docker,
    normalize_media_types,
    search_dialogs,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JOBS_FILE = os.path.join(PROJECT_ROOT, "jobs_history.json")


class WebDownloadManager:
    """Manages the TelegramClient connection, FIFO download job queue, and WebSocket subscribers."""

    def __init__(self):
        self.config: dict | None = None
        self.config_error: str | None = None
        self.client: TelegramClient | None = None
        self.connected: bool = False
        self.authorized: bool = False
        self.user_info: dict | None = None
        self.dialogs_cache: list = []
        self.dialogs_updated_at: float = 0.0

        # Multi-job queue & history state
        self.jobs: list[dict[str, Any]] = []
        self.job_counter: int = 1000
        self.queue_paused: bool = False
        self.current_job_id: str | None = None
        self.queue_runner_task: asyncio.Task | None = None

        # Active or most recent job state
        self.job_status: str = "idle"
        self.job_error: str | None = None
        self.job_info: dict[str, Any] = {}
        self.active_Session: DownloadSession | None = None
        self.active_task: asyncio.Task | None = None
        self.active_workers: dict[int, dict[str, Any]] = {}
        self.recent_logs: deque = deque(maxlen=250)
        self.flood_wait_until: float = 0.0
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.total_files: int = 0
        self.completed_bytes: int = 0

        # WebSocket clients
        self.ws_clients: set[WebSocket] = set()
        self._last_broadcast: float = 0.0
        self._broadcast_pending: bool = False

        self.load_jobs_history()

    def load_jobs_history(self):
        """Load persisted jobs history from disk if available."""
        if not os.path.exists(JOBS_FILE):
            return
        try:
            with open(JOBS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                loaded_jobs = data.get("jobs", [])
                self.job_counter = int(data.get("job_counter", 1000))
                self.queue_paused = bool(data.get("queue_paused", False))
            elif isinstance(data, list):
                loaded_jobs = data
            else:
                loaded_jobs = []

            cleaned_jobs = []
            for job in loaded_jobs:
                if not isinstance(job, dict) or not job.get("id"):
                    continue
                if job.get("status") in ("scanning", "downloading", "cancelling"):
                    job["status"] = "cancelled"
                    job["error"] = "Interrompido por reinicialização do servidor"
                    job["active_workers"] = []
                    job["total_speed_bps"] = 0
                cleaned_jobs.append(job)
            self.jobs = cleaned_jobs
        except (json.JSONDecodeError, OSError, ValueError):
            self.jobs = []

    def save_jobs_history(self):
        """Persist jobs list and queue counter to disk."""
        try:
            serializable_jobs = []
            for j in self.jobs[-100:]:
                copy_j = dict(j)
                copy_j["recent_logs"] = list(copy_j.get("recent_logs", []))[:150]
                copy_j["active_workers"] = [] if copy_j.get("status") not in ("scanning", "downloading") else copy_j.get("active_workers", [])
                serializable_jobs.append(copy_j)
            payload = {
                "job_counter": self.job_counter,
                "queue_paused": self.queue_paused,
                "jobs": serializable_jobs,
            }
            with open(JOBS_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except OSError:
            pass

    def _next_job_id(self) -> str:
        self.job_counter += 1
        return f"TK-{self.job_counter}"

    def _get_job(self, job_id: str | None) -> dict[str, Any] | None:
        if not job_id:
            return None
        for job in self.jobs:
            if job.get("id") == job_id:
                return job
        return None

    async def initialize(self):
        """Load settings and connect TelegramClient if credentials exist."""
        settings, err = load_settings()
        if err or not settings:
            self.config = None
            self.config_error = err
            self.connected = False
            self.authorized = False
            return

        self.config = settings.to_dict()
        self.config_error = None

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
                self._ensure_queue_processor()
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
        if self.queue_runner_task and not self.queue_runner_task.done():
            self.queue_runner_task.cancel()
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    def resolve_destination(self, custom_dir: str | None, media_type: str | list[str], append_type_folder: bool) -> str:
        base_default = (self.config or {}).get("download_dir", "downloads")
        if isinstance(media_type, list):
            effective_key = media_type[0] if len(media_type) == 1 else "all"
        else:
            effective_key = media_type
        media_folder = MEDIA_TYPES.get(effective_key, MEDIA_TYPES["all"])["folder"]

        if custom_dir and custom_dir.strip():
            base = os.path.abspath(os.path.expanduser(custom_dir.strip()))
        else:
            base = os.path.abspath(os.path.expanduser(base_default))

        if append_type_folder:
            if os.path.basename(base).lower() != media_folder.lower():
                return os.path.join(base, media_folder)
        return base

    def _sync_active_job_metrics(self):
        job = self._get_job(self.current_job_id)
        if not job:
            return

        session = self.active_Session
        downloaded = session.downloaded if session else job.get("downloaded", 0)
        skipped = session.skipped if session else job.get("skipped", 0)
        failed_count = len(session.failed) if session else job.get("failed_count", 0)
        processed = downloaded + skipped + failed_count

        now = time.time()
        elapsed = 0.0
        if self.started_at:
            end_ref = self.finished_at or now
            elapsed = max(0.0, end_ref - self.started_at)

        flood_remaining = max(0, int(self.flood_wait_until - now)) if self.flood_wait_until > now else 0
        active_list = list(self.active_workers.values())
        total_speed_bps = round(sum(w.get("speed_bps", 0) for w in active_list))
        in_flight_bytes = sum(w.get("current", 0) for w in active_list)
        bytes_downloaded = self.completed_bytes + in_flight_bytes

        total_files = self.total_files
        percent = round((processed / total_files) * 100, 1) if total_files > 0 else 0.0

        peak_speed = max(job.get("peak_speed_bps", 0), total_speed_bps)
        avg_speed = round(bytes_downloaded / elapsed) if elapsed > 0.5 and bytes_downloaded > 0 else total_speed_bps

        total_size_bytes = job.get("total_size_bytes", 0)
        eta_seconds = None
        if self.job_status == "downloading":
            remaining_bytes = max(0, total_size_bytes - bytes_downloaded)
            speed_ref = total_speed_bps or avg_speed
            if remaining_bytes > 0 and speed_ref > 0:
                eta_seconds = int(remaining_bytes / speed_ref)
            elif total_files > processed and elapsed > 1 and processed > 0:
                rate_files = processed / elapsed
                eta_seconds = int((total_files - processed) / rate_files)

        job.update({
            "status": self.job_status,
            "error": self.job_error,
            "total_files": total_files,
            "downloaded": downloaded,
            "skipped": skipped,
            "failed_count": failed_count,
            "processed": processed,
            "percent": percent,
            "elapsed_seconds": round(elapsed, 1),
            "flood_wait_seconds": flood_remaining,
            "total_speed_bps": total_speed_bps if self.job_status == "downloading" else 0,
            "peak_speed_bps": peak_speed,
            "avg_speed_bps": avg_speed,
            "bytes_downloaded": bytes_downloaded,
            "eta_seconds": eta_seconds,
            "active_workers": active_list if self.job_status in ("scanning", "downloading") else [],
            "recent_logs": list(self.recent_logs),
        })

    def get_snapshot(self) -> dict[str, Any]:
        self._sync_active_job_metrics()

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

        active_list = list(self.active_workers.values())
        total_speed_bps = sum(w.get("speed_bps", 0) for w in active_list)
        in_flight_bytes = sum(w.get("current", 0) for w in active_list)
        bytes_downloaded = self.completed_bytes + in_flight_bytes

        queue_pos = 0
        running_count = 0
        queued_count = 0
        completed_count = 0
        failed_count_jobs = 0

        serialized_jobs = []
        for j in self.jobs:
            st = j.get("status", "queued")
            if st in ("scanning", "downloading", "cancelling"):
                running_count += 1
                j["queue_position"] = None
            elif st == "queued":
                queued_count += 1
                queue_pos += 1
                j["queue_position"] = queue_pos
            elif st == "completed":
                completed_count += 1
                j["queue_position"] = None
            elif st in ("error", "cancelled"):
                failed_count_jobs += 1
                j["queue_position"] = None
            serialized_jobs.append(j)

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
                    for k, v in MEDIA_TYPES.items()
                ],
            },
            "queue": {
                "paused": self.queue_paused,
                "running_count": running_count,
                "queued_count": queued_count,
                "completed_count": completed_count,
                "failed_count": failed_count_jobs,
                "total_count": len(serialized_jobs),
                "current_job_id": self.current_job_id,
            },
            "jobs": serialized_jobs,
            "job": {
                "id": self.current_job_id,
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
                "total_speed_bps": round(total_speed_bps),
                "bytes_downloaded": bytes_downloaded,
                "active_workers": active_list,
                "recent_logs": list(self.recent_logs),
            },
        }

    async def broadcast_state(self, force: bool = False):
        if not self.ws_clients:
            self._sync_active_job_metrics()
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

    def _add_log(self, kind: str, filename: str, message: str, size: int | None = None, job_id: str | None = None):
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "kind": kind,
            "filename": filename,
            "message": message,
            "size": size,
        }
        self.recent_logs.appendleft(entry)
        target_id = job_id or self.current_job_id
        job = self._get_job(target_id)
        if job is not None:
            logs = job.setdefault("recent_logs", [])
            logs.insert(0, entry)
            if len(logs) > 250:
                del logs[250:]

    def _on_download_event(self, event_type: str, payload: dict[str, Any]):
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
            file_bytes = payload.get("size") or 0
            self.completed_bytes += file_bytes
            self._add_log("success", filename, "Baixado com sucesso", size=file_bytes)
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
            filtered = search_dialogs(self.dialogs_cache, query)
        else:
            filtered = [d for d in self.dialogs_cache if d.is_channel or d.is_group]

        results = []
        for d in filtered:
            username = getattr(d.entity, "username", None)
            results.append({
                "id": d.id,
                "name": d.name or "Sem nome",
                "kind": dialog_kind(d),
                "username": f"@{username}" if username else None,
                "unread_count": d.unread_count or 0,
                "input_value": f"@{username}" if username else str(d.id),
            })

        cleaned_q = (query or "").strip()
        if not results and cleaned_q and (
            cleaned_q.startswith("@")
            or "t.me/" in cleaned_q
            or cleaned_q.lstrip("-").isdigit()
        ):
            try:
                entity = await resolve_channel(self.client, cleaned_q)
                if entity is not None:
                    username = getattr(entity, "username", None)
                    entity_id = utils.get_peer_id(entity)
                    is_group = bool(getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False))
                    is_channel = bool(getattr(entity, "broadcast", False))
                    kind = "Grupo" if is_group else ("Canal" if is_channel else ("Bot" if getattr(entity, "bot", False) else "Usuário"))
                    results.append({
                        "id": entity_id,
                        "name": utils.get_display_name(entity) or cleaned_q,
                        "kind": kind,
                        "username": f"@{username}" if username else None,
                        "unread_count": 0,
                        "input_value": f"@{username}" if username else str(entity_id),
                    })
            except Exception:
                pass

        return results

    async def preview_media(self, req: PreviewRequest) -> dict[str, Any]:
        if not self.client or not self.authorized:
            raise HTTPException(status_code=400, detail="Sessão do Telegram não está autenticada.")

        media_types = normalize_media_types(req.media_type, req.media_types)

        try:
            channel = await resolve_channel(self.client, req.chat)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"Não foi possível encontrar o chat '{req.chat}': {e}")

        if channel is None:
            raise HTTPException(status_code=400, detail="Informe um canal, grupo, username ou ID válido.")

        limit = req.message_limit if (req.message_limit and req.message_limit > 0) else None
        filter_class = MEDIA_TYPES[media_types[0]]["filter"] if len(media_types) == 1 else None

        messages = await self.client.get_messages(
            channel,
            filter=filter_class() if filter_class else None,
            limit=limit,
        )
        media_messages = filter_media_messages(messages, media_types)
        dest_dir = self.resolve_destination(req.dest_dir, media_types, req.append_type_folder)
        state = load_state()
        chat_id = str(getattr(channel, "id", req.chat))
        chat_title = utils.get_display_name(channel) or str(req.chat)

        already_count = 0
        total_bytes = 0
        samples = []

        for msg in media_messages:
            fname, _ = get_media_filename(msg)
            dest_path = os.path.join(dest_dir, fname)
            fsize = msg.file.size if msg.file and msg.file.size else 0
            total_bytes += fsize
            already = is_already_downloaded(state, chat_id, msg.id, dest_path)
            if already:
                already_count += 1
            if len(samples) < 8:
                samples.append({
                    "id": msg.id,
                    "filename": fname,
                    "size": fsize,
                    "already_downloaded": already,
                })

        media_labels = [MEDIA_TYPES[k]["label"] for k in media_types]
        return {
            "chat_title": chat_title,
            "chat_id": chat_id,
            "media_type": media_types[0],
            "media_types": media_types,
            "media_label": " + ".join(media_labels),
            "media_labels": media_labels,
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

        media_types = normalize_media_types(req.media_type, req.media_types)
        media_labels = [MEDIA_TYPES[k]["label"] for k in media_types]

        dest_dir = self.resolve_destination(req.dest_dir, media_types, req.append_type_folder)
        batch_size = max(1, req.batch_size or (self.config or {}).get("batch_size", 5))
        limit = req.message_limit if req.message_limit is not None else (self.config or {}).get("message_limit", 2000)
        effective_limit = limit if (limit and limit > 0) else None

        job_id = self._next_job_id()
        now = time.time()
        initial_title = (req.chat_title or "").strip() or req.chat.strip()

        new_job: dict[str, Any] = {
            "id": job_id,
            "chat_input": req.chat.strip(),
            "chat_title": initial_title,
            "chat_id": None,
            "media_type": media_types[0],
            "media_types": media_types,
            "media_label": " + ".join(media_labels),
            "media_labels": media_labels,
            "dest_dir": dest_dir,
            "append_type_folder": req.append_type_folder,
            "batch_size": batch_size,
            "message_limit": effective_limit,
            "status": "queued",
            "error": None,
            "total_files": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed_count": 0,
            "processed": 0,
            "percent": 0.0,
            "bytes_downloaded": 0,
            "total_size_bytes": 0,
            "total_speed_bps": 0,
            "peak_speed_bps": 0,
            "avg_speed_bps": 0,
            "eta_seconds": None,
            "elapsed_seconds": 0.0,
            "flood_wait_seconds": 0,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "active_workers": [],
            "recent_logs": [
                {
                    "time": time.strftime("%H:%M:%S"),
                    "kind": "info",
                    "filename": initial_title,
                    "message": f"Job {job_id} adicionado à fila ({' + '.join(media_labels)})",
                    "size": None,
                }
            ],
        }
        self.jobs.append(new_job)
        self.save_jobs_history()

        self._ensure_queue_processor()
        await asyncio.sleep(0)
        await self.broadcast_state(force=True)
        return self.get_snapshot()

    def _ensure_queue_processor(self):
        if self.queue_runner_task is None or self.queue_runner_task.done():
            try:
                loop = asyncio.get_running_loop()
                self.queue_runner_task = loop.create_task(self._process_queue())
            except RuntimeError:
                pass

    async def _process_queue(self):
        while True:
            if self.queue_paused or not self.client or not self.authorized:
                break

            next_job = None
            for job in self.jobs:
                if job.get("status") == "queued":
                    next_job = job
                    break

            if next_job is None:
                break

            await self._execute_job(next_job)
            self.save_jobs_history()

    async def _execute_job(self, job: dict[str, Any]):
        job_id = job["id"]
        self.current_job_id = job_id
        self.job_status = "scanning"
        self.job_error = None
        self.active_workers.clear()
        self.recent_logs.clear()
        for existing_log in reversed(job.get("recent_logs", [])):
            self.recent_logs.appendleft(existing_log)

        self.started_at = time.time()
        self.finished_at = None
        self.total_files = 0
        self.completed_bytes = 0
        self.flood_wait_until = 0.0

        job["status"] = "scanning"
        job["error"] = None
        job["started_at"] = self.started_at
        job["finished_at"] = None

        self.job_info = {
            "id": job_id,
            "chat_input": job["chat_input"],
            "chat_title": job["chat_title"],
            "chat_id": job.get("chat_id"),
            "media_type": job["media_type"],
            "media_types": job["media_types"],
            "media_label": job["media_label"],
            "media_labels": job["media_labels"],
            "dest_dir": job["dest_dir"],
            "batch_size": job["batch_size"],
            "message_limit": job["message_limit"],
            "downloaded": 0,
            "skipped": 0,
            "failed_count": 0,
        }
        await self.broadcast_state(force=True)

        self.active_task = asyncio.current_task()
        await self._run_download_pipeline(job)
        self.active_task = None

    async def _run_download_pipeline(self, job: dict[str, Any]):
        chat_input = job["chat_input"]
        media_types = job.get("media_types") or [job.get("media_type", "videos")]
        dest_dir = job["dest_dir"]
        batch_size = job["batch_size"]
        effective_limit = job["message_limit"]

        try:
            channel = await resolve_channel(self.client, chat_input)
            if channel is None:
                raise ValueError(f"Chat '{chat_input}' não encontrado.")

            chat_title = utils.get_display_name(channel) or str(chat_input)
            chat_id = str(getattr(channel, "id", chat_input))
            username = getattr(channel, "username", None)
            if username:
                job["username"] = f"@{username}"
            job["chat_title"] = chat_title
            job["chat_id"] = chat_id
            self.job_info["chat_title"] = chat_title
            self.job_info["chat_id"] = chat_id

            self._add_log("info", chat_title, f"Buscando mensagens no chat (limite: {effective_limit or 'ilimitado'})...")
            await self.broadcast_state(force=True)

            filter_class = MEDIA_TYPES[media_types[0]]["filter"] if len(media_types) == 1 else None

            messages = await self.client.get_messages(
                channel,
                filter=filter_class() if filter_class else None,
                limit=effective_limit,
            )
            media_messages = filter_media_messages(messages, media_types)
            self.total_files = len(media_messages)
            total_size_bytes = sum(
                (m.file.size if getattr(m, "file", None) and getattr(m.file, "size", None) else 0)
                for m in media_messages
            )
            job["total_size_bytes"] = total_size_bytes

            if not media_messages:
                self.job_status = "completed"
                self.finished_at = time.time()
                job["finished_at"] = self.finished_at
                self._add_log("warning", chat_title, "Nenhuma mídia encontrada para o filtro selecionado.")
                await self.broadcast_state(force=True)
                return

            os.makedirs(dest_dir, exist_ok=True)
            state = load_state()
            db = get_queue_db((self.config or {}).get("db_path"))

            # Pre-enqueue all messages into the persistent SQLite database
            if media_messages and db:
                items_payload = []
                for msg in media_messages:
                    fn, _ = get_media_filename(msg)
                    dp = os.path.join(dest_dir, fn)
                    fs = (
                        msg.file.size
                        if getattr(msg, "file", None) and getattr(msg.file, "size", None)
                        else 0
                    )
                    items_payload.append({
                        "chat_id": chat_id,
                        "chat_title": chat_title,
                        "message_id": msg.id,
                        "file_name": fn,
                        "dest_path": dp,
                        "file_size": fs,
                        "media_type": media_types[0] if len(media_types) == 1 else "all",
                    })
                db.enqueue_batch(items_payload)

            self.active_Session = DownloadSession(
                dest_dir=dest_dir,
                chat_key=chat_id,
                state=state,
                batch_size=batch_size,
                event_callback=self._on_download_event,
                db=db,
            )
            self.job_status = "downloading"
            self._add_log("info", chat_title, f"Iniciando download de {len(media_messages)} mídias para {dest_dir}")
            await self.broadcast_state(force=True)

            await asyncio.gather(*[
                download_file(msg, self.active_Session)
                for msg in media_messages
            ])

            self.job_info["downloaded"] = self.active_Session.downloaded
            self.job_info["skipped"] = self.active_Session.skipped
            self.job_info["failed_count"] = len(self.active_Session.failed)
            self.active_workers.clear()
            self.finished_at = time.time()
            job["finished_at"] = self.finished_at

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
            job["finished_at"] = self.finished_at
            self.job_status = "cancelled"
            self._add_log("warning", chat_input, "Job cancelado pelo usuário.")
            await self.broadcast_state(force=True)
        except Exception as e:
            self.active_workers.clear()
            self.finished_at = time.time()
            job["finished_at"] = self.finished_at
            self.job_status = "error"
            self.job_error = str(e)
            self._add_log("error", chat_input, f"Falha no job: {e}")
            await self.broadcast_state(force=True)
        finally:
            self._sync_active_job_metrics()
            self.active_Session = None

    async def cancel_job(self, job_id: str | None = None) -> dict[str, Any]:
        target_id = job_id or self.current_job_id
        job = self._get_job(target_id)

        if job and job.get("status") == "queued":
            job["status"] = "cancelled"
            job["finished_at"] = time.time()
            self._add_log("warning", job.get("chat_title", ""), "Job removido da fila pelo usuário.", job_id=job["id"])
            self.save_jobs_history()
            await self.broadcast_state(force=True)
            return self.get_snapshot()

        if not job_id or job_id == self.current_job_id:
            if self.active_Session:
                self.active_Session.cancelled = True
            if self.job_status in ("scanning", "downloading"):
                self.job_status = "cancelling"
            if self.active_task and not self.active_task.done():
                self.active_task.cancel()
            await asyncio.sleep(0)
            self.save_jobs_history()
            await self.broadcast_state(force=True)
        return self.get_snapshot()

    async def delete_job(self, job_id: str) -> dict[str, Any]:
        job = self._get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job não encontrado.")
        if job_id == self.current_job_id and self.job_status in ("scanning", "downloading", "cancelling"):
            await self.cancel_job(job_id)
        self.jobs = [j for j in self.jobs if j.get("id") != job_id]
        if self.current_job_id == job_id:
            self.current_job_id = None
            self.job_status = "idle"
        self.save_jobs_history()
        await self.broadcast_state(force=True)
        return self.get_snapshot()

    async def move_job_up(self, job_id: str) -> dict[str, Any]:
        idx = next((i for i, j in enumerate(self.jobs) if j.get("id") == job_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Job não encontrado.")
        if self.jobs[idx].get("status") != "queued":
            raise HTTPException(status_code=400, detail="Apenas jobs na fila podem ser reordenados.")

        prev_queued_idx = None
        for i in range(idx - 1, -1, -1):
            if self.jobs[i].get("status") == "queued":
                prev_queued_idx = i
                break

        if prev_queued_idx is not None:
            self.jobs[prev_queued_idx], self.jobs[idx] = self.jobs[idx], self.jobs[prev_queued_idx]
            self.save_jobs_history()
            await self.broadcast_state(force=True)
        return self.get_snapshot()

    async def retry_job(self, job_id: str) -> dict[str, Any]:
        job = self._get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job não encontrado.")
        if job.get("status") in ("scanning", "downloading", "cancelling"):
            raise HTTPException(status_code=400, detail="Este job já está em execução.")

        job.update({
            "status": "queued",
            "error": None,
            "downloaded": 0,
            "skipped": 0,
            "failed_count": 0,
            "processed": 0,
            "percent": 0.0,
            "bytes_downloaded": 0,
            "total_speed_bps": 0,
            "eta_seconds": None,
            "elapsed_seconds": 0.0,
            "started_at": None,
            "finished_at": None,
            "active_workers": [],
        })
        self._add_log("info", job.get("chat_title", ""), "Job re-enfileirado para execução.", job_id=job_id)
        self.save_jobs_history()
        self._ensure_queue_processor()
        await asyncio.sleep(0)
        await self.broadcast_state(force=True)
        return self.get_snapshot()

    async def clear_completed_jobs(self) -> dict[str, Any]:
        self.jobs = [
            j for j in self.jobs
            if j.get("status") in ("queued", "scanning", "downloading", "cancelling")
        ]
        if self.current_job_id and not self._get_job(self.current_job_id):
            self.current_job_id = None
            self.job_status = "idle"
        self.save_jobs_history()
        await self.broadcast_state(force=True)
        return self.get_snapshot()

    async def toggle_queue_pause(self, paused: bool | None = None) -> dict[str, Any]:
        if paused is None:
            self.queue_paused = not self.queue_paused
        else:
            self.queue_paused = bool(paused)
        self.save_jobs_history()
        if not self.queue_paused:
            self._ensure_queue_processor()
            await asyncio.sleep(0)
        await self.broadcast_state(force=True)
        return self.get_snapshot()


manager = WebDownloadManager()
