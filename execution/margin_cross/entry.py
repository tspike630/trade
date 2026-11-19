"""Cross-margin CLI entry supporting offline dry-run, 5-state dry-run, and gated --live.

Examples:
    python3 -m execution.margin_cross.entry --show-proposal
    python3 -m execution.margin_cross.entry --dry-run --fixture short-cycle --ledger /tmp/cm.db
    python3 -m execution.margin_cross.entry --live --action preflight --ledger /tmp/cm-live.db --signal-json /tmp/sig.json

``--live`` stays fail-closed (exit code 2 with ``disabled`` on stderr) unless:
1. ``LIVE_TRADING_DISABLED`` kill-switch (env and file) is absent (for new opens;
   ``complete-exit`` remains allowed unless ``CROSS_MARGIN_BLOCK_EXIT_ON_KILL=1``),
2. ``CROSS_MARGIN_PARAMETERS_APPROVED=1``,
3. ``CROSS_MARGIN_LIVE_ENABLED=1``,
4. ``BINANCE_API_KEY`` & ``BINANCE_API_SECRET`` (or ``BINANCE_MARGIN_*``) are set.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import socket
import sys
import time
from typing import Any

from execution.margin_cross.api_semantics import EVIDENCE_LINKS, make_client_id
from execution.margin_cross.fixture import client_from_snapshot, short_case
from execution.margin_cross.http_client import (
    BinanceCrossMarginHttpClient,
    Opener,
    fetch_live_cross_margin_snapshot,
    resolve_api_credentials,
)
from execution.margin_cross.policy import (
    APPROVED_DRY_RUN_MODE,
    LIVE_EXECUTION_MODE,
    TERMINAL_BORROW_REJECT_CODES,
    kill_switch_active,
    live_gate_reasons,
    load_approved_risk_config,
    parameters_approved_by_env,
)
from execution.margin_cross.proposal import unapproved_threshold_proposal
from execution.margin_cross.strategy_gate import (
    ApprovedPositionPlan,
    build_approved_position_plan,
)
from execution.margin_cross.track import CrossMarginTrack


class _SocketBlocked(RuntimeError):
    pass


def _install_network_guard() -> None:
    real = socket.socket
    if getattr(real, "_cm_guarded", False):
        return

    class GuardedSocket(real):  # type: ignore[misc, valid-type]
        _cm_guarded = True

        def connect(self, address: object, /) -> None:  # noqa: ANN001
            raise _SocketBlocked(f"dry-run blocked socket connect to {address!r}")

        def connect_ex(self, address: object, /) -> int:  # noqa: ANN001
            raise _SocketBlocked(f"dry-run blocked socket connect to {address!r}")

    socket.socket = GuardedSocket  # type: ignore[misc]


def open_offline_routed(ledger_path: str, signal: dict[str, Any]) -> dict[str, Any]:
    """Open one offline position through ``CrossMarginTrack``.

    The caller has already chosen the account domain. This does not read
    production environment switches and does not open a socket.
    """
    client = signal.get("client")
    intent = signal.get("intent")
    snapshot = signal.get("snapshot")
    contract = signal.get("contract")
    gate = signal.get("gate")
    if not isinstance(intent, dict) or not isinstance(snapshot, dict) or not isinstance(contract, dict):
        return {
            "ok": False,
            "reason": "offline_payload_missing",
            "entry": "execution.margin_cross.entry.open_offline_routed",
            "margin_called": False,
        }
    if client is None:
        interest = signal.get("interest_on_borrow", "0")
        client = client_from_snapshot(snapshot, Decimal(str(interest)))
    track = CrossMarginTrack(ledger_path, client)
    opened = track.open_position(intent, snapshot, contract, gate if isinstance(gate, dict) else {})
    writes: list[str] = []
    write_log = getattr(client, "write_log", None)
    if isinstance(write_log, list):
        writes = [str(name) for name, _payload in write_log]
    phase_ok = bool(opened.phase_ok)
    return {
        "ok": phase_ok,
        "reason": None if phase_ok else opened.reason,
        "entry": "execution.margin_cross.entry.open_offline_routed",
        "margin_called": True,
        "open": opened.as_dict(),
        "writes": writes,
    }


def run_short_cycle(ledger_path: str) -> dict[str, object]:
    case = short_case()
    track = CrossMarginTrack(ledger_path, case["client"])
    opened = track.open_position(
        case["intent"], case["snapshot"], case["contract"], case["gate"]
    )
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


def stable_decision_intent_id(decision_log_id: object) -> str | None:
    """One id per decision row. The same row always binds the same intent."""
    if isinstance(decision_log_id, bool) or decision_log_id in (None, ""):
        return None
    text = str(decision_log_id).strip()
    if not text.isdigit():
        return None
    number = int(text)
    if number <= 0:
        return None
    return f"dl-{number}"


def bind_document_intent_id(doc: dict[str, Any]) -> dict[str, Any]:
    """Keep an explicit intent id. Otherwise bind the decision row, or leave it absent."""
    out = dict(doc)
    current = out.get("intent_id")
    if isinstance(current, str) and current.strip():
        out["intent_id"] = current.strip()
        return out
    bound = stable_decision_intent_id(out.get("decision_log_id"))
    if bound:
        out["intent_id"] = bound
    else:
        out.pop("intent_id", None)
    return out


def _refused_plan(reason: str) -> ApprovedPositionPlan:
    return ApprovedPositionPlan(
        allowed=False,
        reasons=(reason,),
        regime="blocked",
        strategy_id="",
        net_asset_quote=Decimal(0),
        effective_leverage_after=Decimal(0),
        target_notional_quote=Decimal(0),
        quantity=Decimal(0),
        intent=None,
        contract=None,
    )


def _snapshot_failure_payload(action: str, reason: str) -> dict[str, Any]:
    """Name the snapshot sub-call. A terminal borrow code is not a retry."""
    payload: dict[str, Any] = {
        "mode": LIVE_EXECUTION_MODE,
        "action": action,
        "ok": False,
        "reason": reason,
        "borrow_retried": False,
    }
    parts = str(reason).split(":")
    if len(parts) >= 2 and parts[0] == "live_snapshot_exchange_error":
        try:
            code = int(parts[1])
        except ValueError:
            code = None
        else:
            payload["exchange_code"] = code
            if code in TERMINAL_BORROW_REJECT_CODES:
                payload["terminal_borrow_reject"] = True
        if len(parts) >= 3 and parts[2]:
            payload["snapshot_call"] = parts[2]
    return payload


def _plan_from_signal_doc(
    doc: dict[str, Any],
    snapshot_raw: dict[str, Any],
    now_ms: int,
):
    doc = bind_document_intent_id(doc)
    missing = [
        key
        for key in ("intent_id", "action", "take_profit_price", "stop_price", "stop_limit_price")
        if doc.get(key) in (None, "")
    ]
    if missing:
        return _refused_plan("order_fields_missing:" + ",".join(missing))
    symbol = str(doc["symbol"])
    base_asset = str(doc["base_asset"])
    quote_asset = str(doc.get("quote_asset") or "USDT")
    requested_action = str(doc["action"])
    entry_tier = Decimal(str(doc.get("entry_tier") or "1"))
    limit_px_raw = doc.get("limit_price")
    limit_px = None if limit_px_raw in (None, "") else Decimal(str(limit_px_raw))
    return build_approved_position_plan(
        intent_id=str(doc["intent_id"]),
        symbol=symbol,
        base_asset=base_asset,
        quote_asset=quote_asset,
        requested_action=requested_action,
        snapshot_raw=snapshot_raw,
        market_row=dict(doc.get("market_row") or {}),
        clocks=dict(doc.get("clocks") or {}),
        now_ms=now_ms,
        entry_tier=entry_tier,
        limit_price=limit_px,
        planned_take_profit=Decimal(str(doc["take_profit_price"])),
        stop_price=Decimal(str(doc["stop_price"])),
        stop_limit_price=Decimal(str(doc["stop_limit_price"])),
        risk_config=doc.get("approved_risk_config"),
        macro_halt=doc.get("macro_halt"),
    )


def run_strategy_dry_run(ledger_path: str, signal_path: str) -> tuple[int, dict[str, Any]]:
    doc = json.loads(Path(signal_path).read_text(encoding="utf-8"))
    now_ms = int(doc.get("now_ms") or int(time.time() * 1000))
    snapshot_raw = dict(doc["snapshot"])
    plan = _plan_from_signal_doc(doc, snapshot_raw, now_ms)
    if not plan.allowed or plan.intent is None or plan.contract is None:
        return 1, {
            "mode": APPROVED_DRY_RUN_MODE,
            "allowed": False,
            "reasons": list(plan.reasons),
            "regime": plan.regime,
            "strategy_id": plan.strategy_id,
        }
    client = client_from_snapshot(snapshot_raw, Decimal(str(doc.get("interest_on_borrow") or "0")))
    track = CrossMarginTrack(ledger_path, client)
    gate = {
        "emergency_stop": bool(doc.get("emergency_stop", False)),
        "now_ms": now_ms,
        "execution_mode": APPROVED_DRY_RUN_MODE,
    }
    opened = track.open_position(plan.intent, snapshot_raw, plan.contract, gate)
    return (0 if opened.phase_ok else 1), {
        "mode": APPROVED_DRY_RUN_MODE,
        "allowed": True,
        "regime": plan.regime,
        "strategy_id": plan.strategy_id,
        "quantity": str(plan.quantity),
        "open": opened.as_dict(),
    }


def run_live_command(
    *,
    ledger_path: str,
    action: str,
    signal_path: str = "",
    intent_id: str = "",
    kill_switch_path: str | None = None,
    base_url: str | None = None,
    opener: Opener | None = None,
    clock_ms: Any = None,
) -> tuple[int, dict[str, Any] | None, str]:
    """CLI wrapper. The document is read here; gates stay in ``open_live_document``."""
    doc = None
    if signal_path:
        doc = json.loads(Path(signal_path).read_text(encoding="utf-8"))
    return open_live_document(
        doc,
        ledger_path=ledger_path,
        action=action,
        intent_id=intent_id,
        kill_switch_path=kill_switch_path,
        base_url=base_url,
        opener=opener,
        clock_ms=clock_ms,
    )


def open_live_document(
    doc: dict[str, Any] | None,
    *,
    ledger_path: str,
    action: str,
    intent_id: str = "",
    kill_switch_path: str | None = None,
    base_url: str | None = None,
    opener: Opener | None = None,
    clock_ms: Any = None,
) -> tuple[int, dict[str, Any] | None, str]:
    """Gated live open. Approval is loaded from the pinned file, not from ``doc``."""
    now_fn = clock_ms or (lambda: int(time.time() * 1000))
    now_ms = int(now_fn())
    for_exit = action == "complete-exit"
    reasons = live_gate_reasons(
        allow_live_network=True,
        for_protective_or_repay=for_exit,
        explicit_kill_path=kill_switch_path,
        now_ms=now_ms,
    )
    if reasons:
        return (
            2,
            None,
            f"live cross-margin network writes are disabled ({reasons[0]}); no order, borrow, or repay was sent\n",
        )
    if not ledger_path:
        return (
            2,
            None,
            "live cross-margin disabled: --ledger PATH is required\n",
        )
    api_key, api_secret = resolve_api_credentials()
    if not api_key or not api_secret:
        return (
            2,
            None,
            "live cross-margin disabled: missing API credentials; no order, borrow, or repay was sent\n",
        )

    pinned_cfg, cfg_errs = load_approved_risk_config(now_ms=now_ms)
    if pinned_cfg is None or cfg_errs:
        err_code = cfg_errs[0] if cfg_errs else "approved_config_missing"
        return (
            2,
            None,
            f"live cross-margin network writes are disabled ({err_code}); no order, borrow, or repay was sent\n",
        )
    client = BinanceCrossMarginHttpClient(
        api_key=api_key,
        api_secret=api_secret,
        account_uid=pinned_cfg.approved_account_uid,
        base_url=base_url,
        opener=opener,
        clock_ms=now_fn,
        kill_switch_path=kill_switch_path,
        approved_config=pinned_cfg,
    )
    track = CrossMarginTrack(
        ledger_path,
        client,
        allow_live_network=True,
        kill_switch_path=kill_switch_path,
        approved_config=pinned_cfg,
        now_ms=now_ms,
    )
    kill_on, _kill_name = kill_switch_active(kill_switch_path)

    if action == "complete-exit":
        if not intent_id:
            return 2, None, "live cross-margin complete-exit requires --intent-id\n"
        gate = {
            "emergency_stop": kill_on,
            "now_ms": now_ms,
            "execution_mode": LIVE_EXECUTION_MODE,
        }
        closed = track.complete_exit(intent_id, gate)
        code = 0 if closed.phase_ok else 1
        return code, {"mode": LIVE_EXECUTION_MODE, "action": action, "result": closed.as_dict()}, ""

    if not doc:
        return 2, None, "live cross-margin preflight/open requires --signal-json PATH\n"
    doc = bind_document_intent_id(doc)
    symbol = str(doc["symbol"])
    base_asset = str(doc["base_asset"])
    quote_asset = str(doc.get("quote_asset") or "USDT")
    borrow_asset = base_asset
    snapshot_raw, snap_err = fetch_live_cross_margin_snapshot(
        client,
        symbol=symbol,
        base_asset=base_asset,
        quote_asset=quote_asset,
        borrow_asset=borrow_asset,
        expected_uid=doc.get("expected_uid"),
        fee_rate=str(doc.get("fee_rate") or "0.001"),
        fee_asset=str(doc.get("fee_asset") or "USDT"),
        observed_at_ms=now_ms,
    )
    if snap_err or snapshot_raw is None:
        return 1, _snapshot_failure_payload(action, str(snap_err or "snapshot_missing")), ""

    existing_row = (
        track.ledger.intent_row(str(doc.get("intent_id") or ""))
        if action == "open"
        else None
    )
    if existing_row is not None:
        stored_doc = json.loads(str(existing_row["payload_json"]))
        st_intent = dict(stored_doc["intent"])
        if (
            str(doc.get("symbol") or "") != str(st_intent.get("symbol") or "")
            or str(doc.get("action") or "") != str(st_intent.get("action") or "")
            or str(doc.get("base_asset") or "") != str(st_intent.get("base_asset") or "")
        ):
            return (
                1,
                {
                    "mode": LIVE_EXECUTION_MODE,
                    "action": "open",
                    "ok": False,
                    "reason": "intent_identity_conflict",
                },
                "",
            )
        gate = {
            "emergency_stop": kill_on,
            "now_ms": now_ms,
            "execution_mode": LIVE_EXECUTION_MODE,
        }
        resumed = track.open_position(
            st_intent,
            dict(stored_doc["snapshot"]),
            dict(stored_doc["contract"]),
            gate,
        )
        return (
            0 if resumed.phase_ok else 1,
            {"mode": LIVE_EXECUTION_MODE, "action": "open", "result": resumed.as_dict()},
            "",
        )

    merged_doc = dict(doc)
    clocks = dict(merged_doc.get("clocks") or {})
    clocks["snapshot_observed_at_ms"] = snapshot_raw["observed_at_ms"]
    merged_doc["clocks"] = clocks
    plan = _plan_from_signal_doc(merged_doc, snapshot_raw, now_ms)
    if not plan.allowed or plan.intent is None or plan.contract is None:
        return (
            1,
            {
                "mode": LIVE_EXECUTION_MODE,
                "action": action,
                "ok": False,
                "reasons": list(plan.reasons),
                "regime": plan.regime,
            },
            "",
        )

    if action == "preflight":
        return (
            0,
            {
                "mode": LIVE_EXECUTION_MODE,
                "action": "preflight",
                "ok": True,
                "parameters_approved": parameters_approved_by_env(),
                "regime": plan.regime,
                "strategy_id": plan.strategy_id,
                "intent": plan.intent,
                "contract": plan.contract,
            },
            "",
        )

    gate = {
        "emergency_stop": kill_on,
        "now_ms": now_ms,
        "execution_mode": LIVE_EXECUTION_MODE,
    }
    opened = track.open_position(plan.intent, snapshot_raw, plan.contract, gate)
    code = 0 if opened.phase_ok else 1
    return code, {"mode": LIVE_EXECUTION_MODE, "action": "open", "result": opened.as_dict()}, ""


_MAINTAIN_SKIP_STATES = frozenset(
    {
        "clean_flat",
        "not_flat",
        "protection_failed_flattened",
        "entry_rejected_unwound",
        "risk_dropped_unwound",
        "emergency_unwound",
        "locked",
        "borrow_rejected",
        "entry_rejected",
        "otoco_not_submitted",
        "live_network_disabled",
    }
)


def maintain_live_margin(ledger_path: str | None = None) -> list[dict[str, Any]]:
    """One client and one track for an explicit ledger.

    Unset ``CROSS_MARGIN_LEDGER_PATH`` does nothing. A missing approval,
    credential, or live switch returns one refusal and does not open a socket
    through the default transport until those gates pass. An unknown OCO read
    is returned as-is and is not followed by another order.
    """
    path = (ledger_path or os.environ.get("CROSS_MARGIN_LEDGER_PATH") or "").strip()
    if not path:
        return []
    now_ms = int(time.time() * 1000)
    reasons = live_gate_reasons(
        allow_live_network=True,
        for_protective_or_repay=True,
        now_ms=now_ms,
    )
    if reasons:
        return [
            {
                "ok": False,
                "reason": reasons[0],
                "margin_called": False,
                "entry": "execution.margin_cross.entry.maintain_live_margin",
            }
        ]
    pinned_cfg, cfg_errs = load_approved_risk_config(now_ms=now_ms)
    if pinned_cfg is None or cfg_errs:
        return [
            {
                "ok": False,
                "reason": cfg_errs[0] if cfg_errs else "approved_config_missing",
                "margin_called": False,
                "entry": "execution.margin_cross.entry.maintain_live_margin",
            }
        ]
    api_key, api_secret = resolve_api_credentials()
    if not api_key or not api_secret:
        return [
            {
                "ok": False,
                "reason": "missing_api_credentials",
                "margin_called": False,
                "entry": "execution.margin_cross.entry.maintain_live_margin",
            }
        ]
    client = BinanceCrossMarginHttpClient(
        api_key=api_key,
        api_secret=api_secret,
        account_uid=pinned_cfg.approved_account_uid,
        clock_ms=lambda: now_ms,
        approved_config=pinned_cfg,
    )
    track = CrossMarginTrack(
        path,
        client,
        allow_live_network=True,
        approved_config=pinned_cfg,
        now_ms=now_ms,
    )
    kill_on, _name = kill_switch_active(None)
    gate = {
        "emergency_stop": kill_on,
        "now_ms": now_ms,
        "execution_mode": LIVE_EXECUTION_MODE,
    }
    out: list[dict[str, Any]] = []
    for row in track.ledger.list_intents():
        state = str(row["state"])
        if state in _MAINTAIN_SKIP_STATES:
            continue
        intent_id = str(row["intent_id"])
        closed = track.complete_exit(intent_id, gate)
        if closed.reason == "protection_not_placed":
            stored = json.loads(str(row["payload_json"]))
            opened = track.open_position(
                stored["intent"], stored["snapshot"], stored["contract"], gate
            )
            out.append(
                {
                    "action": "maintain_open",
                    "entry": "execution.margin_cross.entry.maintain_live_margin",
                    "result": opened.as_dict(),
                }
            )
            continue
        out.append(
            {
                "action": "maintain_exit",
                "entry": "execution.margin_cross.entry.maintain_live_margin",
                "result": closed.as_dict(),
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cross-margin track CLI entry")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--show-proposal", action="store_true")
    parser.add_argument("--fixture", choices=("short-cycle",))
    parser.add_argument("--strategy-input", default="")
    parser.add_argument(
        "--action",
        choices=("preflight", "open", "complete-exit"),
        default="preflight",
    )
    parser.add_argument("--signal-json", default="")
    parser.add_argument("--intent-id", default="")
    parser.add_argument("--ledger", default="")
    parser.add_argument("--kill-switch-path", default="")
    parser.add_argument("--api-base", default="")
    args = parser.parse_args(argv)

    if args.show_proposal:
        json.dump(unapproved_threshold_proposal(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if args.live:
        code, payload, err_msg = run_live_command(
            ledger_path=args.ledger,
            action=args.action,
            signal_path=args.signal_json,
            intent_id=args.intent_id,
            kill_switch_path=args.kill_switch_path or None,
            base_url=args.api_base or None,
        )
        if err_msg:
            sys.stderr.write(err_msg)
        if payload is not None:
            json.dump(payload, sys.stdout, indent=2)
            sys.stdout.write("\n")
        return code

    if args.dry_run and args.strategy_input and args.ledger:
        _install_network_guard()
        Path(args.ledger).parent.mkdir(parents=True, exist_ok=True)
        code, report = run_strategy_dry_run(args.ledger, args.strategy_input)
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return code

    if not args.dry_run or args.fixture != "short-cycle" or not args.ledger:
        sys.stderr.write(
            "use --dry-run --fixture short-cycle --ledger PATH, --dry-run --strategy-input JSON --ledger PATH, or --show-proposal\n"
        )
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
