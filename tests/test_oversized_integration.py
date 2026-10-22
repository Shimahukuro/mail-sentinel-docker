"""Opt-in tests against a disposable GreenMail instance (never a real account).

Set MAIL_SENTINEL_TEST_IMAP_PORT to its published loopback IMAP port.
"""
import imaplib
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).parents[1] / "worker"))
import worker
from mail_sentinel_imap import CapabilityIMAP


@unittest.skipUnless(os.environ.get("MAIL_SENTINEL_TEST_IMAP_PORT"), "requires disposable GreenMail")
class GreenMailOversizedTests(unittest.TestCase):
    def test_real_imap_retention_routing_and_dry_run(self):
        port = int(os.environ["MAIL_SENTINEL_TEST_IMAP_PORT"])
        manual_folder = "ManualReview_" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "STATE_DIR": directory, "SPAMC_MAX_SIZE_BYTES": "256", "DRY_RUN": "false",
                "IMAP_INBOX": "INBOX", "IMAP_JUNK": "Junk", "IMAP_MANUAL_REVIEW": "",
                "LEARNING_ENABLED": "false", "LOOKBACK_DAYS": "1", "BATCH_SIZE": "2",
                "PROCESSED_STATE": "auto", "PROCESSED_FLAG": "MailSentinelChecked",
                "NOTIFICATION_ENABLED": "false", "CREATE_MISSING_FOLDERS": "true",
                "IMAP_MOVE_FALLBACK": "auto",
            }
            def connect():
                connection = imaplib.IMAP4("127.0.0.1", port, timeout=10)
                connection.login("test@test.local", "greenmail")
                return connection, CapabilityIMAP(connection)

            connection, _ = connect()
            try:
                connection.create('"Junk"')
                # Disposable server only: reset INBOX so repeated test runs are deterministic.
                connection.select("INBOX")
                connection.store("1:*", "+FLAGS.SILENT", "(\\Deleted)")
                connection.expunge()
                oversized = b"From: sender@test.local\r\nSubject: large\r\n\r\n" + b"x" * 300
                ham = b"From: sender@test.local\r\nSubject: small\r\n\r\nhello\r\n"
                for raw in (oversized, oversized, ham):
                    self.assertEqual(connection.append("INBOX", None, None, raw)[0], "OK")
            finally:
                connection.logout()

            def inspect(folder):
                connection, _ = connect()
                try:
                    connection.select(folder)
                    return worker.search(connection, "ALL")
                finally:
                    connection.logout()

            requests = []
            original_uid = imaplib.IMAP4.uid
            def record(connection, command, *args):
                requests.append((command, args))
                return original_uid(connection, command, *args)

            with patch.dict(os.environ, environment, clear=True), \
                    patch.object(worker, "connect", side_effect=connect), \
                    patch.object(worker, "score", return_value=(0.0, 5.0)) as score, \
                    patch.object(imaplib.IMAP4, "uid", record):
                worker.scan_once()
                self.assertFalse(any(command == "FETCH" and "BODY" in str(args) for command, args in requests))
                worker.scan_once()
                score.assert_called_once_with(ham)
                self.assertEqual(len(inspect("INBOX")), 3)
                os.environ.update(IMAP_MANUAL_REVIEW=manual_folder, DRY_RUN="true")
                worker.scan_once()
                self.assertEqual(len(inspect("INBOX")), 3)
                connection, _ = connect()
                try:
                    self.assertFalse(connection.list('""', '"' + manual_folder + '"')[1][0])
                finally:
                    connection.logout()
                os.environ["DRY_RUN"] = "false"
                worker.scan_once()
                self.assertEqual(len(inspect("INBOX")), 1)
                self.assertEqual(len(inspect(manual_folder)), 2)
                connection, _ = connect()
                try:
                    connection.select(manual_folder)
                    self.assertEqual(worker.search(connection, "KEYWORD", "MailSentinelChecked"), [])
                finally:
                    connection.logout()
