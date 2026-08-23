import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[1] / "python"))
from mail_sentinel_operations import OperationsState


class OperationsStateTests(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.state = OperationsState(self.database)

    def tearDown(self):
        self.database.close()

    def test_incident_opens_at_threshold_and_recovers_once(self):
        with patch.dict(os.environ, {"NOTIFICATION_FAILURE_THRESHOLD": "2"}, clear=True):
            self.assertFalse(self.state.record_failure("imap_connection_failed", "TimeoutError"))
            self.assertTrue(self.state.record_failure("imap_connection_failed", "TimeoutError"))
            self.state.mark_notified("imap_connection_failed")
            self.assertTrue(self.state.record_success("imap_connection_failed"))
            self.state.mark_notified("imap_connection_failed", recovery=True)
            self.assertFalse(self.state.record_success("imap_connection_failed"))
        incident = self.database.execute("SELECT * FROM active_incidents").fetchone()
        self.assertEqual("recovered", incident["status"])
        self.assertEqual(2, incident["occurrence_count"])

    def test_snapshot_contains_status_counters_and_no_message_content(self):
        self.state.set_status("last_scan_success", {"processed": 3})
        self.state.increment("messages_spam_count", 2)
        snapshot = self.state.snapshot()
        self.assertEqual("healthy", snapshot["health"])
        self.assertEqual(2, snapshot["counters"]["messages_spam_count"]["value"])
        self.assertNotIn("subject", json.dumps(snapshot))
        self.assertNotIn("sender", json.dumps(snapshot))

    def test_audit_uses_anonymous_account_identifier(self):
        with patch.dict(os.environ, {"MAIL_SENTINEL_ACCOUNT_ID": "abc123"}, clear=True):
            self.state.audit("backup", "success", component_count=3)
        row = self.database.execute("SELECT * FROM audit_events").fetchone()
        self.assertEqual("abc123", row["account_id"])
        self.assertNotIn("password", row["details"])
