import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import src.tbmd as tbmd


class TestFilenameSanitization(unittest.TestCase):
    def test_safe_filename_removes_invalid_chars(self):
        filename = tbmd.safe_filename('file:name/with*invalid?chars|test.mp4')
        self.assertNotIn(":", filename)
        self.assertNotIn("/", filename)
        self.assertNotIn("*", filename)
        self.assertNotIn("?", filename)
        self.assertNotIn("|", filename)
        self.assertTrue(filename.endswith(".mp4"))

    def test_safe_filename_handles_newlines_and_spaces(self):
        caption = "Title of Video\nDescription on second line\r\nEpisode 1"
        filename = tbmd.safe_filename(caption, default_ext=".mp4")
        self.assertNotIn("\n", filename)
        self.assertNotIn("\r", filename)
        self.assertTrue(filename.endswith(".mp4"))

    def test_safe_filename_truncation(self):
        long_caption = "A" * 300
        filename = tbmd.safe_filename(long_caption, default_ext=".mp4", max_length=100)
        self.assertLessEqual(len(filename), 104)
        self.assertTrue(filename.endswith(".mp4"))

    def test_safe_filename_preserves_extension(self):
        filename = tbmd.safe_filename("document.pdf", default_ext=".pdf")
        self.assertEqual(filename, "document.pdf")

    def test_safe_filename_empty(self):
        self.assertEqual(tbmd.safe_filename("", default_ext=".jpg"), "")


class TestChatInputParsing(unittest.TestCase):
    def test_numeric_channel_id(self):
        self.assertEqual(tbmd.parse_chat_input("-1001234567890"), -1001234567890)
        self.assertEqual(tbmd.parse_chat_input("1234567890"), 1234567890)

    def test_username_with_or_without_at(self):
        self.assertEqual(tbmd.parse_chat_input("@my_channel"), "@my_channel")
        self.assertEqual(tbmd.parse_chat_input("my_channel"), "my_channel")

    def test_telegram_url(self):
        self.assertEqual(tbmd.parse_chat_input("https://t.me/my_channel"), "my_channel")
        self.assertEqual(tbmd.parse_chat_input("t.me/my_channel"), "my_channel")
        self.assertEqual(tbmd.parse_chat_input("  https://t.me/my_channel  "), "my_channel")

    def test_empty_input(self):
        self.assertIsNone(tbmd.parse_chat_input(""))


class TestStateManagement(unittest.TestCase):
    def test_state_load_and_save(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            orig_file = tbmd.STATE_FILE
            tbmd.STATE_FILE = state_file
            try:
                state = tbmd.load_state()
                self.assertEqual(state, {})

                tbmd.record_download(state, "my_channel", 123, "/path/to/file.mp4")
                tbmd.save_state(state)

                loaded = tbmd.load_state()
                self.assertIn("my_channel", loaded)
                self.assertIn("123", loaded["my_channel"])
                self.assertEqual(loaded["my_channel"]["123"]["path"], "/path/to/file.mp4")
            finally:
                tbmd.STATE_FILE = orig_file

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
            self.assertTrue(tbmd.is_already_downloaded(state, "chan1", 10, existing_file))

            # Not in state, but destination path exists and has content
            self.assertTrue(tbmd.is_already_downloaded(state, "chan1", 99, existing_file))

            # Neither in state nor existing on disk
            non_existent = os.path.join(tmpdir, "none.mp4")
            self.assertFalse(tbmd.is_already_downloaded(state, "chan1", 99, non_existent))


class TestMediaNaming(unittest.TestCase):
    def test_media_naming_with_caption(self):
        msg = MagicMock()
        msg.id = 42
        msg.text = "Awesome Video"
        msg.file.ext = ".mp4"
        msg.file.name = "default.mp4"
        filename, ext = tbmd.get_media_filename(msg)
        self.assertEqual(filename, "42_Awesome Video.mp4")
        self.assertEqual(ext, ".mp4")

    def test_media_naming_without_caption_uses_file_name(self):
        msg = MagicMock()
        msg.id = 99
        msg.text = ""
        msg.file.ext = ".pdf"
        msg.file.name = "report_2026.pdf"
        filename, ext = tbmd.get_media_filename(msg)
        self.assertEqual(filename, "99_report_2026.pdf")
        self.assertEqual(ext, ".pdf")


class TestConfigLoading(unittest.TestCase):
    def setUp(self):
        self.patcher = patch("src.tbmd.load_dotenv")
        self.mock_load_dotenv = self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    @patch.dict(os.environ, {"API_ID": "", "API_HASH": ""}, clear=True)
    def test_missing_credentials(self):
        config, error = tbmd.load_config()
        self.assertIsNone(config)
        self.assertIn("API_ID não definido", error)

    @patch.dict(os.environ, {"API_ID": "not_a_number", "API_HASH": "abc"}, clear=True)
    def test_invalid_api_id(self):
        config, error = tbmd.load_config()
        self.assertIsNone(config)
        self.assertIn("número inteiro válido", error)

    @patch.dict(os.environ, {"API_ID": "123456", "API_HASH": "secret_hash", "BATCH_SIZE": "10"}, clear=True)
    def test_valid_config(self):
        config, error = tbmd.load_config()
        self.assertIsNone(error)
        self.assertEqual(config["api_id"], 123456)
        self.assertEqual(config["api_hash"], "secret_hash")
        self.assertEqual(config["batch_size"], 10)
        self.assertEqual(config["session_name"], "default_session")
        self.assertEqual(config["download_dir"], "downloads")

    @patch.dict(os.environ, {"API_ID": "1", "API_HASH": "h", "DOWNLOAD_DIR": "/tmp/tg"}, clear=True)
    def test_custom_download_dir(self):
        config, _ = tbmd.load_config()
        self.assertEqual(config["download_dir"], "/tmp/tg")


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
        names = [d.name for d in tbmd.search_dialogs(self.dialogs, "musica")]
        self.assertEqual(names, ["Música Clássica"])
        names = [d.name for d in tbmd.search_dialogs(self.dialogs, "INGLES")]
        self.assertEqual(names, ["Curso de Inglês"])

    def test_search_matches_username(self):
        names = [d.name for d in tbmd.search_dialogs(self.dialogs, "wave")]
        self.assertEqual(names, ["Curso de Inglês"])

    def test_search_matches_at_username_url_and_id(self):
        self.assertEqual([d.name for d in tbmd.search_dialogs(self.dialogs, "@ingles_wave")], ["Curso de Inglês"])
        self.assertEqual([d.name for d in tbmd.search_dialogs(self.dialogs, "https://t.me/ingles_wave")], ["Curso de Inglês"])
        self.assertEqual([d.name for d in tbmd.search_dialogs(self.dialogs, "-100987654")], ["Curso de Inglês"])
        self.assertEqual([d.name for d in tbmd.search_dialogs(self.dialogs, "555444333")], ["Família"])

    def test_search_empty_term(self):
        self.assertEqual(tbmd.search_dialogs(self.dialogs, "   "), [])

    def test_dialog_kind(self):
        kinds = [tbmd.dialog_kind(d) for d in self.dialogs]
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
        result = tbmd.filter_media_messages([pdf, renamed_pdf, zip_file], "pdfs")
        self.assertEqual(result, [pdf, renamed_pdf])

    def test_archives(self):
        pdf = make_message("application/pdf", "a.pdf")
        zip_file = make_message("application/zip", "c.zip")
        rar_file = make_message("application/octet-stream", "d.rar")
        result = tbmd.filter_media_messages([pdf, zip_file, rar_file], "archives")
        self.assertEqual(result, [zip_file, rar_file])

    def test_audios(self):
        mp3_doc = make_message("audio/mpeg", "song.mp3")
        flac_doc = make_message("application/octet-stream", "track.FLAC")
        pdf = make_message("application/pdf", "a.pdf")
        result = tbmd.filter_media_messages([mp3_doc, flac_doc, pdf], "audios")
        self.assertEqual(result, [mp3_doc, flac_doc])

    def test_all_skips_messages_without_file(self):
        with_file = make_message("video/mp4", "v.mp4")
        text_only = make_message(media=False)
        self.assertEqual(tbmd.filter_media_messages([with_file, text_only], "all"), [with_file])


class TestCliArgs(unittest.TestCase):
    def test_overrides(self):
        config = {"message_limit": 2000, "batch_size": 5}
        args = tbmd.build_parser().parse_args(["--limit", "0", "-b", "0", "-t", "videos"])
        tbmd.apply_overrides(config, args)
        self.assertIsNone(config["message_limit"])
        self.assertEqual(config["batch_size"], 1)
        self.assertEqual(args.type, "videos")


if __name__ == "__main__":
    unittest.main()
