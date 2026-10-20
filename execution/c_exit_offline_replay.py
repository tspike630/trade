"""Offline C-exit four-cell path replay on main-book closed positions.

Read-only against paper_trading.db and dexscreener.db.
Writes report under /workspace/out/ (or --out).

  python -m execution.c_exit_offline_replay
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from execution.c_exit_grid import (  # noqa: E402
    MIN_CLOSED_PER_CELL,
    cells,
    replay_path_exit,
    summarize_cell,
)


def _load_closed(paper_db: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{paper_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [
        dict(r)
        for r in con.execute(
            """
            SELECT id, chain_id, pair_address, entry_price, size_usd,
                   opened_at, closed_at, exit_price, exit_reason, pnl_usd,
                   tp_pct, sl_pct, max_hold_min
            FROM positions
            WHERE status = 'closed'
            ORDER BY opened_at
            """
        )
    ]
    con.close()
    return rows


def _load_bars(
    crawler_db: Path,
    keys: set[tuple[str, str]],
    since: str,
    until: str,
) -> dict[tuple[str, str], list[tuple[str, float]]]:
    """Load pair_history bars for all keys in [since, until] once."""
    con = sqlite3.connect(f"file:{crawler_db}?mode=ro", uri=True)
    out: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    # Chunk OR queries to avoid huge SQL
    key_list = sorted(keys)
    chunk = 40
    for i in range(0, len(key_list), chunk):
        part = key_list[i : i + chunk]
        placeholders = " OR ".join(["(chain_id=? AND pair_address=?)"] * len(part))
        params: list = []
        for c, p in part:
            params.extend([c, p])
        params.extend([since, until])
        sql = f"""
            SELECT chain_id, pair_address, recorded_at, price_usd
            FROM pair_history
            WHERE ({placeholders})
              AND recorded_at >= ? AND recorded_at <= ?
              AND price_usd IS NOT NULL AND price_usd > 0
            ORDER BY recorded_at ASC
        """
        for r in con.execute(sql, params):
            out[(r[0], r[1])].append((r[2], float(r[3])))
    con.close()
    return out


def run(paper_db: Path, crawler_db: Path) -> tuple[list[dict], dict]:
    closed = _load_closed(paper_db)
    actual_pnl = sum(float(r["pnl_usd"] or 0) for r in closed)
    if not closed:
        return [], {"actual_pnl": 0.0, "n_closed": 0}

    # Need bars up to opened + 90m for all cells
    from execution.c_exit_grid import _parse_ts

    opens = [_parse_ts(r["opened_at"]) for r in closed]
    closes = [_parse_ts(r["closed_at"]) for r in closed]
    opens_ok = [t for t in opens if t]
    since = min(opens_ok).strftime("%Y-%m-%dT%H:%M:%SZ")
    # pad +95m past last open
    until_dt = max(opens_ok) + timedelta(minutes=95)
    # also include actual close times for coverage stats
    if any(closes):
        until_dt = max(until_dt, max(t for t in closes if t))
    until = until_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    keys = {(r["chain_id"], r["pair_address"]) for r in closed}
    bars_by = _load_bars(crawler_db, keys, since, until)

    grid = cells()
    per_cell_results: dict[str, list] = {c.tag: [] for c in grid}
    miss_by: dict[str, int] = {c.tag: 0 for c in grid}

    for pos in closed:
        key = (pos["chain_id"], pos["pair_address"])
        bars = bars_by.get(key, [])
        for cell in grid:
            res = replay_path_exit(
                entry=float(pos["entry_price"]),
                size_usd=float(pos["size_usd"]),
                opened_at=pos["opened_at"],
                bars=bars,
                tp_pct=cell.tp_pct,
                sl_pct=cell.sl_pct,
                max_hold_min=cell.max_hold_min,
            )
            if res is None:
                miss_by[cell.tag] += 1
                per_cell_results[cell.tag].append(None)
            else:
                res["position_id"] = pos["id"]
                res["actual_pnl_usd"] = pos["pnl_usd"]
                res["actual_reason"] = pos["exit_reason"]
                per_cell_results[cell.tag].append(res)

    # baseline = c_sl5_h60 replay
    base_rows = [r for r in per_cell_results["c_sl5_h60"] if r is not None]
    baseline_pnl = sum(float(r["pnl_usd"]) for r in base_rows)

    summaries = []
    for cell in grid:
        rows = per_cell_results[cell.tag]
        hit = [r for r in rows if r is not None]
        s = summarize_cell(
            cell.tag,
            hit,
            baseline_pnl=baseline_pnl,
            actual_pnl=actual_pnl,
        )
        s["miss"] = miss_by[cell.tag]
        s["tp_pct"] = cell.tp_pct
        s["sl_pct"] = cell.sl_pct
        s["max_hold_min"] = cell.max_hold_min
        s["enough_for_compare"] = s["n"] >= MIN_CLOSED_PER_CELL
        summaries.append(s)

    meta = {
        "n_closed": len(closed),
        "actual_pnl": actual_pnl,
        "baseline_tag": "c_sl5_h60",
        "baseline_pnl": baseline_pnl,
        "since": since,
        "until": until,
        "n_pairs": len(keys),
    }
    return summaries, meta


def render_md(summaries: list[dict], meta: dict) -> str:
    lines = [
        "# C 出口格 · 离线四格路径重放 · 2026-10-20",
        "",
        "> **角色：** 执行旁簿实验（只读主簿）  ",
        "> **时区：** Asia/Hong_Kong  ",
        "> **纪律：** 不改 `.env`、不停主 loop、不开 live、不写主簿。",
        "",
        "---",
        "",
        "## 0. 口径",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 主簿已平 | **{meta['n_closed']}** |",
        f"| 主簿实盘 Σpnl | **{meta['actual_pnl']:.2f}** |",
        f"| 价格源 | `dexscreener.db.pair_history.price_usd` |",
        f"| 窗 | `{meta['since']}` → `{meta['until']}` |",
        f"| 同 bar 次序 | **先 SL 后 TP**（偏保守，同 M2） |",
        f"| PnL | `size_usd * (exit_mid/entry - 1)` |",
        f"| 四格 | TP=10%；SL∈{{4,5}} × hold∈{{60,90}} |",
        f"| 满笔门槛 | 每格 ≥{MIN_CLOSED_PER_CELL} 平仓才比止损占负（本离线已满则可对照，升主另批） |",
        "",
        "> 粗粒度路径重放，非分钟撮合真值；漏 bar 内刺穿。",
        "",
        "## 1. 四格结果",
        "",
        "| tag | sl | hold | n | miss | Σpnl | Δ vs c_sl5_h60 | Δ vs 实盘 | TP/SL/MH | 胜率 | 止损占负 | 单笔SL峰 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|",
    ]
    for s in summaries:
        sl_share = (
            f"{100.0 * s['sl_share_of_neg']:.1f}%"
            if s.get("sl_share_of_neg") is not None
            else "—"
        )
        peak = f"{s['sl_peak_usd']:.2f}" if s.get("sl_peak_usd") is not None else "—"
        wr = f"{100.0 * s['win_rate']:.1f}%" if s.get("win_rate") is not None else "—"
        lines.append(
            f"| `{s['tag']}` | {s['sl_pct']:g} | {s['max_hold_min']:g} | {s['n']} | {s['miss']} | "
            f"{s['total_pnl']:.2f} | {s.get('delta_vs_baseline', 0):+.2f} | "
            f"{s.get('delta_vs_actual', 0):+.2f} | "
            f"{s['n_tp']}/{s['n_sl']}/{s['n_mh']} | {wr} | {sl_share} | {peak} |"
        )

    lines += [
        "",
        "### 读法",
        "",
        "- `c_sl5_h60` 为旁簿对照格（应贴近主路径重放）。",
        "- **止损占负** = `stop_loss` 负 PnL 合计 / 全部负贡献合计（越低越好，需满笔后才作升主依据）。",
        "- 本表为样本内路径近似；**不**直接升主。升主需满笔 + OOS + 五层批。",
        "",
        "## 2. 接线验收提示",
        "",
        "- 旁簿库：`paper_trading_c_exit.db`（四 tag 分账户）。",
        "- 启动：见 `docs/C_EXIT_GRID_LANDING_2026-10-20.md` §启动。",
        "- 回滚：单笔≤−$500 / 旁簿累计≤−$400 / 污染主簿 → 停旁簿。",
        "",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C exit grid offline replay")
    p.add_argument("--paper-db", default=str(_ROOT / "paper_trading.db"))
    p.add_argument("--crawler-db", default=str(_ROOT / "dexscreener.db"))
    p.add_argument(
        "--out",
        default="/workspace/out/c_exit_grid_offline_2026-10-20.md",
    )
    p.add_argument(
        "--csv",
        default="/workspace/out/c_exit_grid_offline_2026-10-20.csv",
    )
    args = p.parse_args(argv)

    summaries, meta = run(Path(args.paper_db), Path(args.crawler_db))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_md(summaries, meta), encoding="utf-8")

    csv_path = Path(args.csv)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "tag",
                "tp_pct",
                "sl_pct",
                "max_hold_min",
                "n",
                "miss",
                "total_pnl",
                "avg_pnl",
                "n_tp",
                "n_sl",
                "n_mh",
                "win_rate",
                "sl_neg_usd",
                "neg_sum_usd",
                "sl_share_of_neg",
                "sl_peak_usd",
                "delta_vs_baseline",
                "delta_vs_actual",
                "enough_for_compare",
            ],
        )
        w.writeheader()
        for s in summaries:
            w.writerow(s)

    print(json.dumps({"out": str(out), "csv": str(csv_path), "meta": meta, "summaries": summaries}, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
