#!/usr/bin/env python3
"""Capability-driven Mail Sentinel polling worker and startup diagnostics."""

from __future__ import annotations

import argparse
import email
import fcntl
import hashlib
import imaplib
import json
import os
import re
import sqlite3
import subprocess
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mail_sentinel_imap import CapabilityIMAP, MailboxInfo
from mail_sentinel_move import SafeMover
from mail_sentinel_operations import Notifier, OperationsState


STATE_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS processed_messages (
  folder TEXT NOT NULL,
  uidvalidity INTEGER NOT NULL,
  uid INTEGER NOT NULL,
  classification TEXT NOT NULL,
  processed_at TEXT NOT NULL,
  PRIMARY KEY(folder, uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS learning_messages (
  folder TEXT NOT NULL,
  uidvalidity INTEGER NOT NULL,
  uid INTEGER NOT NULL,
  learn_type TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('learned','moved')),
  updated_at TEXT NOT NULL,
  PRIMARY KEY(folder, uidvalidity, uid, learn_type)
);
"""


class WorkerState:
    def __init__(self) -> None:
        root = Path(os.environ.get("STATE_DIR", "/var/lib/mail-sentinel-state"))
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "worker-state.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(STATE_SCHEMA)
        self.operations = OperationsState(self.db)
        self.notifier = Notifier(self.operations)

    def close(self) -> None:
        self.db.close()

    def processed(self, folder: str, uidvalidity: int, uids: list[int]) -> set[int]:
        if not uids:
            return set()
        placeholders = ",".join("?" for _ in uids)
        rows = self.db.execute(
            f"SELECT uid FROM processed_messages WHERE folder=? AND uidvalidity=? AND uid IN ({placeholders})",
            (folder, uidvalidity, *uids),
        )
        return {int(row[0]) for row in rows}

    def mark_processed(self, folder: str, uidvalidity: int, uid: int, classification: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO processed_messages VALUES (?,?,?,?,?)",
            (folder, uidvalidity, uid, classification, utc_now()),
        )
        self.db.commit()

    def learning_status(self, folder: str, uidvalidity: int, uid: int, learn_type: str) -> str | None:
        row = self.db.execute(
            "SELECT status FROM learning_messages WHERE folder=? AND uidvalidity=? AND uid=? AND learn_type=?",
            (folder, uidvalidity, uid, learn_type),
        ).fetchone()
        return str(row[0]) if row else None

    def mark_learning(self, folder: str, uidvalidity: int, uid: int,
                      learn_type: str, status: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO learning_messages VALUES (?,?,?,?,?,?)",
            (folder, uidvalidity, uid, learn_type, status, utc_now()),
        )
        self.db.commit()

    def failure(self, event_code: str, error: Exception) -> None:
        self.operations.increment(f"{event_code}_count")
        if self.operations.record_failure(event_code, type(error).__name__):
            try:
                self.notifier.send(event_code)
            except Exception as notify_error:
                self.operations.increment("notification_failed_count")
                log("warn", "notification_failed", event_code=event_code,
                    error_type=type(notify_error).__name__)

    def success(self, event_code: str) -> None:
        if self.operations.record_success(event_code):
            try:
                self.notifier.send(event_code, recovery=True)
            except Exception as notify_error:
                self.operations.increment("notification_failed_count")
                log("warn", "notification_failed", event_code=event_code, recovery=True,
                    error_type=type(notify_error).__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def boolean(name: str) -> bool:
    value = required(name).lower()
    if value not in ("true", "false"):
        raise RuntimeError(f"{name} must be true or false")
    return value == "true"


def positive(name: str) -> int:
    value = int(required(name))
    if value < 1:
        raise RuntimeError(f"{name} must be positive")
    return value


def log(level: str, event: str, **fields: object) -> None:
    account_id = os.environ.get("MAIL_SENTINEL_ACCOUNT_ID")
    print(json.dumps({
        "timestamp": utc_now(),
        "level": level, "event": event,
        **({"account_id": account_id} if account_id else {}), **fields,
    }, ensure_ascii=False, separators=(",", ":")), flush=True)


def secret(path_variable: str, description: str) -> str:
    value = Path(required(path_variable)).read_text(encoding="utf-8").rstrip("\r\n")
    if not value:
        raise RuntimeError(f"{description} secret is empty")
    return value


def authenticate(connection: imaplib.IMAP4) -> None:
    method = os.environ.get("IMAP_AUTH_METHOD", "password")
    username = required("IMAP_USERNAME")
    if method in ("password", "app_password"):
        connection.login(username, secret("IMAP_PASSWORD_FILE", "IMAP password"))
        return
    if method == "xoauth2":
        token = secret("IMAP_OAUTH_ACCESS_TOKEN_FILE", "OAuth access token")
        connection.authenticate("XOAUTH2", lambda _: f"user={username}\x01auth=Bearer {token}\x01\x01".encode())
        return
    raise RuntimeError("IMAP_AUTH_METHOD must be password, app_password, or xoauth2")


def connect() -> tuple[imaplib.IMAP4, CapabilityIMAP]:
    host, port, mode = required("IMAP_HOST"), int(required("IMAP_PORT")), required("IMAP_TLS_MODE")
    timeout = positive("IMAP_TIMEOUT_SECONDS")
    if mode == "implicit":
        connection = imaplib.IMAP4_SSL(host, port, timeout=timeout)
    else:
        connection = imaplib.IMAP4(host, port, timeout=timeout)
        if mode == "starttls":
            connection.starttls()
        elif mode != "none":
            raise RuntimeError("IMAP_TLS_MODE must be implicit, starttls, or none")
    authenticate(connection)
    return connection, CapabilityIMAP(connection)


def select(connection: imaplib.IMAP4, mailbox: MailboxInfo, readonly: bool = False) -> None:
    status, _ = connection.select(mailbox.wire_name, readonly=readonly)
    if status != "OK":
        raise RuntimeError(f"cannot select IMAP folder: {mailbox.name}")


def uidvalidity(connection: imaplib.IMAP4) -> int:
    response = connection.response("UIDVALIDITY")[1]
    if not response or not response[0]:
        raise RuntimeError("IMAP server did not report UIDVALIDITY")
    return int(response[0])


def supports_keywords(connection: imaplib.IMAP4) -> bool:
    response = connection.response("PERMANENTFLAGS")[1]
    if not response or not response[0]:
        return False
    return b"\\*" in response[0].strip().strip(b"()").split()


def processed_method(connection: imaplib.IMAP4) -> str:
    configured = required("PROCESSED_STATE")
    if configured not in ("auto", "imap_keyword", "local_database"):
        raise RuntimeError("PROCESSED_STATE must be auto, imap_keyword, or local_database")
    available = supports_keywords(connection)
    if configured == "imap_keyword" and not available:
        raise RuntimeError("IMAP server does not support user-defined keywords")
    return "imap_keyword" if configured == "imap_keyword" or (configured == "auto" and available) else "local_database"


def search(connection: imaplib.IMAP4, *criteria: str) -> list[int]:
    status, data = connection.uid("SEARCH", None, *criteria)
    if status != "OK":
        raise RuntimeError("IMAP UID SEARCH failed")
    return [int(value) for value in (data[0] or b"").split()]


def fetch_message(connection: imaplib.IMAP4, uid: int) -> bytes:
    status, data = connection.uid("FETCH", str(uid), "(UID RFC822.SIZE BODY.PEEK[])")
    if status != "OK" or not data or not isinstance(data[0], tuple):
        raise RuntimeError(f"IMAP message fetch failed for UID {uid}")
    raw = data[0][1]
    if len(raw) > positive("SPAMC_MAX_SIZE_BYTES"):
        raise RuntimeError("message_too_large")
    return raw


def spamc(args: list[str], raw: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([
        "spamc", "-x", "-d", os.environ.get("SPAMD_HOST", "spamassassin"),
        "-p", os.environ.get("SPAMD_PORT", "783"), "-s", required("SPAMC_MAX_SIZE_BYTES"), *args,
    ], input=raw, capture_output=True, check=False)


def score(raw: bytes) -> tuple[float, float]:
    result = spamc(["-c"], raw)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"SpamAssassin scan failed with exit {result.returncode}")
    match = re.search(rb"(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)", result.stdout)
    if not match:
        raise RuntimeError("cannot parse SpamAssassin score")
    return float(match.group(1)), float(match.group(2))


def learn(raw: bytes, learn_type: str) -> None:
    result = spamc(["-L", learn_type], raw)
    if result.returncode != 0:
        raise RuntimeError(f"SpamAssassin learning failed with exit {result.returncode}")


def add_flags(connection: imaplib.IMAP4, uid: int, flags: list[str]) -> None:
    status, _ = connection.uid("STORE", str(uid), "+FLAGS.SILENT", "(" + " ".join(flags) + ")")
    if status != "OK":
        raise RuntimeError(f"failed to add flags for UID {uid}")


def move(connection: imaplib.IMAP4, client: CapabilityIMAP, state: WorkerState,
         source: MailboxInfo, generation: int, uid: int, destination: MailboxInfo) -> int | None:
    return SafeMover(connection, client, state.db).move(
        source, generation, uid, destination, os.environ.get("IMAP_MOVE_FALLBACK", "disabled")
    )


def resolve_or_create(connection: imaplib.IMAP4, client: CapabilityIMAP, name: str) -> MailboxInfo:
    try:
        return client.resolve(name)
    except RuntimeError:
        if not boolean("CREATE_MISSING_FOLDERS"):
            raise
        status, _ = connection.create(client.argument(name))
        if status != "OK":
            raise RuntimeError(f"failed to create IMAP folder: {name}")
        log("info", "folder_created")
        return client.resolve(name)


def process_learning(connection: imaplib.IMAP4, client: CapabilityIMAP, state: WorkerState,
                     source: MailboxInfo, destination: MailboxInfo, learn_type: str) -> None:
    select(connection, source)
    generation = uidvalidity(connection)
    method = processed_method(connection)
    learned_flag = required("LEARNED_FLAG")
    processed_flag = required("PROCESSED_FLAG")
    limit = positive("LEARNING_BATCH_SIZE")
    dry_run = boolean("DRY_RUN")
    processed = learned = resumed = failed = 0
    all_uids = search(connection, "ALL")
    resumed_uids = (
        search(connection, "KEYWORD", learned_flag)
        if method == "imap_keyword" else
        [uid for uid in all_uids if state.learning_status(source.name, generation, uid, learn_type) == "learned"]
    )
    for uid in resumed_uids[:limit]:
        try:
            if not dry_run:
                move(connection, client, state, source, generation, uid, destination)
                if method == "local_database":
                    state.mark_learning(source.name, generation, uid, learn_type, "moved")
            resumed += 1
        except Exception as error:
            failed += 1
            state.failure("learning_move_failed", error)
            log("warn", "learning_move_failed", uid=uid, learning_type=learn_type,
                error_type=type(error).__name__)
        processed += 1
    remaining = limit - processed
    pending_uids = (
        search(connection, "UNKEYWORD", learned_flag)
        if method == "imap_keyword" else
        [uid for uid in all_uids if state.learning_status(source.name, generation, uid, learn_type) is None]
    )
    state.operations.set_status(f"learning_{learn_type}_pending_count", len(pending_uids))
    for uid in pending_uids[:remaining]:
        try:
            raw = fetch_message(connection, uid)
            if not dry_run:
                learn(raw, learn_type)
                if method == "imap_keyword":
                    flags = [learned_flag] + ([processed_flag] if learn_type == "ham" else [])
                    add_flags(connection, uid, flags)
                else:
                    state.mark_learning(source.name, generation, uid, learn_type, "learned")
                move(connection, client, state, source, generation, uid, destination)
                if method == "local_database":
                    state.mark_learning(source.name, generation, uid, learn_type, "moved")
            learned += 1
            if not dry_run:
                state.operations.increment(f"learning_{learn_type}_success_count")
                state.operations.set_status("last_learning_success", {"type": learn_type})
                state.success("learning_failed")
            log("info", "learning_succeeded" if not dry_run else "learning_planned",
                uid=uid, learning_type=learn_type, dry_run=dry_run)
        except Exception as error:
            failed += 1
            state.failure("learning_failed", error)
            log("warn", "learning_failed", uid=uid, learning_type=learn_type,
                error_type=type(error).__name__, retry=True)
        processed += 1
    log("info", "learning_scan_complete", learning_type=learn_type, processed=processed,
        learned=learned, resumed=resumed, failed=failed, dry_run=dry_run)


def scan_once() -> None:
    state = WorkerState()
    connection = None
    try:
        try:
            connection, client = connect()
            state.operations.set_status("last_imap_success", True)
            state.success("imap_connection_failed")
        except Exception as error:
            state.failure("imap_connection_failed", error)
            raise
        inbox = client.resolve(required("IMAP_INBOX"))
        junk = client.resolve(required("IMAP_JUNK"), "\\junk")
        if boolean("LEARNING_ENABLED"):
            ham_source = resolve_or_create(connection, client, required("IMAP_LEARN_HAM"))
            spam_source = resolve_or_create(connection, client, required("IMAP_LEARN_SPAM"))
            process_learning(connection, client, state, ham_source, inbox, "ham")
            process_learning(connection, client, state, spam_source, junk, "spam")
        select(connection, inbox)
        generation = uidvalidity(connection)
        method = processed_method(connection)
        since = (datetime.now(timezone.utc) - timedelta(days=int(required("LOOKBACK_DAYS")) + 1)).strftime("%d-%b-%Y")
        if method == "imap_keyword":
            candidates = search(connection, "UNKEYWORD", required("PROCESSED_FLAG"), "SINCE", since)
        else:
            candidates = search(connection, "SINCE", since)
            already_processed = state.processed(inbox.name, generation, candidates)
            candidates = [uid for uid in candidates if uid not in already_processed]
        state.operations.set_status("pending_message_count", len(candidates))
        backlog_threshold = int(os.environ.get("BACKLOG_MESSAGE_THRESHOLD", "100"))
        if backlog_threshold < 1:
            raise RuntimeError("BACKLOG_MESSAGE_THRESHOLD must be positive")
        if len(candidates) >= backlog_threshold:
            state.failure("processing_backlog", RuntimeError("pending message threshold exceeded"))
        else:
            state.success("processing_backlog")
        dry_run = boolean("DRY_RUN")
        counts = {"processed": 0, "spam": 0, "ham": 0, "failed": 0}
        for uid in candidates[:positive("BATCH_SIZE")]:
            try:
                raw = fetch_message(connection, uid)
                try:
                    value, threshold = score(raw)
                    state.operations.set_status("last_spamassassin_success", True)
                    state.success("spamassassin_failed")
                except Exception as error:
                    state.failure("spamassassin_failed", error)
                    raise
                classification = "spam" if value >= threshold else "ham"
                if not dry_run:
                    if classification == "spam":
                        try:
                            move(connection, client, state, inbox, generation, uid, junk)
                            state.success("message_move_failed")
                        except Exception as error:
                            state.failure("message_move_failed", error)
                            raise
                    elif method == "imap_keyword":
                        add_flags(connection, uid, [required("PROCESSED_FLAG")])
                    if method == "local_database":
                        state.mark_processed(inbox.name, generation, uid, classification)
                counts[classification] += 1
                state.operations.increment("messages_inspected_count")
                state.operations.increment(f"messages_{classification}_count")
                log("info", "message_classified", uid=uid, classification=classification,
                    score=value, threshold=threshold, action="would_process" if dry_run else "processed",
                    move_required=classification == "spam", dry_run=dry_run)
            except Exception as error:
                counts["failed"] += 1
                state.operations.increment("message_failed_count")
                log("warn", "message_deferred", uid=uid, error_type=type(error).__name__, retry=True)
            counts["processed"] += 1
        log("info", "scan_complete", **counts, dry_run=dry_run)
        state.operations.set_status("last_scan_success", counts)
        state.operations.set_status("consecutive_scan_failures", 0)
        state.success("scan_failed")
    finally:
        state.close()
        if connection is not None:
            try:
                connection.logout()
            except imaplib.IMAP4.error:
                pass


def lock_paths() -> list[Path]:
    folders = {required("IMAP_INBOX")}
    if boolean("LEARNING_ENABLED"):
        folders.update((required("IMAP_LEARN_HAM"), required("IMAP_LEARN_SPAM")))
    root = Path(os.environ.get("STATE_DIR", "/var/lib/mail-sentinel-state")) / "locks"
    root.mkdir(parents=True, exist_ok=True)
    paths = []
    for folder in folders:
        identity = "\0".join((required("IMAP_HOST"), required("IMAP_PORT"), required("IMAP_USERNAME"), folder))
        paths.append(root / f"{hashlib.sha256(identity.encode()).hexdigest()}.lock")
    return sorted(paths)


def diagnose() -> int:
    connection, client = connect()
    try:
        log("info", "startup_diagnostic", check="imap_connection", result="pass",
            utf8_enabled=client.utf8_enabled)
        inbox = client.resolve(required("IMAP_INBOX"))
        junk = client.resolve(required("IMAP_JUNK"), "\\junk")
        log("info", "startup_diagnostic", check="inbox_folder", result="pass")
        log("info", "startup_diagnostic", check="junk_folder",
            source="configured" if junk.name == required("IMAP_JUNK") else "special_use",
            result="pass")
        # SELECT does not modify messages and is required here because some
        # servers omit writable PERMANENTFLAGS from read-only EXAMINE replies.
        select(connection, inbox)
        method = processed_method(connection)
        generation = uidvalidity(connection)
        if method == "local_database":
            state = WorkerState()
            state.close()
        log("info", "processed_state_selected", method=method,
            reason="configured" if required("PROCESSED_STATE") != "auto" else
            ("imap_keywords_available" if method == "imap_keyword" else "imap_keywords_unavailable"),
            uidvalidity=generation)
        diagnostic_state = WorkerState()
        try:
            move_plan = SafeMover(connection, client, diagnostic_state.db).plan(
                inbox, junk, os.environ.get("IMAP_MOVE_FALLBACK", "disabled")
            )
        finally:
            diagnostic_state.close()
        log("info", "imap_move_method_selected", method=move_plan.method, reason=move_plan.reason)
        if move_plan.method == "unsupported" and not boolean("DRY_RUN"):
            raise RuntimeError(f"safe IMAP move is unsupported: {move_plan.reason}")
        status, data = connection.status(inbox.wire_name, "(MESSAGES)")
        if status != "OK":
            raise RuntimeError("INBOX status request failed")
        match = re.search(rb"MESSAGES\s+(\d+)", data[0] or b"")
        log("info", "startup_diagnostic", check="inbox_read",
            message_count=int(match.group(1)) if match else -1, result="pass")
        result = spamc(["-c"], b"From: diagnostic@example.invalid\r\n\r\nConnectivity test.\r\n")
        if result.returncode not in (0, 1):
            raise RuntimeError("SpamAssassin connectivity test failed")
        log("info", "startup_diagnostic", check="spamassassin_connection", result="pass")
        log("info", "startup_diagnostic_complete", result="pass")
        return 0
    finally:
        try:
            connection.logout()
        except imaplib.IMAP4.error:
            pass


def setup() -> int:
    connection, client = connect()
    try:
        client.resolve(required("IMAP_INBOX"))
        resolve_or_create(connection, client, required("IMAP_JUNK"))
        resolve_or_create(connection, client, required("IMAP_LEARN_HAM"))
        resolve_or_create(connection, client, required("IMAP_LEARN_SPAM"))
        log("info", "mailbox_setup_complete", result="pass")
        return 0
    finally:
        try:
            connection.logout()
        except imaplib.IMAP4.error:
            pass


def run() -> int:
    retry = positive("RETRY_INITIAL_SECONDS")
    retry_max = positive("RETRY_MAX_SECONDS")
    paths = lock_paths()
    state = WorkerState()
    try:
        configuration = {
            key: value for key, value in os.environ.items()
            if key.startswith(("IMAP_", "POLL_", "BATCH_", "LEARNING_", "RETRY_", "NOTIFICATION_"))
            and not any(secret_name in key for secret_name in ("PASSWORD", "TOKEN", "URL_FILE"))
        }
        fingerprint = hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()
        state.operations.audit("worker_configuration_loaded", "success", fingerprint=fingerprint)
    finally:
        state.close()
    log("info", "worker_started", poll_interval_seconds=positive("POLL_INTERVAL_SECONDS"))
    while True:
        try:
            with ExitStack() as stack:
                files = [stack.enter_context(path.open("a+")) for path in paths]
                for file in files:
                    fcntl.flock(file, fcntl.LOCK_EX)
                scan_once()
            retry = positive("RETRY_INITIAL_SECONDS")
            time.sleep(positive("POLL_INTERVAL_SECONDS"))
        except Exception as error:
            state = WorkerState()
            try:
                snapshot = state.operations.snapshot()
                current = snapshot["status"].get("consecutive_scan_failures", {}).get("value", 0)
                state.operations.set_status("consecutive_scan_failures", int(current) + 1)
                state.operations.set_status("next_retry_at", (
                    datetime.now(timezone.utc) + timedelta(seconds=retry)
                ).isoformat().replace("+00:00", "Z"))
                state.operations.increment("retry_count")
                state.failure("scan_failed", error)
            finally:
                state.close()
            log("error", "scan_failed", error_type=type(error).__name__, retry_in_seconds=retry)
            time.sleep(retry)
            retry = min(retry * 2, retry_max)


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--diagnose", action="store_true")
    mode.add_argument("--setup", action="store_true")
    args = parser.parse_args()
    try:
        if args.diagnose:
            return diagnose()
        if args.setup:
            return setup()
        diagnosis = diagnose()
        if diagnosis != 0:
            return diagnosis
        return run()
    except Exception as error:
        log("error", "startup_diagnostic", result="fail", error_type=type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
