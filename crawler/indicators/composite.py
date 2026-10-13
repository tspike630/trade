"""Composite score skeleton — weighted blend of technical / funding / sentiment.

Default weights are **0** (config INDICATORS_WEIGHT_*). With all weights 0 this
module is a no-op: it never writes composite_score and never feeds scoring/alerts.
Enable later by setting positive weights; still does not touch scoring.py.
"""

from __future__ import annotations

from typing import Any

from config import (
    INDICATORS_WEIGHT_FUNDING,
    INDICATORS_WEIGHT_SENTIMENT,
    INDICATORS_WEIGHT_TECHNICAL,
    INDICATORS_WRITE_HISTORY_HOT,
)
from history import history_tier_for_token
from logging_setup import get_logger

from indicators.base import IndicatorStore, utc_now_iso

log = get_logger("indicators.composite")


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def normalize_technical(row: dict[str, Any]) -> float | None:
    """
    Map technical fields → approx [-1, 1].

    Prefer change_1h (clipped ±50% → ±1); fall back to change_5m / change_24h.
    Blend a touch of volume_strength when present (centered at 1.0).
    """
    ch = _num(row.get("change_1h"))
    if ch is None:
        ch = _num(row.get("change_5m"))
    if ch is None:
        ch = _num(row.get("change_24h"))
    if ch is None:
        return None
    tech = _clip(ch / 50.0, -1.0, 1.0)
    vs = _num(row.get("volume_strength"))
    if vs is not None and vs > 0:
        # volume_strength 1.0 = neutral; boost/cut mildly
        vol_bias = _clip((vs - 1.0) * 0.25, -0.25, 0.25)
        tech = _clip(tech + vol_bias, -1.0, 1.0)
    return tech


def normalize_funding(row: dict[str, Any]) -> float | None:
    """Prefer jev_strength (already ~[-1,1]); else funding_bias * raw/100."""
    strength = _num(row.get("jev_strength"))
    if strength is not None:
        return _clip(strength, -1.0, 1.0)
    bias = _num(row.get("jev_funding_bias"))
    raw = _num(row.get("jev_raw_score"))
    if raw is None:
        raw = _num(row.get("jev_score"))
    if bias is None and raw is None:
        return None
    b = bias if bias is not None else 0.0
    r = (raw / 100.0) if raw is not None else 0.0
    return _clip(b * r, -1.0, 1.0)


def normalize_sentiment(row: dict[str, Any]) -> float | None:
    """Prefer sentiment_score (~[-1,1]); else polarity * heat/100."""
    score = _num(row.get("sentiment_score"))
    if score is not None:
        return _clip(score, -1.0, 1.0)
    pol = _num(row.get("sentiment_polarity"))
    heat = _num(row.get("sentiment_heat_score"))
    if pol is None and heat is None:
        return None
    p = pol if pol is not None else 0.0
    h = (heat / 100.0) if heat is not None else 1.0
    return _clip(p * h, -1.0, 1.0)


def blend_composite(
    row: dict[str, Any],
    *,
    w_tech: float,
    w_fund: float,
    w_sent: float,
) -> float | None:
    """
    Weighted average of available normalized components.
    Missing components redistribute remaining weight among present ones.
    Returns None if no component is available or total weight is 0.
    """
    parts: list[tuple[float, float]] = []  # (weight, value)
    t = normalize_technical(row)
    if t is not None and w_tech > 0:
        parts.append((w_tech, t))
    f = normalize_funding(row)
    if f is not None and w_fund > 0:
        parts.append((w_fund, f))
    s = normalize_sentiment(row)
    if s is not None and w_sent > 0:
        parts.append((w_sent, s))
    if not parts:
        return None
    wsum = sum(w for w, _ in parts)
    if wsum <= 0:
        return None
    return round(sum(w * v for w, v in parts) / wsum, 6)


class CompositeIndicators:
    """
    Skeleton composite writer.

    When all INDICATORS_WEIGHT_* are 0 (default), run_batch is a pure no-op
    (seen tokens counted as skip) — scoring/alerts stay untouched.
    """

    def __init__(
        self,
        store: IndicatorStore,
        *,
        weight_technical: float | None = None,
        weight_funding: float | None = None,
        weight_sentiment: float | None = None,
        write_history_for_hot: bool | None = None,
    ):
        self.store = store
        self.w_tech = float(
            INDICATORS_WEIGHT_TECHNICAL if weight_technical is None else weight_technical
        )
        self.w_fund = float(
            INDICATORS_WEIGHT_FUNDING if weight_funding is None else weight_funding
        )
        self.w_sent = float(
            INDICATORS_WEIGHT_SENTIMENT if weight_sentiment is None else weight_sentiment
        )
        self.write_history_for_hot = (
            INDICATORS_WRITE_HISTORY_HOT
            if write_history_for_hot is None
            else write_history_for_hot
        )

    @property
    def enabled(self) -> bool:
        return (self.w_tech + self.w_fund + self.w_sent) > 0

    def resolve_tier(self, chain_id: str, token_address: str) -> str:
        try:
            return history_tier_for_token(self.store, chain_id, token_address)
        except Exception as exc:  # noqa: BLE001
            log.debug("composite tier resolve: %s", exc)
            return "warm"

    def compute_one(self, chain_id: str, token_address: str) -> dict[str, Any] | None:
        if not self.enabled:
            log.debug("composite skip weights=0 %s/%s", chain_id, token_address[:12])
            return None
        row = self.store.get_token_indicators(chain_id, token_address, use_cache=False)
        if not row:
            return None
        score = blend_composite(
            row, w_tech=self.w_tech, w_fund=self.w_fund, w_sent=self.w_sent
        )
        if score is None:
            return None
        return {
            "chain_id": chain_id,
            "token_address": token_address,
            "composite_score": score,
            "composite_updated_at": utc_now_iso(),
            # Shared row clock only. tech_updated_at stays with the technical writer.
            "updated_at": utc_now_iso(),
        }

    def upsert_one(self, chain_id: str, token_address: str) -> dict[str, Any] | None:
        out = self.compute_one(chain_id, token_address)
        if not out:
            return None
        self.store.upsert_token_indicators(out)
        if self.write_history_for_hot:
            try:
                tier = self.resolve_tier(chain_id, token_address)
                if tier == "hot":
                    prev = (
                        self.store.get_token_indicators(
                            chain_id, token_address, use_cache=True
                        )
                        or {}
                    )
                    self.store.insert_indicator_history(
                        {
                            "chain_id": chain_id,
                            "token_address": token_address,
                            "recorded_at": out["updated_at"],
                            "change_5m": prev.get("change_5m"),
                            "change_15m": prev.get("change_15m"),
                            "change_1h": prev.get("change_1h"),
                            "change_4h": prev.get("change_4h"),
                            "change_24h": prev.get("change_24h"),
                            "volatility_24h": prev.get("volatility_24h"),
                            "volume_strength": prev.get("volume_strength"),
                            "jev_score": prev.get("jev_raw_score") or prev.get("jev_score"),
                            "jev_strength": prev.get("jev_strength"),
                            "sentiment_score": prev.get("sentiment_score"),
                            "sentiment_mentions_1h": prev.get("sentiment_mentions_1h"),
                            "sentiment_heat_score": prev.get("sentiment_heat_score"),
                            "sentiment_polarity": prev.get("sentiment_polarity"),
                            "composite_score": out.get("composite_score"),
                            "tier": tier,
                            "primary_pair_address": prev.get("primary_pair_address"),
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("composite history write failed: %s", exc)
        return out

    def run_batch(self, **kwargs: Any) -> dict[str, int]:
        """
        Refresh composite_score for every token_indicators row.
        kwargs accepted for API compatibility with run.py (unused).
        """
        _ = kwargs
        stats = {"seen": 0, "ok": 0, "skip": 0, "err": 0}
        if not self.enabled:
            log.info(
                "composite disabled (weights tech=%.3f fund=%.3f sent=%.3f)",
                self.w_tech,
                self.w_fund,
                self.w_sent,
            )
            return stats
        try:
            rows = self.store.conn.execute(
                "SELECT chain_id, token_address FROM token_indicators"
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("composite list failed: %s", exc)
            return stats
        for r in rows:
            stats["seen"] += 1
            chain_id = r["chain_id"]
            token_address = r["token_address"]
            try:
                out = self.upsert_one(chain_id, token_address)
                if out:
                    stats["ok"] += 1
                else:
                    stats["skip"] += 1
            except Exception as exc:  # noqa: BLE001
                stats["err"] += 1
                log.warning(
                    "composite failed %s/%s: %s",
                    chain_id,
                    token_address[:12],
                    exc,
                )
        return stats
