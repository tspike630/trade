"""Display formatters. UI strings stay in the panels; this module only formats values."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, assert_never

from dashboard.parsing import parse_ts
from dashboard.settings import ConfirmState

Liveness = Literal["alive", "stale", "unknown"]


def liveness_label(state: Liveness) -> str:
    match state:
        case "alive":
            return "存活"
        case "stale":
            return "停滞"
        case "unknown":
            return "未知"
        case _ as unreachable:
            assert_never(unreachable)


def confirm_label(state: ConfirmState) -> str:
    match state:
        case "matched":
            return "已匹配"
        case "missing":
            return "未设置"
        case "mismatch":
            return "未匹配"
        case _ as unreachable:
            assert_never(unreachable)


def fmt_ts(value: str | None) -> str:
    if not value:
        return "—"
    try:
        stamp = parse_ts(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        return text[:32] if text else "—"
    return stamp.strftime("%m-%d %H:%M:%S")


def fmt_usd(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "—"
    number = f"{abs(value):,.2f}"
    if signed:
        if value > 0:
            return f"+${number}"
        if value < 0:
            return f"-${number}"
        return f"${number}"
    if value < 0:
        return f"-${number}"
    return f"${number}"


def fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def fmt_liq(value: float | None) -> str:
    if value is None:
        return "—"
    magnitude = abs(value)
    sign = "-" if value < 0 else ""
    if magnitude >= 1_000_000:
        return f"{sign}${magnitude / 1_000_000:.2f}M"
    if magnitude >= 1_000:
        return f"{sign}${magnitude / 1_000:.1f}K"
    return f"{sign}${magnitude:.0f}"


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}%"


def fmt_pct_raw(value: float | None) -> str:
    """TP/SL as stored, with a percent sign and no rescaling."""
    if value is None:
        return "—"
    return f"{value:g}%"


def fmt_ratio(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.2f}%"


def fmt_rate(rate: float | None, numerator: int, denominator: int) -> str:
    if denominator <= 0 or rate is None:
        return "—"
    return f"{rate * 100:.1f}%（{numerator}/{denominator}）"


def fmt_strength(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.3f}"


def fmt_coverage(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}%"


def fmt_hold(seconds: float | None, max_hold_min: float | None = None) -> str:
    if seconds is None:
        base = "—"
    else:
        whole = int(max(0, seconds))
        days, rem = divmod(whole, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days:
            base = f"{days}天{hours}小时"
        elif hours:
            base = f"{hours}小时{minutes}分"
        else:
            base = f"{minutes}分"
    if max_hold_min is None:
        return base
    cap = f"{max_hold_min:g}分"
    if base == "—":
        return f"—（上限 {cap}）"
    return f"{base}（上限 {cap}）"


def fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "未知"
    whole = int(max(0, seconds))
    if whole < 90:
        return f"{whole}秒前"
    minutes = whole // 60
    if minutes < 90:
        return f"{minutes}分钟前"
    hours = whole // 3600
    if hours < 48:
        return f"{hours}小时前"
    return f"{whole // 86400}天前"


def fmt_pnl(pct: float | None, usd: float | None) -> tuple[str, str]:
    if pct is None and usd is None:
        return "—", "pnl-flat"
    parts: list[str] = []
    if pct is not None:
        parts.append(fmt_pct(pct))
    if usd is not None:
        parts.append(fmt_usd(usd, signed=True))
    probe = usd if usd is not None else pct
    if probe is None or probe == 0:
        klass = "pnl-flat"
    elif probe > 0:
        klass = "pnl-pos"
    else:
        klass = "pnl-neg"
    return " · ".join(parts), klass


def short_addr(value: str | None, *, symbol: str | None = None) -> str:
    text = (value or "").strip()
    if not text:
        addr = "—"
    elif len(text) <= 12:
        addr = text
    else:
        addr = f"{text[:4]}…{text[-4:]}"
    if symbol and symbol.strip():
        return f"{symbol.strip()} · {addr}"
    return addr


def flag_text(value: int | None, *, yes: str, no: str) -> tuple[str, str]:
    if value is None:
        return "—", "muted"
    if value == 1:
        return yes, "ok"
    return no, "bad"


def clock_utc() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
