"""Standard signal structure + dry-run sink (log + decision_log table).

``purpose`` is separate from ``mode``. ``mode`` stays ``dry_run`` / ``live_stub``.
``purpose`` tells the execution gateway which dry-run rows are paper-eligible:

- ``sample`` — weekly check / sampling runs (Mon/Wed/Fri, threshold below the
  resident default, typically ``--min-jev-strength 0.15``). Not paper-eligible.
  The gateway should reject these; this layer only writes the tag.
- ``live_candidate`` — resident dry-run loop/once and any other paper-eligible
  path (default threshold 0.3). This is the default.

Storage contract (what execution reads):
- Column ``decision_log.purpose`` (added on existing databases with ALTER TABLE;
  historical rows stay NULL — they are not backfilled to ``live_candidate``).
- Top-level ``purpose`` inside ``raw_json``. The gateway SELECT already loads
  ``raw_json``, so ``json.loads(raw_json)["purpose"]`` is visible before the
  gateway adds the column to its SELECT.

``extras.pairCreatedAt`` (optional, ms epoch int): copied from the signal when
present, else looked up from ``pairs.pair_created_at`` at write time. Same unit
as the DexScreener / pairs column. Review ``token_age`` reads this from
``decision_log.raw_json``; historical rows without it stay unknown (join pairs
as a fallback outside this writer). No gate / weight / run-param change.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from logging_setup import get_logger

log = get_logger("decision_engine.signal_bus")

# Matches sqlite3.connect(timeout=30). One extra attempt after a short pause.
_BUSY_TIMEOUT_MS = 30000
_PUBLISH_RETRY_SLEEP_SEC = 0.2

# purpose enum. Do not reuse mode for this; dry_run covers both producers.
PURPOSE_SAMPLE = "sample"
PURPOSE_LIVE_CANDIDATE = "live_candidate"
PURPOSE_SHADOW_010 = "shadow_010"  # shadow entry book; never paper-eligible (gateway rejects)
PURPOSE_SHADOW_SELFBUILT_A = "shadow_selfbuilt_a"  # parallel selfbuilt A; never paper-eligible
PURPOSE_SHADOW_ENTRY_IH_GE1H = "shadow_entry_ih_ge1h"  # entry_ih≥1h sidebook; gateway rejects
# Equal-status paper race arms (isolated DBs). Main gateway rejects non-live_candidate.
PURPOSE_RACE_SELFBUILT_A = "race_selfbuilt_a"  # peer A book; not main live_candidate
PURPOSE_RACE_RSI_BOUNCE = "race_rsi_bounce"
PURPOSE_RACE_MACD_CROSS = "race_macd_cross"
PURPOSE_RACE_BOLL_BREAKOUT = "race_boll_breakout"
PURPOSE_RACE_TECH_MOM = "race_tech_mom"
PURPOSE_RACE_JEV_TIMEEXIT = "race_jev_timeexit"
PURPOSE_RACE_JEV_TRAIL = "race_jev_trail"
PURPOSE_RACE_JEV_RSI_AND = "race_jev_rsi_and"
PURPOSE_RACE_COMBINED_JEV_TECH = "race_combined_jev_tech"
PURPOSE_RACE_RSI_MACD_AND = "race_rsi_macd_and"
PURPOSE_RACE_MACD_BOLL_AND = "race_macd_boll_and"
PURPOSE_RACE_RSI_BOLL_AND = "race_rsi_boll_and"
PURPOSE_RACE_TECH_MACD_AND = "race_tech_macd_and"
PURPOSE_RACE_TECH_RSI_AND = "race_tech_rsi_and"
PURPOSE_RACE_TECH_BOLL_AND = "race_tech_boll_and"
PURPOSE_RACE_JEV_MACD_AND = "race_jev_macd_and"
PURPOSE_RACE_JEV_BOLL_AND = "race_jev_boll_and"
PURPOSE_RACE_RSI_MACD_TIMEEXIT = "race_rsi_macd_timeexit"
PURPOSE_RACE_MACD_TRAIL = "race_macd_trail"
PURPOSE_RACE_JEV_STRENGTH = "race_jev_strength"
PURPOSE_RACE_EXIT_TRAIL_TDECAY = "race_exit_trail_tdecay"
PURPOSE_RACE_FILT_OBS_WINDOW = "race_filt_obs_window"
PURPOSE_RACE_EXIT_TIER_72H7D = "race_exit_tier_72h7d"
PURPOSE_RACE_EXIT_TIMESTOP_48H = "race_exit_timestop_48h"
PURPOSE_RACE_SWING_ESTABLISHED = "race_swing_established"
PURPOSE_BENCH_ARTEMIS_F1 = "bench_artemis_f1"
PURPOSE_RACE_REL_STRENGTH_M = "race_rel_strength_m"
PURPOSE_RACE_A_TRAIL = "race_a_trail"
PURPOSE_RACE_A_TIMEEXIT_1H = "race_a_timeexit_1h"
PURPOSE_RACE_A_TIMESTOP_48H = "race_a_timestop_48h"
PURPOSE_RACE_A_TIER_72H7D = "race_a_tier_72h7d"

PURPOSE_RACE_VOL_SPIKE_ENTRY = "race_vol_spike_entry"
PURPOSE_RACE_LIQ_BUCKET_HIGH = "race_liq_bucket_high"
PURPOSE_RACE_LIQ_BUCKET_MID = "race_liq_bucket_mid"
PURPOSE_RACE_AND_SELFBUILT_MACD = "race_and_selfbuilt_macd"
PURPOSE_RACE_AND_VOL_SQUEEZE_BREAK = "race_and_vol_squeeze_break"
PURPOSE_RACE_FILT_KILLFIRST_QF = "race_filt_killfirst_qf"
PURPOSE_RACE_FILT_OBS_BS_RATIO = "race_filt_obs_bs_ratio"
PURPOSE_RACE_FILT_OBS_BUYERS_N = "race_filt_obs_buyers_n"
PURPOSE_RACE_REL_STRENGTH_10D = "race_rel_strength_10d"
PURPOSE_RACE_SWING_PULLBACK = "race_swing_pullback"
PURPOSE_RACE_FILT_LIQ_LADDER = "race_filt_liq_ladder"
PURPOSE_RACE_VOL_CLIMAX_MCAP_BAND = "race_vol_climax_mcap_band"
PURPOSE_RACE_CS_MOM_1W = "race_cs_mom_1w"
PURPOSE_RACE_LIFECYCLE_MOMENTUM = "race_lifecycle_momentum"
PURPOSE_RACE_FUNDING_Z_FADE = "race_funding_z_fade"
PURPOSE_RACE_AND_VOL_CLIMAX_SWING = "race_and_vol_climax_swing"
PURPOSE_RACE_AND_VOL_CLIMAX_SELFBUILT = "race_and_vol_climax_selfbuilt"
PURPOSE_RACE_AND_SWING_SELFBUILT = "race_and_swing_selfbuilt"
PURPOSE_RACE_TECH_FADE = "race_tech_fade"
PURPOSE_RACE_CS_FADE_1W = "race_cs_fade_1w"
PURPOSE_RACE_RS_FADE_10D = "race_rs_fade_10d"
PURPOSE_RACE_VOL_SPIKE_FADE = "race_vol_spike_fade"
PURPOSE_RACE_RSI_BOUNCE_FADE = "race_rsi_bounce_fade"
PURPOSE_RACE_TECH_BOLL_FADE = "race_tech_boll_fade"
PURPOSE_RACE_RSI_BOLL_FADE = "race_rsi_boll_fade"
PURPOSE_RACE_VOL_SQUEEZE_FADE = "race_vol_squeeze_fade"
PURPOSE_RACE_CS_MOM_1W_R90 = "race_cs_mom_1w_r90"
PURPOSE_RACE_RSI_BOUNCE_R90 = "race_rsi_bounce_r90"
PURPOSE_RACE_PORTFOLIO_RISK_SLEEVES = "race_portfolio_risk_sleeves"  # legacy drain
PURPOSE_RACE_PORTFOLIO_RISK = "race_portfolio_risk"  # locked tonight primary
PURPOSE_RACE_PORTFOLIO_TIGHT_SL = "race_portfolio_tight_sl"
PURPOSE_RACE_PORTFOLIO_AND_BLEND = "race_portfolio_and_blend"  # side experiment
PURPOSES = frozenset(
    {
        PURPOSE_SAMPLE,
        PURPOSE_LIVE_CANDIDATE,
        PURPOSE_SHADOW_010,
        PURPOSE_SHADOW_SELFBUILT_A,
        PURPOSE_SHADOW_ENTRY_IH_GE1H,
        PURPOSE_RACE_SELFBUILT_A,
        PURPOSE_RACE_RSI_BOUNCE,
        PURPOSE_RACE_MACD_CROSS,
        PURPOSE_RACE_BOLL_BREAKOUT,
        PURPOSE_RACE_TECH_MOM,
        PURPOSE_RACE_JEV_TIMEEXIT,
        PURPOSE_RACE_JEV_TRAIL,
        PURPOSE_RACE_JEV_RSI_AND,
        PURPOSE_RACE_COMBINED_JEV_TECH,
        PURPOSE_RACE_RSI_MACD_AND,
        PURPOSE_RACE_MACD_BOLL_AND,
        PURPOSE_RACE_RSI_BOLL_AND,
        PURPOSE_RACE_TECH_MACD_AND,
        PURPOSE_RACE_TECH_RSI_AND,
        PURPOSE_RACE_TECH_BOLL_AND,
        PURPOSE_RACE_JEV_MACD_AND,
        PURPOSE_RACE_JEV_BOLL_AND,
        PURPOSE_RACE_RSI_MACD_TIMEEXIT,
        PURPOSE_RACE_MACD_TRAIL,
        PURPOSE_RACE_JEV_STRENGTH,
        PURPOSE_RACE_EXIT_TRAIL_TDECAY,
        PURPOSE_RACE_FILT_OBS_WINDOW,
        PURPOSE_RACE_EXIT_TIER_72H7D,
        PURPOSE_RACE_EXIT_TIMESTOP_48H,
        PURPOSE_RACE_SWING_ESTABLISHED,
        PURPOSE_BENCH_ARTEMIS_F1,
        PURPOSE_RACE_REL_STRENGTH_M,
        PURPOSE_RACE_A_TRAIL,
        PURPOSE_RACE_A_TIMEEXIT_1H,
        PURPOSE_RACE_A_TIMESTOP_48H,
        PURPOSE_RACE_A_TIER_72H7D,
        PURPOSE_RACE_VOL_SPIKE_ENTRY,
        PURPOSE_RACE_LIQ_BUCKET_HIGH,
        PURPOSE_RACE_LIQ_BUCKET_MID,
        PURPOSE_RACE_AND_SELFBUILT_MACD,
        PURPOSE_RACE_AND_VOL_SQUEEZE_BREAK,
        PURPOSE_RACE_FILT_KILLFIRST_QF,
        PURPOSE_RACE_FILT_OBS_BS_RATIO,
        PURPOSE_RACE_FILT_OBS_BUYERS_N,
        PURPOSE_RACE_REL_STRENGTH_10D,
        PURPOSE_RACE_SWING_PULLBACK,
        PURPOSE_RACE_FILT_LIQ_LADDER,
        PURPOSE_RACE_VOL_CLIMAX_MCAP_BAND,
        PURPOSE_RACE_CS_MOM_1W,
        PURPOSE_RACE_LIFECYCLE_MOMENTUM,
        PURPOSE_RACE_FUNDING_Z_FADE,
        PURPOSE_RACE_AND_VOL_CLIMAX_SWING,
        PURPOSE_RACE_AND_VOL_CLIMAX_SELFBUILT,
        PURPOSE_RACE_AND_SWING_SELFBUILT,
        PURPOSE_RACE_TECH_FADE,
        PURPOSE_RACE_CS_FADE_1W,
        PURPOSE_RACE_RS_FADE_10D,
        PURPOSE_RACE_VOL_SPIKE_FADE,
        PURPOSE_RACE_RSI_BOUNCE_FADE,
        PURPOSE_RACE_TECH_BOLL_FADE,
        PURPOSE_RACE_RSI_BOLL_FADE,
        PURPOSE_RACE_VOL_SQUEEZE_FADE,
        PURPOSE_RACE_CS_MOM_1W_R90,
        PURPOSE_RACE_RSI_BOUNCE_R90,
        PURPOSE_RACE_PORTFOLIO_RISK_SLEEVES,
        PURPOSE_RACE_PORTFOLIO_RISK,
        PURPOSE_RACE_PORTFOLIO_TIGHT_SL,
        PURPOSE_RACE_PORTFOLIO_AND_BLEND,
    }
)
DEFAULT_PURPOSE = PURPOSE_LIVE_CANDIDATE

DECISION_LOG_DDL = """
CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    action TEXT NOT NULL,
    chain_id TEXT,
    token_address TEXT,
    size_pct REAL,
    reason TEXT,
    risk_passed INTEGER,
    mode TEXT NOT NULL,
    purpose TEXT,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_decision_log_ts ON decision_log (ts);
CREATE INDEX IF NOT EXISTS idx_decision_log_strategy ON decision_log (strategy_id);
"""

# Created after the purpose column exists. Not part of DECISION_LOG_DDL:
# on an older table the index would run before ALTER and fail.
_PURPOSE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_decision_log_purpose ON decision_log (purpose)"
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _known_purpose(value: Any) -> str | None:
    if value is None:
        return None
    name = str(value).strip().lower()
    if name in PURPOSES:
        return name
    return None


def resolve_run_purpose(
    explicit: str | None,
    *,
    min_jev_strength: float | None,
    live_min_strength: float,
) -> str:
    """Purpose for one ``decision_engine.run`` invocation.

    Resident loop and ``--once`` (no extra flags) are ``live_candidate``.
    The WEEKLY_CHECK sampling command passes ``--min-jev-strength`` below the
    live default (0.15 vs 0.3); that path is ``sample`` even if ``--purpose``
    is omitted, so an existing routine still tags rows. An explicit
    ``--purpose`` always wins. ``mode`` is not involved.
    """
    if explicit is not None and str(explicit).strip():
        chosen = _known_purpose(explicit)
        if chosen is None:
            raise ValueError(
                f"unknown purpose: {explicit!r} (expected one of {sorted(PURPOSES)})"
            )
        return chosen
    if min_jev_strength is not None and float(min_jev_strength) < float(live_min_strength):
        return PURPOSE_SAMPLE
    return PURPOSE_LIVE_CANDIDATE


def coerce_pair_created_at_ms(value: Any) -> int | None:
    """Normalize pair creation time to ms epoch int, matching pairs.pair_created_at.

    Accepts int/float ms (or seconds heuristically), or numeric strings.
    Returns None for missing / invalid — never invents a timestamp.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
    elif isinstance(value, str):
        try:
            n = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if n > 1e12:  # already ms
        return int(n)
    if n > 1e9:  # seconds → ms
        return int(n * 1000.0)
    return None


def _json_safe_age(v: Any) -> float | None:
    """Finite float or None (NaN/inf/garbage -> None so raw_json stays valid JSON)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def normalize_signal(
    sig: dict[str, Any],
    *,
    mode: str = "dry_run",
    purpose: str = DEFAULT_PURPOSE,
) -> dict[str, Any]:
    """Ensure required keys; fill defaults. ``purpose`` is always one of PURPOSES."""
    fallback = _known_purpose(purpose) or DEFAULT_PURPOSE
    if "purpose" not in sig or sig.get("purpose") is None or str(sig.get("purpose")).strip() == "":
        chosen = fallback
    else:
        chosen = _known_purpose(sig.get("purpose"))
        if chosen is None:
            log.warning(
                "unknown decision purpose %r; using %s",
                sig.get("purpose"),
                fallback,
            )
            chosen = fallback
    out = {
        "strategy_id": str(sig.get("strategy_id") or "unknown"),
        "action": str(sig.get("action") or "noop").lower(),
        "chain_id": sig.get("chain_id"),
        "token_address": sig.get("token_address"),
        "pair_address": sig.get("pair_address"),
        "size_pct": float(sig["size_pct"]) if sig.get("size_pct") is not None else None,
        "reason": sig.get("reason") or "",
        "ts": sig.get("ts") or utc_now_iso(),
        "risk_passed": bool(sig.get("risk_passed", False)),
        "mode": mode,
        "purpose": chosen,
    }
    # preserve extras in raw
    extras = {k: v for k, v in sig.items() if k not in out}
    if "jev_age_min" in extras:
        # Logging only (no gate change). Only signals that came through the jev
        # gate carry this key; null then means "gated but age unknown".
        # Non-gated strategies get neither field.
        extras["jev_age_min"] = _json_safe_age(extras.get("jev_age_min"))
        if extras.get("jev_stance_raw") is None:
            extras.pop("jev_stance_raw", None)
    else:
        extras.pop("jev_stance_raw", None)
    if extras:
        out["_extras"] = extras
    return out


class SignalBus:
    """
    Collect candidate signals; in dry-run emit structured logs and write decision_log
    via a dedicated RW connection (separate from indicator RO adapter).

    Every published row gets ``purpose`` (bus default, or the signal's own value
    when it is a known purpose). Default is ``live_candidate``.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        dry_run: bool = True,
        purpose: str = DEFAULT_PURPOSE,
    ):
        chosen = _known_purpose(purpose)
        if chosen is None:
            raise ValueError(
                f"purpose must be one of {sorted(PURPOSES)}, got {purpose!r}"
            )
        self.db_path = Path(db_path).resolve()
        self.dry_run = dry_run
        self.mode = "dry_run" if dry_run else "live_stub"
        self.purpose = chosen
        self._rw: sqlite3.Connection | None = None
        self._emitted: list[dict[str, Any]] = []
        self._ensure_rw()

    def _ensure_rw(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._rw = sqlite3.connect(str(self.db_path), timeout=30.0)
        self._rw.row_factory = sqlite3.Row
        self._rw.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        self._ensure_schema()
        log.info(
            "signal_bus decision_log ready db=%s mode=%s purpose=%s",
            self.db_path,
            self.mode,
            self.purpose,
        )

    def _ensure_schema(self) -> None:
        """Create decision_log and add ``purpose`` on databases that predate it.

        CREATE TABLE IF NOT EXISTS does not add columns. ALTER has no DEFAULT
        so rows written before P2-38 stay NULL instead of being relabeled
        ``live_candidate`` (those rows include the 0.15 sampling history).
        """
        assert self._rw is not None
        self._rw.executescript(DECISION_LOG_DDL)
        have = {
            row[1] for row in self._rw.execute("PRAGMA table_info(decision_log)").fetchall()
        }
        if "purpose" not in have:
            self._rw.execute("ALTER TABLE decision_log ADD COLUMN purpose TEXT")
        self._rw.execute(_PURPOSE_INDEX_SQL)
        self._rw.commit()

    def close(self) -> None:
        if self._rw is not None:
            self._rw.close()
            self._rw = None

    def __enter__(self) -> "SignalBus":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @property
    def emitted(self) -> list[dict[str, Any]]:
        return list(self._emitted)

    def publish(self, signal: dict[str, Any]) -> dict[str, Any] | None:
        self._ensure_pair_created_at(signal)
        norm = normalize_signal(signal, mode=self.mode, purpose=self.purpose)
        log.info(
            "SIGNAL strategy=%s action=%s chain=%s token=%s size_pct=%s risk=%s "
            "reason=%s mode=%s purpose=%s",
            norm["strategy_id"],
            norm["action"],
            norm["chain_id"],
            (norm["token_address"] or "")[:16],
            norm["size_pct"],
            norm["risk_passed"],
            norm["reason"],
            norm["mode"],
            norm["purpose"],
        )
        if not self._persist(norm):
            return None
        self._emitted.append(norm)
        return norm

    def publish_many(self, signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for sig in signals:
            norm = self.publish(sig)
            if norm is not None:
                out.append(norm)
        return out

    def _ensure_pair_created_at(self, signal: dict[str, Any]) -> None:
        """Ensure ``pairCreatedAt`` (ms) is on the signal so it lands in extras.

        Prefer an already-stamped value (engine / strategy). Otherwise look up
        ``pairs.pair_created_at`` by chain_id + pair_address on this bus DB.
        Does not invent; leaves the key absent when unknown. Not a gate.
        """
        existing = coerce_pair_created_at_ms(signal.get("pairCreatedAt"))
        if existing is None:
            existing = coerce_pair_created_at_ms(signal.get("pair_created_at"))
        if existing is not None:
            signal["pairCreatedAt"] = existing
            # Avoid duplicating snake_case into extras alongside camelCase.
            signal.pop("pair_created_at", None)
            return
        chain = signal.get("chain_id")
        pair = signal.get("pair_address")
        if not chain or pair is None or str(pair).strip() == "":
            return
        looked = self._lookup_pair_created_at(str(chain), str(pair))
        if looked is not None:
            signal["pairCreatedAt"] = looked
            signal.pop("pair_created_at", None)

    def _lookup_pair_created_at(self, chain_id: str, pair_address: str) -> int | None:
        assert self._rw is not None
        try:
            cols = {
                row[1]
                for row in self._rw.execute("PRAGMA table_info(pairs)").fetchall()
            }
        except sqlite3.Error:
            return None
        if "pair_created_at" not in cols:
            return None
        try:
            row = self._rw.execute(
                """
                SELECT pair_created_at FROM pairs
                WHERE chain_id=? AND pair_address=?
                  AND pair_created_at IS NOT NULL
                LIMIT 1
                """,
                (chain_id, pair_address),
            ).fetchone()
        except sqlite3.Error as exc:
            log.warning(
                "pair_created_at lookup failed chain=%s pair=%s err=%s",
                chain_id,
                pair_address[:16],
                exc,
            )
            return None
        if row is None or row[0] is None:
            return None
        return coerce_pair_created_at_ms(row[0])

    def _persist(self, norm: dict[str, Any]) -> bool:
        """Write one row. On failure, retry once and warn — do not abort the batch."""
        try:
            self._write_log(norm)
            return True
        except sqlite3.Error as exc:
            log.warning(
                "decision_log publish failed; retrying once strategy=%s action=%s token=%s err=%s",
                norm.get("strategy_id"),
                norm.get("action"),
                (norm.get("token_address") or "")[:16],
                exc,
            )
            time.sleep(_PUBLISH_RETRY_SLEEP_SEC)
            try:
                self._write_log(norm)
                return True
            except sqlite3.Error as exc2:
                log.warning(
                    "decision_log publish dropped after retry strategy=%s action=%s token=%s err=%s",
                    norm.get("strategy_id"),
                    norm.get("action"),
                    (norm.get("token_address") or "")[:16],
                    exc2,
                )
                return False

    def _write_log(self, norm: dict[str, Any]) -> None:
        assert self._rw is not None
        raw = {k: v for k, v in norm.items() if k != "_extras"}
        if "_extras" in norm:
            raw["extras"] = norm["_extras"]
        try:
            self._rw.execute(
                """
                INSERT INTO decision_log
                    (ts, strategy_id, action, chain_id, token_address, size_pct,
                     reason, risk_passed, mode, purpose, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    norm["ts"],
                    norm["strategy_id"],
                    norm["action"],
                    norm.get("chain_id"),
                    norm.get("token_address"),
                    norm.get("size_pct"),
                    norm.get("reason"),
                    1 if norm.get("risk_passed") else 0,
                    norm["mode"],
                    norm["purpose"],
                    json.dumps(raw, ensure_ascii=False, default=str),
                ),
            )
            self._rw.commit()
        except sqlite3.Error:
            try:
                self._rw.rollback()
            except sqlite3.Error:
                pass
            raise
