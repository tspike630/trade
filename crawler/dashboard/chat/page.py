"""M6 AI chat NiceGUI page: sessions sidebar + streaming bubbles (ui.label only)."""

from __future__ import annotations

import logging
import threading
from typing import Any

from nicegui import app, ui

from dashboard.chat.grok import grok_configured, run_chat
from dashboard.chat.sessions import SessionStore
from dashboard.chat.tools import build_tools
from dashboard.data import DataStore

log = logging.getLogger("dashboard.chat")

CHAT_CSS = """
.chat-shell {
  display: flex;
  gap: 0;
  height: calc(100vh - 72px);
  min-height: 420px;
  border: 1px solid #1a1a1a;
  border-radius: 6px;
  overflow: hidden;
  background: #0a0a0a;
}
.chat-sidebar {
  width: 280px;
  min-width: 280px;
  background: #111111;
  border-right: 1px solid #1a1a1a;
  display: flex;
  flex-direction: column;
}
.chat-sidebar.collapsed { width: 0; min-width: 0; overflow: hidden; border-right: none; }
.chat-side-head {
  padding: 8px;
  border-bottom: 1px solid #1a1a1a;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.chat-session {
  padding: 8px 10px;
  border-bottom: 1px solid #161616;
  cursor: pointer;
  color: #e5e5e5;
  font-size: 13px;
}
.chat-session:hover { background: #161616; }
.chat-session.active {
  background: #161616;
  border-left: 2px solid #d97706;
  color: #e5e5e5;
}
.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: #080808;
}
.chat-msgs { flex: 1; padding: 12px; }
.chat-bubble {
  max-width: 86%;
  padding: 8px 10px;
  border-radius: 6px;
  margin: 6px 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: Consolas, "Courier New", monospace;
  font-size: 14px;
  line-height: 1.45;
  color: #e5e5e5;
}
.chat-bubble.user {
  background: #2a6fdb;
  margin-left: auto;
}
.chat-bubble.assistant {
  background: #232830;
  margin-right: auto;
}
.chat-bubble.system-note {
  background: #1a1000;
  color: #808080;
  margin: 4px auto;
  font-size: 12px;
}
.chat-input-bar {
  border-top: 1px solid #1a1a1a;
  padding: 8px 10px;
  background: #0a0a0a;
  display: flex;
  gap: 8px;
  align-items: flex-end;
}
.chat-unconfigured {
  padding: 24px;
  color: #ca8a04;
  font-family: Consolas, "Courier New", monospace;
}
"""


def _context_prefix() -> str:
    try:
        raw = app.storage.user.get("chat_context")
    except Exception:
        return ""
    if not raw:
        return ""
    token = str(raw).strip()
    if not token:
        return ""
    return f"[Context: {token}] "


def set_chat_context(token: str) -> None:
    """Called from M2/M5 row clicks. Stores a short context token for the input prefix."""
    clean = (token or "").strip()
    if not clean:
        return
    try:
        app.storage.user["chat_context"] = clean[:120]
    except Exception:
        pass


def clear_chat_context() -> None:
    try:
        app.storage.user.pop("chat_context", None)
    except Exception:
        pass


def render_chat_page(*, insecure: bool = False) -> None:
    ui.add_css(CHAT_CSS)

    state: dict[str, Any] = {
        "session_id": None,
        "streaming": False,
        "done": False,
        "stop": threading.Event(),
        "stick": True,
        "sidebar": True,
        "bubble": None,
        "acc": "",
        "final": "",
        "error": "",
        "last_painted": "",
    }

    with ui.element("div").classes("page"):
        with ui.element("div").classes("term-strip"):
            with ui.element("div").classes("term-strip-row"):
                with ui.element("div").classes("term-brand"):
                    ui.label("TRADE").classes("term-mark")
                    ui.label("AI 问答").classes("term-title")
                    ui.label("M6 · READ-ONLY TOOLS").classes("term-meta")
                with ui.element("div").classes("top-actions"):
                    ui.button("监控首页", on_click=lambda: ui.navigate.to("/")).props(
                        "outline dense"
                    ).classes("ghost-btn")
                    ui.button(
                        "侧栏",
                        on_click=lambda: _toggle_sidebar(sidebar_el, state),
                    ).props("flat dense").classes("ghost-btn")

        if insecure:
            ui.label("本地未鉴权模式（ALLOW_INSECURE_API=1）。不要暴露到公网。").classes("banner")

        if not grok_configured():
            with ui.element("div").classes("chat-shell"):
                ui.label(
                    "未配置 GROK_API_KEY（或 XAI_API_KEY）。设置环境变量后重启看板即可使用 AI 问答；"
                    "监控首页与其它面板不受影响。"
                ).classes("chat-unconfigured")
            return

        store = DataStore()
        sessions = SessionStore()
        tools = build_tools(store)

        with ui.element("div").classes("chat-shell"):
            sidebar_el = ui.element("div").classes("chat-sidebar")
            with sidebar_el:
                with ui.element("div").classes("chat-side-head"):
                    search = ui.input(placeholder="搜索会话").props("dense outlined dark").classes(
                        "w-full"
                    )
                    ui.button("新建", on_click=lambda: _new_session()).props(
                        "dense unelevated"
                    ).style("background:#d97706;color:#fff;font-weight:600;")
                session_list = ui.column().classes("w-full").style(
                    "overflow:auto;flex:1;padding:0;"
                )

            with ui.element("div").classes("chat-main"):
                scroll = ui.scroll_area().classes("chat-msgs w-full")
                scroll.style("flex:1;height:100%;")
                with scroll:
                    messages_box = ui.column().classes("w-full")

                with ui.element("div").classes("chat-input-bar"):
                    composer = (
                        ui.textarea(placeholder="输入问题… 可用工具查持仓/信号/行情/新闻")
                        .props("outlined dark autogrow")
                        .classes("w-full")
                        .style("background:#0a0a0a;min-height:48px;")
                    )
                    send_btn = ui.button("发送").props("dense unelevated").style(
                        "background:#d97706;color:#fff;font-weight:600;min-width:72px;"
                    )

        def _ensure_session() -> str:
            sid = state.get("session_id")
            if sid:
                return str(sid)
            info = sessions.create_session()
            state["session_id"] = info.id
            return info.id

        def _refresh_sessions() -> None:
            session_list.clear()
            q = (search.value or "").strip()
            items = sessions.list_sessions(q)
            with session_list:
                if not items:
                    ui.label("暂无会话").classes("muted").style("padding:10px;")
                    return
                for item in items:
                    active = item.id == state.get("session_id")
                    klass = "chat-session active" if active else "chat-session"
                    with ui.element("div").classes(klass):
                        ui.label(item.title or "未命名").style(
                            "font-weight:600;font-size:13px;"
                        )
                        ui.label(item.updated_at).style(
                            "color:#525252;font-size:11px;margin-top:2px;"
                        )
                        with ui.row().classes("gap-1").style("margin-top:4px;"):
                            ui.button(
                                "开",
                                on_click=lambda _e=None, s=item.id: _select_session(s),
                            ).props("flat dense size=sm")
                            ui.button(
                                "改名",
                                on_click=lambda _e=None, s=item.id, t=item.title: _rename(s, t),
                            ).props("flat dense size=sm")
                            ui.button(
                                "删",
                                on_click=lambda _e=None, s=item.id: _delete(s),
                            ).props("flat dense size=sm color=negative")

        def _render_history() -> None:
            messages_box.clear()
            sid = state.get("session_id")
            with messages_box:
                if not sid:
                    ui.label("新建或选择会话开始提问。").classes("chat-bubble system-note")
                    return
                for msg in sessions.list_messages(str(sid)):
                    if msg.role not in {"user", "assistant"}:
                        continue
                    klass = "chat-bubble user" if msg.role == "user" else "chat-bubble assistant"
                    ui.label(msg.content or "").classes(klass)
            _scroll_bottom()

        def _scroll_bottom() -> None:
            if not state.get("stick", True):
                return
            try:
                scroll.scroll_to(percent=1.0)
            except Exception:
                pass

        def _select_session(session_id: str) -> None:
            if state.get("streaming"):
                ui.notify("请先停止当前生成")
                return
            state["session_id"] = session_id
            state["stick"] = True
            _refresh_sessions()
            _render_history()

        def _new_session() -> None:
            if state.get("streaming"):
                ui.notify("请先停止当前生成")
                return
            info = sessions.create_session()
            state["session_id"] = info.id
            state["stick"] = True
            _refresh_sessions()
            _render_history()

        def _rename(session_id: str, current: str) -> None:
            with ui.dialog() as dialog, ui.card().style("background:#0a0a0a;min-width:280px;"):
                ui.label("重命名会话").style("color:#e5e5e5;font-weight:600;")
                field = ui.input(value=current).classes("w-full")

                def save() -> None:
                    if sessions.rename_session(session_id, field.value or ""):
                        _refresh_sessions()
                    dialog.close()

                ui.button("保存", on_click=save).style(
                    "background:#d97706;color:#fff;font-weight:600;"
                )
            dialog.open()

        def _delete(session_id: str) -> None:
            sessions.delete_session(session_id)
            if state.get("session_id") == session_id:
                state["session_id"] = None
                messages_box.clear()
            _refresh_sessions()
            if state.get("session_id") is None:
                items = sessions.list_sessions()
                if items:
                    _select_session(items[0].id)

        def _set_sending(active: bool) -> None:
            state["streaming"] = active
            if active:
                send_btn.set_text("停止")
                send_btn.style("background:#dc2626;color:#fff;font-weight:600;min-width:72px;")
            else:
                send_btn.set_text("发送")
                send_btn.style("background:#d97706;color:#fff;font-weight:600;min-width:72px;")

        def _paint_stream() -> None:
            """UI-thread poller: paint worker-accumulated text; finalize when done."""
            if not state.get("streaming") and not state.get("done"):
                return
            bub = state.get("bubble")
            acc = state.get("acc") or ""
            if bub is not None and acc != state.get("last_painted"):
                bub.set_text(acc or "…")
                state["last_painted"] = acc
                _scroll_bottom()
            if not state.get("done"):
                return
            final = (state.get("final") or acc or "").strip()
            if state["stop"].is_set() and final and not final.endswith("（已停止）"):
                final = final + "\n\n（已停止）"
            if not final:
                final = "（已停止）" if state["stop"].is_set() else (state.get("error") or "暂无回复")
            if bub is not None:
                bub.set_text(final)
            sid = state.get("session_id")
            if sid:
                sessions.append_message(str(sid), "assistant", final)
            state["bubble"] = None
            state["done"] = False
            state["final"] = ""
            state["error"] = ""
            state["acc"] = ""
            state["last_painted"] = ""
            _set_sending(False)
            _refresh_sessions()
            _scroll_bottom()

        ui.timer(0.2, _paint_stream)

        def _on_send_or_stop() -> None:
            if state.get("streaming"):
                state["stop"].set()
                return
            text = (composer.value or "").strip()
            prefix = _context_prefix()
            if prefix and not text.startswith("[Context:"):
                text = f"{prefix}{text}".strip()
            if not text:
                return
            composer.value = ""
            clear_chat_context()
            sid = _ensure_session()
            existing = sessions.list_messages(sid)
            if not existing:
                title = text.replace("[Context:", "").strip()[:40] or "新会话"
                sessions.rename_session(sid, title)

            sessions.append_message(sid, "user", text)
            state["stick"] = True
            _refresh_sessions()
            with messages_box:
                ui.label(text).classes("chat-bubble user")
                bubble = ui.label("…").classes("chat-bubble assistant")
            state["bubble"] = bubble
            state["acc"] = ""
            state["final"] = ""
            state["error"] = ""
            state["done"] = False
            state["last_painted"] = ""
            state["stop"] = threading.Event()
            _set_sending(True)
            _scroll_bottom()

            history = _history_for_api(sessions, sid)

            def worker() -> None:
                def on_chunk(piece: str) -> None:
                    state["acc"] = (state.get("acc") or "") + piece

                try:
                    answer = run_chat(
                        history,
                        tools,
                        on_chunk=on_chunk,
                        stop_event=state["stop"],
                    )
                    state["final"] = answer or state.get("acc") or ""
                except Exception:
                    log.warning("chat run failed")
                    state["error"] = "问答失败"
                    state["final"] = state.get("acc") or "问答失败"
                state["done"] = True

            threading.Thread(target=worker, daemon=True).start()

        send_btn.on_click(_on_send_or_stop)
        composer.on("keydown.ctrl.enter", lambda _e: _on_send_or_stop())
        search.on("update:model-value", lambda _e: _refresh_sessions())

        prefix = _context_prefix()
        if prefix:
            composer.value = prefix

        items = sessions.list_sessions()
        if items:
            state["session_id"] = items[0].id
        else:
            info = sessions.create_session()
            state["session_id"] = info.id
        _refresh_sessions()
        _render_history()


def _toggle_sidebar(sidebar_el, state: dict[str, Any]) -> None:
    state["sidebar"] = not state.get("sidebar", True)
    if state["sidebar"]:
        sidebar_el.classes(remove="collapsed")
    else:
        sidebar_el.classes(add="collapsed")


def _history_for_api(sessions: SessionStore, session_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in sessions.list_messages(session_id):
        if msg.role in {"user", "assistant"} and msg.content:
            out.append({"role": msg.role, "content": msg.content})
    return out[-24:]
