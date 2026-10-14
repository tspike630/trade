"""抽样校验：既有 change/vol 手写口径自洽 + pandas-ta ADD-ON 字段范围检查。

用法（仓库根目录）：
    .venv/bin/python scripts/compare_indicators.py --limit 5

说明：
- 不替换既有 change_*/volatility_24h/volume_strength；仅核对它们仍可计算。
- 新列 rsi_14/macd*/boll_* 用 technical_lib 复算，做范围 sanity（非旧公式 diff）。
- atr_14/adx_14 亦复算打印（只算不落库；短历史允许 None）。
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from indicators import technical as tech  # noqa: E402
from indicators import technical_lib  # noqa: E402
from indicators.base import to_epoch  # noqa: E402


def _finite(v: object) -> bool:
    if v is None:
        return False
    try:
        x = float(v)
    except (TypeError, ValueError):
        return False
    return not (math.isnan(x) or math.isinf(x))


def load_series(conn: sqlite3.Connection, chain_id: str, pair_address: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT recorded_at, price_usd, volume_h1, volume_h24
        FROM pair_history
        WHERE chain_id=? AND pair_address=?
        ORDER BY recorded_at ASC LIMIT 2000
        """,
        (chain_id, pair_address),
    ).fetchall()
    out = []
    for ra, px, v1, v24 in rows:
        out.append(
            {
                "recorded_at": ra,
                "price_usd": px,
                "volume_h1": v1,
                "volume_h24": v24,
            }
        )
    return out


def naive_rsi(closes: list[float], period: int = 14) -> float | None:
    """简单 Wilder RSI 对照（非生产路径）；历史不足返回 None。"""
    if len(closes) <= period:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--db", default=str(ROOT / "dexscreener.db"))
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    tokens = conn.execute(
        """
        SELECT ti.chain_id, ti.token_address, ti.primary_pair_address
        FROM token_indicators ti
        WHERE ti.primary_pair_address IS NOT NULL
        ORDER BY ti.updated_at DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    if not tokens:
        # fallback: any pair with enough history
        tokens = conn.execute(
            """
            SELECT chain_id, token_address, pair_address
            FROM (
                SELECT chain_id, token_address, pair_address, COUNT(*) AS n
                FROM pair_history
                GROUP BY chain_id, token_address, pair_address
                HAVING n >= 30
                ORDER BY n DESC
                LIMIT ?
            )
            """,
            (args.limit,),
        ).fetchall()
    if not tokens:
        print("FAIL: no tokens with history")
        return 1

    errors: list[str] = []
    for chain_id, token_address, pair_address in tokens:
        if not pair_address:
            errors.append(f"{chain_id}/{token_address}: no primary pair")
            continue
        series = load_series(conn, chain_id, pair_address)
        n = len(series)
        closes = [
            float(r["price_usd"])
            for r in series
            if r.get("price_usd") is not None and float(r["price_usd"]) > 0
        ]

        # Existing fields still computable
        chg_1h = tech.change_pct(series, 3600) if series else None
        vol = tech.volatility_stdev_returns(series) if series else None
        vs = tech.volume_strength(series) if series else None

        lib = technical_lib.compute_pandas_ta(closes)
        problems: list[str] = []

        rsi = lib.get("rsi_14")
        if _finite(rsi) and not (0.0 <= float(rsi) <= 100.0):
            problems.append(f"rsi_14 out of range: {rsi}")

        for key in ("macd", "macd_signal", "macd_hist"):
            v = lib.get(key)
            if _finite(v) and abs(float(v)) > 1e12:
                problems.append(f"{key} huge: {v}")

        mid = lib.get("boll_mid")
        up = lib.get("boll_upper")
        low = lib.get("boll_lower")
        if all(_finite(x) for x in (mid, up, low)):
            if not (float(low) <= float(mid) <= float(up)):
                problems.append(f"boll order broken: {low}/{mid}/{up}")

        # atr_14 / adx_14: compute-only; short history → None OK
        atr = lib.get("atr_14")
        adx = lib.get("adx_14")
        if _finite(atr) and float(atr) < 0.0:
            problems.append(f"atr_14 negative: {atr}")
        if _finite(adx) and not (0.0 <= float(adx) <= 100.0):
            # ADX is typically 0–100; allow tiny float noise above 100
            if float(adx) < 0.0 or float(adx) > 105.0:
                problems.append(f"adx_14 out of range: {adx}")

        # Naive vs library RSI when both present
        naive = naive_rsi(closes)
        rsi_diff = None
        if _finite(rsi) and _finite(naive):
            rsi_diff = abs(float(rsi) - float(naive))
            # Wilder smoothing path can diverge slightly on init; allow loose bound
            if rsi_diff > 15.0:
                problems.append(f"rsi naive diff large: {rsi_diff:.4f}")

        status = "OK" if not problems else "WARN"
        print(
            f"{status} {chain_id}/{str(token_address)[:12]} n={n} closes={len(closes)} "
            f"rsi={rsi} macd={lib.get('macd')} boll_mid={mid} "
            f"atr_14={atr} adx_14={adx} "
            f"chg1h={chg_1h} vol={vol} vs={vs} rsi_diff={rsi_diff}"
        )
        if problems:
            errors.append(f"{chain_id}/{token_address}: {problems}")

    conn.close()
    if errors:
        print(f"\nFAIL: {len(errors)} anomalies")
        for e in errors[:10]:
            print("  -", e)
        return 1
    print("\nPASS: sample OK (legacy fields intact; pandas-ta ADD-ON sane; atr/adx compute-only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
