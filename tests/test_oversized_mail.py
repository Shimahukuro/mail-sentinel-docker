"""Exercise real scan selection, fetch parsing and persistent retry boundaries."""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).parents[1] / "worker"))
import worker
from mail_sentinel_imap import MailboxInfo


def mailbox(name):
    return MailboxInfo(name, ('"' + name + '"').encode(), frozenset())


class FakeIMAP:
    def __init__(self, messages, keywords=True):
        self.messages = dict(messages)
        self.keywords = keywords
        self.checked = set()
        self.bodies = []
        self.moves = []
        self.generation = 1
        self.selected = "INBOX"
        self.fail_move = False
        self.calls = []

    def select(self, folder, readonly=False):
        self.selected = folder.decode().strip('"')
        return "OK", []

    def response(self, name):
        if name == "UIDVALIDITY":
            return name, [str(self.generation).encode()]
        if name == "PERMANENTFLAGS":
            return name, [b"(\\*)" if self.keywords else b"()"]
        raise AssertionError(name)

    def logout(self):
        pass

    def uid(self, command, uid, *args):
        self.calls.append((command, uid, args))
        if command == "SEARCH":
            candidates = set(self.messages)
            if "UNKEYWORD" in args:
                candidates -= self.checked
            elif "KEYWORD" in args:
                candidates &= self.checked
            return "OK", [b" ".join(str(value).encode() for value in sorted(candidates))]
        uid = int(uid)
        if command == "FETCH":
            if self.selected != "INBOX":
                raise AssertionError("fetch used the wrong selected mailbox")
            raw = self.messages[uid]
            if args[0] == "(UID RFC822.SIZE)":
                return "OK", [f"1 (UID {uid} RFC822.SIZE {len(raw)})".encode()]
            self.bodies.append(uid)
            count = int(re.search(r"<0\.(\d+)>", args[0])[1])
            return "OK", [(f"1 (UID {uid} BODY[]<0> {{{min(count, len(raw))}}}".encode(), raw[:count]), b")"]
        if command == "STORE":
            self.checked.add(uid)
            return "OK", []
        raise AssertionError(command)


class OversizedMailTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = {
            "STATE_DIR": directory.name, "SPAMC_MAX_SIZE_BYTES": "10",
            "DRY_RUN": "false", "IMAP_INBOX": "INBOX", "IMAP_JUNK": "Junk",
            "IMAP_MANUAL_REVIEW": "", "LEARNING_ENABLED": "false", "LOOKBACK_DAYS": "1",
            "BATCH_SIZE": "2", "PROCESSED_STATE": "auto", "PROCESSED_FLAG": "Checked",
            "LEARNED_FLAG": "Learned", "LEARNING_BATCH_SIZE": "2",
            "NOTIFICATION_ENABLED": "false", "CREATE_MISSING_FOLDERS": "false",
            "RETRY_INITIAL_SECONDS": "5", "RETRY_MAX_SECONDS": "300",
        }
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, environment, clear=True).start()
        self.logs = patch.object(worker, "log").start()
        self.scorer = patch.object(worker, "score", return_value=(0.0, 5.0)).start()
        self.client = Mock()
        self.client.resolve.side_effect = lambda name, *args: mailbox(name)

    def scan(self, connection):
        with patch.object(worker, "connect", return_value=(connection, self.client)):
            worker.scan_once()

    def test_original_trigger_progresses_after_restart_in_both_state_modes(self):
        for keywords in (True, False):
            with self.subTest(keywords=keywords), tempfile.TemporaryDirectory() as directory:
                os.environ["STATE_DIR"] = directory
                connection = FakeIMAP({1: b"x" * 11, 2: b"x" * 12, 3: b"ham"}, keywords)
                self.scan(connection)
                self.assertEqual(connection.bodies, [])
                self.scan(connection)  # New WorkerState and DB connection each time.
                self.assertEqual(connection.bodies, [3])
                self.scan(connection)
                self.assertEqual(connection.bodies, [3])
                self.assertFalse({1, 2} & connection.checked)
                self.assertEqual(self.scorer.call_args.args, (b"ham",))

    def test_exact_limit_is_accepted(self):
        connection = FakeIMAP({1: b"x" * 10})
        self.assertEqual(worker.fetch_message(connection, 1), b"x" * 10)

    def test_bad_size_response_never_fetches_body(self):
        for metadata in ([], [b"1 (UID 2 RFC822.SIZE 11)"], [b"1 (UID 1)"],
                         [b"1 (UID 1 RFC822.SIZE -1)"],
                         [b"1 (UID 1 RFC822.SIZE 1)"] * 2):
            with self.subTest(metadata=metadata):
                connection = Mock()
                connection.uid.return_value = ("OK", metadata)
                with self.assertRaises(RuntimeError):
                    worker.fetch_message(connection, 1)
                self.assertEqual(connection.uid.call_count, 1)

    def test_underreported_size_and_truncated_body_are_not_scored(self):
        for raw, exception in ((b"x" * 11, worker.MessageTooLarge), (b"short", RuntimeError)):
            connection = Mock()
            connection.uid.side_effect = [
                ("OK", [b"1 (UID 1 RFC822.SIZE 10)"]),
                ("OK", [(b"1 (UID 1 BODY[]<0> {11}", raw)]),
            ]
            with self.assertRaises(exception):
                worker.fetch_message(connection, 1)

    def test_uid_after_literal_and_unrelated_unsolicited_response(self):
        connection = Mock()
        connection.uid.side_effect = [
            ("OK", [b"9 (UID 9 FLAGS (\\Seen))", b"1 (RFC822.SIZE 3 UID 1)"]),
            ("OK", [(b"1 (BODY[]<0> {3}", b"ham"), b" UID 1)",
                    b"9 (UID 9 FLAGS (\\Seen))"]),
        ]
        self.assertEqual(worker.fetch_message(connection, 1), b"ham")

    def test_case_insensitive_fetch_attributes(self):
        connection = Mock()
        connection.uid.side_effect = [
            ("OK", [b"1 (uid 1 rfc822.size 3)"]),
            ("OK", [(b"1 (uid 1 body[]<0> {3}", b"ham"), b")"]),
        ]
        self.assertEqual(worker.fetch_message(connection, 1), b"ham")

    def test_greenmail_partial_literal_without_space(self):
        connection = Mock()
        connection.uid.side_effect = [
            ("OK", [b"1 (RFC822.SIZE 3 UID 1)"]),
            ("OK", [(b"1 (UID 1 BODY[]<0>{3}", b"ham"), b")"]),
        ]
        self.assertEqual(worker.fetch_message(connection, 1), b"ham")

    def test_manual_review_cannot_alias_special_use_junk(self):
        os.environ["IMAP_MANUAL_REVIEW"] = "Spam"
        def resolve(name, *args):
            return mailbox("Spam" if name == "Junk" else name)
        self.client.resolve.side_effect = resolve
        with patch.object(worker, "move") as move:
            self.scan(FakeIMAP({1: b"x" * 11}))
            move.assert_not_called()

    def test_only_oversized_mail_is_moved_and_logged_uninspected(self):
        os.environ["IMAP_MANUAL_REVIEW"] = "ManualReview"
        connection = FakeIMAP({1: b"x" * 11, 2: b"ham"})
        with patch.object(worker, "move") as move:
            self.scan(connection)
        self.assertEqual(move.call_count, 1)
        self.assertEqual(move.call_args.args[5], 1)
        self.assertEqual(move.call_args.args[6].name, "ManualReview")
        self.assertEqual(connection.bodies, [2])
        self.assertNotIn(1, connection.checked)
        events = [call.kwargs for call in self.logs.call_args_list
                  if call.args[1] == "message_uninspected"]
        self.assertEqual(events[0]["action"], "moved")
        self.assertEqual(events[0]["reason"], "message_too_large")
        self.assertEqual(events[0]["size_bytes"], 11)
        self.assertEqual(events[0]["size_limit_bytes"], 10)

    def test_failed_move_restores_source_and_later_mail_progresses(self):
        os.environ["IMAP_MANUAL_REVIEW"] = "ManualReview"
        connection = FakeIMAP({1: b"x" * 11, 2: b"ham"})
        def fail(*args):
            connection.selected = "ManualReview"
            raise RuntimeError("move failed")
        with patch.object(worker, "move", side_effect=fail) as move:
            self.scan(connection)
            self.scan(connection)
            self.assertEqual(move.call_count, 1)
        self.assertEqual(connection.bodies, [2])

    def test_enabling_destination_revisits_previously_retained_mail(self):
        connection = FakeIMAP({1: b"x" * 11})
        self.scan(connection)
        os.environ["IMAP_MANUAL_REVIEW"] = "ManualReview"
        with patch.object(worker, "move") as move:
            self.scan(connection)
            move.assert_called_once()

    def test_dry_run_does_not_resolve_create_move_or_suppress_live_scan(self):
        os.environ.update(IMAP_MANUAL_REVIEW="ManualReview", DRY_RUN="true")
        connection = FakeIMAP({1: b"x" * 11, 2: b"ham"})
        with patch.object(worker, "move") as move:
            self.scan(connection)
            move.assert_not_called()
            self.assertEqual([call.args[0] for call in self.client.resolve.call_args_list], ["INBOX", "Junk"])
            self.assertEqual(connection.checked, set())
            os.environ["DRY_RUN"] = "false"
            self.scan(connection)
            move.assert_called_once()

    def test_retry_expiry_does_not_reset_cursor_to_failed_head(self):
        with patch.object(worker.time, "time", return_value=100):
            state = worker.WorkerState()
            for uid in state.batch("scan", "INBOX", 1, [1, 2, 3], 2):
                state.advance("scan", "INBOX", 1, uid)
                state.defer("scan", "INBOX", 1, uid, RuntimeError("temporary"))
            state.close()
        with patch.object(worker.time, "time", return_value=1000):
            state = worker.WorkerState()
            self.assertEqual(state.batch("scan", "INBOX", 1, [1, 2, 3], 2), [3, 1])
            state.close()

    def test_limit_generation_and_candidate_changes_invalidate_deferral(self):
        state = worker.WorkerState()
        self.addCleanup(state.close)
        state.defer("scan", "INBOX", 1, 1, worker.MessageTooLarge(11))
        self.assertEqual(state.batch("scan", "INBOX", 1, [1], 2), [])
        os.environ["SPAMC_MAX_SIZE_BYTES"] = "12"
        self.assertEqual(state.batch("scan", "INBOX", 1, [1], 2), [1])
        os.environ["SPAMC_MAX_SIZE_BYTES"] = "10"
        self.assertEqual(state.batch("scan", "INBOX", 2, [1], 2), [1])
        self.assertEqual(state.db.execute("SELECT count(*) FROM deferred_messages").fetchone()[0], 0)
        state.defer("scan", "INBOX", 2, 1, worker.MessageTooLarge(11))
        state.batch("scan", "INBOX", 2, [], 2)
        self.assertEqual(state.db.execute("SELECT count(*) FROM deferred_messages").fetchone()[0], 0)

    def test_transient_scoring_error_is_not_manually_routed(self):
        os.environ["IMAP_MANUAL_REVIEW"] = "ManualReview"
        connection = FakeIMAP({1: b"ham", 2: b"ham"})
        self.scorer.side_effect = [RuntimeError("temporary"), (0.0, 5.0)]
        with patch.object(worker, "move") as move:
            self.scan(connection)
            move.assert_not_called()
        self.assertEqual(connection.checked, {2})

    def test_processing_folders_cannot_be_manual_review_destination(self):
        for name in ("inbox", "Junk", "Learn-Ham", "Learn-Spam"):
            os.environ.update(IMAP_LEARN_HAM="Learn-Ham", IMAP_LEARN_SPAM="Learn-Spam",
                              IMAP_MANUAL_REVIEW=name)
            with self.assertRaises(RuntimeError):
                worker.validate_manual_review()

    def test_learning_oversize_remains_unlearned_and_later_mail_progresses(self):
        os.environ["IMAP_MANUAL_REVIEW"] = "ManualReview"
        for keywords in (True, False):
            with self.subTest(keywords=keywords), tempfile.TemporaryDirectory() as directory:
                os.environ["STATE_DIR"] = directory
                connection = FakeIMAP({1: b"x" * 11, 2: b"x" * 12, 3: b"ham"}, keywords)
                with patch.object(worker, "move") as move, patch.object(worker, "learn") as learn:
                    for _ in range(2):
                        state = worker.WorkerState()
                        try:
                            worker.process_learning(connection, self.client, state,
                                                    mailbox("INBOX"), mailbox("Junk"), "spam")
                        finally:
                            state.close()
                learn.assert_called_once_with(b"ham", "spam")
                move.assert_called_once()
                self.assertEqual(move.call_args.args[5], 3)
                self.assertEqual(connection.bodies, [3])
                self.assertFalse({1, 2} & connection.checked)

    def test_learning_move_retry_does_not_relearn_or_starve_pending_mail(self):
        os.environ["LEARNING_BATCH_SIZE"] = "1"
        connection = FakeIMAP({1: b"already learned", 2: b"ham"})
        connection.checked.add(1)
        def move(*args):
            if args[5] == 1:
                raise RuntimeError("temporary move failure")
        with patch.object(worker, "move", side_effect=move), patch.object(worker, "learn") as learn:
            for _ in range(2):
                state = worker.WorkerState()
                try:
                    worker.process_learning(connection, self.client, state,
                                            mailbox("INBOX"), mailbox("Junk"), "spam")
                finally:
                    state.close()
            learn.assert_called_once_with(b"ham", "spam")
        self.assertEqual(connection.bodies, [2])


if __name__ == "__main__":
    unittest.main()
