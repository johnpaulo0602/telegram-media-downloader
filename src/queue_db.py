import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Generator, Iterable

from src.models import QueueItem, QueueStats, QueueStatus

DEFAULT_DB_FILE = "tbmd.db"


class QueueDatabase:
    """
    SQLite-based persistent queue and download history manager for TBMD.
    Thread-safe and async-friendly using WAL mode and transactional operations.
    Integrates with typed domain models (QueueItem, QueueStatus, QueueStats).
    """

    def __init__(self, db_path: str = DEFAULT_DB_FILE):
        self.db_path = os.path.abspath(db_path)
        self._init_db()

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Create a configured SQLite connection context manager with WAL and auto-close."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        """Initialize database schema and indexes."""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with self.get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS queue_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    chat_title TEXT,
                    message_id INTEGER NOT NULL,
                    file_name TEXT NOT NULL,
                    dest_path TEXT NOT NULL,
                    file_size INTEGER DEFAULT 0,
                    media_type TEXT,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'downloading', 'completed', 'failed', 'skipped', 'cancelled')),
                    downloaded_bytes INTEGER DEFAULT 0,
                    attempts INTEGER DEFAULT 0,
                    max_attempts INTEGER DEFAULT 3,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    UNIQUE(chat_id, message_id)
                );

                CREATE INDEX IF NOT EXISTS idx_queue_chat_status ON queue_items (chat_id, status);
                CREATE INDEX IF NOT EXISTS idx_queue_status ON queue_items (status);
                CREATE INDEX IF NOT EXISTS idx_queue_chat_msg ON queue_items (chat_id, message_id);

                CREATE TABLE IF NOT EXISTS telegram_session (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    ciphertext BLOB NOT NULL,
                    salt BLOB NOT NULL,
                    nonce BLOB NOT NULL,
                    phone TEXT,
                    user_id INTEGER,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    def enqueue_item(
        self,
        chat_id: str | int,
        message_id: int,
        file_name: str,
        dest_path: str,
        file_size: int = 0,
        chat_title: str | None = None,
        media_type: str | None = None,
        max_attempts: int = 3,
    ) -> tuple[int, bool]:
        """
        Enqueue a single media item.
        Returns (item_id, is_new).
        If item already exists:
          - If already completed or skipped, leaves it untouched.
          - If failed/cancelled and re-enqueued, resets to 'pending'.
        """
        chat_str = str(chat_id)
        now = time.time()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, status FROM queue_items WHERE chat_id = ? AND message_id = ?",
                (chat_str, message_id),
            )
            existing = cursor.fetchone()
            if existing:
                item_id = existing["id"]
                current_status = existing["status"]
                if current_status in (QueueStatus.FAILED.value, QueueStatus.CANCELLED.value):
                    cursor.execute(
                        """
                        UPDATE queue_items
                        SET status = ?,
                            file_name = ?,
                            dest_path = ?,
                            file_size = ?,
                            last_error = NULL,
                            attempts = 0,
                            created_at = ?
                        WHERE id = ?
                        """,
                        (QueueStatus.PENDING.value, file_name, dest_path, file_size, now, item_id),
                    )
                return item_id, False

            cursor.execute(
                """
                INSERT INTO queue_items (
                    chat_id, chat_title, message_id, file_name, dest_path,
                    file_size, media_type, status, max_attempts, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_str,
                    chat_title,
                    message_id,
                    file_name,
                    dest_path,
                    file_size,
                    media_type,
                    QueueStatus.PENDING.value,
                    max_attempts,
                    now,
                ),
            )
            return cursor.lastrowid, True

    def enqueue_batch(self, items: Iterable[dict[str, Any] | QueueItem]) -> dict[str, int]:
        """
        Enqueue multiple media items in a single transaction.
        Returns stats dict: {'enqueued': X, 'existing': Y, 'total': Z}
        """
        now = time.time()
        enqueued_count = 0
        existing_count = 0

        with self.get_connection() as conn:
            cursor = conn.cursor()
            for raw_it in items:
                it = raw_it.to_dict() if isinstance(raw_it, QueueItem) else raw_it
                chat_str = str(it["chat_id"])
                msg_id = int(it["message_id"])
                fname = it["file_name"]
                dpath = it["dest_path"]
                fsize = int(it.get("file_size") or 0)
                ctitle = it.get("chat_title")
                mtype = it.get("media_type")
                max_att = int(it.get("max_attempts") or 3)

                cursor.execute(
                    "SELECT id, status FROM queue_items WHERE chat_id = ? AND message_id = ?",
                    (chat_str, msg_id),
                )
                row = cursor.fetchone()
                if row:
                    existing_count += 1
                    if row["status"] in (QueueStatus.FAILED.value, QueueStatus.CANCELLED.value):
                        cursor.execute(
                            """
                            UPDATE queue_items
                            SET status = ?,
                                file_name = ?,
                                dest_path = ?,
                                file_size = ?,
                                last_error = NULL,
                                attempts = 0,
                                created_at = ?
                            WHERE id = ?
                            """,
                            (QueueStatus.PENDING.value, fname, dpath, fsize, now, row["id"]),
                        )
                else:
                    cursor.execute(
                        """
                        INSERT INTO queue_items (
                            chat_id, chat_title, message_id, file_name, dest_path,
                            file_size, media_type, status, max_attempts, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (chat_str, ctitle, msg_id, fname, dpath, fsize, mtype, QueueStatus.PENDING.value, max_att, now),
                    )
                    enqueued_count += 1

        return {
            "enqueued": enqueued_count,
            "existing": existing_count,
            "total": enqueued_count + existing_count,
        }

    def get_item_model(self, chat_id: str | int, message_id: int) -> QueueItem | None:
        """Fetch item as typed QueueItem model."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM queue_items WHERE chat_id = ? AND message_id = ?",
                (str(chat_id), message_id),
            )
            row = cursor.fetchone()
            return QueueItem.from_row(row) if row else None

    def get_item(self, chat_id: str | int, message_id: int) -> dict[str, Any] | None:
        """Fetch item record by chat_id and message_id as dict."""
        model = self.get_item_model(chat_id, message_id)
        return model.to_dict() if model else None

    def get_item_by_id(self, item_id: int) -> dict[str, Any] | None:
        """Fetch item record by primary key id as dict."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,))
            row = cursor.fetchone()
            return QueueItem.from_row(row).to_dict() if row else None

    def get_pending_items(
        self, chat_id: str | int | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Retrieve items ready for download (status='pending') as dictionaries."""
        query = "SELECT * FROM queue_items WHERE status = ?"
        params: list[Any] = [QueueStatus.PENDING.value]
        if chat_id is not None:
            query += " AND chat_id = ?"
            params.append(str(chat_id))
        query += " ORDER BY id ASC"
        if limit and limit > 0:
            query += f" LIMIT {int(limit)}"

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [QueueItem.from_row(r).to_dict() for r in cursor.fetchall()]

    def mark_started(self, item_id: int):
        """Mark an item as currently downloading and increment its attempt counter."""
        now = time.time()
        with self.get_connection() as conn:
            conn.execute(
                """
                UPDATE queue_items
                SET status = ?,
                    started_at = ?,
                    attempts = attempts + 1,
                    last_error = NULL
                WHERE id = ?
                """,
                (QueueStatus.DOWNLOADING.value, now, item_id),
            )

    def update_progress(self, item_id: int, downloaded_bytes: int):
        """Update downloaded bytes counter for an active item."""
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE queue_items SET downloaded_bytes = ? WHERE id = ?",
                (downloaded_bytes, item_id),
            )

    def mark_completed(self, item_id: int, file_path: str | None = None, file_size: int | None = None):
        """Mark an item as successfully downloaded."""
        now = time.time()
        with self.get_connection() as conn:
            params: list[Any] = [QueueStatus.COMPLETED.value, now]
            updates = ["status = ?", "completed_at = ?", "last_error = NULL"]
            if file_path:
                updates.append("dest_path = ?")
                params.append(file_path)
            if file_size is not None and file_size >= 0:
                updates.append("file_size = ?")
                params.append(file_size)
                updates.append("downloaded_bytes = ?")
                params.append(file_size)
            params.append(item_id)
            conn.execute(f"UPDATE queue_items SET {', '.join(updates)} WHERE id = ?", params)

    def mark_skipped(self, item_id: int, reason: str = ""):
        """Mark an item as skipped (e.g. already downloaded on disk)."""
        now = time.time()
        with self.get_connection() as conn:
            conn.execute(
                """
                UPDATE queue_items
                SET status = ?,
                    completed_at = ?,
                    last_error = ?
                WHERE id = ?
                """,
                (QueueStatus.SKIPPED.value, now, reason or "Already downloaded", item_id),
            )

    def mark_failed(self, item_id: int, error_message: str, auto_retry: bool = True) -> bool:
        """
        Record failure. If attempts < max_attempts and auto_retry is True,
        re-queues to 'pending'. Otherwise sets status to 'failed'.
        Returns True if re-queued for retry, False if marked permanently failed.
        """
        now = time.time()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT attempts, max_attempts FROM queue_items WHERE id = ?", (item_id,))
            row = cursor.fetchone()
            if not row:
                return False

            attempts = row["attempts"]
            max_attempts = row["max_attempts"]

            if auto_retry and attempts < max_attempts:
                cursor.execute(
                    """
                    UPDATE queue_items
                    SET status = ?,
                        last_error = ?,
                        downloaded_bytes = 0
                    WHERE id = ?
                    """,
                    (QueueStatus.PENDING.value, str(error_message), item_id),
                )
                return True
            else:
                cursor.execute(
                    """
                    UPDATE queue_items
                    SET status = ?,
                        last_error = ?,
                        completed_at = ?
                    WHERE id = ?
                    """,
                    (QueueStatus.FAILED.value, str(error_message), now, item_id),
                )
                return False

    def mark_cancelled(self, item_id: int):
        """Mark an item as cancelled."""
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE queue_items SET status = ? WHERE id = ?",
                (QueueStatus.CANCELLED.value, item_id),
            )

    def retry_failed(self, chat_id: str | int | None = None) -> int:
        """
        Reset all 'failed' or 'cancelled' items to 'pending' and clear error.
        Returns the number of reset items.
        """
        query = f"UPDATE queue_items SET status = ?, last_error = NULL, attempts = 0 WHERE status IN (?, ?)"
        params: list[Any] = [QueueStatus.PENDING.value, QueueStatus.FAILED.value, QueueStatus.CANCELLED.value]
        if chat_id is not None:
            query += " AND chat_id = ?"
            params.append(str(chat_id))

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.rowcount

    def get_stats_model(self, chat_id: str | int | None = None) -> QueueStats:
        """Return counts of items by status as typed QueueStats."""
        query = "SELECT status, COUNT(*) as count FROM queue_items"
        params: list[Any] = []
        if chat_id is not None:
            query += " WHERE chat_id = ?"
            params.append(str(chat_id))
        query += " GROUP BY status"

        raw: dict[str, int] = {}
        total = 0
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            for row in cursor.fetchall():
                st = row["status"]
                cnt = row["count"]
                raw[st] = cnt
                total += cnt

        return QueueStats(
            pending=raw.get(QueueStatus.PENDING.value, 0),
            downloading=raw.get(QueueStatus.DOWNLOADING.value, 0),
            completed=raw.get(QueueStatus.COMPLETED.value, 0),
            failed=raw.get(QueueStatus.FAILED.value, 0),
            skipped=raw.get(QueueStatus.SKIPPED.value, 0),
            cancelled=raw.get(QueueStatus.CANCELLED.value, 0),
            total=total,
        )

    def get_stats(self, chat_id: str | int | None = None) -> dict[str, int]:
        """Return counts of items by status as dictionary."""
        return self.get_stats_model(chat_id).to_dict()

    def list_items(
        self,
        chat_id: str | int | None = None,
        status: str | QueueStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List queue items with optional filters and pagination."""
        conditions = []
        params: list[Any] = []
        if chat_id is not None:
            conditions.append("chat_id = ?")
            params.append(str(chat_id))
        if status is not None:
            st_val = status.value if isinstance(status, QueueStatus) else str(status)
            conditions.append("status = ?")
            params.append(st_val)

        query = "SELECT * FROM queue_items"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [QueueItem.from_row(r).to_dict() for r in cursor.fetchall()]

    def is_downloaded(
        self, chat_id: str | int, message_id: int, dest_path: str | None = None
    ) -> bool:
        """Check if message media was already downloaded and file exists on disk."""
        item = self.get_item(chat_id, message_id)
        if item and item.get("status") == QueueStatus.COMPLETED.value:
            path = item.get("dest_path")
            if path and os.path.exists(path) and os.path.getsize(path) > 0:
                return True

        if dest_path and os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            return True

        return False

    def import_from_json(self, json_path: str) -> int:
        """Import legacy download_state.json history into SQLite."""
        if not os.path.exists(json_path):
            return 0

        import json

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return 0

        if not isinstance(data, dict):
            return 0

        now = time.time()
        imported = 0

        with self.get_connection() as conn:
            cursor = conn.cursor()
            for chat_str, messages in data.items():
                if not isinstance(messages, dict):
                    continue
                for msg_id_str, info in messages.items():
                    try:
                        msg_id = int(msg_id_str)
                    except ValueError:
                        continue
                    file_path = info.get("path", "") if isinstance(info, dict) else str(info)
                    file_size = info.get("size", 0) if isinstance(info, dict) else 0
                    file_name = os.path.basename(file_path) if file_path else f"{msg_id}.media"

                    cursor.execute(
                        "SELECT id FROM queue_items WHERE chat_id = ? AND message_id = ?",
                        (str(chat_str), msg_id),
                    )
                    if not cursor.fetchone():
                        cursor.execute(
                            """
                            INSERT INTO queue_items (
                                chat_id, message_id, file_name, dest_path, file_size,
                                status, downloaded_bytes, created_at, completed_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(chat_str),
                                msg_id,
                                file_name,
                                file_path,
                                file_size,
                                QueueStatus.COMPLETED.value,
                                file_size,
                                now,
                                now,
                            ),
                        )
                        imported += 1

        return imported

    def export_to_state_dict(self, chat_id: str | int | None = None) -> dict[str, dict[str, Any]]:
        """Export completed downloads as a dictionary matching download_state.json format."""
        query = f"SELECT chat_id, message_id, dest_path, file_size FROM queue_items WHERE status = ?"
        params: list[Any] = [QueueStatus.COMPLETED.value]
        if chat_id is not None:
            query += " AND chat_id = ?"
            params.append(str(chat_id))

        result: dict[str, dict[str, Any]] = {}
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            for row in cursor.fetchall():
                c_id = row["chat_id"]
                m_id = str(row["message_id"])
                if c_id not in result:
                    result[c_id] = {}
                result[c_id][m_id] = {
                    "path": row["dest_path"],
                    "size": row["file_size"],
                }
        return result

    def get_session_row(self) -> dict[str, Any] | None:
        """Return the encrypted Telegram session row, or None if no session is stored."""
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM telegram_session WHERE id = 1").fetchone()
            return dict(row) if row else None

    def upsert_session_row(
        self,
        ciphertext: bytes,
        salt: bytes,
        nonce: bytes,
        phone: str | None = None,
        user_id: int | None = None,
    ) -> None:
        """Insert or replace the single encrypted Telegram session row."""
        now = time.time()
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO telegram_session (
                    id, ciphertext, salt, nonce, phone, user_id, created_at, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    ciphertext = excluded.ciphertext,
                    salt = excluded.salt,
                    nonce = excluded.nonce,
                    phone = excluded.phone,
                    user_id = excluded.user_id,
                    updated_at = excluded.updated_at
                """,
                (ciphertext, salt, nonce, phone, user_id, now, now),
            )

    def delete_session_row(self) -> bool:
        """Delete the stored Telegram session. Returns True if a row was removed."""
        with self.get_connection() as conn:
            cursor = conn.execute("DELETE FROM telegram_session WHERE id = 1")
            return cursor.rowcount > 0
