#!/usr/bin/env python3
"""Explicit, resumable initial-learning and initial-scan administration jobs."""

from __future__ import annotations

import argparse
import email
import fcntl
import hashlib
import imaplib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path

from mail_sentinel_accounts import account_environment, configured_accounts
from mail_sentinel_imap import CapabilityIMAP
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  source_job_id TEXT REFERENCES jobs(job_id),
  job_type TEXT NOT NULL CHECK(job_type IN ('initial_learn','initial_scan')),
  mode TEXT NOT NULL CHECK(mode IN ('preview','apply')),
  status TEXT NOT NULL,
  account_key TEXT NOT NULL,
  folder TEXT NOT NULL,
  uidvalidity INTEGER NOT NULL,
  since_at TEXT,
  before_at TEXT,
  learn_type TEXT,
  max_messages INTEGER NOT NULL,
  max_moves INTEGER,
  batch_size INTEGER NOT NULL,
  threshold REAL,
  rule_set_id TEXT,
  confirmation_token TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  total_count INTEGER NOT NULL DEFAULT 0,
  processed_count INTEGER NOT NULL DEFAULT 0,
  success_count INTEGER NOT NULL DEFAULT 0,
  skipped_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  moved_count INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS one_apply_per_preview
  ON jobs(source_job_id) WHERE mode='apply';
CREATE TABLE IF NOT EXISTS message_results (
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  uidvalidity INTEGER NOT NULL,
  uid INTEGER NOT NULL,
  internaldate TEXT NOT NULL,
  digest TEXT,
  status TEXT NOT NULL,
  learn_type TEXT,
  score REAL,
  classification TEXT,
  rules TEXT,
  subject TEXT,
  sender TEXT,
  moved_to TEXT,
  destination_uid INTEGER,
  error TEXT,
  processed_at TEXT,
  PRIMARY KEY(job_id, uidvalidity, uid)
);
CREATE INDEX IF NOT EXISTS message_digest ON message_results(digest, learn_type, status);
CREATE INDEX IF NOT EXISTS message_scan_identity
  ON message_results(uidvalidity, uid, status);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def emit(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False, separators=(",", ":")), flush=True)


def required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def read_secret(path: str) -> str:
    value = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    if not value:
        raise RuntimeError("IMAP password secret is empty")
    return value


def parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("date-time must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def resolve_range(args: argparse.Namespace) -> tuple[datetime | None, datetime | None]:
    uses_dates = args.since_date is not None or args.through_date is not None
    uses_datetimes = args.since_datetime is not None or args.before_datetime is not None
    if uses_dates and uses_datetimes:
        raise RuntimeError("date and date-time range options cannot be mixed")
    if uses_dates:
        try:
            zone = ZoneInfo(args.timezone)
        except ZoneInfoNotFoundError as error:
            raise RuntimeError(f"unknown timezone: {args.timezone}") from error
        since = datetime.combine(date.fromisoformat(args.since_date), time.min, zone) if args.since_date else None
        before = (
            datetime.combine(date.fromisoformat(args.through_date) + timedelta(days=1), time.min, zone)
            if args.through_date else None
        )
        return (
            since.astimezone(timezone.utc) if since else None,
            before.astimezone(timezone.utc) if before else None,
        )
    return parse_datetime(args.since_datetime), parse_datetime(args.before_datetime)


def decode_text(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError):
        return value


def parse_internaldate(fetch_data: bytes) -> datetime:
    match = re.search(rb'INTERNALDATE "([^"]+)"', fetch_data)
    if not match:
        raise RuntimeError("IMAP response did not include INTERNALDATE")
    value = parsedate_to_datetime(match.group(1).decode("ascii"))
    return value.astimezone(timezone.utc)


def parse_score_report(output: str) -> tuple[float, list[str]]:
    match = re.search(r"(?m)^\s*(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)\s*$", output)
    if not match:
        match = re.search(r"(?im)score[=: ]+(-?\d+(?:\.\d+)?)", output)
    if not match:
        raise RuntimeError("cannot parse SpamAssassin score report")
    score = float(match.group(1))
    rules = []
    for line in output.splitlines():
        rule = re.match(r"^\s*-?\d+(?:\.\d+)?\s+([A-Z0-9_]{2,})\b", line)
        if rule:
            rules.append(rule.group(1))
    return score, sorted(set(rules))


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def job(self, job_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"job not found: {job_id}")
        return row

    def create_job(self, **values: object) -> str:
        job_id = str(uuid.uuid4())
        values = {"job_id": job_id, "created_at": utc_now(), "status": "running", **values}
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        self.db.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", tuple(values.values()))
        self.db.commit()
        return job_id


class Mailbox:
    def __init__(self):
        host = required_env("IMAP_HOST")
        port = int(required_env("IMAP_PORT"))
        timeout = int(required_env("IMAP_TIMEOUT_SECONDS"))
        mode = required_env("IMAP_TLS_MODE")
        if mode == "implicit":
            self.imap = imaplib.IMAP4_SSL(host, port, timeout=timeout)
        else:
            self.imap = imaplib.IMAP4(host, port, timeout=timeout)
            if mode == "starttls":
                self.imap.starttls()
            elif mode != "none":
                raise RuntimeError("IMAP_TLS_MODE must be implicit, starttls, or none")
        self.imap.login(required_env("IMAP_USERNAME"), read_secret(required_env("IMAP_PASSWORD_FILE")))
        self.client = CapabilityIMAP(self.imap)

    def close(self) -> None:
        try:
            self.imap.logout()
        except imaplib.IMAP4.error:
            pass

    def select(self, folder: str, readonly: bool) -> int:
        special_use = "\\junk" if folder == os.environ.get("IMAP_JUNK") else None
        mailbox = self.client.resolve(folder, special_use)
        status, _ = self.imap.select(mailbox.wire_name, readonly=readonly)
        if status != "OK":
            raise RuntimeError(f"cannot select IMAP folder: {folder}")
        response = self.imap.response("UIDVALIDITY")[1]
        if not response or not response[0]:
            raise RuntimeError("IMAP server did not report UIDVALIDITY")
        return int(response[0])

    def candidate_uids(self, since: datetime | None, before: datetime | None) -> list[int]:
        criteria: list[str] = ["ALL"]
        if since:
            criteria += ["SINCE", (since - timedelta(days=1)).strftime("%d-%b-%Y")]
        if before:
            criteria += ["BEFORE", (before + timedelta(days=1)).strftime("%d-%b-%Y")]
        status, data = self.imap.uid("SEARCH", None, *criteria)
        if status != "OK":
            raise RuntimeError("IMAP UID SEARCH failed")
        return [int(value) for value in (data[0] or b"").split()]

    def metadata(self, uid: int) -> tuple[datetime, str, str]:
        status, data = self.imap.uid("FETCH", str(uid), "(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM)])")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            raise RuntimeError(f"IMAP metadata fetch failed for UID {uid}")
        attrs, headers = data[0]
        message = email.message_from_bytes(headers)
        return parse_internaldate(attrs), decode_text(message.get("Subject")), decode_text(message.get("From"))

    def message(self, uid: int) -> bytes:
        status, data = self.imap.uid("FETCH", str(uid), "(UID BODY.PEEK[])")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            raise RuntimeError(f"IMAP message fetch failed for UID {uid}")
        return data[0][1]

    def move(self, uid: int, destination: str) -> int | None:
        if "MOVE" not in self.client.capabilities:
            raise RuntimeError("IMAP server does not support MOVE")
        special_use = "\\junk" if destination == os.environ.get("IMAP_JUNK") else None
        mailbox = self.client.resolve(destination, special_use)
        status, data = self.imap.uid("MOVE", str(uid), mailbox.wire_name)
        if status != "OK":
            raise RuntimeError(f"IMAP MOVE failed for UID {uid}")
        text = b" ".join(item for item in data if isinstance(item, bytes))
        match = re.search(rb"COPYUID\s+\d+\s+\d+\s+(\d+)", text)
        return int(match.group(1)) if match else None

    def find_digest(self, folder: str, digest: str) -> int | None:
        self.select(folder, readonly=True)
        for uid in self.candidate_uids(None, None):
            if hashlib.sha256(self.message(uid)).hexdigest() == digest:
                return uid
        return None


class SpamAssassin:
    def __init__(self):
        self.host = os.environ.get("SPAMD_HOST", "spamassassin")
        self.port = os.environ.get("SPAMD_PORT", "783")
        self.max_size = required_env("SPAMC_MAX_SIZE_BYTES")

    def _run(self, args: list[str], message: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["spamc", "-x", "-d", self.host, "-p", self.port, "-s", self.max_size, *args],
            input=message, capture_output=True, check=False,
        )

    def learn(self, message: bytes, learn_type: str) -> None:
        result = self._run(["-L", learn_type], message)
        if result.returncode != 0:
            raise RuntimeError(f"SpamAssassin learning failed with exit {result.returncode}")

    def score(self, message: bytes) -> tuple[float, list[str]]:
        result = self._run(["-R"], message)
        if result.returncode not in (0, 1):
            raise RuntimeError(f"SpamAssassin scan failed with exit {result.returncode}")
        return parse_score_report(result.stdout.decode("utf-8", "replace"))

    def rule_set_id(self) -> str:
        version = subprocess.run(["spamc", "-V"], capture_output=True, check=False).stdout
        config = Path("/etc/mail-sentinel/local.cf").read_bytes()
        marker = os.environ.get("RULESET_MARKER", "").encode()
        digest = hashlib.sha256(version + b"\0" + config + b"\0" + marker)
        rules_root = Path("/var/lib/spamassassin")
        if rules_root.exists():
            for path in sorted(value for value in rules_root.rglob("*") if value.is_file()):
                digest.update(str(path.relative_to(rules_root)).encode())
                digest.update(b"\0")
                digest.update(path.read_bytes())
        return digest.hexdigest()


def account_key() -> str:
    identity = "\0".join((required_env("IMAP_HOST"), required_env("IMAP_PORT"), required_env("IMAP_USERNAME")))
    return hashlib.sha256(identity.encode()).hexdigest()


@contextmanager
def folder_lock(folder: str) -> Iterator[None]:
    identity = "\0".join((required_env("IMAP_HOST"), required_env("IMAP_PORT"),
                            required_env("IMAP_USERNAME"), folder))
    key = hashlib.sha256(identity.encode()).hexdigest()
    root = Path(os.environ.get("STATE_DIR", "/var/lib/mail-sentinel-state")) / "locks"
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"{key}.lock").open("a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        yield


def in_range(value: datetime, since: datetime | None, before: datetime | None) -> bool:
    return (since is None or value >= since) and (before is None or value < before)


def candidate_metadata(mailbox: Mailbox, since: datetime | None, before: datetime | None,
                       maximum: int) -> list[tuple[int, datetime, str, str]]:
    candidates = []
    for uid in mailbox.candidate_uids(since, before):
        internaldate, subject, sender = mailbox.metadata(uid)
        if in_range(internaldate, since, before):
            candidates.append((uid, internaldate, subject, sender))
    candidates.sort(key=lambda value: (value[1], value[0]))
    return candidates[:maximum]


def preview(args: argparse.Namespace, state: State) -> None:
    since, before = resolve_range(args)
    if since and before and since >= before:
        raise RuntimeError("range start must be earlier than range end")
    spam = SpamAssassin()
    with folder_lock(args.folder):
        mailbox = Mailbox()
        try:
            uidvalidity = mailbox.select(args.folder, readonly=True)
            candidates = candidate_metadata(mailbox, since, before, args.max_messages)
            token = secrets.token_urlsafe(18)
            values = dict(
                job_type=args.job_type, mode="preview", account_key=account_key(), folder=args.folder,
                uidvalidity=uidvalidity, since_at=since.isoformat() if since else None,
                before_at=before.isoformat() if before else None, learn_type=getattr(args, "learn_type", None),
                max_messages=args.max_messages, max_moves=getattr(args, "max_moves", None),
                batch_size=args.batch_size, threshold=getattr(args, "threshold", None),
                rule_set_id=spam.rule_set_id() if args.job_type == "initial_scan" else None,
                confirmation_token=token, started_at=utc_now(), total_count=len(candidates),
            )
            job_id = state.create_job(**values)
            spam_candidates = 0
            for start in range(0, len(candidates), args.batch_size):
                for uid, internaldate, subject, sender in candidates[start:start + args.batch_size]:
                    digest = score = classification = rules = None
                    status = "previewed"
                    if args.job_type == "initial_scan":
                        raw = mailbox.message(uid)
                        digest = hashlib.sha256(raw).hexdigest()
                        score, rule_names = spam.score(raw)
                        classification = "spam" if score >= args.threshold else "ham"
                        rules = ",".join(rule_names)
                        spam_candidates += classification == "spam"
                    state.db.execute(
                        "INSERT INTO message_results(job_id,uidvalidity,uid,internaldate,digest,status,learn_type,score,classification,rules,subject,sender) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (job_id, uidvalidity, uid, internaldate.isoformat(), digest, status,
                         getattr(args, "learn_type", None), score, classification, rules, subject, sender),
                    )
                    if classification == "spam":
                        emit("scan_candidate", uid=uid, internaldate=internaldate.isoformat(),
                             subject=subject, sender=sender, score=score, rules=rule_names)
                state.db.commit()
            state.db.execute("UPDATE jobs SET status='previewed', processed_count=?, finished_at=? WHERE job_id=?",
                             (len(candidates), utc_now(), job_id))
            state.db.commit()
            emit("preview_complete", job_id=job_id, confirmation_token=token,
                 target_count=len(candidates), spam_candidates=spam_candidates,
                 oldest=candidates[0][1].isoformat() if candidates else None,
                 newest=candidates[-1][1].isoformat() if candidates else None)
        finally:
            mailbox.close()


def previous_success(state: State, job: sqlite3.Row, digest: str, uid: int) -> bool:
    if job["job_type"] == "initial_learn":
        return state.db.execute(
            "SELECT 1 FROM message_results WHERE digest=? AND learn_type=? AND status='learned' LIMIT 1",
            (digest, job["learn_type"]),
        ).fetchone() is not None
    return state.db.execute(
        "SELECT 1 FROM message_results mr JOIN jobs j ON j.job_id=mr.job_id "
        "WHERE j.account_key=? AND j.folder=? AND j.rule_set_id=? "
        "AND ((mr.uidvalidity=? AND mr.uid=?) OR mr.digest=?) "
        "AND mr.status IN ('classified','moved') LIMIT 1",
        (job["account_key"], job["folder"], job["rule_set_id"], job["uidvalidity"], uid, digest),
    ).fetchone() is not None


def get_or_create_apply(state: State, preview_job: sqlite3.Row) -> sqlite3.Row:
    existing = state.db.execute("SELECT * FROM jobs WHERE source_job_id=? AND mode='apply'", (preview_job["job_id"],)).fetchone()
    if existing:
        return existing
    copied = {key: preview_job[key] for key in (
        "job_type", "account_key", "folder", "uidvalidity", "since_at", "before_at", "learn_type",
        "max_messages", "max_moves", "batch_size", "threshold", "rule_set_id", "total_count",
    )}
    job_id = state.create_job(source_job_id=preview_job["job_id"], mode="apply", started_at=utc_now(), **copied)
    state.db.execute(
        "INSERT INTO message_results(job_id,uidvalidity,uid,internaldate,digest,status,learn_type,score,classification,rules,subject,sender) "
        "SELECT ?,uidvalidity,uid,internaldate,digest,'pending',learn_type,score,classification,rules,subject,sender "
        "FROM message_results WHERE job_id=?",
        (job_id, preview_job["job_id"]),
    )
    state.db.commit()
    return state.job(job_id)


def apply_job(args: argparse.Namespace, state: State) -> None:
    source = state.job(args.job_id)
    if source["mode"] != "preview" or source["status"] != "previewed":
        raise RuntimeError("--job-id must identify a completed preview job")
    if not secrets.compare_digest(source["confirmation_token"], args.confirm):
        raise RuntimeError("confirmation token does not match the preview")
    spam = SpamAssassin()
    if source["job_type"] == "initial_scan" and source["rule_set_id"] != spam.rule_set_id():
        raise RuntimeError("SpamAssassin rule set changed after preview; create a new preview")
    with folder_lock(source["folder"]):
        mailbox = Mailbox()
        try:
            uidvalidity = mailbox.select(source["folder"], readonly=source["job_type"] == "initial_learn")
            if uidvalidity != source["uidvalidity"]:
                raise RuntimeError("UIDVALIDITY changed after preview; create a new preview")
            job = get_or_create_apply(state, source)
            rows = state.db.execute(
                "SELECT * FROM message_results WHERE job_id=? AND status IN ('pending','retryable','processing') ORDER BY internaldate,uid",
                (job["job_id"],),
            ).fetchall()
            moved = job["moved_count"]
            limit_reached = False
            for start in range(0, len(rows), job["batch_size"]):
                for row in rows[start:start + job["batch_size"]]:
                    if job["job_type"] == "initial_scan" and row["classification"] == "spam" and moved >= job["max_moves"]:
                        limit_reached = True
                        break
                    uid = row["uid"]
                    state.db.execute("UPDATE message_results SET status='processing',error=NULL WHERE job_id=? AND uidvalidity=? AND uid=?",
                                     (job["job_id"], uidvalidity, uid))
                    state.db.commit()
                    try:
                        try:
                            raw = mailbox.message(uid)
                        except RuntimeError:
                            if job["job_type"] == "initial_scan" and row["classification"] == "spam" and row["digest"]:
                                try:
                                    destination_uid = mailbox.find_digest(required_env("IMAP_JUNK"), row["digest"])
                                finally:
                                    mailbox.select(job["folder"], readonly=False)
                                if destination_uid is not None:
                                    state.db.execute(
                                        "UPDATE message_results SET status='moved',moved_to=?,destination_uid=?,processed_at=?,error=NULL WHERE job_id=? AND uidvalidity=? AND uid=?",
                                        (required_env("IMAP_JUNK"), destination_uid, utc_now(), job["job_id"], uidvalidity, uid),
                                    )
                                    state.db.commit()
                                    moved += 1
                                    emit("move_reconciled", job_id=job["job_id"], uid=uid,
                                         destination_uid=destination_uid)
                                    continue
                            raise
                        digest = hashlib.sha256(raw).hexdigest()
                        if row["digest"] and row["digest"] != digest:
                            raise RuntimeError("message digest changed after preview")
                        if previous_success(state, job, digest, uid):
                            status, destination_uid = "skipped", None
                        elif job["job_type"] == "initial_learn":
                            spam.learn(raw, job["learn_type"])
                            status, destination_uid = "learned", None
                        elif row["classification"] == "spam":
                            destination_uid = mailbox.move(uid, required_env("IMAP_JUNK"))
                            status = "moved"
                            moved += 1
                        else:
                            status, destination_uid = "classified", None
                        state.db.execute(
                            "UPDATE message_results SET digest=?,status=?,moved_to=?,destination_uid=?,processed_at=? WHERE job_id=? AND uidvalidity=? AND uid=?",
                            (digest, status, required_env("IMAP_JUNK") if status == "moved" else None,
                             destination_uid, utc_now(), job["job_id"], uidvalidity, uid),
                        )
                        state.db.commit()
                    except Exception as error:
                        state.db.execute(
                            "UPDATE message_results SET status='retryable',error=?,processed_at=? WHERE job_id=? AND uidvalidity=? AND uid=?",
                            (str(error), utc_now(), job["job_id"], uidvalidity, uid),
                        )
                        state.db.commit()
                        emit("message_failed", job_id=job["job_id"], uid=uid, error=str(error), retryable=True)
                if limit_reached:
                    break
            if limit_reached:
                state.db.execute(
                    "UPDATE message_results SET status='skipped_limit',processed_at=? WHERE job_id=? AND status='pending'",
                    (utc_now(), job["job_id"]),
                )
                state.db.commit()
            counts = dict(state.db.execute(
                "SELECT COUNT(*) processed_count, SUM(status IN ('learned','classified','moved')) success_count, "
                "SUM(status IN ('skipped','skipped_limit')) skipped_count, SUM(status='retryable') failed_count, SUM(status='moved') moved_count "
                "FROM message_results WHERE job_id=? AND status!='pending'", (job["job_id"],),
            ).fetchone())
            pending = state.db.execute("SELECT COUNT(*) FROM message_results WHERE job_id=? AND status IN ('pending','retryable','processing')",
                                       (job["job_id"],)).fetchone()[0]
            status = "completed_with_limit" if limit_reached else ("completed" if pending == 0 else "interrupted")
            state.db.execute(
                "UPDATE jobs SET status=?,processed_count=?,success_count=?,skipped_count=?,failed_count=?,moved_count=?,finished_at=? WHERE job_id=?",
                (status, *(value or 0 for value in counts.values()), utc_now(), job["job_id"]),
            )
            state.db.commit()
            emit("apply_complete", job_id=job["job_id"], status=status, **{k: v or 0 for k, v in counts.items()})
        finally:
            mailbox.close()


def status_command(args: argparse.Namespace, state: State) -> None:
    print(json.dumps(dict(state.job(args.job_id)), ensure_ascii=False, indent=2))


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--folder", required=True)
    parser.add_argument("--since-date", help="inclusive local calendar date (YYYY-MM-DD)")
    parser.add_argument("--through-date", help="inclusive local calendar date (YYYY-MM-DD)")
    parser.add_argument("--timezone", default="Asia/Tokyo", help="IANA timezone for date options")
    parser.add_argument("--since-datetime", "--since", dest="since_datetime",
                        help="inclusive ISO 8601 date-time with UTC offset")
    parser.add_argument("--before-datetime", "--before", dest="before_datetime",
                        help="exclusive ISO 8601 date-time with UTC offset")
    parser.add_argument("--max-messages", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=25)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mail-sentinel-admin")
    parser.add_argument("--account", required=True, help="account name from accounts.json")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, job_type in (("initial-learn", "initial_learn"), ("initial-scan", "initial_scan")):
        job_parser = commands.add_parser(name)
        actions = job_parser.add_subparsers(dest="action", required=True)
        preview_parser = actions.add_parser("preview")
        add_common(preview_parser)
        preview_parser.set_defaults(job_type=job_type, handler=preview)
        if name == "initial-learn":
            preview_parser.add_argument("--type", dest="learn_type", choices=("ham", "spam"), required=True)
        else:
            preview_parser.add_argument("--max-moves", type=int, required=True)
            preview_parser.add_argument("--threshold", type=float, default=5.0)
        apply_parser = actions.add_parser("apply")
        apply_parser.add_argument("--job-id", required=True)
        apply_parser.add_argument("--confirm", required=True)
        apply_parser.set_defaults(handler=apply_job)
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--job-id", required=True)
    status_parser.set_defaults(handler=status_command)
    return parser


def validate_limits(args: argparse.Namespace) -> None:
    for name in ("max_messages", "batch_size", "max_moves"):
        value = getattr(args, name, None)
        if value is not None and value < 1:
            raise RuntimeError(f"--{name.replace('_', '-')} must be positive")


def main() -> int:
    args = build_parser().parse_args()
    validate_limits(args)
    matches = [item for item in configured_accounts() if item[0] == args.account]
    if not matches:
        emit("admin_failed", error="configured account was not found")
        return 1
    _name, identifier, configured = matches[0]
    environment = account_environment(identifier, configured)
    os.environ.clear()
    os.environ.update(environment)
    state_dir = Path(os.environ.get("STATE_DIR", "/var/lib/mail-sentinel-state"))
    state = State(state_dir / "state.sqlite3")
    try:
        args.handler(args, state)
        return 0
    except Exception as error:
        emit("admin_failed", error=str(error))
        return 1
    finally:
        state.close()


if __name__ == "__main__":
    raise SystemExit(main())
