#!/usr/bin/env python3
"""Run one isolated worker subprocess per configured IMAP account."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

from mail_sentinel_accounts import account_environment, configured_accounts


WORKER = "/usr/local/bin/mail-sentinel-worker"


def log(level: str, event: str, **fields: object) -> None:
    print(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "level": level, "event": event, **fields,
    }, ensure_ascii=False, separators=(",", ":")), flush=True)


def run() -> int:
    accounts = configured_accounts()
    children: dict[str, subprocess.Popen[bytes]] = {}
    one_shot = any(argument in ("--diagnose", "--setup") for argument in sys.argv[1:])
    result = 0
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        for child in children.values():
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for _name, identifier, configured in accounts:
        children[identifier] = subprocess.Popen(
            [WORKER, *sys.argv[1:]], env=account_environment(identifier, configured))
        log("info", "account_worker_started", account_id=identifier)
    while children:
        for identifier, child in list(children.items()):
            status = child.poll()
            if status is None:
                continue
            if stopping:
                del children[identifier]
                continue
            if one_shot:
                result = status if status != 0 else result
                del children[identifier]
                continue
            log("error", "account_worker_exited", account_id=identifier, exit_code=status,
                retry_in_seconds=5)
            time.sleep(5)
            if stopping:
                del children[identifier]
                continue
            configured = next(item for _name, item_id, item in accounts if item_id == identifier)
            children[identifier] = subprocess.Popen(
                [WORKER, *sys.argv[1:]], env=account_environment(identifier, configured))
        time.sleep(1)
    return result


if __name__ == "__main__":
    raise SystemExit(run())
