"""Step C — global position manager (rules over PaperLedger opens).

Default: PM_ENABLED=0 → check_open always allows (pass-through).
Individual rules default off; enable via env. Reject reasons: pm_rejected:<rule>.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from logging_setup import get_logger
from store import norm_address, norm_chain

log = get_logger("execution.position_manager")

# Suggested values when enabling (not applied unless env is set / rule on)
SUGGEST_MAX_TOTAL_PCT = 30.0  # percent points
SUGGEST_MAX_PER_TOKEN_PCT = 10.0
SUGGEST_MAX_OPEN_PER_STRATEGY = 5


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str) -> float | None:
    """Unset / empty / 0 → None (rule off). Values > 1 treated as percent points."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return None
    try:
        v = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return v


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return None
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return v


def _pct_to_fraction(v: float) -> float:
    """30 → 0.30; 0.30 stays 0.30."""
    if v > 1.0:
        return v / 100.0
    return v


def _parse_priority(raw: str | None) -> list[str]:
    if not raw or not str(raw).strip():
        return []
    return [p.strip() for p in str(raw).split(",") if p.strip()]


def _parse_meta(pos: dict[str, Any]) -> dict[str, Any]:
    meta_json = pos.get("meta_json")
    if isinstance(meta_json, dict):
        return meta_json
    if not meta_json:
        return {}
    try:
        parsed = json.loads(meta_json)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def strategy_id_of(pos: dict[str, Any]) -> str:
    meta = _parse_meta(pos)
    sid = meta.get("strategy_id")
    if sid is not None and str(sid).strip():
        return str(sid).strip()
    return "unknown"


def size_pct_of(pos: dict[str, Any], equity_usd: float) -> float:
    """Prefer meta size_pct; else size_usd / equity."""
    meta = _parse_meta(pos)
    if meta.get("size_pct") is not None:
        try:
            return float(meta["size_pct"])
        except (TypeError, ValueError):
            pass
    if equity_usd > 0:
        try:
            return float(pos.get("size_usd") or 0.0) / equity_usd
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0
    return 0.0


@dataclass
class CheckResult:
    allowed: bool
    reason: str | None = None
    rule: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "rule": self.rule,
            "detail": self.detail,
        }


@dataclass
class PositionManagerConfig:
    enabled: bool = False
    token_dedup: bool = False
    strategy_mutex: bool = False
    max_total_pct: float | None = None  # fraction after normalize
    max_per_token_pct: float | None = None
    max_open_per_strategy: int | None = None
    strategy_priority: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "PositionManagerConfig":
        max_total_raw = _env_float("PM_MAX_TOTAL_PCT")
        max_token_raw = _env_float("PM_MAX_PER_TOKEN_PCT")
        return cls(
            enabled=_env_bool("PM_ENABLED", False),
            token_dedup=_env_bool("PM_TOKEN_DEDUP", False),
            strategy_mutex=_env_bool("PM_STRATEGY_MUTEX", False),
            max_total_pct=(
                _pct_to_fraction(max_total_raw) if max_total_raw is not None else None
            ),
            max_per_token_pct=(
                _pct_to_fraction(max_token_raw) if max_token_raw is not None else None
            ),
            max_open_per_strategy=_env_int("PM_MAX_OPEN_PER_STRATEGY"),
            strategy_priority=_parse_priority(
                os.environ.get("PM_STRATEGY_PRIORITY")
            ),
        )


class PositionManager:
    """Global view + buy gate over PaperLedger open positions."""

    def __init__(
        self,
        *,
        ledger: Any | None = None,
        paper_db: str | Path | None = None,
        config: PositionManagerConfig | None = None,
    ) -> None:
        self.config = config or PositionManagerConfig.from_env()
        self._owns_ledger = False
        self.ledger = ledger
        self.paper_db: Path | None = None
        if ledger is not None:
            self.paper_db = Path(getattr(ledger, "path", paper_db or ".")).resolve()
        elif paper_db is not None and str(paper_db).strip():
            p = Path(str(paper_db)).expanduser()
            self.paper_db = p if p.is_absolute() else (Path.cwd() / p).resolve()

    def _ensure_ledger(self) -> Any | None:
        if self.ledger is not None:
            return self.ledger
        if self.paper_db is None:
            return None
        from paper_trade import PaperLedger

        self.ledger = PaperLedger(self.paper_db)
        self._owns_ledger = True
        return self.ledger

    def close(self) -> None:
        if self._owns_ledger and self.ledger is not None:
            self.ledger.close()
            self.ledger = None
            self._owns_ledger = False

    def __enter__(self) -> "PositionManager":
        return self

    def __exit__(self, *a: object) -> None:
        self.close()

    def _priority_rank(self, strategy_id: str) -> int | None:
        """Lower index = higher priority. None if not on whitelist."""
        pri = self.config.strategy_priority
        if not pri:
            return None
        try:
            return pri.index(strategy_id)
        except ValueError:
            return None  # not on list → lowest

    def _wins_arbitration(self, challenger: str, holders: list[str]) -> bool:
        """True if challenger may override existing holders.

        Priority whitelist: challenger must be on list and strictly better
        (lower index) than every holder that is also ranked; unlisted holders
        count as worst. If no whitelist → FCFS → challenger never wins.
        """
        if not holders:
            return True
        pri = self.config.strategy_priority
        if not pri:
            return False  # first-come-first-served: existing wins
        ch_rank = self._priority_rank(challenger)
        if ch_rank is None:
            return False
        for h in holders:
            h_rank = self._priority_rank(h)
            # unlisted holder → treat as very low priority (large rank)
            effective = h_rank if h_rank is not None else 10**9
            if effective <= ch_rank:
                return False
        return True

    def _same_token(
        self,
        pos: dict[str, Any],
        *,
        chain_id: str,
        token_address: str | None,
        pair_address: str | None,
    ) -> bool:
        if norm_chain(str(pos.get("chain_id") or "")) != chain_id:
            return False
        # Prefer token match when both sides have token
        pos_tok = pos.get("token_address")
        if token_address and pos_tok:
            return norm_address(str(pos_tok)) == token_address
        # Token missing → pair match
        if pair_address and pos.get("pair_address"):
            return norm_address(str(pos["pair_address"])) == pair_address
        # Incoming token but pos lacks token: fall back to pair
        if token_address and not pos_tok and pair_address and pos.get("pair_address"):
            return norm_address(str(pos["pair_address"])) == pair_address
        return False

    def snapshot(self) -> dict[str, Any]:
        """Global open-position view: by token / strategy / totals.

        Safe when PM is off — read-only overview for daily_check.
        """
        ledger = self._ensure_ledger()
        empty = {
            "paper_db": str(self.paper_db) if self.paper_db else None,
            "pm_enabled": self.config.enabled,
            "equity_usd": None,
            "cash_usd": None,
            "open_count": 0,
            "total_size_pct": 0.0,
            "total_size_usd": 0.0,
            "by_token": {},
            "by_strategy": {},
            "positions": [],
            "rules": self._rules_status(),
        }
        if ledger is None:
            return empty

        try:
            eq = ledger.equity()
        except Exception as exc:  # noqa: BLE001
            empty["error"] = str(exc)
            return empty

        equity_usd = float(eq.get("equity_usd") or 0.0)
        opens = ledger.list_open()
        by_token: dict[str, dict[str, Any]] = {}
        by_strategy: dict[str, dict[str, Any]] = {}
        positions: list[dict[str, Any]] = []
        total_size_pct = 0.0
        total_size_usd = 0.0

        for p in opens:
            sid = strategy_id_of(p)
            sp = size_pct_of(p, equity_usd)
            size_usd = float(p.get("size_usd") or 0.0)
            total_size_pct += sp
            total_size_usd += size_usd
            chain = norm_chain(str(p.get("chain_id") or ""))
            tok = (
                norm_address(str(p["token_address"]))
                if p.get("token_address")
                else None
            )
            pair = (
                norm_address(str(p["pair_address"]))
                if p.get("pair_address")
                else None
            )
            token_key = f"{chain}|{tok}" if tok else f"{chain}|pair:{pair}"
            bt = by_token.setdefault(
                token_key,
                {
                    "chain_id": chain,
                    "token_address": tok,
                    "pair_address": pair,
                    "count": 0,
                    "size_pct": 0.0,
                    "size_usd": 0.0,
                    "strategies": [],
                },
            )
            bt["count"] += 1
            bt["size_pct"] += sp
            bt["size_usd"] += size_usd
            if sid not in bt["strategies"]:
                bt["strategies"].append(sid)

            bs = by_strategy.setdefault(
                sid,
                {"count": 0, "size_pct": 0.0, "size_usd": 0.0},
            )
            bs["count"] += 1
            bs["size_pct"] += sp
            bs["size_usd"] += size_usd

            positions.append(
                {
                    "id": p.get("id"),
                    "chain_id": chain,
                    "token_address": tok,
                    "pair_address": pair,
                    "size_usd": size_usd,
                    "size_pct": sp,
                    "strategy_id": sid,
                    "opened_at": p.get("opened_at"),
                    "entry_price": p.get("entry_price"),
                }
            )

        return {
            "paper_db": str(self.paper_db) if self.paper_db else None,
            "pm_enabled": self.config.enabled,
            "equity_usd": equity_usd,
            "cash_usd": float(eq.get("cash_usd") or 0.0),
            "open_count": len(opens),
            "total_size_pct": total_size_pct,
            "total_size_usd": total_size_usd,
            "by_token": by_token,
            "by_strategy": by_strategy,
            "positions": positions,
            "rules": self._rules_status(),
        }

    def _rules_status(self) -> dict[str, Any]:
        c = self.config
        return {
            "PM_ENABLED": c.enabled,
            "PM_TOKEN_DEDUP": c.token_dedup,
            "PM_STRATEGY_MUTEX": c.strategy_mutex,
            "PM_MAX_TOTAL_PCT": c.max_total_pct,
            "PM_MAX_PER_TOKEN_PCT": c.max_per_token_pct,
            "PM_MAX_OPEN_PER_STRATEGY": c.max_open_per_strategy,
            "PM_STRATEGY_PRIORITY": list(c.strategy_priority),
        }

    def check_open(self, sig: dict[str, Any]) -> CheckResult:
        """Gate a buy. Non-buy → allow. PM_ENABLED=0 → allow (pass-through)."""
        action = str(sig.get("action") or "").strip().lower()
        if action and action != "buy":
            return CheckResult(allowed=True, detail={"skipped": "not_buy"})

        if not self.config.enabled:
            return CheckResult(allowed=True, detail={"passthrough": True})

        ledger = self._ensure_ledger()
        if ledger is None:
            # No book to check — allow (cannot enforce without paper state)
            return CheckResult(
                allowed=True,
                detail={"warning": "no_paper_ledger"},
            )

        opens = ledger.list_open()
        try:
            equity_usd = float(ledger.equity().get("equity_usd") or 0.0)
        except Exception:  # noqa: BLE001
            equity_usd = 0.0

        chain_id = norm_chain(str(sig.get("chain_id") or ""))
        token_address = (
            norm_address(str(sig["token_address"]))
            if sig.get("token_address")
            else None
        )
        pair_address = (
            norm_address(str(sig["pair_address"]))
            if sig.get("pair_address")
            else None
        )
        strategy_id = str(sig.get("strategy_id") or "unknown").strip() or "unknown"
        try:
            new_size_pct = float(sig.get("size_pct") or 0.0)
        except (TypeError, ValueError):
            new_size_pct = 0.0

        # --- PM_TOKEN_DEDUP ---
        if self.config.token_dedup:
            conflicts = [
                p
                for p in opens
                if self._same_token(
                    p,
                    chain_id=chain_id,
                    token_address=token_address,
                    pair_address=pair_address,
                )
            ]
            if conflicts:
                holders = [strategy_id_of(p) for p in conflicts]
                arb = "priority" if self.config.strategy_priority else "fcfs"
                if self._wins_arbitration(strategy_id, holders):
                    # priority whitelist: challenger outranks holders → allow
                    pass
                else:
                    return CheckResult(
                        allowed=False,
                        reason="pm_rejected:token_dedup",
                        rule="token_dedup",
                        detail={
                            "holders": holders,
                            "arbitration": arb,
                            "challenger": strategy_id,
                        },
                    )

        # --- PM_STRATEGY_MUTEX ---
        if self.config.strategy_mutex:
            other_holders = sorted(
                {
                    strategy_id_of(p)
                    for p in opens
                    if strategy_id_of(p) != strategy_id
                }
            )
            if other_holders and not self._wins_arbitration(
                strategy_id, other_holders
            ):
                return CheckResult(
                    allowed=False,
                    reason="pm_rejected:strategy_mutex",
                    rule="strategy_mutex",
                    detail={
                        "other_strategies": other_holders,
                        "arbitration": (
                            "priority"
                            if self.config.strategy_priority
                            else "fcfs"
                        ),
                        "challenger": strategy_id,
                    },
                )

        # --- PM_MAX_OPEN_PER_STRATEGY ---
        if self.config.max_open_per_strategy is not None:
            mine = sum(1 for p in opens if strategy_id_of(p) == strategy_id)
            if mine >= self.config.max_open_per_strategy:
                return CheckResult(
                    allowed=False,
                    reason="pm_rejected:max_open_per_strategy",
                    rule="max_open_per_strategy",
                    detail={
                        "open_count": mine,
                        "limit": self.config.max_open_per_strategy,
                        "strategy_id": strategy_id,
                    },
                )

        # --- PM_MAX_PER_TOKEN_PCT ---
        if self.config.max_per_token_pct is not None:
            same = [
                p
                for p in opens
                if self._same_token(
                    p,
                    chain_id=chain_id,
                    token_address=token_address,
                    pair_address=pair_address,
                )
            ]
            existing_pct = sum(size_pct_of(p, equity_usd) for p in same)
            if existing_pct + new_size_pct > self.config.max_per_token_pct + 1e-12:
                return CheckResult(
                    allowed=False,
                    reason="pm_rejected:max_per_token_pct",
                    rule="max_per_token_pct",
                    detail={
                        "existing_pct": existing_pct,
                        "new_pct": new_size_pct,
                        "limit": self.config.max_per_token_pct,
                    },
                )

        # --- PM_MAX_TOTAL_PCT ---
        if self.config.max_total_pct is not None:
            total_pct = sum(size_pct_of(p, equity_usd) for p in opens)
            if total_pct + new_size_pct > self.config.max_total_pct + 1e-12:
                return CheckResult(
                    allowed=False,
                    reason="pm_rejected:max_total_pct",
                    rule="max_total_pct",
                    detail={
                        "existing_pct": total_pct,
                        "new_pct": new_size_pct,
                        "limit": self.config.max_total_pct,
                    },
                )

        return CheckResult(
            allowed=True,
            detail={
                "open_count": len(opens),
                "strategy_id": strategy_id,
                "new_size_pct": new_size_pct,
            },
        )


def resolve_paper_db_for_pm(
    explicit: str | Path | None = None,
) -> Path | None:
    if explicit is not None and str(explicit).strip():
        p = Path(str(explicit)).expanduser()
        return p if p.is_absolute() else (Path.cwd() / p).resolve()
    env = (
        os.environ.get("EXECUTION_PAPER_DB", "").strip()
        or os.environ.get("PAPER_DB_PATH", "").strip()
        or os.environ.get("PM_PAPER_DB", "").strip()
    )
    if not env:
        return None
    p = Path(env).expanduser()
    return p if p.is_absolute() else (Path.cwd() / p).resolve()
