import sqlite3
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[1] / "python"))

from mail_sentinel_imap import CapabilityIMAP, MailboxInfo
from mail_sentinel_move import SafeMover


SOURCE = MailboxInfo("INBOX", b"INBOX", frozenset())
DESTINATION = MailboxInfo("Junk", b"Junk", frozenset({"\\junk"}))


class FakeIMAP:
    def __init__(self, capabilities=b"IMAP4rev1 UIDPLUS", keywords=True):
        self._capabilities = capabilities
        self.capabilities = ()
        self.enabled = []
        self.selected = "INBOX"
        self.keywords = keywords
        self.fail_after_copy = False
        self.copy_count = 0
        self.folders = {
            "INBOX": {10: set(), 20: {"\\Deleted"}},
            "Junk": {},
        }

    def capability(self):
        return "OK", [self._capabilities]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" INBOX', b'(\\HasNoChildren \\Junk) "/" Junk']

    def select(self, wire_name, readonly=False):
        self.selected = wire_name.decode() if isinstance(wire_name, bytes) else wire_name
        return "OK", [b"1"]

    def response(self, name):
        if name == "UIDVALIDITY":
            return name, [b"7" if self.selected == "Junk" else b"5"]
        if name == "PERMANENTFLAGS":
            return name, [b"(\\Seen \\Deleted \\*)" if self.keywords else b"(\\Seen \\Deleted)"]
        raise AssertionError(name)

    def uid(self, command, *args):
        command = command.upper()
        messages = self.folders[self.selected]
        if command == "SEARCH":
            criterion, value = args[-2], str(args[-1])
            if criterion == "KEYWORD":
                found = [uid for uid, flags in messages.items() if value in flags]
            elif criterion == "UID":
                found = [int(value)] if int(value) in messages else []
            else:
                raise AssertionError(args)
            return "OK", [" ".join(map(str, found)).encode()]
        uid = int(args[0])
        if command == "STORE":
            operation, flags = args[1], str(args[2]).strip("()")
            if uid not in messages:
                return "NO", []
            if operation.startswith("+"):
                messages[uid].add(flags)
            else:
                messages[uid].discard(flags)
            return "OK", []
        if command == "COPY":
            destination = args[1].decode() if isinstance(args[1], bytes) else args[1]
            self.folders[destination][110] = set(messages[uid])
            self.copy_count += 1
            if self.fail_after_copy:
                self.fail_after_copy = False
                raise ConnectionError("connection lost after COPY")
            return "OK", [b"[COPYUID 7 10 110]"]
        if command == "EXPUNGE":
            if "\\Deleted" not in messages[uid]:
                return "NO", []
            del messages[uid]
            return "OK", []
        if command == "MOVE":
            destination = args[1].decode() if isinstance(args[1], bytes) else args[1]
            self.folders[destination][110] = messages.pop(uid)
            return "OK", [b"[COPYUID 7 10 110]"]
        raise AssertionError((command, args))


class SafeMoverTests(unittest.TestCase):
    def mover(self, connection):
        database = sqlite3.connect(":memory:")
        return SafeMover(connection, CapabilityIMAP(connection), database)

    def test_move_capability_keeps_uid_move(self):
        connection = FakeIMAP(b"IMAP4rev1 MOVE UIDPLUS")
        mover = self.mover(connection)
        self.assertEqual("uid_move", mover.plan(SOURCE, DESTINATION, "auto").method)
        self.assertEqual(110, mover.move(SOURCE, 5, 10, DESTINATION, "auto"))
        self.assertNotIn(10, connection.folders["INBOX"])

    def test_uidplus_and_keywords_select_safe_fallback(self):
        connection = FakeIMAP()
        mover = self.mover(connection)
        self.assertEqual("copy_uid_expunge", mover.plan(SOURCE, DESTINATION, "auto").method)
        self.assertEqual(110, mover.move(SOURCE, 5, 10, DESTINATION, "auto"))
        self.assertNotIn(10, connection.folders["INBOX"])
        self.assertIn(20, connection.folders["INBOX"])
        self.assertEqual({"\\Deleted"}, connection.folders["INBOX"][20])
        self.assertEqual(set(), connection.folders["Junk"][110])

    def test_retry_after_ambiguous_copy_does_not_copy_twice(self):
        connection = FakeIMAP()
        mover = self.mover(connection)
        connection.fail_after_copy = True
        with self.assertRaises(ConnectionError):
            mover.move(SOURCE, 5, 10, DESTINATION, "auto")
        self.assertEqual(110, mover.move(SOURCE, 5, 10, DESTINATION, "auto"))
        self.assertEqual(1, connection.copy_count)

    def test_retry_after_marker_cleanup_uses_completed_journal(self):
        connection = FakeIMAP()
        mover = self.mover(connection)
        self.assertEqual(110, mover.move(SOURCE, 5, 10, DESTINATION, "auto"))
        mover.database.execute("UPDATE move_transactions SET stage='source_removed'")
        mover.database.commit()
        self.assertEqual(110, mover.move(SOURCE, 5, 10, DESTINATION, "auto"))
        self.assertEqual(1, connection.copy_count)

    def test_unsupported_without_uidplus_or_keywords(self):
        no_uidplus = FakeIMAP(b"IMAP4rev1")
        self.assertEqual("unsupported", self.mover(no_uidplus).plan(SOURCE, DESTINATION, "auto").method)
        no_keywords = FakeIMAP(keywords=False)
        self.assertEqual("unsupported", self.mover(no_keywords).plan(SOURCE, DESTINATION, "auto").method)

    def test_fallback_is_disabled_when_setting_is_omitted(self):
        connection = FakeIMAP()
        mover = self.mover(connection)
        self.assertEqual("fallback_disabled", mover.plan(SOURCE, DESTINATION, "disabled").reason)
        with self.assertRaisesRegex(RuntimeError, "fallback_disabled"):
            mover.move(SOURCE, 5, 10, DESTINATION)
        self.assertIn(10, connection.folders["INBOX"])
        self.assertEqual({}, connection.folders["Junk"])


if __name__ == "__main__":
    unittest.main()
