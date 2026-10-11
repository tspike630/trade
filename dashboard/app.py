"""NiceGUI entrypoint.

Start from the project root (the directory that contains ``dashboard/`` and the
SQLite files)::

    python -m dashboard.app

Binds 127.0.0.1 only. Default port is 8081 (``DASHBOARD_PORT``).
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Request
from nicegui import app, ui

from dashboard.auth import SecretFilter, verify_api_key
from dashboard.data import DataStore
from dashboard.format_util import fmt_rate
from dashboard.panels import risk as risk_panel
from dashboard.panels.common import col, panel_box, panel_guard, show_unavailable
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
    # Optional module: AI CHAT stays a stub when dashboard.chat is absent.
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
  width: 100%;
}
/* Top bar: brand / status-group (margin-left:auto) / actions */
.topbar, .ft-top {
  position: sticky;
  top: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  gap: 24px;
  height: 48px;
  min-height: 48px;
  padding: 0 16px;
  background: #111111;
  border-bottom: 1px solid #1a1a1a;
  width: 100%;
  box-sizing: border-box;
  flex-wrap: nowrap;
}
.topbar .brand, .ft-brand {
  display: flex;
  align-items: baseline;
  gap: 10px;
  white-space: nowrap;
  min-width: 0;
  flex-shrink: 0;
}
.topbar .brand .name, .ft-brand-name {
  font-weight: 700;
  letter-spacing: 0.5px;
  color: #e5e5e5;
  font-size: 13px;
}
.topbar .brand .name span, .ft-brand .name-word { margin-right: 2px; }
.ft-brand-crypto {
  color: #d97706;
  font-weight: 700;
  font-size: 13px;
  letter-spacing: 0.5px;
  margin-right: 2px;
}
.ft-brand-sub, .topbar .brand .tag {
  font-size: 11px;
  color: #d97706;
}
.topbar .status-group, .ft-status-group {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-left: auto;
  flex-shrink: 0;
}
.topbar .live-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #16a34a;
  box-shadow: 0 0 6px #16a34a;
}
.ft-live { color: #16a34a; font-size: 12px; font-family: var(--mono); }
.ft-live-warn { color: #ca8a04; }
.ft-live-muted { color: #525252; }
.ft-proc, .ft-clock, .ft-status-item, .statusbar .proc {
  color: #808080;
  font-family: var(--mono);
  font-size: 11px;
}
.topbar .actions, .ft-actions {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-shrink: 0;
}
.topbar .actions button, .ft-actions .q-btn, .ft-refresh, .ghost-btn.q-btn {
  height: 26px !important;
  min-height: 26px !important;
  padding: 0 12px !important;
  font-size: 11px !important;
  background: #161616 !important;
  color: #e5e5e5 !important;
  border: 1px solid #222222 !important;
  border-radius: 4px !important;
}
.topbar .actions button:hover, .ft-actions .q-btn:hover, .ft-refresh:hover, .ghost-btn.q-btn:hover {
  border-color: #d97706 !important;
  color: #d97706 !important;
}
.ft-body {
  width: 100%;
  max-width: 1440px;
  margin: 0 auto;
  padding: 0 12px 40px;
  flex: 1 1 auto;
  box-sizing: border-box;
}
/* Main tabs: 32px uppercase amber active */
.ft-tabbar .q-tabs__content {
  background: #0a0a0a;
  border-bottom: 1px solid #1a1a1a;
  min-height: 32px;
  height: 32px;
}
.ft-tabbar .q-tab {
  padding: 0 8px;
  min-height: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #808080;
  font-size: 12px;
  font-weight: 600;
  border-bottom: 2px solid transparent;
  cursor: pointer;
}
.ft-tabbar .q-tab:hover { color: #d97706; background: #161616; }
.ft-tabbar .q-tab.q-tab--active {
  background: #161616;
  color: #d97706;
  font-weight: 600;
  border-bottom-color: #d97706;
}
.ft-tabbar .q-tab__indicator, .ft-subtab .q-tab__indicator { display: none; }
/* Nested subtabs under DASHBOARD: 22px muted */
.ft-subtab .q-tabs__content {
  background: #0a0a0a;
  border-bottom: 1px solid #1a1a1a;
  min-height: 22px;
  height: 22px;
  padding: 4px 8px;
  gap: 4px;
}
.ft-subtab .q-tab {
  padding: 0 10px;
  min-height: 22px;
  height: 22px;
  display: flex;
  align-items: center;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #525252;
  font-size: 10px;
  border-radius: 3px;
  border-bottom: none;
  cursor: pointer;
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
  width: 100% !important;
  max-width: 100%;
  box-sizing: border-box;
}
.ft-panels, .ft-subpanels {
  width: 100% !important;
}
.ft-panels > .q-panel, .ft-subpanels > .q-panel,
.ft-panels .q-tab-panels, .ft-subpanels .q-tab-panels {
  width: 100% !important;
}
.ft-chain { min-width: 140px; margin-bottom: 8px; }
/* Single bottom statusbar */
.statusbar, .ft-status {
  position: sticky;
  bottom: 0;
  z-index: 40;
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 32px;
  min-height: 32px;
  padding: 0 12px;
  font-size: 10px;
  color: #808080;
  font-family: var(--mono);
  border-top: 1px solid #1a1a1a;
  background: #0a0a0a;
  width: 100%;
  box-sizing: border-box;
  flex-wrap: nowrap;
}
.statusbar .left, .statusbar .right {
  display: flex;
  gap: 16px;
  align-items: center;
}
.ft-status-miss { color: #ca8a04; }
/* Overview 4-col grid — fixes left-narrow / empty-right */
.overview-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
  width: 100%;
  box-sizing: border-box;
}
.overview-grid > .card {
  background: #0a0a0a;
  border: 1px solid #1a1a1a;
  border-radius: 6px;
  padding: 12px;
  min-height: 110px;
  min-width: 0;
  box-sizing: border-box;
}
.overview-grid > .card.full,
.overview-grid > .panel,
.overview-grid > section.panel {
  grid-column: 1 / -1;
  width: 100%;
  min-width: 0;
}
.overview-grid .card .kpi-label {
  color: #808080;
  font-size: 10px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.overview-grid .card .kpi-value {
  font-family: var(--mono);
  font-size: 24px;
  font-weight: 650;
  color: #e5e5e5;
  margin-top: 4px;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}
.overview-grid .card .kpi-hint {
  color: #525252;
  font-size: 10px;
  margin-top: 6px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.kpi-strip {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
  margin-top: 0;
  width: 100%;
}
.kpi-tile {
  background: #0a0a0a;
  border: 1px solid #1a1a1a;
  border-radius: 6px;
  border-left: 2px solid var(--accent);
  padding: 12px;
  min-width: 0;
  min-height: 110px;
}
.kpi-tile .kpi-label {
  color: #808080;
  font-size: 10px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.kpi-tile .kpi-value {
  font-family: var(--mono);
  font-size: 24px;
  font-weight: 650;
  margin-top: 4px;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  color: #e5e5e5;
}
.kpi-tile .kpi-hint {
  color: #525252;
  font-size: 10px;
  margin-top: 6px;
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
.overview-grid > .card.tone-warn .kpi-value { color: var(--warn); }
.overview-grid > .card.tone-bad .kpi-value { color: var(--bad); }
.overview-grid > .card.tone-ok .kpi-value { color: var(--mint); }
.overview-grid > .card.tone-bad { border-color: #7a3036; background: #1a1012; }
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
  border-radius: 6px;
  padding: 12px 12px 14px;
  margin-bottom: 0;
  width: 100%;
  box-sizing: border-box;
  min-width: 0;
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
  width: 100%;
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
.proc-row { display: flex; flex-wrap: wrap; gap: 8px; width: 100%; }
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
.footer-note { color: var(--muted); font-size: 11px; margin-top: 8px; font-family: var(--mono); grid-column: 1 / -1; }
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
  .overview-grid { grid-template-columns: repeat(2, 1fr); }
  .page { padding: 0 8px 28px; }
  .topbar, .ft-top { gap: 12px; height: auto; min-height: 48px; flex-wrap: wrap; padding: 8px 12px; }
}
/* ============ V2 网格组件体系（基准：dashboard_grid_skeleton_v2.html） ============ */
.gv2-grid {
  display: grid;
  grid-template-columns: repeat(12, 1fr);
  gap: 8px;
  width: 100%;
  box-sizing: border-box;
}
.gv2-kpi {
  background: #0a0a0a;
  border: 1px solid #1a1a1a;
  border-radius: 4px;
  padding: 8px 10px;
  min-height: 72px;
  grid-column: span 3;
  min-width: 0;
  box-sizing: border-box;
}
.gv2-kpi .kpi-label { color: #808080; font-size: 9px; letter-spacing: .06em; text-transform: uppercase; }
.gv2-kpi .kpi-value { font-family: var(--mono); font-size: 18px; font-weight: 650; margin-top: 2px; font-variant-numeric: tabular-nums; }
.gv2-kpi .kpi-hint  { color: #525252; font-size: 9px; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.gv2-kpi.tone-warn .kpi-value { color: var(--warn); }
.gv2-kpi.tone-bad  .kpi-value { color: var(--bad); }
.gv2-kpi.tone-ok   .kpi-value { color: var(--mint); }

.gv2-component {
  background: #0a0a0a;
  border: 1px solid #1a1a1a;
  border-radius: 4px;
  display: flex;
  flex-direction: column;
  min-width: 0;
  box-sizing: border-box;
  overflow: hidden;
}
.gv2-component.span-3 { grid-column: span 3; }
.gv2-component.span-4 { grid-column: span 4; }
.gv2-component.span-6 { grid-column: span 6; }
.gv2-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 10px;
  background: #111111;
  border-bottom: 1px solid #1a1a1a;
}
.gv2-title { font-size: 10px; letter-spacing: .1em; text-transform: uppercase; color: #808080; font-family: var(--mono); }
.gv2-title b { color: #e5e5e5; font-weight: 650; }
.gv2-tools { margin-left: auto; display: flex; gap: 4px; align-items: center; }
.gv2-tool {
  background: transparent; border: 1px solid #2a2a2a; color: #808080;
  font-size: 10px; padding: 1px 7px; border-radius: 3px; cursor: pointer; line-height: 1.5;
}
.gv2-tool:hover { color: #e5e5e5; border-color: var(--accent); }
.gv2-body { padding: 8px 10px; flex: 1; display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.gv2-foot {
  padding: 3px 10px; border-top: 1px solid #1a1a1a;
  font-size: 10px; color: #525252; display: flex; gap: 10px;
}
.gv2-kv { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; align-items: baseline; }
.gv2-kv .k { color: #808080; font-size: 10px; white-space: nowrap; }
.gv2-kv .v { font-size: 13px; font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
.gv2-kv .v.up { color: var(--mint); }
.gv2-kv .v.down { color: var(--bad); }
.gv2-kv .v.mute { color: #525252; }
.gv2-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.gv2-table th { text-align: left; color: #525252; font-weight: 500; padding: 2px 6px; border-bottom: 1px solid #1a1a1a; white-space: nowrap; }
.gv2-table th.num, .gv2-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
.gv2-table td { padding: 2px 6px; border-bottom: 1px solid rgba(26,26,26,.6); white-space: nowrap; }
.gv2-table tr:hover td { background: rgba(217,119,6,.05); }
.gv2-pulse { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; }
.gv2-pulse-item { border: 1px solid #1a1a1a; border-radius: 3px; padding: 5px 8px; }
.gv2-pulse-item .lbl { font-size: 9px; color: #525252; }
.gv2-pulse-item .val { font-size: 15px; font-variant-numeric: tabular-nums; margin-top: 1px; }
@media (max-width: 720px) {
  .gv2-grid { grid-template-columns: repeat(2, 1fr); }
  .gv2-component, .gv2-kpi { grid-column: span 2 !important; }
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
            ui.label("SPIKE'S CRYPTO TERMINER").classes("brand-kicker")
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
            ui.label("SPIKE'S CRYPTO TERMINER").classes("brand-kicker")
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
        with ui.element("div").classes("gv2-grid"):
            try:
                _gv2_kpi_strip(store)
            except Exception:
                log.warning("overview kpi strip failed")
                ui.label("—").classes("muted")
            _gv2_component("进程 · 心跳", "span-3", lambda: _gv2_procs(store))
            _gv2_component("风险 · 闸门", "span-3", lambda: _gv2_gates(), foot="闸门只读·环境变量不可改")
            _gv2_component(
                "先验 · 对照",
                "span-6",
                lambda: _gv2_priors(),
                foot="GMGN n=956 · self n=9317 · CoinGecko · RED-PUMP",
            )
            _gv2_component("今日 · EXECUTION_LOG", "span-4", lambda: _gv2_today(store))
            _gv2_component("近 7 日 · 决策", "span-4", lambda: _gv2_week(store))
            _gv2_component("信号 · 流", "span-4", lambda: _gv2_signals(store))
            _gv2_component("持仓 · 总览", "span-6", lambda: _gv2_positions(store))
            _gv2_component("回测 · 新闻", "span-6", lambda: _gv2_btnews(store))
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
        with ui.element("div").classes("topbar"):
            with ui.element("div").classes("brand"):
                ui.label("SPIKE'S").classes("ft-brand-crypto")
                ui.label("CRYPTO TERMINER").classes("ft-brand-name")
                ui.label("链上新币投研终端").classes("ft-brand-sub")
            with ui.element("div").classes("status-group"):
                live_lbl = ui.label("● 实时").classes("ft-live")
                proc_lbl = ui.label("进程 —").classes("ft-proc")
                clock_lbl = ui.label(_clock_text()).classes("ft-clock")
            with ui.element("div").classes("actions"):
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
        with ui.element("div").classes("statusbar"):
            with ui.element("div").classes("left"):
                ui.label("v0.1.0").classes("ft-status-item")
                up_lbl = ui.label(session_clock(0)).classes("ft-status-item")
                db_lbl = ui.label("库 —").classes("ft-status-item")
                foot_proc = ui.label("进程 —").classes("ft-status-item")
            with ui.element("div").classes("right"):
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


def _gv2_kpi(label_: str, value: str, hint: str = "", tone: str = "") -> None:
    classes = "gv2-kpi"
    if tone:
        classes = f"{classes} {tone}"
    with ui.element("div").classes(classes):
        ui.label(label_).classes("kpi-label")
        ui.label(value).classes("kpi-value")
        if hint:
            ui.label(hint).classes("kpi-hint")


def _gv2_kpi_strip(store: DataStore) -> None:
    gates = read_gates()
    today = store.today_execution_stats()
    paper_on = bool(getattr(gates, "paper_enabled", False))
    pm_on = bool(getattr(gates, "pm_enabled", False))
    live_on = bool(getattr(gates, "live_effective", False))
    count = "—"
    if today.available and today.data is not None:
        raw = getattr(today.data, "n", None)
        if raw is not None:
            count = str(raw)
    reason = getattr(gates, "live_block_reason", None) or "LIVE"
    _gv2_kpi("模拟盘", "ON" if paper_on else "OFF", "PAPER", "tone-warn" if paper_on else "tone-ok")
    _gv2_kpi("仓位闸", "ON" if pm_on else "OFF", "PM", "tone-warn" if pm_on else "tone-ok")
    _gv2_kpi("实盘", "ON" if live_on else "OFF", str(reason), "tone-bad" if live_on else "tone-ok")
    _gv2_kpi("今日信号", count, "execution_log")


def _gv2_component(title: str, span: str, body_fn: Callable[[], None], foot: str = "") -> None:
    with ui.element("div").classes(f"gv2-component {span}"):
        with ui.element("div").classes("gv2-head"):
            ui.label(title).classes("gv2-title")
            with ui.element("div").classes("gv2-tools"):
                ui.button("⟳", on_click=lambda: None).props("flat dense size=xs").classes("gv2-tool")
        with ui.element("div").classes("gv2-body"):
            try:
                body_fn()
            except Exception:
                log.warning("overview component failed: %s", title)
                ui.label("—").classes("muted")
        if foot:
            with ui.element("div").classes("gv2-foot"):
                ui.label(foot).classes("gv2-foot-text")


def _gv2_kv(pairs: list[tuple[str, str, str]]) -> None:
    with ui.element("div").classes("gv2-kv"):
        for key, value, tone in pairs:
            ui.label(key).classes("k")
            ui.label(value).classes(f"v {tone}".rstrip())


def _gv2_text(value: object, limit: int | None = None) -> str:
    if value is None:
        return "—"
    text = str(value).strip()
    if not text:
        return "—"
    if limit is not None:
        return text[:limit]
    return text


def _as_int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _gv2_money(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return f"${value:,.0f}"


def _result_rows(result: object) -> list[object]:
    if not getattr(result, "available", False):
        return []
    data = getattr(result, "data", None)
    if data is None:
        return []
    rows = getattr(data, "rows", None)
    source = rows if rows is not None else data
    if isinstance(source, (str, bytes)):
        return []
    try:
        return list(source)
    except TypeError:
        return []


def _gv2_procs(store: DataStore) -> None:
    live = store.process_liveness()
    if not live.available:
        ui.label("心跳：不可用").classes("muted")
        return
    probes = _result_rows(live)
    if not probes:
        ui.label("心跳：不可用").classes("muted")
        return
    pairs: list[tuple[str, str, str]] = []
    for probe in probes:
        name = _gv2_text(getattr(probe, "name", None))
        state = _gv2_text(getattr(probe, "state", None))
        pairs.append((name, state, "up" if state == "alive" else "mute"))
    _gv2_kv(pairs)


def _gv2_gates() -> None:
    gates = read_gates()
    paper_on = bool(getattr(gates, "paper_enabled", False))
    pm_on = bool(getattr(gates, "pm_enabled", False))
    live_on = bool(getattr(gates, "live_effective", False))
    _gv2_kv([
        ("模拟盘执行", "ON" if paper_on else "关", "up" if paper_on else "mute"),
        ("仓位管理", "ON" if pm_on else "关", "up" if pm_on else "mute"),
        ("实盘", "ON" if live_on else "关", "down" if live_on else "mute"),
    ])


def _ratio_text(value: object, digits: int) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    number = float(value)
    if abs(number) <= 1:
        number *= 100
    return f"{number:.{digits}f}%"


def _read_prior_baseline() -> dict[str, object]:
    """baseline_params.json is the published prior pack. risk.py has no loader yet."""
    relative = (
        Path("data") / "prior" / "exports" / "baseline_params.json",
        Path("crawler") / "data" / "prior" / "exports" / "baseline_params.json",
    )
    roots: list[Path] = []
    try:
        roots.append(db_root())
    except Exception:
        log.warning("prior baseline root unavailable")
    roots.append(Path(__file__).resolve().parent.parent)
    seen: set[Path] = set()
    for root in roots:
        for name in relative:
            path = (root / name).resolve()
            if path in seen:
                continue
            seen.add(path)
            try:
                if not path.is_file():
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                log.warning("prior baseline unreadable: %s", path.name)
                continue
            if isinstance(payload, dict):
                return payload
    return {}


def _mapping(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    return {}


def _prior_from_risk() -> list[tuple[str, str]] | None:
    load = getattr(risk_panel, "_load_prior_kpis", None)
    fmt_peak = getattr(risk_panel, "_fmt_peak_min", None)
    fmt_pct1 = getattr(risk_panel, "_fmt_pct1", None)
    fmt_pct2 = getattr(risk_panel, "_fmt_pct2", None)
    if not all(callable(fn) for fn in (load, fmt_peak, fmt_pct1, fmt_pct2)):
        return None
    try:
        loaded = load()
    except Exception:
        log.warning("prior kpi load failed")
        return None
    if loaded is None:
        return None
    return [
        ("见顶 P50", str(fmt_peak(getattr(loaded, "peak_p50_min", None)))),
        ("4H 存活", str(fmt_pct1(getattr(loaded, "survive_4h", None)))),
        ("当天死", str(fmt_pct2(getattr(loaded, "same_day_death", None)))),
        ("毕业率", str(fmt_pct1(getattr(loaded, "graduation_rate", None)))),
    ]


def _prior_from_baseline(payload: dict[str, object]) -> list[tuple[str, str]]:
    gmgn = _mapping(payload.get("gmgn"))
    self_db = _mapping(payload.get("self_db"))
    survive = _mapping(self_db.get("survive"))
    coingecko = _mapping(payload.get("coingecko"))
    redpump = _mapping(payload.get("redpump"))
    trio = _mapping(payload.get("weekly_baseline_trio"))
    peak = gmgn.get("median_peak_min")
    if isinstance(peak, (int, float)) and not isinstance(peak, bool):
        peak_text = f"{float(peak):.1f} min"
    else:
        peak_text = "—"
    death = trio.get("same_day_death_rate", coingecko.get("same_day_death_rate"))
    grad = trio.get("graduation_rate", redpump.get("graduation_rate"))
    return [
        ("见顶 P50", peak_text),
        ("4H 存活", _ratio_text(survive.get("4h"), 1)),
        ("当天死", _ratio_text(death, 2)),
        ("毕业率", _ratio_text(grad, 1)),
    ]


def _gv2_prior_values() -> list[tuple[str, str]]:
    loaded = _prior_from_risk()
    if loaded is not None:
        return loaded
    payload = _read_prior_baseline()
    if payload:
        return _prior_from_baseline(payload)
    # Same three literals risk._priors already paints when the pack is absent.
    return [
        ("见顶 P50", "1.0 min"),
        ("4H 存活", "75.3%"),
        ("当天死", "68.67%"),
        ("毕业率", "—"),
    ]


def _gv2_priors() -> None:
    with ui.element("div").classes("gv2-pulse"):
        for label_, value in _gv2_prior_values():
            with ui.element("div").classes("gv2-pulse-item"):
                ui.label(label_).classes("lbl")
                ui.label(value).classes("val")


def _gv2_today(store: DataStore) -> None:
    result = store.today_execution_stats()
    if not result.available or result.data is None:
        ui.label("暂无数据").classes("muted")
        return
    block = result.data
    total = _as_int(getattr(block, "n", None))
    _gv2_kv([
        ("信号数", str(getattr(block, "n", "—")), ""),
        (
            "校验通过",
            fmt_rate(getattr(block, "ok_rate", None), _as_int(getattr(block, "n_ok", None)), total),
            "up",
        ),
        (
            "已执行",
            fmt_rate(
                getattr(block, "executed_rate", None),
                _as_int(getattr(block, "n_executed", None)),
                total,
            ),
            "mute",
        ),
    ])


def _gv2_week(store: DataStore) -> None:
    result = store.week_decision_stats()
    if not result.available or result.data is None:
        ui.label("暂无数据").classes("muted")
        return
    block = result.data
    # DecisionWindowStats rates use n_with_exec. There is no `n` field.
    denominator = _as_int(getattr(block, "n_with_exec", None))
    _gv2_kv([
        ("决策数", str(getattr(block, "n_decisions", "—")), ""),
        ("有执行", str(getattr(block, "n_with_exec", "—")), ""),
        (
            "通过率",
            fmt_rate(getattr(block, "ok_rate", None), _as_int(getattr(block, "n_ok", None)), denominator),
            "up",
        ),
        (
            "执行率",
            fmt_rate(
                getattr(block, "executed_rate", None),
                _as_int(getattr(block, "n_executed", None)),
                denominator,
            ),
            "mute",
        ),
    ])


def _gv2_signals(store: DataStore) -> None:
    result = store.recent_signals(limit=5)
    rows = _result_rows(result)
    if not rows:
        ui.label("暂无信号").classes("muted")
        return
    table_rows: list[dict[str, str]] = []
    for index, item in enumerate(rows):
        size = getattr(item, "size_pct", None)
        if isinstance(size, (int, float)) and not isinstance(size, bool):
            size_text = f"{size:.2f}"
        else:
            size_text = "—"
        # decision_id is unique. ts collides when two signals share a timestamp.
        key = getattr(item, "decision_id", None)
        table_rows.append(
            {
                "key": str(key) if key is not None else str(index),
                "ts": _gv2_text(getattr(item, "ts", None)),
                "strategy_id": _gv2_text(getattr(item, "strategy_id", None)),
                "action": _gv2_text(getattr(item, "action", None)),
                "size_pct": size_text,
            }
        )
    with ui.element("div").classes("table-scroll"):
        table = ui.table(
            columns=[
                col("ts", "时间", "right"),
                col("strategy_id", "策略"),
                col("action", "动作"),
                col("size_pct", "仓位", "right"),
            ],
            rows=table_rows,
            row_key="key",
            pagination=5,
        )
        table.classes("w-full gv2-table")


def _gv2_positions(store: DataStore) -> None:
    result = store.open_positions()
    if not result.available or result.data is None:
        ui.label("暂无数据").classes("muted")
        return
    payload = result.data
    cash_raw = getattr(payload, "cash_usd", None)
    equity_raw = getattr(payload, "equity_usd", None)
    cash = _gv2_money(cash_raw)
    equity = _gv2_money(equity_raw) if equity_raw is not None else cash
    held_rows = getattr(payload, "rows", None)
    if held_rows is None:
        count = getattr(payload, "open_count", None)
        held = str(count) if count is not None else "—"
    else:
        held = str(len(tuple(held_rows)))
    pct = getattr(payload, "position_pct", None)
    if isinstance(pct, (int, float)) and not isinstance(pct, bool):
        share = f"{float(pct):.2f}%"
    else:
        ratio = getattr(payload, "position_ratio", None)
        if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
            share = f"{float(ratio) * 100:.2f}%"
        else:
            share = "—"
    _gv2_kv([
        ("总权益", equity, ""),
        ("现金", cash, ""),
        ("持仓数", held, ""),
        ("仓位占比", share, ""),
    ])


def _gv2_btnews(store: DataStore) -> None:
    result = store.latest_backtest()
    if result.available and result.data is not None:
        run = result.data
        end = getattr(run, "end_equity", None)
        if isinstance(end, (int, float)) and not isinstance(end, bool):
            equity = f"${end:,.0f}"
            tone = "up" if end >= 10000 else "down"
        else:
            equity = "—"
            tone = "mute"
        done = getattr(run, "done_at", None)
        if done in (None, ""):
            done = getattr(run, "finished_at", None)
        _gv2_kv([
            ("最近回测", _gv2_text(getattr(run, "run_id", None), 16), "mute"),
            ("权益", equity, tone),
            ("完成", _gv2_text(done), "mute"),
        ])
    else:
        _gv2_kv([("最近回测", "暂无", "mute")])
    items = _result_rows(store.latest_news(limit=3))
    if not items:
        return
    with ui.element("div").classes("gv2-body"):
        for item in items[:3]:
            with ui.element("div").classes("news-item"):
                ui.label(_gv2_text(getattr(item, "title", None))).classes("news-link")


def _status_strip(store: DataStore) -> None:
    """Dense KPI strip on OVERVIEW. Read-only."""
    gates = read_gates()
    today = store.today_execution_stats()
    pos = store.open_positions()

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

    # Four primary KPI cards fill the first overview-grid row.
    _tile("模拟盘", paper, "PAPER", paper_tone)
    _tile("仓位闸", pm, "PM", pm_tone)
    _tile("实盘", live_lbl, gates.live_block_reason or "LIVE", live_tone)
    _tile("今日信号", n, "execution_log")
    # Remaining stats continue on subsequent grid rows (still 4-col).
    _tile("校验通过", ok, "validation_ok")
    _tile("已执行", exe, "executed")
    _tile("持仓数", n_pos, "paper positions")
    _tile("权益$", eq, "cash+mtm")


def _tile(label: str, value: str, hint: str = "", tone: str = "") -> None:
    classes = "card"
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
        title="SPIKE'S CRYPTO TERMINER",
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
