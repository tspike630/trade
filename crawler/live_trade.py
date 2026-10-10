#!/usr/bin/env python3
"""
Live trading adapter SKELETON — dry-run by default.

HARD DEFAULTS:
  LIVE_TRADING_ENABLED=0
  Kill-switch file LIVE_TRADING_DISABLED (present in repo)
  LIVE_TRADING_CONFIRM must equal I_UNDERSTAND_THE_RISKS for live place_order

  python live_trade.py intent --side buy --symbol DEMO --size-usd 10
  python live_trade.py status
  python live_trade.py checklist

Never stores secrets in the repo. API key/secret only from env; never logged.
Paper → small live checklist is printed by `checklist`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import (  # noqa: E402
    LIVE_EXCHANGE_API_KEY,
    LIVE_EXCHANGE_API_SECRET,
    LIVE_EXCHANGE_API_URL,
    LIVE_KILL_SWITCH_PATH,
    LIVE_MAX_DAILY_NOTIONAL_USD,
    LIVE_MAX_ORDER_USD,
    LIVE_TRADING_CONFIRM,
    LIVE_TRADING_CONFIRM_PHRASE,
    LIVE_TRADING_ENABLED,
    USER_AGENT,
)
from logging_setup import get_logger, setup_logging  # noqa: E402

log = get_logger("live_trade")

INTENT_LOG = Path("logs/live_trade_intents.jsonl")
DAILY_NOTIONAL_STATE = Path("exports/live_daily_notional.json")


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _redact(s: str) -> str:
    out = s
    for secret in (LIVE_EXCHANGE_API_KEY, LIVE_EXCHANGE_API_SECRET):
        if secret and secret in out:
            out = out.replace(secret, "***")
    return out


@dataclass
class OrderIntent:
    side: str  # buy|sell
    symbol: str
    size_usd: float
    chain_id: str = ""
    token_address: str = ""
    pair_address: str = ""
    dry_run: bool = True
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderResult:
    ok: bool
    dry_run: bool
    intent: OrderIntent
    exchange_order_id: str | None = None
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class ExchangeAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def place_order(self, intent: OrderIntent) -> OrderResult:
        ...

    @abstractmethod
    def health(self) -> dict[str, Any]:
        ...


class DryRunAdapter(ExchangeAdapter):
    """Always simulates fill; never hits network."""

    name = "dry_run"

    def place_order(self, intent: OrderIntent) -> OrderResult:
        intent.dry_run = True
        return OrderResult(
            ok=True,
            dry_run=True,
            intent=intent,
            exchange_order_id=f"dry-{_utc()}",
            message="dry-run simulated fill",
            raw={"simulated": True},
        )

    def health(self) -> dict[str, Any]:
        return {"adapter": self.name, "ok": True, "network": False}


class HttpStubAdapter(ExchangeAdapter):
    """
    Generic CEX/DEX-aggregator HTTP stub.
    POST {LIVE_EXCHANGE_API_URL}/orders with JSON body.
    Only used when live gates pass.
    """

    name = "http_stub"

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
    ):
        self.url = (url or LIVE_EXCHANGE_API_URL or "").rstrip("/")
        self.api_key = api_key if api_key is not None else LIVE_EXCHANGE_API_KEY
        self.api_secret = api_secret if api_secret is not None else LIVE_EXCHANGE_API_SECRET

    def health(self) -> dict[str, Any]:
        return {
            "adapter": self.name,
            "ok": bool(self.url),
            "url_set": bool(self.url),
            "key_set": bool(self.api_key),
            # never echo secrets
        }

    def place_order(self, intent: OrderIntent) -> OrderResult:
        if not self.url:
            return OrderResult(
                ok=False,
                dry_run=False,
                intent=intent,
                message="LIVE_EXCHANGE_API_URL unset",
            )
        headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key
        # secret may be HMAC in real adapters — stub sends header only if present
        if self.api_secret:
            headers["X-API-Secret"] = self.api_secret
        body = {
            "side": intent.side,
            "symbol": intent.symbol,
            "size_usd": intent.size_usd,
            "chain_id": intent.chain_id,
            "token_address": intent.token_address,
            "pair_address": intent.pair_address,
            "meta": intent.meta,
        }
        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.post(f"{self.url}/orders", json=body, headers=headers)
            if resp.status_code >= 400:
                return OrderResult(
                    ok=False,
                    dry_run=False,
                    intent=intent,
                    message=f"http_{resp.status_code}",
                    raw={"body": _redact(resp.text[:200])},
                )
            data = resp.json() if resp.content else {}
            oid = None
            if isinstance(data, dict):
                oid = data.get("id") or data.get("order_id")
            return OrderResult(
                ok=True,
                dry_run=False,
                intent=intent,
                exchange_order_id=str(oid) if oid else None,
                message="submitted",
                raw=data if isinstance(data, dict) else {},
            )
        except Exception as exc:  # noqa: BLE001
            return OrderResult(
                ok=False,
                dry_run=False,
                intent=intent,
                message=f"error:{type(exc).__name__}",
            )


def kill_switch_active() -> bool:
    path = Path(LIVE_KILL_SWITCH_PATH)
    return path.is_file()


def live_gates_open() -> tuple[bool, str]:
    """Return (allowed, reason)."""
    if not LIVE_TRADING_ENABLED:
        return False, "LIVE_TRADING_ENABLED=0 (hard default)"
    if LIVE_TRADING_CONFIRM != LIVE_TRADING_CONFIRM_PHRASE:
        return False, (
            f"LIVE_TRADING_CONFIRM must equal {LIVE_TRADING_CONFIRM_PHRASE!r}"
        )
    if kill_switch_active():
        return False, f"kill-switch file present: {LIVE_KILL_SWITCH_PATH}"
    if not LIVE_EXCHANGE_API_URL:
        return False, "LIVE_EXCHANGE_API_URL unset"
    return True, "ok"


def _load_daily_notional() -> dict[str, Any]:
    if not DAILY_NOTIONAL_STATE.is_file():
        return {"day": "", "notional": 0.0}
    try:
        return json.loads(DAILY_NOTIONAL_STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"day": "", "notional": 0.0}


def _save_daily_notional(state: dict[str, Any]) -> None:
    DAILY_NOTIONAL_STATE.parent.mkdir(parents=True, exist_ok=True)
    DAILY_NOTIONAL_STATE.write_text(
        json.dumps(state, indent=2) + "\n", encoding="utf-8"
    )


def check_size_caps(size_usd: float) -> tuple[bool, str]:
    if size_usd <= 0:
        return False, "size_usd must be > 0"
    if size_usd > LIVE_MAX_ORDER_USD:
        return False, f"size_usd {size_usd} > LIVE_MAX_ORDER_USD {LIVE_MAX_ORDER_USD}"
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    st = _load_daily_notional()
    if st.get("day") != day:
        st = {"day": day, "notional": 0.0}
    if float(st["notional"]) + size_usd > LIVE_MAX_DAILY_NOTIONAL_USD:
        return False, (
            f"daily notional would exceed LIVE_MAX_DAILY_NOTIONAL_USD="
            f"{LIVE_MAX_DAILY_NOTIONAL_USD}"
        )
    return True, "ok"


def record_notional(size_usd: float) -> None:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    st = _load_daily_notional()
    if st.get("day") != day:
        st = {"day": day, "notional": 0.0}
    st["notional"] = float(st["notional"]) + float(size_usd)
    _save_daily_notional(st)


def log_intent(intent: OrderIntent, result: OrderResult) -> None:
    INTENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": _utc(),
        "side": intent.side,
        "symbol": intent.symbol,
        "size_usd": intent.size_usd,
        "dry_run": result.dry_run,
        "ok": result.ok,
        "message": result.message,
        "order_id": result.exchange_order_id,
        "chain_id": intent.chain_id,
        "pair_address": intent.pair_address,
        # never include secrets
    }
    with INTENT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    log.info(
        "order_intent side=%s symbol=%s size_usd=%s dry_run=%s ok=%s msg=%s",
        intent.side,
        intent.symbol,
        intent.size_usd,
        result.dry_run,
        result.ok,
        result.message,
    )


def get_adapter(*, force_dry: bool = False) -> ExchangeAdapter:
    allowed, _reason = live_gates_open()
    if force_dry or not allowed:
        return DryRunAdapter()
    return HttpStubAdapter()


def submit_intent(intent: OrderIntent, *, force_dry: bool = False) -> OrderResult:
    ok_cap, cap_msg = check_size_caps(intent.size_usd)
    if not ok_cap:
        result = OrderResult(
            ok=False, dry_run=True, intent=intent, message=cap_msg
        )
        log_intent(intent, result)
        return result

    allowed, reason = live_gates_open()
    if force_dry or not allowed:
        intent.dry_run = True
        adapter = DryRunAdapter()
        result = adapter.place_order(intent)
        result.message = f"{result.message} | gate={reason}"
        log_intent(intent, result)
        return result

    # LIVE path
    intent.dry_run = False
    adapter = HttpStubAdapter()
    result = adapter.place_order(intent)
    log_intent(intent, result)
    if result.ok:
        record_notional(intent.size_usd)
    return result


def print_checklist() -> None:
    print(
        """
=== Paper → small live checklist ===
1. Run paper_trade.py demo / tick until equity behavior is understood.
2. backtest --strategy … on your universe; record win_rate / drawdown.
3. Keep LIVE_TRADING_ENABLED=0 and kill-switch file in place while testing intents:
     python live_trade.py intent --side buy --symbol TEST --size-usd 5
4. Confirm logs/live_trade_intents.jsonl shows dry_run=true only.
5. Set LIVE_MAX_ORDER_USD (e.g. 10–25) and LIVE_MAX_DAILY_NOTIONAL_USD low.
6. Configure LIVE_EXCHANGE_API_URL (+ KEY/SECRET via env only — never commit).
7. Remove/rename LIVE_TRADING_DISABLED kill-switch file only when ready.
8. export LIVE_TRADING_ENABLED=1
   export LIVE_TRADING_CONFIRM=I_UNDERSTAND_THE_RISKS
9. First live order: tiny size; watch exchange UI + intent log.
10. Any doubt → recreate kill-switch file or unset LIVE_TRADING_ENABLED.

This skeleton does NOT execute DEX swaps on-chain; HttpStubAdapter posts to your
configured aggregator HTTP API only when all gates pass.
""".strip()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live trade adapter (dry-run default)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("checklist")
    p_int = sub.add_parser("intent", help="Submit order intent (dry-run unless gates open)")
    p_int.add_argument("--side", choices=("buy", "sell"), required=True)
    p_int.add_argument("--symbol", required=True)
    p_int.add_argument("--size-usd", type=float, required=True)
    p_int.add_argument("--chain", default="")
    p_int.add_argument("--pair", default="")
    p_int.add_argument("--token", default="")
    p_int.add_argument(
        "--force-dry",
        action="store_true",
        help="Always dry-run even if live gates somehow open",
    )
    args = parser.parse_args(argv)
    setup_logging()

    if args.cmd == "checklist":
        print_checklist()
        return 0
    if args.cmd == "status":
        allowed, reason = live_gates_open()
        print(
            json.dumps(
                {
                    "live_trading_enabled_env": LIVE_TRADING_ENABLED,
                    "gates_open": allowed,
                    "gate_reason": reason,
                    "kill_switch_file": LIVE_KILL_SWITCH_PATH,
                    "kill_switch_active": kill_switch_active(),
                    "max_order_usd": LIVE_MAX_ORDER_USD,
                    "max_daily_notional_usd": LIVE_MAX_DAILY_NOTIONAL_USD,
                    "daily_notional": _load_daily_notional(),
                    "adapter_health": get_adapter().health(),
                    "confirm_phrase_required": LIVE_TRADING_CONFIRM_PHRASE,
                    "api_url_set": bool(LIVE_EXCHANGE_API_URL),
                    "api_key_set": bool(LIVE_EXCHANGE_API_KEY),
                },
                indent=2,
            )
        )
        return 0
    if args.cmd == "intent":
        intent = OrderIntent(
            side=args.side,
            symbol=args.symbol,
            size_usd=args.size_usd,
            chain_id=args.chain,
            pair_address=args.pair,
            token_address=args.token,
        )
        result = submit_intent(intent, force_dry=args.force_dry)
        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "dry_run": result.dry_run,
                    "message": result.message,
                    "order_id": result.exchange_order_id,
                },
                indent=2,
            )
        )
        return 0 if result.ok else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
