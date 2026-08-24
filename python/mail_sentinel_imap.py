"""Capability-driven IMAP helpers with UTF-8 and Modified UTF-7 support."""

from __future__ import annotations

import base64
import imaplib
import re
from dataclasses import dataclass


def encode_modified_utf7(value: str) -> bytes:
    result = bytearray()
    non_ascii: list[str] = []

    def flush() -> None:
        if not non_ascii:
            return
        encoded = base64.b64encode("".join(non_ascii).encode("utf-16-be")).rstrip(b"=").replace(b"/", b",")
        result.extend(b"&" + encoded + b"-")
        non_ascii.clear()

    for character in value:
        codepoint = ord(character)
        if 0x20 <= codepoint <= 0x7E:
            flush()
            result.extend(b"&-" if character == "&" else character.encode("ascii"))
        else:
            non_ascii.append(character)
    flush()
    return bytes(result)


def decode_modified_utf7(value: bytes) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index:index + 1] != b"&":
            end = value.find(b"&", index)
            if end < 0:
                end = len(value)
            output.append(value[index:end].decode("ascii"))
            index = end
            continue
        end = value.find(b"-", index)
        if end < 0:
            raise ValueError("invalid Modified UTF-7 mailbox name")
        token = value[index + 1:end]
        if not token:
            output.append("&")
        else:
            token = token.replace(b",", b"/") + b"=" * (-len(token) % 4)
            output.append(base64.b64decode(token).decode("utf-16-be"))
        index = end + 1
    return "".join(output)


@dataclass(frozen=True)
class MailboxInfo:
    name: str
    wire_name: bytes
    flags: frozenset[str]


_LIST_PATTERN = re.compile(rb"^\(([^)]*)\)\s+(?:\"(?:[^\"\\]|\\.)*\"|NIL)\s+(.+)$")


def _unquote(value: bytes) -> bytes:
    value = value.strip()
    if len(value) >= 2 and value[:1] == b'"' and value[-1:] == b'"':
        return re.sub(rb"\\(.)", rb"\1", value[1:-1])
    return value


class CapabilityIMAP:
    def __init__(self, connection: imaplib.IMAP4):
        self.connection = connection
        status, data = connection.capability()
        if status != "OK":
            raise RuntimeError("IMAP CAPABILITY failed")
        self.capabilities = frozenset((data[0] or b"").decode("ascii", "replace").upper().split())
        # imaplib caches the pre-authentication capabilities and its enable()
        # guard consults that cache.  Servers may advertise ENABLE only after
        # login, so keep imaplib in sync with the authoritative post-auth reply.
        connection.capabilities = tuple(sorted(self.capabilities))
        self.utf8_enabled = False
        if "ENABLE" in self.capabilities and "UTF8=ACCEPT" in self.capabilities:
            status, _ = connection.enable("UTF8=ACCEPT")
            if status != "OK":
                raise RuntimeError("IMAP ENABLE UTF8=ACCEPT failed")
            self.utf8_enabled = True

    def argument(self, name: str) -> str | bytes:
        return name if self.utf8_enabled else encode_modified_utf7(name)

    def mailboxes(self) -> list[MailboxInfo]:
        status, rows = self.connection.list()
        if status != "OK":
            raise RuntimeError("IMAP LIST failed")
        result = []
        for row in rows or []:
            match = _LIST_PATTERN.match(row)
            if not match:
                continue
            flags = frozenset(value.decode("ascii", "replace").lower() for value in match.group(1).split())
            # Keep the server's quoted/atom representation for subsequent
            # commands.  Passing an unquoted name such as `Bulk Mail` would
            # otherwise be parsed as two IMAP command arguments.
            wire = match.group(2).strip()
            decoded_wire = _unquote(wire)
            name = (
                decoded_wire.decode("utf-8")
                if self.utf8_enabled
                else decode_modified_utf7(decoded_wire)
            )
            result.append(MailboxInfo(name=name, wire_name=wire, flags=flags))
        return result

    def resolve(self, configured_name: str, special_use: str | None = None) -> MailboxInfo:
        mailboxes = self.mailboxes()
        # RFC 3501 and RFC 9051 reserve INBOX as the only mailbox name
        # that is always case-insensitive.  Preserve the server's wire form
        # after matching it; all other mailbox names remain exact matches
        # because their case-sensitivity is server-dependent.
        if configured_name.upper() == "INBOX":
            for mailbox in mailboxes:
                if mailbox.name.upper() == "INBOX":
                    return mailbox
            raise RuntimeError(f"configured IMAP folder does not exist: {configured_name}")
        for mailbox in mailboxes:
            if mailbox.name == configured_name:
                return mailbox
        if special_use:
            expected = special_use.lower()
            matches = [mailbox for mailbox in mailboxes if expected in mailbox.flags]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                names = ", ".join(mailbox.name for mailbox in matches)
                raise RuntimeError(
                    f"multiple IMAP folders have special-use {special_use}: {names}"
                )
        raise RuntimeError(f"configured IMAP folder does not exist: {configured_name}")
