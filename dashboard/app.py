"""NiceGUI entrypoint.

Start from the project root (the directory that contains ``dashboard/`` and the
SQLite files)::

    python -m dashboard.app

Binds 127.0.0.1 only. Default port is 8081 (``DASHBOARD_PORT``).
"""

from __future__ import annotations

import logging
import secrets
from typing import Literal

from fastapi import Request
from nicegui import app, ui

from dashboard.auth import SecretFilter, verify_api_key
from dashboard.data import DataStore
from dashboard.format_util import clock_utc
from dashboard.panels.common import show_unavailable
from dashboard.panels.registry import PANELS
from dashboard.settings import allow_insecure, api_secret, bind_host, db_root, port, refresh_seconds

log = logging.getLogger("dashboard")
Access = Literal["ok", "open", "needed", "unconfigured"]
_health_ready = False
_logging_ready = False

THEME_CSS = """
:root {
  color-scheme: dark;
  --bg: #0a0c10;
  --bg-2: #0f1318;
  --panel: #12161c;
  --panel-2: #171c24;
  --line: #2a3340;
  --text: #d7dde6;
  --muted: #7d8a99;
  --accent: #ff9100;
  --accent-dim: #3a2408;
  --mint: #3dd68c;
  --mint-dim: #123528;
  --warn: #f0b429;
  --warn-dim: #3a2f0e;
  --bad: #ff5c5c;
  --bad-dim: #3a1518;
  --ok: #3dd68c;
  --mono: "IBM Plex Mono", "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  --sans: "IBM Plex Sans", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
}
html, body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--sans);
  font-size: 13px;
}
body { margin: 0; }
.page {
  max-width: 1440px;
  margin: 0 auto;
  padding: 0 12px 40px;
}
.term-strip {
  position: sticky;
  top: 0;
  z-index: 40;
  background: linear-gradient(180deg, #0d1015 0%, #0a0c10 100%);
  border-bottom: 1px solid var(--line);
  margin: 0 -12px 12px;
  padding: 8px 12px 10px;
  backdrop-filter: blur(8px);
}
.term-strip-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 16px;
}
.term-brand {
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-width: 220px;
}
.term-mark {
  color: var(--accent);
  font-family: var(--mono);
  font-weight: 700;
  letter-spacing: 0.08em;
  font-size: 12px;
}
.term-title {
  font-size: 15px;
  font-weight: 700;
  letter-spacing: 0.02em;
}
.term-meta { color: var(--muted); font-family: var(--mono); font-size: 11px; }
.kpi-strip {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(118px, 1fr));
  gap: 6px;
  margin-top: 8px;
}
.kpi-tile {
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-left: 2px solid var(--accent);
  padding: 6px 8px 7px;
  min-width: 0;
}
.kpi-tile .kpi-label {
  color: var(--muted);
  font-size: 10px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.kpi-tile .kpi-value {
  font-family: var(--mono);
  font-size: 16px;
  font-weight: 650;
  margin-top: 2px;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}
.kpi-tile .kpi-hint {
  color: var(--muted);
  font-size: 10px;
  margin-top: 2px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.kpi-tile.tone-warn { border-left-color: var(--warn); }
.kpi-tile.tone-warn .kpi-value { color: var(--warn); }
.kpi-tile.tone-bad { border-left-color: var(--bad); background: #1a1012; }
.kpi-tile.tone-bad .kpi-value { color: var(--bad); }
.kpi-tile.tone-ok { border-left-color: var(--mint); }
.kpi-tile.tone-ok .kpi-value { color: var(--mint); }
.topbar {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 10px;
}
.brand-kicker {
  color: var(--accent);
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
}
.brand-title {
  font-size: 20px;
  font-weight: 700;
  letter-spacing: -0.02em;
  line-height: 1.2;
}
.brand-sub, .muted, .kpi-hint, .kpi-label, .panel-sub, .proc-detail, .section-label {
  color: var(--muted);
}
.brand-sub { font-size: 12px; margin-top: 2px; font-family: var(--mono); }
.top-actions { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.ghost-btn { color: var(--text) !important; }
.banner {
  border: 1px solid var(--warn);
  background: var(--warn-dim);
  color: var(--warn);
  border-radius: 2px;
  padding: 8px 10px;
  margin-bottom: 10px;
  font-size: 12px;
  font-family: var(--mono);
}
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.chip {
  border: 1px solid var(--line);
  border-radius: 2px;
  padding: 2px 8px;
  font-size: 11px;
  color: var(--muted);
  font-family: var(--mono);
}
.chip-ok { border-color: #1f5a40; color: var(--mint); }
.chip-miss { border-color: #6d5a2e; color: var(--warn); }
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 2px;
  padding: 12px 12px 14px;
  margin-bottom: 10px;
}
.risk-live {
  border-color: var(--bad);
  box-shadow: inset 3px 0 0 var(--bad);
}
.panel-head { margin-bottom: 10px; border-bottom: 1px solid var(--line); padding-bottom: 8px; }
.panel-title {
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--accent);
  font-family: var(--mono);
}
.panel-sub { font-size: 12px; margin-top: 2px; }
.section-label {
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  margin: 12px 0 6px;
  font-family: var(--mono);
}
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 8px;
}
.kpi {
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 2px;
  padding: 8px 10px 10px;
  min-width: 0;
}
.kpi-label { font-size: 11px; letter-spacing: 0.04em; text-transform: uppercase; }
.kpi-value {
  font-family: var(--mono);
  font-size: 22px;
  font-weight: 650;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
  margin-top: 2px;
}
.kpi-hint { font-size: 11px; margin-top: 3px; word-break: break-word; }
.tone-warn { border-color: #6d5a2e; }
.tone-warn .kpi-value { color: var(--warn); }
.tone-bad { border-color: #7a3036; background: #1a1012; }
.tone-bad .kpi-value { color: var(--bad); }
.gate-note { font-size: 11px; margin-top: 6px; font-family: var(--mono); }
.proc-row { display: flex; flex-wrap: wrap; gap: 8px; }
.proc {
  flex: 1 1 160px;
  border: 1px solid var(--line);
  border-radius: 2px;
  padding: 8px 10px;
  background: var(--panel-2);
}
.proc-name { font-size: 11px; color: var(--muted); font-family: var(--mono); }
.proc-state { font-size: 16px; font-weight: 650; font-family: var(--mono); }
.proc-alive .proc-state { color: var(--mint); }
.proc-stale .proc-state { color: var(--warn); }
.proc-unknown .proc-state { color: var(--muted); }
.unavailable {
  border: 1px solid #6d5a2e;
  background: #1c1810;
  border-radius: 2px;
  padding: 10px 12px;
  margin: 6px 0;
}
.unavailable-title { color: var(--warn); font-weight: 700; font-size: 13px; font-family: var(--mono); }
.unavailable-reason { color: var(--text); font-size: 12px; margin-top: 4px; }
.empty {
  border: 1px dashed var(--line);
  border-radius: 2px;
  padding: 18px 12px;
  text-align: center;
  margin-top: 6px;
}
.empty-title { font-size: 14px; font-weight: 650; }
.empty-detail { color: var(--muted); font-size: 12px; margin-top: 4px; }
.table-scroll { width: 100%; overflow-x: auto; margin-top: 6px; }
.dash-table { background: transparent !important; color: var(--text); }
.q-table__card, .q-table {
  background: transparent !important;
  color: var(--text) !important;
  box-shadow: none !important;
}
.q-table thead tr, .q-table tbody td { border-color: var(--line) !important; }
.q-table thead tr { background: #151a22; }
.q-td, .q-th {
  font-size: 12px;
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
}
.pnl-pos, .ok { color: var(--mint); }
.pnl-neg, .bad { color: var(--bad); }
.pnl-flat, .muted { color: var(--muted); }
.bucket { margin: 6px 0 8px; }
.bucket-meta { display: flex; justify-content: space-between; gap: 12px; font-size: 12px; font-family: var(--mono); }
.bucket-key { word-break: break-word; }
.bucket-count { font-variant-numeric: tabular-nums; color: var(--muted); }
.bucket-bar { margin-top: 3px; }
.backtest-line { font-variant-numeric: tabular-nums; font-size: 13px; font-family: var(--mono); }
.news-box { width: 100%; margin-top: 6px; background: transparent; color: var(--text); }
.news-item { padding: 5px 0; border-top: 1px solid var(--line); }
.news-link { color: var(--accent) !important; }
.slow { color: var(--warn); font-size: 11px; margin-top: 3px; font-family: var(--mono); }
.footer-note { color: var(--muted); font-size: 11px; margin-top: 8px; font-family: var(--mono); }
.gate-wrap {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px 16px;
  background: radial-gradient(ellipse at top, #151820 0%, var(--bg) 55%);
}
.gate-card {
  width: min(420px, 100%);
  background: var(--panel);
  border: 1px solid var(--line);
  border-top: 2px solid var(--accent);
  border-radius: 2px;
  padding: 20px 18px 18px;
}
.gate-title { font-size: 20px; font-weight: 700; }
.gate-copy { color: var(--muted); font-size: 13px; margin: 8px 0 14px; line-height: 1.5; }
.enter-btn { background: var(--accent) !important; color: #111 !important; font-weight: 700; }
.q-field__native, .q-field__label { color: var(--text) !important; }
@media (max-width: 720px) {
  .brand-title { font-size: 18px; }
  .kpi-value { font-size: 18px; }
  .page { padding: 0 8px 28px; }
}
"""



def setup_logging() -> None:
    global _logging_ready
    if _logging_ready:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger().addFilter(SecretFilter())
    _logging_ready = True


def register_health() -> None:
    global _health_ready
    if _health_ready:
        return

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "ok": True,
            "service": "trading-dashboard",
            "auth_required": api_secret() is not None,
        }

    _health_ready = True


def apply_theme() -> None:
    ui.dark_mode(True)
    ui.add_css(THEME_CSS)
    ui.add_head_html('<meta name="robots" content="noindex, nofollow">')


def _session_ok() -> bool:
    try:
        return app.storage.user.get("authed") is True
    except Exception:
        return False


def _grant_session() -> None:
    """Persist a boolean only. The API key itself is never stored."""
    app.storage.user["authed"] = True


def _revoke_session() -> None:
    app.storage.user["authed"] = False
    ui.navigate.to("/")


def _access(request: Request) -> Access:
    if not api_secret():
        if allow_insecure():
            return "open"
        return "unconfigured"
    if _session_ok():
        return "ok"
    provided = request.headers.get("x-api-key")
    if provided and verify_api_key(provided):
        _grant_session()
        return "ok"
    return "needed"


def register_pages() -> None:
    @ui.page("/")
    def home(request: Request) -> None:
        apply_theme()
        mode = _access(request)
        if mode == "unconfigured":
            _render_locked()
            return
        if mode == "needed":
            _render_login()
            return
        _render_dashboard(insecure=mode == "open")


def _render_locked() -> None:
    with ui.element("div").classes("gate-wrap"):
        with ui.element("div").classes("gate-card"):
            ui.label("只读").classes("brand-kicker")
            ui.label("交易监控看板").classes("gate-title")
            ui.label(
                "未配置 API_SECRET，数据视图已关闭。在环境中设置 API_SECRET 后重启看板，"
                "并用请求头 X-API-Key 或登录框进入。本地调试可设 ALLOW_INSECURE_API=1，"
                "不要把该模式暴露到公网。/health 不需要密钥。"
            ).classes("gate-copy")


def _render_login() -> None:
    with ui.element("div").classes("gate-wrap"):
        with ui.element("div").classes("gate-card"):
            ui.label("只读").classes("brand-kicker")
            ui.label("交易监控看板").classes("gate-title")
            ui.label(
                "数据视图需要 API Key。输入与环境变量 API_SECRET 相同的值，"
                "校验方式与 api_server 的 X-API-Key 一致。密钥不会写入页面或日志。"
            ).classes("gate-copy")
            field = ui.input("API Key", password=True, password_toggle_button=False).props(
                "autocomplete=off"
            ).classes("w-full")
            message = ui.label("").classes("muted")

            def submit() -> None:
                typed = field.value or ""
                field.value = ""
                try:
                    ok = verify_api_key(typed)
                except Exception:
                    log.warning("auth check failed")
                    message.set_text("校验失败")
                    return
                if ok:
                    _grant_session()
                    ui.navigate.to("/")
                    return
                message.set_text("密钥无效")

            field.on("keydown.enter", lambda _event: submit())
            ui.button("进入", on_click=submit).classes("enter-btn")


def _render_dashboard(*, insecure: bool) -> None:
    store = DataStore()
    interval = int(refresh_seconds())

    @ui.refreshable
    def content() -> None:
        _status_strip(store)
        _db_chips(store)
        for spec in PANELS:
            try:
                spec.render(store)
            except Exception:
                log.warning("panel render failed: %s", spec.id)
                with ui.element("section").classes("panel"):
                    show_unavailable(f"{spec.title} 渲染失败")
        ui.label("阶段一只读监控。手动交易确认不在本看板内。").classes("footer-note")

    def tick() -> None:
        if api_secret() and not _session_ok():
            ui.navigate.to("/")
            return
        try:
            content.refresh()
            stamp.set_text(f"上次刷新 {clock_utc()} · 每 {interval} 秒")
        except Exception:
            log.warning("refresh failed")
            stamp.set_text("刷新失败")

    with ui.element("div").classes("page"):
        with ui.element("div").classes("term-strip"):
            with ui.element("div").classes("term-strip-row"):
                with ui.element("div").classes("term-brand"):
                    ui.label("TRADE").classes("term-mark")
                    ui.label("交易监控看板").classes("term-title")
                    ui.label("READ-ONLY · 127.0.0.1").classes("term-meta")
                with ui.element("div").classes("top-actions"):
                    stamp = ui.label(f"REFRESH {clock_utc()} · {interval}s").classes("term-meta")
                    ui.button("刷新", on_click=tick).props("outline dense").classes("ghost-btn")
                    if api_secret():
                        ui.button("退出", on_click=_revoke_session).props("flat dense").classes("ghost-btn")
        if insecure:
            ui.label("本地未鉴权模式（ALLOW_INSECURE_API=1）。不要暴露到公网。").classes("banner")
        content()
    ui.timer(refresh_seconds(), tick, immediate=False)


def _status_strip(store: DataStore) -> None:
    """Fincept/Bloomberg-like dense KPI strip. Read-only."""
    from dashboard.format_util import fmt_rate
    from dashboard.settings import read_gates

    gates = read_gates()
    today = store.today_execution_stats()
    pos = store.open_positions()
    live = store.process_liveness()

    paper = "ON" if gates.paper_enabled else "OFF"
    pm = "ON" if gates.pm_enabled else "OFF"
    live_lbl = "ON" if gates.live_effective else "OFF"
    paper_tone = "tone-warn" if gates.paper_enabled else "tone-ok"
    pm_tone = "tone-warn" if gates.pm_enabled else "tone-ok"
    live_tone = "tone-bad" if gates.live_effective else "tone-ok"

    if today.available:
        n = str(today.data.n)
        ok = fmt_rate(today.data.ok_rate, today.data.n_ok, today.data.n)
        exe = fmt_rate(today.data.executed_rate, today.data.n_executed, today.data.n)
    else:
        n, ok, exe = "—", "—", "—"

    if pos.available:
        n_pos = str(len(pos.data.rows))
        cash = f"{pos.data.cash_usd:,.0f}" if getattr(pos.data, "cash_usd", None) is not None else "—"
        equity = getattr(pos.data, "equity_usd", None)
        eq = f"{equity:,.0f}" if equity is not None else cash
    else:
        n_pos, eq = "—", "—"

    crawl = "—"
    if live.available:
        items = live.data if isinstance(live.data, list) else list(live.data)
        try:
            from dashboard.format_util import liveness_label
            pick = None
            for probe in items:
                name = str(getattr(probe, "name", ""))
                if any(k in name for k in ("爬虫", "采集", "main", "crawler")):
                    pick = probe
                    break
            if pick is None and items:
                pick = items[0]
            if pick is not None:
                crawl = liveness_label(getattr(pick, "state", "unknown"))
        except Exception:
            crawl = "见风险面板"

    with ui.element("div").classes("kpi-strip"):
        _tile("模拟盘", paper, "PAPER", paper_tone)
        _tile("仓位闸", pm, "PM", pm_tone)
        _tile("实盘", live_lbl, gates.live_block_reason or "LIVE", live_tone)
        _tile("今日信号", n, "execution_log")
        _tile("校验通过", ok, "validation_ok")
        _tile("已执行", exe, "executed")
        _tile("持仓数", n_pos, "paper positions")
        _tile("权益$", eq, "cash+mtm")
        _tile("主爬虫", str(crawl)[:18], "heartbeat")


def _tile(label: str, value: str, hint: str = "", tone: str = "") -> None:
    classes = "kpi-tile"
    if tone:
        classes = f"{classes} {tone}"
    with ui.element("div").classes(classes):
        ui.label(label).classes("kpi-label")
        ui.label(value).classes("kpi-value")
        if hint:
            ui.label(hint).classes("kpi-hint")



def _db_chips(store: DataStore) -> None:
    with ui.element("div").classes("chips"):
        for label, name, ready in store.file_status():
            state = "就绪" if ready else "缺失"
            klass = "chip chip-ok" if ready else "chip chip-miss"
            ui.label(f"{label} {name} {state}").classes(klass)


def main() -> None:
    setup_logging()
    register_health()
    register_pages()
    listen_port = port()
    if api_secret():
        auth_mode = "required"
    elif allow_insecure():
        auth_mode = "insecure"
    else:
        auth_mode = "closed"
    log.info(
        "trading dashboard http://%s:%s auth=%s db_root=%s",
        bind_host(),
        listen_port,
        auth_mode,
        db_root(),
    )
    ui.run(
        host=bind_host(),
        port=listen_port,
        title="交易监控看板",
        dark=True,
        language="zh-CN",
        reload=False,
        show=False,
        show_welcome_message=False,
        storage_secret=secrets.token_urlsafe(32),
        reconnect_timeout=10.0,
    )


if __name__ == "__main__":
    main()
