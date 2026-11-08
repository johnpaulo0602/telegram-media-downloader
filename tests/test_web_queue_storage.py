import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.queue_db import QueueDatabase
from src.web.manager import WebDownloadManager


class TestWebQueueStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 'queue.db')
        self.db = QueueDatabase(self.db_path)
        self.legacy = Path(self.tmp.name, 'jobs_history.json')

    def tearDown(self):
        self.tmp.cleanup()

    def manager(self):
        manager = WebDownloadManager()
        manager.db = QueueDatabase(self.db_path)
        return manager

    def write_legacy(self, jobs, counter=1010, paused=True):
        self.legacy.write_text(json.dumps({'jobs': jobs, 'job_counter': counter, 'queue_paused': paused}))

    def test_legacy_import_restores_order_metadata_and_all_fields_without_json(self):
        jobs = [{'id': 'TK-1002', 'status': 'completed', 'dest_dir': '/downloads/a',
                 'downloaded': 3, 'total_size_bytes': 100, 'recent_logs': [{'message': 'concluído'}]},
                {'id': 'TK-1001', 'status': 'queued', 'media_types': ['videos', 'pdfs']}]
        self.write_legacy(jobs)
        with patch('src.web.manager.PROJECT_ROOT', self.tmp.name):
            first = self.manager()
            first.load_queue_state()
            self.legacy.unlink()
            restored = self.manager()
            restored.load_queue_state()
        self.assertEqual(restored.jobs, first.jobs)
        self.assertEqual([j['id'] for j in restored.jobs], ['TK-1002', 'TK-1001'])
        self.assertEqual(restored.jobs[0]['downloaded'], 3)
        self.assertTrue(restored.queue_paused)
        self.assertEqual(restored._next_job_id(), 'TK-1011')
        self.assertFalse(self.legacy.exists())

    def test_legacy_list_and_counter_repair(self):
        self.legacy.write_text(json.dumps([{'id': 'TK-1123', 'status': 'completed'}]))
        self.assertTrue(self.db.import_web_queue_from_json(str(self.legacy)))
        state = self.db.load_web_queue()
        self.assertEqual(state['job_counter'], 1123)
        self.assertFalse(state['queue_paused'])

    def test_empty_sqlite_queue_is_authoritative_and_never_reimports_deleted_jobs(self):
        self.write_legacy([{'id': 'TK-1001', 'status': 'completed'}])
        self.db.import_web_queue_from_json(str(self.legacy))
        self.db.save_web_queue([], 1001, False)
        self.assertFalse(self.db.import_web_queue_from_json(str(self.legacy)))
        state = self.db.load_web_queue()
        self.assertEqual(state['jobs'], [])
        self.assertFalse(state['queue_paused'])
        # A lower caller-supplied counter cannot reuse old IDs after deletion.
        self.db.save_web_queue([], 1000, False)
        self.assertEqual(self.db.load_web_queue()['job_counter'], 1010)

    def test_malformed_import_aborts_without_losing_source_or_marking_migration(self):
        for content in ('{', '{}', '{"jobs": [null]}',
                        '{"jobs": [{"id": "duplicate"}, {"id": "duplicate"}]}',
                        '{"jobs": [{"id": "TK-1001", "status": "unknown"}]}'):
            with self.subTest(content=content):
                self.legacy.write_text(content)
                with self.assertRaises(ValueError):
                    self.db.import_web_queue_from_json(str(self.legacy))
                self.assertIsNone(self.db.load_web_queue())
                self.assertEqual(self.legacy.read_text(), content)
        self.write_legacy([{'id': 'TK-1001', 'status': 'completed'}])
        self.assertTrue(self.db.import_web_queue_from_json(str(self.legacy)))

    def test_failed_save_rolls_back_jobs_order_counter_and_pause(self):
        self.db.save_web_queue([{'id': 'TK-1001', 'status': 'completed'}], 1001, True)
        expected = self.db.load_web_queue()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.save_web_queue([{'id': 'TK-1002', 'status': 'queued'},
                                    {'id': 'TK-1002', 'status': 'queued'}], 2000, False)
        self.assertEqual(self.db.load_web_queue(), expected)

    def test_no_limit_discards_queued_jobs_when_history_exceeds_100(self):
        jobs = [{'id': f'TK-{1001 + i}', 'status': 'queued'} for i in range(120)]
        self.db.save_web_queue(jobs, 1120, True)
        self.assertEqual(len(self.db.load_web_queue()['jobs']), 120)

    def test_restart_recovers_interrupted_jobs_once_and_preserves_pending_jobs(self):
        jobs = [{'id': f'TK-{1001 + i}', 'status': status, 'active_workers': [{'id': 1}],
                 'total_speed_bps': 100, 'downloaded': 5}
                for i, status in enumerate(['scanning', 'downloading', 'cancelling', 'queued', 'completed'])]
        self.db.save_web_queue(jobs, 1005, True)
        with patch('src.web.manager.PROJECT_ROOT', self.tmp.name):
            restored = self.manager()
            restored.load_queue_state()
        self.assertEqual([j['status'] for j in restored.jobs],
                         ['cancelled', 'cancelled', 'cancelled', 'queued', 'completed'])
        state = self.db.load_web_queue()
        self.assertEqual(state['jobs'], restored.jobs)
        self.assertEqual(state['jobs'][1]['downloaded'], 5)
        self.assertEqual(state['jobs'][1]['active_workers'], [])
        self.assertEqual(state['jobs'][1]['total_speed_bps'], 0)
        self.assertIn('reinicialização', state['jobs'][1]['error'])

    def test_schema_upgrade_does_not_change_media_queue_or_encrypted_session(self):
        self.db.enqueue_item('chat', 1, 'a.mp4', '/downloads/a.mp4')
        from src.session_store import SessionStore
        SessionStore(self.db).save('opaque-test-session', 'test-key')
        with self.db.get_connection() as conn:
            conn.execute('DROP TABLE web_jobs')
            conn.execute('DROP TABLE web_queue_state')
        upgraded = QueueDatabase(self.db_path)
        upgraded.save_web_queue([], 1000, False)
        self.assertEqual(upgraded.get_stats()['total'], 1)
        self.assertEqual(SessionStore(upgraded).load('test-key'), 'opaque-test-session')


class TestWebQueueLifecycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = WebDownloadManager()
        self.manager.db = QueueDatabase(os.path.join(self.tmp.name, 'queue.db'))
        self.manager.config = {'db_path': self.manager.db.db_path, 'download_dir': self.tmp.name}
        self.manager.client = MagicMock()
        self.manager.client.disconnect = AsyncMock()
        self.manager.authorized = True
        self.manager._ensure_queue_processor = MagicMock()
        self.root_patch = patch('src.web.manager.PROJECT_ROOT', self.tmp.name)
        self.root_patch.start()
        self.manager.load_queue_state()

    async def asyncTearDown(self):
        self.root_patch.stop()
        self.tmp.cleanup()

    def restored(self):
        restored = WebDownloadManager()
        restored.db = QueueDatabase(self.manager.db.db_path)
        restored.load_queue_state()
        return restored

    async def test_queue_mutations_survive_new_manager_and_json_is_never_created(self):
        from src.models import StartDownloadRequest
        from telethon.tl.types import Channel
        channel = Channel(id=42, title='Canal', photo=None, date=None)
        await self.manager.toggle_queue_pause(True)
        with patch('src.web.manager.resolve_channel', new=AsyncMock(return_value=channel)):
            for name in ('@a', '@b', '@c'):
                await self.manager.start_download_job(StartDownloadRequest(chat=name))
        a, b, c = [job['id'] for job in self.manager.jobs]
        await self.manager.move_job_up(c)
        restored = self.restored()
        self.assertEqual([j['id'] for j in restored.jobs], [a, c, b])
        self.assertTrue(restored.queue_paused)
        self.assertEqual(restored.job_counter, 1003)
        await self.manager.cancel_job(a)
        self.assertEqual(self.restored().jobs[0]['status'], 'cancelled')
        await self.manager.retry_job(a)
        self.assertEqual(self.restored().jobs[0]['status'], 'queued')
        await self.manager.delete_job(b)
        self.assertEqual([j['id'] for j in self.restored().jobs], [a, c])
        await self.manager.cancel_job(a)
        await self.manager.clear_completed_jobs()
        self.assertEqual([j['id'] for j in self.restored().jobs], [c])
        await self.manager.toggle_queue_pause(False)
        self.assertFalse(self.restored().queue_paused)
        self.assertFalse(Path(self.tmp.name, 'jobs_history.json').exists())

    async def test_completion_and_retry_of_current_job_persist_correct_status(self):
        from src.models import StartDownloadRequest
        from telethon.tl.types import Channel
        channel = Channel(id=42, title='Canal', photo=None, date=None)
        with patch('src.web.manager.resolve_channel', new=AsyncMock(return_value=channel)):
            await self.manager.start_download_job(StartDownloadRequest(chat='@a'))
            self.manager.client.get_messages = AsyncMock(return_value=[])
            await self.manager._execute_job(self.manager.jobs[0])
        self.assertEqual(self.restored().jobs[0]['status'], 'completed')
        await self.manager.retry_job(self.manager.jobs[0]['id'])
        self.assertEqual(self.restored().jobs[0]['status'], 'queued')

    async def test_live_progress_checkpoint_without_websocket_and_shutdown(self):
        self.manager.jobs = [{'id': 'TK-1001', 'status': 'queued', 'recent_logs': []}]
        self.manager.current_job_id = 'TK-1001'
        self.manager.job_status = 'downloading'
        self.manager.total_files = 10
        self.manager.active_Session = MagicMock(downloaded=3, skipped=1, failed=[], cancelled=False)
        self.manager._last_queue_save = 0
        await self.manager.broadcast_state()
        state = self.manager.db.load_web_queue()
        self.assertEqual(state['jobs'][0]['status'], 'downloading')
        self.assertEqual(state['jobs'][0]['processed'], 4)
        await self.manager.shutdown()
        self.manager.client.disconnect.assert_awaited_once()
        self.assertTrue(self.manager.active_Session.cancelled)
        self.assertEqual(self.manager.db.load_web_queue()['jobs'][0]['processed'], 4)

    async def test_reconnect_keeps_running_jobs_and_does_not_reload_as_interrupted(self):
        from src.settings import Settings
        self.manager.jobs = [{'id': 'TK-1001', 'status': 'downloading'}]
        self.manager.persist_queue_state()
        settings = Settings(api_id=123, api_hash='dummy', session_key='test-key', db_path=self.manager.db.db_path)
        with patch('src.web.manager.load_settings', return_value=(settings, None)), \
             patch('src.web.manager.get_queue_db', return_value=self.manager.db), \
             patch.object(self.manager.auth, 'restore', new=AsyncMock()):
            await self.manager.initialize()
        self.assertEqual(self.manager.jobs[0]['status'], 'downloading')


    async def test_shutdown_waits_for_worker_finalization_before_last_save(self):
        import asyncio
        self.manager.jobs = [{'id': 'TK-1001', 'status': 'downloading'}]
        started = asyncio.Event()

        async def worker():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                self.manager.jobs[0].update(status='cancelled', downloaded=7)

        task = asyncio.create_task(worker())
        self.manager.active_task = self.manager.queue_runner_task = task
        await started.wait()
        await self.manager.shutdown()
        self.assertTrue(task.done())
        state = self.manager.db.load_web_queue()
        self.assertEqual(state['jobs'][0]['status'], 'cancelled')
        self.assertEqual(state['jobs'][0]['downloaded'], 7)

    async def test_startup_restores_queue_before_enabling_processor(self):
        from src.settings import Settings
        self.manager.db.save_web_queue([{'id': 'TK-1010', 'status': 'queued'}], 1010, True)
        fresh = WebDownloadManager()
        settings = Settings(api_id=123, api_hash='dummy', session_key='test-key', db_path=self.manager.db.db_path)
        fresh._ensure_queue_processor = MagicMock()
        fresh.auth.client = MagicMock()
        fresh.auth.status = 'authorized'
        with patch('src.web.manager.load_settings', return_value=(settings, None)), \
             patch('src.web.manager.get_queue_db', return_value=self.manager.db), \
             patch.object(fresh.auth, 'restore', new=AsyncMock()):
            await fresh.initialize()
        self.assertEqual(fresh.jobs[0]['id'], 'TK-1010')
        self.assertEqual(fresh.job_counter, 1010)
        self.assertTrue(fresh.queue_paused)
        fresh._ensure_queue_processor.assert_called_once()

    async def test_paused_queue_does_not_execute_restored_jobs(self):
        self.manager.jobs = [{'id': 'TK-1001', 'status': 'queued'}]
        await self.manager.toggle_queue_pause(True)
        fresh = self.restored()
        fresh.client = MagicMock()
        fresh.authorized = True
        fresh._execute_job = AsyncMock()
        await fresh._process_queue()
        fresh._execute_job.assert_not_awaited()
