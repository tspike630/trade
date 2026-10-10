#!/usr/bin/env python3
"""
Deep due-diligence markdown for one token (Grok / 蜘蛛爬虫).

  python scripts/grok_dd_report.py --chain solana --token TOKEN
  python scripts/grok_dd_report.py --chain solana --token TOKEN --telegram

Assembles score/risk/history/social/alerts (+ optional arb) into research markdown.
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
    GROK_REPORT_TELEGRAM,
    GROK_SYSTEM_PROMPT_PATH,
)
from logging_setup import setup_logging  # noqa: E402
from store import Store, norm_address, norm_chain  # noqa: E402


def assemble_token_context(store: Store, chain: str, token: str) -> dict[str, Any]:
    chain = norm_chain(chain)
    token = norm_address(token)
    tok = store.get_token_api(chain, token)
    q = store.get_token_quality(chain, token)
    pairs = store._conn.execute(
        """
        SELECT pair_address, raw_json, updated_at FROM pairs
        WHERE chain_id=? AND token_address=?
        ORDER BY updated_at DESC LIMIT 5
        """,
        (chain, token),
    ).fetchall()
    pair_summaries = []
    history = []
    for pr in pairs:
        try:
            raw = json.loads(pr["raw_json"])
        except json.JSONDecodeError:
            raw = {}
        pair_summaries.append(
            {
                "pair_address": pr["pair_address"],
                "priceUsd": raw.get("priceUsd"),
                "liquidity": (raw.get("liquidity") or {}).get("usd")
                if isinstance(raw.get("liquidity"), dict)
                else None,
                "volume_h24": (raw.get("volume") or {}).get("h24")
                if isinstance(raw.get("volume"), dict)
                else None,
                "symbol": (raw.get("baseToken") or {}).get("symbol"),
            }
        )
        history.append(
            {
                "pair": pr["pair_address"],
                "points": store.list_pair_history(chain, pr["pair_address"], limit=24),
            }
        )
    social = None
    try:
        social = store.get_x_watch(chain, token)
    except Exception:  # noqa: BLE001
        social = None
    alerts = [
        a
        for a in store.list_alerts_api(limit=100)
        if (a.get("token_address") or "").lower() == token
        or any(
            (p.get("pair") == a.get("pair_address"))
            for h in history
            for p in [{"pair": h["pair"]}]
        )
    ]
    # also alerts on related pairs
    pair_set = {p["pair_address"] for p in pair_summaries}
    alerts = [
        a
        for a in store.list_alerts_api(limit=100)
        if (a.get("token_address") or "").lower() == token
        or (a.get("pair_address") or "") in pair_set
        or a.get("type") == "arb_opportunity"
    ][:30]

    # Indicators (technical + funding + sentiment) — optional; never fail DD if missing
    indicators = None
    jev_raw = None
    try:
        from indicators.base import IndicatorStore

        with IndicatorStore(store.db_path) as ist:
            indicators = ist.get_token_indicators(chain, token, use_cache=False)
            jev_raw = ist.get_jev_signal(chain, token)
    except Exception:  # noqa: BLE001
        indicators = None
        jev_raw = None

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chain_id": chain,
        "token_address": token,
        "token": tok,
        "quality": q,
        "pairs": pair_summaries,
        "history": history,
        "social": social,
        "alerts": alerts,
        "indicators": indicators,
        "jev_signal": jev_raw,
    }


def _trend_note(ind: dict[str, Any] | None) -> str:
    """Brief trend from signs of change windows; Chinese."""
    if not ind:
        return "暂无数据"
    windows = [
        ("5m", ind.get("change_5m")),
        ("15m", ind.get("change_15m")),
        ("1h", ind.get("change_1h")),
        ("4h", ind.get("change_4h")),
        ("24h", ind.get("change_24h")),
    ]
    present = [(n, v) for n, v in windows if v is not None]
    if not present:
        return "暂无数据（无可用 change 窗口）"
    pos = sum(1 for _, v in present if v > 0)
    neg = sum(1 for _, v in present if v < 0)
    flat = sum(1 for _, v in present if v == 0)
    parts = [f"{n}={v:+.4g}%" for n, v in present]
    if pos > neg and pos >= 2:
        bias = "偏多（多数窗口为正）"
    elif neg > pos and neg >= 2:
        bias = "偏空（多数窗口为负）"
    elif pos and neg:
        bias = "分化（多空窗口并存）"
    else:
        bias = "接近横盘"
    return f"{bias}; " + ", ".join(parts) + (f"; flat={flat}" if flat else "")


def _technical_block(ind: dict[str, Any] | None) -> dict[str, Any]:
    if not ind:
        return {"status": "暂无数据"}
    return {
        "change_5m": ind.get("change_5m"),
        "change_15m": ind.get("change_15m"),
        "change_1h": ind.get("change_1h"),
        "change_4h": ind.get("change_4h"),
        "change_24h": ind.get("change_24h"),
        "volatility_24h": ind.get("volatility_24h"),
        "volume_strength": ind.get("volume_strength"),
        "trend_note": _trend_note(ind),
        "updated_at": ind.get("updated_at"),
    }


def _funding_block(
    ind: dict[str, Any] | None, jev: dict[str, Any] | None
) -> dict[str, Any]:
    """Prefer token_indicators funding fields; fall back to jev_signals raw."""
    if ind and any(
        ind.get(k) is not None
        for k in (
            "jev_raw_score",
            "jev_score",
            "jev_signal",
            "jev_confidence",
            "jev_strength",
        )
    ):
        return {
            "jev_raw_score": ind.get("jev_raw_score"),
            "jev_score": ind.get("jev_score") if ind.get("jev_score") is not None else ind.get("jev_raw_score"),
            "jev_signal": ind.get("jev_signal"),
            "jev_confidence": ind.get("jev_confidence"),
            "jev_strength": ind.get("jev_strength"),
            "jev_updated_at": ind.get("jev_updated_at"),
            "source": "token_indicators",
        }
    if jev and jev.get("jev_score") is not None:
        return {
            "jev_raw_score": jev.get("jev_score"),
            "jev_score": jev.get("jev_score"),
            "jev_signal": None,
            "jev_confidence": (
                (jev.get("coverage_pct") / 100.0)
                if jev.get("coverage_pct") is not None
                else None
            ),
            "jev_strength": None,
            "jev_updated_at": jev.get("fetched_at") or jev.get("analyzed_at"),
            "stance": jev.get("stance"),
            "source": "jev_signals",
            "note": "资金面强度尚未映射到 token_indicators（跑 indicators funding）",
        }
    return {"status": "暂无数据"}


def _sentiment_block(ind: dict[str, Any] | None) -> dict[str, Any]:
    """Pull sentiment_* from token_indicators when present; else 暂无数据."""
    if not ind:
        return {"status": "暂无数据"}
    keys = (
        "sentiment_mentions_1h",
        "sentiment_mentions_6h",
        "sentiment_mentions_24h",
        "sentiment_growth_1h",
        "sentiment_growth_6h",
        "sentiment_growth_24h",
        "sentiment_heat_score",
        "sentiment_polarity",
        "sentiment_score",
        "sentiment_updated_at",
    )
    if not any(ind.get(k) is not None for k in keys):
        return {"status": "暂无数据"}
    return {
        "sentiment_mentions_1h": ind.get("sentiment_mentions_1h"),
        "sentiment_mentions_6h": ind.get("sentiment_mentions_6h"),
        "sentiment_mentions_24h": ind.get("sentiment_mentions_24h"),
        "sentiment_growth_1h": ind.get("sentiment_growth_1h"),
        "sentiment_growth_6h": ind.get("sentiment_growth_6h"),
        "sentiment_growth_24h": ind.get("sentiment_growth_24h"),
        "sentiment_heat_score": ind.get("sentiment_heat_score"),
        "sentiment_polarity": ind.get("sentiment_polarity"),
        "sentiment_score": ind.get("sentiment_score"),
        "sentiment_updated_at": ind.get("sentiment_updated_at"),
        "source": "token_indicators",
    }


def render_dd_markdown(ctx: dict[str, Any]) -> str:
    prompt = (ROOT / "prompts" / "dd_report.md").read_text(encoding="utf-8")
    sys_p = ROOT / GROK_SYSTEM_PROMPT_PATH
    sys_snip = sys_p.read_text(encoding="utf-8")[:800] if sys_p.is_file() else ""
    q = ctx.get("quality") or {}
    lines = [
        f"# Deep DD — `{ctx['chain_id']}` `{ctx['token_address']}`",
        f"",
        f"generated_at: `{ctx['generated_at']}`",
        f"",
        f"## Operator / Grok instructions",
        f"",
        prompt.strip(),
        f"",
        f"## System prompt excerpt",
        f"",
        f"```",
        sys_snip,
        f"```",
        f"",
        f"## Assembled context",
        f"",
        f"### Quality",
        f"",
        f"```json",
        json.dumps(q, indent=2, ensure_ascii=False),
        f"```",
        f"",
        f"### 技术面 Technical",
        f"",
        f"```json",
        json.dumps(_technical_block(ctx.get("indicators")), indent=2, ensure_ascii=False, default=str),
        f"```",
        f"",
        f"trend_note: {_trend_note(ctx.get('indicators'))}",
        f"",
        f"### 资金面 Funding (JEV)",
        f"",
        f"```json",
        json.dumps(
            _funding_block(ctx.get("indicators"), ctx.get("jev_signal")),
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        f"```",
        f"",
        f"### 舆情面 Sentiment",
        f"",
        f"```json",
        json.dumps(
            _sentiment_block(ctx.get("indicators")),
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        f"```",
        f"",
        f"### Pairs",
        f"",
        f"```json",
        json.dumps(ctx.get("pairs") or [], indent=2, ensure_ascii=False),
        f"```",
        f"",
        f"### Social (x_watch)",
        f"",
        f"```json",
        json.dumps(ctx.get("social"), indent=2, ensure_ascii=False, default=str),
        f"```",
        f"",
        f"### Recent related alerts",
        f"",
    ]
    for a in ctx.get("alerts") or []:
        lines.append(
            f"- `{a.get('created_at')}` **{a.get('type')}** ({a.get('severity')}) "
            f"{(a.get('message') or '')[:160]}"
        )
    lines += ["", "### History samples", ""]
    for h in ctx.get("history") or []:
        lines.append(f"#### pair `{h.get('pair')}`")
        for pt in (h.get("points") or [])[-12:]:
            lines.append(
                f"- {pt.get('recorded_at')}: price={pt.get('price_usd')} "
                f"liq={pt.get('liquidity_usd')} tier={pt.get('tier')}"
            )
    lines.append("")
    return "\n".join(lines)



def _push_dd_telegram(md: str, *, chain: str, token: str) -> bool:
    """Push DD report head to admin chat via alerts_telegram. Safe if not configured."""
    try:
        from html import escape as _esc

        from alerts_telegram import get_telegram_alerter, telegram_enabled
        from config import TELEGRAM_CHAT_ID, TELEGRAM_CHAT_ID_ADMIN
        from logging_setup import get_logger

        _log = get_logger("grok_dd_report")
        if not telegram_enabled():
            _log.warning("telegram push skipped: TELEGRAM_BOT_TOKEN unset")
            return False
        chat = TELEGRAM_CHAT_ID_ADMIN or TELEGRAM_CHAT_ID
        if not chat:
            _log.warning(
                "telegram push skipped: no TELEGRAM_CHAT_ID_ADMIN / TELEGRAM_CHAT_ID"
            )
            return False
        # Telegram ~4096 limit; send plain head in <pre>
        head = md[:3500].replace("`", "").replace("**", "")
        plain = (
            head.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        title = (
            f"<b>📄 Deep DD</b> <code>{_esc(chain)}</code> "
            f"<code>{_esc(token[:16])}</code>\n"
        )
        ok = get_telegram_alerter().send_message(
            chat, title + f"<pre>{plain}</pre>", parse_mode="HTML"
        )
        if not ok:
            _log.warning("telegram DD push failed (send_message returned False)")
        return bool(ok)
    except Exception as exc:  # noqa: BLE001
        try:
            from logging_setup import get_logger

            get_logger("grok_dd_report").error("telegram DD push error: %s", exc)
        except Exception:  # noqa: BLE001
            pass
        return False



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grok deep DD report for one token")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--chain", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument(
        "--telegram",
        action="store_true",
        default=False,
        help="Push report head to Telegram admin channel (default off; safe if TG unset)",
    )
    parser.add_argument("--out-dir", default=GROK_REPORT_DIR)
    args = parser.parse_args(argv)
    setup_logging()

    with Store(args.db) as store:
        ctx = assemble_token_context(store, args.chain, args.token)
    md = render_dd_markdown(ctx)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    short = args.token[:10]
    md_path = out_dir / f"dd-{args.chain}-{short}-{stamp}.md"
    json_path = out_dir / f"dd-{args.chain}-{short}-{stamp}.json"
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2, default=str) + "\n")

    # --telegram default off; also honors GROK_REPORT_TELEGRAM env. Never fails the report.
    sent = False
    if args.telegram or GROK_REPORT_TELEGRAM:
        sent = _push_dd_telegram(md, chain=args.chain, token=args.token)

    print(f"DD_OK md={md_path} json={json_path} telegram={sent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
