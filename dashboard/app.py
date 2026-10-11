"""NiceGUI entrypoint.

Start from the project root (the directory that contains ``dashboard/`` and the
SQLite files)::

    python -m dashboard.app

Binds 127.0.0.1 only. Default port is 8081 (``DASHBOARD_PORT``).
"""

from __future__ import annotations

import json
import logging
import resource
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
from dashboard.format_util import liveness_label
from dashboard.panels import risk as risk_panel
from dashboard.panels.common import panel_box, panel_guard, show_unavailable
from dashboard.panels.markets import render_markets
from dashboard.panels.registry import PANELS
from dashboard.panels.shell import (
    db_status_line,
    heartbeat_summary,
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
  --bg: #0d1117;
  --bg-2: #161b22;
  --panel: #161b22;
  --panel-2: #1f2630;
  --line: #2d333b;
  --border: #2d333b;
  --border-strong: #3d444d;
  --text: #e6edf3;
  --fg: #e6edf3;
  --muted: #8b949e;
  --fg-dim: #8b949e;
  --fg-mute: #565d66;
  --accent: #2f81f7;
  --accent-dim: #12243d;
  --mint: #3fb950;
  --up: #3fb950;
  --mint-dim: #0d2416;
  --warn: #d29922;
  --warn-dim: #2a2410;
  --bad: #f85149;
  --down: #f85149;
  --bad-dim: #2a1416;
  --ok: #3fb950;
  --mono: "SF Mono", "Cascadia Code", "JetBrains Mono", Consolas, "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
  --sans: "SF Mono", "Cascadia Code", "JetBrains Mono", Consolas, "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
}
html, body, body.body--dark {
  background: var(--bg) !important;
  color: var(--text) !important;
  font-family: var(--sans);
  font-size: 12px;
  line-height: 1.5;
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
  gap: 0;
  height: 40px;
  min-height: 40px;
  padding: 0 4px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  width: 100%;
  box-sizing: border-box;
  flex-wrap: nowrap;
  overflow-x: auto;
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
  color: var(--fg);
  font-size: 13px;
}
.topbar .brand .name span, .ft-brand .name-word { margin-right: 2px; }
.ft-brand-crypto {
  color: var(--fg);
  font-weight: 700;
  font-size: 13px;
  letter-spacing: 0.5px;
  margin-right: 2px;
}
.ft-brand-sub, .topbar .brand .tag {
  font-size: 10px;
  font-weight: 400;
  color: var(--fg-mute);
  margin-left: 6px;
}
.topbar .status-group, .ft-status-group {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-left: auto;
  flex-shrink: 0;
}
.topbar .live-dot, .status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--up);
  box-shadow: 0 0 4px var(--up);
  display: inline-block;
  flex-shrink: 0;
}
.ft-live { color: var(--fg-dim); font-size: 11px; font-family: var(--mono); }
.ft-live-warn { color: var(--warn); }
.ft-live-muted { color: var(--fg-mute); }
.ft-proc, .ft-clock, .ft-status-item, .statusbar .proc {
  color: var(--fg-dim);
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
  height: 22px !important;
  min-height: 22px !important;
  padding: 0 8px !important;
  font-size: 10px !important;
  background: transparent !important;
  color: var(--fg-dim) !important;
  border: 1px solid var(--border-strong) !important;
  border-radius: 3px !important;
}
.topbar .actions button:hover, .ft-actions .q-btn:hover, .ft-refresh:hover, .ghost-btn.q-btn:hover {
  border-color: var(--accent) !important;
  color: var(--fg) !important;
}
.ft-body {
  width: 100%;
  max-width: none;
  margin: 0;
  padding: 0 0 8px;
  flex: 1 1 auto;
  box-sizing: border-box;
}
/* Main tabs sit in the top bar: 40px, accent underline */
.ft-tabbar { height: 40px; background: transparent; flex-shrink: 0; }
.ft-tabbar .q-tabs__content {
  background: transparent;
  border-bottom: none;
  min-height: 40px;
  height: 40px;
}
.ft-tabbar .q-tab {
  padding: 0 12px;
  min-height: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  text-transform: uppercase;
  letter-spacing: 0.8px;
  color: var(--fg-dim);
  font-size: 11px;
  font-weight: 500;
  border-bottom: 2px solid transparent;
  cursor: pointer;
}
.ft-tabbar .q-tab .q-tab__label { color: inherit; font-size: 11px; letter-spacing: 0.8px; }
.ft-tabbar .q-tab:hover { color: var(--fg); background: rgba(255,255,255,.03); }
.ft-tabbar .q-tab.q-tab--active {
  background: transparent;
  color: var(--fg);
  font-weight: 600;
  border-bottom-color: var(--accent);
}
.ft-tabbar .q-tab__indicator, .ft-subtab .q-tab__indicator { display: none; }
/* Nested subtabs under DASHBOARD */
.ft-subtab .q-tabs__content {
  background: var(--bg);
  border-bottom: 1px solid var(--border);
  min-height: 30px;
  height: 30px;
  padding: 0 8px;
  gap: 2px;
}
.ft-subtab .q-tab {
  padding: 0 12px;
  min-height: 26px;
  height: 26px;
  display: flex;
  align-items: center;
  text-transform: none;
  letter-spacing: 0;
  color: var(--fg-dim);
  font-size: 11px;
  border-radius: 4px 4px 0 0;
  border: 1px solid transparent;
  border-bottom: none;
  cursor: pointer;
}
.ft-subtab .q-tab .q-tab__label { color: inherit; font-size: 11px; }
.ft-subtab .q-tab:hover { color: var(--fg); background: rgba(255,255,255,.03); }
.ft-subtab .q-tab.q-tab--active {
  color: var(--fg);
  font-weight: 500;
  background: var(--panel);
  border-color: var(--border);
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
  height: 26px;
  min-height: 26px;
  padding: 0 12px;
  font-size: 10px;
  color: var(--fg-dim);
  font-family: var(--mono);
  border-top: 1px solid var(--border);
  background: var(--panel);
  width: 100%;
  box-sizing: border-box;
  flex-wrap: nowrap;
}
.ft-status-ok { color: var(--up); }
.ft-status-sep { color: var(--border-strong); }
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
.q-table thead tr { background: var(--panel-2); }
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
  background: radial-gradient(ellipse at top, #161b22 0%, var(--bg) 55%);
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
.enter-btn { background: var(--accent) !important; color: #fff !important; font-weight: 700; }
.q-field__native, .q-field__label { color: var(--text) !important; }
@media (max-width: 720px) {
  .brand-title { font-size: 18px; }
  .kpi-value { font-size: 18px; }
  .overview-grid { grid-template-columns: repeat(2, 1fr); }
  .page { padding: 0 8px 28px; }
  .topbar, .ft-top { gap: 12px; height: auto; min-height: 48px; flex-wrap: wrap; padding: 8px 12px; }
}
/* ============ 网格组件（对齐 dashboard_grid_skeleton_v2） ============ */
.gv2-grid {
  display: grid;
  grid-template-columns: repeat(12, 1fr);
  gap: 8px;
  width: 100%;
  box-sizing: border-box;
}
.gv2-component {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 3px;
  display: flex;
  flex-direction: column;
  min-width: 0;
  box-sizing: border-box;
  overflow: hidden;
}
.gv2-component.span-3 { grid-column: span 3; }
.gv2-component.span-4 { grid-column: span 4; }
.gv2-component.span-6 { grid-column: span 6; }
.gv2-component.span-8 { grid-column: span 8; }
.gv2-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 10px;
  background: var(--panel-2);
  border-bottom: 1px solid var(--border);
}
.gv2-title {
  display: flex;
  align-items: baseline;
  gap: 4px;
  font-size: 11px;
  letter-spacing: 1px;
  text-transform: uppercase;
  color: var(--fg-dim);
  font-family: var(--mono);
}
.gv2-title .em { color: var(--fg); font-weight: 600; letter-spacing: 1px; }
.gv2-title .suffix { color: var(--fg-dim); letter-spacing: 0; text-transform: none; }
.gv2-tools { margin-left: auto; display: flex; gap: 4px; align-items: center; }
.gv2-tool {
  background: transparent;
  border: 1px solid var(--border-strong);
  color: var(--fg-dim);
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 3px;
  line-height: 1.4;
}
.gv2-tool-note { font-size: 10px; color: var(--fg-mute); }
.gv2-body { padding: 8px 10px; flex: 1; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.gv2-foot {
  padding: 4px 10px;
  border-top: 1px solid var(--border);
  font-size: 10px;
  color: var(--fg-mute);
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.gv2-kv { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; align-items: baseline; }
.gv2-kv .k { color: var(--fg-dim); font-size: 11px; white-space: nowrap; }
.gv2-kv .v { font-size: 13px; font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; color: var(--fg); }
.gv2-kv .v.up { color: var(--up); }
.gv2-kv .v.down { color: var(--down); }
.gv2-kv .v.mute { color: var(--fg-mute); }
.gv2-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.gv2-table th { text-align: left; color: var(--fg-mute); font-weight: 500; padding: 3px 6px; border-bottom: 1px solid var(--border); white-space: nowrap; }
.gv2-table th.num, .gv2-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
.gv2-table td { padding: 3px 6px; border-bottom: 1px solid rgba(45,51,59,.5); white-space: nowrap; color: var(--fg); }
.gv2-table tr:hover td { background: rgba(47,129,247,.06); }
.gv2-table .nicegui-label { font-size: inherit; line-height: inherit; }
.gv2-pulse { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; }
.gv2-pulse-item { border: 1px solid var(--border); border-radius: 3px; padding: 6px 8px; }
.gv2-pulse-item .lbl { font-size: 10px; color: var(--fg-mute); }
.gv2-pulse-item .val { font-size: 15px; font-variant-numeric: tabular-nums; margin-top: 2px; color: var(--fg); }
.gv2-note { color: var(--fg-mute); font-size: 10px; font-style: italic; }
@media (max-width: 720px) {
  .gv2-grid { grid-template-columns: repeat(2, 1fr); }
  .gv2-component { grid-column: span 2 !important; }
  .gv2-pulse { grid-template-columns: repeat(2, 1fr); }
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
    ui.colors(primary="#2f81f7")
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
        prior_values, prior_foot = _gv2_prior_pack()
        proc_foot: list[str] = ["心跳不可用"]
        jev_foot: list[str] = ["门槛 n≥15 / unique≥10", "过滤后样本未读到"]
        with ui.element("div").classes("gv2-grid"):
            _gv2_component(
                "进程",
                "心跳",
                "span-3",
                lambda: _gv2_procs(store, proc_foot),
                tool="⟳",
                foot=proc_foot,
            )
            _gv2_component(
                "风险",
                "闸门",
                "span-3",
                _gv2_gates,
                tool="只读",
                foot=["闸门只读，不调权"],
            )
            _gv2_component(
                "先验",
                "对照",
                "span-6",
                lambda: _gv2_pulse(prior_values),
                tool="观察中·不调权",
                tool_plain=True,
                foot=prior_foot,
            )
            _gv2_component(
                "今日",
                "EXECUTION_LOG",
                "span-4",
                lambda: _gv2_today(store),
                tool="UTC 日",
                foot=["execution_log 当日行"],
            )
            _gv2_component(
                "近 7 日",
                "决策",
                "span-4",
                lambda: _gv2_week(store),
                tool="168h",
                foot=["decision_log 滚动 168h"],
            )
            _gv2_component(
                "JEV",
                "门槛",
                "span-4",
                lambda: _gv2_jev(store, jev_foot),
                tool="过滤后",
                tool_plain=True,
                foot=jev_foot,
            )
            _gv2_component(
                "市场",
                "基准",
                "span-8",
                lambda: _gv2_hot(store),
                suffix="（hot）",
                tool="winsor",
                foot=["indicator_history tier=hot", "winsor |x|≤500"],
            )
            _gv2_component(
                "策略",
                "速览",
                "span-4",
                lambda: _gv2_strategies(store),
                tool="⟳",
                foot=["decision_log 按 strategy_id"],
            )

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
        mem_lbl.set_text(_rss_label())

    def paint_status() -> None:
        try:
            live = store.process_liveness()
            if not live.available:
                text, tone = "心跳：不可用", "muted"
            else:
                text, tone = heartbeat_summary([item.state for item in live.data])
            proc_lbl.set_text(text)
            _paint_live(live_lbl, tone)
            files = store.file_status()
            db_text = db_status_line(files)
            db_lbl.set_text(db_text)
            db_lbl.classes(remove="ft-status-ok ft-status-miss")
            if "缺失" in db_text:
                db_lbl.classes(add="ft-status-miss")
                ready_lbl.set_text("● 降级")
                ready_lbl.classes(remove="ft-status-ok")
                ready_lbl.classes(add="ft-live-warn")
            else:
                db_lbl.classes(add="ft-status-ok")
                ready_lbl.set_text("● 就绪")
                ready_lbl.classes(remove="ft-live-warn")
                ready_lbl.classes(add="ft-status-ok")
            main_ok = any(label == "主库" and present for label, _name, present in files)
            src_lbl.set_text("数据源 已连接" if main_ok else "数据源 未连接")
            src_lbl.classes(remove="ft-status-ok ft-status-miss")
            src_lbl.classes(add="ft-status-ok" if main_ok else "ft-status-miss")
        except Exception:
            log.warning("status strip failed")
            proc_lbl.set_text("心跳：不可用")
            ready_lbl.set_text("● 降级")

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
        initial = "AI CHAT" if section == "chat" else "DASHBOARD"
        with ui.element("div").classes("topbar"):
            with ui.element("div").classes("brand"):
                ui.label("SPIKE'S CRYPTO TERMINER").classes("ft-brand-name")
                ui.label("链上新币投研终端").classes("ft-brand-sub")
            with ui.tabs(value=initial).classes("ft-tabbar") as tabs:
                dash = ui.tab("DASHBOARD")
                markets = ui.tab("MARKETS")
                chat = ui.tab("AI CHAT")
            with ui.element("div").classes("status-group"):
                proc_lbl = ui.label("进程 —").classes("ft-proc")
                clock_lbl = ui.label(_clock_text()).classes("ft-clock")
                ui.element("span").classes("status-dot")
                live_lbl = ui.label("实时").classes("ft-live")
            with ui.element("div").classes("actions"):
                ui.button("刷新", on_click=tick).props("flat dense size=sm").classes("ft-refresh")
                if api_secret():
                    ui.button("退出", on_click=_revoke_session).props("flat dense size=sm").classes("ghost-btn")
        with ui.element("div").classes("ft-body"):
            if insecure:
                ui.label("本地未鉴权模式（ALLOW_INSECURE_API=1）。不要暴露到公网。").classes("banner")
            _module_panels(
                initial,
                tabs,
                dash,
                markets,
                chat,
                overview_tab,
                signals_tab,
                execution_tab,
                markets_tab,
                chat_tab,
            )
        with ui.element("div").classes("statusbar"):
            with ui.element("div").classes("left"):
                ui.label("v0.1.0").classes("ft-status-item")
                ui.label("·").classes("ft-status-sep")
                up_lbl = ui.label(session_clock(0)).classes("ft-status-item")
                ui.label("·").classes("ft-status-sep")
                db_lbl = ui.label("库 —").classes("ft-status-item")
                ui.label("·").classes("ft-status-sep")
                src_lbl = ui.label("数据源 —").classes("ft-status-item")
                ui.label("·").classes("ft-status-sep")
                age_lbl = ui.label(refresh_age_label(0)).classes("ft-status-item")
                ui.label("·").classes("ft-status-sep")
                mem_lbl = ui.label(_rss_label()).classes("ft-status-item")
            with ui.element("div").classes("right"):
                ready_lbl = ui.label("● 就绪").classes("ft-status-ok")
    paint_status()
    paint_clock()
    ui.timer(1.0, paint_clock)
    ui.timer(refresh_seconds(), tick, immediate=False)


def _module_panels(
    initial: str,
    tabs: ui.tabs,
    dash: ui.tab,
    markets: ui.tab,
    chat: ui.tab,
    overview_tab: ui.refreshable,
    signals_tab: ui.refreshable,
    execution_tab: ui.refreshable,
    markets_tab: ui.refreshable,
    chat_tab: ui.refreshable,
) -> None:
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
        label.set_text("实时")
        label.classes(remove="ft-live-warn ft-live-muted")
        return
    if tone == "warn":
        label.set_text("停滞")
        label.classes(remove="ft-live-muted")
        label.classes(add="ft-live-warn")
        return
    label.set_text("未知")
    label.classes(remove="ft-live-warn")
    label.classes(add="ft-live-muted")


def _gv2_component(
    prefix: str,
    emphasis: str,
    span: str,
    body_fn: Callable[[], None],
    *,
    suffix: str = "",
    tool: str = "",
    tool_plain: bool = False,
    foot: list[str] | None = None,
) -> None:
    with ui.element("div").classes(f"gv2-component {span}"):
        with ui.element("div").classes("gv2-head"):
            with ui.element("div").classes("gv2-title"):
                ui.label(prefix)
                ui.label(emphasis).classes("em")
                if suffix:
                    ui.label(suffix).classes("suffix")
            if tool:
                with ui.element("div").classes("gv2-tools"):
                    ui.label(tool).classes("gv2-tool-note" if tool_plain else "gv2-tool")
        with ui.element("div").classes("gv2-body"):
            try:
                body_fn()
            except Exception:
                log.warning("overview component failed: %s %s", prefix, emphasis)
                ui.label("—").classes("muted")
        parts = [part for part in (foot or []) if part]
        if parts:
            with ui.element("div").classes("gv2-foot"):
                for part in parts:
                    ui.label(part)


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


def _gv2_procs(store: DataStore, foot: list[str]) -> None:
    slots = ("主爬虫", "指标", "JEV", "决策引擎", "执行网关")
    live = store.process_liveness()
    probes = _result_rows(live) if live.available else []
    by_name = {str(getattr(probe, "name", "")): probe for probe in probes}
    pairs: list[tuple[str, str, str]] = []
    for name in slots:
        probe = by_name.get(name)
        if probe is None:
            pairs.append((name, "—", "mute"))
            continue
        state = getattr(probe, "state", None)
        if state == "alive":
            pairs.append((name, liveness_label("alive"), "up"))
        elif state == "stale":
            pairs.append((name, liveness_label("stale"), "down"))
        else:
            pairs.append((name, "—", "mute"))
    _gv2_kv(pairs)
    known = [probe for probe in probes if getattr(probe, "name", None) in slots[:3]]
    if not known:
        foot[:] = ["心跳不可用"]
        return
    alive = sum(1 for probe in known if getattr(probe, "state", None) == "alive")
    ages = [
        float(age)
        for probe in known
        if isinstance((age := getattr(probe, "age_seconds", None)), (int, float)) and not isinstance(age, bool)
    ]
    age_text = f"{int(min(ages))}s 前" if ages else "--s 前"
    foot[:] = [f"进程 {alive}/{len(known)} 存活", f"上次心跳 {age_text}"]


def _gv2_gates() -> None:
    gates = read_gates()
    paper_on = bool(getattr(gates, "paper_enabled", False))
    pm_on = bool(getattr(gates, "pm_enabled", False))
    live_on = bool(getattr(gates, "live_effective", False))
    switch_on = bool(getattr(gates, "kill_switch_present", False))
    _gv2_kv([
        ("模拟盘", "开" if paper_on else "关", "up" if paper_on else "mute"),
        ("仓位管理", "开" if pm_on else "关", "up" if pm_on else "mute"),
        ("实盘", "开" if live_on else "关", "down" if live_on else "mute"),
        ("杀开关", "存在" if switch_on else "不存在", "mute"),
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


def _gv2_prior_pack() -> tuple[list[tuple[str, str]], list[str]]:
    payload = _read_prior_baseline()
    loaded = _prior_from_risk()
    if loaded is not None:
        values = loaded
    elif payload:
        values = _prior_from_baseline(payload)
    else:
        # Same three literals risk._priors already paints when the pack is absent.
        values = [
            ("见顶 P50", "1.0 min"),
            ("4H 存活", "75.3%"),
            ("当天死", "68.67%"),
            ("毕业率", "—"),
        ]
    return values, _prior_foot(payload)


def _prior_foot(payload: dict[str, object]) -> list[str]:
    meta = _mapping(payload.get("meta"))
    gmgn = _mapping(payload.get("gmgn"))
    self_db = _mapping(payload.get("self_db"))
    gmgn_n = meta.get("gmgn_n", gmgn.get("n"))
    self_n = self_db.get("n")
    gmgn_text = f"GMGN n={gmgn_n}" if isinstance(gmgn_n, int) and not isinstance(gmgn_n, bool) else "GMGN"
    self_text = (
        f"self_survival n={self_n}" if isinstance(self_n, int) and not isinstance(self_n, bool) else "self_survival"
    )
    return [gmgn_text, self_text, "CoinGecko", "RED-PUMP"]


def _gv2_pulse(values: list[tuple[str, str]]) -> None:
    with ui.element("div").classes("gv2-pulse"):
        for label_, value in values:
            with ui.element("div").classes("gv2-pulse-item"):
                ui.label(label_).classes("lbl")
                ui.label(value).classes("val")


def _gv2_table(headers: list[tuple[str, bool]], rows: list[list[str]]) -> None:
    with ui.element("table").classes("gv2-table"):
        with ui.element("tr"):
            for text, numeric in headers:
                with ui.element("th").classes("num" if numeric else ""):
                    ui.label(text)
        for row in rows:
            with ui.element("tr"):
                for index, text in enumerate(row):
                    numeric = headers[index][1] if index < len(headers) else False
                    with ui.element("td").classes("num" if numeric else ""):
                        ui.label(text)


def _gv2_today(store: DataStore) -> None:
    result = store.today_execution_stats()
    if not result.available or result.data is None:
        _gv2_table(
            [("信号数", True), ("校验通过", True), ("已执行", True)],
            [["—", "—", "—"]],
        )
        return
    block = result.data
    _gv2_table(
        [("信号数", True), ("校验通过", True), ("已执行", True)],
        [[
            _count_text(getattr(block, "n", None)),
            _count_text(getattr(block, "n_ok", None)),
            _count_text(getattr(block, "n_executed", None)),
        ]],
    )


def _gv2_week(store: DataStore) -> None:
    result = store.week_decision_stats()
    headers = [("决策数", True), ("有执行", True), ("通过率", True), ("执行率", True)]
    if not result.available or result.data is None:
        _gv2_table(headers, [["—", "—", "—", "—"]])
        return
    block = result.data
    _gv2_table(
        headers,
        [[
            _count_text(getattr(block, "n_decisions", None)),
            _count_text(getattr(block, "n_with_exec", None)),
            _plain_rate(getattr(block, "ok_rate", None)),
            _plain_rate(getattr(block, "executed_rate", None)),
        ]],
    )


def _gv2_jev(store: DataStore, foot: list[str]) -> None:
    result = store.jev_threshold()
    block = result.data if result.available else None
    n = getattr(block, "n", None)
    unique = getattr(block, "unique_token", None)
    up_1h = getattr(block, "up_1h", None)
    up_4h = getattr(block, "up_4h", None)
    _gv2_kv([
        ("n", _count_text(n), ""),
        ("unique_token", _count_text(unique), ""),
        ("1h 上涨率", _plain_rate(up_1h), _jev_tone(up_1h)),
        ("4h 上涨率", _plain_rate(up_4h), _jev_tone(up_4h)),
    ])
    gap = _jev_gap(n, unique)
    foot[:] = ["门槛 n≥15 / unique≥10", gap]


def _jev_gap(n: object, unique: object) -> str:
    if not isinstance(n, int) or not isinstance(unique, int) or isinstance(n, bool) or isinstance(unique, bool):
        return "过滤后样本未读到"
    short_n = 15 - n
    short_unique = 10 - unique
    if short_n <= 0 and short_unique <= 0:
        return "已达门槛"
    if short_n == short_unique and short_n > 0:
        return f"各差 {short_n}"
    parts: list[str] = []
    if short_n > 0:
        parts.append(f"n 差 {short_n}")
    if short_unique > 0:
        parts.append(f"unique 差 {short_unique}")
    return " · ".join(parts)


def _jev_tone(rate: object) -> str:
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        return "mute"
    if rate >= 0.55:
        return "up"
    if rate < 0.50:
        return "down"
    return ""


def _gv2_hot(store: DataStore) -> None:
    headers = [("指标", False), ("n_1h", True), ("中位 1h", True), ("中位 4h", True), ("均值 1h(w)", True)]
    result = store.hot_baseline()
    block = result.data if result.available else None
    if block is None:
        _gv2_table(headers, [["hot 全量", "—", "—", "—", "—"]])
        ui.label("hot 截面未读到").classes("gv2-note")
        return
    _gv2_table(
        headers,
        [[
            "hot 全量",
            _count_text(getattr(block, "n_1h", None)),
            _signed_pct(getattr(block, "median_1h", None)),
            _signed_pct(getattr(block, "median_4h", None)),
            _signed_pct(getattr(block, "mean_1h_winsor", None)),
        ]],
    )


def _gv2_strategies(store: DataStore) -> None:
    headers = [("strategy", False), ("n", True), ("win", True)]
    result = store.strategy_buckets()
    rows = _result_rows(result) if result.available else []
    jev = store.jev_threshold()
    jev_block = jev.data if jev.available else None
    strategy_id = str(getattr(jev_block, "strategy_id", "") or "")
    raw_win = getattr(jev_block, "raw_up_1h", None)
    painted: list[list[str]] = []
    for item in rows:
        name = _gv2_text(getattr(item, "strategy_id", None))
        win = "—"
        if strategy_id and name == strategy_id:
            win = _plain_rate(raw_win)
        painted.append([name, _count_text(getattr(item, "n", None)), win])
    if not painted:
        painted = [["—", "—", "—"]]
    _gv2_table(headers, painted)
    ui.label("B/C 策略出信号后自动分桶").classes("gv2-note")


def _count_text(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        return "—"
    return f"{value:,}"


def _plain_rate(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return f"{float(value) * 100:.1f}%"


def _signed_pct(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return f"{float(value):+.2f}%"


def _rss_label() -> str:
    try:
        kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:
        return "内存 —"
    if not isinstance(kib, (int, float)) or isinstance(kib, bool) or kib < 0:
        return "内存 —"
    return f"内存 {kib / 1024:.0f} MB"


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
