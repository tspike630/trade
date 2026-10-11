"""M6 AI chat page: session history in dashboard_state.db, tools over DataStore."""

from __future__ import annotations

from dashboard.chat.page import render_chat_page

def render(store=None, **kwargs):
    """Shell AI CHAT tab entry; ignore store (chat uses its own sessions)."""
    return render_chat_page(**kwargs)

from dashboard.chat.tools import EMPTY_DATA, TOOL_NAMES, build_tools

__all__ = ["render", "EMPTY_DATA", "TOOL_NAMES", "build_tools", "render_chat_page"]
