"""Technical indicators from pair_history: interval returns, volatility, volume strength.

Also fills ADD-ON pandas-ta fields (rsi_14 / macd* / boll_*) via technical_lib.
Existing change_*/volatility_24h/volume_strength logic is unchanged.
"""

from __future__ import annotations

import math
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from config import HOT_SEEN_WINDOW_SEC
from history import history_tier_for_token
from logging_setup import get_logger
from store import norm_address, norm_chain

from indicators.base import (
    INTERVAL_SEC,
    IndicatorStore,
    parse_iso,
    to_epoch,
    utc_now_iso,
)
from indicators.library_apply import apply_library_fields, technical_refresh_due
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


def _token_key(tok: dict[str, Any]) -> tuple[str, str]:
    return (norm_chain(str(tok["chain_id"])), norm_address(str(tok["token_address"])))


def tier_from_token_row(
    row: dict[str, Any],
    *,
    now: datetime,
    hot_seen_window_sec: int,
) -> str:
    """Map token recency to hot|warm|cold.

    Same rules as history.history_tier_for_token, using last_seen_at / updated_at /
    has_pairs already loaded on the row.
    """
    seen = parse_iso(row.get("last_seen_at") or row.get("updated_at"))
    if seen is None:
        return "cold"
    age = (now - seen).total_seconds()
    if age <= hot_seen_window_sec and row.get("has_pairs") == 1:
        return "hot"
    if age <= hot_seen_window_sec * 6:
        return "warm"
    return "cold"


def technical_pass_rank(
    tier: str,
    tech_updated_at: str | None,
    *,
    now: datetime,
    hot_interval_sec: int,
    warm_interval_sec: int,
    cold_interval_sec: int,
    force_full: bool = False,
) -> int:
    """Lower rank runs first.

    0 — hot and due (tech_updated_at NULL, or older than the hot interval)
    1 — warm and due (NULL, or older than the warm interval)
    2 — cold / other tiers that are due
    3 — any tier still inside its interval

    Due cold is ahead of rows still inside their interval so a bounded round
    refreshes stale cold tokens first. The next round then starts on the
    remaining due cold set instead of re-walking a fresh prefix. Due hot and
    due warm stay ahead of cold. A hot row that becomes due while a round is
    in progress is still picked up by the per-slice due-hot drain.

    ``force_full`` still places every hot token, then every warm token, ahead of
    the rest. Due-ness is technical_refresh_due: NULL is due, and the shared
    updated_at column is not read.
    """
    if force_full:
        if tier == "hot":
            return 0
        if tier == "warm":
            return 1
        return 2
    needs_refresh = technical_refresh_due(
        tech_updated_at,
        tier=tier,
        now=now,
        hot_interval_sec=hot_interval_sec,
        warm_interval_sec=warm_interval_sec,
        cold_interval_sec=cold_interval_sec,
    )
    if not needs_refresh:
        return 3
    if tier == "hot":
        return 0
    if tier == "warm":
        return 1
    return 2


def order_technical_tokens(
    tokens: list[dict[str, Any]],
    *,
    tier_of: Callable[[dict[str, Any]], str],
    tech_updated_at_of: Callable[[dict[str, Any]], str | None],
    now: datetime,
    hot_interval_sec: int,
    warm_interval_sec: int,
    cold_interval_sec: int,
    force_full: bool = False,
) -> list[dict[str, Any]]:
    """Stable order: due hot, then due warm, then due cold, then not-yet-due."""
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for index, tok in enumerate(tokens):
        rank = technical_pass_rank(
            tier_of(tok),
            tech_updated_at_of(tok),
            now=now,
            hot_interval_sec=hot_interval_sec,
            warm_interval_sec=warm_interval_sec,
            cold_interval_sec=cold_interval_sec,
            force_full=force_full,
        )
        ranked.append((rank, index, tok))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [tok for _rank, _index, tok in ranked]


def _with_recency(
    tok: dict[str, Any], recency: dict[tuple[str, str], dict[str, Any]]
) -> dict[str, Any]:
    extra = recency.get(_token_key(tok))
    if not extra:
        return tok
    return {**tok, **extra}


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
        # Kept-old values must not refresh tech_updated_at (P1-08). MACD is one set (P2-13).
        closes: list[float] = []
        for row in series:
            p = _num(row.get("price_usd"))
            if p is not None and p > 0:
                closes.append(p)
        lib = technical_lib.compute_pandas_ta(closes) if closes else {}
        lib_fields, lib_fresh = apply_library_fields(prev, lib)
        out.update(lib_fields)
        out["_lib_fresh"] = lib_fresh

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
        lib_fresh = bool(row.pop("_lib_fresh", True))
        if lib_fresh:
            now = row.get("updated_at") or utc_now_iso()
            row["updated_at"] = now
            row["tech_updated_at"] = now
        else:
            # Preserved RSI/MACD/BOLL. Do not pretend this pass refreshed them.
            row.pop("updated_at", None)
            row.pop("tech_updated_at", None)
        self.store.upsert_token_indicators(row, touch_updated_at=lib_fresh)
        if lib_fresh and self.write_history_for_hot and row.get("tier") == "hot":
            try:
                self.store.insert_indicator_history(
                    {**row, "recorded_at": row["updated_at"]}
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("indicator_history write failed: %s", exc)
        return row

    def _process_token(
        self,
        tok: dict[str, Any],
        *,
        tier: str,
        stats: dict[str, int],
        force_full: bool,
        only_due: bool,
        hot_interval_sec: int,
        warm_interval_sec: int,
        cold_interval_sec: int,
    ) -> None:
        stats["seen"] += 1
        chain_id = tok["chain_id"]
        token_address = tok["token_address"]
        try:
            stats[tier] = stats.get(tier, 0) + 1
            if only_due and not force_full:
                last = self.store.get_tech_updated_at(chain_id, token_address)
                if not technical_refresh_due(
                    last,
                    tier=tier,
                    now=datetime.now(timezone.utc),
                    hot_interval_sec=hot_interval_sec,
                    warm_interval_sec=warm_interval_sec,
                    cold_interval_sec=cold_interval_sec,
                ):
                    stats["skip"] += 1
                    return
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
                str(token_address)[:12],
                exc,
            )

    def _due_priority(
        self,
        universe: list[dict[str, Any]],
        *,
        hot_interval_sec: int,
        warm_interval_sec: int,
        cold_interval_sec: int,
    ) -> list[tuple[int, int, dict[str, Any], str]]:
        """Hot-due then warm-due, using a fresh recency and tech_updated_at read.

        NULL tech_updated_at is due. Rank 2 (cold / not yet due) is omitted.
        """
        now = datetime.now(timezone.utc)
        recency = self.store.map_token_recency()
        stamps = self.store.map_tech_updated_at()
        due: list[tuple[int, int, dict[str, Any], str]] = []
        for index, tok in enumerate(universe):
            tier = tier_from_token_row(
                _with_recency(tok, recency),
                now=now,
                hot_seen_window_sec=HOT_SEEN_WINDOW_SEC,
            )
            rank = technical_pass_rank(
                tier,
                stamps.get(_token_key(tok)),
                now=now,
                hot_interval_sec=hot_interval_sec,
                warm_interval_sec=warm_interval_sec,
                cold_interval_sec=cold_interval_sec,
            )
            if rank < 2:
                due.append((rank, index, tok, tier))
        due.sort(key=lambda item: (item[0], item[1]))
        return due

    def run_batch(
        self,
        *,
        force_full: bool = False,
        only_due: bool = False,
        hot_interval_sec: int = 300,
        warm_interval_sec: int = 1800,
        cold_interval_sec: int = 14400,
        round_budget_sec: float | None = None,
        max_tail_tokens: int | None = None,
        _monotonic: Callable[[], float] | None = None,
    ) -> dict[str, int]:
        """
        Process tokens that have pair_history.

        Order when a refresh is selective (``only_due``) or full:
        due hot, then due warm, then due cold, then tokens still inside
        their interval. Due means tech_updated_at IS NULL or older than
        that tier's interval. Shared updated_at is not consulted.
        NULL tech_updated_at is due.

        While warm tokens or the cold tail are still in progress, due hot is
        drained again every hot interval, measured from the start of the
        previous slice. A large due-hot set does not run as one unbounded
        pass, and neither does due warm: unserved due hot keeps a forward
        cursor, hot that is due again runs after that backlog, and the slice
        stops at the interval. Warm, then cold, takes the time left. If the
        hot slice already used the interval, one warm or cold token still
        runs so the lower tiers keep moving. A hot row updated while its
        due-hot set finishes inside one interval is therefore refreshed
        again before two hot intervals have elapsed.

        ``round_budget_sec`` and ``max_tail_tokens`` apply only to that
        selective loop (``only_due`` and not ``force_full``). ``None`` keeps
        the full drain. A hit returns immediately with ``round_budget_hit``
        1 and ``tail_left`` still counting unvisited cold-tail rows. Hot,
        then warm, still runs before the tail inside a slice.
        """
        stats = {
            "seen": 0,
            "ok": 0,
            "skip": 0,
            "err": 0,
            "hot": 0,
            "warm": 0,
            "cold": 0,
            "hot_due": 0,
            "warm_due": 0,
            "tail_left": 0,
            "round_budget_hit": 0,
        }
        universe = self.store.list_tokens_with_history()
        if not universe:
            return stats

        monotonic = _monotonic or time.monotonic
        now = datetime.now(timezone.utc)
        recency = self.store.map_token_recency()
        stamps = self.store.map_tech_updated_at()

        def tier_of(tok: dict[str, Any]) -> str:
            return tier_from_token_row(
                _with_recency(tok, recency),
                now=now,
                hot_seen_window_sec=HOT_SEEN_WINDOW_SEC,
            )

        def tech_of(tok: dict[str, Any]) -> str | None:
            return stamps.get(_token_key(tok))

        ordered = order_technical_tokens(
            universe,
            tier_of=tier_of,
            tech_updated_at_of=tech_of,
            now=now,
            hot_interval_sec=hot_interval_sec,
            warm_interval_sec=warm_interval_sec,
            cold_interval_sec=cold_interval_sec,
            force_full=force_full,
        )
        tail: list[dict[str, Any]] = []
        for tok in ordered:
            rank = technical_pass_rank(
                tier_of(tok),
                tech_of(tok),
                now=now,
                hot_interval_sec=hot_interval_sec,
                warm_interval_sec=warm_interval_sec,
                cold_interval_sec=cold_interval_sec,
                force_full=force_full,
            )
            if rank == 0:
                stats["hot_due"] += 1
            elif rank == 1:
                stats["warm_due"] += 1
            else:
                tail.append(tok)
        log.info(
            "technical priority hot_due=%s warm_due=%s tail=%s",
            stats["hot_due"],
            stats["warm_due"],
            len(tail),
        )

        repeat = bool(only_due and not force_full)
        if not repeat:
            for tok in ordered:
                self._process_token(
                    tok,
                    tier=tier_of(tok),
                    stats=stats,
                    force_full=force_full,
                    only_due=only_due,
                    hot_interval_sec=hot_interval_sec,
                    warm_interval_sec=warm_interval_sec,
                    cold_interval_sec=cold_interval_sec,
                )
            return stats

        interval = max(1, int(hot_interval_sec))
        tail_index = 0
        tail_taken = 0
        tail_cap = None if max_tail_tokens is None else max(0, int(max_tail_tokens))
        round_started = monotonic()
        slice_n = 0

        def _finish(hit: bool) -> dict[str, int]:
            stats["tail_left"] = max(0, len(tail) - tail_index)
            stats["round_budget_hit"] = 1 if hit else 0
            return stats

        # Warm is queued once per token per pass (appended if it becomes due
        # later). Hot uses a separate cursor: unserved due hot first, then
        # hot that has already been refreshed this pass and is due again.
        warm_queue: list[tuple[dict[str, Any], str]] = []
        warm_queued: set[tuple[str, str]] = set()
        warm_index = 0
        hot_served: set[tuple[str, str]] = set()
        first_drain = True
        deadline = 0.0
        advanced = 0

        def _run(tok: dict[str, Any], tier: str) -> None:
            self._process_token(
                tok,
                tier=tier,
                stats=stats,
                force_full=False,
                only_due=True,
                hot_interval_sec=hot_interval_sec,
                warm_interval_sec=warm_interval_sec,
                cold_interval_sec=cold_interval_sec,
            )

        def _take_hot(
            items: list[tuple[dict[str, Any], str, tuple[str, str]]],
        ) -> int:
            nonlocal advanced
            taken = 0
            for tok, tier, key in items:
                if advanced > 0 and monotonic() >= deadline:
                    break
                _run(tok, tier)
                hot_served.add(key)
                advanced += 1
                taken += 1
            return taken

        while True:
            if (
                round_budget_sec is not None
                and slice_n > 0
                and monotonic() - round_started >= float(round_budget_sec)
            ):
                log.info(
                    "technical round budget sec=%s tail_left=%s warm_left=%s",
                    round_budget_sec,
                    max(0, len(tail) - tail_index),
                    max(0, len(warm_queue) - warm_index),
                )
                return _finish(True)
            slice_n += 1
            drain_started = monotonic()
            # Deadline is anchored to the slice start so the next hot refresh
            # is one hot interval later, inside the decision window (2×),
            # when the due-hot work itself finishes inside that interval.
            deadline = drain_started + interval
            advanced = 0
            due = self._due_priority(
                universe,
                hot_interval_sec=hot_interval_sec,
                warm_interval_sec=warm_interval_sec,
                cold_interval_sec=cold_interval_sec,
            )
            hot_backlog: list[tuple[dict[str, Any], str, tuple[str, str]]] = []
            hot_repeat: list[tuple[dict[str, Any], str, tuple[str, str]]] = []
            for _rank, _index, tok, tier in due:
                key = _token_key(tok)
                if _rank == 0:
                    if key in hot_served:
                        hot_repeat.append((tok, tier, key))
                    else:
                        hot_backlog.append((tok, tier, key))
                elif _rank == 1 and key not in warm_queued:
                    warm_queued.add(key)
                    warm_queue.append((tok, tier))
            hot_n = len(hot_backlog) + len(hot_repeat)
            if not first_drain and hot_n:
                log.info(
                    "technical priority refresh hot_due=%s warm_left=%s tail_left=%s",
                    hot_n,
                    max(0, len(warm_queue) - warm_index),
                    max(0, len(tail) - tail_index),
                )
            first_drain = False
            taken_backlog = _take_hot(hot_backlog)
            _take_hot(hot_repeat)
            backlog_left = taken_backlog < len(hot_backlog)
            lower_left = warm_index < len(warm_queue) or tail_index < len(tail)
            if not lower_left:
                # One pass over warm and cold is enough. Keep walking a hot
                # backlog that the interval cut short; do not spin on hot
                # that is already served and simply due again.
                if backlog_left and advanced > 0:
                    continue
                break

            # Rest of this interval: due warm, then the cold tail. When the
            # hot slice already consumed the interval, still take one lower
            # token so warm/cold cannot stall for a whole hot sweep.
            took_lower = False
            while warm_index < len(warm_queue):
                if took_lower and monotonic() >= deadline:
                    break
                tok, tier = warm_queue[warm_index]
                warm_index += 1
                took_lower = True
                advanced += 1
                _run(tok, tier)
            if tail_index < len(tail) and not (
                took_lower and monotonic() >= deadline
            ):
                # One recency read for the slice. Tier can move while the
                # tail runs; the next slice picks up hot/warm that became due.
                slice_now = datetime.now(timezone.utc)
                slice_recency = self.store.map_token_recency()
                while tail_index < len(tail):
                    if tail_cap is not None and tail_taken >= tail_cap:
                        log.info(
                            "technical tail budget tokens=%s tail_left=%s",
                            tail_cap,
                            max(0, len(tail) - tail_index),
                        )
                        return _finish(True)
                    if took_lower and monotonic() >= deadline:
                        break
                    tok = tail[tail_index]
                    tail_index += 1
                    tail_taken += 1
                    took_lower = True
                    advanced += 1
                    tier = tier_from_token_row(
                        _with_recency(tok, slice_recency),
                        now=slice_now,
                        hot_seen_window_sec=HOT_SEEN_WINDOW_SEC,
                    )
                    _run(tok, tier)
            if advanced == 0:
                break
        return _finish(False)
