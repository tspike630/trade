#!/usr/bin/env python3
"""Patch the production engine blob so only the news isolation hook is added.

The input must be the bc392fea engine (sha256 612bd80a..., 18185 bytes).
The output is not origin/main's engine and does not call the five-state router.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ENGINE_BEFORE = "612bd80af39ca2f6061229a67b39a14cd71cb91661947d8936c48bb101dd9d1f"
ENGINE_BEFORE_BYTES = 18185

_IMPORT_OLD = "from decision_engine.core.live_buy_guards import apply_live_buy_guards\n"
_IMPORT_NEW = (
    _IMPORT_OLD
    + "from decision_engine.core.news_shock_gate import apply_news_isolation\n"
)

_PUBLISH_OLD = """        if bnb_spot:
            passed, top_report = cap_live_bnb_buys(passed, now_ms=now_ms)
        emitted = self.bus.publish_many(passed)
"""

_PUBLISH_NEW = """        if bnb_spot:
            passed, top_report = cap_live_bnb_buys(passed, now_ms=now_ms)
        news_report: dict[str, Any] | None = None
        if bnb_spot:
            try:
                passed, news_report = apply_news_isolation(passed)
            except Exception as exc:  # noqa: BLE001
                log.exception("news isolation failed")
                news_report = {
                    "mode": "isolation",
                    "availability": "error",
                    "detail": f"{type(exc).__name__}:{exc}",
                    "fail_open": True,
                    "candidates_changed": False,
                    "router_called": False,
                }
        emitted = self.bus.publish_many(passed)
"""

_PAYLOAD_ARG_OLD = """        stop_gap_rejected: int = 0,
    ) -> dict[str, Any]:
"""

_PAYLOAD_ARG_NEW = """        stop_gap_rejected: int = 0,
        news_gate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
"""

_PAYLOAD_BODY_OLD = """        if stop_gap_rejected:
            payload["stop_gap_rejected"] = stop_gap_rejected
        return payload
"""

_PAYLOAD_BODY_NEW = """        if stop_gap_rejected:
            payload["stop_gap_rejected"] = stop_gap_rejected
        if news_gate is not None:
            payload["news_gate"] = news_gate
        return payload
"""

_RETURN_OLD = """            stop_gap_rejected=int(guard_report.get("stop_gap_rejected") or 0),
        )
"""

_RETURN_NEW = """            stop_gap_rejected=int(guard_report.get("stop_gap_rejected") or 0),
            news_gate=news_report,
        )
"""


def _once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label} matched {count} times")
    return text.replace(old, new, 1)


def patch_engine(source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if digest != ENGINE_BEFORE or len(source.encode("utf-8")) != ENGINE_BEFORE_BYTES:
        raise SystemExit(
            f"refusing to patch engine {digest} {len(source.encode('utf-8'))} bytes; "
            f"expected {ENGINE_BEFORE} {ENGINE_BEFORE_BYTES}"
        )
    text = source
    text = _once(text, _IMPORT_OLD, _IMPORT_NEW, "import")
    text = _once(text, _PUBLISH_OLD, _PUBLISH_NEW, "publish")
    text = _once(text, _PAYLOAD_ARG_OLD, _PAYLOAD_ARG_NEW, "payload arg")
    text = _once(text, _PAYLOAD_BODY_OLD, _PAYLOAD_BODY_NEW, "payload body")
    text = _once(text, _RETURN_OLD, _RETURN_NEW, "return")
    forbidden = (
        "apply_five_state_regime_router",
        "evaluate_macro_gate",
        "attach_cityu_institutional_context",
        "attach_counterparty_context",
        "drawdown_and_kelly_budget",
        "perp_microstructure_and_beta_hedge",
    )
    for name in forbidden:
        if name in text:
            raise SystemExit(f"patched engine still mentions {name}")
    if "apply_news_isolation" not in text:
        raise SystemExit("patched engine missed the isolation call")
    return text


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: build_news_isolation_engine.py SOURCE DEST", file=sys.stderr)
        return 1
    source = Path(argv[1]).read_text(encoding="utf-8")
    patched = patch_engine(source)
    Path(argv[2]).write_text(patched, encoding="utf-8")
    data = Path(argv[2]).read_bytes()
    print(hashlib.sha256(data).hexdigest(), len(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
