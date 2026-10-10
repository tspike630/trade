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
from nicegui.elements.mixins.text_element import TextElement

from dashboard.auth import SecretFilter, verify_api_key
from dashboard.data import DataStore
from dashboard.panels.common import panel_box, panel_guard, show_unavailable
from dashboard.panels.markets import render_markets
from dashboard.panels.registry import PANELS
from dashboard.panels.risk import live_face
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
.topbar {
  position: sticky;
  top: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  gap: 24px;
  height: 48px;
  padding: 0 16px;
  background: #111111;
  border-bottom: 1px solid #1a1a1a;
  flex-wrap: nowrap;
}
.topbar .brand { display: flex; align-items: baseline; gap: 10px; white-space: nowrap; }
.topbar .brand .name {
  display: inline-flex;
  align-items: baseline;
  font-weight: 700;
  letter-spacing: 0.5px;
  color: #e5e5e5;
  font-size: 13px;
}
.topbar .brand .name span { margin-right: 2px; }
.topbar .brand .tag { font-size: 11px; color: #d97706; }
.topbar .status-group {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-left: auto;
  min-width: 0;
}
.topbar .live-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #16a34a;
  box-shadow: 0 0 6px #16a34a;
  flex: 0 0 auto;
}
.topbar .live-dot.tone-warn { background: #ca8a04; box-shadow: 0 0 6px #ca8a04; }
.topbar .live-dot.tone-muted { background: #525252; box-shadow: none; }
.topbar .live-caption,
.topbar .status-group .proc,
.topbar .status-group .clock {
  font-size: 11px;
  color: #808080;
  font-family: var(--mono);
  white-space: nowrap;
}
.topbar .actions { display: flex; gap: 6px; flex: 0 0 auto; }
.topbar .actions .q-btn {
  height: 26px !important;
  min-height: 26px !important;
  padding: 0 12px !important;
  font-size: 11px !important;
  background: #161616 !important;
  color: #e5e5e5 !important;
  border: 1px solid #222 !important;
  border-radius: 4px !important;
  box-shadow: none !important;
}
.topbar .actions .q-btn:hover,
.topbar .actions .q-btn:hover .q-btn__content {
  border-color: #d97706 !important;
  color: #d97706 !important;
}
.ft-body { width: 100%; max-width: 1440px; margin: 0 auto; padding: 0 12px 48px; box-sizing: border-box; }
.ft-tabbar {
  display: flex;
  height: 32px;
  min-height: 32px;
  border-bottom: 1px solid #1a1a1a;
  background: #0a0a0a;
}
.ft-tabbar .q-tabs__content {
  background: #0a0a0a;
  border-bottom: 1px solid #1a1a1a;
  min-height: 32px !important;
  height: 32px !important;
}
.ft-tabbar .q-tab {
  padding: 0 8px !important;
  height: 32px !important;
  min-height: 32px !important;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #808080;
  font-size: 12px !important;
  font-weight: 600;
}
.ft-tabbar .q-tab:hover { color: #d97706; background: #161616; }
.ft-tabbar .q-tab.q-tab--active {
  background: #161616;
  color: #d97706;
  font-weight: 600;
  border-bottom: 2px solid #d97706;
}
.ft-tabbar .q-tab__indicator, .ft-subtab .q-tab__indicator { display: none; }
.ft-tabbar .q-tab__content { min-height: 32px !important; padding: 0 !important; }
.ft-subtab .q-tab__content { min-height: 22px !important; padding: 0 !important; }
.topbar .actions .q-btn .q-btn__content { min-height: 26px !important; line-height: 26px; }
.ft-subtab {
  display: flex;
  height: auto;
  min-height: 0;
  gap: 4px;
  padding: 4px 8px;
  background: transparent;
}
.ft-subtab .q-tabs__content {
  background: transparent;
  border-bottom: none;
  min-height: 22px !important;
  height: 22px !important;
}
.ft-subtab .q-tab {
  padding: 0 10px !important;
  height: 22px !important;
  min-height: 22px !important;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #525252;
  font-size: 10px !important;
  font-weight: 400;
  border-radius: 3px;
}
.ft-subtab .q-tab:hover { color: #808080; background: #161616; }
.ft-subtab .q-tab.q-tab--active {
  color: #d97706;
  font-weight: 600;
  background: #1a1a1a;
  border-bottom: none;
}
.ft-panels, .ft-panels .q-tab-panel, .ft-subpanels, .ft-subpanels .q-tab-panel {
  background: transparent !important;
  padding: 8px 0 0;
  width: 100%;
}
.nicegui-tab-panel {
  align-items: stretch !important;
  width: 100%;
}
.nicegui-tab-panel > * { width: 100%; max-width: 100%; }
.ft-chain { min-width: 140px; margin-bottom: 8px; }
.statusbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 32px;
  padding: 0 12px;
  font-size: 10px;
  color: #808080;
  font-family: var(--mono);
  border-top: 1px solid #1a1a1a;
  background: #0a0a0a;
  position: sticky;
  bottom: 0;
  z-index: 40;
  margin-top: auto;
  white-space: nowrap;
}
.statusbar .left, .statusbar .right { display: flex; gap: 16px; align-items: center; min-width: 0; }
.statusbar .miss { color: #ca8a04; }
.overview-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
  width: 100%;
}
.overview-grid .card {
  background: #0a0a0a;
  border: 1px solid #1a1a1a;
  border-radius: 6px;
  padding: 12px;
  min-height: 110px;
  min-width: 0;
  box-sizing: border-box;
}
.overview-grid .card.full {
  grid-column: span 4;
  min-height: 0;
}
.overview-grid .card-label { font-size: 10px; color: #808080; }
.overview-grid .card-value {
  font-size: 24px;
  color: #e5e5e5;
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  margin-top: 6px;
  line-height: 1.2;
}
.overview-grid .card-source { font-size: 10px; color: #525252; margin-top: 6px; }
.overview-grid .card.tone-warn .card-value { color: #ca8a04; }
.overview-grid .card.tone-bad .card-value { color: #dc2626; }
.overview-grid .card.full .panel,
.overview-grid .card.full .table-scroll,
.overview-grid .card.full .q-table { width: 100%; }
.overview-grid .card.full .panel { margin-bottom: 0; }
.panel-head-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.gate-tag {
  font-size: 10px;
  font-family: var(--mono);
  color: #808080;
  background: #111111;
  border: 1px solid #222222;
  border-radius: 3px;
  padding: 1px 6px;
}
.gate-tag.tone-warn { color: #ca8a04; border-color: #6d5a2e; }
.gate-tag.tone-bad { color: #dc2626; border-color: #7a3036; }
.panel { width: 100%; box-sizing: border-box; }
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
  .overview-grid { grid-template-columns: repeat(2, 1fr); }
  .overview-grid .card.full { grid-column: span 2; }
  .topbar { gap: 12px; padding: 0 10px; }
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
        with ui.element("div").classes("overview-grid"):
            _overview_cards(store)
            with ui.element("div").classes("card full"):
                _render_panel(store, "positions", overview_tab.refresh)
            with ui.element("div").classes("card full"):
                _render_panel(store, "signals", overview_tab.refresh)
        _render_panel(store, "risk", overview_tab.refresh)
        for spec in PANELS:
            if panel_group(spec.id) == "overview" and spec.id not in {"risk", "positions"}:
                _render_panel(store, spec.id, overview_tab.refresh)
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

    status_bits = {"session": session_clock(0), "db": "库 —", "proc": "进程 —"}

    def sync_status() -> None:
        status_left.set_text(
            f"v0.1.0 · {status_bits['session']} · {status_bits['db']} · {status_bits['proc']}"
        )
        status_left.classes(remove="miss")
        if "缺失" in status_bits["db"]:
            status_left.classes(add="miss")

    def paint_clock() -> None:
        clock_lbl.set_text(_clock_text())
        age = int(time.monotonic() - refreshed_at["mono"])
        age_lbl.set_text(f"{refresh_age_label(age)} · 每 {interval}s")
        status_bits["session"] = session_clock(int(time.monotonic() - opened_at))
        sync_status()

    def paint_status() -> None:
        try:
            live = store.process_liveness()
            if not live.available:
                text, tone = "心跳：不可用", "muted"
            else:
                text, tone = heartbeat_summary([item.state for item in live.data])
            proc_lbl.set_text(text)
            status_bits["proc"] = text
            _paint_live(live_dot, live_lbl, tone)
            status_bits["db"] = db_status_line(store.file_status())
            sync_status()
        except Exception:
            log.warning("status strip failed")
            proc_lbl.set_text("心跳：不可用")
            status_bits["proc"] = "心跳：不可用"
            sync_status()

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
        with ui.element("div").classes("topbar"):
            with ui.element("div").classes("brand"):
                with ui.element("span").classes("name"):
                    _Word("CRYPTO")
                    _Word("TERMINAL")
                ui.label("链上新币投研终端").classes("tag")
            with ui.element("div").classes("status-group"):
                live_dot = ui.element("span").classes("live-dot")
                live_lbl = ui.label("实时").classes("live-caption")
                proc_lbl = ui.label("进程 —").classes("proc")
                clock_lbl = ui.label(_clock_text()).classes("clock")
            with ui.element("div").classes("actions"):
                ui.button("刷新", on_click=tick).props("unelevated dense no-caps")
                if api_secret():
                    ui.button("退出", on_click=_revoke_session).props("unelevated dense no-caps")
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
        with ui.element("div").classes("statusbar"):
            with ui.element("div").classes("left"):
                status_left = ui.label("v0.1.0 · 会话 00:00:00 · 库 — · 进程 —")
            with ui.element("div").classes("right"):
                age_lbl = ui.label(f"{refresh_age_label(0)} · 每 {interval}s")
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
    with ui.tabs(value=initial).props("align=left").classes("w-full ft-tabbar") as tabs:
        dash = ui.tab("DASHBOARD")
        markets = ui.tab("MARKETS")
        chat = ui.tab("AI CHAT")
    with ui.tab_panels(tabs, value=initial).classes("w-full ft-panels"):
        with ui.tab_panel(dash):
            with ui.tabs(value="OVERVIEW").props("align=left").classes("w-full ft-subtab") as sub:
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


def _render_panel(store: DataStore, panel_id: str, on_retry) -> None:
    for spec in PANELS:
        if spec.id != panel_id:
            continue
        panel_guard(spec.title, lambda bound=spec: bound.render(store), on_retry=on_retry)
        return


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


class _Word(TextElement):
    def __init__(self, text: str) -> None:
        super().__init__(tag="span", text=text)


def _paint_live(dot: ui.element, label: ui.label, tone: str) -> None:
    if tone == "ok":
        caption = live_caption("ok")
        dot_tone = ""
    elif tone == "warn":
        caption = live_caption("warn")
        dot_tone = "tone-warn"
    else:
        caption = live_caption("muted")
        dot_tone = "tone-muted"
    label.set_text(caption.removeprefix("● ").strip())
    dot.classes(remove="tone-warn tone-muted")
    if dot_tone:
        dot.classes(add=dot_tone)


def _overview_cards(store: DataStore) -> None:
    """Four OVERVIEW cards. Positions and the signal stream are full-width rows."""
    gates = read_gates()
    today = store.today_execution_stats()
    paper = "开" if gates.paper_enabled else "关"
    pm = "开" if gates.pm_enabled else "关"
    live_value, live_tone = live_face(gates)
    if today.available:
        signal_value = str(today.data.n)
        signal_source = "execution_log"
    else:
        signal_value = "—"
        signal_source = today.reason or "execution_log"
    _card("模拟盘闸", paper, "EXECUTION_PAPER_ENABLED", "tone-warn" if gates.paper_enabled else "")
    _card("仓位闸", pm, "PM_ENABLED", "tone-warn" if gates.pm_enabled else "")
    _card("实盘", live_value, gates.live_block_reason or "LIVE", live_tone)
    _card("今日信号", signal_value, signal_source)


def _card(label: str, value: str, source: str, tone: str = "") -> None:
    classes = "card"
    if tone:
        classes = f"{classes} {tone}"
    with ui.element("div").classes(classes):
        ui.label(label).classes("card-label")
        ui.label(value).classes("card-value")
        ui.label(source).classes("card-source")



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
