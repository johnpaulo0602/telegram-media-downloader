from dataclasses import asdict, dataclass
from enum import Enum
import sqlite3
import time
from typing import Any
from pydantic import BaseModel


class QueueStatus(str, Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class JobStatus(str, Enum):
    IDLE = "idle"
    QUEUED = "queued"
    SCANNING = "scanning"
    DOWNLOADING = "downloading"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class QueueItem:
    chat_id: str
    message_id: int
    file_name: str
    dest_path: str
    id: int | None = None
    chat_title: str | None = None
    file_size: int = 0
    media_type: str | None = None
    status: QueueStatus = QueueStatus.PENDING
    downloaded_bytes: int = 0
    attempts: int = 0
    max_attempts: int = 3
    last_error: str | None = None
    created_at: float = 0.0
    started_at: float | None = None
    completed_at: float | None = None

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()
        if isinstance(self.status, str):
            self.status = QueueStatus(self.status)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict[str, Any]) -> "QueueItem":
        data = dict(row)
        return cls(
            id=data.get("id"),
            chat_id=str(data["chat_id"]),
            chat_title=data.get("chat_title"),
            message_id=int(data["message_id"]),
            file_name=data["file_name"],
            dest_path=data["dest_path"],
            file_size=int(data.get("file_size") or 0),
            media_type=data.get("media_type"),
            status=QueueStatus(data.get("status", QueueStatus.PENDING.value)),
            downloaded_bytes=int(data.get("downloaded_bytes") or 0),
            attempts=int(data.get("attempts") or 0),
            max_attempts=int(data.get("max_attempts") or 3),
            last_error=data.get("last_error"),
            created_at=float(data.get("created_at") or time.time()),
            started_at=float(data["started_at"]) if data.get("started_at") else None,
            completed_at=float(data["completed_at"]) if data.get("completed_at") else None,
        )


@dataclass
class QueueStats:
    pending: int = 0
    downloading: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: int = 0
    total: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


# Pydantic request models for the Web API
class MkdirRequest(BaseModel):
    parent_path: str
    folder_name: str


class PreviewRequest(BaseModel):
    chat: str
    media_type: str = "videos"
    media_types: list[str] | None = None
    message_limit: int | None = None
    dest_dir: str | None = None
    append_type_folder: bool = True


class StartDownloadRequest(BaseModel):
    chat: str
    chat_title: str | None = None
    media_type: str = "videos"
    media_types: list[str] | None = None
    message_limit: int | None = None
    batch_size: int | None = None
    dest_dir: str | None = None
    append_type_folder: bool = True


class QueuePauseRequest(BaseModel):
    paused: bool | None = None


class PhoneRequest(BaseModel):
    phone: str
    passphrase: str | None = None


class CodeRequest(BaseModel):
    code: str


class PasswordRequest(BaseModel):
    password: str


class UnlockRequest(BaseModel):
    passphrase: str


class AccessTokenRequest(BaseModel):
    token: str
