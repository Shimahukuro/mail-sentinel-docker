import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).parents[1] / "worker"))

import worker


class FakeSelectedIMAP:
    def __init__(self, permanent_flags):
        self.permanent_flags = permanent_flags

    def response(self, name):
        if name == "PERMANENTFLAGS":
            return name, [self.permanent_flags]
        if name == "UIDVALIDITY":
            return name, [b"123"]
        raise AssertionError(name)


class ProcessedMethodTests(unittest.TestCase):
    def test_auto_uses_keywords_when_server_allows_custom_flags(self):
        with patch.dict(os.environ, {"PROCESSED_STATE": "auto"}, clear=True):
            self.assertEqual(worker.processed_method(FakeSelectedIMAP(b"(\\Seen \\*)")), "imap_keyword")

    def test_auto_falls_back_when_permanent_flags_are_empty(self):
        with patch.dict(os.environ, {"PROCESSED_STATE": "auto"}, clear=True):
            self.assertEqual(worker.processed_method(FakeSelectedIMAP(b"()")), "local_database")

    def test_forced_keyword_mode_fails_when_unsupported(self):
        with patch.dict(os.environ, {"PROCESSED_STATE": "imap_keyword"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "does not support"):
                worker.processed_method(FakeSelectedIMAP(b"()"))


class WorkerStateTests(unittest.TestCase):
    def test_runtime_version_is_persisted_for_status_and_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"STATE_DIR": directory}, clear=True):
                state = worker.WorkerState()
                try:
                    state.record_runtime_version("0.1.0")
                    status = state.operations.snapshot()["status"]["mail_sentinel_version"]
                    self.assertEqual(status["value"], "0.1.0")
                    audit = state.db.execute(
                        "SELECT action,details FROM audit_events ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    self.assertEqual(audit["action"], "runtime_version_recorded")
                    self.assertEqual(json.loads(audit["details"])["version"], "0.1.0")
                finally:
                    state.close()

    def test_processed_uids_are_scoped_by_folder_and_uidvalidity(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"STATE_DIR": directory}, clear=True):
                state = worker.WorkerState()
                try:
                    state.mark_processed("INBOX", 10, 7, "ham")
                    self.assertEqual(state.processed("INBOX", 10, [7, 8]), {7})
                    self.assertEqual(state.processed("INBOX", 11, [7]), set())
                    self.assertEqual(state.processed("Other", 10, [7]), set())
                finally:
                    state.close()

    def test_learning_state_can_resume_after_learning_before_move(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"STATE_DIR": directory}, clear=True):
                state = worker.WorkerState()
                try:
                    state.mark_learning("Learn-Spam", 10, 9, "spam", "learned")
                    self.assertEqual(state.learning_status("Learn-Spam", 10, 9, "spam"), "learned")
                    state.mark_learning("Learn-Spam", 10, 9, "spam", "moved")
                    self.assertEqual(state.learning_status("Learn-Spam", 10, 9, "spam"), "moved")
                finally:
                    state.close()

    def test_new_github_release_notifies_once(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "STATE_DIR": directory,
                "NOTIFICATION_ENABLED": "true",
                "NOTIFICATION_UPDATE_ENABLED": "true",
                "VERSION_CHECK_INTERVAL_SECONDS": "1",
            }
            with patch.dict(os.environ, environment, clear=True):
                state = worker.WorkerState()
                try:
                    with patch.object(state.notifier, "send", return_value=True) as send:
                        state.check_for_update(
                            "0.1.0", lambda: ("v0.2.0", "https://example.invalid/v0.2.0")
                        )
                        state.check_for_update(
                            "0.1.0", lambda: ("v0.2.0", "https://example.invalid/v0.2.0")
                        )
                    send.assert_called_once_with(
                        "update_available", current_version="0.1.0", latest_version="v0.2.0",
                        release_url="https://example.invalid/v0.2.0"
                    )
                    value = state.db.execute(
                        "SELECT value FROM runtime_status WHERE key='version_update_notified'"
                    ).fetchone()[0]
                    self.assertEqual(json.loads(value), "v0.2.0")
                finally:
                    state.close()

    def test_failed_update_notification_is_not_marked_as_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "STATE_DIR": directory,
                "NOTIFICATION_ENABLED": "true",
                "NOTIFICATION_UPDATE_ENABLED": "true",
            }
            with patch.dict(os.environ, environment, clear=True):
                state = worker.WorkerState()
                try:
                    with patch.object(state.notifier, "send", side_effect=RuntimeError("temporary")):
                        state.check_for_update(
                            "0.1.0", lambda: ("v0.2.0", "https://example.invalid/v0.2.0")
                        )
                    value = state.db.execute(
                        "SELECT value FROM runtime_status WHERE key='version_update_notified'"
                    ).fetchone()
                    self.assertIsNone(value)
                finally:
                    state.close()

    def test_semantic_version_comparison(self):
        self.assertGreater(worker.version_key("v1.2.0"), worker.version_key("1.1.9"))
        self.assertGreater(worker.version_key("1.0.0"), worker.version_key("1.0.0-rc.1"))


if __name__ == "__main__":
    unittest.main()
