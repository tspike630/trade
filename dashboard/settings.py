"""Environment and kill-switch reads.

Nothing here writes config, flips a gate, or returns secret values.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

HOST = "127.0.0.1"
DEFAULT_PORT = 8081
# Public confirmation phrase from the crawler's live_trade module. Not a credential.
CONFIRM_PHRASE = "I_UNDERSTAND_THE_RISKS"

ConfirmState = Literal["matched", "missing", "mismatch"]
_TRUE = {"1", "true", "yes", "on"}
log = logging.getLogger("dashboard.settings")


def api_secret() -> str | None:
    raw = os.environ.get("API_SECRET", "").strip()
    return raw or None


def allow_insecure() -> bool:
    """Match api_server: only the exact value ``1`` opens unauthenticated mode."""
    return os.environ.get("ALLOW_INSECURE_API", "").strip() == "1"


def env_flag(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in _TRUE


def db_root() -> Path:
    raw = os.environ.get("DASHBOARD_DB_ROOT", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    # dashboard/settings.py -> package dir -> project root (parent of dashboard/)
    return Path(__file__).resolve().parent.parent


def bind_host() -> str:
    """Always the loopback address. A public bind is ignored."""
    requested = os.environ.get("DASHBOARD_HOST", "").strip()
    if requested and requested not in {HOST, "localhost"}:
        log.warning("ignoring DASHBOARD_HOST=%s; binding %s only", requested, HOST)
    return HOST


def port() -> int:
    raw = os.environ.get("DASHBOARD_PORT", str(DEFAULT_PORT)).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_PORT
    if not 1 <= value <= 65535:
        return DEFAULT_PORT
    return value


def refresh_seconds() -> float:
    raw = os.environ.get("DASHBOARD_REFRESH_SEC", "45").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 45.0
    if value < 30:
        return 30.0
    if value > 60:
        return 60.0
    return value


def stale_seconds() -> float:
    raw = os.environ.get("HEARTBEAT_STALE_SEC", "900").strip()
    try:
        value = float(raw)
    except ValueError:
        return 900.0
    if value <= 0:
        return 900.0
    return value


def kill_switch_path() -> Path:
    raw = os.environ.get("LIVE_KILL_SWITCH_PATH", "").strip() or "LIVE_TRADING_DISABLED"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = db_root() / path
    return path


def confirm_state() -> ConfirmState:
    raw = os.environ.get("LIVE_TRADING_CONFIRM", "").strip()
    if not raw:
        return "missing"
    if raw == CONFIRM_PHRASE:
        return "matched"
    return "mismatch"


@dataclass(frozen=True)
class GateStatus:
    paper_enabled: bool
    pm_enabled: bool
    live_env_enabled: bool
    kill_switch_present: bool
    kill_switch_name: str
    confirm_state: ConfirmState
    live_effective: bool
    live_block_reason: str


def read_gates() -> GateStatus:
    """Display-only snapshot. Re-read on each refresh so a new kill-switch file shows up."""
    paper = env_flag("EXECUTION_PAPER_ENABLED")
    pm = env_flag("PM_ENABLED")
    live_env = env_flag("LIVE_TRADING_ENABLED")
    switch = kill_switch_path()
    try:
        present = switch.is_file()
    except OSError:
        present = False
    state = confirm_state()
    if not live_env:
        reason = "环境变量关闭"
        effective = False
    elif present:
        reason = "杀开关文件存在"
        effective = False
    elif state != "matched":
        reason = "确认短语未匹配"
        effective = False
    else:
        reason = ""
        effective = True
    return GateStatus(
        paper_enabled=paper,
        pm_enabled=pm,
        live_env_enabled=live_env,
        kill_switch_present=present,
        kill_switch_name=switch.name,
        confirm_state=state,
        live_effective=effective,
        live_block_reason=reason,
    )


def resolve_db_file(env_names: tuple[str, ...], default_name: str, root: Path | None = None) -> Path:
    base = root if root is not None else db_root()
    for name in env_names:
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = base / path
        return path
    return base / default_name
