"""
Telegram authentication service for the web UI.

The Telegram session *is* the authentication: when a valid session is stored
the user is already logged in, otherwise the UI walks through the login flow
(QR code, or phone + code + 2FA password). The session is persisted
encrypted in the SQLite database by :mod:`src.session_store`.

This module owns the temporary client used during the login flow, so the main
client is only replaced once a session has been successfully obtained.
"""
import asyncio
import base64
import io
import os
import time
from typing import Any, Callable, Awaitable

import segno
from telethon import TelegramClient, utils
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from src.session_store import (
    SessionKeyError,
    SessionStore,
    migrate_legacy_session,
    session_to_string,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

AUTH_UNAUTHENTICATED = "unauthenticated"
AUTH_LOCKED = "locked"
AUTH_CODE_SENT = "code_sent"
AUTH_PASSWORD_REQUIRED = "password_required"
AUTH_QR_READY = "qr_ready"
AUTH_QR_EXPIRED = "qr_expired"
AUTH_AUTHORIZED = "authorized"


class AuthError(Exception):
    """Raised when the authentication flow cannot continue."""


def render_qr(data: str) -> str:
    """Render a QR code as a base64 PNG data URI, served from our own host."""
    qr = segno.make(data, error="m")
    buffer = io.BytesIO()
    qr.save(buffer, kind="png", scale=4, border=2)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


class TelegramAuthService:
    """Drives the Telegram login flow and owns the encrypted session storage."""

    def __init__(
        self,
        config_provider: Callable[[], dict[str, Any] | None],
        db_provider: Callable[[], Any],
        on_authorized: Callable[[], Awaitable[None]] | None = None,
    ):
        self._on_authorized = on_authorized
        self._config_provider = config_provider
        self._db_provider = db_provider
        self.store: SessionStore | None = None
        self.client: TelegramClient | None = None
        self.user_info: dict[str, Any] | None = None
        self.status: str = AUTH_UNAUTHENTICATED
        self.error: str | None = None
        self.passphrase: str | None = None
        self.qr_png: str | None = None
        self.qr_expires_at: float | None = None
        self._auth_client: TelegramClient | None = None
        self._qr = None
        self._qr_task: asyncio.Task | None = None
        self._phone: str | None = None
        self._phone_code_hash: str | None = None

    @property
    def authorized(self) -> bool:
        return self.client is not None and self.status == AUTH_AUTHORIZED

    def _config(self) -> dict[str, Any]:
        return self._config_provider() or {}

    async def restore(self) -> None:
        """
        Reconnect using the stored session. Falls back to the legacy
        ``*.session`` file when the database has no session yet.
        """
        config = self._config()
        self.store = SessionStore(self._db_provider())
        self.passphrase = config["session_key"]

        if self.store.has_session():
            try:
                session_string = self.store.load(self.passphrase)
            except SessionKeyError:
                self.status = AUTH_LOCKED
                self.error = (
                    "A senha em TBMD_SESSION_KEY não corresponde à sessão armazenada. "
                    "Corrija a variável no .env e reinicie o servidor."
                )
                return
            await self._connect(session_string)
            return

        legacy = migrate_legacy_session(
            os.path.join(PROJECT_ROOT, config.get("session_name", "default_session"))
        )
        if legacy:
            self.store.save(legacy, self.passphrase)
            await self._connect(legacy)
            return

        self.status = AUTH_UNAUTHENTICATED

    async def _connect(self, session_string: str) -> None:
        config = self._config()
        client = TelegramClient(
            StringSession(session_string), config["api_id"], config["api_hash"]
        )
        try:
            await client.connect()
        except Exception as exc:
            self.status = AUTH_UNAUTHENTICATED
            self.error = f"Falha ao conectar ao Telegram: {exc}"
            return

        if not await client.is_user_authorized():
            await client.disconnect()
            self.status = AUTH_UNAUTHENTICATED
            self.error = "A sessão armazenada não é mais válida. Faça login novamente."
            return

        self.client = client
        self.user_info = await self._fetch_user_info(client)
        self.status = AUTH_AUTHORIZED
        self.error = None

    async def _fetch_user_info(self, client: TelegramClient) -> dict[str, Any] | None:
        try:
            me = await client.get_me()
        except Exception:
            return None
        return {
            "id": me.id,
            "name": utils.get_display_name(me),
            "username": getattr(me, "username", None),
            "phone": getattr(me, "phone", None),
        }

    async def _reset_auth_client(self) -> None:
        if self._qr_task and not self._qr_task.done():
            self._qr_task.cancel()
            try:
                await self._qr_task
            except asyncio.CancelledError:
                pass
        self._qr_task = None
        self._qr = None
        if self._auth_client is not None:
            try:
                await self._auth_client.disconnect()
            except Exception:
                pass
        self._auth_client = None
        self._phone = None
        self._phone_code_hash = None
        self.qr_png = None
        self.qr_expires_at = None

    async def _new_auth_client(self) -> TelegramClient:
        config = self._config()
        client = TelegramClient(None, config["api_id"], config["api_hash"])
        await client.connect()
        return client

    async def request_code(self, phone: str) -> dict[str, Any]:
        """Send the login code to the given phone number."""
        await self._reset_auth_client()

        phone = (phone or "").strip()
        if not phone:
            raise AuthError("Informe o número de telefone com código do país.")

        client = await self._new_auth_client()
        try:
            sent = await client.send_code_request(phone)
        except FloodWaitError as exc:
            await client.disconnect()
            raise AuthError(f"Telegram pediu para aguardar {exc.seconds}s. Tente novamente.") from exc
        except Exception as exc:
            await client.disconnect()
            raise AuthError(f"Não foi possível enviar o código: {exc}") from exc

        self._auth_client = client
        self._phone = phone
        self._phone_code_hash = sent.phone_code_hash
        self.status = AUTH_CODE_SENT
        self.error = None
        return {"status": self.status, "phone": phone, "hint": sent.type}

    async def submit_code(self, code: str) -> dict[str, Any]:
        """Validate the login code received on Telegram."""
        if not self._auth_client:
            raise AuthError("Solicite o código novamente.")

        try:
            await self._auth_client.sign_in(
                phone=self._phone,
                code=code,
                phone_code_hash=self._phone_code_hash,
            )
        except SessionPasswordNeededError:
            self.status = AUTH_PASSWORD_REQUIRED
            return {"status": self.status}
        except PhoneCodeInvalidError:
            self.error = "Código inválido. Confira os dígitos e tente novamente."
            return {"status": AUTH_CODE_SENT, "error": self.error}
        except Exception as exc:
            self.error = str(exc)
            return {"status": self.status, "error": self.error}

        await self._finalize(self._auth_client)
        return {"status": AUTH_AUTHORIZED}

    async def submit_password(self, password: str) -> dict[str, Any]:
        """Complete the login with the 2FA password."""
        if not self._auth_client:
            raise AuthError("Inicie o login novamente.")

        try:
            await self._auth_client.sign_in(password=password)
        except PasswordHashInvalidError:
            self.error = "Senha de verificação incorreta."
            return {"status": AUTH_PASSWORD_REQUIRED, "error": self.error}
        except Exception as exc:
            self.error = str(exc)
            return {"status": self.status, "error": self.error}

        await self._finalize(self._auth_client)
        return {"status": AUTH_AUTHORIZED}

    async def start_qr(self) -> dict[str, Any]:
        """Start a QR login without replacing an authorized or pending 2FA client."""
        if self.status in (AUTH_AUTHORIZED, AUTH_PASSWORD_REQUIRED):
            return self.state()
        await self._reset_auth_client()

        client = await self._new_auth_client()
        try:
            qr = await client.qr_login()
        except Exception as exc:
            await client.disconnect()
            raise AuthError(f"Não foi possível gerar o QR code: {exc}") from exc

        self._auth_client = client
        self._qr = qr
        self.qr_png = render_qr(qr.url)
        self.qr_expires_at = qr.expires.timestamp()
        self.status = AUTH_QR_READY
        self.error = None
        self._qr_task = asyncio.create_task(self._await_qr(qr))
        return {
            "status": self.status,
            "qr_png": self.qr_png,
            "expires_at": self.qr_expires_at,
        }

    async def _await_qr(self, qr):
        try:
            await qr.wait()
        except SessionPasswordNeededError:
            self.status = AUTH_PASSWORD_REQUIRED
            return
        except (asyncio.TimeoutError, TimeoutError):
            self.status = AUTH_QR_EXPIRED
            self.qr_png = None
            return
        except Exception as exc:
            self.status = AUTH_UNAUTHENTICATED
            self.error = str(exc)
            return

        if self._auth_client is not None:
            try:
                await self._finalize(self._auth_client)
            except Exception:
                self.status = AUTH_UNAUTHENTICATED
                self.error = "Não foi possível persistir a sessão. Verifique o banco e tente novamente."

    async def qr_status(self) -> dict[str, Any]:
        """Report the QR login progress, regenerating the code when it expires."""
        if self.status not in (AUTH_QR_READY, AUTH_QR_EXPIRED):
            return {"status": self.status, "error": self.error}

        expired = self._qr is None or self._qr.expires.timestamp() <= time.time()
        if expired:
            return await self.start_qr()
        return {
            "status": AUTH_QR_READY,
            "qr_png": self.qr_png,
            "expires_at": self.qr_expires_at,
        }

    async def logout(self) -> dict[str, Any]:
        """Revoke the session on Telegram's side and erase it from the database."""
        if self.client is not None:
            try:
                await self.client.log_out()
            except Exception:
                pass
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

        await self._reset_auth_client()

        if self.store:
            self.store.delete()

        self.user_info = None
        self.status = AUTH_UNAUTHENTICATED
        self.error = None
        return {"status": self.status}

    async def _finalize(self, client: TelegramClient) -> None:
        """Persist the authorized session encrypted and adopt it as the main client."""
        if not self.passphrase:
            raise AuthError("TBMD_SESSION_KEY não está definida no .env.")

        user_info = await self._fetch_user_info(client)
        session_string = session_to_string(client.session)
        self.store.save(
            session_string,
            self.passphrase,
            phone=(user_info or {}).get("phone"),
            user_id=(user_info or {}).get("id"),
        )

        self.client = client
        self.user_info = user_info
        self.status = AUTH_AUTHORIZED
        self.error = None
        self._auth_client = None
        self._qr = None
        self.qr_png = None
        self.qr_expires_at = None
        self._phone = self._phone_code_hash = None
        if self._on_authorized:
            await self._on_authorized()

    def state(self) -> dict[str, Any]:
        """Serializable authentication state for the frontend."""
        return {
            "status": self.status,
            "error": self.error,
            "has_session": bool(self.store and self.store.has_session()),
            "qr_png": self.qr_png,
            "qr_expires_at": self.qr_expires_at,
        }
