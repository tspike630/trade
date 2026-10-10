"""
Sentiment indicators (Step 3).

READ ONLY from x_watch / alerts (social_spike, sentiment_anomaly) /
optional indicator_history snapshots — never calls X HTTP.
Writes only token_indicators sentiment_* fields (+ hot indicator_history).
"""

from __future__ import annotations

import math
import re
from typing import Any

from history import history_tier_for_token
from logging_setup import get_logger

from config import (
    SENTIMENT_ALERT_LOOKBACK_SEC,
    SENTIMENT_ENABLED,
    SENTIMENT_WRITE_HISTORY,
)
from indicators.base import IndicatorStore, to_epoch, utc_now_iso

log = get_logger("indicators.sentiment")

# --- keyword polarity lexicon (simple rule-based; EN crypto slang) ---
_BULLISH_WORDS = (
    "ape", "aped", "aping", "moon", "mooning", "parabolic", "pump", "pumping",
    "bullish", "bull", "long", "buy", "buying", "gem", "breakout", "rocket",
    "lfg", "send it", "sending", "accumulate", "accumulating", "ath",
    "rally", "squeeze", "going up", "to the moon",
)
_BEARISH_WORDS = (
    "dump", "dumping", "dumped", "rug", "rugpull", "rug pull", "scam",
    "bearish", "bear", "short", "sell", "selling", "exit", "honeypot",
    "dead", "rekt", "crash", "crashing", "liquidated", "avoid", "fake",
)

# Word-boundary-ish match; multi-word phrases checked as substrings first
_BULLISH_RE = [
    re.compile(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", re.I)
    if " " not in w
    else re.compile(re.escape(w), re.I)
    for w in _BULLISH_WORDS
]
_BEARISH_RE = [
    re.compile(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", re.I)
    if " " not in w
    else re.compile(re.escape(w), re.I)
    for w in _BEARISH_WORDS
]


def score_polarity_from_texts(texts: list[str] | None) -> float:
    """
    Keyword polarity in [-1, 1].
    (bull_hits - bear_hits) / (bull_hits + bear_hits); 0 if no hits.
    """
    if not texts:
        return 0.0
    bull = 0
    bear = 0
    for raw in texts:
        if not raw:
            continue
        text = str(raw)
        for rx in _BULLISH_RE:
            bull += len(rx.findall(text))
        for rx in _BEARISH_RE:
            bear += len(rx.findall(text))
    total = bull + bear
    if total <= 0:
        return 0.0
    return round((bull - bear) / total, 6)


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _age_hours(iso_ts: str | None, now_epoch: float | None = None) -> float:
    ep = to_epoch(iso_ts)
    if ep is None:
        return 1.0 / 60.0
    now = now_epoch if now_epoch is not None else __import__("time").time()
    return max((now - ep) / 3600.0, 1.0 / 60.0)


def estimate_mention_windows(watch: dict[str, Any]) -> tuple[float, float, float]:
    """
    Approximate mention counts for 1h / 6h / 24h from x_watch counters.

    x_watch only stores last/prev/total (no tweet time series):
      mentions_1h  ≈ mention_count_last   (latest search window)
      mentions_6h  ≈ total if age≤6h else max(last, total * 6/age)
      mentions_24h ≈ total if age≤24h else max(last, total * 24/age)
    """
    last = float(int(watch.get("mention_count_last") or 0))
    total = float(int(watch.get("mention_count_total") or 0))
    age_h = _age_hours(watch.get("created_at"))

    m1 = last
    if age_h <= 6.0:
        m6 = total
    else:
        m6 = max(last, total * (6.0 / age_h))
    if age_h <= 24.0:
        m24 = total
    else:
        m24 = max(last, total * (24.0 / age_h))
    return m1, m6, m24


def growth_from_prev_last(prev: int, last: int) -> float:
    """Consecutive-search velocity growth ratio."""
    prev = max(0, int(prev))
    last = max(0, int(last))
    if prev <= 0:
        return 1.0 if last > 0 else 0.0
    return (last - prev) / float(prev)


def growth_vs_baseline(current: float, baseline: float | None) -> float | None:
    if baseline is None:
        return None
    base = max(float(baseline), 1.0)
    return (float(current) - float(baseline)) / base


def compute_heat_score(
    mentions_1h: float,
    growth_1h: float | None,
    *,
    recent_spike: bool = False,
    recent_anomaly: bool = False,
) -> float:
    """
    Social heat 0–100.
      base = log10(m1+1)/2.5 * 60
      + clip(growth_1h,0,3)*10
      + spike/anomaly boost
    """
    m1 = max(0.0, float(mentions_1h))
    base = math.log10(m1 + 1.0) / 2.5 * 60.0
    g = float(growth_1h) if growth_1h is not None else 0.0
    growth_boost = _clip(g, 0.0, 3.0) * 10.0
    boost = 0.0
    if recent_anomaly:
        boost += 20.0
    elif recent_spike:
        boost += 12.0
    return round(_clip(base + growth_boost + boost, 0.0, 100.0), 6)


def blend_polarity(
    *,
    keyword_polarity: float,
    growth_1h: float | None,
    recent_spike: bool,
    recent_anomaly: bool,
) -> float:
    """
    Combine keyword score with alert-type / growth heuristics → [-1, 1].

    Tweet bodies are not persisted in x_watch; keyword score usually 0 unless
    alert messages (or future text sources) contain lexicon hits.
    """
    kw = _clip(float(keyword_polarity), -1.0, 1.0)
    alert = 0.0
    if recent_anomaly:
        alert = 0.55
    elif recent_spike:
        alert = 0.35
    g = float(growth_1h) if growth_1h is not None else 0.0
    growth_bias = _clip(g * 0.15, -0.35, 0.35)
    # Prefer keywords when present; otherwise alert + growth
    if abs(kw) > 1e-9:
        out = 0.6 * kw + 0.25 * alert + 0.15 * growth_bias
    else:
        out = 0.65 * alert + 0.35 * growth_bias
    return round(_clip(out, -1.0, 1.0), 6)


def compute_sentiment_fields(
    watch: dict[str, Any],
    *,
    texts: list[str] | None = None,
    recent_spike: bool = False,
    recent_anomaly: bool = False,
    hist_mentions_1h: float | None = None,
    hist_mentions_6h: float | None = None,
    hist_mentions_24h: float | None = None,
) -> dict[str, Any]:
    """
    Derive token_indicators sentiment columns from an x_watch row (+ optional context).

    Formulas (see INDICATORS_STEP3.md):
      mentions_1h/6h/24h  — estimate_mention_windows
      growth_1h           — history vs ~1h ago if present else (last-prev)/prev
      growth_6h / 24h     — vs history baseline or vs average rate
      heat_score          — compute_heat_score
      polarity            — blend_polarity(keyword + alerts + growth)
      sentiment_score     — (heat/100) * polarity   ∈ approx [-1, 1]
    """
    last = int(watch.get("mention_count_last") or 0)
    prev = int(watch.get("mention_count_prev") or 0)
    m1, m6, m24 = estimate_mention_windows(watch)

    g1_inst = growth_from_prev_last(prev, last)
    g1_hist = growth_vs_baseline(m1, hist_mentions_1h)
    g1 = g1_hist if g1_hist is not None else g1_inst

    g6_hist = growth_vs_baseline(m6, hist_mentions_6h)
    if g6_hist is not None:
        g6 = g6_hist
    else:
        # rate now (≈1h window) vs average over ≤6h span
        age_h = min(_age_hours(watch.get("created_at")), 6.0)
        avg = m6 / max(age_h, 1.0 / 60.0)
        g6 = growth_vs_baseline(m1, avg) if avg > 0 else (1.0 if m1 > 0 else 0.0)

    g24_hist = growth_vs_baseline(m24, hist_mentions_24h)
    if g24_hist is not None:
        g24 = g24_hist
    else:
        age_h = min(_age_hours(watch.get("created_at")), 24.0)
        avg = m24 / max(age_h, 1.0 / 60.0)
        g24 = growth_vs_baseline(m1, avg) if avg > 0 else (1.0 if m1 > 0 else 0.0)

    kw = score_polarity_from_texts(texts)
    polarity = blend_polarity(
        keyword_polarity=kw,
        growth_1h=g1,
        recent_spike=recent_spike,
        recent_anomaly=recent_anomaly,
    )
    heat = compute_heat_score(
        m1, g1, recent_spike=recent_spike, recent_anomaly=recent_anomaly
    )
    score = round((heat / 100.0) * polarity, 6)
    updated = watch.get("last_searched_at") or watch.get("updated_at") or utc_now_iso()

    return {
        "sentiment_mentions_1h": round(m1, 6),
        "sentiment_mentions_6h": round(m6, 6),
        "sentiment_mentions_24h": round(m24, 6),
        "sentiment_growth_1h": round(float(g1), 6) if g1 is not None else None,
        "sentiment_growth_6h": round(float(g6), 6) if g6 is not None else None,
        "sentiment_growth_24h": round(float(g24), 6) if g24 is not None else None,
        "sentiment_heat_score": heat,
        "sentiment_polarity": polarity,
        "sentiment_score": score,
        "sentiment_updated_at": updated,
    }


class SentimentIndicators:
    """Map x_watch (+ social alerts) → token_indicators sentiment fields (no HTTP)."""

    def __init__(
        self,
        store: IndicatorStore,
        *,
        write_history_for_hot: bool | None = None,
        enabled: bool | None = None,
    ):
        self.store = store
        self.write_history_for_hot = (
            SENTIMENT_WRITE_HISTORY
            if write_history_for_hot is None
            else write_history_for_hot
        )
        self.enabled = SENTIMENT_ENABLED if enabled is None else enabled

    def resolve_tier(self, chain_id: str, token_address: str) -> str:
        try:
            return history_tier_for_token(self.store, chain_id, token_address)
        except Exception as exc:  # noqa: BLE001
            log.debug("sentiment tier resolve: %s", exc)
            return "warm"

    def _history_mention_baseline(
        self, chain_id: str, token_address: str, lookback_sec: int
    ) -> float | None:
        row = self.store.get_indicator_history_near(
            chain_id, token_address, lookback_sec=lookback_sec
        )
        if not row:
            return None
        v = row.get("sentiment_mentions_1h")
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def compute_one(self, chain_id: str, token_address: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        watch = self.store.get_x_watch(chain_id, token_address)
        if not watch:
            log.debug("sentiment skip no x_watch %s/%s", chain_id, token_address[:12])
            return None

        # Skip never-searched empty watches (no signal yet)
        last = int(watch.get("mention_count_last") or 0)
        prev = int(watch.get("mention_count_prev") or 0)
        total = int(watch.get("mention_count_total") or 0)
        searched = watch.get("last_searched_at")
        if searched is None and total <= 0 and last <= 0 and prev <= 0:
            log.debug(
                "sentiment skip never-searched %s/%s", chain_id, token_address[:12]
            )
            return None

        social = self.store.get_recent_social_flags(
            chain_id, token_address, within_sec=SENTIMENT_ALERT_LOOKBACK_SEC
        )
        texts = list(social.get("texts") or [])
        fields = compute_sentiment_fields(
            watch,
            texts=texts,
            recent_spike=bool(social.get("spike")),
            recent_anomaly=bool(social.get("anomaly")),
            hist_mentions_1h=self._history_mention_baseline(
                chain_id, token_address, 3600
            ),
            hist_mentions_6h=self._history_mention_baseline(
                chain_id, token_address, 6 * 3600
            ),
            hist_mentions_24h=self._history_mention_baseline(
                chain_id, token_address, 24 * 3600
            ),
        )
        return {
            "chain_id": chain_id,
            "token_address": token_address,
            **fields,
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
                            "jev_score": prev.get("jev_raw_score") or prev.get("jev_score"),
                            "jev_strength": prev.get("jev_strength"),
                            "sentiment_score": row.get("sentiment_score"),
                            "sentiment_mentions_1h": row.get("sentiment_mentions_1h"),
                            "sentiment_heat_score": row.get("sentiment_heat_score"),
                            "sentiment_polarity": row.get("sentiment_polarity"),
                            "composite_score": prev.get("composite_score"),
                            "tier": tier,
                            "primary_pair_address": prev.get("primary_pair_address"),
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("sentiment history write failed: %s", exc)
        return row

    def run_batch(self, **kwargs: Any) -> dict[str, int]:
        """
        Refresh sentiment fields for every token that has x_watch.
        kwargs accepted for API compatibility (force_full unused — data gated by X).
        """
        _ = kwargs
        stats = {"seen": 0, "ok": 0, "skip": 0, "err": 0}
        if not self.enabled:
            log.info("sentiment disabled (SENTIMENT_ENABLED=0)")
            return stats
        try:
            watches = self.store.list_tokens_with_x_watch()
        except Exception as exc:  # noqa: BLE001
            log.warning("sentiment list x_watch failed: %s", exc)
            return stats

        for w in watches:
            stats["seen"] += 1
            chain_id = w["chain_id"]
            token_address = w["token_address"]
            try:
                row = self.upsert_one(chain_id, token_address)
                if row:
                    stats["ok"] += 1
                else:
                    stats["skip"] += 1
            except Exception as exc:  # noqa: BLE001
                stats["err"] += 1
                log.warning(
                    "sentiment failed %s/%s: %s",
                    chain_id,
                    token_address[:12],
                    exc,
                )
        return stats
