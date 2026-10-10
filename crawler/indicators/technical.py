"""Technical indicators from pair_history: interval returns, volatility, volume strength.

Also fills ADD-ON pandas-ta fields (rsi_14 / macd* / boll_*) via technical_lib.
Existing change_*/volatility_24h/volume_strength logic is unchanged.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from history import history_tier_for_token
from logging_setup import get_logger

from indicators.base import INTERVAL_SEC, IndicatorStore, parse_iso, to_epoch, utc_now_iso
from indicators import technical_lib

log = get_logger("indicators.technical")

# Default volatility window (overridden by config when available)
DEFAULT_VOL_WINDOW_SEC = 24 * 60 * 60

# Tier → which fields to compute
TIER_FIELDS: dict[str, frozenset[str]] = {
    "hot": frozenset(
        {
            "change_5m",
            "change_15m",
            "change_1h",
            "change_4h",
            "change_24h",
            "volatility_24h",
            "volume_strength",
        }
    ),
    "warm": frozenset(
        {
            "change_15m",
            "change_1h",
            "change_4h",
            "change_24h",
            "volatility_24h",
            "volume_strength",
        }
    ),
    "cold": frozenset(
        {
            "change_1h",
            "change_24h",
            "volume_strength",
        }
    ),
}

# ADD-ON library fields (weight 0 in composite; not part of TIER_FIELDS gating).
# Filled whenever history is long enough; short history → None.
PANDAS_TA_FIELDS: tuple[str, ...] = (
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "boll_upper",
    "boll_mid",
    "boll_lower",
)


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def _price_near(
    series: list[dict[str, Any]], target_epoch: float, *, max_skew_sec: float
) -> float | None:
    """
    Pick price at the snapshot closest to target_epoch, requiring
    |recorded_at - target| <= max_skew_sec (else None).
    Prefer the last point at or before target when multiple are close.
    """
    best: tuple[float, float] | None = None  # (abs_skew, price)
    best_before: tuple[float, float] | None = None  # (epoch, price) last <= target
    for row in series:
        p = _num(row.get("price_usd"))
        ep = to_epoch(row.get("recorded_at"))
        if p is None or ep is None or p <= 0:
            continue
        skew = abs(ep - target_epoch)
        if ep <= target_epoch:
            if best_before is None or ep >= best_before[0]:
                best_before = (ep, p)
        if skew <= max_skew_sec:
            if best is None or skew < best[0]:
                best = (skew, p)
    if best_before is not None and (target_epoch - best_before[0]) <= max_skew_sec:
        return best_before[1]
    if best is not None:
        return best[1]
    return None


def change_pct(
    series: list[dict[str, Any]],
    lookback_sec: int,
    *,
    now_epoch: float | None = None,
) -> float | None:
    """
    (price_now - price_then) / price_then * 100.
    price_then: snapshot nearest to now-lookback within skew = max(lookback*0.5, 120s).
    """
    if not series:
        return None
    priced = [
        r
        for r in series
        if _num(r.get("price_usd")) is not None and _num(r.get("price_usd")) > 0
    ]
    if len(priced) < 1:
        return None
    last = priced[-1]
    p_now = _num(last.get("price_usd"))
    ep_now = to_epoch(last.get("recorded_at"))
    if p_now is None or ep_now is None:
        return None
    ref_now = now_epoch if now_epoch is not None else ep_now
    target = ref_now - lookback_sec
    # Allow generous skew for sparse cold history: half the lookback, min 2 min, max 6h
    skew = min(max(lookback_sec * 0.5, 120.0), 6 * 3600.0)
    p_then = _price_near(priced, target, max_skew_sec=skew)
    if p_then is None or p_then <= 0:
        return None
    return ((p_now - p_then) / p_then) * 100.0


def volatility_stdev_returns(
    series: list[dict[str, Any]],
    *,
    window_sec: int = DEFAULT_VOL_WINDOW_SEC,
) -> float | None:
    """
    Sample standard deviation of simple returns over snapshots in the window.

    Window: last `window_sec` seconds relative to the newest snapshot
    (default 24h → field name volatility_24h).

    For each consecutive pair of prices p_{i-1}, p_i within the window:
        r_i = (p_i - p_{i-1}) / p_{i-1}
    Returns stdev(r) * 100 (percent points). Needs >= 3 prices (>= 2 returns).
    """
    if not series:
        return None
    last_ep = to_epoch(series[-1].get("recorded_at"))
    if last_ep is None:
        return None
    cutoff = last_ep - window_sec
    prices: list[float] = []
    for row in series:
        ep = to_epoch(row.get("recorded_at"))
        p = _num(row.get("price_usd"))
        if ep is None or p is None or p <= 0:
            continue
        if ep >= cutoff:
            prices.append(p)
    if len(prices) < 3:
        return None
    returns: list[float] = []
    for i in range(1, len(prices)):
        prev = prices[i - 1]
        if prev <= 0:
            continue
        returns.append((prices[i] - prev) / prev)
    if len(returns) < 2:
        return None
    try:
        return statistics.stdev(returns) * 100.0
    except statistics.StatisticsError:
        return None


def volume_strength(series: list[dict[str, Any]], *, window_sec: int = DEFAULT_VOL_WINDOW_SEC) -> float | None:
    """
    current_volume / mean(volume over last window_sec).

    Prefers volume_h1; falls back to volume_h24 / 24 when h1 missing.
    Average is the arithmetic mean of the same series over the window
    (relative to newest snapshot). Returns None if mean <= 0.
    """
    if not series:
        return None
    last_ep = to_epoch(series[-1].get("recorded_at"))
    if last_ep is None:
        return None
    cutoff = last_ep - window_sec

    def vol_of(row: dict[str, Any]) -> float | None:
        h1 = _num(row.get("volume_h1"))
        if h1 is not None:
            return h1
        h24 = _num(row.get("volume_h24"))
        if h24 is not None:
            return h24 / 24.0
        return None

    vols: list[float] = []
    for row in series:
        ep = to_epoch(row.get("recorded_at"))
        if ep is None or ep < cutoff:
            continue
        v = vol_of(row)
        if v is not None and v >= 0:
            vols.append(v)
    if not vols:
        return None
    current = vol_of(series[-1])
    if current is None:
        return None
    mean_v = sum(vols) / len(vols)
    if mean_v <= 0:
        return None
    return current / mean_v


class TechnicalIndicators:
    """Compute and upsert technical fields into token_indicators."""

    def __init__(
        self,
        store: IndicatorStore,
        *,
        vol_window_sec: int = DEFAULT_VOL_WINDOW_SEC,
        write_history_for_hot: bool = True,
    ):
        self.store = store
        self.vol_window_sec = int(vol_window_sec)
        self.write_history_for_hot = write_history_for_hot

    def resolve_tier(self, chain_id: str, token_address: str) -> str:
        # Reuse crawler tier logic (hot/warm/cold from last_seen_at)
        try:
            return history_tier_for_token(self.store, chain_id, token_address)
        except Exception as exc:  # noqa: BLE001
            log.warning("tier resolve failed %s/%s: %s", chain_id, token_address[:12], exc)
            return "warm"

    def compute_one(
        self,
        chain_id: str,
        token_address: str,
        *,
        tier: str | None = None,
        force_full: bool = False,
    ) -> dict[str, Any] | None:
        tier = tier or self.resolve_tier(chain_id, token_address)
        fields = TIER_FIELDS["hot"] if force_full else TIER_FIELDS.get(tier, TIER_FIELDS["warm"])

        primary = self.store.pick_primary_pair(chain_id, token_address)
        if not primary:
            return None

        # Load enough history for 24h lookback (+ buffer)
        since_dt = datetime.now(timezone.utc) - timedelta(seconds=self.vol_window_sec + 3600)
        since = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        series = self.store.load_pair_history(chain_id, primary, since=since, limit=5000)
        if len(series) < 2:
            # fall back to all history for this pair
            series = self.store.load_pair_history(chain_id, primary, limit=5000)
        if len(series) < 1:
            return None

        out: dict[str, Any] = {
            "chain_id": chain_id,
            "token_address": token_address,
            "tier": tier,
            "primary_pair_address": primary,
            "sample_count": len(series),
            "updated_at": utc_now_iso(),
        }

        # Preserve previously computed fields not in this tier's set
        # (warm/cold must not null-out hot-only columns like change_5m).
        prev = None
        if not force_full:
            prev = self.store.get_token_indicators(chain_id, token_address, use_cache=False)

        all_tech = (
            "change_5m",
            "change_15m",
            "change_1h",
            "change_4h",
            "change_24h",
            "volatility_24h",
            "volume_strength",
        )
        for key in all_tech:
            if key in fields:
                if key == "volatility_24h":
                    out[key] = volatility_stdev_returns(
                        series, window_sec=self.vol_window_sec
                    )
                elif key == "volume_strength":
                    out[key] = volume_strength(
                        series, window_sec=self.vol_window_sec
                    )
                else:
                    out[key] = change_pct(series, INTERVAL_SEC[key])
            elif prev is not None and prev.get(key) is not None:
                out[key] = prev.get(key)
            else:
                out[key] = None

        # --- ADD-ON: pandas-ta RSI / MACD / BOLL (does not touch fields above) ---
        closes: list[float] = []
        for row in series:
            p = _num(row.get("price_usd"))
            if p is not None and p > 0:
                closes.append(p)
        lib = technical_lib.compute_pandas_ta(closes) if closes else {}
        for key in PANDAS_TA_FIELDS:
            val = lib.get(key) if lib else None
            if val is not None:
                out[key] = val
            elif prev is not None and prev.get(key) is not None:
                # Preserve prior library value if this pass lacked enough bars
                out[key] = prev.get(key)
            else:
                out[key] = None

        return out

    def upsert_one(
        self,
        chain_id: str,
        token_address: str,
        *,
        tier: str | None = None,
        force_full: bool = False,
    ) -> dict[str, Any] | None:
        row = self.compute_one(
            chain_id, token_address, tier=tier, force_full=force_full
        )
        if not row:
            return None
        self.store.upsert_token_indicators(row)
        if self.write_history_for_hot and row.get("tier") == "hot":
            try:
                self.store.insert_indicator_history({**row, "recorded_at": row["updated_at"]})
            except Exception as exc:  # noqa: BLE001
                log.warning("indicator_history write failed: %s", exc)
        return row

    def run_batch(
        self,
        *,
        force_full: bool = False,
        only_due: bool = False,
        hot_interval_sec: int = 300,
        warm_interval_sec: int = 1800,
        cold_interval_sec: int = 14400,
    ) -> dict[str, int]:
        """
        Process all tokens that have pair_history.
        only_due: skip tokens whose indicator updated_at is within tier interval.
        """
        stats = {"seen": 0, "ok": 0, "skip": 0, "err": 0, "hot": 0, "warm": 0, "cold": 0}
        tokens = self.store.list_tokens_with_history()
        now = datetime.now(timezone.utc)

        for tok in tokens:
            stats["seen"] += 1
            chain_id = tok["chain_id"]
            token_address = tok["token_address"]
            try:
                tier = self.resolve_tier(chain_id, token_address)
                stats[tier] = stats.get(tier, 0) + 1

                if only_due and not force_full:
                    last = self.store.get_indicator_updated_at(chain_id, token_address)
                    last_dt = parse_iso(last)
                    if last_dt is not None:
                        age = (now - last_dt).total_seconds()
                        if tier == "hot":
                            need = hot_interval_sec
                        elif tier == "cold":
                            need = cold_interval_sec
                        else:
                            need = warm_interval_sec
                        if age < need:
                            stats["skip"] += 1
                            continue

                row = self.upsert_one(
                    chain_id, token_address, tier=tier, force_full=force_full
                )
                if row:
                    stats["ok"] += 1
                else:
                    stats["skip"] += 1
            except Exception as exc:  # noqa: BLE001 — fault-isolated per token
                stats["err"] += 1
                log.warning(
                    "technical failed %s/%s: %s",
                    chain_id,
                    token_address[:12],
                    exc,
                )
        return stats
