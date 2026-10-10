#!/usr/bin/env python3
"""
Hourly/daily Grok-oriented report export.

  python scripts/grok_report.py --mode hourly
  python scripts/grok_report.py --mode daily --telegram   # if GROK_REPORT_TELEGRAM / --telegram
  python scripts/grok_report.py --mode hourly --incremental

Writes exports/reports/report-{mode}-YYYYMMDD-HHMM.md (+ .json).
Incremental: only alerts/scores newer than last export cursor in GROK_REPORT_STATE.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import (  # noqa: E402
    DB_PATH,
    GROK_REPORT_DIR,
    GROK_REPORT_STATE,
    GROK_REPORT_TELEGRAM,
    GROK_SYSTEM_PROMPT_PATH,
)
from logging_setup import setup_logging, get_logger  # noqa: E402
from store import Store  # noqa: E402
from tools.grok_context import build_context, to_markdown  # noqa: E402

log = get_logger("grok_report")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")


def _load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _filter_incremental(ctx: dict[str, Any], since: str | None) -> dict[str, Any]:
    if not since:
        return ctx
    alerts = [
        a
        for a in (ctx.get("recent_alerts") or [])
        if (a.get("created_at") or "") > since
    ]
    # scores: keep those scored_at after since
    top = [
        t
        for t in (ctx.get("top_scores") or [])
        if (t.get("scored_at") or "") > since
    ]
    low = [
        t
        for t in (ctx.get("low_scores") or [])
        if (t.get("scored_at") or t.get("quality_score") is not None)
        and (t.get("scored_at") or "") > since
    ]
    out = dict(ctx)
    out["recent_alerts"] = alerts
    out["top_scores"] = top or ctx.get("top_scores")  # fall back if none newly scored
    out["low_scores"] = low
    out["incremental_since"] = since
    out["incremental"] = True
    return out


def build_report_markdown(mode: str, ctx: dict[str, Any]) -> str:
    prompt_path = ROOT / "prompts" / f"{mode}_report.md"
    task = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else f"# {mode} report\n"
    sys_path = ROOT / GROK_SYSTEM_PROMPT_PATH
    sys_snip = ""
    if sys_path.is_file():
        sys_snip = "\n".join(sys_path.read_text(encoding="utf-8").splitlines()[:20])
    body = to_markdown(ctx)
    lines = [
        f"# Grok crawler report ({mode})",
        f"",
        f"generated_at: `{ctx.get('generated_at')}`",
        f"incremental: `{ctx.get('incremental', False)}` since=`{ctx.get('incremental_since', '')}`",
        f"",
        f"## System prompt (excerpt)",
        f"",
        f"```",
        sys_snip,
        f"```",
        f"",
        f"## Task prompt",
        f"",
        task.strip(),
        f"",
        f"## Context export",
        f"",
        body,
    ]
    return "\n".join(lines) + "\n"


def maybe_telegram(text: str, *, enabled: bool) -> bool:
    if not enabled:
        return False
    try:
        from alerts_telegram import get_telegram_alerter, telegram_enabled
        from config import TELEGRAM_CHAT_ID_ADMIN, TELEGRAM_CHAT_ID

        if not telegram_enabled():
            log.warning("telegram push skipped: TELEGRAM_BOT_TOKEN unset")
            return False
        chat = TELEGRAM_CHAT_ID_ADMIN or TELEGRAM_CHAT_ID
        if not chat:
            log.warning("telegram push skipped: no admin/fallback chat")
            return False
        # Telegram message limit ~4096; send head
        chunk = text[:3500]
        # convert markdown-ish to plain-ish
        plain = (
            chunk.replace("`", "")
            .replace("**", "")
            .replace("#", "")
        )
        tg = get_telegram_alerter()
        ok = tg.send_message(chat, f"<pre>{_escape(plain)}</pre>", parse_mode="HTML")
        return ok
    except Exception as exc:  # noqa: BLE001
        log.error("telegram report push failed: %s", exc)
        return False


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grok hourly/daily report export")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--mode", choices=("hourly", "daily"), default="hourly")
    parser.add_argument("--incremental", action="store_true")
    parser.add_argument("--telegram", action="store_true", help="Push to Telegram admin chat")
    parser.add_argument("--out-dir", default=GROK_REPORT_DIR)
    args = parser.parse_args(argv)
    setup_logging()

    state_path = Path(GROK_REPORT_STATE)
    state = _load_state(state_path)
    since = state.get("last_export_at") if args.incremental else None

    with Store(args.db) as store:
        ctx = build_context(store, top_n=20 if args.mode == "daily" else 12, alerts_n=40)
    if args.incremental:
        ctx = _filter_incremental(ctx, since)

    md = build_report_markdown(args.mode, ctx)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _utc_stamp()
    md_path = out_dir / f"report-{args.mode}-{stamp}.md"
    json_path = out_dir / f"report-{args.mode}-{stamp}.json"
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # also refresh latest symlinks-ish copies
    (out_dir / f"latest-{args.mode}.md").write_text(md, encoding="utf-8")
    (out_dir / f"latest-{args.mode}.json").write_text(
        json.dumps(ctx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    state["last_export_at"] = ctx.get("generated_at")
    state["last_mode"] = args.mode
    state["last_md"] = str(md_path)
    _save_state(state_path, state)

    push = args.telegram or GROK_REPORT_TELEGRAM
    sent = maybe_telegram(md, enabled=push)
    print(f"REPORT_OK mode={args.mode} md={md_path} json={json_path} telegram={sent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
