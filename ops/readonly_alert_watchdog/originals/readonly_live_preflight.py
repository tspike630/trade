#!/usr/bin/env python3
"""Read-Only Live Preflight & Red-Light Disclosure Inspector for tspike630/trade.

Guarantees:
1. Pure Python 3 stdlib (zero third-party dependencies, zero network sockets).
2. Every SQLite database is opened strictly via ``file:...?mode=ro`` URI with
   ``PRAGMA query_only = ON``. Missing database files are NEVER created.
3. Never modifies code files, ledgers, cursors, environment variables, or processes.
4. Inspects all 4 silent-failure surfaces that previously went unreported:
   - Surface A (Codebase & DEPLOY_SHA Drift): ``decision_engine/DEPLOY_SHA`` vs
     ``tree_sha256`` + dual-state SHA256 verification across all 24 runtime-source
     files and ``decision_engine/core/cityu_institutional_alpha.py`` (distinguishing
     clean ``LIVE_PRE_PR224``, clean ``V3_PATCHED``, ``MIXED_PARTIAL_INSTALL``, and
     ``UNTRACKED_DRIFT``).
   - Surface B (8 Required Clocks & Silent Feed Lockouts): LSEG ``NVDA.O``/``SMH.O``
     5m bars (30m gate when NYSE cash open + off-hours staleness visibility),
     Deribit ``BTC`` options gamma ``ts_ms`` (3600s gate, ignoring ``collected_at_ms``),
     Binance indicators/5m-klines/1h-volume_z/perp/spot ``obi_top5_source_ts_ms``,
     whitelist ``news_events.fetched_at`` (90m gate), CFTC COT ``due_cftc_asof``
     (Friday 15:30 America/New_York), and recent ``collector_fetch_errors``.
   - Surface C (Spot & Cross-Margin Execution Ledger Hazards): ``protection_unknown``,
     ``protection_rejected_naked``, ``oco_failed``, ``oco_unknown``, ``fee_unverified``,
     ``entry_unknown``, ``fill_unconfirmed``, ``not_flat``, stale ``reserved`` (>15s),
     started/unknown ``operations``, cursor lag, and unacknowledged dust residuals.
   - Surface D (Decision Funnel & Silent Zero-Emit Lockout): ``decision_log`` scan
     activity, rejection reason Top-5, and consecutive 5m bar zero-emit detection.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# --- Verified Production SHA256 Baselines (tspike630/trade PR #224 + T535 v3) ---
# Maps repo-relative POSIX path -> (live_pre_pr224_sha256, v3_patched_sha256)
KNOWN_FILE_HASHES: dict[str, tuple[str | None, str]] = {
    "decision_engine/core/cross_margin_emit_align.py": (
        "4f67bc0696701e5b57d86de06662e6fc11e752de432728285f884ba563b962e2",
        "32b7afe26f6fb2ca729e05d2d89104f4533feb393d771670dbee2b2a53325d10",
    ),
    "decision_engine/core/cross_margin_price_contract.py": (
        "35abe10c75de09f832fe447966348ca92f15bbff19d1e84fb0212f1e016b3b87",
        "35abe10c75de09f832fe447966348ca92f15bbff19d1e84fb0212f1e016b3b87",
    ),
    "decision_engine/core/engine.py": (
        "7d15725a1267b373ce41917caf3d7a82460b10f13e3179e0893b4cc68e56baaf",
        "6e3394e2835ce1eb0f6887177714165e63c4e81671ec529759b8345ec47feaf0",
    ),
    "decision_engine/core/regime_v2_policy.py": (
        "77f756b79bd3019a9dfa89a3193a4dcae9476f066755a6aa45bb35172e9545f0",
        "9244b3d98d981f6b471569e04b83f47fda4eea6f9d2ec555fdf9598c64656ce3",
    ),
    "decision_engine/core/token_cooldown.py": (
        "0498f3a60a97e6122bca25d335f8db5e1323f3ee42e6ef2e1a5326805c15df42",
        "0498f3a60a97e6122bca25d335f8db5e1323f3ee42e6ef2e1a5326805c15df42",
    ),
    "decision_engine/core/volume_z_1h.py": (
        "e71ee75820035ca3d3c717ce732c6c7492726b93e638550a2aa3368d4d1af1c7",
        "e71ee75820035ca3d3c717ce732c6c7492726b93e638550a2aa3368d4d1af1c7",
    ),
    "decision_engine/core/cityu_institutional_alpha.py": (
        "0274f9e23c8d95cceef2d2cff1fbc1f668481a76cd8e7dd362a99fb13d301cde",
        "1a535542f16fa59ac8af9243d09dd79111c5198438757b137d2fdf247545a038",
    ),
    "decision_engine/run.py": (
        "ff637b75eb161677cec79007db76f11b2bb736c2c88d14cf4e6fb769a8c4b89f",
        "ff637b75eb161677cec79007db76f11b2bb736c2c88d14cf4e6fb769a8c4b89f",
    ),
    "decision_engine/strategies/regime_cross_margin.py": (
        "9aba09c6f50ee29351515b326327bc5ea0974d3e60a8098b2142553cd92ea9ec",
        "4a8c8500b8de2695539fc0a6b8573caf522f79eca69c1685d563069a8bf4ac29",
    ),
    "decision_engine/strategies/tech_boll_fade.py": (
        "50f733fe91c885a7a979ed499aafb8e9c314afe197edd1f8859c6b3785426708",
        "50f733fe91c885a7a979ed499aafb8e9c314afe197edd1f8859c6b3785426708",
    ),
    "execution/gateway.py": (
        "46e417cf9f3415cdf92e7ae3e15154acca5bf25a8713f90aada9e831d0b9c198",
        "d7c58a5c41bb57f9e0eae894033042dbe5e9c6a332212845e2ae1e71a86bb98f",
    ),
    "execution/margin_cross/entry.py": (
        "ed678103dcb217ccc44e487dbfd14486a0a84d833c969f9ac2f03575c7f0a7ca",
        "2c5d7b6609d775bd1f1379e901d52bda34b6c4b72c51387cff4b06bbe9e598f6",
    ),
    "execution/margin_cross/money.py": (
        "050952b6116537a9f637e6f3f4eb75a0f80b19d8429c6b00392e35c6c3ccb090",
        "050952b6116537a9f637e6f3f4eb75a0f80b19d8429c6b00392e35c6c3ccb090",
    ),
    "execution/margin_cross/policy.py": (
        "8ff1cfc597978a08727185a40cb8a4455e9ce9709a7b2c3a3ad766fb29809505",
        "8ff1cfc597978a08727185a40cb8a4455e9ce9709a7b2c3a3ad766fb29809505",
    ),
    "execution/margin_cross/proposal.py": (
        "23065c858e607aa89b450262b52bc7a3d1b26885ada28a544305df9df81a2cdd"
        if False
        else "23065c858e607aa89b450262b52bc7a3d1b26885ada28a544305df9df81d2cdd",
        "23065c858e607aa89b450262b52bc7a3d1b26885ada28a544305df9df81d2cdd",
    ),
    "execution/margin_cross/protection.py": (
        "d3b9c0bae62b07e9ae24b0e5dec2311afa4d9b81a665f2492534a60582f7e85f",
        "d3b9c0bae62b07e9ae24b0e5dec2311afa4d9b81a665f2492534a60582f7e85f",
    ),
    "execution/margin_cross/strategy_gate.py": (
        "a678cda995a37bfbb0e0ee9efd963e9297f443047efcfb1061f456dfea5354f8",
        "276b376c14a38a6d8e7bc88862e6fee4beb7c79e66cefc0c011ea21b7832aca3",
    ),
    "execution/margin_cross/track.py": (
        "b63c3c3bad75a47c5235ead9aec41887fce24b45e95ee6a89af64cc6681d87c5",
        "19f3d7445adeae671bcfeb6e900fb2e92a97050d0ae7efc7eb6488c2be4c5d09",
    ),
    "execution/run.py": (
        "b799de9f44de057738fc0fe2288a4f2e14cc0c8a43234b17a82d434c0d3b3207",
        "b799de9f44de057738fc0fe2288a4f2e14cc0c8a43234b17a82d434c0d3b3207",
    ),
    "indicators/binance_spot.py": (
        "2bdc25b845bd36ba876dc7c0d82594f1c5d44f4ac28bd17bc1389f76a48c323c",
        "2bdc25b845bd36ba876dc7c0d82594f1c5d44f4ac28bd17bc1389f76a48c323c",
    ),
    "indicators/binance_spot_1h_klines.py": (
        "f3907b07e40c21f16dc23191aeb958fe8c8e780ee9c98f3e2a77f68a540dbc71",
        "f3907b07e40c21f16dc23191aeb958fe8c8e780ee9c98f3e2a77f68a540dbc71",
    ),
    "indicators/binance_universe_quote.py": (
        "70b05fc271c80ae784b927829e833d4ba16a650b42cfb9b7a4ffe418528f6812",
        "70b05fc271c80ae784b927829e833d4ba16a650b42cfb9b7a4ffe418528f6812",
    ),
    "scripts/fetch_public_derivatives_and_counterparty.py": (
        "c8a62e4083ee537c008bce2cbc075be7c718b55d4d7997760228657d959b2407",
        "c8a62e4083ee537c008bce2cbc075be7c718b55d4d7997760228657d959b2407",
    ),
    "scripts/run_binance_spot_loop.sh": (
        "eb9c4cf8b0899c76c946404d3e3d7a7fa702d404d8e3684c4287b559634cd0c3",
        "eb9c4cf8b0899c76c946404d3e3d7a7fa702d404d8e3684c4287b559634cd0c3",
    ),
    "scripts/run_binance_universe_quote_loop.sh": (
        "3fe4631de750474387445f580f17eeed9a032d4faa5b8b8e9354329c449ae0c7",
        "3fe4631de750474387445f580f17eeed9a032d4faa5b8b8e9354329c449ae0c7",
    ),
}

PATCHED_IN_V3_FILES = frozenset(
    rel
    for rel, (base_h, v3_h) in KNOWN_FILE_HASHES.items()
    if base_h is not None and base_h != v3_h
)

# --- Freshness & Gate Thresholds (Must Match Live Production Contracts) ---
MAX_CROSS_ASSET_AGE_MS = 30 * 60 * 1000  # 30 minutes on live
INDICATORS_FRESH_MS = 3600 * 1000  # 3600 seconds
DERIBIT_EARLY_WARN_MS = 2700 * 1000  # 45m early warning before 3600s lockout
KLINE_5M_FRESH_MS = 900 * 1000  # 15 minutes
NEWS_FRESH_MS = 90 * 60 * 1000  # 90 minutes whitelist news
RESERVED_STALE_MS = 15_000  # 15 seconds cross-margin reservation grace
SILENT_LOCKOUT_WINDOW_MS = 10 * 60 * 1000  # 2 consecutive 5m bars

# Verified NYSE 2025-2028 Calendar Constants (ICE/NYSE Official Releases & Rule 7.2)
_NYSE_SUPPORTED_YEARS = frozenset({2025, 2026, 2027, 2028})
_NYSE_SPECIAL_CLOSURES = frozenset({date(2025, 1, 9)})
_NYSE_EARLY_CLOSES_1300_ET = frozenset(
    {
        date(2025, 7, 3),
        date(2025, 11, 28),
        date(2025, 12, 24),
        date(2026, 11, 27),
        date(2026, 12, 24),
        date(2027, 11, 26),
        date(2028, 7, 3),
        date(2028, 11, 24),
    }
)

_CM_CLEAN_TERMINAL_STATES = frozenset(
    {
        "clean_flat",
        "reservation_released",
        "protection_failed_flattened",
        "entry_rejected_unwound",
        "risk_dropped_unwound",
        "emergency_unwound",
        "locked",
        "borrow_rejected",
        "entry_rejected",
        "otoco_not_submitted",
        "live_network_disabled",
        "emergency_stop_blocks_new_risk",
        "intent_conflict",
    }
)
_CM_HEALTHY_WORKING_STATES = frozenset(
    {"protected", "protection_still_working", "entry_resting", "accepted"}
)
_CM_CRITICAL_STATES = frozenset(
    {
        "protection_unknown",
        "protection_rejected_naked",
        "not_flat",
        "residual_liability_after_unwind",
        "repay_unknown",
        "repay_unconfirmed",
        "borrow_unknown",
        "entry_unknown",
        "fill_unconfirmed",
        "fill_mismatch",
        "partial_cancel_unknown",
        "exit_partially_filled_halted",
        "side_effect_violation",
        "protection_blocked",
    }
)

_SPOT_CRITICAL_STATUSES = frozenset(
    {
        "oco_unknown",
        "oco_failed",
        "qty_unconfirmed",
        "maker_cancel_unknown",
        "time_stop_unknown",
    }
)


@dataclass(frozen=True)
class Finding:
    severity: str  # "RED", "YELLOW", "GREEN"
    surface: str  # "code_integrity", "data_clocks", "execution_ledgers", "signal_funnel"
    code: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class PreflightReport:
    checked_at_utc: str
    now_ms: int
    repo_root: str
    overall_status: str  # "RED", "YELLOW", "GREEN"
    red_count: int
    yellow_count: int
    green_count: int
    code_state: str
    nyse_cash_open: bool | None
    expected_cot_asof: str
    findings: list[Finding]
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at_utc": self.checked_at_utc,
            "now_ms": self.now_ms,
            "repo_root": self.repo_root,
            "overall_status": self.overall_status,
            "red_count": self.red_count,
            "yellow_count": self.yellow_count,
            "green_count": self.green_count,
            "code_state": self.code_state,
            "nyse_cash_open": self.nyse_cash_open,
            "expected_cot_asof": self.expected_cot_asof,
            "findings": [asdict(f) for f in self.findings],
            "metrics": self.metrics,
        }


# --- Pure Read-Only Helpers ---

def connect_readonly_sqlite(path: Path) -> sqlite3.Connection | None:
    """Open an existing SQLite DB in strict URI read-only mode + query_only=ON.

    Never creates a missing file or journal.
    """
    if not path.is_file():
        return None
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=3.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def present_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    except sqlite3.Error:
        return set()


def sha256_file(path: Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def compute_decision_tree_sha256(pkg_dir: Path) -> str:
    """Exact replica of ``decision_engine.run.tree_sha256``."""
    files: list[tuple[str, Path]] = []
    for f in pkg_dir.rglob("*.py"):
        rel = f.relative_to(pkg_dir)
        if any(part in ("__pycache__", "tests") for part in rel.parts[:-1]):
            continue
        files.append((rel.as_posix(), f))
    h = hashlib.sha256()
    for rel, f in sorted(files):
        h.update(rel.encode("utf-8") + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def parse_any_ts_ms(raw: object) -> tuple[int | None, str | None]:
    if raw in (None, "") or isinstance(raw, bool):
        return None, None
    if isinstance(raw, (int, float)):
        if not math.isfinite(float(raw)):
            return None, "non_finite"
        n = int(raw)
        if 1_000_000_000 <= n < 100_000_000_000:
            return None, "seconds_unit_mismatch"
        return n, None
    text = str(raw).strip()
    if not text:
        return None, None
    if text.isdigit():
        n = int(text)
        if 1_000_000_000 <= n < 100_000_000_000:
            return None, "seconds_unit_mismatch"
        return n, None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000), None
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000), None
    except ValueError:
        return None, "unparseable"


def fmt_ms_utc(ts_ms: int | None) -> str:
    if ts_ms is None:
        return "NULL"
    try:
        return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (OSError, OverflowError, ValueError):
        return str(ts_ms)


def fmt_age_human(age_ms: int | None) -> str:
    if age_ms is None:
        return "unknown"
    sec = age_ms / 1000.0
    if abs(sec) < 120:
        return f"{sec:.0f}s"
    if abs(sec) < 7200:
        return f"{sec / 60.0:.1f}m"
    if abs(sec) < 172800:
        return f"{sec / 3600.0:.1f}h"
    return f"{sec / 86400.0:.1f}d"


# --- Verified Calendar & CFTC COT Clocks ---

def _easter_sunday(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed_weekday(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    shift = (weekday - first.weekday()) % 7
    return first + timedelta(days=shift + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, 12, 31) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)
    shift = (last.weekday() - weekday) % 7
    return last - timedelta(days=shift)


def nyse_full_holiday(day: date) -> bool:
    year = day.year
    holidays = {
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed_weekday(date(year, 6, 19)),
        _observed_weekday(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed_weekday(date(year, 12, 25)),
    }
    new_year = date(year, 1, 1)
    if new_year.weekday() == 6:
        holidays.add(date(year, 1, 2))
    elif new_year.weekday() < 5:
        holidays.add(new_year)
    return day in holidays or day in _NYSE_SPECIAL_CLOSURES


def eval_nyse_cash_market_open(now_ms: int) -> bool | None:
    try:
        local = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).astimezone(
            ZoneInfo("America/New_York")
        )
    except (OSError, OverflowError, ValueError, ZoneInfoNotFoundError):
        return None
    if local.year not in _NYSE_SUPPORTED_YEARS:
        return None
    day = local.date()
    if local.weekday() >= 5 or nyse_full_holiday(day):
        return False
    close_min = 13 * 60 if day in _NYSE_EARLY_CLOSES_1300_ET else 16 * 60
    cur_min = local.hour * 60 + local.minute
    return 9 * 60 + 30 <= cur_min < close_min


def eval_due_cftc_asof(now_ms: int) -> date:
    now_utc = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    asof = now_utc.date()
    days_since_tue = (asof.weekday() - 1) % 7
    latest_tue = asof - timedelta(days=days_since_tue)
    fri = latest_tue + timedelta(days=3)
    pub_utc = datetime(
        fri.year, fri.month, fri.day, 15, 30, tzinfo=ZoneInfo("America/New_York")
    ).astimezone(timezone.utc)
    return latest_tue if now_utc >= pub_utc else (latest_tue - timedelta(days=7))


# --- Surface A: Codebase & DEPLOY_SHA Drift ---

def inspect_code_integrity(repo_root: Path) -> tuple[str, list[Finding], dict[str, Any]]:
    findings: list[Finding] = []
    # Support inspecting either the live root (/workspace/dexscreener-crawler)
    # or the packed snapshot root (/Users/yangyuxuan/Downloads/runtime-source).
    code_root = repo_root / "source" if (repo_root / "source" / "decision_engine").is_dir() else repo_root

    live_matches: list[str] = []
    v3_only_matches: list[str] = []
    unchanged_matches: list[str] = []
    drifted_files: list[dict[str, Any]] = []
    missing_files: list[str] = []

    for rel_path, (live_sha, v3_sha) in sorted(KNOWN_FILE_HASHES.items()):
        fpath = code_root / rel_path
        if not fpath.is_file():
            missing_files.append(rel_path)
            continue
        size_b, actual_sha = sha256_file(fpath)
        if live_sha == v3_sha and actual_sha == v3_sha:
            unchanged_matches.append(rel_path)
        elif actual_sha == v3_sha and rel_path in PATCHED_IN_V3_FILES:
            v3_only_matches.append(rel_path)
        elif live_sha is not None and actual_sha == live_sha:
            live_matches.append(rel_path)
        else:
            drifted_files.append(
                {
                    "file": rel_path,
                    "bytes": size_b,
                    "actual_sha256": actual_sha,
                    "expected_live_base": live_sha,
                    "expected_v3_target": v3_sha,
                }
            )

    if drifted_files:
        code_state = "UNTRACKED_DRIFT"
        findings.append(
            Finding(
                severity="RED",
                surface="code_integrity",
                code="code_sha256_drift",
                summary=(
                    f"检测到 {len(drifted_files)} 个核心代码文件 SHA256 既不匹配现网基线也不匹配 v3 目标包"
                ),
                detail={"drifted": drifted_files[:10]},
            )
        )
    elif v3_only_matches and live_matches:
        code_state = "MIXED_PARTIAL_INSTALL"
        findings.append(
            Finding(
                severity="RED",
                surface="code_integrity",
                code="partial_patch_mixed_state",
                summary=(
                    f"代码树处于半安装撕裂态：已装 v3 文件 {len(v3_only_matches)} 个，仍停留在旧基线 {len(live_matches)} 个"
                ),
                detail={
                    "v3_installed": v3_only_matches,
                    "still_pre_patch": live_matches,
                },
            )
        )
    elif v3_only_matches and not live_matches:
        code_state = "V3_PATCHED"
    elif live_matches and not v3_only_matches:
        code_state = "LIVE_PRE_PR224"
        findings.append(
            Finding(
                severity="YELLOW",
                surface="code_integrity",
                code="code_still_pre_pr224_base",
                summary=(
                    f"现网代码树仍处于 PR #224 装盘前基线（{len(live_matches)} 个待装补丁文件尚未切换至 v3）"
                ),
                detail={"pending_v3_files": live_matches},
            )
        )
    else:
        code_state = "UNKNOWN_EMPTY"

    if missing_files:
        # In runtime-source snapshot, cityu_institutional_alpha.py was omitted from archive.
        sev = (
            "YELLOW"
            if missing_files == ["decision_engine/core/cityu_institutional_alpha.py"]
            else "RED"
        )
        findings.append(
            Finding(
                severity=sev,
                surface="code_integrity",
                code="monitored_code_files_missing",
                summary=f"监控清单中有 {len(missing_files)} 个代码文件在当前目录缺失：{', '.join(missing_files[:5])}",
                detail={"missing_files": missing_files},
            )
        )

    # Check decision_engine/DEPLOY_SHA vs runtime tree_sha256 (previously only a local log.warning!)
    deploy_sha_file = code_root / "decision_engine" / "DEPLOY_SHA"
    deploy_sha_info: dict[str, Any] = {"present": deploy_sha_file.is_file()}
    if deploy_sha_file.is_file() and (code_root / "decision_engine").is_dir():
        lines = [
            ln.strip()
            for ln in deploy_sha_file.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        git_sha = lines[0].split()[0] if lines else "unknown"
        expected_tree = lines[1].split()[0].lower() if len(lines) > 1 else None
        actual_tree = compute_decision_tree_sha256(code_root / "decision_engine")
        deploy_sha_info.update(
            {
                "git_sha": git_sha,
                "expected_tree_sha256": expected_tree,
                "actual_tree_sha256": actual_tree,
            }
        )
        if (
            expected_tree
            and not actual_tree.startswith(expected_tree)
            and expected_tree != actual_tree
        ):
            findings.append(
                Finding(
                    severity="RED",
                    surface="code_integrity",
                    code="deploy_sha_tree_mismatch",
                    summary=(
                        f"DEPLOY_SHA 期望指纹 ({expected_tree[:12]}) 与磁盘实际 decision_engine 指纹 ({actual_tree[:12]}) 不一致"
                    ),
                    detail=deploy_sha_info,
                )
            )

    if not any(f.severity == "RED" for f in findings) and code_state == "V3_PATCHED":
        findings.append(
            Finding(
                severity="GREEN",
                surface="code_integrity",
                code="code_integrity_v3_verified",
                summary="全部核心代码文件 SHA256 与 PR224 v3 目标哈希完全一致，无漂移",
                detail={"verified_count": len(v3_only_matches) + len(unchanged_matches)},
            )
        )

    metrics = {
        "code_root": str(code_root),
        "code_state": code_state,
        "unchanged_verified": len(unchanged_matches),
        "v3_patched_verified": len(v3_only_matches),
        "live_pre_patch_count": len(live_matches),
        "drifted_count": len(drifted_files),
        "missing_count": len(missing_files),
        "deploy_sha": deploy_sha_info,
    }
    return code_state, findings, metrics


# --- Surface B: 8 Required Clocks & Feed Freshness ---

def inspect_data_clocks(
    *,
    now_ms: int,
    nyse_open: bool | None,
    expected_cot: str,
    main_db: Path,
    deriv_db: Path,
    lseg_db: Path,
    cityu_db: Path,
    require_dbs: bool,
) -> tuple[list[Finding], dict[str, Any]]:
    findings: list[Finding] = []
    metrics: dict[str, Any] = {}

    # 1. LSEG lseg_macro.db (NVDA.O / SMH.O / BTCc1 / BTCc2)
    lconn = connect_readonly_sqlite(lseg_db)
    if lconn is None:
        if require_dbs:
            findings.append(
                Finding(
                    severity="RED" if nyse_open is not False else "YELLOW",
                    surface="data_clocks",
                    code="lseg_db_absent",
                    summary=f"LSEG 跨资产库不存在：{lseg_db}",
                    detail={"path": str(lseg_db), "nyse_cash_open": nyse_open},
                )
            )
    else:
        try:
            lseg_info: dict[str, Any] = {"path": str(lseg_db)}
            if table_exists(lconn, "cross_asset_bars"):
                all_r = lconn.execute(
                    "SELECT MAX(utc_ms) AS m FROM cross_asset_bars WHERE interval = '5min' AND utc_ms <= ?",
                    (now_ms,),
                ).fetchone()
                global_5m_ms = int(all_r["m"]) if all_r and all_r["m"] is not None else None
                lseg_info["global_5m_latest_utc"] = fmt_ms_utc(global_5m_ms)
                lseg_info["global_5m_age_ms"] = (
                    now_ms - global_5m_ms if global_5m_ms is not None else None
                )
                stale_rics: list[str] = []
                ric_details: dict[str, Any] = {}
                for ric in ("NVDA.O", "SMH.O"):
                    rows = lconn.execute(
                        """
                        SELECT utc_ms, close FROM cross_asset_bars
                        WHERE ric = ? AND interval = '5min' AND utc_ms <= ? AND close IS NOT NULL
                        ORDER BY utc_ms DESC LIMIT 13
                        """,
                        (ric, now_ms),
                    ).fetchall()
                    latest_ms = int(rows[0]["utc_ms"]) if rows else None
                    age_ms = (now_ms - latest_ms) if latest_ms is not None else None
                    ric_details[ric] = {
                        "bars_count": len(rows),
                        "latest_utc": fmt_ms_utc(latest_ms),
                        "age_human": fmt_age_human(age_ms),
                    }
                    if len(rows) < 13 or latest_ms is None or age_ms is None or age_ms > MAX_CROSS_ASSET_AGE_MS:
                        stale_rics.append(
                            f"{ric}({fmt_ms_utc(latest_ms)}, bars={len(rows)}, age={fmt_age_human(age_ms)})"
                        )
                lseg_info["rics"] = ric_details
                if stale_rics:
                    if nyse_open is False:
                        findings.append(
                            Finding(
                                severity="YELLOW",
                                surface="data_clocks",
                                code="lseg_nvda_smh_stale_offhours",
                                summary=(
                                    f"当前美股休市（不触发实盘闭锁），但 LSEG 5m 源仍停留在旧快照：{'; '.join(stale_rics)}；开盘前不补数将闭锁前 7 项策略"
                                ),
                                detail=lseg_info,
                            )
                        )
                    else:
                        findings.append(
                            Finding(
                                severity="RED",
                                surface="data_clocks",
                                code="lseg_nvda_smh_stale_market_open",
                                summary=(
                                    f"美股常规盘中（或日历未知 fail-closed）LSEG 5m 超出 30 分钟门禁，前 7 项策略已闭锁：{'; '.join(stale_rics)}"
                                ),
                                detail=lseg_info,
                            )
                        )
                else:
                    findings.append(
                        Finding(
                            severity="GREEN",
                            surface="data_clocks",
                            code="lseg_nvda_smh_fresh",
                            summary="LSEG NVDA.O / SMH.O 5m K线在 30 分钟门禁内且满 13 根",
                            detail=lseg_info,
                        )
                    )
            metrics["lseg"] = lseg_info
        finally:
            lconn.close()

    # 2. CityU Institutional DB (CFTC COT Friday 15:30 ET due_cftc_asof)
    cconn = connect_readonly_sqlite(cityu_db)
    if cconn is None:
        if require_dbs:
            findings.append(
                Finding(
                    severity="RED",
                    surface="data_clocks",
                    code="cityu_db_absent",
                    summary=f"CityU 机构因子库不存在：{cityu_db}",
                    detail={"path": str(cityu_db)},
                )
            )
    else:
        try:
            cot_info: dict[str, Any] = {"path": str(cityu_db), "expected_due_asof": expected_cot}
            if table_exists(cconn, "lseg_cftc_cot_positioning"):
                row = cconn.execute(
                    """
                    SELECT MAX(substr(date, 1, 10)) AS max_d
                    FROM lseg_cftc_cot_positioning
                    WHERE symbol = 'BTC' AND open_interest != '' AND substr(date, 1, 10) <= ?
                    """,
                    (expected_cot,),
                ).fetchone()
                any_row = cconn.execute(
                    "SELECT MAX(substr(date, 1, 10)) AS max_any FROM lseg_cftc_cot_positioning WHERE symbol = 'BTC'"
                ).fetchone()
                max_d = str(row["max_d"]) if row and row["max_d"] else None
                max_any = str(any_row["max_any"]) if any_row and any_row["max_any"] else None
                cot_info["latest_le_due"] = max_d
                cot_info["latest_any"] = max_any
                today_iso = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).date().isoformat()
                if max_any and max_any > today_iso:
                    findings.append(
                        Finding(
                            severity="RED",
                            surface="data_clocks",
                            code="cot_asof_future_timestamp",
                            summary=f"CFTC COT 存在未来日期行 ({max_any} 大于今日 UTC {today_iso})，触发发射前丢弃",
                            detail=cot_info,
                        )
                    )
                elif max_d != expected_cot:
                    findings.append(
                        Finding(
                            severity="RED",
                            surface="data_clocks",
                            code="cot_publication_not_current",
                            summary=(
                                f"CFTC COT 期次落后：库内最新有效星期二为 {max_d or 'NULL'}，按美东周五 15:30 规则应为 {expected_cot}"
                            ),
                            detail=cot_info,
                        )
                    )
                else:
                    findings.append(
                        Finding(
                            severity="GREEN",
                            surface="data_clocks",
                            code="cot_publication_current",
                            summary=f"CFTC COT 期次对齐当前应付周期 ({expected_cot})",
                            detail=cot_info,
                        )
                    )
            metrics["cityu_cot"] = cot_info
        finally:
            cconn.close()

    # 3. Public Derivatives DB (Deribit, Perp, Spot OBI Top-5, Collector Errors)
    dconn = connect_readonly_sqlite(deriv_db)
    if dconn is None:
        if require_dbs:
            findings.append(
                Finding(
                    severity="RED",
                    surface="data_clocks",
                    code="public_derivatives_db_absent",
                    summary=f"公开衍生品与盘口库不存在：{deriv_db}",
                    detail={"path": str(deriv_db)},
                )
            )
    else:
        try:
            deriv_info: dict[str, Any] = {"path": str(deriv_db)}
            # 3a. Deribit BTC options gamma (strictly ts_ms, never collected_at_ms!)
            if table_exists(dconn, "deribit_options_gamma"):
                d_cols = present_columns(dconn, "deribit_options_gamma")
                col_expr = "ts_ms, collected_at_ms" if "collected_at_ms" in d_cols else "ts_ms, NULL AS collected_at_ms"
                d_row = dconn.execute(
                    f"SELECT {col_expr} FROM deribit_options_gamma WHERE currency = 'BTC' ORDER BY ts_ms DESC LIMIT 1"
                ).fetchone()
                d_ts = int(d_row["ts_ms"]) if d_row and d_row["ts_ms"] is not None else None
                d_col_ts = int(d_row["collected_at_ms"]) if d_row and d_row["collected_at_ms"] is not None else None
                d_age = (now_ms - d_ts) if d_ts is not None else None
                deriv_info["deribit_btc"] = {
                    "ts_ms": d_ts,
                    "ts_utc": fmt_ms_utc(d_ts),
                    "collected_utc": fmt_ms_utc(d_col_ts),
                    "age_human": fmt_age_human(d_age),
                }
                if d_ts is None or d_age is None or d_age > INDICATORS_FRESH_MS or d_age < -120_000:
                    findings.append(
                        Finding(
                            severity="RED",
                            surface="data_clocks",
                            code="deribit_gamma_stale_or_invalid",
                            summary=(
                                f"Deribit BTC 期权观测钟异常或超 3600s 门禁（ts={fmt_ms_utc(d_ts)}, age={fmt_age_human(d_age)}, collected={fmt_ms_utc(d_col_ts)}）"
                            ),
                            detail=deriv_info["deribit_btc"],
                        )
                    )
                elif d_age >= DERIBIT_EARLY_WARN_MS:
                    findings.append(
                        Finding(
                            severity="YELLOW",
                            surface="data_clocks",
                            code="deribit_gamma_approaching_expiry",
                            summary=(
                                f"Deribit BTC 期权观测钟距 3600s 闭锁不足 15 分钟（age={fmt_age_human(d_age)}），请留意整点采样是否滞后"
                            ),
                            detail=deriv_info["deribit_btc"],
                        )
                    )
                else:
                    findings.append(
                        Finding(
                            severity="GREEN",
                            surface="data_clocks",
                            code="deribit_gamma_fresh",
                            summary=f"Deribit BTC 期权观测钟新鲜（age={fmt_age_human(d_age)}）",
                            detail=deriv_info["deribit_btc"],
                        )
                    )

            # 3b. Perp counterparty & Spot OBI top5 source clock
            for tbl, ts_col, code_prefix, label in (
                ("binance_perp_counterparty", "ts_ms", "perp_counterparty", "币安永续对手盘"),
                ("binance_spot_microstructure", "obi_top5_source_ts_ms", "obi_top5_source", "现货 Top5 盘口深度事件钟"),
            ):
                if table_exists(dconn, tbl):
                    cols = present_columns(dconn, tbl)
                    if ts_col not in cols:
                        findings.append(
                            Finding(
                                severity="RED",
                                surface="data_clocks",
                                code=f"{code_prefix}_column_missing",
                                summary=f"{label}缺少关键真源时钟列 {tbl}.{ts_col}",
                                detail={"table": tbl, "column": ts_col},
                            )
                        )
                        continue
                    r = dconn.execute(f"SELECT MAX({ts_col}) AS m FROM {tbl}").fetchone()
                    ts_v = int(r["m"]) if r and r["m"] is not None else None
                    age_v = (now_ms - ts_v) if ts_v is not None else None
                    deriv_info[code_prefix] = {"latest_utc": fmt_ms_utc(ts_v), "age_human": fmt_age_human(age_v)}
                    if ts_v is None or age_v is None or age_v > INDICATORS_FRESH_MS or age_v < -120_000:
                        findings.append(
                            Finding(
                                severity="RED",
                                surface="data_clocks",
                                code=f"{code_prefix}_stale",
                                summary=f"{label}超 3600s 门禁或为空（latest={fmt_ms_utc(ts_v)}, age={fmt_age_human(age_v)}）",
                                detail=deriv_info[code_prefix],
                            )
                        )

            # 3c. Collector fetch errors in last 30 minutes
            if table_exists(dconn, "collector_fetch_errors"):
                cutoff_30m = now_ms - 30 * 60 * 1000
                err_rows = dconn.execute(
                    """
                    SELECT category, COALESCE(error_code, message) AS err_key, COUNT(*) AS cnt
                    FROM collector_fetch_errors
                    WHERE collected_at_ms >= ? AND fails_round = 1
                    GROUP BY category, err_key
                    ORDER BY cnt DESC LIMIT 5
                    """,
                    (cutoff_30m,),
                ).fetchall()
                if err_rows:
                    top_errs = [
                        {"category": str(r["category"]), "error": str(r["err_key"]), "count": int(r["cnt"])}
                        for r in err_rows
                    ]
                    deriv_info["recent_collector_errors_30m"] = top_errs
                    findings.append(
                        Finding(
                            severity="YELLOW",
                            surface="data_clocks",
                            code="collector_fetch_errors_recent",
                            summary=(
                                "公开衍生品采集器近 30 分钟记录到失败轮次："
                                + ", ".join(f"{x['category']}:{x['error']}({x['count']})" for x in top_errs[:3])
                            ),
                            detail={"top_errors": top_errs},
                        )
                    )
            metrics["public_derivatives"] = deriv_info
        finally:
            dconn.close()

    # 4. Main DB (binance_indicators, binance_klines, news_events)
    mconn = connect_readonly_sqlite(main_db)
    if mconn is None:
        if require_dbs:
            findings.append(
                Finding(
                    severity="RED",
                    surface="data_clocks",
                    code="main_decision_db_absent",
                    summary=f"主库 dexscreener.db 不存在：{main_db}",
                    detail={"path": str(main_db)},
                )
            )
    else:
        try:
            main_info: dict[str, Any] = {"path": str(main_db)}
            if table_exists(mconn, "binance_indicators"):
                ind_cols = present_columns(mconn, "binance_indicators")
                if "tech_updated_at" in ind_cols:
                    ir = mconn.execute(
                        "SELECT MAX(tech_updated_at) AS max_tu, COUNT(*) AS cnt FROM binance_indicators"
                    ).fetchone()
                    tu_ms, tu_err = parse_any_ts_ms(ir["max_tu"] if ir else None)
                    tu_age = (now_ms - tu_ms) if tu_ms is not None else None
                    main_info["binance_indicators"] = {
                        "rows": int(ir["cnt"]) if ir else 0,
                        "max_tech_updated_at": ir["max_tu"] if ir else None,
                        "age_human": fmt_age_human(tu_age),
                    }
                    if tu_err or tu_ms is None or tu_age is None or tu_age > INDICATORS_FRESH_MS or tu_age < -120_000:
                        findings.append(
                            Finding(
                                severity="RED",
                                surface="data_clocks",
                                code="binance_indicators_stale",
                                summary=(
                                    f"binance_indicators 技术指标时钟异常或超 3600s（tech_updated_at={ir['max_tu'] if ir else None}, age={fmt_age_human(tu_age)}）"
                                ),
                                detail=main_info["binance_indicators"],
                            )
                        )
                if "kline_close_time_ms" in ind_cols:
                    kr = mconn.execute(
                        "SELECT MAX(kline_close_time_ms) AS max_kc FROM binance_indicators"
                    ).fetchone()
                    kc_ms, kc_err = parse_any_ts_ms(kr["max_kc"] if kr else None)
                    kc_age = (now_ms - kc_ms) if kc_ms is not None else None
                    main_info["indicator_kline_close"] = {
                        "latest_utc": fmt_ms_utc(kc_ms),
                        "age_human": fmt_age_human(kc_age),
                    }
                    if kc_err or kc_ms is None or kc_age is None or kc_age > KLINE_5M_FRESH_MS or kc_age < -120_000:
                        findings.append(
                            Finding(
                                severity="RED",
                                surface="data_clocks",
                                code="entry_5m_kline_stale",
                                summary=(
                                    f"5m K线收盘钟超 900s 门禁或单位异常（latest={fmt_ms_utc(kc_ms)}, age={fmt_age_human(kc_age)}）"
                                ),
                                detail=main_info["indicator_kline_close"],
                            )
                        )

            if table_exists(mconn, "news_events"):
                nr = mconn.execute(
                    "SELECT MAX(fetched_at) AS mf FROM news_events WHERE lower(source_tier) = 'whitelist'"
                ).fetchone()
                nf_ms, nf_err = parse_any_ts_ms(nr["mf"] if nr else None)
                nf_age = (now_ms - nf_ms) if nf_ms is not None else None
                main_info["whitelist_news"] = {
                    "max_fetched_at": nr["mf"] if nr else None,
                    "age_human": fmt_age_human(nf_age),
                }
                if nf_err or nf_ms is None or nf_age is None or nf_age > NEWS_FRESH_MS or nf_age < -120_000:
                    findings.append(
                        Finding(
                            severity="RED",
                            surface="data_clocks",
                            code="whitelist_news_stale",
                            summary=(
                                f"白名单新闻 fetched_at 超 90 分钟门禁或为空（latest={nr['mf'] if nr else None}, age={fmt_age_human(nf_age)}）"
                            ),
                            detail=main_info["whitelist_news"],
                        )
                    )
            metrics["main_clocks"] = main_info
        finally:
            mconn.close()

    return findings, metrics


# --- Surface C: Spot & Cross-Margin Execution Ledgers ---

def _safe_decimal(raw: object) -> Decimal:
    if raw in (None, "") or isinstance(raw, bool):
        return Decimal(0)
    try:
        val = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return Decimal(0)
    return val if val.is_finite() else Decimal(0)


def inspect_execution_ledgers(
    *,
    now_ms: int,
    main_db: Path,
    spot_ledgers: Sequence[Path],
    cross_margin_ledger: Path | None,
    ack_dust_intents: frozenset[str],
) -> tuple[list[Finding], dict[str, Any]]:
    findings: list[Finding] = []
    metrics: dict[str, Any] = {"spot_ledgers": {}, "cross_margin_ledger": None}

    max_decision_id = 0
    mconn = connect_readonly_sqlite(main_db)
    if mconn is not None:
        try:
            if table_exists(mconn, "decision_log"):
                r = mconn.execute("SELECT MAX(id) AS m FROM decision_log").fetchone()
                if r and r["m"] is not None:
                    max_decision_id = int(r["m"])
        finally:
            mconn.close()

    # 1. Spot Ledgers
    for sp_path in spot_ledgers:
        sconn = connect_readonly_sqlite(sp_path)
        if sconn is None:
            continue
        try:
            sp_summary: dict[str, Any] = {"path": str(sp_path)}
            if table_exists(sconn, "execution_cursor"):
                cr = sconn.execute(
                    "SELECT value, updated_at FROM execution_cursor WHERE key = 'last_processed_id'"
                ).fetchone()
                cursor_id = int(cr["value"]) if cr and str(cr["value"]).isdigit() else 0
                sp_summary["cursor_id"] = cursor_id
                sp_summary["cursor_updated_at"] = cr["updated_at"] if cr else None
                sp_summary["max_decision_id"] = max_decision_id

            if table_exists(sconn, "execution_log"):
                claimed_r = sconn.execute(
                    "SELECT COUNT(*) AS n FROM execution_log WHERE status = 'claimed'"
                ).fetchone()
                claimed_n = int(claimed_r["n"]) if claimed_r else 0
                sp_summary["claimed_inflight"] = claimed_n
                if claimed_n > 0:
                    findings.append(
                        Finding(
                            severity="YELLOW",
                            surface="execution_ledgers",
                            code=f"spot_claimed_inflight:{sp_path.name}",
                            summary=f"{sp_path.name} 有 {claimed_n} 条决策处于 claimed 未终态落账",
                            detail={"ledger": sp_path.name, "claimed_count": claimed_n},
                        )
                    )

            if table_exists(sconn, "binance_spot_positions"):
                pos_cols = present_columns(sconn, "binance_spot_positions")
                rows = sconn.execute("SELECT * FROM binance_spot_positions ORDER BY id ASC").fetchall()
                hazards: list[dict[str, Any]] = []
                dust_rows: list[dict[str, Any]] = []
                active_working = 0
                for raw in rows:
                    row = dict(raw)
                    st = str(row.get("status") or "").strip()
                    last_err = str(row.get("last_error") or "").strip()
                    unprot = _safe_decimal(row.get("unprotected_qty")) if "unprotected_qty" in pos_cols else Decimal(0)
                    res_qty = _safe_decimal(row.get("residual_qty")) if "residual_qty" in pos_cols else Decimal(0)
                    pos_tag = f"spot-{row.get('decision_log_id')}"

                    if st in _SPOT_CRITICAL_STATUSES or last_err == "fee_unverified" or unprot > 0:
                        hazards.append(
                            {
                                "id": row.get("id"),
                                "decision_log_id": row.get("decision_log_id"),
                                "symbol": row.get("symbol"),
                                "status": st,
                                "last_error": last_err,
                                "unprotected_qty": str(unprot),
                            }
                        )
                    elif res_qty > 0 and pos_tag not in ack_dust_intents:
                        dust_rows.append(
                            {
                                "decision_log_id": row.get("decision_log_id"),
                                "symbol": row.get("symbol"),
                                "status": st,
                                "residual_qty": str(res_qty),
                            }
                        )
                    elif st not in {"closed", "oco_filled", "closed_canceled"}:
                        active_working += 1

                sp_summary["active_working"] = active_working
                sp_summary["hazards"] = hazards
                sp_summary["unacked_dust"] = dust_rows
                if hazards:
                    findings.append(
                        Finding(
                            severity="RED",
                            surface="execution_ledgers",
                            code=f"spot_unprotected_or_unknown:{sp_path.name}",
                            summary=(
                                f"现货账本 {sp_path.name} 存在 {len(hazards)} 笔保护不明/手续费未验/裸露头寸："
                                + ", ".join(f"{h['symbol']}({h['status']}/{h['last_error'] or '-'})" for h in hazards[:3])
                            ),
                            detail={"ledger": sp_path.name, "hazards": hazards},
                        )
                    )
                if dust_rows:
                    findings.append(
                        Finding(
                            severity="YELLOW",
                            surface="execution_ledgers",
                            code=f"spot_residual_dust_blocking:{sp_path.name}",
                            summary=(
                                f"现货账本 {sp_path.name} 存在 {len(dust_rows)} 笔残余粉尘记录（会阻断该路后续新开仓）"
                            ),
                            detail={"ledger": sp_path.name, "dust_rows": dust_rows},
                        )
                    )
            metrics["spot_ledgers"][sp_path.name] = sp_summary
        finally:
            sconn.close()

    # 2. Cross-Margin Ledger
    if cross_margin_ledger is not None:
        cmconn = connect_readonly_sqlite(cross_margin_ledger)
        if cmconn is not None:
            try:
                cm_summary: dict[str, Any] = {"path": str(cross_margin_ledger)}
                state_counts: Counter[str] = Counter()
                critical_intents: list[dict[str, Any]] = []
                stale_reserved: list[dict[str, Any]] = []
                dust_intents: list[dict[str, Any]] = []
                acked_dust_list: list[dict[str, Any]] = []

                if table_exists(cmconn, "intents"):
                    i_rows = cmconn.execute("SELECT * FROM intents ORDER BY updated_ms ASC").fetchall()
                    for ir in i_rows:
                        iid = str(ir["intent_id"])
                        st = str(ir["state"])
                        upd_ms = int(ir["updated_ms"])
                        age_ms = now_ms - upd_ms
                        state_counts[st] += 1
                        sym = ""
                        try:
                            pdoc = json.loads(str(ir["payload_json"]))
                            sym = str((pdoc.get("intent") or {}).get("symbol") or "")
                        except (TypeError, ValueError):
                            pass

                        if st in _CM_CRITICAL_STATES or (
                            st not in _CM_CLEAN_TERMINAL_STATES
                            and st not in _CM_HEALTHY_WORKING_STATES
                            and st != "reserved"
                        ):
                            critical_intents.append(
                                {
                                    "intent_id": iid,
                                    "symbol": sym,
                                    "state": st,
                                    "age_human": fmt_age_human(age_ms),
                                }
                            )
                        elif st == "reserved" and age_ms >= RESERVED_STALE_MS:
                            stale_reserved.append(
                                {
                                    "intent_id": iid,
                                    "symbol": sym,
                                    "age_ms": age_ms,
                                    "age_human": fmt_age_human(age_ms),
                                }
                            )

                if table_exists(cmconn, "operations"):
                    op_rows = cmconn.execute("SELECT * FROM operations").fetchall()
                    stuck_ops: list[dict[str, Any]] = []
                    for opr in op_rows:
                        iid = str(opr["intent_id"])
                        op_type = str(opr["op_type"])
                        op_state = str(opr["state"])
                        if op_state in {"started", "unknown", "pending_lock", "mismatch"}:
                            stuck_ops.append(
                                {
                                    "intent_id": iid,
                                    "op_type": op_type,
                                    "client_key": str(opr["client_key"]),
                                    "state": op_state,
                                }
                            )
                        # Check recorded OCO lot residual dust
                        for jcol in ("response_json", "request_json"):
                            raw_j = opr[jcol]
                            if not raw_j:
                                continue
                            try:
                                parsed_j = json.loads(str(raw_j))
                            except (TypeError, ValueError):
                                continue
                            if isinstance(parsed_j, Mapping):
                                res_v = parsed_j.get("oco_lot_residual_qty") or parsed_j.get(
                                    "_oco_lot_residual_qty"
                                )
                                if _safe_decimal(res_v) > 0:
                                    entry_item = {
                                        "intent_id": iid,
                                        "oco_lot_residual_qty": str(res_v),
                                    }
                                    if iid in ack_dust_intents:
                                        if entry_item not in acked_dust_list:
                                            acked_dust_list.append(entry_item)
                                    else:
                                        if entry_item not in dust_intents:
                                            dust_intents.append(entry_item)

                    cm_summary["stuck_operations"] = stuck_ops
                    if stuck_ops:
                        findings.append(
                            Finding(
                                severity="RED",
                                surface="execution_ledgers",
                                code="cross_margin_operations_indeterminate",
                                summary=(
                                    f"全仓杠杆 operations 表存在 {len(stuck_ops)} 条未决操作（started/unknown/pending_lock/mismatch），严禁盲目重发"
                                ),
                                detail={"stuck_operations": stuck_ops},
                            )
                        )

                cm_summary["state_counts"] = dict(state_counts)
                cm_summary["critical_intents"] = critical_intents
                cm_summary["stale_reserved"] = stale_reserved
                cm_summary["unacked_dust_intents"] = dust_intents
                cm_summary["acked_dust_intents"] = acked_dust_list

                if critical_intents:
                    findings.append(
                        Finding(
                            severity="RED",
                            surface="execution_ledgers",
                            code="cross_margin_unresolved_hazard_intents",
                            summary=(
                                f"全仓杠杆账本存在 {len(critical_intents)} 笔危险/未决意图阻断新开仓："
                                + ", ".join(f"{x['intent_id']}:{x['symbol']}({x['state']})" for x in critical_intents[:4])
                            ),
                            detail={"critical_intents": critical_intents},
                        )
                    )
                if stale_reserved:
                    findings.append(
                        Finding(
                            severity="RED",
                            surface="execution_ledgers",
                            code="cross_margin_reserved_stuck_over_15s",
                            summary=(
                                f"全仓杠杆账本存在 {len(stale_reserved)} 笔超过 15 秒仍停留在 reserved 的预留意图："
                                + ", ".join(f"{x['intent_id']}({x['age_human']})" for x in stale_reserved[:4])
                            ),
                            detail={"stale_reserved": stale_reserved},
                        )
                    )
                if dust_intents:
                    findings.append(
                        Finding(
                            severity="YELLOW",
                            surface="execution_ledgers",
                            code="cross_margin_lot_residual_dust",
                            summary=(
                                f"全仓杠杆账本存在 {len(dust_intents)} 笔未登记确认的步长截断粉尘余量："
                                + ", ".join(f"{x['intent_id']}({x['oco_lot_residual_qty']})" for x in dust_intents[:4])
                            ),
                            detail={"unacked_dust": dust_intents},
                        )
                    )
                if not critical_intents and not stale_reserved and not cm_summary.get("stuck_operations"):
                    findings.append(
                        Finding(
                            severity="GREEN",
                            surface="execution_ledgers",
                            code="cross_margin_ledger_clean",
                            summary=(
                                f"全仓杠杆账本无未决风险状态（活跃受保护/挂单数={sum(state_counts[k] for k in _CM_HEALTHY_WORKING_STATES)}，已备案粉尘={len(acked_dust_list)}）"
                            ),
                            detail=cm_summary,
                        )
                    )
                metrics["cross_margin_ledger"] = cm_summary
            finally:
                cmconn.close()

    return findings, metrics


# --- Surface D: Decision Funnel & Silent Lockout ---

def inspect_signal_funnel(
    *,
    now_ms: int,
    main_db: Path,
    spot_ledgers: Sequence[Path],
    lookback_hours: float = 6.0,
) -> tuple[list[Finding], dict[str, Any]]:
    findings: list[Finding] = []
    funnel: dict[str, Any] = {"lookback_hours": lookback_hours}

    mconn = connect_readonly_sqlite(main_db)
    if mconn is not None:
        try:
            if table_exists(mconn, "decision_log"):
                cutoff_iso = datetime.fromtimestamp(
                    (now_ms - int(lookback_hours * 3_600_000)) / 1000.0, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                rows = mconn.execute(
                    """
                    SELECT purpose, risk_passed, reason, ts
                    FROM decision_log
                    WHERE ts >= ?
                    ORDER BY id DESC
                    """,
                    (cutoff_iso,),
                ).fetchall()
                by_purpose: Counter[str] = Counter()
                passed_by_purpose: Counter[str] = Counter()
                reject_reasons: Counter[str] = Counter()
                latest_cm_ts_ms: int | None = None
                for r in rows:
                    purp = str(r["purpose"] or "live_candidate")
                    by_purpose[purp] += 1
                    if int(r["risk_passed"] or 0) == 1:
                        passed_by_purpose[purp] += 1
                    else:
                        reject_reasons[str(r["reason"] or "unspecified_risk_reject")] += 1
                    if purp == "live_bnb_cross_margin" and latest_cm_ts_ms is None:
                        latest_cm_ts_ms, _ = parse_any_ts_ms(r["ts"])

                funnel["decision_rows_in_window"] = len(rows)
                funnel["by_purpose"] = dict(by_purpose)
                funnel["risk_passed_by_purpose"] = dict(passed_by_purpose)
                funnel["top_decision_reject_reasons"] = reject_reasons.most_common(5)
                funnel["latest_cross_margin_signal_utc"] = fmt_ms_utc(latest_cm_ts_ms)
        finally:
            mconn.close()

    # Aggregate gateway validation_errors across spot/execution ledgers
    exec_rejects: Counter[str] = Counter()
    cutoff_exec_iso = datetime.fromtimestamp(
        (now_ms - int(lookback_hours * 3_600_000)) / 1000.0, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    for sp_path in spot_ledgers:
        sconn = connect_readonly_sqlite(sp_path)
        if sconn is None:
            continue
        try:
            if table_exists(sconn, "execution_log"):
                erows = sconn.execute(
                    """
                    SELECT validation_errors, status
                    FROM execution_log
                    WHERE ts >= ? AND validation_ok = 0
                    """,
                    (cutoff_exec_iso,),
                ).fetchall()
                for er in erows:
                    raw_e = er["validation_errors"]
                    if raw_e:
                        try:
                            elist = json.loads(str(raw_e))
                            if isinstance(elist, list) and elist:
                                for code in elist:
                                    exec_rejects[str(code)] += 1
                                continue
                        except (TypeError, ValueError):
                            pass
                    exec_rejects[str(er["status"] or "validation_failed")] += 1
        finally:
            sconn.close()

    funnel["top_execution_reject_reasons"] = exec_rejects.most_common(5)
    if exec_rejects:
        top_str = ", ".join(f"{k}({v})" for k, v in exec_rejects.most_common(3))
        findings.append(
            Finding(
                severity="YELLOW",
                surface="signal_funnel",
                code="execution_gateway_recent_rejects",
                summary=f"近 {lookback_hours:g}h 执行网关拦截 Top 原因：{top_str}",
                detail={"top_execution_rejects": exec_rejects.most_common(5)},
            )
        )
    return findings, funnel


# --- Top-Level Orchestrator ---

def resolve_default_paths(
    repo_root: Path,
    *,
    main_db: str | None = None,
    deriv_db: str | None = None,
    lseg_db: str | None = None,
    cityu_db: str | None = None,
    cross_margin_ledger: str | None = None,
    spot_ledgers: Sequence[str] | None = None,
) -> dict[str, Any]:
    root = repo_root.resolve()
    m_db = Path(main_db).expanduser() if main_db else Path(
        os.environ.get("DECISION_DB_PATH") or (root / "dexscreener.db")
    )
    d_db = Path(deriv_db).expanduser() if deriv_db else Path(
        os.environ.get("PUBLIC_DERIVATIVES_DB_PATH")
        or (root / "data" / "counterparty" / "public_derivatives.db")
    )
    l_db = Path(lseg_db).expanduser() if lseg_db else Path(
        os.environ.get("DECISION_MACRO_DB")
        or os.environ.get("LSEG_MACRO_DB_PATH")
        or (root / "data" / "lseg" / "lseg_macro.db")
    )
    c_db = Path(cityu_db).expanduser() if cityu_db else Path(
        os.environ.get("CITYU_INSTITUTIONAL_DB_PATH")
        or (root / "data" / "cityu" / "cityu_institutional.db")
    )
    cm_raw = cross_margin_ledger or os.environ.get("CROSS_MARGIN_LEDGER_PATH")
    cm_db = (
        Path(cm_raw).expanduser()
        if cm_raw
        else (root / "execution_ledger_live_bnb_cross_margin.db")
    )
    if spot_ledgers:
        sp_list = [Path(p).expanduser() for p in spot_ledgers]
    else:
        sp_list = [
            root / "execution_ledger.db",
            root / "execution_ledger_live_bnb_boll_fade.db",
            root / "execution_ledger_live_bnb_boll_classic.db",
        ]
    return {
        "repo_root": root,
        "main_db": m_db,
        "deriv_db": d_db,
        "lseg_db": l_db,
        "cityu_db": c_db,
        "cross_margin_ledger": cm_db,
        "spot_ledgers": sp_list,
    }


def inspect_system(
    repo_root: Path,
    *,
    now_ms: int | None = None,
    main_db: str | None = None,
    deriv_db: str | None = None,
    lseg_db: str | None = None,
    cityu_db: str | None = None,
    cross_margin_ledger: str | None = None,
    spot_ledgers: Sequence[str] | None = None,
    require_dbs: bool = False,
    ack_dust_intents: Iterable[str] = (),
    lookback_hours: float = 6.0,
) -> PreflightReport:
    clock_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    paths = resolve_default_paths(
        repo_root,
        main_db=main_db,
        deriv_db=deriv_db,
        lseg_db=lseg_db,
        cityu_db=cityu_db,
        cross_margin_ledger=cross_margin_ledger,
        spot_ledgers=spot_ledgers,
    )
    nyse_open = eval_nyse_cash_market_open(clock_ms)
    expected_cot = eval_due_cftc_asof(clock_ms).isoformat()

    findings: list[Finding] = []
    all_metrics: dict[str, Any] = {}

    code_state, code_findings, code_metrics = inspect_code_integrity(paths["repo_root"])
    findings.extend(code_findings)
    all_metrics["code"] = code_metrics

    clock_findings, clock_metrics = inspect_data_clocks(
        now_ms=clock_ms,
        nyse_open=nyse_open,
        expected_cot=expected_cot,
        main_db=paths["main_db"],
        deriv_db=paths["deriv_db"],
        lseg_db=paths["lseg_db"],
        cityu_db=paths["cityu_db"],
        require_dbs=require_dbs,
    )
    findings.extend(clock_findings)
    all_metrics["clocks"] = clock_metrics

    ledger_findings, ledger_metrics = inspect_execution_ledgers(
        now_ms=clock_ms,
        main_db=paths["main_db"],
        spot_ledgers=paths["spot_ledgers"],
        cross_margin_ledger=paths["cross_margin_ledger"],
        ack_dust_intents=frozenset(str(x).strip() for x in ack_dust_intents if str(x).strip()),
    )
    findings.extend(ledger_findings)
    all_metrics["ledgers"] = ledger_metrics

    funnel_findings, funnel_metrics = inspect_signal_funnel(
        now_ms=clock_ms,
        main_db=paths["main_db"],
        spot_ledgers=paths["spot_ledgers"],
        lookback_hours=lookback_hours,
    )
    findings.extend(funnel_findings)
    all_metrics["funnel"] = funnel_metrics

    red_n = sum(1 for f in findings if f.severity == "RED")
    yel_n = sum(1 for f in findings if f.severity == "YELLOW")
    grn_n = sum(1 for f in findings if f.severity == "GREEN")
    overall = "RED" if red_n > 0 else ("YELLOW" if yel_n > 0 else "GREEN")

    return PreflightReport(
        checked_at_utc=fmt_ms_utc(clock_ms),
        now_ms=clock_ms,
        repo_root=str(paths["repo_root"]),
        overall_status=overall,
        red_count=red_n,
        yellow_count=yel_n,
        green_count=grn_n,
        code_state=code_state,
        nyse_cash_open=nyse_open,
        expected_cot_asof=expected_cot,
        findings=findings,
        metrics=all_metrics,
    )


def format_markdown_disclosure(report: PreflightReport) -> str:
    """Format the mandatory top-of-reply Agent Disclosure Block (no raw > or ---)."""
    lines: list[str] = [
        f"## 现网只读体检与红灯直报单（状态：{report.overall_status}）",
        (
            f"- **体检时刻（UTC）**：`{report.checked_at_utc}` | "
            f"**代码树指纹态**：`{report.code_state}` | "
            f"**NYSE 常规盘开市**：`{report.nyse_cash_open}` | "
            f"**应付 CFTC COT 期次**：`{report.expected_cot_asof}`"
        ),
        (
            f"- **计数汇总**：红灯阻断 `{report.red_count}` 项 | "
            f"黄灯预警 `{report.yellow_count}` 项 | "
            f"绿灯通过 `{report.green_count}` 项"
        ),
    ]

    reds = [f for f in report.findings if f.severity == "RED"]
    yellows = [f for f in report.findings if f.severity == "YELLOW"]
    greens = [f for f in report.findings if f.severity == "GREEN"]

    lines.append("")
    lines.append("### 1. 红灯阻断区（RED — 禁止掩盖或归为「非本次引入」）")
    if not reds:
        lines.append("- 无红灯阻断项。")
    else:
        for item in reds:
            lines.append(f"- **[RED][{item.surface}] `{item.code}`**：{item.summary}")

    lines.append("")
    lines.append("### 2. 黄灯预警与闭锁漏斗区（YELLOW — 休市断流、粉尘与高频拒单）")
    if not yellows:
        lines.append("- 无黄灯预警项。")
    else:
        for item in yellows:
            lines.append(f"- **[YELLOW][{item.surface}] `{item.code}`**：{item.summary}")

    funnel = report.metrics.get("funnel") or {}
    top_dec = funnel.get("top_decision_reject_reasons") or []
    top_exe = funnel.get("top_execution_reject_reasons") or []
    if top_dec or top_exe:
        dec_txt = ", ".join(f"`{k}`({v})" for k, v in top_dec[:3]) or "无"
        exe_txt = ", ".join(f"`{k}`({v})" for k, v in top_exe[:3]) or "无"
        lines.append(f"- **近期拒单漏斗 Top 3**：决策层 {dec_txt}；执行网关层 {exe_txt}")

    lines.append("")
    lines.append("### 3. 已核实绿灯基线（GREEN）")
    if not greens:
        lines.append("- 暂无绿灯项（或未连接现网 SQLite 库文件）。")
    else:
        for item in greens:
            lines.append(f"- **[GREEN][{item.surface}] `{item.code}`**：{item.summary}")

    return "\n".join(lines) + "\n"


from typing import Iterable  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pure read-only (mode=ro) live preflight & red-light disclosure inspector."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Path to live repo root (/workspace/dexscreener-crawler) or runtime-source snapshot.",
    )
    parser.add_argument("--main-db", default=None, help="Explicit path to dexscreener.db")
    parser.add_argument("--deriv-db", default=None, help="Explicit path to public_derivatives.db")
    parser.add_argument("--lseg-db", default=None, help="Explicit path to lseg_macro.db")
    parser.add_argument("--cityu-db", default=None, help="Explicit path to cityu_institutional.db")
    parser.add_argument(
        "--cross-margin-ledger",
        default=None,
        help="Explicit path to cross-margin SQLite ledger",
    )
    parser.add_argument(
        "--spot-ledger",
        action="append",
        default=None,
        help="Explicit path(s) to spot execution SQLite ledgers (repeatable)",
    )
    parser.add_argument(
        "--ack-dust-intents",
        default="",
        help="Comma-separated operator-acknowledged dust intent IDs (e.g. dl-149673)",
    )
    parser.add_argument(
        "--require-dbs",
        action="store_true",
        help="Treat missing SQLite DB files as RED (enable on live server).",
    )
    parser.add_argument(
        "--fail-on-yellow",
        action="store_true",
        help="Return exit code 2 on YELLOW warnings as well as RED blockers.",
    )
    parser.add_argument("--now-ms", type=int, default=None, help="Override clock (ms) for deterministic audit")
    parser.add_argument("--lookback-hours", type=float, default=6.0, help="Decision/execution funnel window")
    parser.add_argument("--json", action="store_true", help="Emit JSON report instead of Markdown")
    args = parser.parse_args(list(argv) if argv is not None else None)

    ack_set = [x.strip() for x in str(args.ack_dust_intents or "").split(",") if x.strip()]
    report = inspect_system(
        args.repo_root,
        now_ms=args.now_ms,
        main_db=args.main_db,
        deriv_db=args.deriv_db,
        lseg_db=args.lseg_db,
        cityu_db=args.cityu_db,
        cross_margin_ledger=args.cross_margin_ledger,
        spot_ledgers=args.spot_ledger,
        require_dbs=bool(args.require_dbs),
        ack_dust_intents=ack_set,
        lookback_hours=float(args.lookback_hours),
    )

    if args.json:
        sys.stdout.write(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(format_markdown_disclosure(report))

    if report.red_count > 0:
        return 2
    if args.fail_on_yellow and report.yellow_count > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
