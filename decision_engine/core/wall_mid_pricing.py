"""IMC-style WallMid fair-price estimator & 1-tick wall-frontrun LIMIT_MAKER quoting.

Inspired by global top-2 algorithmic market-making microstructure practice:
- Raw mid-price ``0.5 * (best_bid + best_ask)`` is easily distorted by tiny 1-lot
  top-of-book spoofing or penny-jumping noise in crypto spot order books.
- ``WallMid`` locates the deepest liquidity "Bid Wall" and "Ask Wall" across the top N
  L2 order book levels (requiring wall USD notional >= ``wall_multiple * median_notional``
  so flat books cleanly fall back to top-of-book) and defines fair price as:
      ``wall_mid = 0.5 * (bid_wall_price + ask_wall_price)``
- For passive ``LIMIT_MAKER`` buy execution, ``maker_buy_price`` quotes 1 tick ahead of
  the whale Bid Wall (``bid_wall_price + tick_size``), strictly clamped to
  ``<= best_ask - tick_size`` (and ``<= best_bid`` when ``post_only_at_or_below_best_bid``
  is False, allowed up to ``best_ask - tick_size`` inside the spread) so the order never
  crosses the ask and enjoys downside protection from the whale wall immediately behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import statistics
from typing import Any, Sequence


@dataclass(frozen=True)
class WallMidQuote:
    wall_mid_price: float
    raw_mid_price: float
    bid_wall_price: float
    ask_wall_price: float
    bid_wall_notional_usd: float
    ask_wall_notional_usd: float
    maker_buy_price: float
    maker_sell_price: float
    used_true_bid_wall: bool
    used_true_ask_wall: bool


def _finite_pos(val: object) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0.0:
        return None
    return f


def _parse_book_levels(levels: object, max_levels: int = 20) -> list[tuple[float, float]]:
    if not isinstance(levels, Sequence) or isinstance(levels, (str, bytes)):
        return []
    out: list[tuple[float, float]] = []
    for item in levels[:max_levels]:
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) >= 2:
            px = _finite_pos(item[0])
            qty = _finite_pos(item[1])
            if px is not None and qty is not None:
                out.append((px, qty))
        elif isinstance(item, dict):
            px = _finite_pos(item.get("price") or item.get("px"))
            qty = _finite_pos(item.get("qty") or item.get("size") or item.get("amount"))
            if px is not None and qty is not None:
                out.append((px, qty))
    return out


def infer_default_tick_size(price: float) -> float:
    """Fallback tick size heuristic when exchange filter tickSize is not supplied."""
    if price >= 10000:
        return 0.01
    if price >= 100:
        return 0.01
    if price >= 1:
        return 0.0001
    if price >= 0.01:
        return 0.00001
    return max(1e-8, price * 1e-4)


def compute_wall_mid_from_book(
    bids: Sequence[Any],
    asks: Sequence[Any],
    *,
    tick_size: float | None = None,
    wall_multiple: float = 2.0,
    max_levels: int = 20,
) -> WallMidQuote | None:
    """Computes WallMid and 1-tick wall-frontrun LIMIT_MAKER prices from L2 bids & asks."""
    parsed_bids = sorted(_parse_book_levels(bids, max_levels=max_levels), key=lambda x: x[0], reverse=True)
    parsed_asks = sorted(_parse_book_levels(asks, max_levels=max_levels), key=lambda x: x[0])
    if not parsed_bids or not parsed_asks:
        return None

    best_bid = parsed_bids[0][0]
    best_ask = parsed_asks[0][0]
    if best_ask <= best_bid:
        return None

    raw_mid = 0.5 * (best_bid + best_ask)
    tick = _finite_pos(tick_size) or infer_default_tick_size(raw_mid)

    bid_notionals = [px * qty for px, qty in parsed_bids]
    ask_notionals = [px * qty for px, qty in parsed_asks]

    med_bid = statistics.median(bid_notionals) if bid_notionals else 0.0
    med_ask = statistics.median(ask_notionals) if ask_notionals else 0.0

    max_bid_idx = max(range(len(parsed_bids)), key=lambda i: bid_notionals[i])
    max_ask_idx = max(range(len(parsed_asks)), key=lambda i: ask_notionals[i])

    used_bid_wall = len(parsed_bids) >= 2 and bid_notionals[max_bid_idx] >= wall_multiple * max(med_bid, 1e-9)
    used_ask_wall = len(parsed_asks) >= 2 and ask_notionals[max_ask_idx] >= wall_multiple * max(med_ask, 1e-9)

    bid_wall_px = parsed_bids[max_bid_idx][0] if used_bid_wall else best_bid
    bid_wall_notional = bid_notionals[max_bid_idx] if used_bid_wall else bid_notionals[0]

    ask_wall_px = parsed_asks[max_ask_idx][0] if used_ask_wall else best_ask
    ask_wall_notional = ask_notionals[max_ask_idx] if used_ask_wall else ask_notionals[0]

    if ask_wall_px <= bid_wall_px:
        bid_wall_px, ask_wall_px = best_bid, best_ask
        used_bid_wall = False
        used_ask_wall = False

    wall_mid = 0.5 * (bid_wall_px + ask_wall_px)

    # Place passive buy 1 tick ahead of the whale Bid Wall, strictly below best_ask
    max_passive_buy = best_ask - tick if (best_ask - tick) > 0 else best_bid
    candidate_buy = bid_wall_px + tick if used_bid_wall else best_bid
    maker_buy = min(max_passive_buy, candidate_buy)
    if maker_buy <= 0:
        maker_buy = best_bid

    min_passive_sell = best_bid + tick
    candidate_sell = ask_wall_px - tick if used_ask_wall else best_ask
    maker_sell = max(min_passive_sell, candidate_sell)

    return WallMidQuote(
        wall_mid_price=round(wall_mid, 8),
        raw_mid_price=round(raw_mid, 8),
        bid_wall_price=round(bid_wall_px, 8),
        ask_wall_price=round(ask_wall_px, 8),
        bid_wall_notional_usd=round(bid_wall_notional, 2),
        ask_wall_notional_usd=round(ask_wall_notional, 2),
        maker_buy_price=round(maker_buy, 8),
        maker_sell_price=round(maker_sell, 8),
        used_true_bid_wall=used_bid_wall,
        used_true_ask_wall=used_ask_wall,
    )


def extract_wall_mid_from_row(row: dict[str, Any]) -> WallMidQuote | None:
    """Extracts WallMidQuote from either raw L2 book arrays or pre-summarized wall fields."""
    if not isinstance(row, dict):
        return None
    bids = row.get("l2_bids") or row.get("bids")
    asks = row.get("l2_asks") or row.get("asks")
    tick = _finite_pos(row.get("tick_size"))
    if bids and asks:
        return compute_wall_mid_from_book(bids, asks, tick_size=tick)

    bid_wall = _finite_pos(row.get("bid_wall_price"))
    ask_wall = _finite_pos(row.get("ask_wall_price"))
    best_bid = _finite_pos(row.get("best_bid") or row.get("bid"))
    best_ask = _finite_pos(row.get("best_ask") or row.get("ask"))
    if bid_wall is not None and ask_wall is not None and ask_wall > bid_wall:
        b_bid = best_bid if (best_bid is not None and best_bid >= bid_wall) else bid_wall
        b_ask = best_ask if (best_ask is not None and best_ask <= ask_wall and best_ask > b_bid) else ask_wall
        t = tick or infer_default_tick_size(0.5 * (bid_wall + ask_wall))
        max_passive_buy = b_ask - t if (b_ask - t) > 0 else b_bid
        maker_buy = min(max_passive_buy, bid_wall + t)
        maker_sell = max(b_bid + t, ask_wall - t)
        return WallMidQuote(
            wall_mid_price=round(0.5 * (bid_wall + ask_wall), 8),
            raw_mid_price=round(0.5 * (b_bid + b_ask), 8),
            bid_wall_price=round(bid_wall, 8),
            ask_wall_price=round(ask_wall, 8),
            bid_wall_notional_usd=float(_finite_pos(row.get("bid_wall_notional_usd")) or 0.0),
            ask_wall_notional_usd=float(_finite_pos(row.get("ask_wall_notional_usd")) or 0.0),
            maker_buy_price=round(maker_buy, 8),
            maker_sell_price=round(maker_sell, 8),
            used_true_bid_wall=True,
            used_true_ask_wall=True,
        )
    return None
