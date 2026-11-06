"""
Encrypted storage for the Telegram MTProto session.

Telethon stores the 256-byte ``auth_key`` in cleartext (both ``SQLiteSession``
and ``StringSession``), so a leaked database or backup would hand over a full
account session. This module keeps the session inside the SQLite database
encrypted with AES-256-GCM, using a key derived from a passphrase via scrypt.

The passphrase is never stored: it is provided by the caller, either typed by
the user in the web UI or read from the ``TBMD_SESSION_KEY`` environment
variable for unattended setups such as Docker.
"""
import os
import secrets
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from telethon import TelegramClient
from telethon.sessions import SQLiteSession, StringSession

from src.queue_db import QueueDatabase

SESSION_AAD = b"tbmd-telegram-session-v1"
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LENGTH = 32
SALT_LENGTH = 16
NONCE_LENGTH = 12


class SessionKeyError(Exception):
    """Raised when the passphrase does not decrypt the stored session."""


class SessionStore:
    """Reads and writes the encrypted Telegram session in the SQLite database."""

    def __init__(self, db: QueueDatabase):
        self.db = db

    def has_session(self) -> bool:
        """Whether an encrypted session is stored."""
        return self.db.get_session_row() is not None

    def get_metadata(self) -> dict[str, Any] | None:
        """Non-sensitive details about the stored session (never the key itself)."""
        row = self.db.get_session_row()
        if not row:
            return None
        return {
            "phone": row["phone"],
            "user_id": row["user_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def save(
        self,
        session_string: str,
        passphrase: str,
        phone: str | None = None,
        user_id: int | None = None,
    ) -> None:
        """Encrypt and persist a Telethon session string."""
        salt = secrets.token_bytes(SALT_LENGTH)
        nonce = secrets.token_bytes(NONCE_LENGTH)
        ciphertext = encrypt(session_string, passphrase, salt, nonce)
        self.db.upsert_session_row(ciphertext, salt, nonce, phone, user_id)

    def load(self, passphrase: str) -> str:
        """
        Decrypt and return the stored session string.
        Raises SessionKeyError when the passphrase is wrong or no session exists.
        """
        row = self.db.get_session_row()
        if not row:
            raise SessionKeyError("Nenhuma sessão armazenada.")
        return decrypt(
            row["ciphertext"],
            passphrase,
            row["salt"],
            row["nonce"],
        )

    def delete(self) -> bool:
        """Remove the stored session."""
        return self.db.delete_session_row()


def derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a 256-bit key from the passphrase using scrypt."""
    if not passphrase:
        raise SessionKeyError("A senha de criptografia não pode ser vazia.")
    kdf = Scrypt(salt=salt, length=KEY_LENGTH, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt(plaintext: str, passphrase: str, salt: bytes, nonce: bytes) -> bytes:
    """Encrypt a session string with AES-256-GCM."""
    key = derive_key(passphrase, salt)
    aesgcm = AESGCM(key)
    return aesgcm.encrypt(nonce, plaintext.encode("utf-8"), SESSION_AAD)


def decrypt(ciphertext: bytes, passphrase: str, salt: bytes, nonce: bytes) -> str:
    """Decrypt a session string, raising SessionKeyError on a wrong passphrase."""
    key = derive_key(passphrase, salt)
    aesgcm = AESGCM(key)
    try:
        return aesgcm.decrypt(nonce, ciphertext, SESSION_AAD).decode("utf-8")
    except InvalidTag as exc:
        raise SessionKeyError("Senha de criptografia incorreta.") from exc


def session_to_string(session: Any) -> str:
    """Serialize any Telethon session object into its string representation."""
    return StringSession.save(session)


def migrate_legacy_session(session_path: str) -> str | None:
    """
    Read a legacy ``*.session`` file and return its string representation.
    Returns None when the file is missing or holds no authentication key.
    """
    if not os.path.exists(session_path):
        return None
    try:
        legacy = SQLiteSession(session_path)
        if not legacy.auth_key:
            return None
        return session_to_string(legacy)
    except Exception:
        return None


def resolve_passphrase(explicit: str | None = None) -> str | None:
    """
    Resolve the encryption passphrase: an explicit value wins, otherwise the
    ``TBMD_SESSION_KEY`` environment variable is used.
    """
    if explicit:
        return explicit
    env_value = os.getenv("TBMD_SESSION_KEY", "").strip()
    return env_value or None


def open_client_from_store(config: dict[str, Any], db: Any) -> Any | None:
    """
    Build a TelegramClient from the encrypted session in the database.
    Returns None when no passphrase is configured, no session is stored, or the
    stored session cannot be decrypted.
    """
    passphrase = resolve_passphrase(config.get("session_key"))
    if not passphrase:
        return None

    store = SessionStore(db)
    if not store.has_session():
        return None

    try:
        session_string = store.load(passphrase)
    except SessionKeyError:
        return None

    return TelegramClient(StringSession(session_string), config["api_id"], config["api_hash"])


def save_client_session(client: Any, db: Any, passphrase: str | None = None) -> bool:
    """
    Persist the session of an authorized client, encrypted.
    Returns True when the session was stored.
    """
    key = resolve_passphrase(passphrase)
    if not key:
        return False
    SessionStore(db).save(session_to_string(client.session), key)
    return True
