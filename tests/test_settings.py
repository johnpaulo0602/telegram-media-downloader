import os
import unittest
from unittest.mock import patch

from src.settings import Settings, load_settings
from src.cli.main import run_cli
from src.web.app import main as web_main


class TestSettings(unittest.TestCase):
    @patch.dict(os.environ, {"API_ID": "999888", "API_HASH": "my_hash", "TBMD_SESSION_KEY": "k"}, clear=True)
    def test_load_settings_defaults(self):
        settings, error = load_settings(load_env=False)
        self.assertIsNone(error)
        self.assertIsNotNone(settings)
        self.assertEqual(settings.api_id, 999888)
        self.assertEqual(settings.api_hash, "my_hash")
        self.assertEqual(settings.batch_size, 5)
        self.assertEqual(settings.download_dir, "downloads")
        self.assertEqual(settings.db_path, "tbmd.db")
        self.assertEqual(settings.web_host, "127.0.0.1")
        self.assertEqual(settings.web_port, 8000)
        self.assertEqual(settings.session_key, "k")
        self.assertIsNone(settings.web_token)

        # to_dict
        d = settings.to_dict()
        self.assertEqual(d["api_id"], 999888)
        self.assertEqual(d["web_port"], 8000)

    @patch.dict(os.environ, {"API_ID": "", "API_HASH": "hash"}, clear=True)
    def test_load_settings_missing_api_id(self):
        settings, error = load_settings(load_env=False)
        self.assertIsNone(settings)
        self.assertIn("API_ID não definido", error)

    @patch.dict(os.environ, {"API_ID": "123", "API_HASH": ""}, clear=True)
    def test_load_settings_missing_api_hash(self):
        settings, error = load_settings(load_env=False)
        self.assertIsNone(settings)
        self.assertIn("API_HASH não definido", error)

    @patch.dict(os.environ, {"API_ID": "123", "API_HASH": "h"}, clear=True)
    def test_load_settings_missing_session_key(self):
        settings, error = load_settings(load_env=False)
        self.assertIsNone(settings)
        self.assertIn("TBMD_SESSION_KEY", error)

    @patch.dict(
        os.environ,
        {
            "API_ID": "123",
            "API_HASH": "h",
            "BATCH_SIZE": "15",
            "MESSAGE_LIMIT": "500",
            "DB_PATH": "/tmp/custom.db",
            "WEB_HOST": "0.0.0.0",
            "WEB_PORT": "9090",
            "TBMD_SESSION_KEY": "segredo",
            "TBMD_WEB_TOKEN": "token-do-painel",
        },
        clear=True,
    )
    def test_load_settings_custom_values(self):
        settings, error = load_settings(load_env=False)
        self.assertIsNone(error)
        self.assertEqual(settings.batch_size, 15)
        self.assertEqual(settings.message_limit, 500)
        self.assertEqual(settings.db_path, "/tmp/custom.db")
        self.assertEqual(settings.web_host, "0.0.0.0")
        self.assertEqual(settings.web_port, 9090)
        self.assertEqual(settings.session_key, "segredo")
        self.assertEqual(settings.web_token, "token-do-painel")


class TestWebStartupSettings(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    @patch("src.cli.main.load_dotenv")
    @patch("uvicorn.run")
    @patch("sys.argv", ["tbmd", "--web"])
    def test_cli_web_uses_dotenv_host_and_port(self, uvicorn_run, load_env):
        def set_web_settings():
            os.environ["WEB_HOST"] = "127.0.0.1"
            os.environ["WEB_PORT"] = "9090"

        load_env.side_effect = set_web_settings
        run_cli()
        uvicorn_run.assert_called_once_with("src.web:app", host="127.0.0.1", port=9090, reload=False)

    @patch.dict(os.environ, {}, clear=True)
    @patch("dotenv.load_dotenv")
    @patch("uvicorn.run")
    def test_web_entrypoint_uses_standard_port(self, uvicorn_run, _load_env):
        web_main()
        uvicorn_run.assert_called_once_with("src.web.app:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    unittest.main()
