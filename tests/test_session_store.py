import os
import tempfile
import unittest

from src.queue_db import QueueDatabase
from src.session_store import (
    SessionKeyError,
    SessionStore,
    decrypt,
    derive_key,
    encrypt,
    migrate_legacy_session,
)


class TestSessionCrypto(unittest.TestCase):
    def test_encrypt_decrypt_roundtrip(self):
        salt = b"0123456789abcdef"
        nonce = b"123456789012"
        ciphertext = encrypt("session-payload", "minha-senha", salt, nonce)
        self.assertNotIn(b"session-payload", ciphertext)
        self.assertEqual(decrypt(ciphertext, "minha-senha", salt, nonce), "session-payload")

    def test_wrong_passphrase_raises(self):
        salt = b"0123456789abcdef"
        nonce = b"123456789012"
        ciphertext = encrypt("session-payload", "minha-senha", salt, nonce)
        with self.assertRaises(SessionKeyError):
            decrypt(ciphertext, "outra-senha", salt, nonce)

    def test_tampered_ciphertext_raises(self):
        salt = b"0123456789abcdef"
        nonce = b"123456789012"
        ciphertext = bytearray(encrypt("session-payload", "minha-senha", salt, nonce))
        ciphertext[0] ^= 0xFF
        with self.assertRaises(SessionKeyError):
            decrypt(bytes(ciphertext), "minha-senha", salt, nonce)

    def test_derive_key_is_deterministic_and_salted(self):
        salt = b"0123456789abcdef"
        self.assertEqual(derive_key("senha", salt), derive_key("senha", salt))
        self.assertNotEqual(derive_key("senha", salt), derive_key("senha", b"fedcba9876543210"))

    def test_empty_passphrase_raises(self):
        with self.assertRaises(SessionKeyError):
            derive_key("", b"0123456789abcdef")


class TestSessionStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = QueueDatabase(os.path.join(self.tmp.name, "sessions.db"))
        self.store = SessionStore(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_and_load(self):
        self.assertFalse(self.store.has_session())
        self.store.save("1sessionstring", "senha", phone="+5511999999999", user_id=42)
        self.assertTrue(self.store.has_session())
        self.assertEqual(self.store.load("senha"), "1sessionstring")

    def test_metadata_never_exposes_the_key(self):
        self.store.save("1sessionstring", "senha", phone="+5511999999999", user_id=42)
        metadata = self.store.get_metadata()
        self.assertEqual(metadata["phone"], "+5511999999999")
        self.assertEqual(metadata["user_id"], 42)
        self.assertNotIn("1sessionstring", str(metadata))

    def test_load_with_wrong_passphrase_raises(self):
        self.store.save("1sessionstring", "senha")
        with self.assertRaises(SessionKeyError):
            self.store.load("errada")

    def test_load_without_session_raises(self):
        with self.assertRaises(SessionKeyError):
            self.store.load("senha")

    def test_delete_removes_the_session(self):
        self.store.save("1sessionstring", "senha")
        self.assertTrue(self.store.delete())
        self.assertFalse(self.store.has_session())
        self.assertFalse(self.store.delete())

    def test_save_replaces_the_previous_session(self):
        self.store.save("primeira", "senha")
        self.store.save("segunda", "senha")
        self.assertEqual(self.store.load("senha"), "segunda")

    def test_ciphertext_is_not_stored_in_cleartext(self):
        self.store.save("1sessionstring", "senha")
        row = self.db.get_session_row()
        self.assertNotIn(b"1sessionstring", row["ciphertext"])


class TestLegacyMigration(unittest.TestCase):
    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(migrate_legacy_session(os.path.join(tmpdir, "inexistente.session")))

    def test_file_without_auth_key_returns_none(self):
        from telethon.sessions import SQLiteSession

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "vazio.session")
            SQLiteSession(path)
            self.assertIsNone(migrate_legacy_session(path))


if __name__ == "__main__":
    unittest.main()
