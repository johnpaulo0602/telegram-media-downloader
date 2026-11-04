import unittest
from src.models import (
    JobStatus,
    QueueItem,
    QueueStats,
    QueueStatus,
    StartDownloadRequest,
)


class TestModels(unittest.TestCase):
    def test_queue_status_enum(self):
        self.assertEqual(QueueStatus.PENDING.value, "pending")
        self.assertEqual(QueueStatus.COMPLETED.value, "completed")
        self.assertEqual(QueueStatus.FAILED.value, "failed")
        self.assertEqual(QueueStatus.DOWNLOADING.value, "downloading")

    def test_queue_item_creation_and_to_dict(self):
        item = QueueItem(
            chat_id="12345",
            message_id=99,
            file_name="video.mp4",
            dest_path="/d/video.mp4",
            file_size=1024,
            status=QueueStatus.PENDING,
        )
        self.assertEqual(item.chat_id, "12345")
        self.assertEqual(item.status, QueueStatus.PENDING)
        self.assertGreater(item.created_at, 0)

        d = item.to_dict()
        self.assertEqual(d["chat_id"], "12345")
        self.assertEqual(d["status"], "pending")
        self.assertEqual(d["file_size"], 1024)

    def test_queue_item_from_row(self):
        row = {
            "id": 1,
            "chat_id": "999",
            "chat_title": "Canal VIP",
            "message_id": 42,
            "file_name": "audio.mp3",
            "dest_path": "/d/audio.mp3",
            "file_size": 2048,
            "media_type": "audios",
            "status": "completed",
            "downloaded_bytes": 2048,
            "attempts": 1,
            "max_attempts": 3,
            "last_error": None,
            "created_at": 1000.0,
            "started_at": 1001.0,
            "completed_at": 1005.0,
        }
        item = QueueItem.from_row(row)
        self.assertEqual(item.id, 1)
        self.assertEqual(item.chat_id, "999")
        self.assertEqual(item.status, QueueStatus.COMPLETED)
        self.assertEqual(item.completed_at, 1005.0)

    def test_queue_stats(self):
        stats = QueueStats(pending=5, downloading=1, completed=10, failed=2, total=18)
        d = stats.to_dict()
        self.assertEqual(d["pending"], 5)
        self.assertEqual(d["completed"], 10)
        self.assertEqual(d["total"], 18)

    def test_pydantic_requests(self):
        req = StartDownloadRequest(chat="my_channel", media_type="videos", batch_size=4)
        self.assertEqual(req.chat, "my_channel")
        self.assertEqual(req.batch_size, 4)
        self.assertEqual(req.media_type, "videos")


if __name__ == "__main__":
    unittest.main()
