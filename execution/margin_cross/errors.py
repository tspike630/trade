"""Errors for the cross-margin track. No network I/O."""

from __future__ import annotations

# Explicit pre-execution parameter, filter, or balance rejection codes on POST /sapi/v1/margin/order/oco.
# Spot-style list lookup codes (-2018 "Order list does not exist.", -2011, -2013), rate limits (-1003),
# clock skew (-1021), and malformed responses (-1000) are NEVER treated as proven OCO rejections.
_DETERMINISTIC_PRE_EXEC_OCO_REJECT_CODES = frozenset(
    {-1013, -1100, -1102, -1111, -1116, -1117, -1121, -2010, -3006, -3045}
)


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
        """True strictly for confirmed pre-execution filter/balance rejects on new_oco.

        HTTP 400 / -2018 ('Order list does not exist.') and any unrecognized or
        ambiguous margin OCO error return False so the track holds in
        ``protection_unknown`` instead of triggering an unverified buyback.
        """
        if self.is_indeterminate or self.is_rate_limited:
            return False
        if self.http_status not in {None, 400}:
            return False
        return self.code in _DETERMINISTIC_PRE_EXEC_OCO_REJECT_CODES
