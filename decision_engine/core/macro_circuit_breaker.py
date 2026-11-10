"""Pre-trade LSEG macro calendar blackout & cross-asset contagion circuit breaker.

Strictly opt-in via ``DECISION_MACRO_DB`` (or ``DECISION_MACRO_GATE_ENABLED=1``
pointing at ``data/lseg/lseg_macro.db``). When unwired, returns ``None`` and
has zero effect on signal emission, paper arms, or ``CELL_LEDGER``.

When wired for live Binance spot purposes (``live_bnb_boll_fade``,
``live_bnb_boll_classic``), reads ``lseg_macro.db`` in read-only mode
(``?mode=ro``) and blocks new BUY entries (while never blocking SELL exits)
under four evidence-backed conditions:

1. ``central_bank_blackout``: High-impact central bank event (FOMC, Powell,
   ECB/BOJ press conference, minutes) within ``[-15m, +45m]`` of ``now_ms``.
2. ``macro_rel3_blackout``: Tier-1 (``Relevance = 3``) economic release
   with a scheduled timestamp within ``[-15m, +30m]`` of ``now_ms``.
3. ``macro_rel2_auction_blackout``: US Treasury note/bond/bill auction or
   Tier-2 release within ``[-10m, +20m]`` of ``now_ms``.
4. ``cross_asset_*``: Real-time 5m cross-asset contagion when fresh LSEG
   5m bars (age <= 30m) show Nasdaq futures (``NQc1``) 15m drop <= -0.20%,
   US 2Y yield (``US2YT=RR``) 15m spike >= +3.0 bps, USDJPY (``JPY=``)
   15m carry unwind <= -0.15%, or CME BTC futures (``BTCc1`` vs ``BTC=``)
   basis discount < -15.0 bps.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from decision_engine.core.signal_bus import (
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    PURPOSE_LIVE_BNB_BOLL_FADE,
)

LIVE_BNB_SPOT_PURPOSES = frozenset(
    {
        PURPOSE_LIVE_BNB_BOLL_FADE,
        PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    }
)

_MACRO_DB_ENV = (
    "DECISION_MACRO_DB",
    "LSEG_MACRO_DB_PATH",
)

DEFAULT_CB_BEFORE_MIN = 15
DEFAULT_CB_AFTER_MIN = 45
DEFAULT_REL3_BEFORE_MIN = 15
DEFAULT_REL3_AFTER_MIN = 30
DEFAULT_REL2_BEFORE_MIN = 10
DEFAULT_REL2_AFTER_MIN = 20
DEFAULT_SHOCK_AFTER_MIN = 90
DEFAULT_INLINE_AFTER_MIN = 5
DEFAULT_SURPRISE_SHOCK_RATIO = 0.25
DEFAULT_PREDICTED_SURPRISE_RATIO = 0.15

DEFAULT_NQ_15M_FLOOR_PCT = -0.20
DEFAULT_US2Y_15M_CAP_BPS = 3.0
DEFAULT_JPY_15M_FLOOR_PCT = -0.15
DEFAULT_CME_BASIS_FLOOR_BPS = -15.0
DEFAULT_MAX_DEALER_SPREAD_BPS = 20.0
DEFAULT_MAX_HYG_SPREAD_BPS = 35.0
DEFAULT_MIN_QUOTE_IMBALANCE = -0.60
DEFAULT_MIN_NQ_QUOTE_IMBALANCE = -0.10
MAX_CROSS_ASSET_AGE_MS = 30 * 60 * 1000
_TRUE = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class MacroGateStatus:
    """Read-only evaluation of the macro & cross-asset circuit breaker."""

    configured: bool
    halt_reason: str | None
    detail: str | None = None
    error: str | None = None

    @staticmethod
    def unwired() -> MacroGateStatus:
        return MacroGateStatus(False, None, None, None)

    @staticmethod
    def clear() -> MacroGateStatus:
        return MacroGateStatus(True, None, None, None)

    @staticmethod
    def halted(reason: str, detail: str | None = None) -> MacroGateStatus:
        return MacroGateStatus(True, reason, detail, None)

    @staticmethod
    def failed(error: str) -> MacroGateStatus:
        return MacroGateStatus(True, "macro_status_unknown", None, error)


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def _env_bool(env: Mapping[str, str], key: str) -> bool:
    raw = env.get(key)
    if raw is None:
        return False
    return str(raw).strip().lower() in _TRUE


def resolve_macro_db_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """Return configured LSEG macro SQLite path, or ``None`` when disabled."""
    env = os.environ if environ is None else environ
    gate_flag = str(env.get("DECISION_MACRO_GATE_ENABLED") or "").strip().lower()
    if gate_flag in {"0", "false", "no", "off"}:
        return None
    for key in _MACRO_DB_ENV:
        raw = env.get(key)
        if raw is not None and str(raw).strip():
            return Path(str(raw).strip()).expanduser()
    if gate_flag in {"1", "true", "yes", "on"}:
        repo_root = Path(__file__).resolve().parents[2]
        return repo_root / "data" / "lseg" / "lseg_macro.db"
    return None


def _ro_uri(path: Path) -> str:
    return "file:" + quote(path.resolve().as_posix(), safe="/") + "?mode=ro"


def _has_tables(conn: sqlite3.Connection, required: tuple[str, ...]) -> str | None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    have = {str(r[0]) for r in rows}
    for t in required:
        if t not in have:
            return f"macro_table_missing:{t}"
    return None


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(r[1]) for r in rows}


def _check_cross_asset(
    conn: sqlite3.Connection,
    now_ms: int,
    env: Mapping[str, str],
) -> tuple[str | None, str | None]:
    """Check 5min cross-asset contagion if fresh bars exist within 30m of now_ms."""
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cross_asset_bars'"
    ).fetchone()
    if found is None:
        return None, None

    min_ts = now_ms - MAX_CROSS_ASSET_AGE_MS

    def _recent_closes(ric: str, limit: int = 4) -> list[tuple[int, float]]:
        rows = conn.execute(
            """
            SELECT utc_ms, close
            FROM cross_asset_bars
            WHERE ric = ? AND interval = '5min' AND utc_ms <= ? AND close IS NOT NULL
            ORDER BY utc_ms DESC
            LIMIT ?
            """,
            (ric, now_ms, limit),
        ).fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]

    # 1. NQc1 15m (3-bar) return
    nq = _recent_closes("NQc1", 4)
    if len(nq) >= 4 and nq[0][0] >= min_ts and nq[3][1] > 0:
        nq_ret_pct = ((nq[0][1] - nq[3][1]) / nq[3][1]) * 100.0
        floor = _env_float(env, "MACRO_NQ_15M_FLOOR_PCT", DEFAULT_NQ_15M_FLOOR_PCT)
        if nq_ret_pct <= floor:
            return "cross_asset_nq_dump", f"NQc1_15m={nq_ret_pct:+.3f}%"

    # 2. US2YT=RR 15m yield spike (in bps)
    us2y = _recent_closes("US2YT=RR", 4)
    if len(us2y) >= 4 and us2y[0][0] >= min_ts:
        us2y_bps = (us2y[0][1] - us2y[3][1]) * 100.0
        cap = _env_float(env, "MACRO_US2Y_15M_CAP_BPS", DEFAULT_US2Y_15M_CAP_BPS)
        if us2y_bps >= cap:
            return "cross_asset_us2y_spike", f"US2YT_15m={us2y_bps:+.1f}bps"

    # 3. JPY= 15m drop (Yen surge / carry unwind)
    jpy = _recent_closes("JPY=", 4)
    if len(jpy) >= 4 and jpy[0][0] >= min_ts and jpy[3][1] > 0:
        jpy_ret_pct = ((jpy[0][1] - jpy[3][1]) / jpy[3][1]) * 100.0
        floor = _env_float(env, "MACRO_JPY_15M_FLOOR_PCT", DEFAULT_JPY_15M_FLOOR_PCT)
        if jpy_ret_pct <= floor:
            return "cross_asset_jpy_unwind", f"JPY_15m={jpy_ret_pct:+.3f}%"

    # 4. CME BTC futures basis discount (BTCc1 vs BTC=)
    btcc1 = _recent_closes("BTCc1", 1)
    btc_spot = _recent_closes("BTC=", 1)
    if (
        btcc1
        and btc_spot
        and btcc1[0][0] >= min_ts
        and btc_spot[0][0] >= min_ts
        and abs(btcc1[0][0] - btc_spot[0][0]) <= 300_000
        and btc_spot[0][1] > 0
    ):
        basis_bps = ((btcc1[0][1] - btc_spot[0][1]) / btc_spot[0][1]) * 10000.0
        floor = _env_float(
            env, "MACRO_CME_BASIS_FLOOR_BPS", DEFAULT_CME_BASIS_FLOOR_BPS
        )
        if basis_bps < floor:
            return "cross_asset_cme_basis_discount", f"CME_basis={basis_bps:+.1f}bps"

    # 5. Dealer Microstructure Gate: CME/NQ/HYG Bid-Ask Spread Blowout & Quote Movement Toxicity
    if _env_bool(env, "DECISION_MACRO_MICROSTRUCTURE_GATE"):
        bar_cols = _table_cols(conn, "cross_asset_bars")
        if {"spread_bps", "quote_imbalance"}.issubset(bar_cols):
            max_spread = _env_float(
                env, "MACRO_MAX_DEALER_SPREAD_BPS", DEFAULT_MAX_DEALER_SPREAD_BPS
            )
            max_hyg_spread = _env_float(
                env, "MACRO_MAX_HYG_SPREAD_BPS", DEFAULT_MAX_HYG_SPREAD_BPS
            )
            min_qi = _env_float(
                env, "MACRO_MIN_QUOTE_IMBALANCE", DEFAULT_MIN_QUOTE_IMBALANCE
            )
            min_nq_qi = _env_float(
                env, "MACRO_MIN_NQ_QUOTE_IMBALANCE", DEFAULT_MIN_NQ_QUOTE_IMBALANCE
            )
            for ric in ("BTCc1", "NQc1", "HYG.P"):
                m_row = conn.execute(
                    """
                    SELECT utc_ms, spread_bps, quote_imbalance
                    FROM cross_asset_bars
                    WHERE ric = ? AND interval = '5min' AND utc_ms <= ? AND utc_ms >= ?
                    ORDER BY utc_ms DESC
                    LIMIT 1
                    """,
                    (ric, now_ms, min_ts),
                ).fetchone()
                if m_row is None:
                    continue
                sp = m_row["spread_bps"]
                qi = m_row["quote_imbalance"]
                sp_cap = max_hyg_spread if ric == "HYG.P" else max_spread
                if sp is not None and float(sp) >= sp_cap:
                    return (
                        "cross_asset_dealer_spread_blowout",
                        f"{ric}_spread={float(sp):.1f}bps",
                    )
                qi_floor = (
                    min_nq_qi
                    if (ric == "NQc1" and "MACRO_MIN_QUOTE_IMBALANCE" not in env)
                    else min_qi
                )
                if qi is not None and float(qi) <= qi_floor:
                    return (
                        "cross_asset_dealer_quote_toxicity",
                        f"{ric}_quote_imb={float(qi):+.2f}",
                    )

    # 6. CityU V2 Gates (opt-in via DECISION_MACRO_CITYU_V2_GATE=1):
    #    CME Term Structure (BTCc2 vs BTCc1), JPY 25D Risk Reversal (JPY1MRR=),
    #    Systematic Vol-Control Unwind (SVOL.P), and Treasury Vol (.MOVE)
    if _env_bool(env, "DECISION_MACRO_CITYU_V2_GATE"):
        btcc2 = _recent_closes("BTCc2", 1)
        if (
            btcc1
            and btcc2
            and btcc1[0][0] >= min_ts
            and btcc2[0][0] >= min_ts
            and abs(btcc1[0][0] - btcc2[0][0]) <= 300_000
            and btcc1[0][1] > 0
        ):
            term_bps = ((btcc2[0][1] - btcc1[0][1]) / btcc1[0][1]) * 10000.0
            term_floor = _env_float(env, "MACRO_CME_TERM_FLOOR_BPS", -15.0)
            if term_bps < term_floor:
                return (
                    "cross_asset_cme_term_backwardation",
                    f"BTCc2_vs_BTCc1={term_bps:+.1f}bps",
                )

        jpy_rr = _recent_closes("JPY1MRR=", 4)
        if len(jpy_rr) >= 4 and jpy_rr[0][0] >= min_ts:
            rr_chg = jpy_rr[0][1] - jpy_rr[3][1]
            rr_floor = _env_float(env, "MACRO_JPY_RR_15M_FLOOR", -0.25)
            if rr_chg <= rr_floor:
                return (
                    "cross_asset_jpy_rr_skew_crash",
                    f"JPY1MRR_15m={rr_chg:+.3f}",
                )

        svol = _recent_closes("SVOL.P", 4)
        if len(svol) >= 4 and svol[0][0] >= min_ts and svol[3][1] > 0:
            svol_ret_pct = ((svol[0][1] - svol[3][1]) / svol[3][1]) * 100.0
            svol_floor = _env_float(env, "MACRO_SVOL_15M_FLOOR_PCT", -0.45)
            if svol_ret_pct <= svol_floor:
                return (
                    "cross_asset_vol_control_unwind",
                    f"SVOL_15m={svol_ret_pct:+.3f}%",
                )

        move_idx = _recent_closes(".MOVE", 4)
        if len(move_idx) >= 4 and move_idx[0][0] >= min_ts and move_idx[3][1] > 0:
            move_ret_pct = ((move_idx[0][1] - move_idx[3][1]) / move_idx[3][1]) * 100.0
            move_cap = _env_float(env, "MACRO_MOVE_15M_CAP_PCT", 2.50)
            if move_ret_pct >= move_cap:
                return (
                    "cross_asset_move_vol_spike",
                    f"MOVE_15m={move_ret_pct:+.3f}%",
                )

    return None, None


def _evaluate_dynamic_surprise(
    conn: sqlite3.Connection,
    *,
    now_ms: int,
    r3_before_ms: int,
    r3_after_ms: int,
    env: Mapping[str, str],
) -> tuple[str | None, str | None, bool]:
    """Evaluate SmartEconomics predicted surprise and Out-of-Bounds / In-Line release windows.

    Returns ``(halt_reason, detail, fast_cleared)``.
    """
    ev_cols = _table_cols(conn, "macro_events")
    has_bounds = {"min_forecast", "max_forecast"}.issubset(ev_cols)
    has_smart = {"reuters_poll", "actual", "surprise", "predicted_surprise"}.issubset(ev_cols)
    if not has_smart:
        return None, None, False

    shock_after_ms = _env_int(env, "MACRO_SHOCK_AFTER_MIN", DEFAULT_SHOCK_AFTER_MIN) * 60_000
    inline_after_ms = _env_int(env, "MACRO_INLINE_AFTER_MIN", DEFAULT_INLINE_AFTER_MIN) * 60_000
    shock_ratio = _env_float(env, "MACRO_SURPRISE_SHOCK_RATIO", DEFAULT_SURPRISE_SHOCK_RATIO)
    pred_ratio = _env_float(env, "MACRO_PREDICTED_SURPRISE_RATIO", DEFAULT_PREDICTED_SURPRISE_RATIO)

    select_cols = [
        "country",
        "indicator_name",
        "utc_ts",
        "utc_ms",
        "reuters_poll",
        "actual",
        "surprise",
        "predicted_surprise",
    ]
    if has_bounds:
        select_cols.extend(["min_forecast", "max_forecast"])

    rows = conn.execute(
        f"""
        SELECT {', '.join(select_cols)}
        FROM macro_events
        WHERE relevance = 3
          AND local_time_hkt != ''
          AND utc_ms BETWEEN ? AND ?
        ORDER BY ABS(utc_ms - ?) ASC
        """,
        (now_ms - shock_after_ms, now_ms + r3_before_ms, now_ms),
    ).fetchall()

    for r in rows:
        ev_ms = int(r["utc_ms"])
        poll = float(r["reuters_poll"]) if r["reuters_poll"] is not None else None
        actual = float(r["actual"]) if r["actual"] is not None else None
        surprise = float(r["surprise"]) if r["surprise"] is not None else None
        pred_surp = float(r["predicted_surprise"]) if r["predicted_surprise"] is not None else None
        min_f = float(r["min_forecast"]) if (has_bounds and r["min_forecast"] is not None) else None
        max_f = float(r["max_forecast"]) if (has_bounds and r["max_forecast"] is not None) else None

        # Pre-release SmartEconomics Predicted Surprise Alert (ev_ms > now_ms)
        if ev_ms > now_ms:
            if pred_surp is not None and poll is not None:
                rel_pred = abs(pred_surp) / max(abs(poll), 0.1)
                if rel_pred >= pred_ratio:
                    return (
                        "macro_predicted_surprise_blackout",
                        f"{r['country']}:{r['indicator_name']}@pred_surp={pred_surp:+.3g}",
                        False,
                    )
            return (
                "macro_rel3_blackout",
                f"{r['country']}:{r['indicator_name']}@{r['utc_ts']}",
                False,
            )

        # Post-release (ev_ms <= now_ms): check Out-of-Bounds Shock vs In-Line Fast-Clear
        elapsed_ms = now_ms - ev_ms
        out_of_bounds = (
            actual is not None
            and min_f is not None
            and max_f is not None
            and max_f >= min_f
            and (actual > max_f or actual < min_f)
        )
        large_rel_shock = (
            surprise is not None
            and poll is not None
            and (abs(surprise) / max(abs(poll), 0.1)) >= shock_ratio
        )
        if out_of_bounds or large_rel_shock:
            if elapsed_ms <= shock_after_ms:
                tag = "out_of_bounds" if out_of_bounds else f"surprise={surprise:+.3g}"
                return (
                    "macro_surprise_shock_blackout",
                    f"{r['country']}:{r['indicator_name']}@{tag}",
                    False,
                )

        # Check In-Line Fast-Clear after inline_after_ms (default 5m)
        if elapsed_ms <= r3_after_ms:
            is_inline = False
            if actual is not None and poll is not None:
                if min_f is not None and max_f is not None and max_f > min_f:
                    is_inline = (min_f <= actual <= max_f) and (
                        abs(actual - poll) <= 0.15 * (max_f - min_f)
                    )
                else:
                    is_inline = abs(actual - poll) <= 0.02 * max(abs(poll), 0.1)
            if is_inline and elapsed_ms > inline_after_ms:
                return None, None, True
            return (
                "macro_rel3_blackout",
                f"{r['country']}:{r['indicator_name']}@{r['utc_ts']}",
                False,
            )

    return None, None, False


def evaluate_macro_gate(
    path: str | Path | None,
    *,
    now_ms: int,
    purpose: str = PURPOSE_LIVE_BNB_BOLL_FADE,
    environ: Mapping[str, str] | None = None,
) -> MacroGateStatus:
    """Evaluate calendar blackouts & cross-asset contagion at ``now_ms``.

    Only live Binance spot purposes (``live_bnb_boll_fade``,
    ``live_bnb_boll_classic``) are halted unless
    ``DECISION_MACRO_GATE_ALL_PURPOSES=1`` is set.
    """
    if path is None:
        return MacroGateStatus.unwired()
    env = os.environ if environ is None else environ
    all_purposes = str(env.get("DECISION_MACRO_GATE_ALL_PURPOSES") or "").strip() == "1"
    if not all_purposes and purpose not in LIVE_BNB_SPOT_PURPOSES:
        return MacroGateStatus.unwired()

    try:
        uri = _ro_uri(Path(path).expanduser())
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except (OSError, sqlite3.Error) as exc:
        return MacroGateStatus.failed(f"macro_db_unreadable:{exc.__class__.__name__}")

    try:
        conn.row_factory = sqlite3.Row
        missing = _has_tables(conn, ("macro_events", "central_bank_events"))
        if missing is not None:
            return MacroGateStatus.failed(missing)

        # 1. Central Bank high-impact window [-15m, +45m]
        cb_before_ms = _env_int(env, "MACRO_CB_BEFORE_MIN", DEFAULT_CB_BEFORE_MIN) * 60_000
        cb_after_ms = _env_int(env, "MACRO_CB_AFTER_MIN", DEFAULT_CB_AFTER_MIN) * 60_000
        cb_row = conn.execute(
            """
            SELECT country, event_name, utc_ts
            FROM central_bank_events
            WHERE is_high_impact = 1
              AND local_time_hkt != ''
              AND utc_ms BETWEEN ? AND ?
            ORDER BY ABS(utc_ms - ?) ASC
            LIMIT 1
            """,
            (now_ms - cb_after_ms, now_ms + cb_before_ms, now_ms),
        ).fetchone()
        if cb_row is not None:
            detail = f"{cb_row['country']}:{str(cb_row['event_name'])[:60]}@{cb_row['utc_ts']}"
            return MacroGateStatus.halted("central_bank_blackout", detail)

        # 2. Tier-1 Macro Release (Relevance = 3) window [-15m, +30m] (or dynamic surprise window)
        r3_before_ms = _env_int(env, "MACRO_REL3_BEFORE_MIN", DEFAULT_REL3_BEFORE_MIN) * 60_000
        r3_after_ms = _env_int(env, "MACRO_REL3_AFTER_MIN", DEFAULT_REL3_AFTER_MIN) * 60_000
        if _env_bool(env, "DECISION_MACRO_SURPRISE_DYNAMIC"):
            s_reason, s_detail, fast_cleared = _evaluate_dynamic_surprise(
                conn,
                now_ms=now_ms,
                r3_before_ms=r3_before_ms,
                r3_after_ms=r3_after_ms,
                env=env,
            )
            if s_reason is not None:
                return MacroGateStatus.halted(s_reason, s_detail)
            r3_row = None if fast_cleared else None
        else:
            r3_row = conn.execute(
                """
                SELECT country, indicator_name, utc_ts
                FROM macro_events
                WHERE relevance = 3
                  AND local_time_hkt != ''
                  AND utc_ms BETWEEN ? AND ?
                ORDER BY ABS(utc_ms - ?) ASC
                LIMIT 1
                """,
                (now_ms - r3_after_ms, now_ms + r3_before_ms, now_ms),
            ).fetchone()
        if r3_row is not None:
            detail = f"{r3_row['country']}:{r3_row['indicator_name']}@{r3_row['utc_ts']}"
            return MacroGateStatus.halted("macro_rel3_blackout", detail)

        # 3. Tier-2 US Treasury Auction / High-Vol Rel=2 window [-10m, +20m]
        r2_before_ms = _env_int(env, "MACRO_REL2_BEFORE_MIN", DEFAULT_REL2_BEFORE_MIN) * 60_000
        r2_after_ms = _env_int(env, "MACRO_REL2_AFTER_MIN", DEFAULT_REL2_AFTER_MIN) * 60_000
        block_all_rel2 = str(env.get("DECISION_MACRO_BLOCK_ALL_REL2") or "").strip() == "1"
        if block_all_rel2:
            r2_row = conn.execute(
                """
                SELECT country, indicator_name, utc_ts
                FROM macro_events
                WHERE relevance = 2
                  AND local_time_hkt != ''
                  AND utc_ms BETWEEN ? AND ?
                ORDER BY ABS(utc_ms - ?) ASC
                LIMIT 1
                """,
                (now_ms - r2_after_ms, now_ms + r2_before_ms, now_ms),
            ).fetchone()
        else:
            r2_row = conn.execute(
                """
                SELECT country, indicator_name, utc_ts
                FROM macro_events
                WHERE relevance = 2
                  AND local_time_hkt != ''
                  AND (indicator_name LIKE '%Auc%' OR indicator_name LIKE '%PMI%' OR indicator_name LIKE '%Jobless%')
                  AND utc_ms BETWEEN ? AND ?
                ORDER BY ABS(utc_ms - ?) ASC
                LIMIT 1
                """,
                (now_ms - r2_after_ms, now_ms + r2_before_ms, now_ms),
            ).fetchone()
        if r2_row is not None:
            detail = f"{r2_row['country']}:{r2_row['indicator_name']}@{r2_row['utc_ts']}"
            return MacroGateStatus.halted("macro_rel2_auction_blackout", detail)

        # 4. Cross-Asset Contagion Circuit Breakers
        ca_reason, ca_detail = _check_cross_asset(conn, now_ms, env)
        if ca_reason is not None:
            return MacroGateStatus.halted(ca_reason, ca_detail)

        return MacroGateStatus.clear()
    except sqlite3.Error as exc:
        return MacroGateStatus.failed(f"macro_db_unreadable:{exc.__class__.__name__}")
    finally:
        conn.close()
