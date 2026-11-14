"""Cutover check for the regime v2 profile.

The check reads clocks and signal fields. It does not place orders, does not
open a private exchange client, and does not invent a missing feature.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from decision_engine.core.binance_indicators import binance_indicators_fresh_sec
from decision_engine.core.cityu_institutional_alpha import due_cftc_asof
from decision_engine.core.macro_circuit_breaker import MAX_CROSS_ASSET_AGE_MS
from decision_engine.core.news_shock_gate import NEWS_WINDOW_MIN
from decision_engine.core.regime_strategy_router import (
    _STATE1_DISTINGUISHERS,
    state1_fallthrough_unconfirmed,
)
from decision_engine.core.volume_z_1h import source_close_is_fresh

HKT = timezone(timedelta(hours=8))
ENTRY_STRATEGY_ID = "tech_boll_fade_v1"
PROFILE_RELATIVE = Path("configs") / "live_regime_v2.env"

# One-shot and pyramid fractions printed in the 11-10 manual and the tier-1
# switch. They are not the 2026-11-12 24% baseline.
UNRULED_KELLY_FRACTIONS = frozenset(
    {0.0, 0.05, 0.185, 0.20, 0.30, 0.35, 0.45, 0.60, 0.70, 0.85, 0.95}
)

REQUIRED_DERIVATIVE_CLOCKS = (
    ("binance_perp_counterparty", "ts_ms"),
    ("cross_exchange_spot_baseline", "ts_ms"),
    ("deribit_options_gamma", "ts_ms"),
)
OBI_READER_CLOCK = ("binance_spot_microstructure", "obi_top5_source_ts_ms")

_OFF_KEYS = (
    "USE_OPTIMAL_KELLY_MATRIX",
    "EXPAND_REGIME_OCO_TP",
    "ENABLE_PERP_SHORT_ENGINE",
    "BINANCE_PERP_SHORT_LIVE_ENABLED",
    "BINANCE_SPOT_TRAILING_RATCHET",
    "BINANCE_SPOT_MAX_HOLD_SEC",
    "BINANCE_SPOT_REANCHOR_OCO_ON_FILL",
    "ENFORCE_REAL_DEMAND_WHITELIST",
    "LIVE_BNB_INVERSE_VOL_SIZING",
)
_ON_KEYS = (
    "BINANCE_SPOT_ORDERBOOK_GATE",
    "BINANCE_SPOT_POST_ONLY_LIMIT",
    "LIVE_BNB_POST_ONLY_LIMIT",
    "BINANCE_SPOT_SOR_MAKER_FALLBACK",
    "DECISION_MACRO_GATE_ENABLED",
)


def _block(code: str, detail: str, *, source: str) -> dict[str, str]:
    return {"code": code, "detail": detail, "source": source}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def profile_path() -> Path:
    return repo_root() / PROFILE_RELATIVE


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Comments and blank lines are skipped."""
    out: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            raise ValueError(f"{path}:{line_no}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            raise ValueError(f"{path}:{line_no}: empty key")
        out[key] = value
    return out


def profile_blocks(env: Mapping[str, str]) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    secret_markers = ("API_KEY", "API_SECRET", "SECRET", "TOKEN", "PASSWORD", "WEBHOOK")
    for key in env:
        upper = key.upper()
        if any(marker in upper for marker in secret_markers):
            blocks.append(_block("profile_secret", key, source="profile"))
    for key in _OFF_KEYS:
        if str(env.get(key, "")).strip() not in {"0", "false", "no", "off"}:
            blocks.append(
                _block("profile_switch_not_off", f"{key}={env.get(key)!r}", source="profile")
            )
    for key in _ON_KEYS:
        if str(env.get(key, "")).strip().lower() not in {"1", "true", "yes", "on"}:
            blocks.append(
                _block("profile_gate_not_on", f"{key}={env.get(key)!r}", source="profile")
            )
    position = str(env.get("REGIME_V2_POSITION_PCT", "")).strip()
    if position != "0.24":
        blocks.append(
            _block(
                "position_not_the_pending_baseline",
                f"REGIME_V2_POSITION_PCT={position!r}; 24% remains pending, not a new Kelly fraction",
                source="profile",
            )
        )
    for key, relative in (
        ("PUBLIC_DERIVATIVES_DB_PATH", "data/counterparty/public_derivatives.db"),
        ("NEWS_EVENTS_DB_PATH", "news_events.db"),
        ("DECISION_MACRO_DB", "data/lseg/lseg_macro.db"),
    ):
        if str(env.get(key, "")).strip() != relative:
            blocks.append(
                _block("feature_path_not_wired", f"{key}={env.get(key)!r}", source="profile")
            )
    if "DECISION_EXECUTION_LEDGER_FADE" not in env or "DECISION_EXECUTION_LEDGER_CLASSIC" not in env:
        blocks.append(
            _block(
                "purpose_ledger_not_named",
                "fade and classic ledgers must be named separately",
                source="profile",
            )
        )
    if env.get("DECISION_EXECUTION_LEDGER_FADE") == env.get("DECISION_EXECUTION_LEDGER_CLASSIC"):
        blocks.append(
            _block("purpose_ledger_shared", "fade and classic ledgers are the same path", source="profile")
        )
    return blocks


def _parse_hkt(text: str) -> datetime:
    cleaned = text.replace("HKT", "").strip()
    parsed = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
    return parsed.replace(tzinfo=HKT)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _age_block(
    *,
    code: str,
    detail: str,
    ts_ms: int | None,
    now_ms: int,
    window_ms: int,
    source: str,
) -> dict[str, str] | None:
    if ts_ms is None:
        return _block(code, detail + ": clock absent", source=source)
    age = now_ms - ts_ms
    if age < 0 or age > window_ms:
        return _block(code, f"{detail}: age_ms={age} window_ms={window_ms}", source=source)
    return None


def _is_supplement(document: Mapping[str, Any]) -> bool:
    derivatives = document.get("public_derivatives")
    return isinstance(derivatives, dict) and "microstructure_max" in derivatives


def supplement_clock_blocks(
    document: Mapping[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Judge the 11:34 HKT supplement by the clocks the readers already use.

    A production path in the evidence file does not create that file here.
    COT uses the publication cycle, not a seconds TTL. Unlock rows have no
    collection clock, so file mtime is not a freshness result.
    """
    blocks: list[dict[str, str]] = []
    observations: list[dict[str, str]] = []
    now_ms = _as_int(document.get("now_ms"))
    if now_ms is None:
        try:
            now_ms = int(_parse_hkt(str(document.get("written_hkt") or "")).timestamp() * 1000)
        except (TypeError, ValueError):
            return [_block("pack_as_of_unparsed", str(document.get("written_hkt")), source="production_evidence")], observations
    fresh_ms = binance_indicators_fresh_sec() * 1000
    as_of = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

    derivatives = document.get("public_derivatives")
    if not isinstance(derivatives, dict):
        blocks.append(_block("public_derivatives_missing", "supplement", source="production_evidence"))
    else:
        observations.append(
            _block(
                "production_store_present_in_evidence",
                str(derivatives.get("path") or "public_derivatives.db"),
                source="production_evidence",
            )
        )
        micro = derivatives.get("microstructure_max")
        obi_ms = None
        if isinstance(micro, dict):
            obi_ms = _as_int(micro.get("max_obi_top5_source_ts_ms"))
        found = _age_block(
            code="obi_top5_source_stale_or_absent",
            detail="obi_top5_source_ts_ms",
            ts_ms=obi_ms,
            now_ms=now_ms,
            window_ms=fresh_ms,
            source="production_evidence",
        )
        if found is None:
            observations.append(
                _block("obi_top5_source.fresh", f"ts_ms={obi_ms} window_ms={fresh_ms}", source="production_evidence")
            )
        else:
            blocks.append(found)
        perp = derivatives.get("perp_counterparty_max")
        perp_ms = _as_int(perp.get("max_ts_ms")) if isinstance(perp, dict) else None
        found = _age_block(
            code="binance_perp_counterparty.stale_or_absent",
            detail="binance_perp_counterparty.ts_ms",
            ts_ms=perp_ms,
            now_ms=now_ms,
            window_ms=fresh_ms,
            source="production_evidence",
        )
        if found is None:
            observations.append(
                _block(
                    "binance_perp_counterparty.fresh",
                    f"ts_ms={perp_ms} window_ms={fresh_ms}",
                    source="production_evidence",
                )
            )
        else:
            blocks.append(found)
        columns = derivatives.get("token_unlock_schedule_columns")
        clock_names = {"ts_ms", "source_ts_ms", "collected_at_ms", "fetched_at"}
        has_record_clock = isinstance(columns, list) and any(str(name) in clock_names for name in columns)
        if not has_record_clock:
            observations.append(
                _block(
                    "token_unlock_record_clock_absent",
                    "next_unlock_utc is recomputed at read time; mtime is not a freshness clock",
                    source="production_evidence",
                )
            )

    close_ms = _volume_z_close_ms(document)
    if close_ms is None:
        blocks.append(_block("volume_z_close_clock_absent", "source_close_time_ms", source="production_evidence"))
    elif source_close_is_fresh(close_ms, now_ms):
        observations.append(
            _block(
                "volume_z_source_close.fresh",
                f"source_close_time_ms={close_ms} window_ms={fresh_ms}",
                source="production_evidence",
            )
        )
    else:
        blocks.append(
            _block(
                "volume_z_source_close_stale",
                f"source_close_time_ms={close_ms} window_ms={fresh_ms}",
                source="production_evidence",
            )
        )
    computed_ms = _volume_z_computed_ms(document)
    if computed_ms is not None:
        observations.append(
            _block(
                "volume_z_computed_at_not_the_reader_clock",
                f"computed_at_ms={computed_ms} fresh_under_same_window="
                f"{source_close_is_fresh(computed_ms, now_ms)}",
                source="production_evidence",
            )
        )

    news = document.get("news_events")
    fetched = _nested_text(news, "max_clocks", "fetched_at", "raw") if isinstance(news, dict) else None
    fetched_ms = _utc_ms(fetched or "")
    collector = _collector_observed(document, "news_events collector process")
    found = _age_block(
        code="news_fetched_at_stale",
        detail=f"fetched_at={fetched or 'absent'}; collector_observed={collector}",
        ts_ms=fetched_ms,
        now_ms=now_ms,
        window_ms=NEWS_WINDOW_MIN * 60_000,
        source="production_evidence",
    )
    if found is None and isinstance(news, dict):
        observations.append(_block("news_fetched_at.fresh", fetched or "", source="production_evidence"))
    elif found is not None:
        blocks.append(found)

    lseg = document.get("lseg_macro")
    bar_ts = ""
    if isinstance(lseg, dict):
        bar_ts = str(lseg.get("cross_asset_bars_max_utc_ts") or "")
        if not bar_ts:
            bar_ts = _nested_text(lseg, "cross_asset_bars", "max_utc_ts", "raw") or ""
    lseg_collector = _collector_observed(document, "lseg collector process")
    found = _age_block(
        code="lseg_cross_asset_bars_stale",
        detail=f"cross_asset_bars.utc_ts={bar_ts or 'absent'}; collector_observed={lseg_collector}",
        ts_ms=_utc_ms(bar_ts),
        now_ms=now_ms,
        window_ms=MAX_CROSS_ASSET_AGE_MS,
        source="production_evidence",
    )
    if found is not None:
        blocks.append(found)

    cot_date = _cot_max_date(document)
    due = due_cftc_asof(as_of).isoformat()
    observations.append(
        _block(
            "cot_publication_cycle_not_a_ttl",
            f"max_date={cot_date or 'absent'}; due_cftc_asof={due}; no seconds TTL is applied",
            source="production_evidence",
        )
    )
    return blocks, observations


def _volume_z_close_ms(document: Mapping[str, Any]) -> int | None:
    search = document.get("volume_z_search")
    if isinstance(search, dict):
        stored = search.get("decision_volume_z")
        if isinstance(stored, dict):
            raw = stored.get("max_source_close_time_ms")
            if isinstance(raw, dict):
                return _as_int(raw.get("raw"))
            return _as_int(raw)
    addendum = document.get("decision_volume_z")
    if isinstance(addendum, dict):
        return _as_int(addendum.get("max_source_close_time_ms"))
    return None


def _volume_z_computed_ms(document: Mapping[str, Any]) -> int | None:
    addendum = document.get("decision_volume_z")
    if isinstance(addendum, dict):
        return _as_int(addendum.get("max_computed_at_ms"))
    return None


def _cot_max_date(document: Mapping[str, Any]) -> str | None:
    cityu = document.get("cityu_institutional")
    if not isinstance(cityu, dict):
        return None
    clocks = cityu.get("readonly_clocks")
    if not isinstance(clocks, dict):
        return None
    cot = clocks.get("lseg_cftc_cot_positioning")
    if not isinstance(cot, dict):
        return None
    raw = cot.get("max_date")
    if isinstance(raw, dict):
        text = str(raw.get("raw") or "").strip()
        return text or None
    text = str(raw or "").strip()
    return text or None


def _collector_observed(document: Mapping[str, Any], name: str) -> bool | None:
    collectors = document.get("collectors_observed_this_check")
    if not isinstance(collectors, dict):
        return None
    row = collectors.get(name)
    if not isinstance(row, dict) or "observed" not in row and "observed_by_process_name_search" not in row:
        return None
    if "observed_by_process_name_search" in row:
        return bool(row.get("observed_by_process_name_search"))
    return bool(row.get("observed"))


def _nested_text(value: object, *keys: str) -> str | None:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if current is None:
        return None
    text = str(current).strip()
    return text or None


def pack_clock_blocks(document: Mapping[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Judge a 2026-11-14 evidence file by record clocks, not file mtime."""
    if _is_supplement(document):
        return supplement_clock_blocks(document)
    blocks: list[dict[str, str]] = []
    observations: list[dict[str, str]] = []
    written = str(document.get("written_hkt") or "")
    try:
        as_of = _parse_hkt(written)
    except (TypeError, ValueError):
        return [_block("pack_as_of_unparsed", written, source="pack")], observations
    now_ms = int(as_of.timestamp() * 1000)
    fresh_ms = binance_indicators_fresh_sec() * 1000
    databases = document.get("databases")
    if not isinstance(databases, dict):
        return [_block("pack_databases_absent", "databases", source="pack")], observations

    derivatives = databases.get("public_derivatives")
    if not isinstance(derivatives, dict) or not derivatives.get("exists"):
        blocks.append(_block("public_derivatives_missing", "pack", source="pack"))
        deriv_max: dict[str, Any] = {}
    else:
        raw_max = derivatives.get("max_time")
        deriv_max = raw_max if isinstance(raw_max, dict) else {}
        for table, column in REQUIRED_DERIVATIVE_CLOCKS:
            key = f"{table}.{column}"
            found = _age_block(
                code=f"{table}.stale_or_absent",
                detail=key,
                ts_ms=_as_int(deriv_max.get(key)),
                now_ms=now_ms,
                window_ms=fresh_ms,
                source="pack",
            )
            if found is None:
                observations.append(
                    _block(f"{table}.fresh", f"{key} within {fresh_ms}ms", source="pack")
                )
            else:
                blocks.append(found)
        obi_table, obi_column = OBI_READER_CLOCK
        obi_key = f"{obi_table}.{obi_column}"
        if obi_key not in deriv_max:
            blocks.append(
                _block(
                    "obi_top5_source_clock_not_in_evidence",
                    f"reader uses {obi_key}; pack max_time has {obi_table}.ts_ms instead",
                    source="pack",
                )
            )
        else:
            found = _age_block(
                code="obi_top5_source_stale_or_absent",
                detail=obi_key,
                ts_ms=_as_int(deriv_max.get(obi_key)),
                now_ms=now_ms,
                window_ms=fresh_ms,
                source="pack",
            )
            if found is not None:
                blocks.append(found)
        stable = _as_int(deriv_max.get("stablecoin_supply.ts_ms"))
        if stable is None or now_ms - stable > fresh_ms:
            observations.append(
                _block(
                    "stablecoin_supply_not_a_router_input",
                    f"ts_ms={stable}; stale or absent, not read by attach_counterparty_context",
                    source="pack",
                )
            )
        if deriv_max.get("onchain_holder_bot_summary.ts_ms") in (None, ""):
            observations.append(
                _block(
                    "onchain_holder_clock_absent",
                    "not read by the spot router",
                    source="pack",
                )
            )
        if "token_unlock_schedule.next_unlock_utc" not in deriv_max and not _unlock_sample(derivatives):
            blocks.append(
                _block(
                    "token_unlock_values_not_in_evidence",
                    "row count is not next_unlock_utc; samples are empty",
                    source="pack",
                )
            )

    news = databases.get("news_events")
    news_max = news.get("max_time") if isinstance(news, dict) else None
    if not isinstance(news, dict) or not news.get("exists"):
        blocks.append(_block("news_events_missing", "pack", source="pack"))
    elif not isinstance(news_max, dict) or not news_max:
        blocks.append(
            _block(
                "news_record_clock_absent",
                f"mtime is not fetched_at; window is {NEWS_WINDOW_MIN} minutes",
                source="pack",
            )
        )

    lseg = databases.get("lseg_macro")
    if not isinstance(lseg, dict) or not lseg.get("exists"):
        blocks.append(_block("lseg_macro_missing", "pack", source="pack"))
    else:
        lseg_max = lseg.get("max_time") if isinstance(lseg.get("max_time"), dict) else {}
        bar_ts = str(lseg_max.get("cross_asset_bars.utc_ts") or "")
        bar_ms = _utc_ms(bar_ts)
        found = _age_block(
            code="lseg_cross_asset_bars_stale",
            detail=f"cross_asset_bars.utc_ts={bar_ts or 'absent'}; contagion window is 5min bars",
            ts_ms=bar_ms,
            now_ms=now_ms,
            window_ms=MAX_CROSS_ASSET_AGE_MS,
            source="pack",
        )
        if found is not None:
            blocks.append(found)
        sample_interval = _sample_interval(lseg)
        if sample_interval and sample_interval != "5min":
            observations.append(
                _block(
                    "lseg_sample_interval_is_not_5min",
                    sample_interval,
                    source="pack",
                )
            )

    if "cityu" not in databases and "cityu_institutional" not in databases:
        blocks.append(
            _block(
                "cot_store_not_in_pack",
                "cityu_institutional.db was not in the baseline pack; cot_regime cannot be replayed from it",
                source="pack",
            )
        )
    if "volume_z" not in databases and "indicators" not in databases:
        blocks.append(
            _block(
                "volume_z_store_not_in_pack",
                "1h volume_z is written from the indicator store, which was not in the pack",
                source="pack",
            )
        )
    return blocks, observations


def _unlock_sample(derivatives: Mapping[str, Any]) -> bool:
    samples = derivatives.get("samples")
    if not isinstance(samples, dict):
        return False
    rows = samples.get("token_unlock_schedule")
    return isinstance(rows, list) and bool(rows)


def _sample_interval(lseg: Mapping[str, Any]) -> str | None:
    samples = lseg.get("samples")
    if not isinstance(samples, dict):
        return None
    bars = samples.get("cross_asset_bars")
    if not isinstance(bars, list) or not bars:
        return None
    first = bars[0]
    if not isinstance(first, dict):
        return None
    text = str(first.get("interval") or "").strip()
    return text or None


def _utc_ms(text: str) -> int | None:
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


def local_store_blocks(env: Mapping[str, str], *, root: Path) -> list[dict[str, str]]:
    """Block when a wired store is absent. Do not treat mtime as freshness."""
    blocks: list[dict[str, str]] = []
    fresh_ms = binance_indicators_fresh_sec() * 1000
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    specs = (
        ("PUBLIC_DERIVATIVES_DB_PATH", "public_derivatives_missing"),
        ("NEWS_EVENTS_DB_PATH", "news_events_missing"),
        ("DECISION_MACRO_DB", "lseg_macro_missing"),
    )
    for key, code in specs:
        relative = str(env.get(key, "")).strip()
        path = Path(relative)
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            blocks.append(_block(code, str(path), source="local"))
            continue
        if key == "PUBLIC_DERIVATIVES_DB_PATH":
            blocks.extend(_sqlite_clock_blocks(path, now_ms=now_ms, fresh_ms=fresh_ms))
        elif key == "NEWS_EVENTS_DB_PATH":
            blocks.extend(_news_clock_blocks(path, now_ms=now_ms))
        else:
            blocks.extend(_lseg_clock_blocks(path, now_ms=now_ms))
    for key in ("DECISION_EXECUTION_LEDGER_FADE", "DECISION_EXECUTION_LEDGER_CLASSIC"):
        relative = str(env.get(key, "")).strip()
        path = root / relative if relative and not Path(relative).is_absolute() else Path(relative)
        if not relative or not path.is_file():
            blocks.append(_block("purpose_ledger_not_attached", f"{key}={path}", source="local"))
    return blocks


def _connect(path: Path) -> sqlite3.Connection | None:
    try:
        conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _max_int(conn: sqlite3.Connection, table: str, column: str) -> int | None:
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        return None
    row = conn.execute(f"SELECT MAX({column}) AS ts FROM {table}").fetchone()
    if row is None:
        return None
    return _as_int(row["ts"])


def _sqlite_clock_blocks(path: Path, *, now_ms: int, fresh_ms: int) -> list[dict[str, str]]:
    conn = _connect(path)
    if conn is None:
        return [_block("public_derivatives_unreadable", str(path), source="local")]
    blocks: list[dict[str, str]] = []
    try:
        for table, column in REQUIRED_DERIVATIVE_CLOCKS + (OBI_READER_CLOCK,):
            ts = _max_int(conn, table, column)
            found = _age_block(
                code=f"{table}.{column}.stale_or_absent",
                detail=str(path),
                ts_ms=ts,
                now_ms=now_ms,
                window_ms=fresh_ms,
                source="local",
            )
            if found is not None:
                blocks.append(found)
    except sqlite3.Error as exc:
        blocks.append(_block("public_derivatives_unreadable", exc.__class__.__name__, source="local"))
    finally:
        conn.close()
    return blocks


def _news_clock_blocks(path: Path, *, now_ms: int) -> list[dict[str, str]]:
    conn = _connect(path)
    if conn is None:
        return [_block("news_events_unreadable", str(path), source="local")]
    try:
        row = conn.execute(
            """
            SELECT MAX(fetched_at) AS fetched_at, MAX(published_at) AS published_at
            FROM news_events
            WHERE lower(source_tier) = 'whitelist'
            """
        ).fetchone()
    except sqlite3.Error as exc:
        return [_block("news_events_unreadable", exc.__class__.__name__, source="local")]
    finally:
        conn.close()
    fetched = None if row is None else row["fetched_at"]
    if not fetched:
        return [
            _block(
                "news_record_clock_absent",
                f"whitelist fetched_at absent; window {NEWS_WINDOW_MIN} minutes",
                source="local",
            )
        ]
    fetched_ms = _text_ms(str(fetched))
    found = _age_block(
        code="news_fetched_at_stale",
        detail=str(fetched),
        ts_ms=fetched_ms,
        now_ms=now_ms,
        window_ms=NEWS_WINDOW_MIN * 60_000,
        source="local",
    )
    return [] if found is None else [found]


def _lseg_clock_blocks(path: Path, *, now_ms: int) -> list[dict[str, str]]:
    conn = _connect(path)
    if conn is None:
        return [_block("lseg_macro_unreadable", str(path), source="local")]
    try:
        row = conn.execute(
            """
            SELECT MAX(utc_ms) AS ts
            FROM cross_asset_bars
            WHERE interval = '5min'
            """
        ).fetchone()
    except sqlite3.Error as exc:
        return [_block("lseg_macro_unreadable", exc.__class__.__name__, source="local")]
    finally:
        conn.close()
    ts = None if row is None else _as_int(row["ts"])
    found = _age_block(
        code="lseg_cross_asset_bars_stale",
        detail="interval=5min",
        ts_ms=ts,
        now_ms=now_ms,
        window_ms=MAX_CROSS_ASSET_AGE_MS,
        source="local",
    )
    return [] if found is None else [found]


def _text_ms(text: str) -> int | None:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    return None


def _close(left: object, right: float) -> bool:
    try:
        return abs(float(left) - right) <= 1e-9  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def signal_blocks(signal: Mapping[str, Any], *, kelly_off: bool) -> list[dict[str, str]]:
    """Reject a stamp that only renames the strategy or copies a Kelly fraction."""
    blocks: list[dict[str, str]] = []
    strategy_id = str(signal.get("strategy_id") or "")
    if strategy_id != ENTRY_STRATEGY_ID:
        blocks.append(
            _block(
                "strategy_is_not_the_entry_module",
                strategy_id or "absent",
                source="signal",
            )
        )
    classified = signal.get("regime_classification")
    playbook = str(signal.get("regime_playbook") or "")
    if not isinstance(classified, dict) or not isinstance(classified.get("inputs"), dict):
        blocks.append(_block("classification_not_replayable", playbook or "absent", source="signal"))
    elif state1_fallthrough_unconfirmed(playbook, classified):
        missing = [name for name in _STATE1_DISTINGUISHERS if name in set(classified.get("missing") or [])]
        blocks.append(
            _block(
                "state1_unconfirmed",
                "missing " + ",".join(missing),
                source="signal",
            )
        )
    if kelly_off:
        size = signal.get("size_pct")
        for fraction in UNRULED_KELLY_FRACTIONS:
            if _close(size, fraction):
                blocks.append(
                    _block(
                        "size_matches_unruled_kelly",
                        f"size_pct={size} fraction={fraction}",
                        source="signal",
                    )
                )
                break
    if str(signal.get("action") or "").lower() == "buy" and not signal.get("recommended_order_type"):
        blocks.append(_block("order_type_absent", playbook or strategy_id, source="signal"))
    return blocks


def isolation_blocks(
    *,
    kill_present: bool,
    dry_run: bool,
    output_db: Path,
    source_db: Path,
    isolated_prefix: Path,
) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    if not kill_present:
        blocks.append(_block("kill_file_absent", "LIVE_TRADING_DISABLED", source="isolation"))
    if not dry_run:
        blocks.append(_block("not_dry_run", str(output_db), source="isolation"))
    if output_db.resolve() == source_db.resolve():
        blocks.append(_block("output_db_not_isolated", str(output_db), source="isolation"))
    if isolated_prefix.resolve() not in output_db.resolve().parents and output_db.resolve() != isolated_prefix.resolve():
        blocks.append(_block("output_db_outside_isolation_dir", str(output_db), source="isolation"))
    return blocks


def load_emitted_signals(output_db: Path) -> list[dict[str, Any]]:
    if not output_db.is_file():
        return []
    conn = _connect(output_db)
    if conn is None:
        return []
    try:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if "decision_log" not in tables:
            return []
        rows = conn.execute("SELECT raw_json FROM decision_log").fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    signals: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["raw_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            extras = payload.get("extras")
            if isinstance(extras, dict):
                merged = dict(extras)
                merged.setdefault("strategy_id", payload.get("strategy_id"))
                merged.setdefault("action", payload.get("action"))
                merged.setdefault("size_pct", payload.get("size_pct"))
                signals.append(merged)
            else:
                signals.append(payload)
    return signals


def build_report(
    *,
    env: Mapping[str, str],
    root: Path,
    pack: Mapping[str, Any] | None,
    signals: list[Mapping[str, Any]],
    kill_present: bool,
    dry_run: bool,
    output_db: Path,
    source_db: Path,
    isolated_prefix: Path,
    engine_exit_code: int | None,
) -> dict[str, Any]:
    blocks: list[dict[str, str]] = []
    observations: list[dict[str, str]] = []
    blocks.extend(profile_blocks(env))
    blocks.extend(local_store_blocks(env, root=root))
    if pack is not None:
        pack_blocks, pack_observations = pack_clock_blocks(pack)
        blocks.extend(pack_blocks)
        observations.extend(pack_observations)
    blocks.extend(
        isolation_blocks(
            kill_present=kill_present,
            dry_run=dry_run,
            output_db=output_db,
            source_db=source_db,
            isolated_prefix=isolated_prefix,
        )
    )
    kelly_off = str(env.get("USE_OPTIMAL_KELLY_MATRIX", "0")).strip() in {"0", "false", "no", "off"}
    perp_off = str(env.get("ENABLE_PERP_SHORT_ENGINE", "0")).strip() in {"0", "false", "no", "off"}
    if not perp_off:
        blocks.append(_block("perp_enabled", "ENABLE_PERP_SHORT_ENGINE", source="profile"))
    if not signals:
        blocks.append(_block("no_replayable_signal", str(output_db), source="isolation"))
    for signal in signals:
        blocks.extend(signal_blocks(signal, kelly_off=kelly_off))
        if signal.get("require_perp_beta_hedge"):
            observations.append(
                _block(
                    "perp_hedge_not_executed",
                    "require_perp_beta_hedge is a stamp; ENABLE_PERP_SHORT_ENGINE stays off",
                    source="signal",
                )
            )
    observations.append(
        _block(
            "position_pending_ruling",
            "REGIME_V2_POSITION_PCT=0.24 is the 2026-11-12 baseline, not a new fraction",
            source="profile",
        )
    )
    if engine_exit_code not in (None, 0):
        blocks.append(_block("engine_exit", str(engine_exit_code), source="isolation"))
    # A dry-run that only proved the stores are missing must not look successful.
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in blocks:
        identity = (item["code"], item["detail"], item["source"])
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(item)
    return {
        "cutover_allowed": not unique,
        "blocks": unique,
        "observations": observations,
        "signal_count": len(signals),
        "engine_exit_code": engine_exit_code,
        "position_pct_pending": env.get("REGIME_V2_POSITION_PCT"),
        "kelly": env.get("USE_OPTIMAL_KELLY_MATRIX"),
        "perp": env.get("ENABLE_PERP_SHORT_ENGINE"),
        "trailing_ratchet": env.get("BINANCE_SPOT_TRAILING_RATCHET"),
        "max_hold_sec": env.get("BINANCE_SPOT_MAX_HOLD_SEC"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a regime v2 cutover report. No orders.")
    parser.add_argument("--profile", type=Path, default=profile_path())
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--output-db", type=Path, required=True)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--isolated-dir", type=Path, required=True)
    parser.add_argument("--kill-file", type=Path, required=True)
    parser.add_argument("--feature-snapshot", type=Path, default=None)
    parser.add_argument("--engine-exit-code", type=int, default=0)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    env = parse_env_file(args.profile)
    pack = None
    if args.feature_snapshot is not None:
        pack = json.loads(args.feature_snapshot.read_text(encoding="utf-8"))
    report = build_report(
        env=env,
        root=args.root,
        pack=pack,
        signals=load_emitted_signals(args.output_db),
        kill_present=args.kill_file.is_file(),
        dry_run=True,
        output_db=args.output_db,
        source_db=args.source_db,
        isolated_prefix=args.isolated_dir,
        engine_exit_code=args.engine_exit_code,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"cutover_allowed": report["cutover_allowed"], "blocks": len(report["blocks"])}))
    return 0 if report["cutover_allowed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
