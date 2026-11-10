"""M6 LLM tool whitelist. Read-only wrappers over DataStore. No gate/order/param tools."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from dashboard.data import DataStore, ExecutionRow, MarketRow, NewsItem, PositionRow, SignalRow

EMPTY_DATA = "暂无数据"

TOOL_NAMES = (
    "get_positions",
    "get_signals",
    "get_indicators",
    "get_market",
    "get_executions",
    "get_news",
)

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_positions",
            "description": "查询当前模拟盘持仓及浮盈亏。可选按链过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "chain": {
                        "type": "string",
                        "description": "链 id，如 solana / ethereum / bsc / base；省略则全部",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_signals",
            "description": "查询最近交易信号及执行结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "条数，默认 20，最大 50",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_indicators",
            "description": "查询指标（JEV/技术/舆情）。可按链或 token 过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "chain": {"type": "string", "description": "链 id"},
                    "token": {"type": "string", "description": "token 地址（可截断匹配）"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market",
            "description": "查询某链行情 Top（按 JEV 降序）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "chain": {"type": "string", "description": "链 id；省略则全部"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_executions",
            "description": "查询执行记录（校验/成交/执行器摘要）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "条数，默认 20，最大 50",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_news",
            "description": "查询最近新闻事件（旁路库）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "条数，默认 10，最大 30",
                    }
                },
            },
        },
    },
]


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _fmt(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _pos_dict(row: PositionRow) -> dict[str, Any]:
    return {
        "token": row.symbol or row.token_address or "—",
        "chain": row.chain_id or "—",
        "size_usd": row.size_usd if row.size_usd is not None else row.mtm_usd,
        "entry_price": row.entry_price,
        "mark_price": row.mark_price,
        "pnl_pct": row.pnl_pct,
        "pnl_usd": row.pnl_usd,
        "status": row.status,
    }


def _sig_dict(row: SignalRow) -> dict[str, Any]:
    return {
        "id": row.decision_id,
        "strategy": row.strategy_id,
        "action": row.action,
        "token": row.token_address,
        "chain": row.chain_id,
        "risk_passed": row.validation_ok,
        "executed": row.executed,
        "reject_reason": row.validation_errors or "",
        "executor": row.executor,
    }


def _ind_dict(row: MarketRow) -> dict[str, Any]:
    return {
        "token": row.token_address,
        "chain": row.chain_id,
        "jev_strength": row.jev_strength,
        "coverage": row.coverage,
        "change_5m": row.change_5m,
        "change_1h": row.change_1h,
        "change_4h": row.change_4h,
        "volatility": row.volatility,
        "sentiment": row.sentiment or "—",
        "tier": row.tier,
        "tech_at": row.tech_at or "",
        "fresh_at": row.fresh_at or "",
        "fresh_state": row.fresh_state,
    }


def _mkt_dict(row: MarketRow) -> dict[str, Any]:
    return {
        "token": row.token_address,
        "chain": row.chain_id,
        "price": row.price_usd,
        "liquidity": row.liquidity_usd,
        "change_1h": row.change_1h,
        "jev_strength": row.jev_strength,
        "tier": row.tier,
    }


def _exe_dict(row: ExecutionRow) -> dict[str, Any]:
    return {
        "signal_id": row.decision_log_id,
        "validation_ok": row.validation_ok,
        "validation_errors": row.validation_errors or "",
        "executed": row.executed,
        "executor": row.executor,
        "note": row.receipt_summary,
        "book_state": row.book_label,
        "filled_qty": row.filled_text,
        "order_qty": row.order_text,
        "protection": row.protection_text,
        "action": row.action,
        "token": row.token_address,
        "ts": row.ts,
    }


def _news_dict(row: NewsItem) -> dict[str, Any]:
    return {
        "source": row.source,
        "title": row.title,
        "url": row.url,
        "published_at": row.published_at,
        "onchain_status": row.onchain_status,
        "source_tier": row.source_tier,
    }


def build_tools(store: DataStore) -> dict[str, Callable[..., str]]:
    """Return name -> callable. Each callable is side-effect free and returns text."""

    def get_positions(chain: str | None = None) -> str:
        result = store.open_positions()
        if not result.available:
            return EMPTY_DATA
        rows = list(result.data.rows)
        if chain:
            want = str(chain).strip().lower()
            rows = [r for r in rows if (r.chain_id or "").lower() == want]
        if not rows:
            return EMPTY_DATA
        return _fmt([_pos_dict(r) for r in rows])

    def get_signals(limit: int | None = None) -> str:
        n = _clamp(limit, 1, 50, 20)
        result = store.recent_signals(n)
        if not result.available:
            return EMPTY_DATA
        rows = list(result.data.rows)
        if not rows:
            return EMPTY_DATA
        return _fmt([_sig_dict(r) for r in rows])

    def get_indicators(chain: str | None = None, token: str | None = None) -> str:
        result = store.market_rows(chain=chain or None, limit=100)
        if not result.available:
            return EMPTY_DATA
        rows: list[MarketRow] = list(result.data or [])
        if token:
            needle = str(token).strip().lower()
            rows = [r for r in rows if needle in (r.token_address or "").lower()]
        if not rows:
            return EMPTY_DATA
        return _fmt([_ind_dict(r) for r in rows[:40]])

    def get_market(chain: str | None = None) -> str:
        result = store.market_rows(chain=chain or None, limit=80)
        if not result.available:
            return EMPTY_DATA
        rows: list[MarketRow] = list(result.data or [])
        if not rows:
            return EMPTY_DATA
        rows.sort(
            key=lambda r: (r.jev_strength is not None, r.jev_strength or 0.0),
            reverse=True,
        )
        return _fmt([_mkt_dict(r) for r in rows[:30]])

    def get_executions(limit: int | None = None) -> str:
        n = _clamp(limit, 1, 50, 20)
        result = store.recent_executions(n)
        if not result.available:
            return EMPTY_DATA
        rows = list(result.data or [])
        if not rows:
            return EMPTY_DATA
        return _fmt([_exe_dict(r) for r in rows])

    def get_news(limit: int | None = None) -> str:
        n = _clamp(limit, 1, 30, 10)
        result = store.latest_news(n)
        if not result.available:
            return EMPTY_DATA
        rows = list(result.data or [])
        if not rows:
            return EMPTY_DATA
        return _fmt([_news_dict(r) for r in rows])

    return {
        "get_positions": get_positions,
        "get_signals": get_signals,
        "get_indicators": get_indicators,
        "get_market": get_market,
        "get_executions": get_executions,
        "get_news": get_news,
    }


def run_tool(tools: dict[str, Callable[..., str]], name: str, arguments: Any) -> str:
    fn = tools.get(name)
    if fn is None:
        return EMPTY_DATA
    args: dict[str, Any]
    if arguments is None or arguments == "":
        args = {}
    elif isinstance(arguments, dict):
        args = arguments
    elif isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
            args = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            args = {}
    else:
        args = {}
    # Drop unknown keys; never pass through unexpected callables.
    allowed = set(fn.__code__.co_varnames[: fn.__code__.co_argcount])
    clean = {k: v for k, v in args.items() if k in allowed}
    try:
        return fn(**clean)
    except Exception:
        return EMPTY_DATA
