#!/usr/bin/env python3
"""
Multi-strategy backtest on pair_history.

Strategies:
  hold  — buy after X min, sell after hold min (default)
  tpsl  — buy after X min; exit on take-profit / stop-loss / max hold
  grid  — simple grid: buy dips / sell rips within a band after entry

  python backtest.py --buy-after 5 --hold 30
  python backtest.py --strategy tpsl --buy-after 5 --hold 60 --tp 10 --sl 5 --seed-demo
  python backtest.py --strategy grid --buy-after 5 --hold 60 --grid-step 3 --grid-levels 3 --seed-demo
  python backtest.py --strategy dca|martingale|trend --seed-demo
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import DB_PATH  # noqa: E402
from store import Store  # noqa: E402


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None


def _ordered(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for p in sorted(points, key=lambda x: x.get("recorded_at") or ""):
        if p.get("price_usd") is None:
            continue
        try:
            float(p["price_usd"])
        except (TypeError, ValueError):
            continue
        out.append(p)
    return out


def _nearest(points: list[dict[str, Any]], target: datetime) -> dict[str, Any] | None:
    best = None
    best_abs = None
    for p in points:
        t = _parse_iso(p.get("recorded_at"))
        if not t:
            continue
        d = abs((t - target).total_seconds())
        if best_abs is None or d < best_abs:
            best_abs = d
            best = p
    if best is None or best_abs is None or best_abs > 3600:
        return None
    return best


def _points_after(points: list[dict[str, Any]], t0: datetime) -> list[dict[str, Any]]:
    out = []
    for p in points:
        t = _parse_iso(p.get("recorded_at"))
        if t and t >= t0:
            out.append(p)
    return out


def backtest_hold(
    points: list[dict[str, Any]],
    *,
    buy_after_min: float,
    hold_min: float,
) -> dict[str, Any] | None:
    ordered = _ordered(points)
    if len(ordered) < 2:
        return None
    t0 = _parse_iso(ordered[0]["recorded_at"])
    if not t0:
        return None
    buy = _nearest(ordered, t0 + timedelta(minutes=buy_after_min))
    sell = _nearest(ordered, t0 + timedelta(minutes=buy_after_min + hold_min))
    if not buy or not sell:
        return None
    bp, sp = float(buy["price_usd"]), float(sell["price_usd"])
    if bp <= 0:
        return None
    ret = (sp - bp) / bp
    return _trade_dict(ordered[0], buy, sell, bp, sp, ret, reason="hold_expiry")


def backtest_tpsl(
    points: list[dict[str, Any]],
    *,
    buy_after_min: float,
    hold_min: float,
    tp_pct: float,
    sl_pct: float,
) -> dict[str, Any] | None:
    ordered = _ordered(points)
    if len(ordered) < 2:
        return None
    t0 = _parse_iso(ordered[0]["recorded_at"])
    if not t0:
        return None
    buy = _nearest(ordered, t0 + timedelta(minutes=buy_after_min))
    if not buy:
        return None
    bp = float(buy["price_usd"])
    if bp <= 0:
        return None
    buy_t = _parse_iso(buy["recorded_at"])
    assert buy_t
    deadline = buy_t + timedelta(minutes=hold_min)
    tp_px = bp * (1 + tp_pct / 100.0)
    sl_px = bp * (1 - sl_pct / 100.0)
    sell = None
    reason = "hold_expiry"
    for p in _points_after(ordered, buy_t):
        if p is buy or p["recorded_at"] == buy["recorded_at"]:
            continue
        px = float(p["price_usd"])
        t = _parse_iso(p["recorded_at"])
        if t and t > deadline:
            sell = _nearest(ordered, deadline) or p
            reason = "hold_expiry"
            break
        if px >= tp_px:
            sell = p
            reason = "take_profit"
            break
        if px <= sl_px:
            sell = p
            reason = "stop_loss"
            break
    if sell is None:
        sell = ordered[-1]
        reason = "series_end"
    sp = float(sell["price_usd"])
    ret = (sp - bp) / bp
    return _trade_dict(ordered[0], buy, sell, bp, sp, ret, reason=reason)


def backtest_grid(
    points: list[dict[str, Any]],
    *,
    buy_after_min: float,
    hold_min: float,
    grid_step_pct: float,
    grid_levels: int,
) -> dict[str, Any] | None:
    """
    Simple grid PnL approximation: enter at buy_after; over hold window,
    count fills at ±k*step from entry mid; net return ≈ (sells - buys) * step.
    Uses path of prices to count how many step levels were touched downward (buys)
    vs upward (sells), then marks to market at end.
    """
    ordered = _ordered(points)
    if len(ordered) < 2:
        return None
    t0 = _parse_iso(ordered[0]["recorded_at"])
    if not t0:
        return None
    entry = _nearest(ordered, t0 + timedelta(minutes=buy_after_min))
    if not entry:
        return None
    ep = float(entry["price_usd"])
    if ep <= 0 or grid_step_pct <= 0 or grid_levels < 1:
        return None
    entry_t = _parse_iso(entry["recorded_at"])
    assert entry_t
    deadline = entry_t + timedelta(minutes=hold_min)
    step = grid_step_pct / 100.0
    buy_levels_hit = set()
    sell_levels_hit = set()
    last = entry
    for p in _points_after(ordered, entry_t):
        t = _parse_iso(p["recorded_at"])
        if t and t > deadline:
            break
        last = p
        px = float(p["price_usd"])
        for k in range(1, grid_levels + 1):
            if px <= ep * (1 - k * step):
                buy_levels_hit.add(k)
            if px >= ep * (1 + k * step):
                sell_levels_hit.add(k)
    # Inventory: +1 per buy level, -1 per sell level; mark last price
    inv = len(buy_levels_hit) - len(sell_levels_hit)
    # Realized: each completed round-trip level min(buy,sell) earns ~step
    rounds = min(len(buy_levels_hit), len(sell_levels_hit))
    realized = rounds * grid_step_pct  # percent
    # Unrealized on leftover inventory
    lp = float(last["price_usd"])
    unreal = inv * ((lp - ep) / ep) * 100.0
    ret = realized + unreal
    return {
        "chain_id": entry.get("chain_id") or ordered[0].get("chain_id"),
        "pair_address": entry.get("pair_address") or ordered[0].get("pair_address"),
        "buy_at": entry["recorded_at"],
        "sell_at": last["recorded_at"],
        "buy_price": ep,
        "sell_price": lp,
        "return_pct": round(ret, 4),
        "win": ret > 0,
        "reason": "grid",
        "grid_buys": sorted(buy_levels_hit),
        "grid_sells": sorted(sell_levels_hit),
        "grid_rounds": rounds,
        "inventory": inv,
    }


def _trade_dict(first, buy, sell, bp, sp, ret, *, reason: str) -> dict[str, Any]:
    return {
        "chain_id": buy.get("chain_id") or first.get("chain_id"),
        "pair_address": buy.get("pair_address") or first.get("pair_address"),
        "buy_at": buy["recorded_at"],
        "sell_at": sell["recorded_at"],
        "buy_price": bp,
        "sell_price": sp,
        "return_pct": round(ret * 100, 4),
        "win": ret > 0,
        "reason": reason,
    }



def backtest_dca(
    points: list[dict[str, Any]],
    *,
    buy_after_min: float,
    hold_min: float,
    dca_steps: int = 3,
    dca_interval_min: float = 10.0,
) -> dict[str, Any] | None:
    """Equal-weight DCA buys over dca_steps, exit at hold after last buy."""
    ordered = _ordered(points)
    if len(ordered) < 2:
        return None
    t0 = _parse_iso(ordered[0]["recorded_at"])
    if not t0:
        return None
    buys = []
    for i in range(max(1, dca_steps)):
        b = _nearest(
            ordered,
            t0 + timedelta(minutes=buy_after_min + i * dca_interval_min),
        )
        if b:
            buys.append(b)
    if not buys:
        return None
    prices = [float(b["price_usd"]) for b in buys if float(b["price_usd"]) > 0]
    if not prices:
        return None
    avg = sum(prices) / len(prices)
    last_buy_t = _parse_iso(buys[-1]["recorded_at"])
    assert last_buy_t
    sell = _nearest(ordered, last_buy_t + timedelta(minutes=hold_min)) or ordered[-1]
    sp = float(sell["price_usd"])
    ret = (sp - avg) / avg
    tr = _trade_dict(ordered[0], buys[0], sell, avg, sp, ret, reason="dca_exit")
    tr["dca_fills"] = len(prices)
    tr["dca_avg_entry"] = avg
    return tr


def backtest_martingale(
    points: list[dict[str, Any]],
    *,
    buy_after_min: float,
    hold_min: float,
    martingale_max_adds: int = 3,
    martingale_drop_pct: float = 5.0,
) -> dict[str, Any] | None:
    """
    Capped martingale: initial unit at buy_after; on each drop_pct from last fill,
    double size (capped adds). Exit at hold after first entry window end.
    """
    ordered = _ordered(points)
    if len(ordered) < 2:
        return None
    t0 = _parse_iso(ordered[0]["recorded_at"])
    if not t0:
        return None
    first = _nearest(ordered, t0 + timedelta(minutes=buy_after_min))
    if not first:
        return None
    fills: list[tuple[float, float]] = []  # (price, units)
    units = 1.0
    fills.append((float(first["price_usd"]), units))
    last_px = float(first["price_usd"])
    first_t = _parse_iso(first["recorded_at"])
    assert first_t
    deadline = first_t + timedelta(minutes=hold_min)
    adds = 0
    for p in _points_after(ordered, first_t):
        t = _parse_iso(p["recorded_at"])
        if t and t > deadline:
            break
        px = float(p["price_usd"])
        if adds < martingale_max_adds and last_px > 0:
            drop = (last_px - px) / last_px * 100.0
            if drop >= martingale_drop_pct:
                units *= 2.0
                fills.append((px, units))
                last_px = px
                adds += 1
    # weighted avg entry
    tot_u = sum(u for _, u in fills)
    if tot_u <= 0:
        return None
    avg = sum(px * u for px, u in fills) / tot_u
    sell = _nearest(ordered, deadline) or ordered[-1]
    sp = float(sell["price_usd"])
    ret = (sp - avg) / avg
    tr = _trade_dict(ordered[0], first, sell, avg, sp, ret, reason="martingale_exit")
    tr["martingale_adds"] = adds
    tr["martingale_units"] = tot_u
    return tr


def backtest_trend(
    points: list[dict[str, Any]],
    *,
    buy_after_min: float,
    hold_min: float,
    trend_lookback: int = 3,
) -> dict[str, Any] | None:
    """
    Trend-follow: after buy_after, enter when last `lookback` returns are net positive;
    exit on hold or when lookback turns negative.
    """
    ordered = _ordered(points)
    if len(ordered) < trend_lookback + 2:
        return None
    t0 = _parse_iso(ordered[0]["recorded_at"])
    if not t0:
        return None
    start = _nearest(ordered, t0 + timedelta(minutes=buy_after_min))
    if not start:
        return None
    # find index of start
    try:
        idx = next(
            i for i, p in enumerate(ordered) if p["recorded_at"] == start["recorded_at"]
        )
    except StopIteration:
        return None
    if idx < trend_lookback:
        idx = trend_lookback
    # wait for uptrend signal
    entry = None
    for i in range(idx, len(ordered) - 1):
        window = ordered[i - trend_lookback : i + 1]
        p0 = float(window[0]["price_usd"])
        p1 = float(window[-1]["price_usd"])
        if p0 > 0 and (p1 - p0) / p0 > 0:
            entry = ordered[i]
            idx = i
            break
    if entry is None:
        return None
    ep = float(entry["price_usd"])
    if ep <= 0:
        return None
    entry_t = _parse_iso(entry["recorded_at"])
    assert entry_t
    deadline = entry_t + timedelta(minutes=hold_min)
    sell = None
    reason = "trend_hold"
    for j in range(idx + 1, len(ordered)):
        p = ordered[j]
        t = _parse_iso(p["recorded_at"])
        if t and t > deadline:
            sell = _nearest(ordered, deadline) or p
            reason = "hold_expiry"
            break
        if j >= trend_lookback:
            w = ordered[j - trend_lookback : j + 1]
            a, b = float(w[0]["price_usd"]), float(w[-1]["price_usd"])
            if a > 0 and (b - a) / a < 0:
                sell = p
                reason = "trend_exit"
                break
    if sell is None:
        sell = ordered[-1]
        reason = "series_end"
    sp = float(sell["price_usd"])
    ret = (sp - ep) / ep
    return _trade_dict(ordered[0], entry, sell, ep, sp, ret, reason=reason)


# backward-compatible alias
def backtest_pair(points, *, buy_after_min, hold_min):
    return backtest_hold(points, buy_after_min=buy_after_min, hold_min=hold_min)


def _summarize(trades: list[dict[str, Any]], strategy_meta: dict[str, Any], pairs_n: int) -> dict[str, Any]:
    rets = [t["return_pct"] for t in trades]
    wins = sum(1 for t in trades if t["win"])
    n = len(trades)
    reasons: dict[str, int] = {}
    for t in trades:
        r = t.get("reason") or "?"
        reasons[r] = reasons.get(r, 0) + 1
    return {
        "strategy": strategy_meta,
        "pairs_considered": pairs_n,
        "trades": n,
        "win_rate": round(wins / n, 4) if n else None,
        "avg_return_pct": round(sum(rets) / n, 4) if n else None,
        "median_return_pct": round(sorted(rets)[n // 2], 4) if n else None,
        "exit_reasons": reasons,
        "pnl_distribution": {
            "min": min(rets) if rets else None,
            "p25": round(sorted(rets)[max(0, n // 4)], 4) if n else None,
            "p50": round(sorted(rets)[n // 2], 4) if n else None,
            "p75": round(sorted(rets)[min(n - 1, (3 * n) // 4)], 4) if n else None,
            "max": max(rets) if rets else None,
        },
        "sample_trades": trades[:20],
    }


def run_backtest(
    store: Store,
    *,
    buy_after_min: float,
    hold_min: float,
    limit_pairs: int = 200,
    strategy: str = "hold",
    tp_pct: float = 10.0,
    sl_pct: float = 5.0,
    grid_step_pct: float = 3.0,
    grid_levels: int = 3,
    dca_steps: int = 3,
    dca_interval_min: float = 10.0,
    martingale_max_adds: int = 3,
    martingale_drop_pct: float = 5.0,
    trend_lookback: int = 3,
) -> dict[str, Any]:
    pairs = store._conn.execute(
        """
        SELECT chain_id, pair_address, COUNT(*) AS n
        FROM pair_history
        WHERE price_usd IS NOT NULL
        GROUP BY chain_id, pair_address
        HAVING n >= 2
        ORDER BY n DESC
        LIMIT ?
        """,
        (limit_pairs,),
    ).fetchall()
    trades: list[dict[str, Any]] = []
    for r in pairs:
        pts = store.list_pair_history(r["chain_id"], r["pair_address"], limit=500)
        for p in pts:
            p.setdefault("chain_id", r["chain_id"])
            p.setdefault("pair_address", r["pair_address"])
        if strategy == "tpsl":
            tr = backtest_tpsl(
                pts,
                buy_after_min=buy_after_min,
                hold_min=hold_min,
                tp_pct=tp_pct,
                sl_pct=sl_pct,
            )
        elif strategy == "grid":
            tr = backtest_grid(
                pts,
                buy_after_min=buy_after_min,
                hold_min=hold_min,
                grid_step_pct=grid_step_pct,
                grid_levels=grid_levels,
            )
        elif strategy == "dca":
            tr = backtest_dca(
                pts,
                buy_after_min=buy_after_min,
                hold_min=hold_min,
                dca_steps=dca_steps,
                dca_interval_min=dca_interval_min,
            )
        elif strategy == "martingale":
            tr = backtest_martingale(
                pts,
                buy_after_min=buy_after_min,
                hold_min=hold_min,
                martingale_max_adds=martingale_max_adds,
                martingale_drop_pct=martingale_drop_pct,
            )
        elif strategy == "trend":
            tr = backtest_trend(
                pts,
                buy_after_min=buy_after_min,
                hold_min=hold_min,
                trend_lookback=trend_lookback,
            )
        else:
            tr = backtest_hold(pts, buy_after_min=buy_after_min, hold_min=hold_min)
        if tr:
            trades.append(tr)

    meta = {
        "name": strategy,
        "buy_after_min": buy_after_min,
        "hold_min": hold_min,
        "tp_pct": tp_pct if strategy == "tpsl" else None,
        "sl_pct": sl_pct if strategy == "tpsl" else None,
        "grid_step_pct": grid_step_pct if strategy == "grid" else None,
        "grid_levels": grid_levels if strategy == "grid" else None,
        "dca_steps": dca_steps if strategy == "dca" else None,
        "martingale_max_adds": martingale_max_adds if strategy == "martingale" else None,
        "trend_lookback": trend_lookback if strategy == "trend" else None,
    }
    return _summarize(trades, meta, len(pairs))


def seed_demo_history(store: Store, *, n_pairs: int = 8, points: int = 12) -> int:
    rng = random.Random(42)
    base = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    written = 0
    for i in range(n_pairs):
        chain = "solana"
        pair = (f"btpair{i:04d}" + ("1" * 32))[:44]
        token = (f"bttoken{i:04d}" + ("2" * 32))[:44]
        price = rng.uniform(0.0001, 0.05)
        store.upsert_token(
            chain, token,
            {"chainId": chain, "tokenAddress": token, "description": "backtest seed"},
            "backtest_seed",
        )
        store.upsert_pair(
            chain, pair,
            {
                "chainId": chain,
                "pairAddress": pair,
                "priceUsd": price,
                "baseToken": {"address": token, "symbol": f"BT{i}"},
                "liquidity": {"usd": 20000},
                "volume": {"h24": 10000},
            },
            "backtest_seed",
            token_address=token,
        )
        for j in range(points):
            price *= math.exp(rng.uniform(-0.08, 0.1))
            ts = (base + timedelta(minutes=5 * j)).strftime("%Y-%m-%dT%H:%M:%SZ")
            store.insert_pair_history(
                chain, pair,
                token_address=token,
                price_usd=price,
                volume_h24=10000 + j * 100,
                liquidity_usd=20000,
                tier="hot",
                recorded_at=ts,
            )
            written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="pair_history multi-strategy backtest")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument(
        "--strategy",
        choices=("hold", "tpsl", "grid", "dca", "martingale", "trend"),
        default="hold",
    )
    parser.add_argument("--buy-after", type=float, default=5.0)
    parser.add_argument("--hold", type=float, default=30.0)
    parser.add_argument("--tp", type=float, default=10.0, help="Take-profit %% (tpsl)")
    parser.add_argument("--sl", type=float, default=5.0, help="Stop-loss %% (tpsl)")
    parser.add_argument("--grid-step", type=float, default=3.0, help="Grid step %%")
    parser.add_argument("--grid-levels", type=int, default=3)
    parser.add_argument("--dca-steps", type=int, default=3)
    parser.add_argument("--dca-interval", type=float, default=10.0)
    parser.add_argument("--martingale-max-adds", type=int, default=3)
    parser.add_argument("--martingale-drop", type=float, default=5.0)
    parser.add_argument("--trend-lookback", type=int, default=3)
    parser.add_argument("--seed-demo", action="store_true")
    parser.add_argument("--out", default="")
    parser.add_argument("--limit-pairs", type=int, default=200)
    args = parser.parse_args(argv)

    with Store(args.db) as store:
        if args.seed_demo:
            n = seed_demo_history(store)
            print(f"seeded_demo_history_points={n}")
        result = run_backtest(
            store,
            buy_after_min=args.buy_after,
            hold_min=args.hold,
            limit_pairs=args.limit_pairs,
            strategy=args.strategy,
            tp_pct=args.tp,
            sl_pct=args.sl,
            grid_step_pct=args.grid_step,
            grid_levels=args.grid_levels,
            dca_steps=args.dca_steps,
            dca_interval_min=args.dca_interval,
            martingale_max_adds=args.martingale_max_adds,
            martingale_drop_pct=args.martingale_drop,
            trend_lookback=args.trend_lookback,
        )

    print(f"=== backtest strategy={args.strategy} ===")
    print(json.dumps({k: result[k] for k in result if k != "sample_trades"}, indent=2))
    print(f"sample_trades={len(result.get('sample_trades') or [])}")
    for t in (result.get("sample_trades") or [])[:5]:
        print(
            f"  {t['chain_id']}/{str(t['pair_address'])[:10]}… "
            f"ret={t['return_pct']}% win={t['win']} reason={t.get('reason')}"
        )

    out = args.out or str(
        Path("exports")
        / f"backtest-{args.strategy}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"JSON → {out}")
    return 0 if result.get("trades") else 2


if __name__ == "__main__":
    raise SystemExit(main())
