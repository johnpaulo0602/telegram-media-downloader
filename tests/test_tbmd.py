import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import src.downloader as downloader
from src.cli.main import apply_overrides, build_parser
from src.downloader import (
    DownloadSession,
    console,
    download_file,
    download_media_list,
    is_already_downloaded,
    load_state,
    record_download,
    save_state,
)
from src.queue_db import QueueDatabase
from src.settings import load_settings
from src.utils import (
    dialog_kind,
    filter_media_messages,
    get_media_filename,
    parse_chat_input,
    safe_filename,
    search_dialogs,
)


class TestFilenameSanitization(unittest.TestCase):
    def test_safe_filename_removes_invalid_chars(self):
        filename = safe_filename('file:name/with*invalid?chars|test.mp4')
        self.assertNotIn(":", filename)
        self.assertNotIn("/", filename)
        self.assertNotIn("*", filename)
        self.assertNotIn("?", filename)
        self.assertNotIn("|", filename)
        self.assertTrue(filename.endswith(".mp4"))

    def test_safe_filename_handles_newlines_and_spaces(self):
        caption = "Title of Video\nDescription on second line\r\nEpisode 1"
        filename = safe_filename(caption, default_ext=".mp4")
        self.assertNotIn("\n", filename)
        self.assertNotIn("\r", filename)
        self.assertTrue(filename.endswith(".mp4"))

    def test_safe_filename_truncation(self):
        long_caption = "A" * 300
        filename = safe_filename(long_caption, default_ext=".mp4", max_length=100)
        self.assertLessEqual(len(filename), 104)
        self.assertTrue(filename.endswith(".mp4"))

    def test_safe_filename_preserves_extension(self):
        filename = safe_filename("document.pdf", default_ext=".pdf")
        self.assertEqual(filename, "document.pdf")

    def test_safe_filename_empty(self):
        self.assertEqual(safe_filename("", default_ext=".jpg"), "")


class TestChatInputParsing(unittest.TestCase):
    def test_numeric_channel_id(self):
        self.assertEqual(parse_chat_input("-1001234567890"), -1001234567890)
        self.assertEqual(parse_chat_input("1234567890"), 1234567890)

    def test_username_with_or_without_at(self):
        self.assertEqual(parse_chat_input("@my_channel"), "@my_channel")
        self.assertEqual(parse_chat_input("my_channel"), "my_channel")

    def test_telegram_url(self):
        self.assertEqual(parse_chat_input("https://t.me/my_channel"), "my_channel")
        self.assertEqual(parse_chat_input("t.me/my_channel"), "my_channel")
        self.assertEqual(parse_chat_input("  https://t.me/my_channel  "), "my_channel")

    def test_empty_input(self):
        self.assertIsNone(parse_chat_input(""))


class TestStateManagement(unittest.TestCase):
    def test_state_load_and_save(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            orig_file = downloader.STATE_FILE
            downloader.STATE_FILE = state_file
            try:
                state = load_state()
                self.assertEqual(state, {})

                record_download(state, "my_channel", 123, "/path/to/file.mp4")
                save_state(state)

                loaded = load_state()
                self.assertIn("my_channel", loaded)
                self.assertIn("123", loaded["my_channel"])
                self.assertEqual(loaded["my_channel"]["123"]["path"], "/path/to/file.mp4")
            finally:
                downloader.STATE_FILE = orig_file

    def test_is_already_downloaded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            existing_file = os.path.join(tmpdir, "test.mp4")
            with open(existing_file, "w") as f:
                f.write("dummy content")

            state = {
                "chan1": {
                    "10": {"path": existing_file, "size": 13}
                }
            }

            # In state and file exists
            self.assertTrue(is_already_downloaded(state, "chan1", 10, existing_file))

            # Not in state, but destination path exists and has content
            self.assertTrue(is_already_downloaded(state, "chan1", 99, existing_file))

            # Neither in state nor existing on disk
            non_existent = os.path.join(tmpdir, "none.mp4")
            self.assertFalse(is_already_downloaded(state, "chan1", 99, non_existent))


class TestMediaNaming(unittest.TestCase):
    def test_media_naming_with_caption(self):
        msg = MagicMock()
        msg.id = 42
        msg.text = "Awesome Video"
        msg.file.ext = ".mp4"
        msg.file.name = "default.mp4"
        filename, ext = get_media_filename(msg)
        self.assertEqual(filename, "42_Awesome Video.mp4")
        self.assertEqual(ext, ".mp4")

    def test_media_naming_without_caption_uses_file_name(self):
        msg = MagicMock()
        msg.id = 99
        msg.text = ""
        msg.file.ext = ".pdf"
        msg.file.name = "report_2026.pdf"
        filename, ext = get_media_filename(msg)
        self.assertEqual(filename, "99_report_2026.pdf")
        self.assertEqual(ext, ".pdf")


class TestConfigLoading(unittest.TestCase):
    @patch.dict(os.environ, {"API_ID": "", "API_HASH": ""}, clear=True)
    def test_missing_credentials(self):
        settings, error = load_settings(load_env=False)
        self.assertIsNone(settings)
        self.assertIn("API_ID não definido", error)

    @patch.dict(os.environ, {"API_ID": "not_a_number", "API_HASH": "abc"}, clear=True)
    def test_invalid_api_id(self):
        settings, error = load_settings(load_env=False)
        self.assertIsNone(settings)
        self.assertIn("número inteiro válido", error)

    @patch.dict(os.environ, {"API_ID": "123456", "API_HASH": "secret_hash", "BATCH_SIZE": "10", "TBMD_SESSION_KEY": "k"}, clear=True)
    def test_valid_config(self):
        settings, error = load_settings(load_env=False)
        self.assertIsNone(error)
        config = settings.to_dict()
        self.assertEqual(config["api_id"], 123456)
        self.assertEqual(config["api_hash"], "secret_hash")
        self.assertEqual(config["batch_size"], 10)
        self.assertEqual(config["session_name"], "default_session")
        self.assertEqual(config["download_dir"], "downloads")

    @patch.dict(os.environ, {"API_ID": "1", "API_HASH": "h", "DOWNLOAD_DIR": "/tmp/tg", "TBMD_SESSION_KEY": "k"}, clear=True)
    def test_custom_download_dir(self):
        settings, _ = load_settings(load_env=False)
        self.assertEqual(settings.download_dir, "/tmp/tg")


def make_dialog(name, username=None, is_channel=False, is_group=False, bot=False, dialog_id=-100111222):
    entity = SimpleNamespace(username=username, bot=bot, id=abs(dialog_id))
    return SimpleNamespace(name=name, entity=entity, is_channel=is_channel, is_group=is_group, id=dialog_id)


class TestDialogs(unittest.TestCase):
    def setUp(self):
        self.dialogs = [
            make_dialog("Curso de Inglês", username="ingles_wave", is_channel=True, dialog_id=-100987654),
            make_dialog("Família", is_group=True, dialog_id=-555444333),
            make_dialog("Música Clássica", is_channel=True, is_group=True, dialog_id=-100222333),
            make_dialog("Helper", username="helper_bot", bot=True, dialog_id=777888),
        ]

    def test_search_ignores_case_and_accents(self):
        names = [d.name for d in search_dialogs(self.dialogs, "musica")]
        self.assertEqual(names, ["Música Clássica"])
        names = [d.name for d in search_dialogs(self.dialogs, "INGLES")]
        self.assertEqual(names, ["Curso de Inglês"])

    def test_search_matches_username(self):
        names = [d.name for d in search_dialogs(self.dialogs, "wave")]
        self.assertEqual(names, ["Curso de Inglês"])

    def test_search_matches_at_username_url_and_id(self):
        self.assertEqual([d.name for d in search_dialogs(self.dialogs, "@ingles_wave")], ["Curso de Inglês"])
        self.assertEqual([d.name for d in search_dialogs(self.dialogs, "https://t.me/ingles_wave")], ["Curso de Inglês"])
        self.assertEqual([d.name for d in search_dialogs(self.dialogs, "-100987654")], ["Curso de Inglês"])
        self.assertEqual([d.name for d in search_dialogs(self.dialogs, "555444333")], ["Família"])

    def test_search_empty_term(self):
        self.assertEqual(search_dialogs(self.dialogs, "   "), [])

    def test_dialog_kind(self):
        kinds = [dialog_kind(d) for d in self.dialogs]
        self.assertEqual(kinds, ["Canal", "Grupo", "Grupo", "Bot"])


def make_message(mime_type=None, name=None, media=True):
    document = SimpleNamespace(mime_type=mime_type) if mime_type else None
    file = SimpleNamespace(name=name) if media else None
    return SimpleNamespace(document=document, file=file, media=media)


class TestMediaFiltering(unittest.TestCase):
    def test_pdfs_by_mime_or_extension(self):
        pdf = make_message("application/pdf", "a.pdf")
        renamed_pdf = make_message("application/octet-stream", "B.PDF")
        zip_file = make_message("application/zip", "c.zip")
        result = filter_media_messages([pdf, renamed_pdf, zip_file], "pdfs")
        self.assertEqual(result, [pdf, renamed_pdf])

    def test_archives(self):
        pdf = make_message("application/pdf", "a.pdf")
        zip_file = make_message("application/zip", "c.zip")
        rar_file = make_message("application/octet-stream", "d.rar")
        result = filter_media_messages([pdf, zip_file, rar_file], "archives")
        self.assertEqual(result, [zip_file, rar_file])

    def test_audios(self):
        mp3_doc = make_message("audio/mpeg", "song.mp3")
        flac_doc = make_message("application/octet-stream", "track.FLAC")
        pdf = make_message("application/pdf", "a.pdf")
        result = filter_media_messages([mp3_doc, flac_doc, pdf], "audios")
        self.assertEqual(result, [mp3_doc, flac_doc])

    def test_all_skips_messages_without_file(self):
        with_file = make_message("video/mp4", "v.mp4")
        text_only = make_message(media=False)
        result = filter_media_messages([with_file, text_only], "all")
        self.assertEqual(result, [with_file])

    def test_multiple_media_types_filter(self):
        video_msg = make_message("video/mp4", "aula.mp4")
        pdf_msg = make_message("application/pdf", "apostila.pdf")
        zip_msg = make_message("application/zip", "exercicios.zip")
        result = filter_media_messages([video_msg, pdf_msg, zip_msg], ["videos", "pdfs"])
        self.assertEqual(result, [video_msg, pdf_msg])


class TestCliArgs(unittest.TestCase):
    def test_overrides(self):
        config = {"message_limit": 2000, "batch_size": 5}
        args = build_parser().parse_args(["--limit", "0", "-b", "0", "-t", "videos", "--db", "custom.db"])
        apply_overrides(config, args)
        self.assertIsNone(config["message_limit"])
        self.assertEqual(config["batch_size"], 1)
        self.assertEqual(config["db_path"], "custom.db")
        self.assertEqual(args.type, "videos")
        self.assertTrue(hasattr(args, "queue_stats"))
        self.assertTrue(hasattr(args, "retry_failed"))


class TestSQLiteIntegration(unittest.TestCase):
    def test_download_session_integrates_with_sqlite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            orig_state = downloader.STATE_FILE
            downloader.STATE_FILE = os.path.join(tmpdir, "state.json")
            try:
                db_path = os.path.join(tmpdir, "test.db")
                db = QueueDatabase(db_path)
                session = DownloadSession(
                    dest_dir=tmpdir,
                    chat_key="test_chat",
                    state={},
                    batch_size=2,
                    db=db,
                )

                msg = MagicMock()
                msg.id = 555
                msg.text = "Video Aula"
                msg.file.ext = ".mp4"
                msg.file.name = "aula.mp4"
                msg.file.size = 2048

                async def fake_download(file, progress_callback=None):
                    with open(file, "wb") as f:
                        f.write(b"y" * 2048)

                msg.download_media = AsyncMock(side_effect=fake_download)

                asyncio.run(download_file(msg, session))

                self.assertEqual(session.downloaded, 1)
                item = db.get_item("test_chat", 555)
                self.assertIsNotNone(item)
                self.assertEqual(item["status"], "completed")
                self.assertEqual(item["file_size"], 2048)
                self.assertTrue(os.path.exists(item["dest_path"]))

                # Running again should skip it
                session2 = DownloadSession(
                    dest_dir=tmpdir,
                    chat_key="test_chat",
                    state={},
                    batch_size=2,
                    db=db,
                )
                asyncio.run(download_file(msg, session2))
                self.assertEqual(session2.skipped, 1)
            finally:
                downloader.STATE_FILE = orig_state

    def test_download_media_list_pre_enqueues_in_sqlite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            orig_state = downloader.STATE_FILE
            downloader.STATE_FILE = os.path.join(tmpdir, "state.json")
            try:
                db_path = os.path.join(tmpdir, "test.db")
                db = QueueDatabase(db_path)
                session = DownloadSession(
                    dest_dir=tmpdir,
                    chat_key="chan_batch",
                    state={},
                    batch_size=2,
                    db=db,
                )

                msg1 = MagicMock()
                msg1.id = 1
                msg1.text = "File 1"
                msg1.file.ext = ".mp4"
                msg1.file.name = "f1.mp4"
                msg1.file.size = 100

                msg2 = MagicMock()
                msg2.id = 2
                msg2.text = "File 2"
                msg2.file.ext = ".mp4"
                msg2.file.name = "f2.mp4"
                msg2.file.size = 200

                async def fake_dl(file, progress_callback=None):
                    with open(file, "wb") as f:
                        f.write(b"z" * 100)

                msg1.download_media = AsyncMock(side_effect=fake_dl)
                msg2.download_media = AsyncMock(side_effect=fake_dl)

                with patch.object(console, "print"):
                    asyncio.run(download_media_list([msg1, msg2], session))

                stats = db.get_stats("chan_batch")
                self.assertEqual(stats["completed"], 2)
                self.assertEqual(stats["total"], 2)
            finally:
                downloader.STATE_FILE = orig_state


if __name__ == "__main__":
    unittest.main()

class TestChannelFolders(unittest.TestCase):
    def test_portable_folder_names(self):
        from src.utils import channel_folder_name
        cases = [
            ('Curso de Python — Avançado 🚀', 'curso_de_python_avancado'),
            ('../../Aulas / Vídeos\\PDFs', 'aulas_videos_pdfs'),
            ('CON', 'canal_con'),
            ('🔥', 'canal_123'),
            ('a' * 200, 'a' * 100),
        ]
        for title, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(channel_folder_name(title, '123'), expected)

    def test_only_download_volume_is_recommended(self):
        from src.utils import get_filesystem_shortcuts
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, 'future_downloads')
            shortcuts = get_filesystem_shortcuts(target)
            self.assertEqual([item['path'] for item in shortcuts], [target])
