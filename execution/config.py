"""Execution-layer switches.

Risk skeleton (plan 05) defaults off. Paper / PM gates stay in their own env
flags and are not changed here.
"""

from __future__ import annotations

import os

# 执行层风控骨架（方案5；默认关）
EXECUTION_RISK_ENABLED = os.getenv("EXECUTION_RISK_ENABLED", "0") == "1"
EXECUTION_MAX_OPEN = int(os.getenv("EXECUTION_MAX_OPEN", "5"))

# 订单事件流（方案5-2a；默认开——纯审计增强、零行为副作用，可关）
EXECUTION_EVENTS_ENABLED = os.getenv("EXECUTION_EVENTS_ENABLED", "1") == "1"
