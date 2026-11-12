"""Read-only spot book labels for the dashboard.

The current execution ledger keeps the first receipt on ``execution_log``.
Later fills are written on ``binance_spot_positions`` by
``confirm_binance_spot_qty`` (status ``open`` plus base ``quantity``) and, when
a native OCO is verified, by ``update_binance_spot_position``. This module
only renders fields that are already on those rows. It does not invent a
fill, a cancel, or an OCO.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

_ACCEPTED_STATUSES = frozenset(
    {
        "qty_unconfirmed",
        "open",
        "oco_working",
        "oco_unknown",
        "oco_failed",
        "closed",
        "closed_canceled",
        "closed_dust",
        "exit_submitted",
        "oco_filled",
    }
)
_OCO_STATUSES = frozenset({"oco_working", "oco_unknown"})


@dataclass(frozen=True)
class SpotBookView:
    """One execution row after the spot book is applied. Empty strings are unused."""

    label: str
    tone: str
    filled_text: str
    order_text: str
    protection_text: str
    summary: str
    is_rejection: bool
    snapshot_only: bool = False


def spot_book_view(
    receipt: Mapping[str, Any] | None,
    log_status: str | None,
    position: Mapping[str, Any] | None,
    *,
    position_table: bool,
) -> SpotBookView | None:
    """Return the spot label for one receipt, or None when the row is not spot.

    ``position_table`` is whether ``binance_spot_positions`` exists. A missing
    table can only show the first-ack arm inside the receipt. A present table
    with no row is not treated as that arm: the insert did not stick.
    """
    body = receipt if isinstance(receipt, Mapping) else {}
    if not _is_spot_receipt(body):
        return None
    order_text, order_qty = _order_quantity(body)
    if position_table and isinstance(position, Mapping):
        viewed = _from_row(position, order_text, order_qty, snapshot_only=False)
        if viewed is not None:
            return viewed
    elif not position_table:
        arm = _arm(body)
        if arm is not None and _entry_id(arm):
            viewed = _from_row(arm, order_text, order_qty, snapshot_only=True)
            if viewed is not None:
                return viewed
    return _without_row(body, log_status, order_text)


def _is_spot_receipt(receipt: Mapping[str, Any]) -> bool:
    if str(receipt.get("step") or "") == "binance_spot":
        return True
    status = str(receipt.get("status") or "").strip().casefold()
    if status in {"live_refused", "binance_spot_submitted"}:
        return True
    return _arm(receipt) is not None


def _arm(receipt: Mapping[str, Any]) -> Mapping[str, Any] | None:
    result = receipt.get("exec_result")
    if not isinstance(result, Mapping):
        return None
    arm = result.get("arm_position")
    if isinstance(arm, Mapping):
        return arm
    return None


def _without_row(
    receipt: Mapping[str, Any],
    log_status: str | None,
    order_text: str,
) -> SpotBookView | None:
    status = str(receipt.get("status") or "").strip().casefold()
    column = str(log_status or "").strip().casefold()
    if status == "dry_run_acked":
        view = _view(
            label="演练未下单",
            tone="muted",
            filled_text="未入账",
            order_text=order_text,
            protection_text="未向交易所发单",
            is_rejection=False,
        )
        return view
    if status == "binance_spot_submitted":
        return _view(
            label="回执称已提交",
            tone="muted",
            filled_text="未入账",
            order_text=order_text,
            protection_text="无仓表行，不声称成交或保护",
            is_rejection=False,
        )
    if status in {"live_refused", "dry_run_rejected"} or column == "rejected":
        error = str(receipt.get("error") or "").strip()
        label = f"拒绝 · {error}" if error else "拒绝"
        return _view(
            label=label,
            tone="bad",
            filled_text="未入账",
            order_text=order_text,
            protection_text="无仓表行",
            is_rejection=True,
        )
    return None


def _from_row(
    row: Mapping[str, Any],
    order_text: str,
    order_qty: Decimal | None,
    *,
    snapshot_only: bool,
) -> SpotBookView | None:
    status = str(row.get("status") or "").strip()
    status_key = status.casefold()
    entry = _entry_id(row)
    if not entry and status_key not in _ACCEPTED_STATUSES:
        return None
    if not entry and status_key == "qty_unconfirmed":
        return None
    filled_qty = _decimal(row.get("quantity"))
    filled_text = _filled_text(row.get("quantity"), filled_qty)
    relation = _relation(filled_qty, order_qty)
    oco = status_key in _OCO_STATUSES or str(row.get("protection") or "") == "exchange_oco"
    if status_key == "closed_canceled":
        label = "已撤单" if relation in {"unknown", "zero"} else "已撤单·残余成交"
    elif status_key == "qty_unconfirmed" and relation in {"unknown", "zero"}:
        label = "已接受未成交"
    elif status_key == "oco_failed":
        label = "保护未证实"
    elif status_key == "exit_submitted":
        label = "退出已提交"
    elif status_key == "oco_filled":
        label = "保护单已成交"
    elif status_key == "closed_dust":
        label = "尘埃归档"
    elif status_key == "closed":
        label = "仓表已关闭"
    elif relation == "partial":
        label = "部分成交"
    elif relation in {"match", "fill_only"}:
        label = "已成交"
    elif relation == "over":
        label = "成交超过委托"
    elif oco:
        label = "OCO状态"
    elif status:
        label = f"仓表{status}"
    else:
        label = "已入账"
    if snapshot_only:
        label = f"首包{label}"
    protection = _protection_text(
        status_key=status_key,
        protection=str(row.get("protection") or "").strip(),
        oco_list=str(row.get("oco_list_status") or "").strip(),
        filled_text=filled_text,
        relation=relation,
        oco=oco,
        matched=relation == "match",
        residual_qty=row.get("residual_qty"),
    )
    if snapshot_only:
        protection = f"{protection} · 无仓表，后续状态不可用"
    tone = "ok" if label.endswith("已成交") and "部分" not in label else "muted"
    if label.startswith("保护未证实"):
        tone = "muted"
    return _view(
        label=label,
        tone=tone,
        filled_text=filled_text,
        order_text=order_text,
        protection_text=protection,
        is_rejection=False,
        snapshot_only=snapshot_only,
    )


def _protection_text(
    *,
    status_key: str,
    protection: str,
    oco_list: str,
    filled_text: str,
    relation: str,
    oco: bool,
    matched: bool,
    residual_qty: object = None,
) -> str:
    if status_key == "closed_canceled":
        return "无交易所OCO"
    if status_key == "closed_dust" and str(residual_qty or "").strip() not in {"", "0"}:
        return "尘埃残余待人工复核，未自动清除，未外发通知"
    if status_key in {"oco_failed", "oco_unknown"} or protection == "oco_pending":
        return "OCO未证实，不声称已保护；本账户新买入已拦住，未外发通知"
    if oco:
        if relation in {"unknown", "zero"}:
            return "OCO状态已写入，已成交数量为空，不声称已保护"
        list_bit = f"，list={oco_list}" if oco_list else ""
        same = "（与委托数量一致）" if matched else ""
        return f"OCO仅覆盖已成交 {filled_text}{same}{list_bit}"
    if protection == "held_position_exit" or status_key == "qty_unconfirmed":
        return "无交易所OCO"
    if protection:
        return f"protection={protection}"
    return "保护字段为空"


def _view(
    *,
    label: str,
    tone: str,
    filled_text: str,
    order_text: str,
    protection_text: str,
    is_rejection: bool,
    snapshot_only: bool = False,
) -> SpotBookView:
    summary = " · ".join(
        (
            label,
            f"已成交 {filled_text}",
            f"委托 {order_text}",
            protection_text,
        )
    )
    return SpotBookView(
        label=label,
        tone=tone,
        filled_text=filled_text,
        order_text=order_text,
        protection_text=protection_text,
        summary=summary,
        is_rejection=is_rejection,
        snapshot_only=snapshot_only,
    )


def _order_quantity(receipt: Mapping[str, Any]) -> tuple[str, Decimal | None]:
    result = receipt.get("exec_result")
    order = result.get("order") if isinstance(result, Mapping) else None
    if not isinstance(order, Mapping):
        return "回执无委托数量", None
    raw = order.get("quantity")
    number = _decimal(raw)
    if number is None or number <= 0:
        return "回执无委托数量", None
    return str(raw).strip(), number


def _filled_text(raw: object, number: Decimal | None) -> str:
    if raw is None:
        return "未入账"
    if isinstance(raw, str) and not raw.strip():
        return "未入账"
    if number is None:
        return "不可读"
    if number == 0:
        return "0"
    return str(raw).strip()


def _relation(filled: Decimal | None, order: Decimal | None) -> str:
    if filled is None:
        return "unknown"
    if filled == 0:
        return "zero"
    if order is None:
        return "fill_only"
    if filled < order:
        return "partial"
    if filled == order:
        return "match"
    return "over"


def _entry_id(row: Mapping[str, Any]) -> str:
    raw = row.get("entry_order_id")
    if raw is None:
        return ""
    return str(raw).strip()


def _decimal(raw: object) -> Decimal | None:
    if isinstance(raw, bool) or raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0:
        return None
    return number
