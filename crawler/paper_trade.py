#!/usr/bin/env python3
"""
Paper trading ledger — virtual positions from strategy params using history/live pair prices.

  python paper_trade.py init
  python paper_trade.py open --chain solana --pair PAIR --size-usd 100
  python paper_trade.py tick          # mark-to-market + apply TP/SL/hold exits
  python paper_trade.py status
  python paper_trade.py demo         # seed + open + tick on temp-friendly demo

Ledger: PAPER_DB_PATH (default paper_trading.db), separate from crawler DB.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import (  # noqa: E402
    DB_PATH,
    PAPER_DB_PATH,
    PAPER_START_CASH,
)
from execution.exits import scan_exits  # noqa: E402
from store import Store, norm_address, norm_chain  # noqa: E402


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PaperLedger:
    def __init__(self, path: str | Path = PAPER_DB_PATH):
        self.path = Path(path)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self._init()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "PaperLedger":
        return self

    def __exit__(self, *a: object) -> None:
        self.close()

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cash_usd REAL NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS positions (
                id TEXT PRIMARY KEY,
                chain_id TEXT NOT NULL,
                pair_address TEXT NOT NULL,
                token_address TEXT,
                qty REAL NOT NULL,
                entry_price REAL NOT NULL,
                size_usd REAL NOT NULL,
                opened_at TEXT NOT NULL,
                status TEXT NOT NULL,
                tp_pct REAL,
                sl_pct REAL,
                max_hold_min REAL,
                exit_price REAL,
                closed_at TEXT,
                exit_reason TEXT,
                pnl_usd REAL,
                meta_json TEXT
            );
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                qty REAL NOT NULL,
                notional_usd REAL NOT NULL,
                created_at TEXT NOT NULL,
                note TEXT
            );
            """
        )
        row = self.conn.execute("SELECT 1 FROM account WHERE id=1").fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO account (id, cash_usd, updated_at) VALUES (1, ?, ?)",
                (PAPER_START_CASH, _utc()),
            )
        self.conn.commit()

    def get_cash(self) -> float:
        return float(self.conn.execute("SELECT cash_usd FROM account WHERE id=1").fetchone()[0])

    def set_cash(self, cash: float) -> None:
        self.conn.execute(
            "UPDATE account SET cash_usd=?, updated_at=? WHERE id=1",
            (cash, _utc()),
        )
        self.conn.commit()

    def open_position(
        self,
        *,
        chain_id: str,
        pair_address: str,
        price: float,
        size_usd: float,
        token_address: str | None = None,
        tp_pct: float | None = 10.0,
        sl_pct: float | None = 5.0,
        max_hold_min: float | None = 60.0,
        meta: dict[str, Any] | None = None,
    ) -> str:
        chain_id = norm_chain(chain_id)
        pair_address = norm_address(pair_address)
        if price <= 0 or size_usd <= 0:
            raise ValueError("price/size must be > 0")
        cash = self.get_cash()
        if size_usd > cash:
            raise ValueError(f"insufficient cash {cash} < {size_usd}")
        qty = size_usd / price
        pid = str(uuid.uuid4())
        now = _utc()
        self.set_cash(cash - size_usd)
        self.conn.execute(
            """
            INSERT INTO positions (
                id, chain_id, pair_address, token_address, qty, entry_price, size_usd,
                opened_at, status, tp_pct, sl_pct, max_hold_min, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
            """,
            (
                pid,
                chain_id,
                pair_address,
                norm_address(token_address) if token_address else None,
                qty,
                price,
                size_usd,
                now,
                tp_pct,
                sl_pct,
                max_hold_min,
                json.dumps(meta or {}),
            ),
        )
        self.conn.execute(
            """
            INSERT INTO fills (position_id, side, price, qty, notional_usd, created_at, note)
            VALUES (?, 'buy', ?, ?, ?, ?, 'open')
            """,
            (pid, price, qty, size_usd, now),
        )
        self.conn.commit()
        return pid

    def close_position(
        self, pid: str, *, price: float, reason: str
    ) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
        if not row or row["status"] != "open":
            raise ValueError("position not open")
        qty = float(row["qty"])
        notional = qty * price
        pnl = notional - float(row["size_usd"])
        now = _utc()
        self.set_cash(self.get_cash() + notional)
        self.conn.execute(
            """
            UPDATE positions SET status='closed', exit_price=?, closed_at=?,
                   exit_reason=?, pnl_usd=? WHERE id=?
            """,
            (price, now, reason, pnl, pid),
        )
        self.conn.execute(
            """
            INSERT INTO fills (position_id, side, price, qty, notional_usd, created_at, note)
            VALUES (?, 'sell', ?, ?, ?, ?, ?)
            """,
            (pid, price, qty, notional, now, reason),
        )
        self.conn.commit()
        return {"id": pid, "pnl_usd": pnl, "exit_price": price, "reason": reason}

    def list_open(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM positions WHERE status='open' ORDER BY opened_at"
        ).fetchall()]

    def list_closed(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()]

    def equity(self, marks: dict[str, float] | None = None) -> dict[str, Any]:
        cash = self.get_cash()
        open_pos = self.list_open()
        mtm = 0.0
        for p in open_pos:
            key = f"{p['chain_id']}|{p['pair_address']}"
            px = (marks or {}).get(key, float(p["entry_price"]))
            mtm += float(p["qty"]) * px
        return {
            "cash_usd": cash,
            "positions_mtm_usd": mtm,
            "equity_usd": cash + mtm,
            "open_count": len(open_pos),
        }


def latest_price(store: Store, chain_id: str, pair_address: str) -> float | None:
    # prefer latest history, else pair raw
    hist = store.list_pair_history(chain_id, pair_address, limit=500)
    if hist:
        for p in reversed(hist):
            if p.get("price_usd") is not None:
                return float(p["price_usd"])
    raw = store.get_pair_raw(chain_id, pair_address)
    if raw and raw.get("priceUsd") is not None:
        try:
            return float(raw["priceUsd"])
        except (TypeError, ValueError):
            return None
    return None


def tick(
    ledger: PaperLedger,
    store: Store,
) -> list[dict[str, Any]]:
    """Mark open positions; close on TP/SL/max hold (unified exit rules)."""
    actions = []

    def price_lookup(p):
        return latest_price(store, p["chain_id"], p["pair_address"])

    for pos, reason in scan_exits(ledger.list_open(), price_lookup):
        px = price_lookup(pos)
        actions.append(ledger.close_position(pos["id"], price=px, reason=reason))
    return actions


def cmd_status(ledger: PaperLedger, store: Store) -> None:
    marks = {}
    for p in ledger.list_open():
        key = f"{p['chain_id']}|{p['pair_address']}"
        px = latest_price(store, p["chain_id"], p["pair_address"])
        if px is not None:
            marks[key] = px
    eq = ledger.equity(marks)
    print(json.dumps({"equity": eq, "open": ledger.list_open(), "closed": ledger.list_closed(10)}, indent=2, default=str))



def cmd_simulate_strategy(
    ledger: PaperLedger,
    store: Store,
    *,
    strategy: str,
    buy_after: float,
    hold: float,
    size_usd: float,
    tp: float,
    sl: float,
) -> None:
    """
    Run one backtest-style path on the richest history pair and mirror entry/exit
    into the paper ledger (virtual only).
    """
    import backtest as bt

    pairs = store._conn.execute(
        """
        SELECT chain_id, pair_address, COUNT(*) AS n FROM pair_history
        WHERE price_usd IS NOT NULL GROUP BY 1,2 HAVING n >= 4
        ORDER BY n DESC LIMIT 1
        """
    ).fetchall()
    if not pairs:
        print("SIM_FAIL: need pair_history (try backtest --seed-demo first)")
        return
    r = pairs[0]
    pts = store.list_pair_history(r["chain_id"], r["pair_address"], limit=500)
    for p in pts:
        p.setdefault("chain_id", r["chain_id"])
        p.setdefault("pair_address", r["pair_address"])
    if strategy == "dca":
        tr = bt.backtest_dca(pts, buy_after_min=buy_after, hold_min=hold)
    elif strategy == "martingale":
        tr = bt.backtest_martingale(pts, buy_after_min=buy_after, hold_min=hold)
    elif strategy == "trend":
        tr = bt.backtest_trend(pts, buy_after_min=buy_after, hold_min=hold)
    elif strategy == "tpsl":
        tr = bt.backtest_tpsl(pts, buy_after_min=buy_after, hold_min=hold, tp_pct=tp, sl_pct=sl)
    else:
        tr = bt.backtest_hold(pts, buy_after_min=buy_after, hold_min=hold)
    if not tr:
        print("SIM_FAIL: strategy produced no trade")
        return
    pid = ledger.open_position(
        chain_id=tr["chain_id"],
        pair_address=tr["pair_address"],
        price=float(tr["buy_price"]),
        size_usd=min(size_usd, ledger.get_cash()),
        tp_pct=tp,
        sl_pct=sl,
        max_hold_min=hold,
        meta={"sim_strategy": strategy, "backtest_return_pct": tr.get("return_pct")},
    )
    # close at backtest sell price with reason
    closed = ledger.close_position(
        pid, price=float(tr["sell_price"]), reason=f"sim_{tr.get('reason') or strategy}"
    )
    print(json.dumps({"opened": pid, "closed": closed, "backtest": tr}, indent=2, default=str))


def cmd_demo(ledger: PaperLedger, crawler_db: str) -> None:
    import backtest as bt

    with Store(crawler_db) as store:
        bt.seed_demo_history(store, n_pairs=2, points=16)
        # pick first seeded pair
        row = store._conn.execute(
            "SELECT chain_id, pair_address FROM pair_history WHERE pair_address LIKE 'btpair%' LIMIT 1"
        ).fetchone()
        if not row:
            print("DEMO_FAIL no seed pair")
            return
        chain, pair = row["chain_id"], row["pair_address"]
        # use early history price as entry
        pts = store.list_pair_history(chain, pair, limit=20)
        px = float(pts[1]["price_usd"]) if len(pts) > 1 else float(pts[0]["price_usd"])
        pid = ledger.open_position(
            chain_id=chain,
            pair_address=pair,
            price=px,
            size_usd=min(500.0, ledger.get_cash() * 0.1),
            tp_pct=8.0,
            sl_pct=4.0,
            max_hold_min=0.0,  # expire immediately so tick closes for demo
            meta={"demo": True},
        )
        print(f"opened {pid} @ {px}")
        # force a tick using later price
        actions = tick(ledger, store)
        print(f"tick_actions={actions}")
        cmd_status(ledger, store)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paper trading ledger")
    parser.add_argument("--paper-db", default=PAPER_DB_PATH)
    parser.add_argument("--db", default=DB_PATH, help="Crawler DB for prices")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Create ledger with starting cash")
    p_init.add_argument(
        "--cash",
        type=float,
        default=None,
        help="Starting cash (default PAPER_START_CASH env, often 10000; use 1000 for small books)",
    )
    p_init.add_argument(
        "--reset-cash",
        action="store_true",
        help="If ledger exists, set cash to --cash / PAPER_START_CASH",
    )
    p_open = sub.add_parser("open")
    p_open.add_argument("--chain", required=True)
    p_open.add_argument("--pair", required=True)
    p_open.add_argument("--size-usd", type=float, default=100.0)
    p_open.add_argument("--price", type=float, default=0.0, help="0 = use latest from crawler DB")
    p_open.add_argument("--tp", type=float, default=10.0)
    p_open.add_argument("--sl", type=float, default=5.0)
    p_open.add_argument("--max-hold", type=float, default=60.0)

    sub.add_parser("tick")
    sub.add_parser("status")
    sub.add_parser("demo")
    p_sim = sub.add_parser("simulate", help="Mirror backtest strategy into paper ledger")
    p_sim.add_argument("--strategy", choices=("hold", "tpsl", "dca", "martingale", "trend"), default="dca")
    p_sim.add_argument("--buy-after", type=float, default=5.0)
    p_sim.add_argument("--hold", type=float, default=30.0)
    p_sim.add_argument("--size-usd", type=float, default=100.0)
    p_sim.add_argument("--tp", type=float, default=10.0)
    p_sim.add_argument("--sl", type=float, default=5.0)

    args = parser.parse_args(argv)
    with PaperLedger(args.paper_db) as ledger:
        if args.cmd == "init":
            target = args.cash if args.cash is not None else PAPER_START_CASH
            if args.reset_cash or args.cash is not None:
                ledger.set_cash(float(target))
            print(f"PAPER_OK cash={ledger.get_cash()} db={args.paper_db} (PAPER_START_CASH={PAPER_START_CASH})")
            return 0
        with Store(args.db) as store:
            if args.cmd == "open":
                px = args.price or latest_price(store, args.chain, args.pair)
                if px is None:
                    print("FAIL: no price")
                    return 1
                pid = ledger.open_position(
                    chain_id=args.chain,
                    pair_address=args.pair,
                    price=px,
                    size_usd=args.size_usd,
                    tp_pct=args.tp,
                    sl_pct=args.sl,
                    max_hold_min=args.max_hold,
                )
                print(f"OPENED {pid} price={px} size={args.size_usd}")
                return 0
            if args.cmd == "tick":
                actions = tick(ledger, store)
                print(json.dumps({"closed": actions, "equity": ledger.equity()}, indent=2))
                return 0
            if args.cmd == "status":
                cmd_status(ledger, store)
                return 0
            if args.cmd == "demo":
                cmd_demo(ledger, args.db)
                return 0
            if args.cmd == "simulate":
                cmd_simulate_strategy(
                    ledger, store,
                    strategy=args.strategy,
                    buy_after=args.buy_after,
                    hold=args.hold,
                    size_usd=args.size_usd,
                    tp=args.tp,
                    sl=args.sl,
                )
                return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
