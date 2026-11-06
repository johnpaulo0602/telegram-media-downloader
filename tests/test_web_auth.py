import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import src.web as web
from src.queue_db import QueueDatabase
from src.web.auth import TelegramAuthService


class TestAuthServiceState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = QueueDatabase(os.path.join(self.tmp.name, "auth.db"))
        self.config = {
            "api_id": 12345,
            "api_hash": "dummy_hash",
            "session_name": "test_session",
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
        self.assertIn("needs_passphrase", state)
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
            "session_name": "test_session",
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
        self.assertTrue(data["needs_passphrase"])

    def test_phone_flow_without_passphrase_is_rejected(self):
        resp = self.client.post("/api/auth/phone", json={"phone": "+5511999999999"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("criptografia", resp.json()["detail"])

    def test_phone_flow_sends_the_code(self):
        sent = MagicMock()
        sent.phone_code_hash = "hash123"
        sent.type = "app"

        client = MagicMock()
        client.connect = AsyncMock()
        client.send_code_request = AsyncMock(return_value=sent)
        client.disconnect = AsyncMock()

        with patch("src.web.auth.TelegramClient", return_value=client):
            resp = self.client.post(
                "/api/auth/phone",
                json={"phone": "+5511999999999", "passphrase": "senha-secreta"},
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
            "session_name": "test_session",
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
