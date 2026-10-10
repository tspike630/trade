"""Shared panel chrome. No data access."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from nicegui import ui

UNAVAILABLE = "数据不可用"

_CELL_SLOT = """
<q-td :props="props">
  <span :class="props.row.__CLASS__">{{ props.value }}</span>
</q-td>
"""


@contextmanager
def panel_box(title: str, subtitle: str = "", extra_class: str = "") -> Iterator[None]:
    classes = "panel"
    if extra_class:
        classes = f"{classes} {extra_class}"
    with ui.element("section").classes(classes):
        with ui.element("div").classes("panel-head"):
            ui.label(title).classes("panel-title")
            if subtitle:
                ui.label(subtitle).classes("panel-sub")
        yield


def show_unavailable(reason: str) -> None:
    with ui.element("div").classes("unavailable"):
        ui.label(UNAVAILABLE).classes("unavailable-title")
        ui.label(reason or "未知原因").classes("unavailable-reason")


def empty_state(title: str, detail: str) -> None:
    with ui.element("div").classes("empty"):
        ui.label(title).classes("empty-title")
        ui.label(detail).classes("empty-detail")


def kpi(label: str, value: str, hint: str = "", tone: str = "") -> None:
    classes = "kpi"
    if tone:
        classes = f"{classes} {tone}"
    with ui.element("div").classes(classes):
        ui.label(label).classes("kpi-label")
        ui.label(value).classes("kpi-value")
        if hint:
            ui.label(hint).classes("kpi-hint")


def slow_hint(elapsed_ms: float) -> None:
    if elapsed_ms > 200:
        ui.label(f"查询耗时 {elapsed_ms:.0f} ms，超过 200 ms 目标").classes("slow")


def col(name: str, label: str, align: str = "left") -> dict[str, str]:
    return {"name": name, "label": label, "field": name, "align": align, "sortable": True}


def data_table(
    columns: list[dict[str, str]],
    rows: list[dict[str, object]],
    *,
    row_key: str = "id",
    color_fields: tuple[str, ...] = (),
) -> None:
    if not rows:
        ui.label("暂无记录").classes("muted")
        return
    with ui.element("div").classes("table-scroll"):
        table = ui.table(columns=columns, rows=rows, row_key=row_key, pagination=15)
        table.classes("w-full dash-table")
        for field in color_fields:
            html = _CELL_SLOT.replace("__CLASS__", f"{field}_class")
            table.add_slot(f"body-cell-{field}", html)
