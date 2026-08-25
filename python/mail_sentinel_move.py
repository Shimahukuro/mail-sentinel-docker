"""Crash-safe IMAP message moves shared by the worker and admin tools."""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from mail_sentinel_imap import CapabilityIMAP, MailboxInfo


MOVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS move_transactions (
  move_id TEXT PRIMARY KEY,
  source_folder TEXT NOT NULL,
  source_uidvalidity INTEGER NOT NULL,
  source_uid INTEGER NOT NULL,
  destination_folder TEXT NOT NULL,
  marker TEXT NOT NULL UNIQUE,
  method TEXT NOT NULL,
  stage TEXT NOT NULL,
  destination_uidvalidity INTEGER,
  destination_uid INTEGER,
  updated_at TEXT NOT NULL,
  UNIQUE(source_folder, source_uidvalidity, source_uid, destination_folder)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _uid_set(data: list[bytes | None] | None) -> list[int]:
    if not data:
        return []
    return [int(value) for value in (data[0] or b"").split()]


def _permanent_keywords(connection) -> bool:
    response = connection.response("PERMANENTFLAGS")[1]
    return bool(response and response[0] and b"\\*" in response[0].strip().strip(b"()").split())


@dataclass(frozen=True)
class MovePlan:
    method: str
    reason: str


class SafeMover:
    """Select a safe move method and make COPY fallback retries idempotent."""

    def __init__(self, connection, client: CapabilityIMAP, database: sqlite3.Connection):
        self.connection = connection
        self.client = client
        self.database = database
        self.database.executescript(MOVE_SCHEMA)

    def _select(self, mailbox: MailboxInfo, readonly: bool = False) -> int:
        status, _ = self.connection.select(mailbox.wire_name, readonly=readonly)
        if status != "OK":
            raise RuntimeError("cannot select IMAP folder")
        response = self.connection.response("UIDVALIDITY")[1]
        if not response or not response[0]:
            raise RuntimeError("IMAP server did not report UIDVALIDITY")
        return int(response[0])

    def plan(self, source: MailboxInfo, destination: MailboxInfo, configured: str) -> MovePlan:
        if configured not in ("auto", "disabled"):
            raise RuntimeError("IMAP_MOVE_FALLBACK must be auto or disabled")
        if "MOVE" in self.client.capabilities:
            return MovePlan("uid_move", "move_available")
        if configured == "disabled":
            return MovePlan("unsupported", "fallback_disabled")
        if "UIDPLUS" not in self.client.capabilities:
            return MovePlan("unsupported", "uidplus_unavailable")
        self._select(source)
        source_keywords = _permanent_keywords(self.connection)
        self._select(destination)
        destination_keywords = _permanent_keywords(self.connection)
        self._select(source)
        if not source_keywords:
            return MovePlan("unsupported", "source_keywords_unavailable")
        if not destination_keywords:
            return MovePlan("unsupported", "destination_keywords_unavailable")
        return MovePlan("copy_uid_expunge", "uidplus_and_keywords_available")

    def move(self, source: MailboxInfo, source_uidvalidity: int, uid: int,
             destination: MailboxInfo, configured: str = "disabled") -> int | None:
        plan = self.plan(source, destination, configured)
        if plan.method == "uid_move":
            self._select(source)
            status, data = self.connection.uid("MOVE", str(uid), destination.wire_name)
            if status != "OK":
                raise RuntimeError("IMAP UID MOVE failed")
            return self._copyuid(data)
        if plan.method == "unsupported":
            raise RuntimeError(f"safe IMAP move is unsupported: {plan.reason}")
        return self._copy_uid_expunge(source, source_uidvalidity, uid, destination)

    @staticmethod
    def _copyuid(data) -> int | None:
        text = b" ".join(item for item in (data or []) if isinstance(item, bytes))
        match = re.search(rb"COPYUID\s+\d+\s+\d+(?::\d+)?\s+(\d+)", text)
        return int(match.group(1)) if match else None

    def _transaction(self, source: MailboxInfo, generation: int, uid: int,
                     destination: MailboxInfo) -> sqlite3.Row:
        self.database.row_factory = sqlite3.Row
        row = self.database.execute(
            "SELECT * FROM move_transactions WHERE source_folder=? AND source_uidvalidity=? "
            "AND source_uid=? AND destination_folder=?",
            (source.name, generation, uid, destination.name),
        ).fetchone()
        if row:
            return row
        move_id = uuid.uuid4().hex
        marker = f"MailSentinelMove_{move_id}"
        self.database.execute(
            "INSERT INTO move_transactions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (move_id, source.name, generation, uid, destination.name, marker,
             "copy_uid_expunge", "prepared", None, None, _now()),
        )
        self.database.commit()
        return self.database.execute("SELECT * FROM move_transactions WHERE move_id=?", (move_id,)).fetchone()

    def _find_marker(self, destination: MailboxInfo, marker: str) -> tuple[int, list[int]]:
        generation = self._select(destination)
        status, data = self.connection.uid("SEARCH", None, "KEYWORD", marker)
        if status != "OK":
            raise RuntimeError("IMAP marker search failed")
        return generation, _uid_set(data)

    def _set_stage(self, move_id: str, stage: str, destination_uidvalidity: int | None = None,
                   destination_uid: int | None = None) -> None:
        self.database.execute(
            "UPDATE move_transactions SET stage=?,destination_uidvalidity=COALESCE(?,destination_uidvalidity),"
            "destination_uid=COALESCE(?,destination_uid),updated_at=? WHERE move_id=?",
            (stage, destination_uidvalidity, destination_uid, _now(), move_id),
        )
        self.database.commit()

    def _copy_uid_expunge(self, source: MailboxInfo, generation: int, uid: int,
                          destination: MailboxInfo) -> int:
        transaction = self._transaction(source, generation, uid, destination)
        marker = str(transaction["marker"])
        if transaction["stage"] in ("source_removed", "completed"):
            destination_uid = int(transaction["destination_uid"])
            destination_generation = self._select(destination)
            status, data = self.connection.uid("SEARCH", None, "UID", str(destination_uid))
            if status != "OK" or destination_uid not in _uid_set(data):
                raise RuntimeError("verified destination message is missing")
            if transaction["stage"] == "source_removed":
                status, _ = self.connection.uid(
                    "STORE", str(destination_uid), "-FLAGS.SILENT", f"({marker})"
                )
                if status != "OK":
                    raise RuntimeError("failed to remove destination move marker")
                self._set_stage(str(transaction["move_id"]), "completed",
                                destination_generation, destination_uid)
            self._select(source)
            return destination_uid
        destination_generation, copies = self._find_marker(destination, marker)
        if len(copies) > 1:
            raise RuntimeError("multiple marked destination copies found")

        if copies:
            destination_uid = copies[0]
        else:
            if self._select(source) != generation:
                raise RuntimeError("source UIDVALIDITY changed during move")
            status, data = self.connection.uid("SEARCH", None, "UID", str(uid))
            if status != "OK" or uid not in _uid_set(data):
                raise RuntimeError("source message missing before verified copy")
            status, _ = self.connection.uid("STORE", str(uid), "+FLAGS.SILENT", f"({marker})")
            if status != "OK":
                raise RuntimeError("failed to apply move marker")
            status, copy_data = self.connection.uid("COPY", str(uid), destination.wire_name)
            if status != "OK":
                # A tagged NO proves COPY was not accepted; restore the source flags.
                self.connection.uid("STORE", str(uid), "-FLAGS.SILENT", f"({marker})")
                raise RuntimeError("IMAP UID COPY failed")
            destination_generation, copies = self._find_marker(destination, marker)
            if len(copies) != 1:
                raise RuntimeError("copied message could not be verified")
            destination_uid = copies[0]
            copyuid = self._copyuid(copy_data)
            if copyuid is not None and copyuid != destination_uid:
                raise RuntimeError("COPYUID does not match marked destination copy")
            self._set_stage(str(transaction["move_id"]), "copied", destination_generation, destination_uid)

        source_exists = False
        if self._select(source) == generation:
            status, data = self.connection.uid("SEARCH", None, "UID", str(uid))
            if status != "OK":
                raise RuntimeError("IMAP source verification failed")
            source_exists = uid in _uid_set(data)
        if source_exists:
            status, _ = self.connection.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Deleted)")
            if status != "OK":
                raise RuntimeError("failed to mark source message deleted")
            status, _ = self.connection.uid("EXPUNGE", str(uid))
            if status != "OK":
                raise RuntimeError("IMAP UID EXPUNGE failed")
        self._set_stage(str(transaction["move_id"]), "source_removed", destination_generation, destination_uid)

        self._select(destination)
        status, _ = self.connection.uid("STORE", str(destination_uid), "-FLAGS.SILENT", f"({marker})")
        if status != "OK":
            raise RuntimeError("failed to remove destination move marker")
        self._set_stage(str(transaction["move_id"]), "completed", destination_generation, destination_uid)
        self._select(source)
        return destination_uid
