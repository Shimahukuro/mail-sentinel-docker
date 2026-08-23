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


if __name__ == "__main__":
    unittest.main()
