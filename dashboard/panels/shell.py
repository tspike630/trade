"""Pure shell labels for the Fincept-style frame. No UI and no database access."""

from __future__ import annotations

from typing import Literal, assert_never

Tone = Literal["ok", "warn", "muted"]


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
