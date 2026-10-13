"""Order event stream (NautilusTrader-style status history).

execution_log 一行 = 一个信号的最新状态；order_events 一行 = 一个状态事件。
状态链：new → validated → submitted → filled，或在任一阶段 rejected。
cancelled 在 EVENTS 里预留，当前没有写入方（没有撤单路径）。

Events:
    new        - 信号进入网关（校验前）
    validated  - 校验通过（未成交也停在这）
    submitted  - 已提交执行器
    filled     - 成交（executed=1）
    rejected   - 校验失败 / purpose 门禁 / 风控拒 / 执行失败（detail 带原因）
    cancelled  - 预留，尚未写入
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

EVENTS = ("new", "validated", "submitted", "filled", "rejected", "cancelled")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_events(db: Path) -> None:
    """创建 order_events 表（幂等）。网关启动时调用一次。"""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS order_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_log_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                event_ts TEXT NOT NULL,
                detail_json TEXT,
                UNIQUE(execution_log_id, event)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def record_event(
    db: Path | None,
    execution_log_id: int,
    event: str,
    detail: Optional[dict[str, Any]] = None,
    *,
    event_ts: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
    commit: Optional[bool] = None,
) -> None:
    """幂等写一条事件；同 (execution_log_id, event) 只写一次（INSERT OR IGNORE）。

    Pass ``conn`` to join the caller's transaction (ledger finalize). When conn
    is omitted, a short-lived connection is opened and committed.
    """
    if event not in EVENTS:
        raise ValueError(f"unknown_event:{event}")
    own = conn is None
    if own:
        if db is None:
            raise ValueError("record_event requires db or conn")
        conn = sqlite3.connect(str(db))
        do_commit = True if commit is None else commit
    else:
        do_commit = False if commit is None else commit
    try:
        conn.execute(
            "INSERT OR IGNORE INTO order_events (execution_log_id, event, event_ts, detail_json) "
            "VALUES (?, ?, ?, ?)",
            (
                execution_log_id,
                event,
                event_ts or _utc_now_iso(),
                json.dumps(detail, default=str) if detail else None,
            ),
        )
        if do_commit:
            conn.commit()
    finally:
        if own and conn is not None:
            conn.close()


def event_chain(db: Path, execution_log_id: int) -> list[dict[str, Any]]:
    """按时间序返回某信号的事件链（查询用）。"""
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT event, event_ts, detail_json FROM order_events "
            "WHERE execution_log_id=? ORDER BY id",
            (execution_log_id,),
        ).fetchall()
        return [
            {
                "event": r[0],
                "event_ts": r[1],
                "detail": json.loads(r[2]) if r[2] else None,
            }
            for r in rows
        ]
    finally:
        conn.close()
