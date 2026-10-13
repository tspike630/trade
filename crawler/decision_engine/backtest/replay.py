"""Replay: build time-ordered snapshot sequences from indicator_history + pair_history.

Read-only on the main business DB (URI mode=ro).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from logging_setup import get_logger

from decision_engine.backtest.filters import safe_float

log = get_logger("decision_engine.backtest.replay")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    s = str(s).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def to_iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not str(value).strip())


def coalesce_indicator_rows(
    prev: dict[str, Any] | None, new: dict[str, Any]
) -> dict[str, Any]:
    """Merge same-bar indicator_history rows.

    Funding / sentiment passes write a later row whose RSI/MACD/BOLL columns
    are NULL. A later NULL or blank must not wipe an earlier technical value.
    When both rows have a value, the later one wins (rows are applied in
    recorded_at order).
    """
    if not prev:
        return dict(new)
    merged = dict(prev)
    for key, val in new.items():
        if _is_empty(val):
            continue
        merged[key] = val
    return merged


def merge_bar_rows(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Same merge as coalesce_indicator_rows; name kept for sweep tests."""
    return coalesce_indicator_rows(base, incoming)


def floor_bar(dt: datetime, bar_minutes: int) -> datetime:
    bar_minutes = max(1, int(bar_minutes))
    epoch = int(dt.timestamp())
    step = bar_minutes * 60
    floored = epoch - (epoch % step)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


@dataclass
class DataWindow:
    start: datetime
    end: datetime
    indicator_rows: int
    pair_rows: int
    unique_tokens: int
    unique_bars: int
    sufficient: bool
    message: str = ""


@dataclass
class Snapshot:
    """One replay tick: bar time + hot token indicator rows (+ price/liq attach)."""

    bar_ts: str
    hot_tokens: list[dict[str, Any]] = field(default_factory=list)
    prices: dict[str, float] = field(default_factory=dict)  # chain|pair -> price
    liquidities: dict[str, float] = field(default_factory=dict)
    coverages: dict[str, float] = field(default_factory=dict)  # chain|token -> coverage


class HistoryReplay:
    """Load history once, then iterate bar snapshots."""

    _IND_COLS = (
        "chain_id",
        "token_address",
        "recorded_at",
        "change_5m",
        "change_15m",
        "change_1h",
        "change_4h",
        "change_24h",
        "volatility_24h",
        "volume_strength",
        "jev_score",
        "jev_strength",
        "sentiment_score",
        "composite_score",
        "tier",
        "primary_pair_address",
        # 3-3: technical columns. Missing columns are dropped by _ind_cols.
        # indicator_history stores bands as boll_upper / boll_lower (not boll_up / boll_low).
        "rsi_14",
        "macd",
        "macd_signal",
        "boll_mid",
        "boll_upper",
        "boll_lower",
    )

    def _ind_cols(self) -> tuple[str, ...]:
        if not self._table_exists("indicator_history"):
            return ()
        try:
            cols = {
                str(r["name"])
                for r in self.conn.execute("PRAGMA table_info(indicator_history)").fetchall()
            }
        except sqlite3.OperationalError:
            return ()
        chosen = tuple(c for c in self._IND_COLS if c in cols)
        if not getattr(self, "_logged_boll", False):
            self._logged_boll = True
            wanted = ("boll_upper", "boll_mid", "boll_lower")
            selected = [c for c in wanted if c in cols]
            missing = [c for c in wanted if c not in cols]
            log.info(
                "indicator_history boll columns selected=%s",
                ",".join(selected) if selected else "(none)",
            )
            if cols and missing:
                log.warning(
                    "indicator_history missing boll columns (PRAGMA filtered): %s",
                    ",".join(missing),
                )
        return chosen

    def __init__(
        self,
        db_path: str | Path,
        *,
        days: float = 7.0,
        bar_minutes: int = 5,
        hot_limit: int = 50,
        min_bars: int = 3,
        min_indicator_rows: int = 10,
        price_tol_sec: int = 1800,
    ):
        self.db_path = Path(db_path).resolve()
        self.days = float(days)
        self.bar_minutes = max(1, int(bar_minutes))
        self.hot_limit = max(1, int(hot_limit))
        self.min_bars = max(1, int(min_bars))
        self.min_indicator_rows = max(1, int(min_indicator_rows))
        self.price_tol_sec = max(60, int(price_tol_sec))
        self._conn: sqlite3.Connection | None = None
        self._window: DataWindow | None = None
        self._open()

    def _open(self) -> None:
        if not self.db_path.is_file():
            log.warning("db missing path=%s", self.db_path)
            self._conn = sqlite3.connect(":memory:")
            self._conn.row_factory = sqlite3.Row
            return
        uri = f"file:{self.db_path}?mode=ro"
        try:
            self._conn = sqlite3.connect(uri, uri=True, timeout=10.0)
        except sqlite3.OperationalError as exc:
            log.warning("ro URI failed (%s); fallback SELECT-only", exc)
            self._conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        log.info("replay open ro db=%s", self.db_path)

    @property
    def conn(self) -> sqlite3.Connection:
        assert self._conn is not None
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "HistoryReplay":
        return self

    def __exit__(self, *a: object) -> None:
        self.close()

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        return row is not None

    def inspect_window(self) -> DataWindow:
        """Compute available history window vs requested --days."""
        if not self._table_exists("indicator_history"):
            w = DataWindow(
                start=datetime.now(timezone.utc),
                end=datetime.now(timezone.utc),
                indicator_rows=0,
                pair_rows=0,
                unique_tokens=0,
                unique_bars=0,
                sufficient=False,
                message="indicator_history table missing — cannot replay",
            )
            self._window = w
            return w

        row = self.conn.execute(
            "SELECT MIN(recorded_at) AS mn, MAX(recorded_at) AS mx, COUNT(*) AS n "
            "FROM indicator_history"
        ).fetchone()
        mn, mx, n = row["mn"], row["mx"], int(row["n"] or 0)
        if not mn or not mx or n == 0:
            w = DataWindow(
                start=datetime.now(timezone.utc),
                end=datetime.now(timezone.utc),
                indicator_rows=0,
                pair_rows=0,
                unique_tokens=0,
                unique_bars=0,
                sufficient=False,
                message="indicator_history empty — no snapshots to replay",
            )
            self._window = w
            return w

        data_start = parse_ts(mn) or datetime.now(timezone.utc)
        data_end = parse_ts(mx) or datetime.now(timezone.utc)
        want_start = data_end - timedelta(days=self.days)
        start = max(data_start, want_start)
        end = data_end

        start_iso, end_iso = to_iso_z(start), to_iso_z(end)
        n_ind = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM indicator_history "
                "WHERE recorded_at>=? AND recorded_at<=?",
                (start_iso, end_iso),
            ).fetchone()[0]
        )
        n_tok = int(
            self.conn.execute(
                "SELECT COUNT(DISTINCT chain_id || '|' || token_address) "
                "FROM indicator_history WHERE recorded_at>=? AND recorded_at<=?",
                (start_iso, end_iso),
            ).fetchone()[0]
        )
        n_pair = 0
        if self._table_exists("pair_history"):
            n_pair = int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM pair_history "
                    "WHERE recorded_at>=? AND recorded_at<=?",
                    (start_iso, end_iso),
                ).fetchone()[0]
            )

        # Approximate unique bars
        span_sec = max(0.0, (end - start).total_seconds())
        approx_bars = int(span_sec // (self.bar_minutes * 60)) + 1

        span_hours = span_sec / 3600.0
        msgs = []
        if n_ind < self.min_indicator_rows:
            msgs.append(
                f"indicator rows in window={n_ind} < min={self.min_indicator_rows}"
            )
        if approx_bars < self.min_bars:
            msgs.append(f"approx bars={approx_bars} < min={self.min_bars}")
        if span_hours < 1.0:
            msgs.append(f"history span only {span_hours:.2f}h")
        if self.days > 0 and span_hours < self.days * 24 * 0.5:
            msgs.append(
                f"requested --days={self.days:g} but available≈{span_hours/24:.2f}d "
                f"({to_iso_z(data_start)} → {to_iso_z(data_end)}); using available window"
            )

        sufficient = n_ind >= self.min_indicator_rows and approx_bars >= self.min_bars
        message = "; ".join(msgs) if msgs else "ok"
        if not sufficient and not msgs:
            message = "insufficient history for replay"
        elif not sufficient:
            message = "INSUFFICIENT: " + message

        w = DataWindow(
            start=start,
            end=end,
            indicator_rows=n_ind,
            pair_rows=n_pair,
            unique_tokens=n_tok,
            unique_bars=approx_bars,
            sufficient=sufficient,
            message=message,
        )
        self._window = w
        return w

    def _load_coverage_map(self) -> dict[str, float]:
        """chain|token -> coverage_pct (0–100). Prefer jev_signals; else confidence*100."""
        out: dict[str, float] = {}
        if self._table_exists("jev_signals"):
            for r in self.conn.execute(
                "SELECT chain_id, token_address, coverage_pct FROM jev_signals "
                "WHERE coverage_pct IS NOT NULL"
            ):
                key = f"{r['chain_id']}|{r['token_address']}"
                out[key] = float(r["coverage_pct"])
        if self._table_exists("token_indicators"):
            for r in self.conn.execute(
                "SELECT chain_id, token_address, jev_confidence FROM token_indicators "
                "WHERE jev_confidence IS NOT NULL"
            ):
                key = f"{r['chain_id']}|{r['token_address']}"
                if key not in out:
                    out[key] = float(r["jev_confidence"]) * 100.0
        return out

    def iter_snapshots(self) -> Iterator[Snapshot]:
        """Yield bar snapshots newest-history ascending in time."""
        w = self._window or self.inspect_window()
        if not w.sufficient and w.indicator_rows == 0:
            return

        start_iso, end_iso = to_iso_z(w.start), to_iso_z(w.end)
        ind_cols = self._ind_cols()
        if not ind_cols:
            log.warning("indicator_history has no usable columns")
            return
        cols = ", ".join(ind_cols)
        rows = self.conn.execute(
            f"""
            SELECT {cols}
            FROM indicator_history
            WHERE recorded_at >= ? AND recorded_at <= ?
            ORDER BY recorded_at ASC
            """,
            (start_iso, end_iso),
        ).fetchall()
        if not rows:
            return

        coverage_map = self._load_coverage_map()

        # Group by floor bar. Same bar+token: merge, preferring non-null fields.
        bars: dict[str, dict[str, dict[str, Any]]] = {}
        bar_order: list[str] = []
        for r in rows:
            dt = parse_ts(r["recorded_at"])
            if not dt:
                continue
            bar_dt = floor_bar(dt, self.bar_minutes)
            bar_ts = to_iso_z(bar_dt)
            if bar_ts not in bars:
                bars[bar_ts] = {}
                bar_order.append(bar_ts)
            tok_key = f"{r['chain_id']}|{r['token_address']}"
            bars[bar_ts][tok_key] = coalesce_indicator_rows(
                bars[bar_ts].get(tok_key), dict(r)
            )

        # Preload pair_history price/liq into sorted lists per pair for nearest lookup
        pair_pts: dict[str, list[tuple[datetime, float, Optional[float]]]] = {}
        if self._table_exists("pair_history"):
            # Restrict to pairs referenced in indicator window
            pairs_needed: set[tuple[str, str]] = set()
            for bar_map in bars.values():
                for row in bar_map.values():
                    pa = row.get("primary_pair_address")
                    if pa:
                        pairs_needed.add((str(row["chain_id"]), str(pa)))
            for chain_id, pair_address in pairs_needed:
                pts: list[tuple[datetime, float, Optional[float]]] = []
                for pr in self.conn.execute(
                    """
                    SELECT recorded_at, price_usd, liquidity_usd
                    FROM pair_history
                    WHERE chain_id=? AND pair_address=?
                      AND recorded_at>=? AND recorded_at<=?
                      AND price_usd IS NOT NULL
                    ORDER BY recorded_at ASC
                    """,
                    (
                        chain_id,
                        pair_address,
                        to_iso_z(w.start - timedelta(seconds=self.price_tol_sec)),
                        to_iso_z(w.end + timedelta(seconds=self.price_tol_sec)),
                    ),
                ):
                    pdt = parse_ts(pr["recorded_at"])
                    px = safe_float(pr["price_usd"])
                    if pdt is None or px is None:
                        continue
                    pts.append((pdt, px, safe_float(pr["liquidity_usd"])))
                if pts:
                    pair_pts[f"{chain_id}|{pair_address}"] = pts

        def nearest_price(
            key: str, target: datetime
        ) -> tuple[Optional[float], Optional[float]]:
            # Known simplification: entry price is the nearest pair_history print
            # within ±price_tol_sec of the bar *start* (bidirectional). A print
            # slightly after the bar open can be selected. As-of / bar-end pricing
            # is a separate replay change and is intentionally left as-is.
            pts = pair_pts.get(key)
            if not pts:
                return None, None
            best_px, best_liq, best_abs = None, None, None
            for pdt, px, liq in pts:
                d = abs((pdt - target).total_seconds())
                if d > self.price_tol_sec:
                    continue
                if best_abs is None or d < best_abs:
                    best_abs = d
                    best_px = px
                    best_liq = liq
            return best_px, best_liq

        for bar_ts in bar_order:
            bar_dt = parse_ts(bar_ts)
            assert bar_dt is not None
            token_map = bars[bar_ts]
            # Rank hot: prefer tier=hot, then jev_strength, then volume_strength
            ranked = list(token_map.values())

            def sort_key(row: dict[str, Any]) -> tuple:
                tier_rank = 0 if (row.get("tier") or "") == "hot" else 1
                js = safe_float(row.get("jev_strength"))
                vs = safe_float(row.get("volume_strength"))
                return (tier_rank, -(js if js is not None else -1.0), -(vs if vs is not None else -1.0))

            ranked.sort(key=sort_key)
            hot = ranked[: self.hot_limit]

            prices: dict[str, float] = {}
            liquids: dict[str, float] = {}
            coverages: dict[str, float] = {}
            enriched: list[dict[str, Any]] = []
            for row in hot:
                out = dict(row)
                out["updated_at"] = row.get("recorded_at")
                tok_key = f"{row['chain_id']}|{row['token_address']}"
                cov = coverage_map.get(tok_key)
                if cov is not None:
                    coverages[tok_key] = cov
                    out["coverage"] = cov
                pa = row.get("primary_pair_address")
                if pa:
                    pkey = f"{row['chain_id']}|{pa}"
                    px, liq = nearest_price(pkey, bar_dt)
                    if px is not None:
                        prices[pkey] = px
                        out["price_usd"] = px
                    if liq is not None:
                        liquids[pkey] = liq
                        out["liquidity_usd"] = liq
                enriched.append(out)

            yield Snapshot(
                bar_ts=bar_ts,
                hot_tokens=enriched,
                prices=prices,
                liquidities=liquids,
                coverages=coverages,
            )

        log.info(
            "replay yielded bars=%s tokens_window=%s ind_rows=%s",
            len(bar_order),
            w.unique_tokens,
            w.indicator_rows,
        )
