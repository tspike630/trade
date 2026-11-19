"""Errors for the cross-margin track. No network I/O."""

from __future__ import annotations


class LiveNetworkDisabled(RuntimeError):
    """Live margin HTTP transport is locked by approval, enable, or kill-switch gates."""


class MarginTransportTimeout(Exception):
    """Injected or live transport timeout. Not proof the write was lost."""

    def __init__(self, method: str) -> None:
        super().__init__(method)
        self.method = method


class MarginExchangeError(Exception):
    """Exchange-shaped error. ``code`` is the Binance code when one is known."""

    def __init__(
        self,
        code: int,
        message: str,
        *,
        http_status: int | None = None,
        method: str = "",
    ) -> None:
        super().__init__(f"{code} {message}")
        self.code = code
        self.message = message
        self.http_status = http_status
        self.method = method

    @property
    def is_indeterminate(self) -> bool:
        """HTTP 5xx, -1001/-1007, or -1000 malformed response means outcome on Binance is unknown."""
        if self.http_status is not None and self.http_status >= 500:
            return True
        return self.code in {-1001, -1007, -1000}

    @property
    def is_rate_limited(self) -> bool:
        """HTTP 429/418 or -1003 means request was throttled or IP-banned."""
        if self.http_status in {418, 429}:
            return True
        return self.code == -1003

    @property
    def is_deterministic_pre_exec_oco_reject(self) -> bool:
        """Integer codes do not prove a margin OCO failed before it landed.

        ``-2010`` is reused for filter or balance text and for duplicate
        client-order text. A duplicate can mean the list already exists, and a
        momentarily empty ``openOrders`` read does not prove otherwise. This
        build has no margin-protocol evidence that any code plus a clean locked
        balance is a completed rejection, so the automatic flatten path stays
        closed. Returning false is that closure, not a claim about the exchange.
        """
        return False
