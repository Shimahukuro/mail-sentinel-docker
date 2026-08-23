import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[1] / "worker"))
sys.path.insert(0, str(Path(__file__).parents[1] / "python"))

from mail_sentinel_accounts import account_environment, account_id, load_accounts


class AccountConfigurationTests(unittest.TestCase):
    def write_config(self, document):
        temporary = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False)
        self.addCleanup(Path(temporary.name).unlink, missing_ok=True)
        with temporary:
            json.dump(document, temporary)
        return Path(temporary.name)

    def test_loads_multiple_accounts_and_normalizes_values(self):
        path = self.write_config({"accounts": [
            {"name": "primary", "environment": {
                "IMAP_AUTH_METHOD": "password", "IMAP_PASSWORD_FILE": "/run/secrets/primary",
                "IMAP_HOST": "mail.example", "IMAP_PORT": 993, "DRY_RUN": False,
            }},
            {"name": "secondary", "environment": {
                "IMAP_AUTH_METHOD": "xoauth2",
                "IMAP_OAUTH_ACCESS_TOKEN_FILE": "/run/secrets/secondary_token",
                "IMAP_HOST": "imap.example",
            }},
        ]})
        accounts = load_accounts(path)
        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[0][2]["IMAP_PORT"], "993")
        self.assertEqual(accounts[0][2]["DRY_RUN"], "false")
        self.assertNotIn("primary", accounts[0][1])

    def test_rejects_duplicate_names(self):
        path = self.write_config({"accounts": [
            {"name": "same", "environment": {"IMAP_PASSWORD_FILE": "/one"}},
            {"name": "same", "environment": {"IMAP_PASSWORD_FILE": "/two"}},
        ]})
        with self.assertRaisesRegex(RuntimeError, "unique"):
            load_accounts(path)

    def test_defaults_are_shared_and_account_values_override_them(self):
        path = self.write_config({
            "defaults": {"IMAP_PORT": 993, "DRY_RUN": True, "BATCH_SIZE": 25},
            "accounts": [{"name": "primary", "environment": {
                "IMAP_PASSWORD_FILE": "/secret", "BATCH_SIZE": 5,
            }}],
        })
        environment = load_accounts(path)[0][2]
        self.assertEqual(environment["IMAP_PORT"], "993")
        self.assertEqual(environment["DRY_RUN"], "true")
        self.assertEqual(environment["BATCH_SIZE"], "5")

    def test_rejects_secret_missing_for_auth_method(self):
        path = self.write_config({"accounts": [
            {"name": "oauth", "environment": {"IMAP_AUTH_METHOD": "xoauth2"}},
        ]})
        with self.assertRaisesRegex(RuntimeError, "IMAP_OAUTH_ACCESS_TOKEN_FILE"):
            load_accounts(path)

    def test_state_and_identity_are_isolated(self):
        with patch.dict(os.environ, {"STATE_DIR": "/state", "SHARED": "yes"}, clear=True):
            environment = account_environment("abc123", {"IMAP_HOST": "mail.example"})
        self.assertEqual(environment["STATE_DIR"], "/state/accounts/abc123")
        self.assertEqual(environment["MAIL_SENTINEL_ACCOUNT_ID"], "abc123")
        self.assertEqual(environment["SHARED"], "yes")

    def test_account_identifier_is_stable_and_anonymous(self):
        self.assertEqual(account_id("private@example.com"), account_id("private@example.com"))
        self.assertRegex(account_id("private@example.com"), r"^[0-9a-f]{12}$")


if __name__ == "__main__":
    unittest.main()
