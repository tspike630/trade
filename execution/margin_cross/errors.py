"""Errors for the cross-margin track. No network I/O."""

from __future__ import annotations


class LiveNetworkDisabled(RuntimeError):
    """This build has no margin HTTP transport and will not open one."""


class MarginTransportTimeout(Exception):
    """Injected or future transport timeout. Not proof the write was lost."""

    def __init__(self, method: str) -> None:
        super().__init__(method)
        self.method = method


class MarginExchangeError(Exception):
    """Exchange-shaped reject. ``code`` is the Binance code when one is known."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code} {message}")
        self.code = code
        self.message = message
