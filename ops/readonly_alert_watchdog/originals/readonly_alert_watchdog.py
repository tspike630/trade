#!/usr/bin/env python3
"""Independent Read-Only Alert Watchdog for tspike630/trade.

Runs completely outside ``decision_engine.run`` and ``execution.run`` so silent
``fail-closed`` lockouts, indeterminate ``protection_unknown`` positions, stale
LSEG/Deribit clocks, or runtime SHA256 drift push immediate out-of-band alerts
without relying on any Quant Agent being asked.

Safety Invariants:
1. Reads production SQLite databases strictly via ``readonly_live_preflight``
   (``file:...?mode=ro`` + ``PRAGMA query_only = ON``).
2. Refuses any ``--state-file`` or ``--alert-jsonl`` located inside ``--repo-root``
   or ending in ``.db`` / ``.sqlite`` / ``.py`` so production trees and ledgers can
   never be touched.
3. Detects consecutive 5-minute bar silent clock/gate lockouts (>= 2 consecutive
   5m bars) and escalates persistent feed lockouts explicitly.
4. Deduplicates identical active alert sets within ``--renotify-sec`` (default
   1800s), pushes immediately whenever a NEW red/yellow code appears, and sends
   an explicit ``[RESOLVED]`` clear notification when red alerts recover.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Allow importing sibling readonly_live_preflight regardless of working directory
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from readonly_live_preflight import (  # noqa: E402
    Finding,
    PreflightReport,
    fmt_ms_utc,
    inspect_system,
)

DEFAULT_STATE_FILE = Path("/tmp/trade_readonly_watchdog_state.json")
DEFAULT_INTERVAL_SEC = 300
DEFAULT_RENOTIFY_SEC = 1800
FIVE_MIN_MS = 300_000

_CLOCK_LOCKOUT_PREFIXES = (
    "lseg_nvda_smh_stale_market_open",
    "deribit_gamma_stale_or_invalid",
    "perp_counterparty_stale",
    "obi_top5_source_stale",
    "binance_indicators_stale",
    "entry_5m_kline_stale",
    "whitelist_news_stale",
    "cot_publication_not_current",
    "cot_asof_future_timestamp",
)


class UnsafeWatchdogPathError(ValueError):
    """Raised when a state/log path points inside the monitored repo or a DB file."""


def validate_external_state_path(target: Path, repo_root: Path) -> Path:
    resolved_target = target.expanduser().resolve()
    resolved_repo = repo_root.expanduser().resolve()
    if resolved_target == Path("/") or str(resolved_target) == "":
        raise UnsafeWatchdogPathError(f"refusing root state path: {resolved_target}")
    if resolved_target == resolved_repo or resolved_repo in resolved_target.parents:
        raise UnsafeWatchdogPathError(
            f"state/log file {resolved_target} must live outside monitored repo {resolved_repo}"
        )
    if resolved_target.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".py", ".sh", ".patch"}:
        raise UnsafeWatchdogPathError(
            f"refusing state/log path with protected suffix: {resolved_target}"
        )
    return resolved_target


def load_watchdog_state(state_path: Path) -> dict[str, Any]:
    if not state_path.is_file():
        return {
            "active_red_codes": [],
            "active_yellow_codes": [],
            "last_push_ms": 0,
            "consecutive_lockout_bars": 0,
            "last_lockout_bar_bucket": None,
        }
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except (OSError, ValueError):
        pass
    return {
        "active_red_codes": [],
        "active_yellow_codes": [],
        "last_push_ms": 0,
        "consecutive_lockout_bars": 0,
        "last_lockout_bar_bucket": None,
    }


def save_watchdog_state(state_path: Path, state: Mapping[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(f".{state_path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(dict(state), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(state_path)


@dataclass(frozen=True)
class AlertDecision:
    should_push: bool
    event_type: str  # "NEW_ALERT", "ESCALATED_SILENT_LOCKOUT", "RENOTIFY", "RESOLVED", "QUIET"
    title: str
    body_text: str
    red_codes: tuple[str, ...]
    yellow_codes: tuple[str, ...]
    consecutive_lockout_bars: int


def evaluate_alert_transition(
    report: PreflightReport,
    prior_state: Mapping[str, Any],
    *,
    renotify_sec: int = DEFAULT_RENOTIFY_SEC,
    alert_on_yellow: bool = False,
) -> tuple[AlertDecision, dict[str, Any]]:
    now_ms = report.now_ms
    cur_bar_bucket = now_ms // FIVE_MIN_MS

    red_findings = [f for f in report.findings if f.severity == "RED"]
    yellow_findings = [f for f in report.findings if f.severity == "YELLOW"]

    has_clock_lockout = any(
        any(f.code.startswith(pfx) for pfx in _CLOCK_LOCKOUT_PREFIXES)
        for f in red_findings
    )
    prev_bars = int(prior_state.get("consecutive_lockout_bars") or 0)
    prev_bucket = prior_state.get("last_lockout_bar_bucket")

    if has_clock_lockout:
        if prev_bucket is None:
            consec_bars = 1
        elif int(cur_bar_bucket) > int(prev_bucket):
            consec_bars = prev_bars + 1
        else:
            consec_bars = max(1, prev_bars)
        next_bucket: int | None = int(cur_bar_bucket)
    else:
        consec_bars = 0
        next_bucket = None

    # Inject synthetic consecutive silent-lockout finding when >= 2 5m bars
    effective_reds = list(red_findings)
    if consec_bars >= 2:
        effective_reds.insert(
            0,
            Finding(
                severity="RED",
                surface="signal_funnel",
                code="consecutive_5m_silent_clock_lockout",
                summary=(
                    f"策略真源时钟已连续闭锁 {consec_bars} 根 5m K线周期（实盘新开仓静默停摆中）"
                ),
                detail={"consecutive_5m_bars": consec_bars},
            ),
        )

    cur_red_codes = tuple(sorted({f.code for f in effective_reds}))
    cur_yel_codes = tuple(sorted({f.code for f in yellow_findings}))
    prev_red_codes = set(str(x) for x in (prior_state.get("active_red_codes") or []))
    prev_yel_codes = set(str(x) for x in (prior_state.get("active_yellow_codes") or []))
    last_push_ms = int(prior_state.get("last_push_ms") or 0)

    new_reds = [c for c in cur_red_codes if c not in prev_red_codes]
    new_yels = [c for c in cur_yel_codes if c not in prev_yel_codes]

    should_push = False
    event_type = "QUIET"

    if cur_red_codes:
        if "consecutive_5m_silent_clock_lockout" in new_reds:
            should_push = True
            event_type = "ESCALATED_SILENT_LOCKOUT"
        elif new_reds:
            should_push = True
            event_type = "NEW_ALERT"
        elif now_ms - last_push_ms >= int(renotify_sec) * 1000:
            should_push = True
            event_type = "RENOTIFY"
    elif prev_red_codes and not cur_red_codes:
        should_push = True
        event_type = "RESOLVED"
    elif alert_on_yellow and cur_yel_codes:
        if new_yels:
            should_push = True
            event_type = "NEW_ALERT"
        elif now_ms - last_push_ms >= int(renotify_sec) * 1000:
            should_push = True
            event_type = "RENOTIFY"

    title, body = _render_push_message(
        report=report,
        event_type=event_type,
        reds=effective_reds,
        yellows=yellow_findings,
        consec_bars=consec_bars,
    )

    next_state = {
        "active_red_codes": list(cur_red_codes),
        "active_yellow_codes": list(cur_yel_codes),
        "last_push_ms": now_ms if should_push else last_push_ms,
        "consecutive_lockout_bars": consec_bars,
        "last_lockout_bar_bucket": next_bucket,
        "last_checked_utc": report.checked_at_utc,
    }
    decision = AlertDecision(
        should_push=should_push,
        event_type=event_type,
        title=title,
        body_text=body,
        red_codes=cur_red_codes,
        yellow_codes=cur_yel_codes,
        consecutive_lockout_bars=consec_bars,
    )
    return decision, next_state


def _render_push_message(
    *,
    report: PreflightReport,
    event_type: str,
    reds: Sequence[Finding],
    yellows: Sequence[Finding],
    consec_bars: int,
) -> tuple[str, str]:
    if event_type == "RESOLVED":
        title = f"[RESOLVED] trade 现网红灯已解除 ({report.checked_at_utc})"
        lines = [
            f"时刻: {report.checked_at_utc} | 代码态: {report.code_state}",
            f"当前红灯: 0 | 黄灯预警: {len(yellows)}",
        ]
        for y in yellows[:3]:
            lines.append(f"- [YELLOW] {y.code}: {y.summary}")
        return title, "\n".join(lines)

    prefix = f"[{event_type}]" if event_type != "QUIET" else "[OK]"
    title = (
        f"{prefix} trade 哨兵告警: 红灯 {len(reds)} 项 / 黄灯 {len(yellows)} 项 "
        f"({report.checked_at_utc})"
    )
    lines = [
        f"时刻: {report.checked_at_utc} | 代码态: {report.code_state} | NYSE开市: {report.nyse_cash_open} | 连续闭锁棒数: {consec_bars}",
    ]
    for r in reds[:6]:
        lines.append(f"- [RED] {r.code}: {r.summary}")
    for y in yellows[:3]:
        lines.append(f"- [YELLOW] {y.code}: {y.summary}")
    return title, "\n".join(lines)


# --- Outbound Notification Sinks (Webhook / Telegram / Bark / macOS / JSONL) ---

def post_json_webhook(url: str, payload: Mapping[str, Any], timeout: float = 6.0) -> tuple[bool, str]:
    body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "trade-readonly-watchdog/1.0"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310
            code = int(getattr(resp, "status", 200) or 200)
            return 200 <= code < 300, f"http_{code}"
    except HTTPError as exc:
        return False, f"http_{exc.code}"
    except (URLError, TimeoutError, OSError) as exc:
        return False, f"network_err:{type(exc).__name__}"


def dispatch_notifications(
    decision: AlertDecision,
    report: PreflightReport,
    *,
    webhook_url: str = "",
    telegram_bot_token: str = "",
    telegram_chat_id: str = "",
    bark_url: str = "",
    macos_notify: bool = False,
    alert_jsonl: Path | None = None,
    http_poster: Callable[[str, Mapping[str, Any]], tuple[bool, str]] | None = None,
) -> list[dict[str, Any]]:
    poster = http_poster or (lambda u, p: post_json_webhook(u, p))
    receipts: list[dict[str, Any]] = []
    full_text = f"{decision.title}\n{decision.body_text}"

    if alert_jsonl is not None:
        alert_jsonl.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts_utc": report.checked_at_utc,
            "event_type": decision.event_type,
            "title": decision.title,
            "red_codes": list(decision.red_codes),
            "yellow_codes": list(decision.yellow_codes),
            "consecutive_lockout_bars": decision.consecutive_lockout_bars,
            "body": decision.body_text,
        }
        with alert_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        receipts.append({"channel": "jsonl", "ok": True, "target": str(alert_jsonl)})

    if webhook_url:
        # Compatible with Slack/Discord/Feishu/Generic JSON webhooks
        payload = {
            "msg_type": "text",
            "text": full_text,
            "content": full_text,
            " {}" if False else "title": decision.title,
        }
        ok, detail = poster(webhook_url, payload)
        receipts.append({"channel": "webhook", "ok": ok, "detail": detail})

    if telegram_bot_token and telegram_chat_id:
        tg_url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
        ok, detail = poster(
            tg_url,
            {"chat_id": telegram_chat_id, "text": full_text, "disable_web_page_preview": True},
        )
        receipts.append({"channel": "telegram", "ok": ok, "detail": detail})

    if bark_url:
        ok, detail = poster(
            bark_url.rstrip("/"),
            {"title": decision.title, "body": decision.body_text, "group": "trade-watchdog"},
        )
        receipts.append({"channel": "bark", "ok": ok, "detail": detail})

    if macos_notify and sys.platform == "darwin":
        safe_title = decision.title.replace("\\", "\\\\").replace('"', '\\"')
        safe_body = decision.body_text[:220].replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        try:
            subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    f'display notification "{safe_body}" with title "{safe_title}"',
                ],
                check=False,
                timeout=3.0,
                capture_output=True,
            )
            receipts.append({"channel": "macos_notification", "ok": True})
        except (OSError, subprocess.SubprocessError) as exc:
            receipts.append({"channel": "macos_notification", "ok": False, "detail": str(exc)})

    return receipts


def run_watchdog_once(
    repo_root: Path,
    *,
    state_file: Path = DEFAULT_STATE_FILE,
    alert_jsonl: Path | None = None,
    now_ms: int | None = None,
    main_db: str | None = None,
    deriv_db: str | None = None,
    lseg_db: str | None = None,
    cityu_db: str | None = None,
    cross_margin_ledger: str | None = None,
    spot_ledgers: Sequence[str] | None = None,
    require_dbs: bool = False,
    ack_dust_intents: Sequence[str] = (),
    renotify_sec: int = DEFAULT_RENOTIFY_SEC,
    alert_on_yellow: bool = False,
    webhook_url: str = "",
    telegram_bot_token: str = "",
    telegram_chat_id: str = "",
    bark_url: str = "",
    macos_notify: bool = False,
    http_poster: Callable[[str, Mapping[str, Any]], tuple[bool, str]] | None = None,
) -> dict[str, Any]:
    safe_state = validate_external_state_path(state_file, repo_root)
    safe_jsonl = (
        validate_external_state_path(alert_jsonl, repo_root)
        if alert_jsonl is not None
        else None
    )

    prior = load_watchdog_state(safe_state)
    report = inspect_system(
        repo_root,
        now_ms=now_ms,
        main_db=main_db,
        deriv_db=deriv_db,
        lseg_db=lseg_db,
        cityu_db=cityu_db,
        cross_margin_ledger=cross_margin_ledger,
        spot_ledgers=spot_ledgers,
        require_dbs=require_dbs,
        ack_dust_intents=ack_dust_intents,
    )
    decision, next_state = evaluate_alert_transition(
        report,
        prior,
        renotify_sec=renotify_sec,
        alert_on_yellow=alert_on_yellow,
    )
    deliveries: list[dict[str, Any]] = []
    if decision.should_push:
        deliveries = dispatch_notifications(
            decision,
            report,
            webhook_url=webhook_url,
            telegram_bot_token=telegram_bot_token,
            telegram_chat_id=telegram_chat_id,
            bark_url=bark_url,
            macos_notify=macos_notify,
            alert_jsonl=safe_jsonl,
            http_poster=http_poster,
        )
    save_watchdog_state(safe_state, next_state)
    return {
        "checked_at_utc": report.checked_at_utc,
        "overall_status": report.overall_status,
        "event_type": decision.event_type,
        "should_push": decision.should_push,
        "title": decision.title,
        "red_codes": list(decision.red_codes),
        "yellow_codes": list(decision.yellow_codes),
        "consecutive_lockout_bars": decision.consecutive_lockout_bars,
        "deliveries": deliveries,
        "state_file": str(safe_state),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Independent read-only watchdog sentinel for tspike630/trade."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--alert-jsonl", type=Path, default=None)
    parser.add_argument("--main-db", default=None)
    parser.add_argument("--deriv-db", default=None)
    parser.add_argument("--lseg-db", default=None)
    parser.add_argument("--cityu-db", default=None)
    parser.add_argument("--cross-margin-ledger", default=None)
    parser.add_argument("--spot-ledger", action="append", default=None)
    parser.add_argument("--ack-dust-intents", default="")
    parser.add_argument("--require-dbs", action="store_true")
    parser.add_argument("--alert-on-yellow", action="store_true")
    parser.add_argument("--renotify-sec", type=int, default=DEFAULT_RENOTIFY_SEC)
    parser.add_argument("--loop", action="store_true", help="Run continuously every --interval-sec")
    parser.add_argument("--interval-sec", type=int, default=DEFAULT_INTERVAL_SEC)
    parser.add_argument("--macos-notify", action="store_true")
    parser.add_argument("--now-ms", type=int, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    webhook_url = os.environ.get("WATCHDOG_WEBHOOK_URL", "").strip()
    tg_token = os.environ.get("WATCHDOG_TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("WATCHDOG_TELEGRAM_CHAT_ID", "").strip()
    bark_url = os.environ.get("WATCHDOG_BARK_URL", "").strip()
    ack_set = [x.strip() for x in str(args.ack_dust_intents or "").split(",") if x.strip()]

    stop = False

    def _handle_sig(_sig: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    if args.loop:
        signal.signal(signal.SIGINT, _handle_sig)
        signal.signal(signal.SIGTERM, _handle_sig)

    while True:
        started = time.monotonic()
        summary = run_watchdog_once(
            args.repo_root,
            state_file=args.state_file,
            alert_jsonl=args.alert_jsonl,
            now_ms=args.now_ms,
            main_db=args.main_db,
            deriv_db=args.deriv_db,
            lseg_db=args.lseg_db,
            cityu_db=args.cityu_db,
            cross_margin_ledger=args.cross_margin_ledger,
            spot_ledgers=args.spot_ledger,
            require_dbs=bool(args.require_dbs),
            ack_dust_intents=ack_set,
            renotify_sec=int(args.renotify_sec),
            alert_on_yellow=bool(args.alert_on_yellow),
            webhook_url=webhook_url,
            telegram_bot_token=tg_token,
            telegram_chat_id=tg_chat,
            bark_url=bark_url,
            macos_notify=bool(args.macos_notify),
        )
        sys.stdout.write(json.dumps(summary, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        if not args.loop or stop:
            return 2 if summary["overall_status"] == "RED" else 0
        elapsed = time.monotonic() - started
        sleep_s = max(1.0, float(args.interval_sec) - elapsed)
        end_at = time.monotonic() + sleep_s
        while not stop and time.monotonic() < end_at:
            time.sleep(min(1.0, end_at - time.monotonic()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
