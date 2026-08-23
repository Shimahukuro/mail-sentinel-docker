"""Persistent operational state, incident lifecycle, audit events, and notifications."""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


OPERATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_status (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_counters (
  key TEXT PRIMARY KEY,
  value INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS active_incidents (
  event_code TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN ('pending','open','recovered')),
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  occurrence_count INTEGER NOT NULL,
  notified_at TEXT,
  recovered_at TEXT,
  recovery_notified_at TEXT,
  last_error_type TEXT
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  occurred_at TEXT NOT NULL,
  action TEXT NOT NULL,
  result TEXT NOT NULL,
  account_id TEXT,
  details TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS audit_events_occurred_at ON audit_events(occurred_at);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def env_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value < 1:
        raise RuntimeError(f"{name} must be positive")
    return value


class OperationsState:
    def __init__(self, database: sqlite3.Connection):
        self.db = database
        self.db.executescript(OPERATIONS_SCHEMA)

    def set_status(self, key: str, value: object) -> None:
        now = utc_now()
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        self.db.execute(
            "INSERT INTO runtime_status VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, encoded, now),
        )
        self.db.commit()

    def increment(self, key: str, amount: int = 1) -> None:
        now = utc_now()
        self.db.execute(
            "INSERT INTO runtime_counters VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=value+excluded.value,updated_at=excluded.updated_at",
            (key, amount, now),
        )
        self.db.commit()

    def audit(self, action: str, result: str, **details: object) -> None:
        safe = {key: value for key, value in details.items() if value is not None}
        self.db.execute(
            "INSERT INTO audit_events(occurred_at,action,result,account_id,details) VALUES (?,?,?,?,?)",
            (utc_now(), action, result, os.environ.get("MAIL_SENTINEL_ACCOUNT_ID"),
             json.dumps(safe, ensure_ascii=False, separators=(",", ":"))),
        )
        self.db.commit()

    def record_success(self, event_code: str) -> bool:
        """Record recovery. Return true when a recovery notification is due."""
        row = self.db.execute(
            "SELECT status,notified_at FROM active_incidents WHERE event_code=?", (event_code,)
        ).fetchone()
        if not row:
            return False
        if row[0] == "recovered":
            recovery = self.db.execute(
                "SELECT recovery_notified_at FROM active_incidents WHERE event_code=?", (event_code,)
            ).fetchone()
            return row[1] is not None and recovery[0] is None
        now = utc_now()
        self.db.execute(
            "UPDATE active_incidents SET status='recovered',recovered_at=?,last_seen_at=? WHERE event_code=?",
            (now, now, event_code),
        )
        self.db.commit()
        return row[0] == "open" and row[1] is not None

    def record_failure(self, event_code: str, error_type: str) -> bool:
        """Record a failure. Return true when an initial or repeat notification is due."""
        now = utc_now()
        row = self.db.execute(
            "SELECT status,occurrence_count,notified_at FROM active_incidents WHERE event_code=?",
            (event_code,),
        ).fetchone()
        if not row or row[0] == "recovered":
            count, notified_at = 1, None
            self.db.execute(
                "INSERT INTO active_incidents(event_code,status,first_seen_at,last_seen_at,occurrence_count,last_error_type) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(event_code) DO UPDATE SET status='pending',"
                "first_seen_at=excluded.first_seen_at,last_seen_at=excluded.last_seen_at,"
                "occurrence_count=1,notified_at=NULL,recovered_at=NULL,recovery_notified_at=NULL,"
                "last_error_type=excluded.last_error_type",
                (event_code, "pending", now, now, count, error_type),
            )
        else:
            count, notified_at = int(row[1]) + 1, row[2]
            self.db.execute(
                "UPDATE active_incidents SET last_seen_at=?,occurrence_count=?,last_error_type=? WHERE event_code=?",
                (now, count, error_type, event_code),
            )
        threshold = env_int("NOTIFICATION_FAILURE_THRESHOLD", 3)
        repeat = timedelta(seconds=env_int("NOTIFICATION_REPEAT_SECONDS", 21600))
        due = count >= threshold and (
            notified_at is None or datetime.now(timezone.utc) - parse_time(notified_at) >= repeat
        )
        if due:
            self.db.execute(
                "UPDATE active_incidents SET status='open' WHERE event_code=?", (event_code,)
            )
        self.db.commit()
        return due

    def mark_notified(self, event_code: str, recovery: bool = False) -> None:
        column = "recovery_notified_at" if recovery else "notified_at"
        self.db.execute(
            f"UPDATE active_incidents SET {column}=? WHERE event_code=?", (utc_now(), event_code)
        )
        self.db.commit()

    def snapshot(self) -> dict[str, object]:
        status = {
            row[0]: {"value": json.loads(row[1]), "updated_at": row[2]}
            for row in self.db.execute("SELECT key,value,updated_at FROM runtime_status")
        }
        counters = {
            row[0]: {"value": row[1], "updated_at": row[2]}
            for row in self.db.execute("SELECT key,value,updated_at FROM runtime_counters")
        }
        incidents = [dict(row) for row in self.db.execute(
            "SELECT * FROM active_incidents WHERE status IN ('pending','open') ORDER BY first_seen_at"
        )]
        last_success = status.get("last_scan_success", {}).get("updated_at")
        health = "unknown" if not last_success else ("unhealthy" if any(
            item["status"] == "open" for item in incidents) else ("degraded" if incidents else "healthy"))
        return {"health": health, "status": status, "counters": counters, "incidents": incidents}


class Notifier:
    def __init__(self, state: OperationsState):
        self.state = state

    def enabled(self) -> bool:
        return os.environ.get("NOTIFICATION_ENABLED", "false").lower() == "true"

    def send(self, event_code: str, recovery: bool = False) -> bool:
        if not self.enabled():
            return False
        if recovery and os.environ.get("NOTIFICATION_RECOVERY_ENABLED", "true").lower() != "true":
            return False
        path = os.environ.get("NOTIFICATION_WEBHOOK_URL_FILE", "")
        if not path:
            raise RuntimeError("NOTIFICATION_WEBHOOK_URL_FILE is required when notifications are enabled")
        url = Path(path).read_text(encoding="utf-8").strip()
        payload = json.dumps({
            "source": "mail-sentinel", "event_code": event_code,
            "state": "recovered" if recovery else "firing",
            "account_id": os.environ.get("MAIL_SENTINEL_ACCOUNT_ID"), "timestamp": utc_now(),
        }, separators=(",", ":")).encode()
        request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=env_int("NOTIFICATION_TIMEOUT_SECONDS", 10)) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"notification webhook returned HTTP {response.status}")
        self.state.mark_notified(event_code, recovery)
        self.state.audit("notification_sent", "success", event_code=event_code, recovery=recovery)
        return True


def open_operations(state_dir: Path) -> tuple[sqlite3.Connection, OperationsState]:
    database = sqlite3.connect(state_dir / "worker-state.sqlite3")
    database.row_factory = sqlite3.Row
    return database, OperationsState(database)
