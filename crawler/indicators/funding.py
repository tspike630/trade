"""
Funding / JEV indicators (Step 2).

READ ONLY from jev_signals — never calls HTTP.
Maps JEV enricher rows into token_indicators.jev_* fields.
"""

from __future__ import annotations

import re
from typing import Any

from history import history_tier_for_token
from logging_setup import get_logger

from datetime import datetime, timezone

from indicators.base import IndicatorStore, parse_iso, utc_now_iso

log = get_logger("indicators.funding")

# Score bands when stance is inconclusive
BULLISH_SCORE_MIN = 65.0
BEARISH_SCORE_MAX = 35.0

_NOT_BULLISH_RE = re.compile(r"\bNOT\s+BULLISH\b", re.I)
_NOT_BEARISH_RE = re.compile(r"\bNOT\s+BEARISH\b", re.I)
_BULLISH_RE = re.compile(r"\b(BUY|BULLISH|LONG)\b", re.I)
_BEARISH_RE = re.compile(r"\b(SELL|BEARISH|SHORT)\b", re.I)


def map_jev_signal(stance: str | None, raw_score: float | None) -> str:
    """
    Map stance / score → bullish | bearish | neutral.

    Rules (2026-10-21 fix; was: non-directional stance fell through to score
    bands, so NOT BULLISH + score>=65 became bullish):
    1. stance matches SELL/BEARISH/SHORT (and not NOT BEARISH) → bearish
    2. stance matches BUY/BULLISH/LONG (and not NOT BULLISH) → bullish only if
       raw_score is missing or >= BULLISH_SCORE_MIN; else neutral
    3. any other stance (NOT BULLISH / NEUTRAL / INSUFFICIENT EVIDENCE / empty /
       None / unknown) → never bullish. raw_score <= BEARISH_SCORE_MAX → bearish,
       else neutral.
    """
    s = (stance or "").strip()
    if s and _BEARISH_RE.search(s) and not _NOT_BEARISH_RE.search(s):
        return "bearish"
    if s and _BULLISH_RE.search(s) and not _NOT_BULLISH_RE.search(s):
        if raw_score is None or raw_score >= BULLISH_SCORE_MIN:
            return "bullish"
        return "neutral"
    if raw_score is not None and raw_score <= BEARISH_SCORE_MAX:
        return "bearish"
    return "neutral"


def jev_age_minutes(fetched_at: str | None, now: datetime | None = None) -> float | None:
    """Minutes between ``now`` (UTC) and the JEV fetch time; None if unparseable."""
    t = parse_iso(fetched_at) if fetched_at else None
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return round(max(0.0, (now - t).total_seconds() / 60.0), 2)


def direction_coef(signal: str) -> float:
    if signal == "bullish":
        return 1.0
    if signal == "bearish":
        return -1.0
    return 0.0


def compute_funding_fields(
    sig: dict[str, Any], now: datetime | None = None
) -> dict[str, Any] | None:
    """
    Derive token_indicators funding columns from a jev_signals row.

    Formulas:
      jev_raw_score   = jev_signals.jev_score          # 0–100 or null
      jev_confidence  = clip(coverage_pct / 100, 0, 1) # null if coverage missing
      jev_signal      = map_jev_signal(stance, raw)
      jev_strength    = (raw/100) * confidence * direction_coef
                        # range approx [-1, 1]; 0 when neutral or missing inputs
      jev_score       = jev_raw_score                   # compat alias
      jev_funding_bias = direction_coef                 # -1 / 0 / +1
      jev_updated_at  = fetched_at or analyzed_at   # JEV clock, not row updated_at
      jev_age_min     = now - jev fetch time (minutes)
      jev_stance_raw  = jev_signals.stance verbatim
    """
    if sig.get("error") and sig.get("jev_score") is None:
        # Still allow mapping nulls so consumers see a refresh timestamp
        pass

    raw = sig.get("jev_score")
    try:
        raw_f = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        raw_f = None

    cov = sig.get("coverage_pct")
    try:
        cov_f = float(cov) if cov is not None else None
    except (TypeError, ValueError):
        cov_f = None

    confidence: float | None
    if cov_f is None:
        confidence = None
    else:
        confidence = max(0.0, min(1.0, cov_f / 100.0))

    signal = map_jev_signal(sig.get("stance"), raw_f)
    coef = direction_coef(signal)

    # strength = (raw/100) * confidence * direction; missing raw → 0
    if raw_f is None:
        strength = 0.0
    else:
        conf = confidence if confidence is not None else 0.0
        strength = round((raw_f / 100.0) * conf * coef, 6)

    jev_ts = sig.get("fetched_at") or sig.get("analyzed_at")
    updated = jev_ts or utc_now_iso()
    age_min = jev_age_minutes(jev_ts, now)

    return {
        "jev_raw_score": raw_f,
        "jev_score": raw_f,  # compat with Step 1 column
        "jev_signal": signal,
        "jev_confidence": confidence,
        "jev_strength": strength,
        "jev_funding_bias": coef,
        "jev_updated_at": updated,
        "jev_age_min": age_min,
        "jev_stance_raw": sig.get("stance"),
    }


class FundingIndicators:
    """Map jev_signals → token_indicators funding fields (no HTTP)."""

    def __init__(
        self,
        store: IndicatorStore,
        *,
        write_history_for_hot: bool = True,
    ):
        self.store = store
        self.write_history_for_hot = write_history_for_hot

    def resolve_tier(self, chain_id: str, token_address: str) -> str:
        try:
            return history_tier_for_token(self.store, chain_id, token_address)
        except Exception as exc:  # noqa: BLE001
            log.debug("funding tier resolve: %s", exc)
            return "warm"

    def compute_one(self, chain_id: str, token_address: str) -> dict[str, Any] | None:
        sig = self.store.get_jev_signal(chain_id, token_address)
        if not sig:
            log.debug("funding skip no jev_signals %s/%s", chain_id, token_address[:12])
            return None
        fields = compute_funding_fields(sig)
        if not fields:
            return None
        return {
            "chain_id": chain_id,
            "token_address": token_address,
            **fields,
            # Shared row clock only. tech_updated_at stays with the technical writer.
            "updated_at": utc_now_iso(),
        }

    def upsert_one(self, chain_id: str, token_address: str) -> dict[str, Any] | None:
        row = self.compute_one(chain_id, token_address)
        if not row:
            return None
        self.store.upsert_token_indicators(row)
        if self.write_history_for_hot:
            try:
                tier = self.resolve_tier(chain_id, token_address)
                if tier == "hot":
                    prev = self.store.get_token_indicators(
                        chain_id, token_address, use_cache=True
                    ) or {}
                    self.store.insert_indicator_history(
                        {
                            "chain_id": chain_id,
                            "token_address": token_address,
                            "recorded_at": row["updated_at"],
                            "change_5m": prev.get("change_5m"),
                            "change_15m": prev.get("change_15m"),
                            "change_1h": prev.get("change_1h"),
                            "change_4h": prev.get("change_4h"),
                            "change_24h": prev.get("change_24h"),
                            "volatility_24h": prev.get("volatility_24h"),
                            "volume_strength": prev.get("volume_strength"),
                            "jev_score": row.get("jev_raw_score"),
                            "jev_strength": row.get("jev_strength"),
                            "jev_age_min": row.get("jev_age_min"),
                            "jev_stance_raw": row.get("jev_stance_raw"),
                            "sentiment_score": prev.get("sentiment_score"),
                            "composite_score": prev.get("composite_score"),
                            "tier": tier,
                            "primary_pair_address": prev.get("primary_pair_address"),
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("funding history write failed: %s", exc)
        return row

    def run_batch(self, **kwargs: Any) -> dict[str, int]:
        """
        Refresh funding fields for every token that has jev_signals.
        kwargs accepted for API compatibility with run.py (force_full unused —
        funding always refreshes from latest jev_signals snapshot).
        """
        _ = kwargs  # force_full / only_due unused — data already gated by JEV pull
        stats = {"seen": 0, "ok": 0, "skip": 0, "err": 0}
        try:
            signals = self.store.list_tokens_with_jev_signals()
        except Exception as exc:  # noqa: BLE001
            log.warning("funding list jev_signals failed: %s", exc)
            return stats

        for sig in signals:
            stats["seen"] += 1
            chain_id = sig["chain_id"]
            token_address = sig["token_address"]
            try:
                row = self.upsert_one(chain_id, token_address)
                if row:
                    stats["ok"] += 1
                else:
                    stats["skip"] += 1
            except Exception as exc:  # noqa: BLE001
                stats["err"] += 1
                log.warning(
                    "funding failed %s/%s: %s",
                    chain_id,
                    token_address[:12],
                    exc,
                )
        return stats
