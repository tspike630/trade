"""Phase0 shadow tags (旁路双列只读，不进决策).

Flags (both written every run; decision must not read them):
  1) shadow_R3_vol_chg_rsi  (rule R3, frozen sample-in thresholds)
       volatility_24h > 2.5
       AND (change_1h < 0 OR rsi_14 < 30 OR rsi_14 >= 70)
  2) shadow_liq_lt_150k
       True only when liq_usd is not NULL and liq_usd < 150000.
       NULL / missing measurement ≠ low liquidity → flag stays 0.

Features: indicator_history as-of opened_at (recorded_at <= opened_at, lag <= 2h),
prefer rows with rsi_14 set; liq_usd from positions.meta_json.liquidity_usd.
Read-only on paper_trading.db / dexscreener.db.
Writes exports/shadow_skip_stop_prone_live.csv (optional independent sqlite).
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

RULE_ID_R3 = "R3"
FLAG_R3 = "shadow_R3_vol_chg_rsi"
FLAG_LIQ = "shadow_liq_lt_150k"
# legacy alias kept in CSV as mirror of FLAG_R3 for older readers
FLAG_R3_LEGACY = "shadow_skip_stop_prone"

VOL_THR = 2.5
RSI_LO = 30.0
RSI_HI = 70.0
LIQ_THR = 150_000.0
MAX_LAG = timedelta(hours=2)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PAPER = ROOT / "paper_trading.db"
DEFAULT_DEX = ROOT / "dexscreener.db"
DEFAULT_CSV = ROOT / "exports" / "shadow_skip_stop_prone_live.csv"
DEFAULT_SHADOW_DB = ROOT / "indicators_shadow.db"

CSV_FIELDS = [
    "position_id",
    "status",
    "chain_id",
    "pair_address",
    "token_address",
    "opened_at",
    "closed_at",
    "exit_reason",
    "pnl_usd",
    "liq_usd",
    "volatility_24h",
    "change_1h",
    "rsi_14",
    "ih_recorded_at",
    "ih_lag_hours",
    "ih_join_ok",
    FLAG_R3,
    FLAG_LIQ,
    FLAG_R3_LEGACY,  # == shadow_R3_vol_chg_rsi
    "rule_id",
    "tagged_at",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    t = s.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


def parse_meta(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw or not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def liq_from_meta(meta: dict[str, Any]) -> float | None:
    """Entry liquidity from meta; None if absent/unparseable (must NOT count as < thr)."""
    for key in ("liquidity_usd", "liq_usd", "liquidity"):
        if key in meta and meta[key] is not None and meta[key] != "":
            return _f(meta[key])
    return None


def eval_r3(
    volatility_24h: float | None,
    change_1h: float | None,
    rsi_14: float | None,
    *,
    vol_thr: float = VOL_THR,
) -> bool:
    if volatility_24h is None or not (volatility_24h > vol_thr):
        return False
    weak_chg = change_1h is not None and change_1h < 0
    rsi_extreme = rsi_14 is not None and (rsi_14 < RSI_LO or rsi_14 >= RSI_HI)
    return bool(weak_chg or rsi_extreme)


def eval_liq_lt(
    liq_usd: float | None,
    *,
    liq_thr: float = LIQ_THR,
) -> bool:
    """True only when measured liq is present and strictly below threshold."""
    if liq_usd is None:
        return False
    return liq_usd < liq_thr


def _ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30.0)
    con.row_factory = sqlite3.Row
    return con


def fetch_positions(paper: sqlite3.Connection, *, include_open: bool = True) -> list[dict[str, Any]]:
    where = "status IN ('closed', 'open')" if include_open else "status = 'closed'"
    rows = paper.execute(
        f"""
        SELECT id, status, chain_id, pair_address, token_address,
               opened_at, closed_at, exit_reason, pnl_usd, meta_json
        FROM positions
        WHERE {where}
        ORDER BY opened_at ASC, id ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def asof_indicator(
    dex: sqlite3.Connection,
    chain_id: str,
    token_address: str,
    opened_at: str,
    *,
    max_lag: timedelta = MAX_LAG,
) -> dict[str, Any] | None:
    if not chain_id or not token_address or not opened_at:
        return None
    opened_dt = _parse_ts(opened_at)
    if opened_dt is None:
        return None
    row = dex.execute(
        """
        SELECT id, recorded_at, volatility_24h, change_1h, rsi_14,
               volume_strength, primary_pair_address
        FROM indicator_history
        WHERE chain_id = ?
          AND token_address = ?
          AND recorded_at <= ?
        ORDER BY
            (rsi_14 IS NULL) ASC,
            recorded_at DESC,
            id DESC
        LIMIT 1
        """,
        (chain_id, token_address, opened_at),
    ).fetchone()
    if row is None:
        row = dex.execute(
            """
            SELECT id, recorded_at, volatility_24h, change_1h, rsi_14,
                   volume_strength, primary_pair_address
            FROM indicator_history
            WHERE chain_id = ?
              AND lower(token_address) = lower(?)
              AND recorded_at <= ?
            ORDER BY
                (rsi_14 IS NULL) ASC,
                recorded_at DESC,
                id DESC
            LIMIT 1
            """,
            (chain_id, token_address, opened_at),
        ).fetchone()
    if row is None:
        return None
    ra = _parse_ts(row["recorded_at"])
    if ra is None:
        return None
    lag = opened_dt - ra
    if lag < timedelta(0) or lag > max_lag:
        return None
    d = dict(row)
    d["ih_lag_hours"] = lag.total_seconds() / 3600.0
    return d


@dataclass
class FlagStats:
    name: str
    n_flag: int = 0
    n_sl_flag: int = 0
    n_tp_flag: int = 0
    n_mh_flag: int = 0
    flagged_pnl: float = 0.0
    n_null_input: int = 0  # e.g. liq missing (explicitly not flagged)


@dataclass
class TagStats:
    n_total: int = 0
    n_closed: int = 0
    n_open: int = 0
    n_join_ok: int = 0
    n_sl: int = 0
    n_tp: int = 0
    closed_pnl: float = 0.0
    flags: dict[str, FlagStats] = field(default_factory=dict)

    def ensure(self, name: str) -> FlagStats:
        if name not in self.flags:
            self.flags[name] = FlagStats(name=name)
        return self.flags[name]


def _bump_flag(
    fs: FlagStats,
    *,
    flagged: bool,
    exit_reason: Any,
    pnl: float | None,
    status: str,
) -> None:
    if not flagged:
        return
    fs.n_flag += 1
    if status == "closed" and pnl is not None:
        fs.flagged_pnl += pnl
    if exit_reason == "stop_loss":
        fs.n_sl_flag += 1
    elif exit_reason == "take_profit":
        fs.n_tp_flag += 1
    elif exit_reason == "max_hold":
        fs.n_mh_flag += 1


def tag_positions(
    positions: list[dict[str, Any]],
    dex: sqlite3.Connection,
    *,
    tagged_at: str | None = None,
    vol_thr: float = VOL_THR,
    liq_thr: float = LIQ_THR,
) -> tuple[list[dict[str, Any]], TagStats]:
    tagged_at = tagged_at or _utc_now_iso()
    stats = TagStats()
    stats.ensure(FLAG_R3)
    stats.ensure(FLAG_LIQ)
    rows_out: list[dict[str, Any]] = []

    for pos in positions:
        stats.n_total += 1
        status = str(pos.get("status") or "")
        if status == "closed":
            stats.n_closed += 1
        elif status == "open":
            stats.n_open += 1

        meta = parse_meta(pos.get("meta_json"))
        liq = liq_from_meta(meta)
        if liq is None:
            stats.ensure(FLAG_LIQ).n_null_input += 1

        ih = asof_indicator(
            dex,
            str(pos.get("chain_id") or ""),
            str(pos.get("token_address") or ""),
            str(pos.get("opened_at") or ""),
        )
        vol = chg = rsi = None
        ih_ra = None
        lag_h = None
        join_ok = 0
        if ih is not None:
            join_ok = 1
            stats.n_join_ok += 1
            vol = _f(ih.get("volatility_24h"))
            chg = _f(ih.get("change_1h"))
            rsi = _f(ih.get("rsi_14"))
            ih_ra = ih.get("recorded_at")
            lag_h = ih.get("ih_lag_hours")

        flag_r3 = 1 if eval_r3(vol, chg, rsi, vol_thr=vol_thr) else 0
        flag_liq = 1 if eval_liq_lt(liq, liq_thr=liq_thr) else 0

        exit_reason = pos.get("exit_reason")
        pnl = _f(pos.get("pnl_usd"))
        if status == "closed" and pnl is not None:
            stats.closed_pnl += pnl
        if exit_reason == "stop_loss":
            stats.n_sl += 1
        elif exit_reason == "take_profit":
            stats.n_tp += 1

        _bump_flag(
            stats.ensure(FLAG_R3),
            flagged=bool(flag_r3),
            exit_reason=exit_reason,
            pnl=pnl,
            status=status,
        )
        _bump_flag(
            stats.ensure(FLAG_LIQ),
            flagged=bool(flag_liq),
            exit_reason=exit_reason,
            pnl=pnl,
            status=status,
        )

        rows_out.append(
            {
                "position_id": pos.get("id"),
                "status": status,
                "chain_id": pos.get("chain_id"),
                "pair_address": pos.get("pair_address"),
                "token_address": pos.get("token_address"),
                "opened_at": pos.get("opened_at"),
                "closed_at": pos.get("closed_at"),
                "exit_reason": exit_reason,
                "pnl_usd": pnl,
                "liq_usd": liq,
                "volatility_24h": vol,
                "change_1h": chg,
                "rsi_14": rsi,
                "ih_recorded_at": ih_ra,
                "ih_lag_hours": lag_h,
                "ih_join_ok": join_ok,
                FLAG_R3: flag_r3,
                FLAG_LIQ: flag_liq,
                FLAG_R3_LEGACY: flag_r3,
                "rule_id": f"{RULE_ID_R3}+LIQ150k",
                "tagged_at": tagged_at,
            }
        )
    return rows_out, stats


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    tmp.replace(path)


def write_shadow_db(path: Path, rows: list[dict[str, Any]], tagged_at: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            f"""
            CREATE TABLE IF NOT EXISTS shadow_phase0_tags (
                position_id TEXT NOT NULL,
                status TEXT,
                chain_id TEXT,
                pair_address TEXT,
                token_address TEXT,
                opened_at TEXT,
                closed_at TEXT,
                exit_reason TEXT,
                pnl_usd REAL,
                liq_usd REAL,
                volatility_24h REAL,
                change_1h REAL,
                rsi_14 REAL,
                ih_recorded_at TEXT,
                ih_lag_hours REAL,
                ih_join_ok INTEGER,
                {FLAG_R3} INTEGER NOT NULL,
                {FLAG_LIQ} INTEGER NOT NULL,
                rule_id TEXT NOT NULL,
                tagged_at TEXT NOT NULL,
                PRIMARY KEY (position_id, tagged_at)
            )
            """
        )
        con.execute("DELETE FROM shadow_phase0_tags")
        con.executemany(
            f"""
            INSERT INTO shadow_phase0_tags (
                position_id, status, chain_id, pair_address, token_address,
                opened_at, closed_at, exit_reason, pnl_usd, liq_usd,
                volatility_24h, change_1h, rsi_14,
                ih_recorded_at, ih_lag_hours, ih_join_ok,
                {FLAG_R3}, {FLAG_LIQ}, rule_id, tagged_at
            ) VALUES (
                :position_id, :status, :chain_id, :pair_address, :token_address,
                :opened_at, :closed_at, :exit_reason, :pnl_usd, :liq_usd,
                :volatility_24h, :change_1h, :rsi_14,
                :ih_recorded_at, :ih_lag_hours, :ih_join_ok,
                :{FLAG_R3}, :{FLAG_LIQ}, :rule_id, :tagged_at
            )
            """,
            rows,
        )
        con.commit()
    finally:
        con.close()


def format_stats(stats: TagStats) -> str:
    lines = [
        f"n_total={stats.n_total} closed={stats.n_closed} open={stats.n_open} "
        f"join_ok={stats.n_join_ok} closed_pnl={stats.closed_pnl:.4f}",
        f"SL={stats.n_sl} TP={stats.n_tp}",
    ]
    for name in (FLAG_R3, FLAG_LIQ):
        fs = stats.flags.get(name) or FlagStats(name=name)
        retained = stats.closed_pnl - fs.flagged_pnl
        extra = ""
        if name == FLAG_LIQ:
            extra = f"  liq_null={fs.n_null_input}(not flagged)"
        lines.append(
            f"{name}: n_flag={fs.n_flag}  SL_hit={fs.n_sl_flag}/{stats.n_sl}  "
            f"TP_flag={fs.n_tp_flag}/{stats.n_tp}  max_hold_flag={fs.n_mh_flag}  "
            f"flagged_pnl={fs.flagged_pnl:.4f}  if_skip_retained≈{retained:.4f}{extra}"
        )
    lines.append(
        f"thresholds: vol_thr={VOL_THR} liq_thr={LIQ_THR}  "
        f"(NULL liq ≠ low-liq)"
    )
    return "\n".join(lines)


def stats_to_dict(stats: TagStats, out_csv: str | Path) -> dict[str, Any]:
    out: dict[str, Any] = {
        "n_total": stats.n_total,
        "n_closed": stats.n_closed,
        "n_open": stats.n_open,
        "n_join_ok": stats.n_join_ok,
        "n_sl": stats.n_sl,
        "n_tp": stats.n_tp,
        "closed_pnl": stats.closed_pnl,
        "out_csv": str(out_csv),
        "flags": {},
    }
    for name, fs in stats.flags.items():
        out["flags"][name] = {
            "n_flag": fs.n_flag,
            "n_sl_flag": fs.n_sl_flag,
            "n_tp_flag": fs.n_tp_flag,
            "n_mh_flag": fs.n_mh_flag,
            "flagged_pnl": fs.flagged_pnl,
            "n_null_input": fs.n_null_input,
        }
    return out


def run(
    *,
    paper_db: Path = DEFAULT_PAPER,
    dex_db: Path = DEFAULT_DEX,
    out_csv: Path = DEFAULT_CSV,
    shadow_db: Path | None = None,
    include_open: bool = True,
    vol_thr: float = VOL_THR,
    liq_thr: float = LIQ_THR,
) -> TagStats:
    if not paper_db.is_file():
        raise FileNotFoundError(f"paper db missing: {paper_db}")
    if not dex_db.is_file():
        raise FileNotFoundError(f"dex db missing: {dex_db}")
    tagged_at = _utc_now_iso()
    paper = _ro(paper_db)
    dex = _ro(dex_db)
    try:
        positions = fetch_positions(paper, include_open=include_open)
        rows, stats = tag_positions(
            positions, dex, tagged_at=tagged_at, vol_thr=vol_thr, liq_thr=liq_thr
        )
    finally:
        paper.close()
        dex.close()
    write_csv(out_csv, rows)
    if shadow_db is not None:
        write_shadow_db(shadow_db, rows, tagged_at)
    return stats


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Phase0 dual shadow tags: R3 + liq<150k (no decision impact)"
    )
    p.add_argument("--paper-db", type=Path, default=DEFAULT_PAPER)
    p.add_argument("--dex-db", type=Path, default=DEFAULT_DEX)
    p.add_argument("--out-csv", type=Path, default=DEFAULT_CSV)
    p.add_argument(
        "--shadow-db",
        type=Path,
        default=None,
        help="Optional independent sqlite. Never paper_trading.db.",
    )
    p.add_argument("--no-open", action="store_true")
    p.add_argument("--vol-thr", type=float, default=VOL_THR)
    p.add_argument("--liq-thr", type=float, default=LIQ_THR)
    p.add_argument("--json-stats", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)
    if args.shadow_db is not None and Path(args.shadow_db).resolve() == DEFAULT_PAPER.resolve():
        print("REFUSE: --shadow-db must not be paper_trading.db", file=sys.stderr)
        return 2
    stats = run(
        paper_db=args.paper_db,
        dex_db=args.dex_db,
        out_csv=args.out_csv,
        shadow_db=args.shadow_db,
        include_open=not args.no_open,
        vol_thr=args.vol_thr,
        liq_thr=args.liq_thr,
    )
    if args.json_stats:
        print(json.dumps(stats_to_dict(stats, args.out_csv), ensure_ascii=False))
    else:
        print(format_stats(stats))
        print(f"wrote {args.out_csv}")
        if args.shadow_db:
            print(f"wrote {args.shadow_db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
