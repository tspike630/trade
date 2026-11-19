"""Explicit offline entry. Refuses --live and does not open a socket.

    python3 -m execution.margin_cross.entry --dry-run --fixture short-cycle --ledger /tmp/margin-cross-dry-run.db
    python3 -m execution.margin_cross.entry --show-proposal
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

from execution.margin_cross.api_semantics import EVIDENCE_LINKS, make_client_id
from execution.margin_cross.fixture import short_case
from execution.margin_cross.proposal import unapproved_threshold_proposal
from execution.margin_cross.track import CrossMarginTrack


class _SocketBlocked(RuntimeError):
    pass


def _install_network_guard() -> None:
    real = socket.socket

    class GuardedSocket(real):  # type: ignore[misc, valid-type]
        def connect(self, address: object, /) -> None:  # noqa: ANN001
            raise _SocketBlocked(f"dry-run blocked socket connect to {address!r}")

        def connect_ex(self, address: object, /) -> int:  # noqa: ANN001
            raise _SocketBlocked(f"dry-run blocked socket connect to {address!r}")

    socket.socket = GuardedSocket  # type: ignore[misc]


def run_short_cycle(ledger_path: str) -> dict[str, object]:
    case = short_case()
    track = CrossMarginTrack(ledger_path, case["client"])
    opened = track.open_position(case["intent"], case["snapshot"], case["contract"], case["gate"])
    list_id = make_client_id(str(case["intent"]["intent_id"]), "oco-list")
    case["client"].fill_oco_limit(list_id)
    closed = track.complete_exit(str(case["intent"]["intent_id"]), case["gate"])
    writes = [name for name, _payload in case["client"].write_log]
    return {
        "exchange_empirical": False,
        "mock_coverage": True,
        "parameters_approved": False,
        "production_halt_unchanged": True,
        "live_writes_blocked": True,
        "evidence": list(EVIDENCE_LINKS),
        "writes": writes,
        "open": opened.as_dict(),
        "close": closed.as_dict(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline cross-margin track entry")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--show-proposal", action="store_true")
    parser.add_argument("--fixture", choices=("short-cycle",))
    parser.add_argument("--ledger", default="")
    args = parser.parse_args(argv)
    if args.show_proposal:
        json.dump(unapproved_threshold_proposal(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.live:
        sys.stderr.write(
            "live cross-margin network writes are disabled; no order, borrow, or repay was sent\n"
        )
        return 2
    if not args.dry_run or args.fixture != "short-cycle" or not args.ledger:
        sys.stderr.write("use --dry-run --fixture short-cycle --ledger PATH, or --show-proposal\n")
        return 2
    _install_network_guard()
    Path(args.ledger).parent.mkdir(parents=True, exist_ok=True)
    report = run_short_cycle(args.ledger)
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    close = report["close"]
    if not isinstance(close, dict):
        return 1
    if close.get("state") == "clean_flat" and close.get("phase_ok") is True:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
