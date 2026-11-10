"""Tick loop: load indicators → strategies → risk → signal bus."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from logging_setup import get_logger

from decision_engine import config as de_config
from decision_engine.core.binance_indicators import (
    binance_indicators_ready,
    list_binance_hot_tokens,
    resolve_live_bnb_factor_source,
)
from decision_engine.core.bnb_native import limit_kline_scan, load_native_candidate_rows
from decision_engine.core.live_bnb_entry_coords import attach_live_entry_coordinates
from decision_engine.core.live_bnb_turnover import (
    cap_live_bnb_buys,
    filter_live_bnb_hot,
    stamp_turnover,
)
from decision_engine.core.live_buy_guards import apply_live_buy_guards
from decision_engine.core.volume_z_1h import (
    attach_stored_volume_z,
    persist_volume_z_for_decision_tick,
)
from decision_engine.core.macro_circuit_breaker import (
    evaluate_macro_gate,
    resolve_macro_db_path,
)
from decision_engine.core.protection_halt import (
    ProtectionBook,
    purpose_halt_reason,
    read_protection_book,
    resolve_ledger_path,
)
from decision_engine.core.bnb_spot_map import (
    default_listing_db_paths,
    load_binance_spot_usdt_symbols,
    load_token_bases,
    map_hot_tokens_to_binance_spot,
    stamp_bnb_spot_signals,
)
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import (
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    PURPOSE_LIVE_BNB_BOLL_FADE,
    SignalBus,
)
from decision_engine.entry_ih_age import filter_buys_by_entry_ih_age
from decision_engine.strategies.base_strategy import BaseStrategy

log = get_logger("decision_engine.engine")

# Binance spot scan and factor feed. Entry rules stay on the strategy.
LIVE_BNB_SPOT_PURPOSES = frozenset(
    {
        PURPOSE_LIVE_BNB_BOLL_FADE,
        PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    }
)


def scan_limit_for_purpose(purpose: str, explicit: int | None = None) -> int:
    """How many names one tick may load.

    A scan window, not an entry gate. Strategy thresholds stay put.
    Binance spot purposes (``live_bnb_boll_fade``, ``live_bnb_boll_classic``)
    default to at least ``LIVE_BNB_SCAN_LIMIT`` so a broad dump does not drop
    spot names ranked past the global hot limit (universe about 518). Other
    purposes keep ``HOT_TOKEN_LIMIT``. An explicit limit, including
    ``--limit``, is kept as given.
    """
    if explicit is not None:
        return int(explicit)
    if purpose in LIVE_BNB_SPOT_PURPOSES:
        return max(int(de_config.HOT_TOKEN_LIMIT), int(de_config.LIVE_BNB_SCAN_LIMIT))
    return int(de_config.HOT_TOKEN_LIMIT)


class DecisionEngine:
    def __init__(
        self,
        adapter: IndicatorDataAdapter,
        bus: SignalBus,
        strategies: list[BaseStrategy],
        *,
        risk: RiskManager | None = None,
        hot_limit: int | None = None,
        token_cooldown: Any = None,
        entry_ih_min_hours: float | None = None,
        protection_book: ProtectionBook | None = None,
        protection_ledger: str | Path | None = None,
        macro_db: str | Path | None = None,
    ):
        self.adapter = adapter
        self.bus = bus
        self.strategies = strategies
        self.risk = risk or RiskManager()
        self.hot_limit = scan_limit_for_purpose(bus.purpose, hot_limit)
        self.token_cooldown = token_cooldown
        self.entry_ih_min_hours = (
            None if entry_ih_min_hours is None else float(entry_ih_min_hours)
        )
        if protection_book is not None:
            self._protection_book: ProtectionBook | None = protection_book
            self._protection_ledger: Path | None = None
        else:
            self._protection_book = None
            self._protection_ledger = (
                Path(protection_ledger).expanduser()
                if protection_ledger is not None
                else resolve_ledger_path()
            )
        self._macro_db: Path | None = (
            Path(macro_db).expanduser() if macro_db is not None else None
        )
        self._protection_unwired_logged = False
        self.tick_count = 0
        resolved = getattr(adapter, "db_path", None)
        if resolved is not None:
            log.info("resolved DB_PATH=%s", resolved)

    def tick(self) -> dict[str, Any]:
        self.tick_count += 1
        n_ind = self.adapter.count_indicators()
        hot = self.adapter.list_hot_tokens(limit=self.hot_limit)
        bnb_spot = self.bus.purpose in LIVE_BNB_SPOT_PURPOSES
        bnb_source: str | None = None
        turnover_report: dict[str, Any] | None = None
        protection_book = self._protection_snapshot() if bnb_spot else ProtectionBook.unwired()
        protection_halt = (
            purpose_halt_reason(self.bus.purpose, protection_book) if bnb_spot else None
        )
        now_ms = int(time.time() * 1000)
        macro_path = self._macro_db if self._macro_db is not None else resolve_macro_db_path()
        macro_status = evaluate_macro_gate(
            macro_path,
            now_ms=now_ms,
            purpose=self.bus.purpose,
        )
        macro_halt = macro_status.halt_reason
        if bnb_spot:
            hot, bnb_source = self._load_live_bnb_hot(hot)
            # Coordinates are the entry-zone overlay. The classic arm reads
            # the factor fields the paper legs already use.
            if self.bus.purpose == PURPOSE_LIVE_BNB_BOLL_FADE:
                hot = attach_live_entry_coordinates(
                    self.adapter.conn,
                    hot,
                    now_ms=now_ms,
                )
                from decision_engine.core.perp_microstructure_and_beta_hedge import (
                    attach_counterparty_context,
                )
                from decision_engine.core.cityu_institutional_alpha import (
                    attach_cityu_institutional_context,
                )

                hot = attach_counterparty_context(hot, now_ms=now_ms)
                hot = attach_cityu_institutional_context(hot, now_ms=now_ms)
        scanned = len(hot)
        if bnb_spot:
            # Floor and universe membership. The 600-name scan is already loaded;
            # Top 2 is applied only after eligibility, risk, and cooldown.
            hot, turnover_report = filter_live_bnb_hot(
                self.adapter.conn,
                self.adapter.db_path,
                hot,
                now_ms=now_ms,
            )
        log.info(
            "tick#%s indicators_total=%s hot_loaded=%s bnb_factor_source=%s strategies=%s",
            self.tick_count,
            n_ind,
            scanned,
            bnb_source or "-",
            [s.strategy_id for s in self.strategies],
        )
        data = {
            "hot_tokens": hot,
            "indicators_total": n_ind,
            "tick": self.tick_count,
            "adapter": self.adapter,
            "purpose": self.bus.purpose,
        }
        candidates: list[dict[str, Any]] = []
        for strat in self.strategies:
            try:
                out = strat.on_tick(data) or []
                candidates.extend(out)
            except Exception as exc:  # noqa: BLE001
                log.exception("strategy %s failed: %s", strat.strategy_id, exc)

        if bnb_spot:
            before = len(candidates)
            candidates = stamp_bnb_spot_signals(candidates, hot)
            log.info(
                "bnb_spot_stamp kept=%s dropped_unmapped=%s",
                len(candidates),
                before - len(candidates),
            )

        entry_ih_rejected: dict[str, int] | None = None
        if self.entry_ih_min_hours is not None:
            candidates, entry_ih_rejected = filter_buys_by_entry_ih_age(
                candidates,
                self.adapter.latest_indicator_history_recorded_at,
                min_hours=self.entry_ih_min_hours,
            )
            log.info(
                "entry_ih_gate min_hours=%s rejected=%s remaining=%s",
                self.entry_ih_min_hours,
                dict(sorted(entry_ih_rejected.items())),
                len(candidates),
            )

        if not candidates:
            log.info("tick#%s 无信号 (no candidates)", self.tick_count)
            return self._tick_payload(
                hot_loaded=scanned,
                indicators_total=n_ind,
                candidates=0,
                emitted=0,
                entry_ih_rejected=entry_ih_rejected,
                bnb_factor_source=bnb_source,
                turnover_report=turnover_report,
                protection_halt=protection_halt,
                macro_halt=macro_halt,
            )

        # Shared buy choke point: every strategy's candidates pass through
        # RiskManager before SignalBus.emit. Liquidity comes from the scanned
        # rows (adapter batch lookup), so tech/combined/jev do not each filter.
        # Also stamp pairCreatedAt (ms) for decision_log extras / token_age.
        self._stamp_buy_liquidity(candidates, hot)
        if bnb_spot:
            stamp_turnover(candidates, hot)
        passed = self.risk.filter(candidates)
        if self.token_cooldown is not None:
            passed = self.token_cooldown.filter(passed)
        passed, guard_report = apply_live_buy_guards(
            passed,
            purpose=self.bus.purpose,
            book=protection_book,
            macro_halt=macro_halt,
        )
        top_report: dict[str, Any] | None = None
        if bnb_spot:
            from decision_engine.core.regime_strategy_router import (
                apply_five_state_regime_router,
            )

            persist_volume_z_for_decision_tick(self.adapter.db_path, now_ms=now_ms)
            hot = attach_stored_volume_z(self.adapter.conn, hot, now_ms=now_ms)
            passed, _ = apply_five_state_regime_router(
                passed,
                hot,
                macro_halt=macro_halt,
            )
            passed, top_report = cap_live_bnb_buys(passed, now_ms=now_ms)
        emitted = self.bus.publish_many(passed)
        log.info(
            "tick#%s candidates=%s emitted=%s",
            self.tick_count,
            len(candidates),
            len(emitted),
        )
        return self._tick_payload(
            hot_loaded=scanned,
            indicators_total=n_ind,
            candidates=len(candidates),
            emitted=len(emitted),
            entry_ih_rejected=entry_ih_rejected,
            bnb_factor_source=bnb_source,
            turnover_report=turnover_report,
            top_report=top_report,
            protection_halt=guard_report.get("protection_halt") or protection_halt,
            macro_halt=guard_report.get("macro_halt") or macro_halt,
            stop_gap_rejected=int(guard_report.get("stop_gap_rejected") or 0),
        )

    def _tick_payload(
        self,
        *,
        hot_loaded: int,
        indicators_total: int,
        candidates: int,
        emitted: int,
        entry_ih_rejected: dict[str, int] | None,
        bnb_factor_source: str | None = None,
        turnover_report: dict[str, Any] | None = None,
        top_report: dict[str, Any] | None = None,
        protection_halt: str | None = None,
        macro_halt: str | None = None,
        stop_gap_rejected: int = 0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tick": self.tick_count,
            "hot_loaded": hot_loaded,
            "indicators_total": indicators_total,
            "candidates": candidates,
            "emitted": emitted,
        }
        if entry_ih_rejected is not None:
            payload["entry_ih_rejected"] = entry_ih_rejected
        if bnb_factor_source is not None:
            payload["bnb_factor_source"] = bnb_factor_source
        if turnover_report is not None:
            payload["quote_turnover"] = turnover_report
        if top_report is not None:
            payload["live_bnb_top_buys"] = top_report
        if protection_halt:
            payload["protection_halt"] = protection_halt
        if macro_halt:
            payload["macro_halt"] = macro_halt
        if stop_gap_rejected:
            payload["stop_gap_rejected"] = stop_gap_rejected
        return payload

    def _protection_snapshot(self) -> ProtectionBook:
        """Ledger book for this tick. A static book is the test injection.

        An unconfigured path stays unwired. A configured path that cannot
        be read comes back with ``error`` set so the halt fails closed.
        """
        if self._protection_book is not None:
            return self._protection_book
        path = self._protection_ledger
        if path is None:
            if not self._protection_unwired_logged:
                log.info(
                    "protection_book_unwired purpose=%s "
                    "set DECISION_EXECUTION_LEDGER to the execution ledger",
                    self.bus.purpose,
                )
                self._protection_unwired_logged = True
            return ProtectionBook.unwired()
        return read_protection_book(path)

    def _load_live_bnb_hot(
        self, dex_hot: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], str]:
        """CEX factors for a Binance spot purpose.

        ``auto`` (default): ``binance_indicators`` once that table exists,
        even if this pass is empty — klines are not recomputed on top of it.
        If the table was never created, use ``binance_klines`` (in-process
        factors) when those bars exist. DEX∩listing only when neither CEX
        feed is available. ``binance`` never fills from DEX. ``dex_map``
        keeps the old intersection.
        """
        source = resolve_live_bnb_factor_source(os.environ.get("LIVE_BNB_FACTOR_SOURCE"))
        if source == "dex_map":
            log.info("bnb_factor_source=dex_map requested=%s", source)
            return self._map_bnb_spot_universe(dex_hot), "dex_map"
        if binance_indicators_ready(self.adapter.conn):
            rows = list_binance_hot_tokens(self.adapter.conn, limit=self.hot_limit)
            log.info(
                "bnb_factor_source=binance_indicators requested=%s rows=%s",
                source,
                len(rows),
            )
            return rows, "binance_indicators"
        kline_rows, snapshot_empty = self._load_kline_mvp()
        if kline_rows or snapshot_empty:
            log.info(
                "bnb_factor_source=binance_klines requested=%s rows=%s snapshot_empty=%s",
                source,
                len(kline_rows),
                int(snapshot_empty),
            )
            return kline_rows, "binance_klines"
        if source == "binance":
            log.info("bnb_factor_source=binance_indicators requested=%s rows=0", source)
            return [], "binance_indicators"
        log.info(
            "bnb_factor_source=dex_map requested=%s binance_indicators=no klines=no",
            source,
        )
        return self._map_bnb_spot_universe(dex_hot), "dex_map"

    def _load_kline_mvp(self) -> tuple[list[dict[str, Any]], bool]:
        """Factors from ``binance_klines``. Not used when ``binance_indicators`` exists.

        The second value is true when a collection symbol snapshot was present
        and produced no rows, so ``auto`` must not fill from the DEX map.
        """
        try:
            rows, stats = load_native_candidate_rows(
                self.adapter.conn,
                self.adapter.db_path,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("binance_klines failed closed: %s", exc)
            return [], False
        if not rows:
            log.info(
                "binance_klines empty listed=%s kline_symbols=%s fetched=%s primary=%s",
                stats.get("listed"),
                stats.get("kline_symbols"),
                stats.get("fetched"),
                stats.get("primary_snapshot"),
            )
            return [], bool(stats.get("primary_snapshot"))
        return limit_kline_scan(rows, self.hot_limit), False

    def _map_bnb_spot_universe(self, hot: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace the scan set with listed Binance spot USDT names.

        Failure or an empty listing set yields no rows. The original DEX
        identities are not passed through on this purpose.
        """
        try:
            listed = load_binance_spot_usdt_symbols(
                self.adapter.conn,
                extra_paths=default_listing_db_paths(self.adapter.db_path),
            )
            bases = load_token_bases(self.adapter.conn, hot)
            mapped, stats = map_hot_tokens_to_binance_spot(
                hot,
                listed_symbols=listed,
                symbol_by_token=bases,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("bnb_spot_map failed closed: %s", exc)
            return []
        log.info(
            "bnb_spot_map listed=%s scanned=%s mapped=%s "
            "skipped_no_symbol=%s skipped_unlisted=%s skipped_duplicate=%s",
            len(listed),
            stats["scanned"],
            stats["mapped"],
            stats["skipped_no_symbol"],
            stats["skipped_unlisted"],
            stats["skipped_duplicate"],
        )
        return mapped

    def _stamp_buy_liquidity(
        self,
        candidates: list[dict[str, Any]],
        hot: list[dict[str, Any]],
    ) -> None:
        """Copy scanned-row liquidity onto buys; stamp pairCreatedAt on all actions.

        Liquidity: Missing row → leave unset (fail closed for the buy floor).
        pairCreatedAt: ms epoch from pairs.pair_created_at when the hot row has
        it. Does not invent values; SignalBus may still look up pairs at write
        time if this stamp is absent. No gate / weight change.
        """
        by_token: dict[tuple[str, str], dict[str, Any]] = {}
        for row in hot:
            key = (
                str(row.get("chain_id") or ""),
                str(row.get("token_address") or ""),
            )
            meta: dict[str, Any] = {"liquidity_usd": row.get("liquidity_usd")}
            if row.get("pair_created_at") is not None:
                meta["pair_created_at"] = row.get("pair_created_at")
            by_token[key] = meta
        for sig in candidates:
            key = (
                str(sig.get("chain_id") or ""),
                str(sig.get("token_address") or ""),
            )
            meta = by_token.get(key)
            if meta is None:
                continue
            if str(sig.get("action") or "").lower() == "buy":
                sig["liquidity_usd"] = meta.get("liquidity_usd")
            # Attribution reads extras.pairCreatedAt (ms). Prefer camelCase once.
            if (
                sig.get("pairCreatedAt") is None
                and sig.get("pair_created_at") is None
                and meta.get("pair_created_at") is not None
            ):
                try:
                    sig["pairCreatedAt"] = int(meta["pair_created_at"])
                except (TypeError, ValueError):
                    pass

    def run(
        self,
        *,
        once: bool = False,
        ticks: int | None = None,
        interval_sec: float = 60.0,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if once:
            ticks = 1
        if ticks is not None and ticks <= 0:
            return results

        n = 0
        while True:
            try:
                results.append(self.tick())
            except Exception as exc:  # noqa: BLE001
                log.exception("tick crashed: %s", exc)
                results.append({"tick": self.tick_count, "error": str(exc)})
            n += 1
            if ticks is not None and n >= ticks:
                break
            time.sleep(max(1.0, float(interval_sec)))
        return results
