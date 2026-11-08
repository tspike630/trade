"""Backtest engine: replay ticks → strategies.on_tick → filters → ledger."""

from __future__ import annotations

from typing import Any, Optional

from logging_setup import get_logger

from decision_engine.backtest.filters import FilterConfig, apply_entry_filters
from decision_engine.backtest.ledger import BacktestLedger
from decision_engine.backtest.metrics import compute_metrics
from decision_engine.backtest.replay import HistoryReplay, parse_ts
from decision_engine.strategies.base_strategy import BaseStrategy
from execution.exits import scan_exits

log = get_logger("decision_engine.backtest.engine")


class BacktestEngine:
    def __init__(
        self,
        replay: HistoryReplay,
        ledger: BacktestLedger,
        strategies: list[BaseStrategy],
        *,
        filters: FilterConfig | None = None,
        start_cash: float = 10_000.0,
        position_usd: float | None = None,
        tp_pct: float = 10.0,
        sl_pct: float = 5.0,
        max_hold_min: float = 60.0,
        max_open_per_strategy: int = 5,
        dry_run: bool = True,
    ):
        self.replay = replay
        self.ledger = ledger
        self.strategies = strategies
        self.filters = filters or FilterConfig()
        self.start_cash = float(start_cash)
        self.position_usd = position_usd  # if None, size_pct * equity
        self.tp_pct = float(tp_pct)
        self.sl_pct = float(sl_pct)
        self.max_hold_min = float(max_hold_min)
        self.max_open_per_strategy = max(1, int(max_open_per_strategy))
        self.dry_run = bool(dry_run)
        self.stats: dict[str, Any] = {
            "bars": 0,
            "signals": 0,
            "filtered_out": 0,
            "opens": 0,
            "closes": 0,
        }

    def _already_open(self, strategy_id: str, chain_id: str, token_address: str) -> bool:
        for p in self.ledger.list_open(strategy_id):
            if p.get("chain_id") == chain_id and p.get("token_address") == token_address:
                return True
        return False

    def _manage_exits(
        self,
        strategy_id: str,
        bar_ts: str,
        prices: dict[str, float],
    ) -> int:
        """Unified exit rules (shared with paper/live via execution.exits)."""
        closed_n = 0
        bar_dt = parse_ts(bar_ts)
        open_pos = list(self.ledger.list_open(strategy_id))

        def price_lookup(p: dict) -> Optional[float]:
            return prices.get(f"{p['chain_id']}|{p['pair_address']}")

        for pos, reason in scan_exits(open_pos, price_lookup, now=bar_dt):
            px = price_lookup(pos)
            if px is None:
                continue
            self.ledger.close_position(
                pos["id"], price=float(px), ts=bar_ts, reason=reason
            )
            closed_n += 1
            self.stats["closes"] += 1
        return closed_n

    def _size_usd(self, strategy_id: str, size_pct: float | None) -> float:
        if self.position_usd is not None:
            return float(self.position_usd)
        cash = self.ledger.get_cash(strategy_id)
        # Use cash as proxy for equity sizing (open MTM ignored for simplicity)
        pct = float(size_pct) if size_pct is not None else 0.05
        return max(0.0, cash * pct)

    def run(self) -> dict[str, Any]:
        window = self.replay.inspect_window()
        log.info(
            "backtest window start=%s end=%s ind=%s pairs=%s tokens=%s bars≈%s sufficient=%s msg=%s",
            window.start.isoformat(),
            window.end.isoformat(),
            window.indicator_rows,
            window.pair_rows,
            window.unique_tokens,
            window.unique_bars,
            window.sufficient,
            window.message,
        )
        if not window.sufficient:
            return {
                "ok": False,
                "reason": "insufficient_data",
                "message": window.message,
                "window": {
                    "start": window.start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end": window.end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "indicator_rows": window.indicator_rows,
                    "pair_rows": window.pair_rows,
                    "unique_tokens": window.unique_tokens,
                    "unique_bars": window.unique_bars,
                },
                "by_strategy": {},
            }

        # One ledger run per strategy for clean grouping (same run_id batch note)
        results: dict[str, dict[str, Any]] = {}
        # We run strategies in parallel on the same replay stream, sharing one run_id
        # tagged with strategy list.
        strat_ids = [s.strategy_id for s in self.strategies]
        run_id = self.ledger.start_run(
            strategy_id=",".join(strat_ids),
            params={
                "filters": {
                    "enabled": self.filters.enabled,
                    "min_liq": self.filters.min_liq,
                    "min_coverage": self.filters.min_coverage,
                    "min_price": self.filters.min_price,
                    "coverage_fail_open": self.filters.coverage_fail_open,
                },
                "tp_pct": self.tp_pct,
                "sl_pct": self.sl_pct,
                "max_hold_min": self.max_hold_min,
                "dry_run": self.dry_run,
                "start_cash": self.start_cash,
            },
            notes=window.message,
        )
        for s in self.strategies:
            self.ledger.ensure_account(s.strategy_id)
        self.ledger.commit()

        last_prices: dict[str, float] = {}
        for snap in self.replay.iter_snapshots():
            self.stats["bars"] += 1
            if snap.prices:
                last_prices.update(snap.prices)
            data = {
                "hot_tokens": snap.hot_tokens,
                "bar_ts": snap.bar_ts,
                "tick": self.stats["bars"],
                "mode": "backtest",
            }
            for strat in self.strategies:
                sid = strat.strategy_id
                # exits first
                self._manage_exits(sid, snap.bar_ts, snap.prices)
                data["open_positions"] = self.ledger.list_open(sid)
                try:
                    signals = strat.on_tick(data) or []
                except Exception as exc:  # noqa: BLE001
                    log.exception("strategy %s failed on bar %s: %s", sid, snap.bar_ts, exc)
                    continue
                open_count = len(self.ledger.list_open(sid))
                for sig in signals:
                    self.stats["signals"] += 1
                    action = (sig.get("action") or "").lower()
                    if action != "buy":
                        # sell signals: close matching open if any
                        if action == "sell":
                            for p in list(self.ledger.list_open(sid)):
                                if (
                                    p.get("chain_id") == sig.get("chain_id")
                                    and p.get("token_address") == sig.get("token_address")
                                ):
                                    key = f"{p['chain_id']}|{p['pair_address']}"
                                    px = snap.prices.get(key)
                                    if px is not None:
                                        self.ledger.close_position(
                                            p["id"],
                                            price=px,
                                            ts=snap.bar_ts,
                                            reason=sig.get("reason") or "signal_sell",
                                        )
                                        self.stats["closes"] += 1
                        continue

                    chain_id = str(sig.get("chain_id") or "")
                    token = str(sig.get("token_address") or "")
                    pair = sig.get("pair_address") or ""
                    if not chain_id or not token or not pair:
                        self.ledger.log_event(
                            strategy_id=sid,
                            action="skip_no_pair",
                            ts=snap.bar_ts,
                            chain_id=chain_id,
                            token_address=token,
                            reason="missing pair_address",
                            raw=sig,
                        )
                        continue
                    pkey = f"{chain_id}|{pair}"
                    tkey = f"{chain_id}|{token}"
                    price = snap.prices.get(pkey)
                    liq = snap.liquidities.get(pkey)
                    cov = snap.coverages.get(tkey)
                    # also accept values embedded on hot row
                    for row in snap.hot_tokens:
                        if (
                            row.get("chain_id") == chain_id
                            and row.get("token_address") == token
                        ):
                            if price is None:
                                price = row.get("price_usd")
                            if liq is None:
                                liq = row.get("liquidity_usd")
                            if cov is None:
                                cov = row.get("coverage")
                            break

                    fr = apply_entry_filters(
                        price=price,
                        liquidity=liq,
                        coverage=cov,
                        cfg=self.filters,
                    )
                    if not fr.ok:
                        self.stats["filtered_out"] += 1
                        self.ledger.log_event(
                            strategy_id=sid,
                            action="filtered",
                            ts=snap.bar_ts,
                            chain_id=chain_id,
                            token_address=token,
                            pair_address=pair,
                            price=fr.price,
                            reason=";".join(fr.reasons),
                            filter_ok=False,
                            filter_reasons=fr.reasons,
                            raw=sig,
                        )
                        continue

                    if self._already_open(sid, chain_id, token):
                        continue
                    if open_count >= self.max_open_per_strategy:
                        self.ledger.log_event(
                            strategy_id=sid,
                            action="skip_max_open",
                            ts=snap.bar_ts,
                            chain_id=chain_id,
                            token_address=token,
                            pair_address=pair,
                            reason=f"open>={self.max_open_per_strategy}",
                        )
                        continue

                    size = self._size_usd(sid, sig.get("size_pct"))
                    pid = self.ledger.open_position(
                        strategy_id=sid,
                        chain_id=chain_id,
                        pair_address=str(pair),
                        token_address=token,
                        price=float(fr.price or 0),
                        size_usd=size,
                        ts=snap.bar_ts,
                        tp_pct=self.tp_pct,
                        sl_pct=self.sl_pct,
                        max_hold_min=self.max_hold_min,
                        reason=sig.get("reason") or "",
                        meta=sig,
                        size_pct=sig.get("size_pct"),
                    )
                    if pid:
                        open_count += 1
                        self.stats["opens"] += 1

                # mark equity each bar
                self.ledger.mark_equity(
                    strategy_id=sid, ts=snap.bar_ts, marks=snap.prices
                )

            if self.stats["bars"] % 50 == 0:
                self.ledger.commit()

        # Force-close leftovers at last bar mid (use entry if no mark)
        for strat in self.strategies:
            sid = strat.strategy_id
            for p in list(self.ledger.list_open(sid)):
                # use entry as last resort
                key = f"{p['chain_id']}|{p['pair_address']}"
                # close at last known mark price (fall back to entry only if never marked)
                px = float(last_prices.get(key, p["entry_price"]))
                # try last known from equity path — not stored; flat close
                self.ledger.close_position(
                    p["id"], price=px, ts=window.end.strftime("%Y-%m-%dT%H:%M:%SZ"), reason="eod_flat"
                )
                self.stats["closes"] += 1

        by_strategy: dict[str, dict[str, Any]] = {}
        end_eq_sum = 0.0
        for strat in self.strategies:
            sid = strat.strategy_id
            closed = self.ledger.list_closed(sid)
            curve = self.ledger.equity_series(sid)
            m = compute_metrics(closed, curve, start_cash=self.start_cash)
            by_strategy[sid] = m
            end_eq_sum += float(m.get("final_equity") or self.start_cash)

        self.ledger.finish_run(
            strategy_id=",".join(strat_ids),
            end_equity=end_eq_sum / max(1, len(self.strategies)),
            status="done",
        )
        self.ledger.commit()
        log.info(
            "backtest done bars=%s signals=%s filtered=%s opens=%s closes=%s",
            self.stats["bars"],
            self.stats["signals"],
            self.stats["filtered_out"],
            self.stats["opens"],
            self.stats["closes"],
        )
        return {
            "ok": True,
            "run_id": run_id,
            "message": window.message,
            "window": {
                "start": window.start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": window.end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "indicator_rows": window.indicator_rows,
                "pair_rows": window.pair_rows,
                "unique_tokens": window.unique_tokens,
                "unique_bars": window.unique_bars,
            },
            "stats": dict(self.stats),
            "by_strategy": by_strategy,
            "dry_run": self.dry_run,
            "ledger_path": str(self.ledger.path),
        }
