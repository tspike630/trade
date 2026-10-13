"""Read-only §10 evolution suggestions from the latest weekly review report.

Parses ``## 10. 进化建议`` only. Never writes DBs, env, gates, or weights.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from nicegui import ui

from dashboard.data import DataStore
from dashboard.panels.common import col, data_table, empty_state, panel_box
from dashboard.settings import db_root

_SECTION_RE = re.compile(
    r"^##\s*10\.\s*进化建议\s*$",
    re.MULTILINE,
)
_NEXT_SECTION_RE = re.compile(r"^##\s+\d+\.", re.MULTILINE)
_MD_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_DATE_RE = re.compile(r"review_weekly_(\d{4}-\d{2}-\d{2})\.md$")

_EVIDENCE_LIMIT = 96
_HINT_LIMIT = 80

_REPORT_GLOBS = (
    "exports/reports/review_weekly_*.md",
    "crawler/exports/reports/review_weekly_*.md",
)


@dataclass(frozen=True)
class EvolutionItem:
    layer: str
    item: str
    status: str
    evidence: str
    action_hint: str


def find_latest_weekly_report(root: Path | None = None) -> Path | None:
    """Pick the newest ``review_weekly_YYYY-MM-DD.md`` under known export dirs."""
    base = root if root is not None else db_root()
    candidates: list[tuple[str, Path]] = []
    for pattern in _REPORT_GLOBS:
        for path in base.glob(pattern):
            if not path.is_file():
                continue
            match = _DATE_RE.search(path.name)
            stamp = match.group(1) if match else path.name
            candidates.append((stamp, path))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def _strip_md(text: str) -> str:
    return _MD_BOLD_RE.sub(r"\1", text).strip()


def _truncate(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    if limit <= 1:
        return "…"
    return cleaned[: limit - 1] + "…"


def _split_row(line: str) -> list[str] | None:
    text = line.strip()
    if not text.startswith("|") or text.count("|") < 6:
        return None
    # Skip separator rows like |---|---|
    if re.match(r"^\|[\s\-:|]+\|$", text):
        return None
    cells = [cell.strip() for cell in text.strip("|").split("|")]
    if len(cells) < 5:
        return None
    return cells[:5]


def parse_evolution_section(text: str) -> list[EvolutionItem]:
    """Parse the first markdown table under ``## 10. 进化建议``."""
    match = _SECTION_RE.search(text)
    if match is None:
        return []
    start = match.end()
    nxt = _NEXT_SECTION_RE.search(text, start)
    body = text[start : nxt.start() if nxt else len(text)]
    items: list[EvolutionItem] = []
    header_seen = False
    for line in body.splitlines():
        cells = _split_row(line)
        if cells is None:
            if header_seen and items:
                # Stop at first non-table line after data rows (subsections).
                if line.strip() and not line.strip().startswith("|"):
                    break
            continue
        layer, item, status, evidence, action_hint = (_strip_md(c) for c in cells)
        if (layer, item, status) == ("层", "项", "状态"):
            header_seen = True
            continue
        if not header_seen:
            continue
        if not (layer or item or status or evidence or action_hint):
            continue
        items.append(
            EvolutionItem(
                layer=layer or "—",
                item=item or "—",
                status=status or "—",
                evidence=evidence or "—",
                action_hint=action_hint or "—",
            )
        )
    return items


def load_evolution_suggestions(
    root: Path | None = None,
) -> tuple[list[EvolutionItem], str | None, Path | None]:
    """Return (items, empty_reason, source_path). empty_reason set when no rows."""
    path = find_latest_weekly_report(root)
    if path is None:
        return [], "未找到 review_weekly_*.md（exports/reports）", None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], f"无法读取周报：{exc}", path
    items = parse_evolution_section(text)
    if not items:
        return [], f"周报无 §10 表格：{path.name}", path
    return items, None, path


def status_class(status: str) -> str:
    """Map §10 status vocabulary onto existing table tone classes."""
    raw = _strip_md(status)
    if "建议落地" in raw:
        return "warn"
    if "裁决" in raw:
        return "ok"
    if "观察中" in raw:
        return "muted"
    return "muted"


def render_evolution_body(*, root: Path | None = None) -> None:
    """Body only (for OVERVIEW v2 grid). No outer panel chrome."""
    items, reason, path = load_evolution_suggestions(root)
    if path is not None:
        ui.label(f"源：{path.name}").classes("muted")
    if reason is not None:
        empty_state("暂无数据", reason)
        return
    rows = [
        {
            "id": index,
            "layer": item.layer,
            "item": item.item,
            "status": item.status,
            "status_class": status_class(item.status),
            "evidence": _truncate(item.evidence, _EVIDENCE_LIMIT),
            "hint": _truncate(item.action_hint, _HINT_LIMIT),
        }
        for index, item in enumerate(items)
    ]
    data_table(
        [
            col("layer", "层"),
            col("item", "项"),
            col("status", "状态"),
            col("evidence", "证据"),
            col("hint", "行动提示"),
        ],
        rows,
        row_key="id",
        color_fields=("status",),
    )


def render_evolution(store: DataStore) -> None:
    """Registry entry: titled read-only panel (also safe if overview mounts it)."""
    del store  # PanelSpec signature; this panel reads markdown, not DBs.
    with panel_box("进化建议", "周报 §10 · 只观察 · 不调权"):
        render_evolution_body()
