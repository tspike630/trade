"""NiceGUI entrypoint.

Start from the project root (the directory that contains ``dashboard/`` and the
SQLite files)::

    python -m dashboard.app

Binds 127.0.0.1 only. Default port is 8081 (``DASHBOARD_PORT``).
"""

from __future__ import annotations

import csv
import json
import logging
import os
import secrets
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Request
from nicegui import app, ui

from dashboard.auth import attach_secret_filters, verify_api_key
from dashboard.data import GOAL_DEADLINE_DAY, GOAL_START_DAY, DataStore
from dashboard.format_util import fmt_age, fmt_rate, liveness_label
from dashboard.panels import risk as risk_panel
from dashboard.panels.common import panel_box, panel_guard, show_unavailable
from dashboard.panels.evolution import render_evolution_body
from dashboard.panels.markets import render_markets
from dashboard.panels.registry import PANELS
from dashboard.panels.shell import (
    TOP_TABS,
    HostName as HostKey,
    db_status_line,
    heartbeat_summary,
    host_visible,
    panel_group,
    select_top_tab,
    session_clock,
)
from dashboard.settings import allow_insecure, api_secret, bind_host, db_root, port, read_gates, refresh_seconds
from dashboard.c_exit_shadow import (
    C_EXIT_DISCIPLINE,
    C_EXIT_GRID,
    C_EXIT_NAIL,
    C_EXIT_ROLLBACK,
    C_EXIT_THRESHOLD,
    load_c_exit_shadow,
    main_line as c_exit_main_line,
    rollback_hit_line,
    tag_line as c_exit_tag_line,
    verdict_line as c_exit_verdict_line,
)
from dashboard.entry_c_exit_joint import (
    JOINT_DISCIPLINE,
    JOINT_LIQ_LIVE,
    JOINT_LIQ_NAIL,
    JOINT_NAIL,
    JOINT_PNL_NOTE,
    JOINT_SHARE,
    LIQ_SPLIT_HEADERS,
    TABLE_HEADERS,
    liq_risk_line as joint_liq_risk_line,
    liq_split_rows as joint_liq_split_rows,
    load_entry_c_exit_joint,
    nail_line as joint_nail_line,
    nine_line as joint_nine_line,
    peak_line as joint_peak_line,
    table_rows as joint_table_rows,
    verdict_line as joint_verdict_line,
)
from dashboard.phase1_counterfactual import (
    PHASE1_CANDIDATE,
    PHASE1_CF_NAIL,
    PHASE1_WINDOW,
    baseline_line,
    case_line,
    honest_line,
    load_phase1_counterfactual,
)
from dashboard.shadow_phase0 import (
    FLAG_LIQ,
    FLAG_R3,
    PHASE0_NAIL,
    format_both,
    format_count,
    format_sl,
    format_tp,
    load_shadow_phase0,
)

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
/* ============================================================
   V2 FULL ALIGN —— token 与 dashboard_grid_skeleton_v2.html 逐项一致
   ============================================================ */
:root {
  --v2-bg: #0d1117; --v2-panel: #161b22; --v2-header: #1f2630;
  --v2-border: #2d333b; --v2-border-strong: #3d444d;
  --v2-fg: #e6edf3; --v2-dim: #8b949e; --v2-mute: #565d66;
  --v2-up: #3fb950; --v2-down: #f85149; --v2-warn: #d29922; --v2-accent: #2f81f7;
  --v2-mono: "SF Mono", "Cascadia Code", "JetBrains Mono", Consolas, "Segoe UI", Roboto, sans-serif;
  --v2-base: 12px; --v2-num: 13px; --v2-title: 11px; --v2-lg: 22px;
  --v2-gap: 8px; --v2-pad: 8px 10px; --v2-lh: 1.5;
}

/* ---- 顶栏（40px） ---- */
.v2-topbar {
  height: 40px; display: flex; align-items: center; gap: 0;
  background: var(--v2-panel); border-bottom: 1px solid var(--v2-border);
  padding: 0 4px; flex-shrink: 0; font-family: var(--v2-mono); font-size: var(--v2-base);
  box-sizing: border-box;
}
.v2-brand { padding: 0 14px; font-weight: 700; font-size: 13px; letter-spacing: .5px; color: var(--v2-fg); white-space: nowrap; }
.v2-brand .sub { color: var(--v2-mute); font-weight: 400; font-size: 10px; margin-left: 6px; }
.v2-tab {
  height: 100%; padding: 0 8px; display: flex; align-items: center; gap: 6px;
  color: var(--v2-dim); font-size: 11px; letter-spacing: .5px; text-transform: uppercase;
  border-bottom: 2px solid transparent; cursor: pointer; white-space: nowrap;
  background: transparent; border-top: none; border-left: none; border-right: none;
  font-family: var(--v2-mono);
}
.v2-tab:hover { color: var(--v2-fg); background: rgba(255,255,255,.03); }
.v2-tab.active { color: var(--v2-fg); border-bottom-color: var(--v2-accent); }
.v2-tabs {
  display: flex; align-items: stretch; height: 100%; min-width: 0; flex: 1 1 auto;
  overflow-x: auto; overflow-y: hidden; scrollbar-width: thin;
  scrollbar-color: var(--v2-border-strong) transparent;
}
.v2-tabs::-webkit-scrollbar { height: 3px; }
.v2-tabs::-webkit-scrollbar-thumb { background: var(--v2-border-strong); }
.v2-tab { flex: 0 0 auto; }
.v2-tab.placeholder {
  color: var(--v2-mute); cursor: default; user-select: none;
  border-bottom-color: transparent;
}
.v2-tab.placeholder:hover,
.v2-tab.placeholder:hover > * { color: var(--v2-mute); background: transparent; }
.v2-tab.placeholder > * {
  margin: 0; padding: 0; color: inherit; font: inherit; letter-spacing: inherit;
  text-transform: inherit; line-height: 1; white-space: nowrap;
}
.v2-topright { margin-left: auto; display: flex; align-items: center; gap: 8px; padding: 0 10px; flex: 0 0 auto; }
.v2-clock { font-size: 11px; color: var(--v2-dim); font-variant-numeric: tabular-nums; white-space: nowrap; }
.v2-statusdot { width: 7px; height: 7px; border-radius: 50%; background: var(--v2-up); box-shadow: 0 0 4px var(--v2-up); display: inline-block; }
.v2-realtime { font-size: 11px; color: var(--v2-dim); }

/* ---- 子标签行（30px） ---- */
.v2-subbar {
  height: 30px; display: flex; align-items: center; gap: 2px;
  background: var(--v2-bg); border-bottom: 1px solid var(--v2-border);
  padding: 0 8px; flex-shrink: 0; font-family: var(--v2-mono); font-size: var(--v2-base);
  box-sizing: border-box;
}
.v2-subtab {
  padding: 5px 12px; color: var(--v2-dim); font-size: 11px; border-radius: 4px 4px 0 0;
  cursor: pointer; border: 1px solid transparent; border-bottom: none;
  background: transparent; font-family: var(--v2-mono);
}
.v2-subtab.active { color: var(--v2-fg); background: var(--v2-panel); border-color: var(--v2-border); }
.v2-subbar .spacer { flex: 1; }

/* ---- 通用工具按钮 ---- */
.v2-tool {
  background: transparent; border: 1px solid var(--v2-border-strong); color: var(--v2-dim);
  font-size: 10px; padding: 2px 8px; border-radius: 3px; cursor: pointer; line-height: 1.4;
  font-family: var(--v2-mono);
}
.v2-tool:hover { color: var(--v2-fg); border-color: var(--v2-accent); }
.v2-tool.on { background: var(--v2-accent); color: #fff; border-color: var(--v2-accent); }

/* ---- 组件网格（12 列） ---- */
.v2-grid {
  display: grid; grid-template-columns: repeat(12, 1fr); gap: var(--v2-gap);
  grid-auto-rows: minmax(84px, auto); align-items: stretch; width: 100%;
  box-sizing: border-box; font-family: var(--v2-mono); font-size: var(--v2-base);
  line-height: var(--v2-lh); color: var(--v2-fg);
}
.v2-wrap { flex: 1; overflow-y: auto; padding: var(--v2-gap); width: 100%; align-self: stretch; }

/* ---- 组件卡（#161b22 + #2d333b 边框 + 表头 #1f2630） ---- */
.v2-comp {
  background: var(--v2-panel); border: 1px solid var(--v2-border); border-radius: 3px;
  display: flex; flex-direction: column; min-width: 0; min-height: 84px; box-sizing: border-box; overflow: hidden;
  font-family: var(--v2-mono);
}
.v2-comp.span-3 { grid-column: span 3; }
.v2-comp.span-4 { grid-column: span 4; }
.v2-comp.span-6 { grid-column: span 6; }
.v2-comp.span-8 { grid-column: span 8; }
.v2-comp.span-12 { grid-column: span 12; }
.v2-head {
  display: flex; align-items: center; gap: 8px; padding: 5px 10px;
  background: var(--v2-header); border-bottom: 1px solid var(--v2-border); flex-shrink: 0;
}
.v2-title { font-size: var(--v2-title); letter-spacing: 1px; text-transform: uppercase; color: var(--v2-dim); font-family: var(--v2-mono); }
.v2-title b { color: var(--v2-fg); font-weight: 600; }
.v2-tools { margin-left: auto; display: flex; gap: 4px; align-items: center; }
.v2-body { padding: var(--v2-pad); flex: 1; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.v2-foot {
  padding: 4px 10px; border-top: 1px solid var(--v2-border); font-size: 10px;
  color: var(--v2-mute); display: flex; gap: 10px; flex-shrink: 0; font-family: var(--v2-mono);
  white-space: normal; overflow: hidden;
}
.v2-foot > * { min-width: 0; white-space: normal; }

/* ---- kv 行 ---- */
.v2-kv { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; align-items: baseline; }
.v2-kv .k { color: var(--v2-dim); font-size: 11px; white-space: nowrap; font-family: var(--v2-mono); }
.v2-kv .v { font-size: var(--v2-num); color: var(--v2-fg); font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; font-family: var(--v2-mono); }
.v2-kv .v.up { color: var(--v2-up); }
.v2-kv .v.down { color: var(--v2-down); }
.v2-kv .v.mute { color: var(--v2-mute); }

/* ---- 组件内表格（模板同款，无分页） ---- */
.v2-table { width: 100%; border-collapse: collapse; font-size: 11px; font-family: var(--v2-mono); }
.v2-table th { text-align: left; color: var(--v2-mute); font-weight: 500; padding: 3px 6px; border-bottom: 1px solid var(--v2-border); white-space: nowrap; }
.v2-table th.num, .v2-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
.v2-table td { padding: 3px 6px; border-bottom: 1px solid rgba(45,51,59,.5); white-space: nowrap; }
.v2-table td.up, .v2-table .v.up { color: var(--v2-up); }
.v2-table td.down, .v2-table .v.down { color: var(--v2-down); }
.v2-table td.mute, .v2-table .v.mute { color: var(--v2-mute); }
.v2-table tr:hover td { background: rgba(47,129,247,.06); }

/* ---- 脉冲块 ---- */
.v2-pulse { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; }
.v2-pulse-item { border: 1px solid var(--v2-border); border-radius: 3px; padding: 4px 8px; }
.v2-pulse-item .lbl { font-size: 10px; color: var(--v2-mute); font-family: var(--v2-mono); }
.v2-pulse-item .val { font-size: 14px; font-variant-numeric: tabular-nums; margin-top: 1px; font-family: var(--v2-mono); }
.v2-pulse-item .val.up { color: var(--v2-up); }
.v2-pulse-item .val.down { color: var(--v2-down); }
.v2-pulse-item .val.mute { color: var(--v2-mute); }

/* ---- KPI 顶行（4 卡） ---- */
.v2-kpi {
  background: var(--v2-panel); border: 1px solid var(--v2-border); border-radius: 3px;
  padding: 8px 10px; min-height: 70px; grid-column: span 3; min-width: 0; box-sizing: border-box;
  display: flex; flex-direction: column; justify-content: center; gap: 2px; font-family: var(--v2-mono);
}
.v2-kpi .lbl { color: var(--v2-dim); font-size: 9px; letter-spacing: .1em; text-transform: uppercase; }
.v2-kpi .val { font-family: var(--v2-mono); font-size: var(--v2-lg); font-weight: 650; font-variant-numeric: tabular-nums; line-height: 1.15; }
.v2-kpi .hint { color: var(--v2-mute); font-size: 9px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.v2-kpi.tone-warn .val { color: var(--v2-warn); }
.v2-kpi.tone-bad .val { color: var(--v2-down); }
.v2-kpi.tone-ok .val { color: var(--v2-up); }

/* ---- 底栏状态条（26px） ---- */
.v2-statusbar {
  height: 26px; display: flex; align-items: center; gap: 14px;
  background: var(--v2-panel); border-top: 1px solid var(--v2-border);
  padding: 0 12px; font-size: 10px; color: var(--v2-dim); flex-shrink: 0;
  font-family: var(--v2-mono); white-space: nowrap; overflow: hidden; box-sizing: border-box;
}
.v2-statusbar .sep { color: var(--v2-border-strong); }
.v2-statusbar .ok { color: var(--v2-up); }
.v2-statusbar .warn { color: var(--v2-warn); }
.v2-statusbar .right { margin-left: auto; }

/* ---- 滚动条（暗色） ---- */
.v2-wrap::-webkit-scrollbar { width: 8px; height: 8px; }
.v2-wrap::-webkit-scrollbar-track { background: var(--v2-bg); }
.v2-wrap::-webkit-scrollbar-thumb { background: var(--v2-border); border-radius: 4px; }
.v2-wrap::-webkit-scrollbar-thumb:hover { background: var(--v2-border-strong); }

.v2-note { color: var(--v2-mute); font-size: 10px; font-style: italic; }
.v2-note-warn { color: var(--v2-warn); font-style: normal; font-weight: 650; }

@media (max-width: 720px) {
  .v2-grid { grid-template-columns: repeat(2, 1fr); }
  .v2-comp, .v2-kpi { grid-column: span 2 !important; }
}

/* NiceGUI button chrome reset so v2-tab / v2-subtab / v2-tool match the skeleton. */
html, body, .shell { background: var(--v2-bg); color: var(--v2-fg); }
.nicegui-content {
  padding: 0 !important; gap: 0 !important; align-items: stretch !important;
  height: 100vh; overflow: hidden;
}
.shell {
  height: 100vh; max-height: 100vh; width: 100%; overflow: hidden;
  display: flex !important; flex-direction: column !important; align-items: stretch !important;
  gap: 0 !important;
}
.ft-body.v2-stage {
  max-width: none; margin: 0; padding: 0; min-height: 0;
  display: flex; flex-direction: column; overflow: hidden;
}
.v2-subslot { flex: 0 0 auto; width: 100%; align-self: stretch; display: block; }
.v2-subslot > * { display: block; width: 100%; }
.v2-mainslot { flex: 1; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
.v2-mainslot > * {
  flex: 1; min-height: 0; display: flex !important; flex-direction: column; overflow: hidden;
}
.v2-host { flex: 1; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
.v2-host.v2-off { display: none !important; }
.v2-wrap { min-height: 0; }
.v2-brand, .v2-title { display: flex; align-items: baseline; }
.v2-title { gap: 4px; }
.v2-title .em { color: var(--v2-fg); font-weight: 600; }
.v2-topbar { height: 40px !important; min-height: 40px !important; max-height: 40px !important; overflow: hidden; }
.v2-subbar {
  height: 30px !important; min-height: 30px !important; max-height: 30px !important;
  width: 100%; display: flex !important; flex-direction: row !important; flex-wrap: nowrap !important;
  align-items: center !important; overflow: hidden; position: relative; z-index: 4;
}
.v2-warnbar {
  display: flex; align-items: center; justify-content: center; height: 20px;
  min-height: 20px; max-height: 20px; flex: 0 0 20px; width: 100%;
  background: #3a2d10; color: #d29922; font-size: 10px; font-family: var(--v2-mono);
  margin: 0; padding: 0; border: none; border-radius: 0; overflow: hidden; white-space: nowrap;
}
.v2-warnbar > * { margin: 0; padding: 0; font-size: 10px; line-height: 20px; color: inherit; font-family: inherit; }
.v2-ready { margin-left: auto; display: flex; align-items: center; gap: 6px; flex: 0 0 auto; }
.v2-statusdot.warn { background: var(--v2-warn); box-shadow: 0 0 4px var(--v2-warn); }
.v2-statusdot.mute { background: var(--v2-mute); box-shadow: none; }
.v2-statusbar { height: 26px !important; min-height: 26px !important; max-height: 26px !important; }
.v2-tab.q-btn, .v2-subtab.q-btn, .v2-tool.q-btn {
  box-shadow: none !important; text-transform: none !important; min-height: 0 !important;
  font-family: var(--v2-mono) !important; font-weight: 400 !important;
}
.v2-tab.q-btn, .v2-tab.q-btn .q-btn__content {
  height: 40px !important; padding: 0 8px !important; border-radius: 0 !important;
  background: transparent !important; color: var(--v2-dim) !important;
  font-size: 11px !important; letter-spacing: .5px !important;
  border: none !important; border-bottom: 2px solid transparent !important;
}
.v2-tab.q-btn:hover, .v2-tab.q-btn:hover .q-btn__content {
  color: var(--v2-fg) !important; background: rgba(255,255,255,.03) !important;
}
.v2-tab.q-btn.active, .v2-tab.q-btn.active .q-btn__content {
  color: var(--v2-fg) !important; border-bottom-color: var(--v2-accent) !important;
}
.v2-tab.placeholder, .v2-tab.placeholder > * {
  height: 40px; box-sizing: border-box; display: flex; align-items: center;
  color: var(--v2-mute) !important; background: transparent !important;
  cursor: default !important; border-bottom: 2px solid transparent !important;
  font-family: var(--v2-mono) !important; font-size: 11px !important;
  letter-spacing: .5px !important; text-transform: uppercase;
  padding: 0 8px; width: auto !important; flex: 0 0 auto !important;
}
@media (max-width: 1440px) {
  .v2-tab, .v2-tab.q-btn, .v2-tab.q-btn .q-btn__content, .v2-tab.placeholder {
    padding-left: 6px !important; padding-right: 6px !important;
    letter-spacing: .3px !important;
  }
}
.v2-tab.placeholder > * { height: auto; padding: 0 !important; border: none !important; }
.v2-subtab.q-btn, .v2-subtab.q-btn .q-btn__content {
  height: 22px !important; min-height: 0 !important; max-height: 22px !important;
  padding: 0 12px !important;
  background: transparent !important; color: var(--v2-dim) !important;
  border: 1px solid transparent !important; border-bottom: none !important;
  border-radius: 4px 4px 0 0 !important; font-size: 11px !important; letter-spacing: 0 !important;
  line-height: 22px !important;
}
.v2-subtab.q-btn.active, .v2-subtab.q-btn.active .q-btn__content {
  color: var(--v2-fg) !important; background: var(--v2-panel) !important;
  border-color: var(--v2-border) !important;
}
.v2-tool.q-btn, .v2-head .v2-tool, .v2-subbar .v2-tool.q-btn {
  display: inline-flex; align-items: center;
  height: auto !important; padding: 2px 8px !important;
  background: transparent !important; color: var(--v2-dim) !important;
  border: 1px solid var(--v2-border-strong) !important; border-radius: 3px !important;
  font-size: 10px !important; line-height: 1.4 !important;
  flex: 0 0 auto !important; width: auto !important; white-space: nowrap !important;
}
.v2-subbar .v2-subtab.q-btn, .v2-subbar .v2-tool.q-btn {
  flex: 0 0 auto !important; width: auto !important; white-space: nowrap !important;
}
.v2-tool.q-btn .q-btn__content { color: inherit !important; padding: 0 !important; min-height: 0 !important; }
.v2-tool.q-btn.on, .v2-tool.q-btn.on .q-btn__content, .v2-tool.on {
  background: var(--v2-accent) !important; color: #fff !important; border-color: var(--v2-accent) !important;
}
.v2-table th > *, .v2-table td > *, .v2-brand > *, .v2-title > *, .v2-foot > *, .v2-kpi > *, .v2-pulse-item > * {
  margin: 0; font: inherit; color: inherit; line-height: inherit;
}
.v2-news {
  font-size: 11px; color: var(--v2-dim);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.footer-note { color: var(--v2-mute); }
.banner { margin: 8px 8px 0; }
.v2-body { overflow-x: auto; }
.v2-grid.v2-compact .v2-comp, .v2-grid.v2-compact .v2-kpi { grid-column: span 6 !important; }
@media (max-width: 720px) {
  .v2-grid.v2-compact .v2-comp, .v2-grid.v2-compact .v2-kpi { grid-column: span 2 !important; }
  .v2-topbar { overflow-x: auto; }
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
    attach_secret_filters()
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
    opened_at = time.monotonic()
    nav: dict[str, object] = {
        "tab": "AI CHAT" if section == "chat" else "DASHBOARD",
        "sub": "OVERVIEW",
        "compact": False,
    }
    host_keys: tuple[HostKey, ...] = ("OVERVIEW", "SIGNALS", "EXECUTION", "MARKETS", "AI CHAT")

    def host_on(key: HostKey) -> bool:
        return host_visible(str(nav["tab"]), str(nav["sub"]), key)

    def on_chain(event: object) -> None:
        value = getattr(event, "value", None) or "all"
        text = str(value)
        if text == chain_filter["value"]:
            return
        chain_filter["value"] = text
        markets_tab.refresh()

    @ui.refreshable
    def overview_tab() -> None:
        grid = "v2-grid v2-compact" if nav["compact"] else "v2-grid"
        with ui.element("div").classes("v2-wrap"):
            with ui.element("div").classes(grid):
                try:
                    _v2_kpi_strip(store)
                except Exception:
                    log.warning("overview kpi strip failed")
                    ui.label("—").classes("v2-note")
                _v2_comp(
                    ("进程", "心跳"),
                    "span-3",
                    lambda: _v2_procs(store),
                    foot=_proc_foot(store),
                    tools=("⟳",),
                    on_tool=lambda _name: tick(),
                )
                _v2_comp(
                    ("风险", "闸门"),
                    "span-3",
                    _v2_gates,
                    foot="闸门只读 · 不调权",
                    tools=("只读",),
                )
                _v2_comp(
                    ("先验", "对照"),
                    "span-6",
                    _v2_priors,
                    foot="GMGN n=956 · self n=9317 · CoinGecko · RED-PUMP",
                    tools=("观察中·不调权",),
                )
                _v2_comp(
                    ("今日", "EXECUTION_LOG"),
                    "span-4",
                    lambda: _v2_today(store),
                    foot="execution_log 当日行",
                    tools=("UTC 日",),
                )
                _v2_comp(
                    ("近 7 日", "决策"),
                    "span-4",
                    lambda: _v2_week(store),
                    foot="decision_log 滚动 168h",
                    tools=("168h",),
                )
                _v2_comp(
                    ("JEV", "门槛"),
                    "span-4",
                    _v2_jev_gate,
                    foot="门槛 n≥15 / unique≥10",
                    tools=("过滤后",),
                )
                _v2_comp(
                    ("市场", "基准（hot）"),
                    "span-8",
                    lambda: _v2_market_base(store),
                    foot="indicator_history tier=hot · winsor |x|<=500",
                    tools=("winsor",),
                )
                _v2_comp(
                    ("策略", "速览"),
                    "span-4",
                    lambda: _v2_strategy(store),
                    foot="decision_log 按 strategy_id",
                    tools=("⟳",),
                    on_tool=lambda _name: tick(),
                )
                _v2_comp(
                    ("持仓", "总览"),
                    "span-6",
                    lambda: _v2_positions(store),
                    foot="模拟盘当前持仓 · 现价取 pair_history",
                )
                _v2_comp(
                    ("拒因", "分桶"),
                    "span-6",
                    lambda: _v2_reject(store),
                    foot="execution_log · pm/risk 细分（含 daily_loss）",
                    tools=("7d",),
                )
                _v2_comp(
                    ("平仓", "旁证"),
                    "span-6",
                    lambda: _v2_closed(store),
                    foot=f"首笔命中 + exit×liq · {_EVIDENCE_NAIL}",
                    tools=("14d",),
                )
                _v2_comp(
                    ("交易", "频率"),
                    "span-6",
                    lambda: _v2_trade_freq(store),
                    foot=f"G-1 多样本可取 · 日开↑常伴损耗↑ · 观察峰≠加仓频 · {_EVIDENCE_NAIL}",
                    tools=("14d",),
                )
                _v2_comp(
                    ("信号", "连亏"),
                    "span-6",
                    lambda: _v2_signal_streak(store),
                    foot=f"F-1 只记录不熔断 · P(第3亏|连2亏) · {_EVIDENCE_NAIL}",
                    tools=("14d",),
                )
                _v2_comp(
                    ("退出", "持仓"),
                    "span-6",
                    lambda: _v2_exit_hold(store),
                    foot=f"F-3 / C3：exit×持仓分钟 vs max_hold · {_EVIDENCE_NAIL}",
                    tools=("14d",),
                )
                _v2_comp(
                    ("持仓", "集中"),
                    "span-6",
                    lambda: _v2_intel_holders(store),
                    foot=f"{_EVIDENCE_NAIL} · 不进盘中参 · 高 bot 不跳单",
                    tools=("权重0",),
                )
                _v2_comp(
                    ("滑点", "bot"),
                    "span-6",
                    lambda: _v2_intel_slippage(store),
                    foot=f"{_EVIDENCE_NAIL} · 不进盘中参 · 高 bot 不跳单",
                    tools=("不跳单",),
                )
                _v2_comp(
                    ("代币", "年龄"),
                    "span-6",
                    lambda: _v2_token_age(store),
                    foot=f"{_EVIDENCE_NAIL} · 不进盘中参 · extras.pairCreatedAt",
                    tools=("只读",),
                )
                _mount_goal_20k(store)
                _mount_phase0_shadow()
                _mount_phase1_counterfactual()
                _mount_c_exit_shadow()
                _mount_entry_c_exit_joint()
                _mount_m7_shadow(store)
                _v2_comp(
                    ("回测", "新闻"),
                    "span-6",
                    lambda: _v2_btnews(store),
                    foot="latest_backtest · news_events",
                )
                _v2_comp(
                    ("进化", "建议"),
                    "span-12",
                    render_evolution_body,
                    foot="周报 §10 · 只观察 · 不调权",
                    tools=("只读",),
                )
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
        session_lbl.set_text(session_clock(int(time.monotonic() - opened_at)))

    def paint_status() -> None:
        try:
            db_text = db_status_line(store.file_status())
            db_lbl.set_text(db_text)
            _set_tone(db_lbl, "ok" if "缺失" not in db_text else "warn")
            live = store.process_liveness()
            if not live.available:
                text, tone = "心跳：不可用", "muted"
            else:
                text, tone = heartbeat_summary([item.state for item in live.data])
            proc_lbl.set_text(text)
            _set_tone(proc_lbl, "ok" if tone == "ok" else ("warn" if tone == "warn" else ""))
            if tone == "ok" and "缺失" not in db_text:
                _paint_ready(ready_lbl, ready_dot, "就绪", "ok")
            elif tone == "warn":
                _paint_ready(ready_lbl, ready_dot, "停滞", "warn")
            else:
                _paint_ready(ready_lbl, ready_dot, "未知", "mute")
        except Exception:
            log.warning("status strip failed")
            proc_lbl.set_text("心跳：不可用")
            _paint_ready(ready_lbl, ready_dot, "未知", "mute")

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
            paint_clock()
        except Exception:
            log.warning("refresh failed")
            _paint_ready(ready_lbl, ready_dot, "刷新失败", "warn")

    def apply_nav() -> None:
        hosts = nav.get("hosts")
        if isinstance(hosts, dict):
            for key in host_keys:
                host = hosts.get(key)
                if host is None:
                    continue
                if host_on(key):
                    host.classes(remove="v2-off")
                else:
                    host.classes(add="v2-off")
        refresh_tabs = nav.get("refresh_tabs")
        refresh_sub = nav.get("refresh_sub")
        if callable(refresh_tabs):
            refresh_tabs()
        if callable(refresh_sub):
            refresh_sub()

    def switch_tab(key: str) -> None:
        nxt = select_top_tab(str(nav["tab"]), key)
        if nxt == nav["tab"]:
            return
        nav["tab"] = nxt
        apply_nav()

    def switch_sub(key: str) -> None:
        if key not in ("OVERVIEW", "SIGNALS", "EXECUTION"):
            return
        if nav["tab"] == "DASHBOARD" and nav["sub"] == key:
            return
        nav["tab"] = "DASHBOARD"
        nav["sub"] = key
        apply_nav()

    def toggle_compact() -> None:
        nav["compact"] = not bool(nav["compact"])
        overview_tab.refresh()
        refresh_sub = nav.get("refresh_sub")
        if callable(refresh_sub):
            refresh_sub()

    @ui.refreshable
    def tab_bar() -> None:
        with ui.element("div").classes("v2-tabs").props("role=tablist"):
            for spec in TOP_TABS:
                if spec.kind == "placeholder":
                    ui.label(spec.label).classes("v2-tab placeholder").props(
                        'role=tab aria-disabled=true aria-selected=false tabindex=-1 title="未接线"'
                    )
                    continue
                active = nav["tab"] == spec.label
                ui.button(spec.label, on_click=lambda k=spec.label: switch_tab(k)).props(
                    "flat dense unelevated no-caps role=tab aria-selected="
                    + ("true" if active else "false")
                ).classes(f"v2-tab{' active' if active else ''}")

    nav["refresh_tabs"] = tab_bar.refresh

    with ui.element("div").classes("shell"):
        with ui.element("div").classes("v2-topbar"):
            with ui.element("div").classes("v2-brand"):
                ui.label("SPIKE'S CRYPTO TERMINER")
                ui.label("链上新币投研终端").classes("sub")
            tab_bar()
            with ui.element("div").classes("v2-topright"):
                clock_lbl = ui.label(_clock_text()).classes("v2-clock")
                ui.element("div").classes("v2-statusdot")
                ui.label("实时").classes("v2-realtime")
                if api_secret():
                    ui.button("退出", on_click=_revoke_session).props(
                        "flat dense unelevated no-caps"
                    ).classes("v2-tool")
        if insecure:
            with ui.element("div").classes("v2-warnbar"):
                ui.label("本地未鉴权模式（ALLOW_INSECURE_API=1）。不要暴露到公网。")
        _module_tabs(
            section,
            overview_tab,
            signals_tab,
            execution_tab,
            markets_tab,
            chat_tab,
            nav=nav,
            on_sub=switch_sub,
            on_compact=toggle_compact,
            on_refresh=tick,
            host_on=host_on,
        )
        session_lbl, db_lbl, proc_lbl, ready_lbl, ready_dot = _status_strip(store)
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
    *,
    nav: dict[str, object],
    on_sub: Callable[[str], None],
    on_compact: Callable[[], None],
    on_refresh: Callable[[], None],
    host_on: Callable[[HostKey], bool],
) -> None:
    """Sub-tab row plus the five hosts. Top-level tabs stay in the top bar."""
    if section == "chat":
        nav["tab"] = "AI CHAT"

    @ui.refreshable
    def subbar() -> None:
        if nav["tab"] != "DASHBOARD":
            return
        with ui.element("div").classes("v2-subbar"):
            for name in ("OVERVIEW", "SIGNALS", "EXECUTION"):
                active = "active" if nav["sub"] == name else ""
                ui.button(name, on_click=lambda k=name: on_sub(k)).props(
                    "flat dense unelevated no-caps"
                ).classes(f"v2-subtab {active}".rstrip())
            ui.element("div").classes("spacer")
            compact = "v2-tool on" if nav["compact"] else "v2-tool"
            ui.button("紧凑", on_click=on_compact).props("flat dense unelevated no-caps").classes(compact)
            ui.button("刷新", on_click=on_refresh).props("flat dense unelevated no-caps").classes("v2-tool")

    nav["refresh_sub"] = subbar.refresh

    def panel_wrap(render: Callable[[], None]) -> None:
        with ui.element("div").classes("v2-wrap"):
            render()

    def chat_wrap() -> None:
        with ui.element("div").classes("v2-wrap"):
            chat_tab()
            ui.link("在 /chat 打开", "/chat").classes("news-link")

    panels: tuple[tuple[HostKey, Callable[[], None]], ...] = (
        ("OVERVIEW", overview_tab),
        ("SIGNALS", lambda: panel_wrap(signals_tab)),
        ("EXECUTION", lambda: panel_wrap(execution_tab)),
        ("MARKETS", lambda: panel_wrap(markets_tab)),
        ("AI CHAT", chat_wrap),
    )
    hosts: dict[str, ui.element] = {}
    with ui.element("div").classes("v2-subslot"):
        subbar()
    with ui.element("div").classes("v2-mainslot"):
        for key, render in panels:
            host = ui.element("div").classes("v2-host" if host_on(key) else "v2-host v2-off")
            hosts[key] = host
            with host:
                render()
    nav["hosts"] = hosts


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


def _set_tone(label: ui.label, tone: str) -> None:
    label.classes(remove="ok warn")
    if tone:
        label.classes(add=tone)


def _paint_ready(ready_lbl: ui.label, ready_dot: ui.element, text: str, tone: str) -> None:
    """Green status dot plus a word. The bullet character does not render in this font."""
    ready_lbl.set_text(text)
    ready_dot.classes(remove="ok warn mute")
    ready_dot.classes(add=tone if tone in ("ok", "warn", "mute") else "mute")


def _status_strip(store: DataStore) -> tuple[ui.label, ui.label, ui.label, ui.label, ui.element]:
    """Bottom status bar. Clock and heartbeat paints update the returned labels."""
    db_text = "库 —"
    try:
        db_text = db_status_line(store.file_status())
    except Exception:
        log.warning("status strip failed")
    with ui.element("div").classes("v2-statusbar"):
        ui.label("v0.1.0")
        ui.label("·").classes("sep")
        session_lbl = ui.label(session_clock(0))
        ui.label("·").classes("sep")
        db_lbl = ui.label(db_text)
        ui.label("·").classes("sep")
        proc_lbl = ui.label("进程 —")
        ui.label("·").classes("sep")
        ui.label("延迟 --ms")
        ready_dot = ui.element("div").classes("v2-statusdot ok right")
        ready_lbl = ui.label("就绪")
    return session_lbl, db_lbl, proc_lbl, ready_lbl, ready_dot


def _v2_kpi(label_: str, value: str, hint: str = "", tone: str = "") -> None:
    classes = "v2-kpi"
    if tone:
        classes = f"{classes} {tone}"
    with ui.element("div").classes(classes):
        ui.label(label_).classes("lbl")
        ui.label(value).classes("val")
        if hint:
            ui.label(hint).classes("hint")


def _v2_kpi_strip(store: DataStore) -> None:
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
    _v2_kpi(
        "模拟盘",
        "ON" if paper_on else "OFF",
        gates.origin_hint("paper"),
        "tone-warn" if paper_on else "tone-ok",
    )
    _v2_kpi(
        "仓位闸",
        "ON" if pm_on else "OFF",
        gates.origin_hint("pm"),
        "tone-warn" if pm_on else "tone-ok",
    )
    _v2_kpi("实盘", "ON" if live_on else "OFF", str(reason), "tone-bad" if live_on else "tone-ok")
    _v2_kpi("今日信号", count, "execution_log", "tone-ok" if today.available else "")


def _v2_title(plain: str, strong: str) -> None:
    with ui.element("div").classes("v2-title"):
        ui.label(plain)
        ui.label(strong).classes("em")


def _v2_comp(
    title: tuple[str, str],
    span: str,
    body_fn: Callable[[], None],
    foot: str = "",
    tools: tuple[str, ...] = (),
    on_tool: Callable[[str], None] | None = None,
) -> None:
    with ui.element("div").classes(f"v2-comp {span}"):
        with ui.element("div").classes("v2-head"):
            _v2_title(title[0], title[1])
            if tools:
                with ui.element("div").classes("v2-tools"):
                    for tool in tools:
                        if tool == "⟳" and on_tool is not None:
                            ui.button("⟳", on_click=lambda name=tool: on_tool(name)).props(
                                "flat dense unelevated no-caps"
                            ).classes("v2-tool")
                        else:
                            ui.label(tool).classes("v2-tool")
        with ui.element("div").classes("v2-body"):
            try:
                body_fn()
            except Exception:
                log.warning("overview component failed: %s %s", title[0], title[1])
                ui.label("—").classes("v2-note")
        if foot:
            with ui.element("div").classes("v2-foot"):
                ui.label(foot)


def _v2_kv(pairs: list[tuple[str, str, str]]) -> None:
    with ui.element("div").classes("v2-kv"):
        for key, value, tone in pairs:
            ui.label(key).classes("k")
            ui.label(value).classes(f"v {tone}".rstrip())


def _v2_table(
    headers: list[tuple[str, str]],
    rows: list[list[str]],
    tones: list[list[str]] | None = None,
) -> None:
    with ui.element("table").classes("v2-table"):
        with ui.element("tr"):
            for text, align in headers:
                with ui.element("th").classes(align):
                    ui.label(text)
        for row_index, row in enumerate(rows):
            with ui.element("tr"):
                for index, text in enumerate(row):
                    align = headers[index][1] if index < len(headers) else ""
                    tone = ""
                    if tones is not None and row_index < len(tones) and index < len(tones[row_index]):
                        tone = tones[row_index][index]
                    with ui.element("td").classes(f"{align} {tone}".strip()):
                        ui.label(text)


def _v2_text(value: object, limit: int | None = None) -> str:
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


def _v2_money(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return f"${value:,.0f}"


def _signed_usd(value: object) -> tuple[str, str]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—", "mute"
    if value > 0:
        return f"+${value:,.0f}", "up"
    if value < 0:
        return f"-${abs(value):,.0f}", "down"
    return "$0", "mute"


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


def _proc_foot(store: DataStore) -> str:
    try:
        live = store.process_liveness()
    except Exception:
        log.warning("process foot failed")
        return "心跳不可用"
    if not live.available:
        return "心跳不可用"
    probes = _result_rows(live)
    if not probes:
        return "心跳不可用"
    alive = sum(1 for probe in probes if getattr(probe, "state", "") == "alive")
    ages: list[float] = []
    for probe in probes:
        age = getattr(probe, "age_seconds", None)
        if isinstance(age, (int, float)) and not isinstance(age, bool):
            ages.append(float(age))
    if ages:
        return f"进程 {alive}/{len(probes)} 存活 · 上次心跳 {fmt_age(min(ages))}"
    return f"进程 {alive}/{len(probes)} 存活 · 上次心跳 —"


_PROCESS_ROWS = ("主爬虫", "指标", "JEV", "决策引擎", "执行网关")


def _v2_procs(store: DataStore) -> None:
    """Five heartbeat rows. A missing process is an em dash, not an error."""
    by_name: dict[str, tuple[str, str]] = {}
    try:
        live = store.process_liveness()
    except Exception:
        log.warning("process rows failed")
        live = None
    if live is not None and getattr(live, "available", False):
        for probe in _result_rows(live):
            name = str(getattr(probe, "name", "") or "")
            state = str(getattr(probe, "state", "") or "")
            if state == "alive":
                by_name[name] = (liveness_label("alive"), "up")
            elif state == "stale":
                by_name[name] = (liveness_label("stale"), "mute")
            else:
                by_name[name] = ("—", "mute")
    _v2_kv([
        (name, *by_name.get(name, ("—", "mute")))
        for name in _PROCESS_ROWS
    ])


def _v2_gates() -> None:
    gates = read_gates()
    paper_on = bool(getattr(gates, "paper_enabled", False))
    pm_on = bool(getattr(gates, "pm_enabled", False))
    live_on = bool(getattr(gates, "live_effective", False))
    kill = bool(getattr(gates, "kill_switch_present", False))
    daily_hit = False
    daily_day = ""
    try:
        snap = DataStore().risk_daily_loss_today()
        if snap.available and isinstance(snap.data, dict):
            daily_hit = bool(snap.data.get("hit"))
            daily_day = str(snap.data.get("day") or "")
    except Exception:
        log.warning("daily_loss gate read failed")
    _v2_kv([
        ("模拟盘", "ON" if paper_on else "关", "up" if paper_on else "mute"),
        ("仓位管理", "ON" if pm_on else "关", "up" if pm_on else "mute"),
        ("实盘", "ON" if live_on else "关", "down" if live_on else "mute"),
        ("日亏闸", "已触发" if daily_hit else "未触发", "down" if daily_hit else "mute"),
        ("杀开关", "存在" if kill else "—", "mute"),
    ])
    note = (
        f"模拟盘来源：{gates.origin_hint('paper')}。仓位来源：{gates.origin_hint('pm')}。"
        "关是当前预期状态；实盘若变「开启」会标红。"
        "网关「陈旧」要和拒因一起读：仓满拒单时无新成交属健康态。"
    )
    if daily_hit:
        note += f" 日亏闸今日（{daily_day or '—'} HKT）已拒单——保护态，不盘中改阈值。"
    ui.label(note).classes("v2-note")


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


def _v2_prior_values() -> list[tuple[str, str]]:
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


def _percent_points(value: str) -> float | None:
    text = value.strip().rstrip("%").strip()
    if not text or text == "—":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def prior_pulse_tone(label: str, value: str) -> str:
    """4H 存活 greens at >=60%. 当天死 reds at >=50%. Peak and graduation stay gray."""
    if label == "4H 存活":
        number = _percent_points(value)
        if number is not None and number >= 60:
            return "up"
        return ""
    if label == "当天死":
        number = _percent_points(value)
        if number is not None and number >= 50:
            return "down"
        return ""
    return "mute"


def _v2_priors() -> None:
    with ui.element("div").classes("v2-pulse"):
        for label_, value in _v2_prior_values():
            tone = prior_pulse_tone(label_, value)
            with ui.element("div").classes("v2-pulse-item"):
                ui.label(label_).classes("lbl")
                ui.label(value).classes(f"val {tone}".strip())


def _v2_today(store: DataStore) -> None:
    result = store.today_execution_stats()
    if not result.available or result.data is None:
        ui.label("暂无数据").classes("v2-note")
        return
    block = result.data
    total = _as_int(getattr(block, "n", None))
    _v2_table(
        [("信号数", "num"), ("校验通过", "num"), ("已执行", "num")],
        [[
            str(getattr(block, "n", "—")),
            fmt_rate(getattr(block, "ok_rate", None), _as_int(getattr(block, "n_ok", None)), total),
            fmt_rate(
                getattr(block, "executed_rate", None),
                _as_int(getattr(block, "n_executed", None)),
                total,
            ),
        ]],
    )


def _v2_week(store: DataStore) -> None:
    result = store.week_decision_stats()
    if not result.available or result.data is None:
        ui.label("暂无数据").classes("v2-note")
        return
    block = result.data
    # DecisionWindowStats rates use n_with_exec. There is no `n` field.
    denominator = _as_int(getattr(block, "n_with_exec", None))
    _v2_table(
        [("决策数", "num"), ("有执行", "num"), ("通过率", "num"), ("执行率", "num")],
        [[
            str(getattr(block, "n_decisions", "—")),
            str(getattr(block, "n_with_exec", "—")),
            fmt_rate(getattr(block, "ok_rate", None), _as_int(getattr(block, "n_ok", None)), denominator),
            fmt_rate(
                getattr(block, "executed_rate", None),
                _as_int(getattr(block, "n_executed", None)),
                denominator,
            ),
        ]],
    )


def _jev_percent(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return "—"
    if text.endswith("%"):
        return text
    try:
        number = float(text)
    except ValueError:
        return text
    if abs(number) <= 1.5:
        number *= 100.0
    return f"{number:.1f}%"


def jev_gate_view(row: dict[str, str] | None) -> list[tuple[str, str, str]] | None:
    """Map a trend CSV row onto n / unique_token / up_1h / up_4h. Empty row is None."""
    if not row:
        return None
    count = (row.get("n") or row.get("filtered_n") or row.get("raw_n") or "").strip()
    unique = (row.get("unique_token") or "").strip()
    up_1h = (row.get("up_1h") or row.get("up_1h_rate") or "").strip()
    up_4h = (row.get("up_4h") or row.get("up_4h_rate") or "").strip()
    if not any((count, unique, up_1h, up_4h)):
        return None
    return [
        ("n", count or "—", ""),
        ("unique_token", unique or "—", ""),
        ("1h 上涨率", _jev_percent(up_1h), "up" if up_1h else "mute"),
        ("4h 上涨率", _jev_percent(up_4h), ""),
    ]


def _jev_trend_row() -> dict[str, str] | None:
    candidates: list[Path] = []
    raw = os.environ.get("JEV_TREND_CSV", "").strip()
    if raw:
        candidates.append(Path(raw))
    relative = (
        Path("data") / "prior" / "exports" / "jev_validity_trend.csv",
        Path("exports") / "reports" / "jev_validity_trend.csv",
        Path("crawler") / "data" / "prior" / "exports" / "jev_validity_trend.csv",
        Path("crawler") / "exports" / "reports" / "jev_validity_trend.csv",
    )
    roots: list[Path] = []
    try:
        roots.append(db_root())
    except Exception:
        log.warning("jev trend root unavailable")
    roots.append(Path(__file__).resolve().parent.parent)
    seen: set[Path] = set()
    for root in roots:
        for name in relative:
            candidates.append(root / name)
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            if not resolved.is_file():
                continue
            with resolved.open("r", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        except OSError:
            log.warning("jev trend unreadable")
            continue
        if not rows:
            continue
        last = rows[-1]
        return {str(key): str(value) for key, value in last.items() if key}
    return None


def _v2_jev_gate() -> None:
    try:
        view = jev_gate_view(_jev_trend_row())
    except Exception:
        log.warning("jev gate read failed")
        view = None
    if not view:
        ui.label("暂无数据（周检后自动填充）").classes("v2-note")
    else:
        _v2_kv(view)
    ui.label(
        "统计功效旁证：门槛 n≥15 只排除随机级噪声；"
        "证 edge≈0.02 约需 ~350 已结算样本（Foresight Arena）。"
        "解读收紧，不改门槛、不算有效性。"
    ).classes("v2-note")


def _v2_market_base(store: DataStore) -> None:
    reader = getattr(store, "market_base_stats", None)
    result = reader() if callable(reader) else None
    if not result or not getattr(result, "available", False) or getattr(result, "data", None) is None:
        ui.label("暂无数据（indicator_history tier=hot）").classes("v2-note")
        return
    data = result.data
    n_1h = getattr(data, "n_1h", None)
    count = f"{n_1h:,}" if isinstance(n_1h, int) else "—"
    _v2_table(
        [("指标", ""), ("n_1h", "num"), ("中位 1h", "num"), ("中位 4h", "num"), ("均值 1h(w)", "num")],
        [[
            "hot 全量",
            count,
            _v2_text(getattr(data, "med_1h", None)),
            _v2_text(getattr(data, "med_4h", None)),
            _v2_text(getattr(data, "mean_1h_w", None)),
        ]],
    )


def _v2_strategy(store: DataStore) -> None:
    reader = getattr(store, "strategy_buckets", None)
    result = reader() if callable(reader) else None
    rows = getattr(getattr(result, "data", None), "rows", None)
    if not result or not getattr(result, "available", False) or not rows:
        ui.label("暂无数据（decision_log 按 strategy_id）").classes("v2-note")
        return
    table_rows: list[list[str]] = []
    for bucket in rows:
        table_rows.append([
            _v2_text(getattr(bucket, "strategy_id", None)),
            _v2_text(getattr(bucket, "n", None)),
            _v2_text(getattr(bucket, "ok_rate", None)),
        ])
    _v2_table(
        [("strategy", ""), ("n", "num"), ("通过率", "num")],
        table_rows,
    )
    ui.label("B/C 策略出信号后自动分桶").classes("v2-note")


def _v2_positions(store: DataStore) -> None:
    result = store.open_positions()
    if not result.available or result.data is None:
        ui.label("暂无数据").classes("v2-note")
        return
    payload = result.data
    cash_raw = getattr(payload, "cash_usd", None)
    equity_raw = getattr(payload, "equity_usd", None)
    cash = _v2_money(cash_raw)
    equity = _v2_money(equity_raw) if equity_raw is not None else cash
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
    start_cash = 10_000.0
    dd_text = "—"
    dd_tone = "mute"
    if isinstance(equity_raw, (int, float)) and not isinstance(equity_raw, bool) and start_cash:
        dd_pct = (float(equity_raw) - start_cash) / start_cash
        dd_text = f"{dd_pct * 100:+.2f}%"
        dd_tone = "up" if dd_pct > 0 else ("down" if dd_pct < 0 else "mute")
    _v2_kv([
        ("总权益", equity, dd_tone if dd_tone != "mute" else ""),
        ("相对起盘", dd_text, dd_tone),
        ("现金", cash, ""),
        ("持仓数", held, ""),
        ("仓位占比", share, ""),
    ])
    detail = tuple(held_rows)[:3] if held_rows is not None else ()
    if detail:
        table_rows: list[list[str]] = []
        tones: list[list[str]] = []
        for row in detail:
            symbol = str(getattr(row, "symbol", "") or "").strip() or "—"
            pnl, pnl_tone = _signed_usd(getattr(row, "pnl_usd", None))
            table_rows.append([symbol, _v2_money(getattr(row, "mtm_usd", None)), pnl])
            tones.append(["", "", pnl_tone])
        _v2_table(
            [("symbol", ""), ("市值", "num"), ("盈亏", "num")],
            table_rows,
            tones,
        )
    ui.label("现价取 pair_history 最新；无价为「—」").classes("v2-note")


def backtest_equity_tone(end: float, start: float | None) -> str:
    """Color a backtest against its own start cash.

    A fixed 10000 baseline paints a losing run green whenever it finishes
    above that number, and a winning run red when the book started higher.
    """
    if start is None:
        return "mute"
    if end > start:
        return "up"
    if end < start:
        return "down"
    return "mute"


def _v2_reject(store: DataStore) -> None:
    result = store.rejection_buckets(days=7)
    if not result.available or result.data is None:
        ui.label("暂无数据").classes("v2-note")
        return
    report = result.data
    buckets = getattr(report, "buckets", ()) or ()
    if not buckets:
        ui.label(f"近 {getattr(report, 'days', 7)} 日无拒因").classes("v2-note")
        return
    rows = [
        [_v2_text(getattr(item, "key", None)), str(getattr(item, "count", "—"))]
        for item in buckets[:12]
    ]
    _v2_table([("拒因", ""), ("笔数", "num")], rows)
    ui.label(
        f"合计 {getattr(report, 'total', 0)} · pm_check / risk[] 细分（含 daily_loss）"
    ).classes("v2-note")




# 旁证短钉：信号或胜率方向不是盈亏结论；只记分布缝，不算有效性。
_EVIDENCE_NAIL = "信号或胜率方向≠盈亏；旁证只记分布缝，不算有效性"
# 目标旁证短钉：跟踪缺口不是盈亏承诺，也不改闸。
_GOAL_NAIL = "目标跟踪≠会赚会亏；不改闸"
# M7 影簿短钉：S0 对照只有注册时的 8 笔，不能拿薄样本改闸。
_M7_NAIL = "影簿≠已改闸 · S0 历史仅 8 笔对照极薄 · 勿用薄样本批改闸"
_M7_PHASE0_NOTE = "与 Phase0 双列并存 · 不算有效性"


def _v2_signal_streak(store: DataStore) -> None:
    result = store.paper_signal_streaks(days=14)
    if not result.available or result.data is None:
        ui.label("暂无数据").classes("v2-note")
        return
    reports = result.data
    if not reports:
        ui.label("近 14 日无平仓序列").classes("v2-note")
        return
    rows = []
    for item in reports[:8]:
        rate = getattr(item, "cond_third_loss_rate", None)
        rate_t = f"{float(rate)*100:.0f}%" if isinstance(rate, (int, float)) else "—"
        rows.append([
            _v2_text(getattr(item, "strategy_id", None), 18),
            str(getattr(item, "closed", "—")),
            str(getattr(item, "max_loss_streak", "—")),
            str(getattr(item, "current_loss_streak", "—")),
            f"{rate_t} (n={getattr(item, 'cond_n', 0)})",
        ])
    _v2_table(
        [("策略", ""), ("已平", "num"), ("最长连亏", "num"), ("当前连亏", "num"), ("P(3亏|2亏)", "num")],
        rows,
    )
    ui.label(
        "F-1：paper 只记连亏，不自动熔断；"
        "周检看条件概率有增益再进 PM（R2/R3）。"
        + _EVIDENCE_NAIL
    ).classes("v2-note")


def _v2_exit_hold(store: DataStore) -> None:
    result = store.paper_exit_holds(days=14)
    if not result.available or result.data is None:
        ui.label("暂无数据").classes("v2-note")
        return
    report = result.data
    buckets = getattr(report, "buckets", ()) or ()
    if not buckets:
        ui.label("近 14 日无退出持仓").classes("v2-note")
        return
    rows = []
    tones = []
    for item in buckets[:8]:
        pnl = getattr(item, "pnl_usd", None)
        pnl_t, pnl_tone = _signed_usd(pnl)
        med = getattr(item, "median_hold_min", None)
        med_t = f"{float(med):.0f}m" if isinstance(med, (int, float)) else "—"
        rows.append([
            _v2_text(getattr(item, "exit_reason", None)),
            str(getattr(item, "count", "—")),
            med_t,
            pnl_t,
        ])
        tones.append(["", "", "", pnl_tone])
    _v2_table(
        [("退出", ""), ("笔数", "num"), ("持仓中位", "num"), ("PnL", "num")],
        rows,
        tones,
    )
    ui.label(
        "F-3/C3：对照同步/快速退出 vs max_hold 硬时间；"
        "现网 stop_loss 持仓更短但亏更大——退出快≠亏得小。"
        + _EVIDENCE_NAIL
    ).classes("v2-note")

def _v2_trade_freq(store: DataStore) -> None:
    result = store.paper_trade_frequency(days=14)
    if not result.available or result.data is None:
        ui.label("暂无数据").classes("v2-note")
        return
    report = result.data
    rows_data = getattr(report, "rows", ()) or ()
    if not rows_data:
        ui.label(f"近 {getattr(report, 'days', 14)} 日无成交").classes("v2-note")
        return
    avg = getattr(report, "avg_opens_per_active_day", None)
    _v2_kv([
        ("活跃日均开仓", f"{avg:.1f}" if isinstance(avg, (int, float)) else "—", "mute"),
        ("开仓合计", str(getattr(report, "total_opens", "—")), "mute"),
        ("平仓合计", str(getattr(report, "total_closed", "—")), "mute"),
        (
            "实现合计",
            _v2_money(getattr(report, "total_pnl_usd", None)),
            "down" if isinstance(getattr(report, "total_pnl_usd", None), (int, float)) and report.total_pnl_usd < 0 else "mute",
        ),
    ])
    table = []
    tones = []
    for item in list(rows_data)[-10:]:
        pnl = getattr(item, "pnl_usd", None)
        pnl_text, pnl_tone = _signed_usd(pnl)
        table.append([
            _v2_text(getattr(item, "day", None)),
            str(getattr(item, "opens", "—")),
            str(getattr(item, "closed", "—")),
            pnl_text,
        ])
        tones.append(["", "", "", pnl_tone])
    _v2_table(
        [("日", ""), ("开仓", "num"), ("平仓", "num"), ("PnL", "num")],
        table,
        tones,
    )
    ui.label(
        "G-1：日切 HKT；多样本可取，但日开↑常伴随损耗↑——观察峰，不解读为应加仓频。"
        "对照冷却/max_open，不盘中改参。"
        + _EVIDENCE_NAIL
    ).classes("v2-note")

def _v2_closed(store: DataStore) -> None:
    result = store.paper_closed_buckets(days=14)
    if not result.available or result.data is None:
        ui.label("暂无数据").classes("v2-note")
        return
    report = result.data
    buckets = getattr(report, "buckets", ()) or ()
    if not buckets:
        ui.label(f"近 {getattr(report, 'days', 14)} 日无平仓").classes("v2-note")
        return
    def _pct(rate: object) -> str:
        if isinstance(rate, (int, float)) and not isinstance(rate, bool):
            return f"{float(rate) * 100:.1f}%"
        return "—"

    win_rate = getattr(report, "win_rate", None)
    wr_text = _pct(win_rate)
    first_n = getattr(report, "first_hit_n", 0) or 0
    first_rate = getattr(report, "first_hit_rate", None)
    live_n = getattr(report, "first_hit_live_n", 0) or 0
    live_rate = getattr(report, "first_hit_live_rate", None)
    first_text = f"{_pct(first_rate)} (n={first_n})" if first_n else "—"
    live_text = f"{_pct(live_rate)} (n={live_n})" if live_n else "—"
    worst = getattr(report, "worst_pnl_usd", None)
    rest = getattr(report, "rest_pnl_usd", None)
    wipe = bool(getattr(report, "single_wipe", False))
    _v2_kv([
        ("累计胜率", wr_text, "mute"),
        ("首笔命中", first_text, "mute"),
        ("live 首笔", live_text, "mute"),
        (
            "最差单笔",
            _v2_money(worst) if isinstance(worst, (int, float)) else "—",
            "down" if isinstance(worst, (int, float)) and worst < 0 else "mute",
        ),
        (
            "剔除最差",
            _v2_money(rest) if isinstance(rest, (int, float)) else "—",
            "up" if isinstance(rest, (int, float)) and rest > 0 else "mute",
        ),
        ("单笔抹盈", "是" if wipe else "否", "down" if wipe else "mute"),
        (
            "止损占亏",
            (
                f"{float(getattr(report, 'stop_loss_share', 0) or 0) * 100:.0f}%"
                if isinstance(getattr(report, "stop_loss_share", None), (int, float))
                else "—"
            ),
            (
                "down"
                if isinstance(getattr(report, "stop_loss_share", None), (int, float))
                and float(report.stop_loss_share) >= 0.5
                else "mute"
            ),
        ),
    ])
    ui.label(
        "半胜仍亏：止损可抹盈，常由少数 stop_loss 打穿主导；"
        "日亏闸触发是保护不是失效——对齐 min_liq/冷却，不盘中改 TP·SL。"
    ).classes("v2-note v2-note-warn")
    rows: list[list[str]] = []
    for item in buckets[:12]:
        pnl = getattr(item, "pnl_usd", None)
        rows.append([
            _v2_text(getattr(item, "exit_reason", None)),
            _v2_text(getattr(item, "liq_bucket", None)),
            str(getattr(item, "count", "—")),
            _v2_money(pnl) if isinstance(pnl, (int, float)) else "—",
        ])
    _v2_table(
        [("退出", ""), ("流动性", ""), ("笔数", "num"), ("PnL", "num")],
        rows,
    )
    total = getattr(report, "total_pnl_usd", None)
    by_exit: dict[str, list[float | int]] = {}
    for item in buckets:
        er = str(getattr(item, "exit_reason", None) or "unknown")
        slot = by_exit.setdefault(er, [0, 0.0])
        slot[0] = int(slot[0]) + int(getattr(item, "count", 0) or 0)
        pnl = getattr(item, "pnl_usd", None)
        if isinstance(pnl, (int, float)) and not isinstance(pnl, bool):
            slot[1] = float(slot[1]) + float(pnl)
    if by_exit:
        parts = [
            f"{name}×{int(cnt)}≈{_v2_money(pnl)}"
            for name, (cnt, pnl) in sorted(by_exit.items(), key=lambda kv: float(kv[1][1]))
        ]
        ui.label("退出合计：" + "；".join(parts)).classes("v2-note")
    ui.label(
        f"已平 {getattr(report, 'closed', 0)} · 合计 "
        f"{_v2_money(total) if isinstance(total, (int, float)) else '—'} · {_EVIDENCE_NAIL}"
    ).classes("v2-note")
    ui.label(
        "首笔命中：按 pair 最早一笔已平仓；live_candidate 另列（n 仍小，对照周检）。"
    ).classes("v2-note")


def _observe_num(value: object, digits: int = 4) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "data gap"
    return f"{float(value):.{digits}f}"


def _short_addr(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    if len(text) <= 12:
        return text
    return text[:4] + "…" + text[-4:]


def _v2_intel_holders(store: DataStore) -> None:
    result = store.intel_holder_observe()
    if not result.available or result.data is None:
        ui.label(result.reason or "data gap").classes("v2-note")
        ui.label("权重0 / 不接决策").classes("v2-note")
        return
    report = result.data
    _v2_kv([
        ("快照样本", str(getattr(report, "snapshot_n", 0)), "mute"),
        ("top10 非空", str(getattr(report, "non_null_share_n", 0)), "mute"),
        ("devΔ 非空", str(getattr(report, "non_null_delta_n", 0)), "mute"),
        ("权重", "0", "mute"),
    ])
    latest = getattr(report, "latest", ()) or ()
    if not latest or getattr(report, "gap", "") == "empty":
        ui.label("data gap").classes("v2-note")
    if latest:
        rows = []
        for item in latest:
            chain = _v2_text(getattr(item, "chain_id", None), 8)
            token = _short_addr(getattr(item, "token_address", None))
            rows.append([
                f"{chain} {token}".strip(),
                _observe_num(getattr(item, "top10_share", None)),
                _observe_num(getattr(item, "dev_balance_delta", None)),
                _v2_text(getattr(item, "recorded_at", None), 20),
            ])
        _v2_table(
            [("币", ""), ("top10_share", "num"), ("devΔ", "num"), ("时间", "")],
            rows,
        )
    ui.label("权重0 / 不接决策").classes("v2-note")
    ui.label(f"{_EVIDENCE_NAIL} · 不进盘中参 · 高 bot 不跳单").classes("v2-note")
    crowd = store.intel_crowding_note()
    if crowd.available and crowd.data is not None:
        ui.label(_v2_text(getattr(crowd.data, "line", None))).classes("v2-note")
    else:
        ui.label(f"拥挤旁证 data gap · 不否决 · {_EVIDENCE_NAIL}").classes("v2-note")


def _goal_short_text(value: object) -> tuple[str, str]:
    """Signed dollars. A positive gap (still short of the mark) is down."""
    text, _tone = _signed_usd(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—", "mute"
    if value > 0:
        return text, "down"
    if value < 0:
        return text, "up"
    return text, "mute"


def _mount_goal_20k(store: DataStore) -> None:
    _v2_comp(
        ("距", "$20k"),
        "span-6",
        lambda: _v2_goal_20k(store),
        foot=(
            f"10日目标旁证 · 起点 {GOAL_START_DAY.isoformat()} · "
            f"截止 {GOAL_DEADLINE_DAY.isoformat()} HKT · {_GOAL_NAIL}"
        ),
        tools=("只读",),
    )


def _v2_goal_20k(store: DataStore, *, now: datetime | None = None) -> None:
    result = store.paper_goal_pace(now=now)
    if not result.available or result.data is None:
        ui.label(result.reason or "data gap").classes("v2-note")
        ui.label(_GOAL_NAIL).classes("v2-note")
        return
    report = result.data
    equity = getattr(report, "equity_usd", None)
    gap = getattr(report, "gap_usd", None)
    days = getattr(report, "days_left", None)
    daily = getattr(report, "daily_need_usd", None)
    target = getattr(report, "target_usd", None)
    gap_text, gap_tone = _goal_short_text(gap)
    if isinstance(days, bool) or not isinstance(days, int):
        days_text = "—"
        daily_text, daily_tone = "—", "mute"
    elif days <= 0:
        days_text = "0"
        daily_text, daily_tone = "—", "mute"
    else:
        days_text = str(days)
        daily_text, daily_tone = _goal_short_text(daily)
    if isinstance(equity, bool) or not isinstance(equity, (int, float)):
        equity_text = "—"
    else:
        equity_text = _v2_money(equity)
    if isinstance(target, bool) or not isinstance(target, (int, float)):
        target_text = "$20,000"
    else:
        target_text = _v2_money(target)
    _v2_kv([
        ("权益粗算", equity_text, "mute"),
        ("目标", target_text, "mute"),
        ("缺口", gap_text, gap_tone),
        ("剩余天", days_text, "mute"),
        ("日均需", daily_text, daily_tone),
    ])
    if getattr(report, "mark_to_market", False):
        basis = "盯市口径异常 · 本卡只读粗算"
    else:
        basis = "未盯市 · cash + 未平 size_usd"
    as_of = _v2_text(getattr(report, "as_of_day", None))
    ui.label(f"{basis} · 口径日 {as_of} HKT").classes("v2-note")
    note = getattr(report, "note", "") or ""
    if note:
        ui.label(_v2_text(note)).classes("v2-note")
    ui.label(_GOAL_NAIL).classes("v2-note")


def _mount_phase0_shadow() -> None:
    """Own OVERVIEW card, mounted beside the other evidence cards."""
    _v2_comp(
        ("止损", "影子"),
        "span-12",
        _v2_phase0_shadow,
        foot=f"Phase0 双列 · exports/shadow_skip_stop_prone_live.csv · 不 soft-skip · {PHASE0_NAIL}",
        tools=("只读",),
    )


def _mount_phase1_counterfactual() -> None:
    """Read-only OVERVIEW card, directly after the Phase0 dual column."""
    _v2_comp(
        ("Phase1", "soft-skip 反事实"),
        "span-12",
        _v2_phase1_counterfactual,
        foot=f"只读旁证 · 不 soft-skip · 不改闸 · {PHASE1_CF_NAIL}",
        tools=("只读",),
    )


def _v2_phase1_counterfactual() -> None:
    try:
        report = load_phase1_counterfactual()
    except Exception:
        log.warning("phase1 counterfactual card failed")
        ui.label("反事实不可读").classes("v2-note")
        ui.label(PHASE1_CF_NAIL).classes("v2-note")
        return
    ui.label(baseline_line(report)).classes("v2-note")
    for case in report.cases:
        ui.label(case_line(case)).classes("v2-note")
    ui.label(honest_line(report)).classes("v2-note")
    ui.label(PHASE1_WINDOW).classes("v2-note")
    ui.label(PHASE1_CANDIDATE).classes("v2-note")
    if report.note:
        ui.label(report.note).classes("v2-note")
    ui.label("只读旁证 · 不 soft-skip · 不改闸").classes("v2-note")
    ui.label(PHASE1_CF_NAIL).classes("v2-note")


def _mount_c_exit_shadow() -> None:
    """Read-only OVERVIEW card for the C-exit shadow book, beside Phase1."""
    _v2_comp(
        ("C 出口", "旁簿"),
        "span-12",
        _v2_c_exit_shadow,
        foot=f"{C_EXIT_DISCIPLINE} · {C_EXIT_NAIL}",
        tools=("只读",),
    )


def _v2_c_exit_shadow() -> None:
    try:
        report = load_c_exit_shadow()
    except Exception:
        log.warning("c exit shadow card failed")
        ui.label("旁簿不可读").classes("v2-note")
        ui.label(C_EXIT_DISCIPLINE).classes("v2-note")
        ui.label(C_EXIT_NAIL).classes("v2-note")
        return
    ui.label(C_EXIT_NAIL).classes("v2-note")
    ui.label(C_EXIT_DISCIPLINE).classes("v2-note")
    ui.label(C_EXIT_GRID).classes("v2-note")
    for row in report.tags:
        ui.label(c_exit_tag_line(row)).classes("v2-note")
    ui.label(c_exit_main_line(report)).classes("v2-note")
    ui.label(c_exit_verdict_line(report)).classes("v2-note")
    ui.label(C_EXIT_THRESHOLD).classes("v2-note")
    ui.label(C_EXIT_ROLLBACK).classes("v2-note")
    hit = rollback_hit_line(report)
    if hit:
        ui.label(hit).classes("v2-note")
    if report.note:
        ui.label(report.note).classes("v2-note")


def _mount_entry_c_exit_joint() -> None:
    """Read-only OVERVIEW card for the entry-shadow × C-exit joint, after C-exit."""
    _v2_comp(
        ("实验A", "入口×C出口"),
        "span-12",
        _v2_entry_c_exit_joint,
        foot=JOINT_DISCIPLINE,
        tools=("只读",),
    )


def _v2_entry_c_exit_joint() -> None:
    try:
        report = load_entry_c_exit_joint()
    except Exception:
        log.warning("entry c exit joint card failed")
        ui.label("联合表不可读").classes("v2-note")
        ui.label(JOINT_NAIL).classes("v2-note")
        ui.label(JOINT_LIQ_NAIL).classes("v2-note")
        ui.label(JOINT_LIQ_LIVE).classes("v2-note")
        ui.label(JOINT_DISCIPLINE).classes("v2-note")
        return
    ui.label(joint_nail_line(report)).classes("v2-note")
    ui.label(JOINT_LIQ_NAIL).classes("v2-note")
    ui.label(JOINT_LIQ_LIVE).classes("v2-note")
    ui.label(JOINT_DISCIPLINE).classes("v2-note")
    ui.label(JOINT_SHARE).classes("v2-note")
    ui.label(joint_nine_line(report)).classes("v2-note")
    split_rows, split_tones = joint_liq_split_rows(report)
    _v2_table(list(LIQ_SPLIT_HEADERS), split_rows, split_tones)
    ui.label(joint_liq_risk_line(report)).classes("v2-note")
    rows, tones = joint_table_rows(report)
    _v2_table(list(TABLE_HEADERS), rows, tones)
    ui.label("★ = R3 × c_sl5_h60、c_sl5_h90 主旁证 · liq† = missing 风险").classes("v2-note")
    ui.label(joint_peak_line(report)).classes("v2-note")
    ui.label(JOINT_PNL_NOTE).classes("v2-note")
    ui.label(joint_verdict_line(report)).classes("v2-note")
    if report.note:
        ui.label(report.note).classes("v2-note")


def _mount_m7_shadow(store: DataStore) -> None:
    _v2_comp(
        ("M7", "S0|S1|liq_missing"),
        "span-6",
        lambda: _v2_m7_shadow(store),
        foot=f"{_M7_NAIL} · {_M7_PHASE0_NOTE}",
        tools=("只读",),
    )


def _v2_phase0_shadow() -> None:
    try:
        report = load_shadow_phase0()
    except Exception:
        log.warning("phase0 shadow card failed")
        ui.label("CSV 不可读").classes("v2-note")
        ui.label(PHASE0_NAIL).classes("v2-note")
        return
    if not report.available:
        ui.label(report.reason or "CSV 缺失").classes("v2-note")
        ui.label("不 soft-skip · 不改闸").classes("v2-note")
        ui.label(PHASE0_NAIL).classes("v2-note")
        return
    _v2_table(
        [("", ""), (FLAG_R3, "num"), (FLAG_LIQ, "num")],
        [
            ["命中 n", format_count(report.r3), format_count(report.liq)],
            ["抓 SL", format_sl(report.r3), format_sl(report.liq)],
            ["误伤 TP", format_tp(report.r3), format_tp(report.liq)],
        ],
    )
    null_text = "—" if report.liq_null is None else str(report.liq_null)
    ui.label(f"同为 1：{format_both(report)}").classes("v2-note")
    ui.label(f"closed {report.closed} · NULL liq {null_text} 不打 liq 标").classes("v2-note")
    ui.label("源 exports/shadow_skip_stop_prone_live.csv · 不 soft-skip").classes("v2-note")
    ui.label(PHASE0_NAIL).classes("v2-note")


def _v2_m7_shadow(store: DataStore) -> None:
    result = store.m7_shadow_register()
    if not result.available or result.data is None:
        ui.label(result.reason or "data gap").classes("v2-note")
        ui.label(_M7_NAIL).classes("v2-note")
        ui.label(_M7_PHASE0_NOTE).classes("v2-note")
        return
    report = result.data
    _v2_kv([
        ("S1", str(_as_int(getattr(report, "n_s1", 0))), "mute"),
        ("S0", str(_as_int(getattr(report, "n_s0", 0))), "mute"),
        ("liq_missing", str(_as_int(getattr(report, "n_missing", 0))), "mute"),
        ("would_open_S0", str(_as_int(getattr(report, "would_open_s0", 0))), "mute"),
        ("would_open_S1", str(_as_int(getattr(report, "would_open_s1", 0))), "mute"),
        ("leak", str(_as_int(getattr(report, "n_leak", 0))), "mute"),
        ("Phase0", str(_as_int(getattr(report, "n_phase0", 0))), "mute"),
    ])
    cohort = _v2_text(getattr(report, "cohort", ""))
    source = _v2_text(getattr(report, "source", ""))
    ui.label(f"{cohort} · 来源 {source}").classes("v2-note")
    ui.label("leak 不并进 S0 / S1 / liq_missing").classes("v2-note")
    note = getattr(report, "note", "") or ""
    if note:
        ui.label(_v2_text(note)).classes("v2-note")
    ui.label(_M7_NAIL).classes("v2-note")
    ui.label(_M7_PHASE0_NOTE).classes("v2-note")


def _v2_token_age(store: DataStore) -> None:
    result = store.token_age_observe()
    if not result.available or result.data is None:
        ui.label(result.reason or "data gap").classes("v2-note")
        ui.label(f"{_EVIDENCE_NAIL} · 不进盘中参").classes("v2-note")
        return
    report = result.data
    total = _as_int(getattr(report, "total", 0))
    unknown = _as_int(getattr(report, "unknown", 0))
    ratio = getattr(report, "unknown_ratio", None)
    if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
        ratio_text = f"{float(ratio) * 100:.1f}%"
    else:
        ratio_text = "—"
    unknown_text = f"{unknown}/{total}" if total else "—"
    _v2_kv([
        ("近端", str(total), "mute"),
        ("有字段", str(_as_int(getattr(report, "with_field", 0))), "mute"),
        ("unknown", unknown_text, "mute"),
        ("unknown 占比", ratio_text, "mute"),
    ])
    buckets = getattr(report, "buckets", ()) or ()
    _v2_table(
        [("桶", ""), ("笔数", "num")],
        [
            [_v2_text(getattr(item, "label", None)), str(_as_int(getattr(item, "count", 0)))]
            for item in buckets
        ],
    )
    samples = getattr(report, "samples", ()) or ()
    sample_rows: list[list[str]] = []
    for item in samples:
        age_days = getattr(item, "age_days", None)
        if isinstance(age_days, bool) or not isinstance(age_days, (int, float)):
            continue
        sample_rows.append([str(_as_int(getattr(item, "decision_id", 0))), f"{float(age_days):.2f}"])
    if sample_rows:
        _v2_table([("id", "num"), ("age_days", "num")], sample_rows)
    if total == 0:
        ui.label("近端无决策行").classes("v2-note")
    ui.label(
        f"近 {_as_int(getattr(report, 'limit', 0))} 行 · {_EVIDENCE_NAIL} · 不进盘中参 · "
        "源 decision_log.raw_json.extras.pairCreatedAt · 旧行无字段计入 unknown · 不改闸/不改权"
    ).classes("v2-note")


def _v2_intel_slippage(store: DataStore) -> None:
    result = store.intel_slippage_observe()
    if not result.available or result.data is None:
        ui.label(result.reason or "data gap").classes("v2-note")
        ui.label("高 bot 不跳单").classes("v2-note")
        return
    report = result.data
    fills = getattr(report, "total_fills", None)
    bot_fills = getattr(report, "bot_fills", None)
    share = getattr(report, "bot_share", None)
    if isinstance(share, (int, float)) and not isinstance(share, bool) and isinstance(fills, int) and isinstance(bot_fills, int):
        fill_text = f"{bot_fills}/{fills}"
    else:
        fill_text = "data gap"
    _v2_kv([
        ("1h n", str(getattr(report, "slippage_n", 0)), "mute"),
        ("mean", _observe_num(getattr(report, "mean", None)), "mute"),
        ("p50", _observe_num(getattr(report, "p50", None)), "mute"),
        ("p90", _observe_num(getattr(report, "p90", None)), "mute"),
        ("bot 份额", _observe_num(share), "mute"),
        ("bot 笔数", fill_text, "mute"),
        ("高 bot", "不跳单", "mute"),
    ])
    if getattr(report, "high_bot_skip_wired", False):
        ui.label("high_bot_skip 标记异常 · 仍不跳单").classes("v2-note")
    ui.label(f"{_EVIDENCE_NAIL} · 不进盘中参 · 高 bot 不跳单").classes("v2-note")


def _v2_btnews(store: DataStore) -> None:
    result = store.latest_backtest()
    if result.available and result.data is not None:
        run = result.data
        end = getattr(run, "end_equity", None)
        if isinstance(end, (int, float)) and not isinstance(end, bool):
            equity = f"${end:,.0f}"
            start = getattr(run, "start_cash", None)
            start_value = start if isinstance(start, (int, float)) and not isinstance(start, bool) else None
            tone = backtest_equity_tone(float(end), start_value)
        else:
            equity = "—"
            tone = "mute"
        done = getattr(run, "done_at", None)
        if done in (None, ""):
            done = getattr(run, "finished_at", None)
        _v2_kv([
            ("最近回测", _v2_text(getattr(run, "run_id", None), 16), "mute"),
            ("权益", equity, tone),
            ("完成", _v2_text(done), "mute"),
        ])
    else:
        _v2_kv([("最近回测", "暂无", "mute")])
    items = _result_rows(store.latest_news(limit=3))
    if not items:
        ui.label("暂无新闻").classes("v2-note")
        return
    for item in items[:3]:
        ui.label(f"· {_v2_text(getattr(item, 'title', None))}").classes("v2-news")



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
