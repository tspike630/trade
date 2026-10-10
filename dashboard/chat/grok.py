"""Grok / xAI chat client for M6. Key from env only; never logged."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from dashboard.chat.tools import TOOL_SPECS, run_tool

log = logging.getLogger("dashboard.chat.grok")

SYSTEM_PROMPT = (
    "你是本地交易监控看板的只读助手。"
    "只能依据用户通过工具返回的本地数据回答；无数据时明确说「暂无数据」，不要编造数字。"
    "不得建议开闸、下单、改参数、开启实盘/模拟盘或任何交易操作。"
    "回答用中文，简洁条理；金额与地址按工具原文引用。"
)

DEFAULT_BASE = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-3-mini"
MAX_TOOL_ROUNDS = 4


def grok_api_key() -> str | None:
    for name in ("GROK_API_KEY", "XAI_API_KEY"):
        raw = os.environ.get(name, "").strip()
        if raw:
            return raw
    return None


def grok_configured() -> bool:
    return grok_api_key() is not None


def grok_base_url() -> str:
    return os.environ.get("GROK_API_BASE", DEFAULT_BASE).strip().rstrip("/") or DEFAULT_BASE


def grok_model() -> str:
    return os.environ.get("GROK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _headers() -> dict[str, str]:
    key = grok_api_key()
    if not key:
        raise RuntimeError("GROK_API_KEY 未配置")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _post_json(payload: dict[str, Any], *, timeout: float = 90.0) -> dict[str, Any]:
    url = f"{grok_base_url()}/chat/completions"
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, headers=_headers(), json=payload)
        response.raise_for_status()
        return response.json()


def _iter_sse(payload: dict[str, Any], stop_event: threading.Event) -> Iterator[dict[str, Any]]:
    url = f"{grok_base_url()}/chat/completions"
    body = dict(payload)
    body["stream"] = True
    with httpx.Client(timeout=httpx.Timeout(90.0, connect=15.0)) as client:
        with client.stream("POST", url, headers=_headers(), json=body) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if stop_event.is_set():
                    break
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue


def _assistant_message(choice_message: dict[str, Any]) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant"}
    content = choice_message.get("content")
    if content:
        msg["content"] = content
    tool_calls = choice_message.get("tool_calls")
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def run_chat(
    history: list[dict[str, Any]],
    tools: dict[str, Callable[..., str]],
    *,
    on_chunk: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
) -> str:
    """Tool-calling loop then streamed final answer. Returns full assistant text."""
    if not grok_configured():
        return "未配置"

    stop = stop_event or threading.Event()
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}, *history]

    for _round in range(MAX_TOOL_ROUNDS):
        if stop.is_set():
            return ""
        payload = {
            "model": grok_model(),
            "messages": messages,
            "tools": TOOL_SPECS,
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        try:
            data = _post_json(payload)
        except httpx.HTTPStatusError as exc:
            log.warning("grok http error status=%s", exc.response.status_code)
            return "模型请求失败，请稍后重试"
        except Exception:
            log.warning("grok request failed")
            return "模型请求失败，请稍后重试"

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            existing = (message.get("content") or "").strip()
            if existing:
                if on_chunk is not None:
                    on_chunk(existing)
                return existing
            return _stream_final(messages, on_chunk=on_chunk, stop_event=stop)

        messages.append(_assistant_message(message))
        for call in tool_calls:
            if stop.is_set():
                return ""
            fn = call.get("function") or {}
            name = str(fn.get("name") or "")
            raw_args = fn.get("arguments") or "{}"
            result = run_tool(tools, name, raw_args)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or name,
                    "content": result,
                }
            )

    return _stream_final(messages, on_chunk=on_chunk, stop_event=stop)


def _stream_final(
    messages: list[dict[str, Any]],
    *,
    on_chunk: Callable[[str], None] | None,
    stop_event: threading.Event,
) -> str:
    payload = {
        "model": grok_model(),
        "messages": messages,
        "temperature": 0.2,
    }
    if on_chunk is not None:
        acc = ""
        try:
            for event in _iter_sse(payload, stop_event):
                if stop_event.is_set():
                    break
                delta = ((event.get("choices") or [{}])[0].get("delta") or {})
                piece = delta.get("content") or ""
                if not piece:
                    continue
                acc += piece
                on_chunk(piece)
        except httpx.HTTPStatusError as exc:
            log.warning("grok stream http error status=%s", exc.response.status_code)
            if not acc:
                return "模型请求失败，请稍后重试"
        except Exception:
            log.warning("grok stream failed")
            if not acc:
                return "模型请求失败，请稍后重试"
        return acc

    try:
        data = _post_json(payload)
    except Exception:
        log.warning("grok final request failed")
        return "模型请求失败，请稍后重试"
    choice = (data.get("choices") or [{}])[0]
    return str((choice.get("message") or {}).get("content") or "")
