"""NiceGUI entrypoint.

Start from the project root (the directory that contains ``dashboard/`` and the
SQLite files)::

    python -m dashboard.app

Binds 127.0.0.1 only. Default port is 8081 (``DASHBOARD_PORT``).
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Literal

from fastapi import Request
from nicegui import app, ui

from dashboard.auth import SecretFilter, verify_api_key
from dashboard.data import DataStore
from dashboard.format_util import fmt_rate, liveness_label
from dashboard.panels.common import panel_box, panel_guard, show_unavailable
from dashboard.panels.markets import render_markets
from dashboard.panels.registry import PANELS
from dashboard.panels.shell import (
    db_status_line,
    heartbeat_summary,
    live_caption,
    panel_group,
    refresh_age_label,
    session_clock,
)
from dashboard.settings import allow_insecure, api_secret, bind_host, db_root, port, read_gates, refresh_seconds

try:
    from dashboard import chat as chat_module
except ImportError:
    chat_module = None

log = logging.getLogger("dashboard")
Access = Literal["ok", "open", "needed", "unconfigured"]
Section = Literal["home", "chat"]
_health_ready = False
_logging_ready = False

THEME_CSS = """
:root {
  color-scheme: dark;
  --bg: #080808;
  --bg-2: #111111;
  --panel: #0a0a0a;
  --panel-2: #111111;
  --line: #1a1a1a;
  --text: #e5e5e5;
  --muted: #808080;
  --accent: #d97706;
  --accent-dim: #3a2408;
  --mint: #16a34a;
  --mint-dim: #052e16;
  --warn: #ca8a04;
  --warn-dim: #3a2f0e;
  --bad: #dc2626;
  --bad-dim: #3a1518;
  --ok: #16a34a;
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
.shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--bg);
}
.ft-top {
  position: sticky;
  top: 0;
  z-index: 50;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px 14px;
  min-height: 44px;
  padding: 0 16px;
  background: #111111;
  border-bottom: 1px solid #1a1a1a;
}
.ft-brand { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.ft-brand-crypto {
  color: #d97706;
  font-weight: 700;
  font-size: 13px;
  letter-spacing: 0.04em;
}
.ft-brand-name { color: #e5e5e5; font-weight: 700; font-size: 13px; letter-spacing: 0.04em; }
.ft-brand-sub { color: #808080; font-size: 11px; }
.ft-live { color: #16a34a; font-size: 12px; font-family: var(--mono); }
.ft-live-warn { color: #ca8a04; }
.ft-live-muted { color: #525252; }
.ft-proc, .ft-clock, .ft-status-item {
  color: #808080;
  font-family: var(--mono);
  font-size: 11px;
}
.ft-grow { flex: 1 1 auto; }
.ft-actions { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.ft-refresh {
  color: #e5e5e5 !important;
  background: #161616 !important;
  border: 1px solid #222222 !important;
}
.ft-body { width: 100%; max-width: 1440px; margin: 0 auto; padding: 0 12px 40px; }
.ft-tabbar .q-tabs__content { background: #080808; border-bottom: 1px solid #1a1a1a; min-height: 32px; }
.ft-tabbar .q-tab {
  padding: 0 8px;
  min-height: 32px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #525252;
  font-size: 12px;
}
.ft-tabbar .q-tab:hover { color: #d97706; background: #111111; }
.ft-tabbar .q-tab.q-tab--active {
  background: #111111;
  color: #d97706;
  font-weight: 600;
  border-bottom: 2px solid #78350f;
}
.ft-tabbar .q-tab__indicator, .ft-subtab .q-tab__indicator { display: none; }
.ft-subtab .q-tabs__content { background: #0a0a0a; border-bottom: 1px solid #1a1a1a; min-height: 26px; }
.ft-subtab .q-tab {
  padding: 0 10px;
  min-height: 26px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #525252;
  font-size: 10px;
}
.ft-subtab .q-tab:hover { color: #d97706; background: #161616; }
.ft-subtab .q-tab.q-tab--active {
  color: #e5e5e5;
  font-weight: 600;
  border-bottom: 2px solid #d97706;
}
.ft-panels, .ft-panels .q-tab-panel, .ft-subpanels, .ft-subpanels .q-tab-panel {
  background: transparent !important;
  padding: 8px 0 0;
}
.ft-chain { min-width: 140px; margin-bottom: 8px; }
.ft-status {
  position: sticky;
  bottom: 0;
  z-index: 40;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  min-height: 24px;
  padding: 2px 12px;
  background: #111111;
  border-top: 1px solid #1a1a1a;
}
.ft-status-miss { color: #ca8a04; }
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
  background: radial-gradient(ellipse at top, #111111 0%, var(--bg) 55%);
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
    ui.colors(primary="#d97706")
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
        _open(request, "home")

    @ui.page("/chat")
    def chat(request: Request) -> None:
        _open(request, "chat")


def _open(request: Request, section: Section) -> None:
    apply_theme()
    mode = _access(request)
    if mode == "unconfigured":
        _render_locked()
        return
    if mode == "needed":
        _render_login("/chat" if section == "chat" else "/")
        return
    _render_dashboard(insecure=mode == "open", section=section)


def _render_locked() -> None:
    with ui.element("div").classes("gate-wrap"):
        with ui.element("div").classes("gate-card"):
            ui.label("CRYPTO TERMINAL").classes("brand-kicker")
            ui.label("链上新币投研终端").classes("gate-title")
            ui.label(
                "未配置 API_SECRET，数据视图已关闭。在环境中设置 API_SECRET 后重启看板，"
                "并用请求头 X-API-Key 或登录框进入。本地调试可设 ALLOW_INSECURE_API=1，"
                "不要把该模式暴露到公网。/health 不需要密钥。"
            ).classes("gate-copy")


def _render_login(next_path: str) -> None:
    target = "/chat" if next_path == "/chat" else "/"
    with ui.element("div").classes("gate-wrap"):
        with ui.element("div").classes("gate-card"):
            ui.label("CRYPTO TERMINAL").classes("brand-kicker")
            ui.label("链上新币投研终端").classes("gate-title")
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
                    ui.navigate.to(target)
                    return
                message.set_text("密钥无效")

            field.on("keydown.enter", lambda _event: submit())
            ui.button("进入", on_click=submit).classes("enter-btn")


def _render_dashboard(*, insecure: bool, section: Section) -> None:
    store = DataStore()
    interval = int(refresh_seconds())
    chain_filter = {"value": "all"}
    refreshed_at = {"mono": time.monotonic()}
    opened_at = time.monotonic()

    def on_chain(event: object) -> None:
        value = getattr(event, "value", None) or "all"
        text = str(value)
        if text == chain_filter["value"]:
            return
        chain_filter["value"] = text
        markets_tab.refresh()

    @ui.refreshable
    def overview_tab() -> None:
        _status_strip(store)
        _render_panel_group(store, "overview", overview_tab.refresh)
        ui.label("阶段一只读监控。手动交易确认不在本看板内。").classes("footer-note")

    @ui.refreshable
    def signals_tab() -> None:
        _render_panel_group(store, "signals", signals_tab.refresh)

    @ui.refreshable
    def execution_tab() -> None:
        _render_panel_group(store, "execution", execution_tab.refresh)

    @ui.refreshable
    def markets_tab() -> None:
        panel_guard(
            "链上行情",
            lambda: render_markets(
                store,
                chain=chain_filter["value"],
                on_chain=on_chain,
                on_retry=markets_tab.refresh,
            ),
            on_retry=markets_tab.refresh,
        )

    @ui.refreshable
    def chat_tab() -> None:
        panel_guard("AI 问答", lambda: _render_chat(store), on_retry=chat_tab.refresh)

    def paint_clock() -> None:
        clock_lbl.set_text(_clock_text())
        age = int(time.monotonic() - refreshed_at["mono"])
        age_lbl.set_text(refresh_age_label(age))
        up_lbl.set_text(session_clock(int(time.monotonic() - opened_at)))

    def paint_status() -> None:
        try:
            live = store.process_liveness()
            if not live.available:
                text, tone = "心跳：不可用", "muted"
            else:
                text, tone = heartbeat_summary([item.state for item in live.data])
            proc_lbl.set_text(text)
            foot_proc.set_text(text)
            _paint_live(live_lbl, tone)
            db_text = db_status_line(store.file_status())
            db_lbl.set_text(db_text)
            db_lbl.classes(remove="ft-status-miss")
            if "缺失" in db_text:
                db_lbl.classes(add="ft-status-miss")
        except Exception:
            log.warning("status strip failed")
            proc_lbl.set_text("心跳：不可用")
            foot_proc.set_text("心跳：不可用")

    def tick() -> None:
        if api_secret() and not _session_ok():
            ui.navigate.to("/")
            return
        try:
            overview_tab.refresh()
            signals_tab.refresh()
            execution_tab.refresh()
            markets_tab.refresh()
            paint_status()
            refreshed_at["mono"] = time.monotonic()
            paint_clock()
        except Exception:
            log.warning("refresh failed")
            age_lbl.set_text("刷新失败")

    with ui.element("div").classes("shell"):
        with ui.element("div").classes("ft-top"):
            with ui.element("div").classes("ft-brand"):
                ui.label("CRYPTO").classes("ft-brand-crypto")
                ui.label("TERMINAL").classes("ft-brand-name")
                ui.label("链上新币投研终端").classes("ft-brand-sub")
            live_lbl = ui.label("● 实时").classes("ft-live")
            proc_lbl = ui.label("进程 —").classes("ft-proc")
            ui.element("div").classes("ft-grow")
            clock_lbl = ui.label(_clock_text()).classes("ft-clock")
            with ui.element("div").classes("ft-actions"):
                ui.button("刷新", on_click=tick).props("flat dense size=sm").classes("ft-refresh")
                if api_secret():
                    ui.button("退出", on_click=_revoke_session).props("flat dense size=sm").classes("ghost-btn")
        with ui.element("div").classes("ft-body"):
            if insecure:
                ui.label("本地未鉴权模式（ALLOW_INSECURE_API=1）。不要暴露到公网。").classes("banner")
            _module_tabs(
                section,
                overview_tab,
                signals_tab,
                execution_tab,
                markets_tab,
                chat_tab,
            )
        with ui.element("div").classes("ft-status"):
            ui.label("v0.1.0").classes("ft-status-item")
            up_lbl = ui.label(session_clock(0)).classes("ft-status-item")
            db_lbl = ui.label("库 —").classes("ft-status-item")
            foot_proc = ui.label("进程 —").classes("ft-status-item")
            ui.element("div").classes("ft-grow")
            age_lbl = ui.label(refresh_age_label(0)).classes("ft-status-item")
            ui.label(f"每 {interval}s").classes("ft-status-item")
    paint_status()
    paint_clock()
    ui.timer(1.0, paint_clock)
    ui.timer(refresh_seconds(), tick, immediate=False)


def _module_tabs(
    section: Section,
    overview_tab: ui.refreshable,
    signals_tab: ui.refreshable,
    execution_tab: ui.refreshable,
    markets_tab: ui.refreshable,
    chat_tab: ui.refreshable,
) -> None:
    initial = "AI CHAT" if section == "chat" else "DASHBOARD"
    with ui.tabs(value=initial).classes("w-full ft-tabbar") as tabs:
        dash = ui.tab("DASHBOARD")
        markets = ui.tab("MARKETS")
        chat = ui.tab("AI CHAT")
    with ui.tab_panels(tabs, value=initial).classes("w-full ft-panels"):
        with ui.tab_panel(dash):
            with ui.tabs(value="OVERVIEW").classes("w-full ft-subtab").props("dense") as sub:
                overview = ui.tab("OVERVIEW")
                signals = ui.tab("SIGNALS")
                execution = ui.tab("EXECUTION")
            with ui.tab_panels(sub, value="OVERVIEW").classes("w-full ft-subpanels"):
                with ui.tab_panel(overview):
                    overview_tab()
                with ui.tab_panel(signals):
                    signals_tab()
                with ui.tab_panel(execution):
                    execution_tab()
        with ui.tab_panel(markets):
            markets_tab()
        with ui.tab_panel(chat):
            chat_tab()
            ui.link("在 /chat 打开", "/chat").classes("news-link")


def _render_panel_group(store: DataStore, group: str, on_retry) -> None:
    for spec in PANELS:
        if panel_group(spec.id) != group:
            continue
        panel_guard(spec.title, lambda bound=spec: bound.render(store), on_retry=on_retry)


def _render_chat(store: DataStore) -> None:
    render = getattr(chat_module, "render", None) if chat_module is not None else None
    if not callable(render):
        with panel_box("AI 问答", "路径 /chat。只读，不写业务库。"):
            show_unavailable(
                "未挂载 dashboard.chat（Grok 与工具白名单）。"
                "本页不调用模型，也不写入 dashboard_state.db。"
            )
        return
    render(store)


def _clock_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _paint_live(label: ui.label, tone: str) -> None:
    if tone == "ok":
        label.set_text(live_caption("ok"))
    elif tone == "warn":
        label.set_text(live_caption("warn"))
        label.classes(remove="ft-live-muted")
        label.classes(add="ft-live-warn")
        return
    else:
        label.set_text(live_caption("muted"))
        label.classes(remove="ft-live-warn")
        label.classes(add="ft-live-muted")
        return
    label.classes(remove="ft-live-warn ft-live-muted")


def _status_strip(store: DataStore) -> None:
    """Dense KPI strip on OVERVIEW. Read-only."""
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
        title="CRYPTO TERMINAL",
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
