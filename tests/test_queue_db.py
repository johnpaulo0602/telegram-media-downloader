import os
import tempfile
import unittest

from src.queue_db import QueueDatabase


class TestQueueDatabase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_queue.db")
        self.db = QueueDatabase(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_schema_initialization(self):
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='queue_items'")
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["name"], "queue_items")

    def test_enqueue_single_item(self):
        item_id, is_new = self.db.enqueue_item(
            chat_id="123456",
            message_id=1,
            file_name="video.mp4",
            dest_path="/downloads/video.mp4",
            file_size=1024,
            chat_title="My Channel",
            media_type="videos",
        )
        self.assertTrue(is_new)
        self.assertGreater(item_id, 0)

        # Enqueue duplicate
        item_id2, is_new2 = self.db.enqueue_item(
            chat_id="123456",
            message_id=1,
            file_name="video.mp4",
            dest_path="/downloads/video.mp4",
        )
        self.assertFalse(is_new2)
        self.assertEqual(item_id, item_id2)

        item = self.db.get_item("123456", 1)
        self.assertIsNotNone(item)
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["file_size"], 1024)

    def test_enqueue_batch(self):
        items = [
            {
                "chat_id": "chan1",
                "message_id": 10,
                "file_name": "file1.mp4",
                "dest_path": "/d/file1.mp4",
                "file_size": 100,
            },
            {
                "chat_id": "chan1",
                "message_id": 20,
                "file_name": "file2.mp4",
                "dest_path": "/d/file2.mp4",
                "file_size": 200,
            },
            {
                "chat_id": "chan1",
                "message_id": 10,  # Duplicate
                "file_name": "file1.mp4",
                "dest_path": "/d/file1.mp4",
            },
        ]
        res = self.db.enqueue_batch(items)
        self.assertEqual(res["enqueued"], 2)
        self.assertEqual(res["existing"], 1)

        pending = self.db.get_pending_items("chan1")
        self.assertEqual(len(pending), 2)

    def test_status_transitions_and_progress(self):
        item_id, _ = self.db.enqueue_item("c1", 100, "doc.pdf", "/d/doc.pdf", 500)

        # Mark started
        self.db.mark_started(item_id)
        item = self.db.get_item_by_id(item_id)
        self.assertEqual(item["status"], "downloading")
        self.assertEqual(item["attempts"], 1)
        self.assertIsNotNone(item["started_at"])

        # Progress update
        self.db.update_progress(item_id, 250)
        item = self.db.get_item_by_id(item_id)
        self.assertEqual(item["downloaded_bytes"], 250)

        # Mark completed
        self.db.mark_completed(item_id, "/d/doc.pdf", 500)
        item = self.db.get_item_by_id(item_id)
        self.assertEqual(item["status"], "completed")
        self.assertEqual(item["downloaded_bytes"], 500)
        self.assertIsNotNone(item["completed_at"])

    def test_failure_and_retry_lifecycle(self):
        item_id, _ = self.db.enqueue_item("c1", 200, "audio.mp3", "/d/audio.mp3", max_attempts=2)

        # First attempt failed -> should auto-retry back to pending
        self.db.mark_started(item_id)
        retried = self.db.mark_failed(item_id, "Network timeout", auto_retry=True)
        self.assertTrue(retried)

        item = self.db.get_item_by_id(item_id)
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["attempts"], 1)
        self.assertEqual(item["last_error"], "Network timeout")

        # Second attempt failed -> exceeds max_attempts (2) -> permanently failed
        self.db.mark_started(item_id)
        retried_2 = self.db.mark_failed(item_id, "Telegram server error", auto_retry=True)
        self.assertFalse(retried_2)

        item = self.db.get_item_by_id(item_id)
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["attempts"], 2)

        # Manual retry_failed
        reset_count = self.db.retry_failed("c1")
        self.assertEqual(reset_count, 1)

        item = self.db.get_item_by_id(item_id)
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["attempts"], 0)
        self.assertIsNone(item["last_error"])

    def test_skipped_and_stats(self):
        self.db.enqueue_item("c1", 1, "a.mp4", "/a.mp4")
        self.db.enqueue_item("c1", 2, "b.mp4", "/b.mp4")
        self.db.enqueue_item("c1", 3, "c.mp4", "/c.mp4")

        self.db.mark_completed(1, "/a.mp4", 100)
        self.db.mark_skipped(2, "Exists on disk")

        stats = self.db.get_stats("c1")
        self.assertEqual(stats["completed"], 1)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["total"], 3)

    def test_is_downloaded_checks_file_and_db(self):
        real_file = os.path.join(self.temp_dir.name, "real.mp4")
        with open(real_file, "w") as f:
            f.write("content")

        self.db.enqueue_item("c1", 10, "real.mp4", real_file)
        self.db.mark_completed(10, real_file, os.path.getsize(real_file))

        # Completed in DB and exists on disk
        self.assertTrue(self.db.is_downloaded("c1", 10, real_file))

        # Not in DB completed, but file exists on disk
        self.assertTrue(self.db.is_downloaded("c1", 999, real_file))

        # Neither
        self.assertFalse(self.db.is_downloaded("c1", 999, "/non/existent.mp4"))
