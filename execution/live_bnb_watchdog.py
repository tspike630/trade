"""Production supervisor for the one ``live_bnb_boll_fade`` execution loop.

Fail closed. This module never deletes ``LIVE_TRADING_DISABLED``, never
writes ``BINANCE_SPOT_LIVE_ENABLED``, and never reads secrets into a
report or onto a command line. It starts the loop only when the operator
has already removed the kill file, set the live flag, set a spot book,
and provided an API key and secret in the process environment.

``--dry-run`` prints the preflight and does not spawn. The dry wrapper
script calls only that mode.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from execution.executors.binance_spot import (  # noqa: E402
    API_KEY_ENV,
    API_SECRET_ENV,
    ENABLE_ENV,
    PURPOSE_LIVE_BNB_BOLL_FADE,
    _env_flag,
    _env_secret,
    kill_switch_active,
)
from execution.binance_account_snapshot import production_snapshot  # noqa: E402
from execution.executors.binance_spot_gate import (  # noqa: E402
    REST_BOOK,
    funding_block,
    snapshot_usdt_free,
)
from execution.executors.binance_spot_protect import (  # noqa: E402
    BOOK_ENV,
    ENTRY_MIN_NOTIONAL_USDT,
    book_usdt,
)

RESTART_DELAY_SEC = 3.0
LEDGER_NAME = "execution_ledger_live_bnb_boll_fade.db"
LOCK_NAME = "live_bnb_boll_fade.watchdog.lock"


def child_argv(python: str | None = None) -> list[str]:
    """The only command this supervisor is allowed to spawn."""
    exe = python or sys.executable
    return [
        exe,
        "-m",
        "execution.run",
        "--loop",
        "--ledger",
        LEDGER_NAME,
        "--accept-purpose",
        PURPOSE_LIVE_BNB_BOLL_FADE,
    ]


def preflight() -> dict[str, Any]:
    """Read gates. Do not create, delete, or rewrite them."""
    reasons: list[str] = []
    active, name = kill_switch_active(None)
    if active:
        reasons.append(f"kill_switch:{name}")
    if not _env_flag(ENABLE_ENV):
        reasons.append("binance_spot_live_disabled")
    if not _env_secret(API_KEY_ENV) or not _env_secret(API_SECRET_ENV):
        reasons.append("missing_api_credentials")
    if book_usdt() is None:
        reasons.append("missing_book_usdt")
    book = os.environ.get("BINANCE_SPOT_PRODUCTION_BOOK", "").strip()
    if book != REST_BOOK:
        reasons.append("production_book_unselected")
    else:
        payload, snap_error = production_snapshot()
        if snap_error:
            reasons.append(snap_error)
        elif funding_block(snapshot_usdt_free(payload), ENTRY_MIN_NOTIONAL_USDT):
            reasons.append("funding_block")
    ok = not reasons
    report: dict[str, Any] = {
        "ok": ok,
        "would_start": ok,
        "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        "paper_enabled": False,
        "restart_delay_sec": RESTART_DELAY_SEC,
        "reasons": reasons,
        "argv": child_argv() if ok else None,
        "live_flag_env": ENABLE_ENV,
        "book_env": BOOK_ENV,
        "api_credentials_present": "missing_api_credentials" not in reasons,
    }
    return report


def _lock(path: Path):
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def default_runner(argv: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(argv)


def supervise(
    *,
    runner: Callable[[list[str]], Any] | None = None,
    sleeper: Callable[[float], None] | None = None,
    max_cycles: int | None = None,
    lock_path: Path | None = None,
    python: str | None = None,
) -> int:
    """Run one instance. Restart at most ``RESTART_DELAY_SEC`` after an exit.

    Returns 2 when preflight refuses, 3 when another instance holds the lock.
    """
    if RESTART_DELAY_SEC > 3.0:
        return 2
    report = preflight()
    if not report["ok"]:
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return 2
    path = lock_path or Path.cwd() / LOCK_NAME
    held = _lock(path)
    if held is None:
        print(
            json.dumps(
                {"ok": False, "reasons": ["already_running"], "would_start": False},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 3
    run = runner or default_runner
    pause = sleeper or time.sleep
    argv = child_argv(python)
    cycles = 0
    code = 0
    try:
        while True:
            proc = run(argv)
            code = int(proc.wait())
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return code
            pause(RESTART_DELAY_SEC)
    finally:
        held.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="live_bnb_boll_fade execution watchdog")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="print preflight JSON and do not spawn",
    )
    mode.add_argument(
        "--supervise",
        action="store_true",
        help="spawn the one live_bnb execution loop when preflight passes",
    )
    args = parser.parse_args(argv)
    if args.dry_run:
        report = preflight()
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return 0 if report["ok"] else 2
    return supervise()


if __name__ == "__main__":
    raise SystemExit(main())
