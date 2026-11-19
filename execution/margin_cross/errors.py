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
    ) -> None:
        super().__init__(f"{code} {message}")
        self.code = code
        self.message = message
        self.http_status = http_status

    @property
    def is_indeterminate(self) -> bool:
        """HTTP 5xx or -1001/-1007 means execution outcome on Binance is unknown."""
        if self.http_status is not None and self.http_status >= 500:
            return True
        return self.code in {-1001, -1007}

    @property
    def is_rate_limited(self) -> bool:
        """HTTP 429/418 or -1003 means request was throttled or IP-banned."""
        if self.http_status in {418, 429}:
            return True
        return self.code == -1003
