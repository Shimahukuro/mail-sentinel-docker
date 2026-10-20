"""Exercise real TLS handshakes at both application authentication boundaries."""

import importlib.util
import os
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "python"))

import mail_sentinel_imap


def load_application(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = load_application("tls_worker", "worker/worker.py")
admin = load_application("tls_admin", "admin/admin.py")


class IMAPPeer:
    """A single-use loopback peer; records whether credentials reached it."""

    def __init__(self, mode, certificate, key, refuse_starttls=False):
        self.mode = mode
        self.refuse_starttls = refuse_starttls
        self.authenticated = False
        self.errors = []
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(certificate, key)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(5)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def serve(self):
        connection = None
        stream = None
        try:
            connection, _ = self.listener.accept()
            connection.settimeout(5)
            if self.mode == "implicit":
                connection = self.context.wrap_socket(connection, server_side=True)
            connection.sendall(b"* OK test peer\r\n")
            stream = connection.makefile("rb")
            while line := stream.readline():
                tag, command, *_ = line.decode("ascii").strip().split()
                command = command.upper()
                if command == "CAPABILITY":
                    connection.sendall(b"* CAPABILITY IMAP4rev1 STARTTLS AUTH=XOAUTH2\r\n")
                elif command == "STARTTLS":
                    if self.refuse_starttls:
                        connection.sendall(f"{tag} NO TLS unavailable\r\n".encode())
                        continue
                    connection.sendall(f"{tag} OK begin TLS\r\n".encode())
                    stream.close()
                    stream = None
                    connection = self.context.wrap_socket(connection, server_side=True)
                    stream = connection.makefile("rb")
                    continue
                elif command == "LOGIN":
                    self.authenticated = True
                elif command == "AUTHENTICATE":
                    connection.sendall(b"+ \r\n")
                    self.authenticated = bool(stream.readline())
                elif command == "LOGOUT":
                    connection.sendall(b"* BYE closing\r\n")
                    connection.sendall(f"{tag} OK logout\r\n".encode())
                    return
                else:
                    raise AssertionError(f"unexpected test command: {command}")
                connection.sendall(f"{tag} OK done\r\n".encode())
        except (ssl.SSLError, ConnectionError):
            # Certificate rejection closes the socket during the handshake.
            pass
        except Exception as error:
            self.errors.append(error)
        finally:
            if stream is not None:
                stream.close()
            if connection is not None:
                connection.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.thread.join(6)
        self.listener.close()
        if self.thread.is_alive():
            raise AssertionError("test peer did not stop")
        if self.errors:
            raise self.errors[0]


class TLSAuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        openssl = shutil.which("openssl")
        if openssl is None:
            raise RuntimeError("TLS regression tests require the openssl executable")

        def run(*arguments):
            subprocess.run([openssl, *arguments], cwd=cls.root, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-subj", "/CN=Mail Sentinel Test CA", "-keyout", "ca.key", "-out", "ca.pem",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        (cls.root / "extensions.cnf").write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost\n")
        run("req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
            "-keyout", "server.key", "-out", "server.csr")
        run("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
            "-set_serial", "1", "-days", "1", "-extfile", "extensions.cnf", "-out", "valid.pem")
        # Fixed historical dates avoid clock-dependent expiration tests.
        (cls.root / "index").touch()
        (cls.root / "serial").write_text("02\n")
        (cls.root / "ca.cnf").write_text(
            "[ca]\ndefault_ca=test\n[test]\ndatabase=index\nserial=serial\n"
            "new_certs_dir=.\ncertificate=ca.pem\nprivate_key=ca.key\n"
            "default_md=sha256\npolicy=names\nx509_extensions=server\n"
            "[names]\ncommonName=supplied\n[server]\n"
            "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost\n")
        run("ca", "-batch", "-config", "ca.cnf", "-in", "server.csr", "-out", "expired.pem",
            "-startdate", "20000101000000Z", "-enddate", "20000102000000Z", "-notext")
        (cls.root / "credential").write_text("synthetic-test-credential\n")

    @contextmanager
    def application(self, name, mode, port, trusted=True, hostname="localhost", oauth=False):
        environment = {
            "IMAP_HOST": hostname, "IMAP_PORT": str(port), "IMAP_TLS_MODE": mode,
            "IMAP_TIMEOUT_SECONDS": "3", "IMAP_USERNAME": "test@example.invalid",
            "IMAP_PASSWORD_FILE": str(self.root / "credential"),
            "IMAP_OAUTH_ACCESS_TOKEN_FILE": str(self.root / "credential"),
            "IMAP_AUTH_METHOD": "xoauth2" if oauth else "password",
        }
        if trusted:
            environment["IMAP_CA_FILE"] = str(self.root / "ca.pem")
        with patch.dict(os.environ, environment, clear=True):
            connection = None
            mailbox = None
            database = sqlite3.connect(":memory:") if name == "admin" else None
            try:
                if name == "worker":
                    connection, _ = worker.connect()
                else:
                    mailbox = admin.Mailbox(database)
                    connection = mailbox.imap
                yield connection
            finally:
                if mailbox is not None:
                    mailbox.close()
                elif connection is not None:
                    connection.logout()
                if database is not None:
                    database.close()

    def test_untrusted_peer_is_rejected_before_authentication(self):
        for name in ("worker", "admin"):
            for mode in ("implicit", "starttls"):
                with self.subTest(application=name, mode=mode):
                    with IMAPPeer(mode, self.root / "valid.pem", self.root / "server.key") as peer:
                        with self.assertRaises(ssl.SSLCertVerificationError):
                            with self.application(name, mode, peer.port, trusted=False):
                                pass
                    self.assertFalse(peer.authenticated)

    def test_custom_ca_accepts_valid_peer(self):
        for name in ("worker", "admin"):
            for mode in ("implicit", "starttls"):
                with self.subTest(application=name, mode=mode):
                    with IMAPPeer(mode, self.root / "valid.pem", self.root / "server.key") as peer:
                        with self.application(name, mode, peer.port):
                            pass
                    self.assertTrue(peer.authenticated)

    def test_hostname_mismatch_and_expired_certificate_are_rejected(self):
        for name in ("worker", "admin"):
            for mode in ("implicit", "starttls"):
                for certificate, hostname in (("valid.pem", "127.0.0.1"), ("expired.pem", "localhost")):
                    with self.subTest(application=name, mode=mode, certificate=certificate):
                        with IMAPPeer(mode, self.root / certificate, self.root / "server.key") as peer:
                            with self.assertRaises(ssl.SSLCertVerificationError):
                                with self.application(name, mode, peer.port, hostname=hostname):
                                    pass
                        self.assertFalse(peer.authenticated)

    def test_starttls_refusal_does_not_fall_back_or_authenticate(self):
        for name in ("worker", "admin"):
            with self.subTest(application=name):
                with IMAPPeer("starttls", self.root / "valid.pem", self.root / "server.key", True) as peer:
                    with self.assertRaises(worker.imaplib.IMAP4.error):
                        with self.application(name, "starttls", peer.port):
                            pass
                self.assertFalse(peer.authenticated)

    def test_worker_oauth_is_only_sent_to_verified_peer(self):
        for mode in ("implicit", "starttls"):
            for trusted in (False, True):
                with self.subTest(mode=mode, trusted=trusted):
                    with IMAPPeer(mode, self.root / "valid.pem", self.root / "server.key") as peer:
                        if trusted:
                            with self.application("worker", mode, peer.port, oauth=True):
                                pass
                        else:
                            with self.assertRaises(ssl.SSLCertVerificationError):
                                with self.application("worker", mode, peer.port, trusted=False, oauth=True):
                                    pass
                    self.assertEqual(peer.authenticated, trusted)

    def test_explicit_plaintext_mode_remains_available(self):
        for name in ("worker", "admin"):
            with self.subTest(application=name):
                with IMAPPeer("none", self.root / "valid.pem", self.root / "server.key") as peer:
                    with self.application(name, "none", peer.port, trusted=False):
                        pass
                self.assertTrue(peer.authenticated)

    def test_bad_ca_configuration_fails_before_network_access(self):
        malformed = self.root / "malformed.pem"
        malformed.write_text("not a certificate\n")
        for name in ("worker", "admin"):
            for mode in ("implicit", "starttls"):
                for ca in (self.root / "missing.pem", malformed):
                    with self.subTest(application=name, mode=mode, ca=ca.name):
                        with patch.object(mail_sentinel_imap.imaplib, "IMAP4_SSL") as implicit, \
                             patch.object(mail_sentinel_imap.imaplib, "IMAP4") as plaintext, \
                             closing(sqlite3.connect(":memory:")) as database, \
                             patch.dict(os.environ, {
                                 "IMAP_HOST": "localhost", "IMAP_PORT": "993", "IMAP_TLS_MODE": mode,
                                 "IMAP_TIMEOUT_SECONDS": "3", "IMAP_CA_FILE": str(ca),
                             }, clear=True):
                            with self.assertRaises((OSError, ssl.SSLError)):
                                worker.connect() if name == "worker" else admin.Mailbox(database)
                            implicit.assert_not_called()
                            plaintext.assert_not_called()


class TransportConfigurationTests(unittest.TestCase):
    def test_invalid_mode_or_timeout_opens_no_connection(self):
        with patch.object(mail_sentinel_imap.imaplib, "IMAP4_SSL") as implicit, \
             patch.object(mail_sentinel_imap.imaplib, "IMAP4") as plaintext:
            for mode, timeout in (("invalid", 3), ("implicit", 0), ("starttls", -1), ("none", 0)):
                with self.subTest(mode=mode, timeout=timeout), self.assertRaises(RuntimeError):
                    mail_sentinel_imap.connect_imap("localhost", 993, mode, timeout)
            implicit.assert_not_called()
            plaintext.assert_not_called()

    def test_tls_uses_system_roots_and_requires_certificate_and_hostname(self):
        default_context = ssl.create_default_context
        for mode in ("implicit", "starttls"):
            with self.subTest(mode=mode), \
                 patch.object(mail_sentinel_imap.ssl, "create_default_context", wraps=default_context) as factory, \
                 patch.object(mail_sentinel_imap.imaplib, "IMAP4_SSL") as implicit, \
                 patch.object(mail_sentinel_imap.imaplib, "IMAP4") as plaintext:
                mail_sentinel_imap.connect_imap("localhost", 993, mode, 3)
                factory.assert_called_once_with()
                call = implicit.call_args if mode == "implicit" else plaintext.return_value.starttls.call_args
                context = call.kwargs["ssl_context"]
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(context.check_hostname)
                self.assertGreater(context.cert_store_stats()["x509_ca"], 0)

    def test_plaintext_does_not_load_ca_or_attempt_tls(self):
        with patch.object(mail_sentinel_imap.ssl, "create_default_context") as factory, \
             patch.object(mail_sentinel_imap.imaplib, "IMAP4") as plaintext:
            connection = mail_sentinel_imap.connect_imap("localhost", 143, "none", 3, "/missing/ca.pem")
            self.assertIs(connection, plaintext.return_value)
            factory.assert_not_called()
            connection.starttls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
