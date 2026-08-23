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
import subprocess
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mail_sentinel_imap import CapabilityIMAP, MailboxInfo


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
    print(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "level": level, "event": event, **fields,
    }, ensure_ascii=False, separators=(",", ":")), flush=True)


def password() -> str:
    value = Path(required("IMAP_PASSWORD_FILE")).read_text(encoding="utf-8").rstrip("\r\n")
    if not value:
        raise RuntimeError("IMAP password secret is empty")
    return value


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
    connection.login(required("IMAP_USERNAME"), password())
    return connection, CapabilityIMAP(connection)


def select(connection: imaplib.IMAP4, mailbox: MailboxInfo, readonly: bool = False) -> None:
    status, _ = connection.select(mailbox.wire_name, readonly=readonly)
    if status != "OK":
        raise RuntimeError(f"cannot select IMAP folder: {mailbox.name}")


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


def move(connection: imaplib.IMAP4, client: CapabilityIMAP, uid: int, destination: MailboxInfo) -> None:
    if "MOVE" not in client.capabilities:
        raise RuntimeError("IMAP server does not support MOVE; unsafe fallback is disabled")
    status, _ = connection.uid("MOVE", str(uid), destination.wire_name)
    if status != "OK":
        raise RuntimeError(f"IMAP MOVE failed for UID {uid}")


def resolve_or_create(connection: imaplib.IMAP4, client: CapabilityIMAP, name: str) -> MailboxInfo:
    try:
        return client.resolve(name)
    except RuntimeError:
        if not boolean("CREATE_MISSING_FOLDERS"):
            raise
        status, _ = connection.create(client.argument(name))
        if status != "OK":
            raise RuntimeError(f"failed to create IMAP folder: {name}")
        log("info", "folder_created", folder=name)
        return client.resolve(name)


def process_learning(connection: imaplib.IMAP4, client: CapabilityIMAP, source: MailboxInfo,
                     destination: MailboxInfo, learn_type: str) -> None:
    select(connection, source)
    learned_flag = required("LEARNED_FLAG")
    processed_flag = required("PROCESSED_FLAG")
    limit = positive("LEARNING_BATCH_SIZE")
    dry_run = boolean("DRY_RUN")
    processed = learned = resumed = failed = 0
    for uid in search(connection, "KEYWORD", learned_flag)[:limit]:
        try:
            if not dry_run:
                move(connection, client, uid, destination)
            resumed += 1
        except Exception as error:
            failed += 1
            log("warn", "learning_move_failed", uid=uid, learning_type=learn_type, error=str(error))
        processed += 1
    remaining = limit - processed
    for uid in search(connection, "UNKEYWORD", learned_flag)[:remaining]:
        try:
            raw = fetch_message(connection, uid)
            if not dry_run:
                learn(raw, learn_type)
                flags = [learned_flag] + ([processed_flag] if learn_type == "ham" else [])
                add_flags(connection, uid, flags)
                move(connection, client, uid, destination)
            learned += 1
            log("info", "learning_succeeded" if not dry_run else "learning_planned",
                uid=uid, learning_type=learn_type, dry_run=dry_run)
        except Exception as error:
            failed += 1
            log("warn", "learning_failed", uid=uid, learning_type=learn_type, error=str(error), retry=True)
        processed += 1
    log("info", "learning_scan_complete", learning_type=learn_type, processed=processed,
        learned=learned, resumed=resumed, failed=failed, dry_run=dry_run)


def scan_once() -> None:
    connection, client = connect()
    try:
        inbox = client.resolve(required("IMAP_INBOX"))
        junk = client.resolve(required("IMAP_JUNK"), "\\junk")
        if boolean("LEARNING_ENABLED"):
            ham_source = resolve_or_create(connection, client, required("IMAP_LEARN_HAM"))
            spam_source = resolve_or_create(connection, client, required("IMAP_LEARN_SPAM"))
            process_learning(connection, client, ham_source, inbox, "ham")
            process_learning(connection, client, spam_source, junk, "spam")
        select(connection, inbox)
        since = (datetime.now(timezone.utc) - timedelta(days=int(required("LOOKBACK_DAYS")) + 1)).strftime("%d-%b-%Y")
        candidates = search(connection, "UNKEYWORD", required("PROCESSED_FLAG"), "SINCE", since)
        dry_run = boolean("DRY_RUN")
        counts = {"processed": 0, "spam": 0, "ham": 0, "failed": 0}
        for uid in candidates[:positive("BATCH_SIZE")]:
            try:
                raw = fetch_message(connection, uid)
                value, threshold = score(raw)
                classification = "spam" if value >= threshold else "ham"
                if not dry_run:
                    if classification == "spam":
                        move(connection, client, uid, junk)
                    else:
                        add_flags(connection, uid, [required("PROCESSED_FLAG")])
                counts[classification] += 1
                log("info", "message_classified", uid=uid, classification=classification,
                    score=value, threshold=threshold, action="would_process" if dry_run else "processed",
                    destination=junk.name if classification == "spam" else None, dry_run=dry_run)
            except Exception as error:
                counts["failed"] += 1
                log("warn", "message_deferred", uid=uid, error=str(error), retry=True)
            counts["processed"] += 1
        log("info", "scan_complete", **counts, dry_run=dry_run)
    finally:
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
        log("info", "startup_diagnostic", check="inbox_folder", folder=inbox.name, result="pass")
        log("info", "startup_diagnostic", check="junk_folder", folder=junk.name,
            source="special_use" if "\\junk" in junk.flags else "configured", result="pass")
        select(connection, inbox, readonly=True)
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
            log("error", "scan_failed", error=str(error), retry_in_seconds=retry)
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
        log("error", "startup_diagnostic", result="fail", error=str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
