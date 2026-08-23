#!/usr/bin/env python3
"""Consistent backup, verification, and restore for Mail Sentinel volumes."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path


COMPONENTS = {
    "spamassassin-data": Path("/data/spamassassin-data"),
    "spamassassin-rules": Path("/data/spamassassin-rules"),
    "mail-sentinel-state": Path("/data/mail-sentinel-state"),
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def emit(event: str, **fields: object) -> None:
    print(json.dumps({"timestamp": now(), "event": event, **fields}, separators=(",", ":")), flush=True)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def safe_members(archive: tarfile.TarFile, root: Path) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    resolved = root.resolve()
    for member in members:
        target = (root / member.name).resolve()
        if resolved not in (target, *target.parents) or member.issym() or member.islnk():
            raise RuntimeError(f"unsafe archive member: {member.name}")
    return members


def create(args: argparse.Namespace) -> None:
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise RuntimeError(f"backup already exists: {output}")
    with tempfile.TemporaryDirectory() as temporary:
        staging = Path(temporary)
        hashes = {}
        for name, source in COMPONENTS.items():
            component = staging / f"{name}.tar.gz"
            with tarfile.open(component, "w:gz") as archive:
                for item in sorted(source.iterdir(), key=lambda value: value.name):
                    archive.add(item, arcname=item.name, recursive=True)
            hashes[component.name] = digest(component)
        manifest = {
            "format": 1, "created_at": now(), "components": hashes,
            "contains_secrets": False,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        with tarfile.open(output, "w:gz") as archive:
            archive.add(staging / "manifest.json", arcname="manifest.json")
            for filename in sorted(hashes):
                archive.add(staging / filename, arcname=filename)
    emit("backup_created", result="success", output=str(output), sha256=digest(output))


def unpack_verified(source: Path, staging: Path) -> dict[str, object]:
    with tarfile.open(source, "r:gz") as archive:
        archive.extractall(staging, members=safe_members(archive, staging), filter="data")
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or set(manifest.get("components", {})) != {
        f"{name}.tar.gz" for name in COMPONENTS
    }:
        raise RuntimeError("unsupported or incomplete backup manifest")
    for filename, expected in manifest["components"].items():
        if digest(staging / filename) != expected:
            raise RuntimeError(f"backup checksum mismatch: {filename}")
    return manifest


def verify(args: argparse.Namespace) -> None:
    source = Path(args.archive)
    with tempfile.TemporaryDirectory() as temporary:
        manifest = unpack_verified(source, Path(temporary))
    emit("backup_verified", result="success", archive=str(source), created_at=manifest["created_at"])


def restore(args: argparse.Namespace) -> None:
    if args.confirm != "RESTORE":
        raise RuntimeError("restore requires --confirm RESTORE")
    source = Path(args.archive)
    with tempfile.TemporaryDirectory() as temporary:
        staging = Path(temporary)
        manifest = unpack_verified(source, staging)
        for name, destination in COMPONENTS.items():
            for item in destination.iterdir():
                if item.is_dir() and not item.is_symlink():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            with tarfile.open(staging / f"{name}.tar.gz", "r:gz") as archive:
                archive.extractall(destination, members=safe_members(archive, destination), filter="data")
    emit("backup_restored", result="success", archive=str(source), created_at=manifest["created_at"])


def check(_args: argparse.Namespace) -> None:
    databases = list(COMPONENTS["mail-sentinel-state"].rglob("*.sqlite3"))
    for database in databases:
        connection = sqlite3.connect(database)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()
        if result != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {database.name}")
    emit("state_integrity_checked", result="success", database_count=len(databases))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="mail-sentinel-maintenance")
    commands = root.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--output", required=True)
    backup.set_defaults(handler=create)
    verification = commands.add_parser("verify")
    verification.add_argument("--archive", required=True)
    verification.set_defaults(handler=verify)
    restoration = commands.add_parser("restore")
    restoration.add_argument("--archive", required=True)
    restoration.add_argument("--confirm", required=True)
    restoration.set_defaults(handler=restore)
    integrity = commands.add_parser("check-state")
    integrity.set_defaults(handler=check)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        args.handler(args)
        return 0
    except Exception as error:
        emit("maintenance_failed", result="failure", command=args.command,
             error_type=type(error).__name__, error=str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
