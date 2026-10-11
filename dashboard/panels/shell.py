"""Pure shell labels for the Fincept-style frame. No UI and no database access."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, assert_never

Tone = Literal["ok", "warn", "muted"]
HostName = Literal["OVERVIEW", "SIGNALS", "EXECUTION", "MARKETS", "AI CHAT"]
TopKind = Literal["live", "placeholder"]


@dataclass(frozen=True)
class TopTab:
    """One skeleton top-bar slot. Placeholders are visible and do not navigate."""

    label: str
    kind: TopKind


# Order matches dashboard_grid_skeleton_v2.html. Live slots are panels that
# already exist. The rest stay in the row as muted placeholders.
TOP_TABS: tuple[TopTab, ...] = (
    TopTab("DASHBOARD", "live"),
    TopTab("MARKETS", "live"),
    TopTab("CRYPTO", "placeholder"),
    TopTab("PORTFOLIO", "placeholder"),
    TopTab("SIGNALS", "live"),
    TopTab("EXECUTION", "live"),
    TopTab("NEWS", "placeholder"),
    TopTab("AI CHAT", "live"),
    TopTab("BACKTEST", "placeholder"),
    TopTab("ALGO", "placeholder"),
    TopTab("NODES", "placeholder"),
    TopTab("QUANT LAB", "placeholder"),
    TopTab("SETTINGS", "placeholder"),
)


def is_live_top_tab(label: str) -> bool:
    return any(tab.label == label and tab.kind == "live" for tab in TOP_TABS)


def select_top_tab(current: str, key: str) -> str:
    """Switch only among wired tabs. Placeholders and unknown labels stay put."""
    if key == current or not is_live_top_tab(key):
        return current
    return key


def host_visible(tab: str, sub: str, host: HostName) -> bool:
    """Content host for the current top tab. Placeholder labels never map here."""
    match host:
        case "OVERVIEW":
            return tab == "DASHBOARD" and sub == "OVERVIEW"
        case "SIGNALS":
            return tab == "SIGNALS" or (tab == "DASHBOARD" and sub == "SIGNALS")
        case "EXECUTION":
            return tab == "EXECUTION" or (tab == "DASHBOARD" and sub == "EXECUTION")
        case "MARKETS":
            return tab == "MARKETS"
        case "AI CHAT":
            return tab == "AI CHAT"
        case _ as unreachable:
            assert_never(unreachable)


def panel_group(panel_id: str) -> Literal["overview", "signals", "execution"]:
    """Map a registry id onto a DASHBOARD sub-tab. Unknown ids stay on OVERVIEW."""
    if panel_id == "signals":
        return "signals"
    if panel_id == "execution":
        return "execution"
    return "overview"


def heartbeat_summary(states: list[str]) -> tuple[str, Tone]:
    """Compress process liveness into the top-bar / status-bar heartbeat."""
    if not states:
        return "进程 —", "muted"
    alive = sum(1 for state in states if state == "alive")
    text = f"进程 {alive}/{len(states)} 存活"
    if alive:
        return text, "ok"
    if any(state == "stale" for state in states):
        return text, "warn"
    return text, "muted"


def live_caption(tone: Tone) -> str:
    match tone:
        case "ok":
            return "● 实时"
        case "warn":
            return "● 停滞"
        case "muted":
            return "● 未知"
        case _ as unreachable:
            assert_never(unreachable)


def db_status_line(items: list[tuple[str, str, bool]]) -> str:
    ready = sum(1 for _label, _name, ok in items if ok)
    missing = [label for label, _name, ok in items if not ok]
    base = f"库 {ready}/{len(items)}"
    if missing:
        return f"{base} 缺失:{','.join(missing)}"
    return f"{base} 就绪"


def refresh_age_label(seconds: int | None) -> str:
    if seconds is None:
        return "刷新 —"
    whole = max(0, int(seconds))
    return f"刷新 {whole}s 前"


def session_clock(seconds: int) -> str:
    whole = max(0, int(seconds))
    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)
    return f"会话 {hours:02d}:{minutes:02d}:{secs:02d}"
