import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import src.tbmd as tbmd
import src.web as web


class TestFilesystemBrowser(unittest.TestCase):
    def test_browse_directory_lists_subfolders_and_breadcrumbs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sub_a = os.path.join(tmpdir, "Videos")
            sub_b = os.path.join(tmpdir, "Fotos")
            hidden = os.path.join(tmpdir, ".hidden_folder")
            os.makedirs(sub_a)
            os.makedirs(sub_b)
            os.makedirs(hidden)

            # Without hidden folders
            res = web.browse_directory(tmpdir, default_dir=tmpdir, show_hidden=False)
            self.assertTrue(res["exists"])
            self.assertTrue(res["writable"])
            self.assertGreater(res["disk_total_bytes"], 0)
            self.assertGreater(res["disk_free_bytes"], 0)
            dir_names = [d["name"] for d in res["directories"]]
            self.assertEqual(dir_names, ["Fotos", "Videos"])
            self.assertTrue(len(res["breadcrumbs"]) >= 1)

            # With hidden folders
            res_hidden = web.browse_directory(tmpdir, default_dir=tmpdir, show_hidden=True)
            dir_names_hidden = [d["name"] for d in res_hidden["directories"]]
            self.assertIn(".hidden_folder", dir_names_hidden)

    def test_browse_nonexistent_target_falls_back_to_existing_parent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            non_existent = os.path.join(tmpdir, "future_downloads", "videos")
            res = web.browse_directory(non_existent, default_dir=tmpdir)
            self.assertFalse(res["exists"])
            self.assertEqual(res["current_path"], os.path.abspath(tmpdir))
            self.assertEqual(res["requested_path"], os.path.abspath(non_existent))


class TestDownloadSessionEvents(unittest.TestCase):
    def test_download_file_emits_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            orig_state_file = tbmd.STATE_FILE
            tbmd.STATE_FILE = state_file
            try:
                events = []

                def on_event(event_type, payload):
                    events.append((event_type, payload))

                session = tbmd.DownloadSession(
                    dest_dir=tmpdir,
                    chat_key="chan_1",
                    state={},
                    batch_size=2,
                    event_callback=on_event,
                )

                msg = MagicMock()
                msg.id = 101
                msg.text = "Aula 01"
                msg.file.ext = ".mp4"
                msg.file.name = "aula01.mp4"
                msg.file.size = 1024

                async def fake_download(file, progress_callback=None):
                    if progress_callback:
                        progress_callback(512, 1024)
                        progress_callback(1024, 1024)
                    with open(file, "wb") as f:
                        f.write(b"x" * 1024)

                msg.download_media = AsyncMock(side_effect=fake_download)

                asyncio.run(tbmd.download_file(msg, session))

                self.assertEqual(session.downloaded, 1)
                event_types = [e[0] for e in events]
                self.assertIn("file_start", event_types)
                self.assertIn("file_progress", event_types)
                self.assertIn("file_complete", event_types)
            finally:
                tbmd.STATE_FILE = orig_state_file


class TestWebEndpoints(unittest.TestCase):
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
        }
        self.client = TestClient(web.app)

    def tearDown(self):
        self.init_patcher.stop()
        self.shutdown_patcher.stop()

    def test_index_serves_html(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Telegram Media Downloader", resp.text)

    def test_status_endpoint(self):
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("telegram", data)
        self.assertIn("environment", data)
        self.assertIn("job", data)

    def test_fs_and_mkdir_endpoints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            resp = self.client.get("/api/fs", params={"path": tmpdir})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["current_path"], os.path.abspath(tmpdir))

            mkdir_resp = self.client.post(
                "/api/fs/mkdir",
                json={"parent_path": tmpdir, "folder_name": "Meus Videos:2026"},
            )
            self.assertEqual(mkdir_resp.status_code, 200)
            created_path = mkdir_resp.json()["current_path"]
            self.assertTrue(os.path.isdir(created_path))
            self.assertEqual(os.path.basename(created_path), "Meus Videos_2026")

    def test_resolve_destination_with_and_without_subfolder(self):
        p1 = web.manager.resolve_destination("/mnt/media", "videos", append_type_folder=True)
        self.assertEqual(p1, os.path.abspath("/mnt/media/videos"))

        # Does not duplicate `/videos/videos` if already ending with `/videos`
        p2 = web.manager.resolve_destination("/mnt/media/videos", "videos", append_type_folder=True)
        self.assertEqual(p2, os.path.abspath("/mnt/media/videos"))

        p3 = web.manager.resolve_destination("/mnt/media", "videos", append_type_folder=False)
        self.assertEqual(p3, os.path.abspath("/mnt/media"))

    def test_clear_logs_endpoint(self):
        web.manager._add_log("info", "test.mp4", "Teste")
        self.assertGreater(len(web.manager.recent_logs), 0)
        resp = self.client.post("/api/logs/clear")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["job"]["recent_logs"], [])


if __name__ == "__main__":
    unittest.main()
