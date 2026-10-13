"""Environment and kill-switch reads.

Nothing here writes config, flips a gate, or returns secret values.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, assert_never

HOST = "127.0.0.1"
DEFAULT_PORT = 8081
# Public confirmation phrase from the crawler's live_trade module. Not a credential.
CONFIRM_PHRASE = "I_UNDERSTAND_THE_RISKS"
# Dashboard override first, then the path the execution gateway actually writes.
EXECUTION_DB_ENV: tuple[str, ...] = ("DASHBOARD_EXECUTION_DB", "EXECUTION_LEDGER_PATH")
# API heartbeat in crawler/config.py is 120s. Gateway liveness uses this,
# not the 900s window shared by the other dashboard probes.
_GATEWAY_STALE_DEFAULT = 120.0

ConfirmState = Literal["matched", "missing", "mismatch"]
GateOrigin = Literal["网关回执", "看板进程"]
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
    """Liveness window for crawler, indicators, JEV, and the decision engine.

    Default stays 900s. Do not collapse this to the API's 120s heartbeat:
    those probes update slower than the execution gateway.
    """
    raw = os.environ.get("HEARTBEAT_STALE_SEC", "900").strip()
    try:
        value = float(raw)
    except ValueError:
        return 900.0
    if value <= 0:
        return 900.0
    return value


def gateway_stale_seconds() -> float:
    """Stricter window for the execution gateway and its gate receipt.

    Independent of ``HEARTBEAT_STALE_SEC``. Default matches the API heartbeat
    (120s) so a stopped gateway does not stay "alive" for fifteen minutes.
    """
    raw = os.environ.get("GATEWAY_HEARTBEAT_STALE_SEC", str(int(_GATEWAY_STALE_DEFAULT))).strip()
    try:
        value = float(raw)
    except ValueError:
        return _GATEWAY_STALE_DEFAULT
    if value <= 0:
        return _GATEWAY_STALE_DEFAULT
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
    paper_origin: GateOrigin
    pm_origin: GateOrigin
    gateway_stale: bool
    exchange_url_set: bool

    def origin_hint(self, which: Literal["paper", "pm"]) -> str:
        """Label the light so a process env value is never mistaken for the gateway."""
        if which == "paper":
            origin = self.paper_origin
        elif which == "pm":
            origin = self.pm_origin
        else:
            assert_never(which)
        if self.gateway_stale:
            return f"网关陈旧 · {origin}"
        return origin


@dataclass(frozen=True)
class _ReceiptFlags:
    found: bool
    ts: datetime | None
    paper: bool | None
    pm: bool | None


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _TRUE:
            return True
        if low in {"0", "false", "no", "off"}:
            return False
    return None


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _apply_receipt(
    env_paper: bool,
    env_pm: bool,
    receipt: _ReceiptFlags,
) -> tuple[bool, GateOrigin, bool, GateOrigin, bool]:
    """Prefer a fresh receipt. A stale one must not keep an old ON/OFF lit."""
    if not receipt.found:
        return env_paper, "看板进程", env_pm, "看板进程", False
    if receipt.ts is None:
        age: float | None = None
    else:
        age = (datetime.now(timezone.utc) - receipt.ts).total_seconds()
    fresh = age is not None and age <= gateway_stale_seconds()
    if not fresh:
        return env_paper, "看板进程", env_pm, "看板进程", True
    paper = env_paper if receipt.paper is None else receipt.paper
    pm = env_pm if receipt.pm is None else receipt.pm
    paper_origin: GateOrigin = "看板进程" if receipt.paper is None else "网关回执"
    pm_origin: GateOrigin = "看板进程" if receipt.pm is None else "网关回执"
    return paper, paper_origin, pm, pm_origin, False


def exchange_url_set() -> bool:
    """Same gate as live_trade.live_gates_open: URL must be non-empty. Value is not shown."""
    return bool(os.environ.get("LIVE_EXCHANGE_API_URL", "").strip())


def read_gates() -> GateStatus:
    """Display-only snapshot. Re-read on each refresh so a new kill-switch file shows up.

    Paper and PM follow the newest ``execution_log.receipt_json`` when its ``ts``
    is inside the gateway heartbeat window. Older receipts are labeled stale and
    the lights fall back to this process's env, labeled ``看板进程``.
    Live also requires ``LIVE_EXCHANGE_API_URL``, matching ``live_trade``.

    The receipt read lives in ``dashboard.data`` (the only business-db opener).
    That module imports this one, so the call is local to avoid a cycle.
    """
    # Circular import: dashboard.data imports dashboard.settings at module load.
    from dashboard.data import latest_gate_receipt

    env_paper = env_flag("EXECUTION_PAPER_ENABLED")
    env_pm = env_flag("PM_ENABLED")
    paper, paper_origin, pm, pm_origin, gateway_stale = _apply_receipt(
        env_paper,
        env_pm,
        latest_gate_receipt(),
    )
    live_env = env_flag("LIVE_TRADING_ENABLED")
    switch = kill_switch_path()
    try:
        present = switch.is_file()
    except OSError:
        present = False
    state = confirm_state()
    url_set = exchange_url_set()
    if not live_env:
        reason = "环境变量关闭"
        effective = False
    elif present:
        reason = "杀开关文件存在"
        effective = False
    elif state != "matched":
        reason = "确认短语未匹配"
        effective = False
    elif not url_set:
        reason = "交易所 URL 未设置"
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
        paper_origin=paper_origin,
        pm_origin=pm_origin,
        gateway_stale=gateway_stale,
        exchange_url_set=url_set,
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
