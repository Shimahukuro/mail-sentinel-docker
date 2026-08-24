import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[1] / "python"))

from mail_sentinel_imap import CapabilityIMAP, decode_modified_utf7, encode_modified_utf7


class FakeIMAP:
    def __init__(self, capabilities: bytes, mailboxes: list[bytes]):
        self._capabilities = capabilities
        self._mailboxes = mailboxes
        self.enabled = []

    def capability(self):
        return "OK", [self._capabilities]

    def enable(self, capability):
        self.enabled.append(capability)
        return "OK", [b"enabled"]

    def list(self):
        return "OK", self._mailboxes


class ModifiedUtf7Tests(unittest.TestCase):
    def test_round_trip(self):
        for value in ("迷惑メール", "[Gmail]/迷惑メール", "R&D", "INBOX"):
            self.assertEqual(decode_modified_utf7(encode_modified_utf7(value)), value)

    def test_known_nifty_mailbox_encoding(self):
        self.assertEqual(decode_modified_utf7(b"&j,dg0TDhMPww6w-"), "迷惑メール")


class CapabilityTests(unittest.TestCase):
    def test_enables_utf8_only_when_server_advertises_both_capabilities(self):
        connection = FakeIMAP(
            b"IMAP4rev1 ENABLE UTF8=ACCEPT SPECIAL-USE",
            ['(\\HasNoChildren \\Junk) "/" "迷惑メール"'.encode("utf-8")],
        )
        client = CapabilityIMAP(connection)
        self.assertTrue(client.utf8_enabled)
        self.assertEqual(connection.enabled, ["UTF8=ACCEPT"])
        self.assertIn("ENABLE", connection.capabilities)
        self.assertEqual(client.resolve("Junk", "\\Junk").name, "迷惑メール")

    def test_legacy_server_uses_modified_utf7_and_special_use(self):
        connection = FakeIMAP(
            b"IMAP4rev1 MOVE",
            [b'(\\Junk) "/" "&j,dg0TDhMPww6w-"', b'(\\HasNoChildren) "/" INBOX'],
        )
        client = CapabilityIMAP(connection)
        mailbox = client.resolve("Junk", "\\Junk")
        self.assertFalse(client.utf8_enabled)
        self.assertEqual(mailbox.name, "迷惑メール")
        self.assertEqual(mailbox.wire_name, b'"&j,dg0TDhMPww6w-"')

    def test_configured_name_fallback_is_provider_independent(self):
        connection = FakeIMAP(b"IMAP4rev1", [b'(\\HasNoChildren) "/" Spam'])
        self.assertEqual(CapabilityIMAP(connection).resolve("Spam").wire_name, b"Spam")

    def test_inbox_is_case_insensitive_and_preserves_server_wire_name(self):
        connection = FakeIMAP(b"IMAP4rev1 MOVE", [b'(\\HasNoChildren) "/" Inbox'])
        mailbox = CapabilityIMAP(connection).resolve("INBOX")
        self.assertEqual(mailbox.name, "Inbox")
        self.assertEqual(mailbox.wire_name, b"Inbox")

    def test_non_inbox_mailbox_names_remain_case_sensitive(self):
        connection = FakeIMAP(b"IMAP4rev1", [b'(\\HasNoChildren) "/" spam'])
        with self.assertRaisesRegex(RuntimeError, "configured IMAP folder does not exist: Spam"):
            CapabilityIMAP(connection).resolve("Spam")

    def test_quoted_mailbox_wire_name_is_preserved_for_commands(self):
        connection = FakeIMAP(
            b"IMAP4rev1 MOVE",
            [b'(\\HasNoChildren \\Junk) "/" "Bulk Mail"'],
        )
        mailbox = CapabilityIMAP(connection).resolve("Bulk Mail", "\\Junk")
        self.assertEqual(mailbox.name, "Bulk Mail")
        self.assertEqual(mailbox.wire_name, b'"Bulk Mail"')

    def test_configured_name_takes_priority_over_special_use(self):
        connection = FakeIMAP(
            b"IMAP4rev1 MOVE SPECIAL-USE",
            [
                b'(\\HasNoChildren \\Junk) "/" "Spam"',
                b'(\\HasNoChildren) "/" "Junk_Mail-Sentinel"',
            ],
        )
        mailbox = CapabilityIMAP(connection).resolve("Junk_Mail-Sentinel", "\\Junk")
        self.assertEqual(mailbox.name, "Junk_Mail-Sentinel")
        self.assertNotIn("\\junk", mailbox.flags)

    def test_unique_special_use_is_fallback_when_configured_name_is_missing(self):
        connection = FakeIMAP(
            b"IMAP4rev1 MOVE SPECIAL-USE",
            [b'(\\HasNoChildren \\Junk) "/" "Spam"'],
        )
        mailbox = CapabilityIMAP(connection).resolve("Missing", "\\Junk")
        self.assertEqual(mailbox.name, "Spam")

    def test_multiple_special_use_fallbacks_are_rejected_as_ambiguous(self):
        connection = FakeIMAP(
            b"IMAP4rev1 MOVE SPECIAL-USE",
            [
                b'(\\HasNoChildren \\Junk) "/" "Spam"',
                b'(\\HasNoChildren \\Junk) "/" "Old Spam"',
            ],
        )
        with self.assertRaisesRegex(
            RuntimeError, r"multiple IMAP folders have special-use \\Junk: Spam, Old Spam"
        ):
            CapabilityIMAP(connection).resolve("Missing", "\\Junk")


if __name__ == "__main__":
    unittest.main()
