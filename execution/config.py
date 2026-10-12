"""Execution-layer switches.

Risk skeleton (plan 05) defaults off. Paper / PM gates stay in their own env
flags and are not changed here.
"""

from __future__ import annotations

import os

# 执行层风控骨架（方案5；默认关）
EXECUTION_RISK_ENABLED = os.getenv("EXECUTION_RISK_ENABLED", "0") == "1"
EXECUTION_MAX_OPEN = int(os.getenv("EXECUTION_MAX_OPEN", "5"))
