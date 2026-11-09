import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import src.web as web
from src.queue_db import QueueDatabase
from src.web.auth import TelegramAuthService


class TestStartupRequiresSessionKey(unittest.IsolatedAsyncioTestCase):
    @patch("src.web.manager.load_settings", return_value=(None, "TBMD_SESSION_KEY não definido no .env."))
    async def test_manager_fails_startup_without_key(self, _load_settings):
        with self.assertRaisesRegex(RuntimeError, "TBMD_SESSION_KEY"):
            await web.manager.initialize()


class TestAuthServiceState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = QueueDatabase(os.path.join(self.tmp.name, "auth.db"))
        self.config = {
            "api_id": 12345,
            "api_hash": "dummy_hash",
            "session_key": None,
        }
        self.service = TelegramAuthService(lambda: self.config, lambda: self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_initial_state_is_unauthenticated(self):
        self.assertEqual(self.service.status, "unauthenticated")
        self.assertFalse(self.service.authorized)
        self.assertFalse(self.service.state()["has_session"])

    def test_store_has_session_reflects_database(self):
        from src.session_store import SessionStore

        SessionStore(self.db).save("1session", "senha")
        self.assertFalse(self.service.state()["has_session"])

    def test_state_payload_never_leaks_the_key(self):
        state = self.service.state()
        self.assertIn("status", state)
        self.assertNotIn("needs_passphrase", state)
        self.assertNotIn("ciphertext", state)
        self.assertNotIn("salt", state)


class TestAuthEndpoints(unittest.TestCase):
    def setUp(self):
        self.init_patcher = patch.object(web.manager, "initialize", new_callable=AsyncMock)
        self.shutdown_patcher = patch.object(web.manager, "shutdown", new_callable=AsyncMock)
        self.init_patcher.start()
        self.shutdown_patcher.start()

        self.tmp = tempfile.TemporaryDirectory()
        self.db = QueueDatabase(os.path.join(self.tmp.name, "web_auth.db"))
        web.manager.config = {
            "api_id": 12345,
            "api_hash": "dummy_hash",
            "download_dir": "downloads",
            "batch_size": 5,
            "message_limit": 2000,
            "db_path": self.db.db_path,
            "session_key": None,
            "web_token": None,
        }
        web.manager.auth = TelegramAuthService(lambda: web.manager.config, lambda: web.manager.db)
        web.manager.db = self.db
        self.client = TestClient(web.app)

    def tearDown(self):
        self.init_patcher.stop()
        self.shutdown_patcher.stop()
        self.tmp.cleanup()

    def test_auth_state_endpoint(self):
        resp = self.client.get("/api/auth/state")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "unauthenticated")
        self.assertNotIn("needs_passphrase", data)

    def test_phone_flow_sends_the_code(self):
        sent = MagicMock()
        sent.phone_code_hash = "hash123"
        sent.type = "app"

        client = MagicMock()
        client.connect = AsyncMock()
        client.send_code_request = AsyncMock(return_value=sent)
        client.disconnect = AsyncMock()
        web.manager.auth.passphrase = "chave-configurada-no-servidor"

        with patch("src.web.auth.TelegramClient", return_value=client):
            resp = self.client.post(
                "/api/auth/phone",
                json={"phone": "+5511999999999"},
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "code_sent")
        client.send_code_request.assert_awaited_once_with("+5511999999999")

    def test_code_submission_reports_invalid_code(self):
        from telethon.errors import PhoneCodeInvalidError

        client = MagicMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.sign_in = AsyncMock(side_effect=PhoneCodeInvalidError(request=None))

        web.manager.auth._auth_client = client
        web.manager.auth._phone = "+5511999999999"
        web.manager.auth._phone_code_hash = "hash123"
        web.manager.auth.status = "code_sent"

        resp = self.client.post("/api/auth/code", json={"code": "00000"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "code_sent")
        self.assertIn("inválido", resp.json()["error"])

    def test_code_submission_requests_the_two_factor_password(self):
        from telethon.errors import SessionPasswordNeededError

        client = MagicMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.sign_in = AsyncMock(side_effect=SessionPasswordNeededError(request=None))

        web.manager.auth._auth_client = client
        web.manager.auth._phone = "+5511999999999"
        web.manager.auth._phone_code_hash = "hash123"
        web.manager.auth.status = "code_sent"

        resp = self.client.post("/api/auth/code", json={"code": "12345"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "password_required")

    def test_logout_clears_the_stored_session(self):
        from src.session_store import SessionStore

        SessionStore(self.db).save("1session", "senha")
        web.manager.auth.store = SessionStore(self.db)

        resp = self.client.post("/api/auth/logout")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "unauthenticated")
        self.assertFalse(SessionStore(self.db).has_session())
        self.assertFalse(web.manager.authorized)


class TestAccessTokenGuard(unittest.TestCase):
    def setUp(self):
        self.init_patcher = patch.object(web.manager, "initialize", new_callable=AsyncMock)
        self.shutdown_patcher = patch.object(web.manager, "shutdown", new_callable=AsyncMock)
        self.init_patcher.start()
        self.shutdown_patcher.start()
        web.manager.config = {
            "api_id": 12345,
            "api_hash": "dummy_hash",
            "download_dir": "downloads",
            "batch_size": 5,
            "message_limit": 2000,
            "db_path": "tbmd.db",
            "session_key": None,
            "web_token": None,
        }
        self.client = TestClient(web.app)

    def tearDown(self):
        self.init_patcher.stop()
        self.shutdown_patcher.stop()

    def test_no_token_configured_leaves_the_api_open(self):
        web.manager.config = {**web.manager.config, "web_token": None}
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)

    def test_api_is_blocked_without_the_token(self):
        web.manager.config = {**web.manager.config, "web_token": "segredo"}
        try:
            resp = self.client.get("/api/status")
            self.assertEqual(resp.status_code, 401)

            resp = self.client.get("/api/status", headers={"X-Auth-Token": "errado"})
            self.assertEqual(resp.status_code, 401)

            resp = self.client.get("/api/status", headers={"X-Auth-Token": "segredo"})
            self.assertEqual(resp.status_code, 200)
        finally:
            web.manager.config = {**web.manager.config, "web_token": None}

    def test_index_is_always_served(self):
        web.manager.config = {**web.manager.config, "web_token": "segredo"}
        try:
            resp = self.client.get("/")
            self.assertEqual(resp.status_code, 200)
        finally:
            web.manager.config = {**web.manager.config, "web_token": None}

    def test_token_endpoint_sets_the_cookie(self):
        web.manager.config = {**web.manager.config, "web_token": "segredo"}
        try:
            resp = self.client.post("/api/auth/token", json={"token": "errado"})
            self.assertEqual(resp.status_code, 401)

            resp = self.client.post("/api/auth/token", json={"token": "segredo"})
            self.assertEqual(resp.status_code, 200)
            self.assertIn("tbmd_token", resp.cookies)
        finally:
            web.manager.config = {**web.manager.config, "web_token": None}


if __name__ == "__main__":
    unittest.main()

class TestCompletedLogin(unittest.IsolatedAsyncioTestCase):
    async def test_password_completion_adopts_client_and_persists_session(self):
        from src.web.manager import WebDownloadManager
        from src.session_store import SessionStore
        from telethon.sessions import MemorySession
        from telethon.crypto import AuthKey
        with tempfile.TemporaryDirectory() as tmp:
            manager = WebDownloadManager()
            manager.db = QueueDatabase(os.path.join(tmp, 'test.db'))
            manager.config = {'session_key': 'test-key'}
            manager._ensure_queue_processor = MagicMock()
            manager.broadcast_state = AsyncMock()
            auth = manager.auth
            auth.store = SessionStore(manager.db)
            auth.passphrase = 'test-key'
            client = MagicMock()
            client.session = MemorySession()
            client.session.set_dc(2, '149.154.167.51', 443)
            client.session.auth_key = AuthKey(bytes(range(256)))
            client.sign_in = AsyncMock()
            client.get_me = AsyncMock(return_value=MagicMock(id=42, first_name='Test', last_name='', username=None, phone=None))
            auth._auth_client = client
            auth.status = 'password_required'
            result = await auth.submit_password('transient')
            self.assertEqual(result['status'], 'authorized')
            self.assertTrue(manager.authorized)
            self.assertIs(manager.client, client)
            self.assertTrue(auth.store.has_session())
            manager.broadcast_state.assert_awaited_once()
            self.assertIsNone(auth._auth_client)
            self.assertNotIn('transient', str(auth.state()))
            # Late QR polling/start must never reset a completed login.
            await auth.start_qr()
            self.assertTrue(manager.authorized)

    async def test_qr_poll_does_not_replace_phone_flow(self):
        auth = TelegramAuthService(lambda: {}, lambda: None)
        auth.status = 'code_sent'
        auth.start_qr = AsyncMock()
        self.assertEqual((await auth.qr_status())['status'], 'code_sent')
        auth.start_qr.assert_not_awaited()

    async def test_qr_two_factor_keeps_pending_client(self):
        from telethon.errors import SessionPasswordNeededError
        auth = TelegramAuthService(lambda: {}, lambda: None)
        client = MagicMock()
        auth._auth_client = client
        qr = MagicMock()
        qr.wait = AsyncMock(side_effect=SessionPasswordNeededError(None))
        await auth._await_qr(qr)
        self.assertEqual(auth.status, 'password_required')
        await auth.start_qr()
        self.assertIs(auth._auth_client, client)

class TestCLIPersistence(unittest.IsolatedAsyncioTestCase):
    async def test_cli_saves_session_after_automatic_context_login(self):
        import importlib
        from types import SimpleNamespace
        cli = importlib.import_module('src.cli.main')
        args = SimpleNamespace(queue_stats=False, retry_failed=False, list=True, search=None)
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.is_user_authorized = AsyncMock(return_value=True)
        settings = MagicMock()
        settings.to_dict.return_value = {'session_key': 'test-key'}
        with patch.object(cli, 'build_parser') as parser, \
             patch.object(cli, 'load_settings', return_value=(settings, None)), \
             patch.object(cli, 'get_queue_db'), \
             patch.object(cli, 'open_client_from_store', return_value=client), \
             patch.object(cli, 'save_client_session') as save, \
             patch.object(cli, 'fetch_dialogs', new=AsyncMock(return_value=[])):
            parser.return_value.parse_args.return_value = args
            await cli.main()
            save.assert_called_once()
            self.assertIs(save.call_args.args[0], client)

class TestQRCompletion(unittest.IsolatedAsyncioTestCase):
    async def test_qr_without_two_factor_finalizes_in_background(self):
        service = TelegramAuthService(lambda: {}, lambda: None)
        client = MagicMock()
        service._auth_client = client
        service._finalize = AsyncMock()
        qr = MagicMock()
        qr.wait = AsyncMock()
        await service._await_qr(qr)
        service._finalize.assert_awaited_once_with(client)

class TestSQLiteOnlyRestore(unittest.IsolatedAsyncioTestCase):
    async def test_empty_database_requests_login_without_reading_session_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = QueueDatabase(os.path.join(tmp, 'auth.db'))
            service = TelegramAuthService(lambda: {'session_key': 'test-key'}, lambda: db)
            with patch('builtins.open', side_effect=AssertionError('Unexpected file read')):
                await service.restore()
            self.assertEqual(service.status, 'unauthenticated')
            self.assertIsNone(service.client)
            self.assertFalse(service.store.has_session())
