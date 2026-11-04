import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import src.downloader as downloader
import src.web as web
from src.downloader import DownloadSession, download_file, set_queue_db
from src.queue_db import QueueDatabase


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
            orig_state_file = downloader.STATE_FILE
            downloader.STATE_FILE = state_file
            try:
                events = []

                def on_event(event_type, payload):
                    events.append((event_type, payload))

                session = DownloadSession(
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

                asyncio.run(download_file(msg, session))

                self.assertEqual(session.downloaded, 1)
                event_types = [e[0] for e in events]
                self.assertIn("file_start", event_types)
                self.assertIn("file_progress", event_types)
                self.assertIn("file_complete", event_types)
            finally:
                downloader.STATE_FILE = orig_state_file


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

    def test_queue_operations_and_multi_media_types(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            orig_jobs_file = web.JOBS_FILE
            web.JOBS_FILE = os.path.join(tmpdir, "jobs_history.json")
            try:
                web.manager.jobs = []
                web.manager.client = MagicMock()
                web.manager.authorized = True
                # Pause queue so jobs stay in `queued` state for inspection and reordering
                pause_resp = self.client.post("/api/queue/pause", json={"paused": True})
                self.assertEqual(pause_resp.status_code, 200)
                self.assertTrue(pause_resp.json()["queue"]["paused"])

                r1 = self.client.post(
                    "/api/downloads/start",
                    json={
                        "chat": "@canal_um",
                        "chat_title": "Canal Um",
                        "media_types": ["videos", "pdfs"],
                        "dest_dir": tmpdir,
                        "append_type_folder": False,
                    },
                )
                self.assertEqual(r1.status_code, 200)

                r2 = self.client.post(
                    "/api/downloads/start",
                    json={
                        "chat": "@canal_dois",
                        "chat_title": "Canal Dois",
                        "media_types": ["images"],
                        "dest_dir": tmpdir,
                        "append_type_folder": True,
                    },
                )
                self.assertEqual(r2.status_code, 200)
                snap = r2.json()
                self.assertEqual(snap["queue"]["queued_count"], 2)
                job1_id = snap["jobs"][0]["id"]
                job2_id = snap["jobs"][1]["id"]
                self.assertEqual(snap["jobs"][0]["media_types"], ["videos", "pdfs"])
                self.assertEqual(snap["jobs"][0]["queue_position"], 1)
                self.assertEqual(snap["jobs"][1]["queue_position"], 2)

                # Move second job up to position #1
                move_resp = self.client.post(f"/api/jobs/{job2_id}/move-up")
                self.assertEqual(move_resp.status_code, 200)
                moved_jobs = move_resp.json()["jobs"]
                self.assertEqual(moved_jobs[0]["id"], job2_id)
                self.assertEqual(moved_jobs[0]["queue_position"], 1)

                # Cancel job1 (marks it as cancelled) and then clear completed/cancelled jobs
                cancel_resp = self.client.post(f"/api/jobs/{job1_id}/cancel")
                self.assertEqual(cancel_resp.status_code, 200)

                clear_resp = self.client.post("/api/jobs/clear-completed")
                self.assertEqual(clear_resp.status_code, 200)
                remaining_ids = [j["id"] for j in clear_resp.json()["jobs"]]
                self.assertEqual(remaining_ids, [job2_id])
            finally:
                web.manager.queue_paused = False
                web.manager.jobs = []
                web.JOBS_FILE = orig_jobs_file

    def test_sqlite_queue_endpoints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = os.path.join(tmpdir, "web_test.db")
            db = QueueDatabase(test_db)
            orig_db = downloader._queue_db
            set_queue_db(db)
            try:
                # Enqueue some items
                db.enqueue_item("test_web_chat", 101, "v1.mp4", "/tmp/v1.mp4", 1000)
                item2_id, _ = db.enqueue_item("test_web_chat", 102, "v2.mp4", "/tmp/v2.mp4", 2000)
                db.mark_failed(item2_id, "Connection error", auto_retry=False)

                # 1. Test /api/queue/stats
                resp_stats = self.client.get("/api/queue/stats?chat_id=test_web_chat")
                self.assertEqual(resp_stats.status_code, 200)
                data = resp_stats.json()["stats"]
                self.assertEqual(data["pending"], 1)
                self.assertEqual(data["failed"], 1)
                self.assertEqual(data["total"], 2)

                # 2. Test /api/queue/items
                resp_items = self.client.get("/api/queue/items?chat_id=test_web_chat")
                self.assertEqual(resp_items.status_code, 200)
                items = resp_items.json()["items"]
                self.assertEqual(len(items), 2)

                # 3. Test /api/queue/retry-failed
                resp_retry = self.client.post("/api/queue/retry-failed?chat_id=test_web_chat")
                self.assertEqual(resp_retry.status_code, 200)
                self.assertEqual(resp_retry.json()["retried_count"], 1)
                new_stats = resp_retry.json()["stats"]
                self.assertEqual(new_stats["failed"], 0)
                self.assertEqual(new_stats["pending"], 2)
            finally:
                set_queue_db(orig_db)


if __name__ == "__main__":
    unittest.main()
