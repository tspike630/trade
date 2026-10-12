"""P0 risk strip: gates, today's execution rates, failures, process heartbeats."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, assert_never

from nicegui import ui

from dashboard.data import DataStore, DecisionWindowStats, FailureRow, ProcessProbe, RateBlock, connect_ro
from dashboard.format_util import (
    Liveness,
    confirm_label,
    fmt_age,
    fmt_rate,
    fmt_ts,
    fmt_usd,
    liveness_label,
    short_addr,
)
from dashboard.panels.common import col, data_table, kpi, panel_box, show_unavailable, slow_hint
from dashboard.settings import GateStatus, db_root, read_gates

log = logging.getLogger("dashboard.panels.risk")

# §10 observe fallbacks — display only when JSON / prior.db reads fail.
_FALLBACK_PEAK_P50_MIN = 1.0
_FALLBACK_SURVIVE_4H = 0.753
_FALLBACK_SAME_DAY_DEATH = 0.6867
_FALLBACK_GRADUATION = 0.002
_OBSERVE = "观察中·不调权"


def render_risk(store: DataStore) -> None:
    gates = read_gates()
    extra = "risk-live" if gates.live_effective else ""
    with panel_box("风险", "闸门只读。本看板不能修改环境变量或杀开关。", extra):
        _gates(gates)
        _priors()
        _today(store)
        _week(store)
        _processes(store)
        _failures(store)
        _backtest(store)
        _news(store)


def _gates(status: GateStatus) -> None:
    paper_value, paper_tone = ("开", "tone-warn") if status.paper_enabled else ("关", "")
    pm_value, pm_tone = ("开", "tone-warn") if status.pm_enabled else ("关", "")
    live_value, live_tone = _live_face(status)
    with ui.element("div").classes("kpi-grid"):
        kpi("模拟盘执行", paper_value, "EXECUTION_PAPER_ENABLED", paper_tone)
        kpi("仓位管理", pm_value, "PM_ENABLED", pm_tone)
        kpi("实盘", live_value, _live_hint(status), live_tone)
    ui.label(
        "关是当前预期状态（模拟盘未开闸）。实盘若变为「开启」会标红。这里没有开关。"
    ).classes("muted gate-note")


@dataclass(frozen=True)
class _PriorKpis:
    peak_p50_min: float
    survive_4h: float
    same_day_death: float
    graduation_rate: float
    peak_hint: str
    survive_hint: str
    death_hint: str
    grad_hint: str
    load_note: str
    keys_used: tuple[str, ...]


def _prior_candidate_roots() -> list[Path]:
    """Prefer DASHBOARD_DB_ROOT, then project root (parent of dashboard/)."""
    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in (db_root(), Path(__file__).resolve().parent.parent.parent):
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


def _prior_json_paths() -> list[Path]:
    return [root / "data" / "prior" / "exports" / "baseline_params.json" for root in _prior_candidate_roots()]


def _prior_db_paths() -> list[Path]:
    return [root / "data" / "prior" / "db" / "prior.db" for root in _prior_candidate_roots()]


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _load_baseline_json() -> tuple[dict[str, Any] | None, str]:
    errors: list[str] = []
    for path in _prior_json_paths():
        try:
            if not path.is_file():
                errors.append(f"missing {path.name}")
                continue
            with path.open(encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                return raw, str(path)
            errors.append(f"invalid json shape at {path.name}")
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            log.warning("prior baseline json read failed path=%s err=%s", path, exc)
            errors.append(f"{path.name}: {exc}")
    return None, "; ".join(errors) if errors else "baseline_params.json unavailable"


def _extract_from_json(params: dict[str, Any]) -> dict[str, float | None]:
    """Map baseline_params.json keys to the four §10 KPIs."""
    trio = params.get("weekly_baseline_trio") if isinstance(params.get("weekly_baseline_trio"), dict) else {}
    redpump = params.get("redpump") if isinstance(params.get("redpump"), dict) else {}
    cg = params.get("coingecko") if isinstance(params.get("coingecko"), dict) else {}
    gmgn = params.get("gmgn") if isinstance(params.get("gmgn"), dict) else {}
    self_db = params.get("self_db") if isinstance(params.get("self_db"), dict) else {}
    survive = self_db.get("survive") if isinstance(self_db.get("survive"), dict) else {}

    peak = _as_float(gmgn.get("median_peak_min"))
    survive_4h = _as_float(survive.get("4h"))
    same_day = _as_float(trio.get("same_day_death_rate"))
    if same_day is None:
        same_day = _as_float(cg.get("same_day_death_rate"))
    graduation = _as_float(trio.get("graduation_rate"))
    if graduation is None:
        graduation = _as_float(redpump.get("graduation_rate"))

    return {
        "peak_p50_min": peak,
        "survive_4h": survive_4h,
        "same_day_death": same_day,
        "graduation_rate": graduation,
    }


def _json_hints(params: dict[str, Any]) -> dict[str, str]:
    meta = params.get("meta") if isinstance(params.get("meta"), dict) else {}
    gmgn = params.get("gmgn") if isinstance(params.get("gmgn"), dict) else {}
    n = meta.get("gmgn_n")
    if n is None:
        n = gmgn.get("n") or gmgn.get("acceptance_unique_tokens")
    pack = meta.get("gmgn_pack") or ("完整数据集" if gmgn.get("complete_pack") else "GMGN")
    peak_hint = f"GMGN {pack} n={n} · {_OBSERVE}" if n is not None else f"GMGN · {_OBSERVE}"
    return {
        "peak_hint": peak_hint,
        "survive_hint": f"自库 self_survival，短窗 · {_OBSERVE}",
        "death_hint": f"CoinGecko · {_OBSERVE}",
        "grad_hint": f"RED-PUMP · {_OBSERVE}",
    }


def _load_baseline_history_row() -> dict[str, float | None] | None:
    """Read-only latest baseline_history row; never writes prior.db."""
    for path in _prior_db_paths():
        try:
            if not path.is_file():
                continue
            conn = connect_ro(path)
            try:
                row = conn.execute(
                    """
                    SELECT peak_p50_min, survive_4h, same_day_death_rate, graduation_rate
                    FROM baseline_history
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
            finally:
                conn.close()
            if row is None:
                continue
            return {
                "peak_p50_min": _as_float(row["peak_p50_min"]),
                "survive_4h": _as_float(row["survive_4h"]),
                "same_day_death": _as_float(row["same_day_death_rate"]),
                "graduation_rate": _as_float(row["graduation_rate"]),
            }
        except (OSError, FileNotFoundError, Exception) as exc:  # noqa: BLE001 — degrade to fallback
            log.warning("prior.db baseline_history read failed path=%s err=%s", path, exc)
    return None


def _load_prior_kpis() -> _PriorKpis:
    """Prefer baseline_params.json; fill gaps from prior.db; else hardcoded fallbacks."""
    defaults = {
        "peak_p50_min": _FALLBACK_PEAK_P50_MIN,
        "survive_4h": _FALLBACK_SURVIVE_4H,
        "same_day_death": _FALLBACK_SAME_DAY_DEATH,
        "graduation_rate": _FALLBACK_GRADUATION,
    }
    default_hints = {
        "peak_hint": f"GMGN 完整 n=956 · {_OBSERVE}",
        "survive_hint": f"自库 self_survival，短窗 · {_OBSERVE}",
        "death_hint": f"CoinGecko · {_OBSERVE}",
        "grad_hint": f"RED-PUMP · {_OBSERVE}",
    }
    values = dict(defaults)
    keys_used: list[str] = []
    load_note = ""
    hints = dict(default_hints)

    params, json_meta = _load_baseline_json()
    json_ok = params is not None
    if json_ok and params is not None:
        extracted = _extract_from_json(params)
        hints.update(_json_hints(params))
        for field, number in extracted.items():
            if number is not None:
                values[field] = number
                keys_used.append(f"json:{field}")
    else:
        load_note = f"先验 JSON 不可用（{json_meta}），已用对照回退。"

    missing = [
        name
        for name in ("peak_p50_min", "survive_4h", "same_day_death", "graduation_rate")
        if f"json:{name}" not in keys_used
    ]
    if missing:
        db_row = _load_baseline_history_row()
        if db_row:
            for name in missing:
                number = db_row.get(name)
                if number is not None:
                    values[name] = number
                    keys_used.append(f"db:{name}")
            still_missing = [name for name in missing if f"db:{name}" not in keys_used]
            if not json_ok and any(k.startswith("db:") for k in keys_used):
                load_note = "先验 JSON 不可用，已从 prior.db baseline_history 读取。"
            elif still_missing and not load_note:
                load_note = "部分先验字段缺失，已用内置对照值。"
        elif not json_ok:
            load_note = f"先验读取失败（{json_meta}），已显示内置对照值。"
        elif not load_note:
            load_note = "部分先验字段缺失且 prior.db 不可用，已用内置对照值。"

    for name in ("peak_p50_min", "survive_4h", "same_day_death", "graduation_rate"):
        if f"json:{name}" not in keys_used and f"db:{name}" not in keys_used:
            keys_used.append(f"fallback:{name}")

    return _PriorKpis(
        peak_p50_min=float(values["peak_p50_min"]),
        survive_4h=float(values["survive_4h"]),
        same_day_death=float(values["same_day_death"]),
        graduation_rate=float(values["graduation_rate"]),
        peak_hint=hints["peak_hint"],
        survive_hint=hints["survive_hint"],
        death_hint=hints["death_hint"],
        grad_hint=hints["grad_hint"],
        load_note=load_note,
        keys_used=tuple(keys_used),
    )


def _fmt_peak_min(value: float) -> str:
    return f"{value:.1f} min"


def _fmt_pct1(value: float) -> str:
    return f"{value * 100:.1f}%"


def _fmt_pct2(value: float) -> str:
    return f"{value * 100:.2f}%"


def _priors() -> None:
    """§10 observe baselines — display only; never touch weights/gates/env/execution."""
    kpis = _load_prior_kpis()
    ui.label("先验对照（观察中·不调权）").classes("section-label")
    with ui.element("div").classes("kpi-grid"):
        kpi("见顶 P50", _fmt_peak_min(kpis.peak_p50_min), kpis.peak_hint)
        kpi("4h 存活", _fmt_pct1(kpis.survive_4h), kpis.survive_hint)
        kpi("当天死", _fmt_pct2(kpis.same_day_death), kpis.death_hint)
        kpi("毕业率", _fmt_pct1(kpis.graduation_rate), kpis.grad_hint)
    ui.label(
        "§10 观察基线，只读对照。不改策略权重、闸门、环境变量或执行。"
    ).classes("muted gate-note")
    if kpis.load_note:
        ui.label(kpis.load_note).classes("muted gate-note")


def _live_face(status: GateStatus) -> tuple[str, str]:
    if status.live_effective:
        return "开启", "tone-bad"
    if status.live_env_enabled and status.kill_switch_present:
        return "阻断", "tone-warn"
    if status.live_env_enabled:
        return "未确认", "tone-warn"
    return "关闭", ""


def _live_hint(status: GateStatus) -> str:
    env = "开" if status.live_env_enabled else "关"
    switch = "存在" if status.kill_switch_present else "不存在"
    parts = [
        f"LIVE_TRADING_ENABLED {env}",
        f"杀开关 {status.kill_switch_name} {switch}",
        f"确认短语{confirm_label(status.confirm_state)}",
    ]
    if status.live_block_reason and status.live_block_reason not in parts:
        parts.append(status.live_block_reason)
    return " · ".join(parts)


def _today(store: DataStore) -> None:
    result = store.today_execution_stats()
    slow_hint(result.elapsed_ms)
    ui.label("今日 · execution_log 行（UTC 日）").classes("section-label")
    if not result.available:
        show_unavailable(result.reason)
        return
    block: RateBlock = result.data
    with ui.element("div").classes("kpi-grid"):
        kpi("今日信号数", str(block.n), "当日执行账本行数")
        kpi("校验通过率", fmt_rate(block.ok_rate, block.n_ok, block.n), "validation_ok = 1")
        kpi("已执行率", fmt_rate(block.executed_rate, block.n_executed, block.n), "executed = 1")


def _week(store: DataStore) -> None:
    result = store.week_decision_stats()
    slow_hint(result.elapsed_ms)
    ui.label("近 7 日 · 决策口径（滚动 168 小时，比率与 review 周报一致）").classes("section-label")
    if not result.available:
        show_unavailable(result.reason)
        return
    block: DecisionWindowStats = result.data
    with ui.element("div").classes("kpi-grid"):
        kpi("决策数", str(block.n_decisions), "decision_log")
        kpi("有执行", str(block.n_with_exec), "关联到 execution_log 的决策")
        kpi(
            "校验通过率",
            fmt_rate(block.ok_rate, block.n_ok, block.n_with_exec),
            "通过数 / 有执行的决策",
        )
        kpi(
            "已执行率",
            fmt_rate(block.executed_rate, block.n_executed, block.n_with_exec),
            "executed 数 / 有执行的决策",
        )


def _processes(store: DataStore) -> None:
    result = store.process_liveness()
    slow_hint(result.elapsed_ms)
    ui.label("进程心跳").classes("section-label")
    if not result.available:
        show_unavailable(result.reason)
        return
    with ui.element("div").classes("proc-row"):
        for probe in result.data:
            _process_chip(probe)


def _process_chip(probe: ProcessProbe) -> None:
    with ui.element("div").classes(f"proc {_process_class(probe.state)}"):
        ui.label(probe.name).classes("proc-name")
        ui.label(liveness_label(probe.state)).classes("proc-state")
        detail = fmt_age(probe.age_seconds)
        if probe.detail:
            detail = f"{detail} · {probe.detail}"
        ui.label(detail).classes("proc-detail")


def _process_class(state: Liveness) -> str:
    match state:
        case "alive":
            return "proc-alive"
        case "stale":
            return "proc-stale"
        case "unknown":
            return "proc-unknown"
        case _ as unreachable:
            assert_never(unreachable)


def _failures(store: DataStore) -> None:
    result = store.recent_failures(10)
    slow_hint(result.elapsed_ms)
    ui.label("最近失败").classes("section-label")
    if not result.available:
        show_unavailable(result.reason)
        return
    rows = [_failure_row(item) for item in result.data]
    if not rows:
        ui.label("暂无 validation_errors").classes("muted")
        return
    data_table(
        [
            col("ts", "时间"),
            col("decision", "决策"),
            col("strategy", "策略"),
            col("action", "动作"),
            col("token", "代币"),
            col("errors", "错误"),
        ],
        rows,
    )


def _failure_row(item: FailureRow) -> dict[str, object]:
    return {
        "id": item.id,
        "ts": fmt_ts(item.ts),
        "decision": item.decision_log_id,
        "strategy": item.strategy_id or "—",
        "action": item.action or "—",
        "token": short_addr(item.token_address),
        "errors": item.validation_errors or "—",
    }


def _backtest(store: DataStore) -> None:
    result = store.latest_backtest()
    slow_hint(result.elapsed_ms)
    ui.label("最近回测").classes("section-label")
    if not result.available:
        show_unavailable(result.reason)
        return
    run = result.data
    if run is None:
        ui.label("暂无回测记录").classes("muted")
        return
    bits = [
        run.run_id or "—",
        run.strategy_id or "—",
        run.status or "—",
        f"起始 {fmt_usd(run.start_cash)}",
        f"结束权益 {fmt_usd(run.end_equity)}",
        fmt_ts(run.started_at),
    ]
    if run.finished_at:
        bits.append(f"结束 {fmt_ts(run.finished_at)}")
    ui.label(" · ".join(bits)).classes("backtest-line")
    if run.notes:
        ui.label(run.notes).classes("muted")


def _news(store: DataStore) -> None:
    result = store.latest_news(5)
    with ui.expansion("新闻旁路", icon="newspaper").classes("news-box"):
        slow_hint(result.elapsed_ms)
        if not result.available:
            show_unavailable(result.reason)
            return
        if not result.data:
            ui.label("暂无新闻").classes("muted")
            return
        for item in result.data:
            title = item.title or "—"
            meta = " · ".join(
                part
                for part in (item.source, item.source_tier, item.onchain_status, fmt_ts(item.published_at))
                if part
            )
            with ui.element("div").classes("news-item"):
                _news_title(title, item.url)
                if meta:
                    ui.label(meta).classes("muted")


def _news_title(title: str, url: str) -> None:
    href = (url or "").strip()
    if href.startswith("https://") or href.startswith("http://"):
        ui.link(title, href, new_tab=True).classes("news-link")
        return
    ui.label(title)
