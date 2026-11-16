"""認証結果の取得、共通判定、workerとの連携を検証する。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_oversized_mail import FakeIMAP, mailbox, worker
from mail_sentinel_authentication import AuthenticationSettings


PROVIDERS = ("mx.provider-a.example", "inbound.provider-b.example")
FIXTURE = Path(__file__).parent / "fixtures" / "authentication-forged.eml"


def config(server=PROVIDERS[0], **overrides):
    return {
        "AUTH_SOURCE": "authentication_results",
        "AUTH_AUTHSERV_ID": server,
        "AUTH_TRUST_BOUNDARY": "verified_scrubbed_ingress",
        "IMAP_TLS_MODE": "implicit",
        **overrides,
    }


def message(server, *results):
    headers = "\r\n".join(f"Authentication-Results: {server}; {result}" for result in results)
    return (f"{headers}\r\nFrom: sender@example.invalid\r\n\r\nSynthetic body.\r\n").encode()


class AuthenticationTests(unittest.TestCase):
    def test_same_policy_for_two_provider_profiles(self):
        cases = [
            ("pass", "pass", False, 0.0, None),
            ("fail", "pass", False, 0.5, "AUTH_SPF_FAIL_SCORE"),
            ("pass", "fail", False, 0.5, "AUTH_DMARC_FAIL_SCORE"),
            ("fail", "fail", True, 0.0, "AUTH_SPF_DMARC_FAILURE"),
            ("fail", "none", False, 0.5, "AUTH_SPF_FAIL_SCORE"),
            ("none", "fail", False, 0.5, "AUTH_DMARC_FAIL_SCORE"),
            ("none", "none", False, 0.0, None),
            ("softfail", "neutral", False, 0.0, None),
            ("permerror", "temperror", False, 0.0, None),
            ("temperror", "fail", False, 0.5, "AUTH_DMARC_FAIL_SCORE"),
        ]
        for server in PROVIDERS:
            settings = AuthenticationSettings.from_environment(config(server))
            for spf, dmarc, *expected in cases:
                with self.subTest(server=server, spf=spf, dmarc=dmarc):
                    if server == PROVIDERS[0]:
                        raw = message(server, f"spf={spf}; dmarc={dmarc}; dkim=pass")
                    else:
                        raw = message(f'"{server.upper()}" 1',
                                      f" DmArC={dmarc.upper()};\r\n\tDKIM=PASS; SPF={spf.upper()}")
                    evidence = settings.read(raw)
                    self.assertTrue(evidence.trusted)
                    self.assertEqual((evidence.spf, evidence.dmarc, evidence.dkim), (spf, dmarc, "pass"))
                    self.assertEqual(settings.policy.evaluate(evidence), tuple(expected))

    def test_distinguishes_missing_none_and_unrecognized(self):
        server = PROVIDERS[0]
        settings = AuthenticationSettings.from_environment(config())
        for text, expected in (("spf=none", "none"), ("dkim=none", None), ("spf=unknown", None)):
            with self.subTest(text=text):
                evidence = settings.read(message(server, text))
                self.assertEqual(evidence.spf, expected)
                self.assertEqual(settings.policy.evaluate(evidence), (False, 0.0, None))
        self.assertEqual(settings.read(message(server, "none")).reason, "verified_ingress")
        self.assertEqual(settings.read(b"From: sender@example.invalid\r\n\r\nBody.").reason,
                         "missing_authentication_results")

    def test_quoted_delimiter_nested_comment_and_multiple_dkim(self):
        settings = AuthenticationSettings.from_environment(config())
        raw = message(PROVIDERS[0], 'spf=fail (first (nested; note)); dmarc=pass reason="a; b"; '
                                    "dkim=pass header.d=a.example; dkim=fail header.d=b.example")
        evidence = settings.read(raw)
        self.assertEqual((evidence.spf, evidence.dmarc, evidence.dkim), ("fail", "pass", None))
        self.assertEqual(settings.policy.evaluate(evidence), (False, 0.5, "AUTH_SPF_FAIL_SCORE"))

    def test_unscrubbed_duplicate_and_conflicting_headers_never_force(self):
        server = PROVIDERS[0]
        settings = AuthenticationSettings.from_environment(config())
        forged_fail = message(server, "spf=pass; dmarc=pass", "spf=fail; dmarc=fail")
        conflicting = message(server, "spf=pass; spf=fail; dmarc=fail")
        for raw in (FIXTURE.read_bytes(), forged_fail, conflicting):
            with self.subTest(raw_length=len(raw)):
                evidence = settings.read(raw)
                self.assertEqual(evidence.reason, "ambiguous_results")
                self.assertFalse(evidence.trusted)
                self.assertEqual(settings.policy.evaluate(evidence), (False, 0.0, None))

    def test_forged_trace_or_other_id_cannot_override_verified_results(self):
        settings = AuthenticationSettings.from_environment(config())
        original = message(PROVIDERS[0], "spf=fail; dmarc=fail")
        trace = (b"Received: from attacker.example by mx.provider-a.example\r\n"
                 b"Received-SPF: pass\r\n"
                 b"ARC-Authentication-Results: i=1; mx.provider-a.example; spf=pass; dmarc=pass\r\n"
                 b"Authentication-Results: attacker.example; spf=pass; dmarc=pass\r\n")
        self.assertEqual(settings.read(trace + original).spf, "fail")
        self.assertEqual(settings.read(trace + b"From: attacker@example.invalid\r\n\r\nBody.").reason,
                         "missing_authentication_results")

    def test_disabled_unverified_or_invalid_settings_use_normal_scan(self):
        raw = message(PROVIDERS[0], "spf=fail; dmarc=fail")
        for env, expected in (
            ({}, "disabled"),
            (config(AUTH_SOURCE="unsupported"), "unsupported_source"),
            (config(AUTH_TRUST_BOUNDARY=""), "boundary_unverified"),
            (config(IMAP_TLS_MODE="none"), "imap_tls_required"),
            (config(""), "invalid_authserv_id"),
            (config(AUTH_SPF_FAIL_SCORE="NaN"), "invalid_authentication_scores"),
        ):
            with self.subTest(expected=expected):
                settings = AuthenticationSettings.from_environment(env)
                evidence = settings.read(raw)
                self.assertEqual(evidence.reason, expected)
                self.assertEqual(settings.policy.evaluate(evidence), (False, 0.0, None))

    def test_malformed_selected_result_uses_normal_scan(self):
        settings = AuthenticationSettings.from_environment(config())
        evidence = settings.read(message(PROVIDERS[0], 'spf=fail; dmarc=fail reason="unclosed'))
        self.assertEqual(evidence.reason, "malformed_authentication_results")
        self.assertEqual(settings.policy.evaluate(evidence), (False, 0.0, None))

    def test_configurable_scores(self):
        settings = AuthenticationSettings.from_environment(
            config(AUTH_SPF_FAIL_SCORE="0", AUTH_DMARC_FAIL_SCORE="0.75"))
        for result, adjustment in (("spf=fail; dmarc=none", 0.0), ("spf=none; dmarc=fail", 0.75)):
            evidence = settings.read(message(PROVIDERS[0], result))
            self.assertEqual(settings.policy.evaluate(evidence)[1], adjustment)

    def test_worker_routes_once_and_logs_both_decisions(self):
        server = PROVIDERS[0]
        messages = {
            1: message(server, "spf=fail; dmarc=fail"),
            2: FIXTURE.read_bytes(),
            3: message(server, "spf=fail; dmarc=pass"),
            4: message(server, "spf=pass; dmarc=fail"),
            5: message(server, "spf=pass; dmarc=pass"),
        }
        connection = FakeIMAP(messages, keywords=False)
        client = Mock()
        client.resolve.side_effect = lambda name, *args: mailbox(name)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            **config(), "STATE_DIR": directory, "SPAMC_MAX_SIZE_BYTES": "10000", "DRY_RUN": "false",
            "IMAP_INBOX": "INBOX", "IMAP_JUNK": "Junk", "LEARNING_ENABLED": "false",
            "LOOKBACK_DAYS": "1", "BATCH_SIZE": "10", "PROCESSED_STATE": "auto",
            "NOTIFICATION_ENABLED": "false",
        }, clear=True), patch.object(worker, "connect", return_value=(connection, client)), \
                patch.object(worker, "score", side_effect=[(-4.0, 5.0), (3.8, 5.0), (3.8, 5.0),
                                                          (4.7, 5.0), (6.0, 5.0)]) as score, \
                patch.object(worker, "move") as move, patch.object(worker, "log") as log:
            worker.scan_once()
            worker.scan_once()
        self.assertEqual(score.call_count, len(messages))
        self.assertEqual([call.args[-2:] for call in move.call_args_list],
                         [(1, mailbox("Junk")), (4, mailbox("Junk")), (5, mailbox("Junk"))])
        logged = [call.kwargs for call in log.call_args_list if call.args[1] == "message_classified"]
        self.assertEqual(
            [(r["spamassassin_classification"], r["classification"], r["classification_reason"],
              r["authentication_score_adjustment"]) for r in logged],
            [("ham", "spam", "AUTH_SPF_DMARC_FAILURE", 0.0), ("ham", "ham", "BELOW_THRESHOLD", 0.0),
             ("ham", "ham", "BELOW_THRESHOLD", 0.5), ("ham", "spam", "AUTH_DMARC_FAIL_SCORE", 0.5),
             ("spam", "spam", "SPAMASSASSIN", 0.0)],
        )
