import argparse
import importlib.util
import os
import tempfile
import unittest
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "python"))

SPEC = importlib.util.spec_from_file_location(
    "mail_sentinel_admin", Path(__file__).parents[1] / "admin" / "admin.py"
)
admin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admin)
REAL_MAILBOX = admin.Mailbox


class FakeMailbox:
    uidvalidity = 41
    messages = {
        10: (datetime(2026, 7, 1, tzinfo=timezone.utc), b"Subject: ham\r\n\r\nhello"),
        11: (datetime(2026, 7, 2, tzinfo=timezone.utc), b"Subject: spam\r\n\r\nGTUBE"),
    }
    moves = []

    def __init__(self, _database=None):
        pass

    def close(self):
        pass

    def select(self, folder, readonly):
        return self.uidvalidity

    def candidate_uids(self, since, before):
        return list(self.messages)

    def metadata(self, uid):
        date, raw = self.messages[uid]
        return date, f"subject-{uid}", f"sender-{uid}"

    def message(self, uid):
        return self.messages[uid][1]

    def move(self, uid, destination):
        self.moves.append((uid, destination))
        return uid + 100

    def find_digest(self, folder, digest):
        return None


class FakeSpamAssassin:
    learned = []
    fail_next = False

    def rule_set_id(self):
        return "rules-v1"

    def score(self, raw):
        return (8.0, ["GTUBE"]) if b"GTUBE" in raw else (0.1, [])

    def learn(self, raw, learn_type):
        if type(self).fail_next:
            type(self).fail_next = False
            raise RuntimeError("temporary learning failure")
        self.learned.append((raw, learn_type))


class AdminJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "STATE_DIR": self.temp.name,
            "IMAP_HOST": "greenmail",
            "IMAP_PORT": "3143",
            "IMAP_USERNAME": "test@test.local",
            "IMAP_JUNK": "Junk",
            "IMAP_MOVE_FALLBACK": "auto",
            "IMAP_TIMEOUT_SECONDS": "10",
            "SPAMC_MAX_SIZE_BYTES": "10485760",
        })
        self.env.start()
        self.mail_patch = patch.object(admin, "Mailbox", FakeMailbox)
        self.spam_patch = patch.object(admin, "SpamAssassin", FakeSpamAssassin)
        self.mail_patch.start()
        self.spam_patch.start()
        FakeMailbox.uidvalidity = 41
        FakeMailbox.moves = []
        FakeSpamAssassin.learned = []
        FakeSpamAssassin.fail_next = False
        self.state = admin.State(Path(self.temp.name) / "state.sqlite3")

    def tearDown(self):
        self.state.close()
        self.spam_patch.stop()
        self.mail_patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def preview_args(self, job_type="initial_learn"):
        values = dict(folder="INBOX", since_date="2026-07-01", through_date="2026-07-31",
                      timezone="Asia/Tokyo", since_datetime=None, before_datetime=None, max_messages=10,
                      batch_size=1, job_type=job_type)
        if job_type == "initial_learn":
            values["learn_type"] = "ham"
        else:
            values.update(max_moves=1, threshold=5.0)
        return argparse.Namespace(**values)

    def latest_preview(self):
        return self.state.db.execute("SELECT * FROM jobs WHERE mode='preview' ORDER BY created_at DESC").fetchone()

    def test_learning_preview_is_read_only_and_apply_preserves_messages(self):
        admin.preview(self.preview_args(), self.state)
        preview = self.latest_preview()
        self.assertEqual([], FakeSpamAssassin.learned)
        self.assertEqual([], FakeMailbox.moves)
        admin.apply_job(argparse.Namespace(job_id=preview["job_id"], confirm=preview["confirmation_token"]), self.state)
        self.assertEqual(2, len(FakeSpamAssassin.learned))
        self.assertEqual([], FakeMailbox.moves)

    def test_interrupted_learning_resumes_same_apply_job(self):
        admin.preview(self.preview_args(), self.state)
        preview = self.latest_preview()
        FakeSpamAssassin.fail_next = True
        arguments = argparse.Namespace(job_id=preview["job_id"], confirm=preview["confirmation_token"])
        admin.apply_job(arguments, self.state)
        apply_id = self.state.db.execute("SELECT job_id FROM jobs WHERE mode='apply'").fetchone()[0]
        self.assertEqual("interrupted", self.state.job(apply_id)["status"])
        admin.apply_job(arguments, self.state)
        self.assertEqual(apply_id, self.state.db.execute("SELECT job_id FROM jobs WHERE mode='apply'").fetchone()[0])
        self.assertEqual("completed", self.state.job(apply_id)["status"])

    def test_uidvalidity_change_rejects_apply(self):
        admin.preview(self.preview_args(), self.state)
        preview = self.latest_preview()
        FakeMailbox.uidvalidity = 42
        with self.assertRaisesRegex(RuntimeError, "UIDVALIDITY changed"):
            admin.apply_job(argparse.Namespace(job_id=preview["job_id"], confirm=preview["confirmation_token"]), self.state)

    def test_scan_moves_only_spam_after_confirmation(self):
        admin.preview(self.preview_args("initial_scan"), self.state)
        preview = self.latest_preview()
        self.assertEqual([], FakeMailbox.moves)
        with self.assertRaisesRegex(RuntimeError, "confirmation token"):
            admin.apply_job(argparse.Namespace(job_id=preview["job_id"], confirm="wrong"), self.state)
        admin.apply_job(argparse.Namespace(job_id=preview["job_id"], confirm=preview["confirmation_token"]), self.state)
        self.assertEqual([(11, "Junk")], FakeMailbox.moves)

    def test_scan_reconciles_move_completed_before_state_commit(self):
        admin.preview(self.preview_args("initial_scan"), self.state)
        preview = self.latest_preview()
        arguments = argparse.Namespace(job_id=preview["job_id"], confirm=preview["confirmation_token"])
        admin.apply_job(arguments, self.state)
        apply_id = self.state.db.execute("SELECT job_id FROM jobs WHERE mode='apply'").fetchone()[0]
        self.state.db.execute(
            "UPDATE message_results SET status='retryable' WHERE job_id=? AND uid=11", (apply_id,)
        )
        self.state.db.execute("UPDATE jobs SET moved_count=0,status='interrupted' WHERE job_id=?", (apply_id,))
        self.state.db.commit()
        original_message = FakeMailbox.message

        def missing_source(mailbox, uid):
            if uid == 11:
                raise RuntimeError("message no longer exists in source")
            return original_message(mailbox, uid)

        with patch.object(FakeMailbox, "message", missing_source), \
             patch.object(FakeMailbox, "find_digest", return_value=111):
            admin.apply_job(arguments, self.state)
        row = self.state.db.execute(
            "SELECT status,destination_uid FROM message_results WHERE job_id=? AND uid=11", (apply_id,)
        ).fetchone()
        self.assertEqual(("moved", 111), tuple(row))

    def test_digest_prevents_duplicate_learning_after_uid_change(self):
        digest = "abc"
        old_job = self.state.create_job(
            job_type="initial_learn", mode="apply", status="completed", account_key="a",
            folder="INBOX", uidvalidity=1, learn_type="ham", max_messages=1,
            batch_size=1, started_at=admin.utc_now(),
        )
        self.state.db.execute(
            "INSERT INTO message_results(job_id,uidvalidity,uid,internaldate,digest,status,learn_type) VALUES(?,?,?,?,?,?,?)",
            (old_job, 1, 9, admin.utc_now(), digest, "learned", "ham"),
        )
        self.state.db.commit()
        job = self.state.job(old_job)
        self.assertTrue(admin.previous_success(self.state, job, digest, 999))

    def test_period_and_maximum_limit_candidates(self):
        arguments = self.preview_args()
        arguments.since_date = "2026-07-02"
        arguments.max_messages = 1
        admin.preview(arguments, self.state)
        preview = self.latest_preview()
        rows = self.state.db.execute(
            "SELECT uid FROM message_results WHERE job_id=?", (preview["job_id"],)
        ).fetchall()
        self.assertEqual([11], [row[0] for row in rows])

    def test_jst_date_range_becomes_inclusive_utc_half_open_range(self):
        arguments = self.preview_args()
        arguments.since_date = "2026-06-25"
        arguments.through_date = "2026-08-18"
        since, before = admin.resolve_range(arguments)
        self.assertEqual("2026-06-24T15:00:00+00:00", since.isoformat())
        self.assertEqual("2026-08-18T15:00:00+00:00", before.isoformat())

    def test_datetime_range_remains_available_for_advanced_use(self):
        arguments = self.preview_args()
        arguments.since_date = arguments.through_date = None
        arguments.since_datetime = "2026-06-25T00:00:00+09:00"
        arguments.before_datetime = "2026-08-18T20:00:00+09:00"
        since, before = admin.resolve_range(arguments)
        self.assertEqual("2026-06-24T15:00:00+00:00", since.isoformat())
        self.assertEqual("2026-08-18T11:00:00+00:00", before.isoformat())

    def test_imap_date_prefilter_is_wider_than_exact_range(self):
        class SearchRecorder:
            def __init__(self):
                self.arguments = None

            def uid(self, *arguments):
                self.arguments = arguments
                return "OK", [b""]

        mailbox = REAL_MAILBOX.__new__(REAL_MAILBOX)
        mailbox.imap = SearchRecorder()
        arguments = self.preview_args()
        arguments.since_date = "2026-06-25"
        arguments.through_date = "2026-08-18"
        since, before = admin.resolve_range(arguments)
        mailbox.candidate_uids(since, before)
        self.assertEqual(
            ("SEARCH", None, "ALL", "SINCE", "23-Jun-2026", "BEFORE", "19-Aug-2026"),
            mailbox.imap.arguments,
        )


if __name__ == "__main__":
    unittest.main()
