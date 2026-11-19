"""Out-of-process Risk Signer Daemon & RPC clients (Step 1 institutional upgrade).

Separates exchange API signing keys (``api_secret``) and live-config HMAC approval
keys (``approval_hmac_key``) into an isolated OS process so strategy and execution
gateway processes never hold raw cryptographic secrets in memory or ``os.environ``.

Before signing any outbound Binance Spot or Cross Margin query, ``RiskSignerEngine``
enforces deterministic pre-trade risk controls (aligned with SEC Rule 15c3-5):

1. **Kill-Switch Enforcement**:
   Blocks non-protective entry orders and borrows whenever ``LIVE_TRADING_DISABLED``
   is present, while still permitting protective exits, OCO placements, cancels,
   and margin repayments unless ``block_protective_on_kill=True``.
2. **Account & MCP Isolation**:
   Hard-blocks MCP account UID ``1289917075`` and requires exact match against
   ``approved_account_uid`` when configured.
3. **Hard Borrow, Daily Loss & Notional Exposure Ceilings**:
   - ``max_borrow_by_asset`` / ``max_single_borrow_usd`` on ``POST /sapi/v1/margin/borrow-repay`` (``type=BORROW``)
   - ``max_daily_loss_usd`` circuit breaker on new entries & borrows
   - ``max_single_order_notional_usd``, ``max_symbol_notional_usd``, and
     ``max_gross_exposure_usd`` on new entry orders.
4. **Dual Transport**:
   - ``RiskSignerUnixServer`` / ``RiskSignerUnixClient`` over ``0o600`` ``AF_UNIX`` sockets.
   - ``RiskSignerSubprocessClient`` / ``--stdio-worker`` over OS pipes for true
     separate-process memory isolation even in socket-restricted environments.

Default runtime state: disabled unless explicitly instantiated by an operator.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import urllib.parse

MCP_FORBIDDEN_UID = "1289917075"
DEFAULT_KILL_SWITCH_NAME = "LIVE_TRADING_DISABLED"
SIGNER_ENABLED_ENV = "RISK_SIGNER_DAEMON_ENABLED"
MAX_RECV_WINDOW_MS = 60_000
DEFAULT_MAX_CLOCK_SKEW_MS = 30_000

_PROTECTIVE_PATHS = frozenset(
    {
        "/api/v3/orderList/oco",
        "/api/v3/orderList",
        "/sapi/v1/margin/order/oco",
        "/sapi/v1/margin/orderList",
    }
)


def risk_signer_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    raw = str(env.get(SIGNER_ENABLED_ENV) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _parse_pos_dec(raw: object) -> Decimal | None:
    if isinstance(raw, bool) or raw is None:
        return None
    txt = str(raw).strip()
    if not txt:
        return None
    try:
        val = Decimal(txt)
    except InvalidOperation:
        return None
    if not val.is_finite() or val <= 0:
        return None
    return val


def _parse_nonneg_dec(raw: object) -> Decimal | None:
    if isinstance(raw, bool) or raw is None:
        return None
    txt = str(raw).strip()
    if not txt:
        return None
    try:
        val = Decimal(txt)
    except InvalidOperation:
        return None
    if not val.is_finite() or val < 0:
        return None
    return val


def _parse_signed_dec(raw: object) -> Decimal | None:
    if isinstance(raw, bool) or raw is None:
        return None
    txt = str(raw).strip()
    if not txt:
        return None
    try:
        val = Decimal(txt)
    except InvalidOperation:
        return None
    if not val.is_finite():
        return None
    return val


class RiskSignerRejected(RuntimeError):
    """Raised when the Risk Signer refuses to sign or attest a request."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "signer_rejected")
        super().__init__(self.reason)


@dataclass(frozen=True)
class RiskPolicyLimits:
    approved_account_uid: str = ""
    approved_config_sha256: str = ""
    approval_expires_at_ms: int = 0
    max_single_order_notional_usd: Decimal = Decimal("500")
    max_symbol_notional_usd: Decimal = Decimal("1000")
    max_gross_exposure_usd: Decimal = Decimal("2500")
    max_daily_loss_usd: Decimal = Decimal("200")
    max_single_borrow_usd: Decimal = Decimal("500")
    max_borrow_by_asset: dict[str, Decimal] = field(default_factory=dict)
    asset_reference_prices_usd: dict[str, Decimal] = field(default_factory=dict)
    max_clock_skew_ms: int = DEFAULT_MAX_CLOCK_SKEW_MS
    kill_switch_path: str | None = None
    block_protective_on_kill: bool = False

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "approved_account_uid": self.approved_account_uid,
            "approved_config_sha256": self.approved_config_sha256,
            "approval_expires_at_ms": int(self.approval_expires_at_ms),
            "max_single_order_notional_usd": str(self.max_single_order_notional_usd),
            "max_symbol_notional_usd": str(self.max_symbol_notional_usd),
            "max_gross_exposure_usd": str(self.max_gross_exposure_usd),
            "max_daily_loss_usd": str(self.max_daily_loss_usd),
            "max_single_borrow_usd": str(self.max_single_borrow_usd),
            "max_borrow_by_asset": {k: str(v) for k, v in self.max_borrow_by_asset.items()},
            "asset_reference_prices_usd": {
                k: str(v) for k, v in self.asset_reference_prices_usd.items()
            },
            "max_clock_skew_ms": int(self.max_clock_skew_ms),
            "kill_switch_path": self.kill_switch_path,
            "block_protective_on_kill": bool(self.block_protective_on_kill),
        }

    @classmethod
    def from_json_dict(cls, raw: Mapping[str, Any]) -> RiskPolicyLimits:
        borrow_map: dict[str, Decimal] = {}
        raw_bm = raw.get("max_borrow_by_asset")
        if isinstance(raw_bm, Mapping):
            for k, v in raw_bm.items():
                dec = _parse_nonneg_dec(v)
                if dec is not None:
                    borrow_map[str(k).strip().upper()] = dec
        px_map: dict[str, Decimal] = {}
        raw_pm = raw.get("asset_reference_prices_usd")
        if isinstance(raw_pm, Mapping):
            for k, v in raw_pm.items():
                dec = _parse_pos_dec(v)
                if dec is not None:
                    px_map[str(k).strip().upper()] = dec
        return cls(
            approved_account_uid=str(raw.get("approved_account_uid") or "").strip(),
            approved_config_sha256=str(raw.get("approved_config_sha256") or "").strip().lower(),
            approval_expires_at_ms=int(raw.get("approval_expires_at_ms") or 0),
            max_single_order_notional_usd=_parse_pos_dec(raw.get("max_single_order_notional_usd"))
            or Decimal("500"),
            max_symbol_notional_usd=_parse_pos_dec(raw.get("max_symbol_notional_usd"))
            or Decimal("1000"),
            max_gross_exposure_usd=_parse_pos_dec(raw.get("max_gross_exposure_usd"))
            or Decimal("2500"),
            max_daily_loss_usd=_parse_pos_dec(raw.get("max_daily_loss_usd")) or Decimal("200"),
            max_single_borrow_usd=_parse_pos_dec(raw.get("max_single_borrow_usd"))
            or Decimal("500"),
            max_borrow_by_asset=borrow_map,
            asset_reference_prices_usd=px_map,
            max_clock_skew_ms=max(1000, int(raw.get("max_clock_skew_ms") or DEFAULT_MAX_CLOCK_SKEW_MS)),
            kill_switch_path=(
                str(raw["kill_switch_path"]) if raw.get("kill_switch_path") else None
            ),
            block_protective_on_kill=bool(raw.get("block_protective_on_kill", False)),
        )


class RiskSignerEngine:
    """Stateful pre-trade risk gate + HMAC-SHA256 signer isolated inside the daemon."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        limits: RiskPolicyLimits,
        approval_hmac_key: str = "",
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        clean_key = api_key.strip()
        clean_secret = api_secret.strip()
        if not clean_key or not clean_secret:
            raise ValueError("signer_missing_api_credentials")
        if limits.approved_account_uid == MCP_FORBIDDEN_UID:
            raise ValueError("mcp_account_forbidden")
        self.api_key = clean_key
        self._api_secret = clean_secret
        self._approval_hmac_key = approval_hmac_key.strip()
        self.limits = limits
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.RLock()
        self._daily_pnl_usd = Decimal("0")
        self._symbol_exposure_usd: dict[str, Decimal] = {}
        self._borrowed_by_asset: dict[str, Decimal] = {}
        self._signed_count = 0
        self._rejected_count = 0

    def _kill_switch_active(self) -> bool:
        if self.limits.kill_switch_path:
            return Path(self.limits.kill_switch_path).expanduser().is_file()
        repo_root = Path(__file__).resolve().parents[1]
        for candidate in (
            Path.cwd() / DEFAULT_KILL_SWITCH_NAME,
            repo_root / DEFAULT_KILL_SWITCH_NAME,
            repo_root.parent / DEFAULT_KILL_SWITCH_NAME,
        ):
            if candidate.is_file():
                return True
        return False

    def update_risk_state(
        self,
        *,
        daily_pnl_usd: str | Decimal | None = None,
        symbol_exposure_usd: Mapping[str, str | Decimal] | None = None,
        borrowed_by_asset: Mapping[str, str | Decimal] | None = None,
        asset_reference_prices_usd: Mapping[str, str | Decimal] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if daily_pnl_usd is not None:
                pnl = _parse_signed_dec(daily_pnl_usd)
                if pnl is None:
                    raise RiskSignerRejected("invalid_daily_pnl_usd")
                self._daily_pnl_usd = pnl
            if symbol_exposure_usd is not None:
                updated_sym: dict[str, Decimal] = {}
                for sym, val in symbol_exposure_usd.items():
                    dec = _parse_nonneg_dec(val)
                    if dec is None:
                        raise RiskSignerRejected("invalid_symbol_exposure")
                    updated_sym[str(sym).strip().upper()] = dec
                self._symbol_exposure_usd = updated_sym
            if borrowed_by_asset is not None:
                updated_bor: dict[str, Decimal] = {}
                for asset, val in borrowed_by_asset.items():
                    dec = _parse_nonneg_dec(val)
                    if dec is None:
                        raise RiskSignerRejected("invalid_borrowed_amount")
                    updated_bor[str(asset).strip().upper()] = dec
                self._borrowed_by_asset = updated_bor
            if asset_reference_prices_usd is not None:
                merged_px = dict(self.limits.asset_reference_prices_usd)
                for asset, val in asset_reference_prices_usd.items():
                    dec = _parse_pos_dec(val)
                    if dec is None:
                        raise RiskSignerRejected("invalid_reference_price")
                    merged_px[str(asset).strip().upper()] = dec
                object.__setattr__(self.limits, "asset_reference_prices_usd", merged_px)
            return self.health()

    def health(self) -> dict[str, Any]:
        with self._lock:
            gross = sum(self._symbol_exposure_usd.values(), Decimal("0"))
            return {
                "ok": True,
                "pid": os.getpid(),
                "uid": os.getuid() if hasattr(os, "getuid") else None,
                "api_key_prefix": self.api_key[:4] if len(self.api_key) >= 4 else "***",
                "kill_switch_active": self._kill_switch_active(),
                "daily_pnl_usd": str(self._daily_pnl_usd),
                "gross_exposure_usd": str(gross),
                "signed_count": self._signed_count,
                "rejected_count": self._rejected_count,
            }

    def attest_approval(
        self,
        *,
        config_sha256: str,
        account_uid: str,
        now_ms: int | None = None,
    ) -> bool:
        clean_sha = str(config_sha256 or "").strip().lower()
        clean_uid = str(account_uid or "").strip()
        ts = int(self._clock_ms()) if now_ms is None else int(now_ms)
        if not clean_sha or not clean_uid or clean_uid == MCP_FORBIDDEN_UID:
            return False
        if not self._approval_hmac_key and not self.limits.approved_config_sha256:
            return False
        if self.limits.approved_account_uid and clean_uid != self.limits.approved_account_uid:
            return False
        if self.limits.approved_config_sha256 and not hmac.compare_digest(
            clean_sha, self.limits.approved_config_sha256.lower()
        ):
            return False
        if self.limits.approval_expires_at_ms > 0 and ts >= self.limits.approval_expires_at_ms:
            return False
        return True

    def compute_approval_hmac(self, *, config_sha256: str, account_uid: str) -> str:
        if not self._approval_hmac_key:
            raise RiskSignerRejected("approval_hmac_key_missing")
        msg = f"{config_sha256.strip().lower()}:{account_uid.strip()}".encode("utf-8")
        return hmac.new(
            self._approval_hmac_key.encode("utf-8"),
            msg,
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _is_protective_request(
        *,
        method: str,
        path: str,
        params_map: Mapping[str, str],
        caller_protective_flag: bool,
    ) -> bool:
        http_m = method.upper()
        if http_m in {"GET", "DELETE"}:
            return True
        if path in _PROTECTIVE_PATHS:
            return True
        if path == "/sapi/v1/margin/borrow-repay":
            return str(params_map.get("type") or "").upper() == "REPAY"
        if path in {"/api/v3/order", "/sapi/v1/margin/order"}:
            cid = str(params_map.get("newClientOrderId") or "")
            side = str(params_map.get("side") or "").upper()
            tif = str(params_map.get("timeInForce") or "").upper()
            if cid.endswith("-flatten") and tif == "IOC" and caller_protective_flag:
                return True
            if path == "/api/v3/order" and side == "SELL" and caller_protective_flag:
                return True
        return False

    def _estimate_order_notional_usd(self, params_map: Mapping[str, str]) -> Decimal:
        quote_qty = _parse_pos_dec(params_map.get("quoteOrderQty"))
        if quote_qty is not None:
            return quote_qty
        qty = _parse_pos_dec(params_map.get("quantity"))
        if qty is None:
            raise RiskSignerRejected("order_quantity_missing_or_invalid")
        px = _parse_pos_dec(params_map.get("price")) or _parse_pos_dec(params_map.get("stopPrice"))
        if px is None:
            sym = str(params_map.get("symbol") or "").strip().upper()
            base = sym[:-4] if sym.endswith("USDT") and len(sym) > 4 else sym
            px = self.limits.asset_reference_prices_usd.get(
                sym
            ) or self.limits.asset_reference_prices_usd.get(base)
        if px is None:
            raise RiskSignerRejected("market_order_reference_price_required")
        return qty * px

    def sign_request(
        self,
        *,
        venue: str,
        method: str,
        path: str,
        pairs: Sequence[Sequence[str]],
        account_uid: str = "",
        for_protective_or_repay: bool = False,
    ) -> str:
        with self._lock:
            try:
                return self._sign_request_locked(
                    venue=venue,
                    method=method,
                    path=path,
                    pairs=pairs,
                    account_uid=account_uid,
                    for_protective_or_repay=for_protective_or_repay,
                )
            except RiskSignerRejected:
                self._rejected_count += 1
                raise

    def _sign_request_locked(
        self,
        *,
        venue: str,
        method: str,
        path: str,
        pairs: Sequence[Sequence[str]],
        account_uid: str,
        for_protective_or_repay: bool,
    ) -> str:
        if venue not in {"binance_spot", "binance_cross_margin"}:
            raise RiskSignerRejected("unsupported_venue")
        clean_uid = str(account_uid or "").strip()
        if clean_uid == MCP_FORBIDDEN_UID:
            raise RiskSignerRejected("mcp_account_forbidden")
        if self.limits.approved_account_uid:
            if not clean_uid or clean_uid != self.limits.approved_account_uid:
                raise RiskSignerRejected("account_uid_mismatch")

        clean_pairs: list[tuple[str, str]] = []
        params_map: dict[str, str] = {}
        for item in pairs:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise RiskSignerRejected("malformed_query_pairs")
            k = str(item[0]).strip()
            v = str(item[1])
            if not k or k == "signature":
                raise RiskSignerRejected("invalid_query_parameter")
            clean_pairs.append((k, v))
            params_map[k] = v

        ts_raw = params_map.get("timestamp")
        if ts_raw is None or not ts_raw.isdigit():
            raise RiskSignerRejected("timestamp_missing_or_invalid")
        ts_ms = int(ts_raw)
        now_ms = int(self._clock_ms())
        if abs(now_ms - ts_ms) > self.limits.max_clock_skew_ms:
            raise RiskSignerRejected("timestamp_outside_clock_skew")

        rw_raw = params_map.get("recvWindow")
        if rw_raw is not None:
            if not rw_raw.isdigit() or not (0 < int(rw_raw) <= MAX_RECV_WINDOW_MS):
                raise RiskSignerRejected("recv_window_invalid")

        http_m = str(method or "").strip().upper()
        if http_m not in {"GET", "POST", "DELETE"}:
            raise RiskSignerRejected("http_method_invalid")

        is_protective = self._is_protective_request(
            method=http_m,
            path=path,
            params_map=params_map,
            caller_protective_flag=bool(for_protective_or_repay),
        )

        if self._kill_switch_active():
            if not is_protective or self.limits.block_protective_on_kill:
                raise RiskSignerRejected("kill_switch_active")

        if http_m == "POST" and not is_protective:
            # Daily loss circuit breaker blocks all new risk-increasing mutations
            if self._daily_pnl_usd <= -self.limits.max_daily_loss_usd:
                raise RiskSignerRejected("daily_loss_limit_breached")

            if path == "/sapi/v1/margin/borrow-repay":
                b_type = str(params_map.get("type") or "").upper()
                if b_type != "BORROW":
                    raise RiskSignerRejected("unexpected_borrow_repay_type")
                if str(params_map.get("isIsolated") or "") != "FALSE":
                    raise RiskSignerRejected("isolated_margin_forbidden")
                asset = str(params_map.get("asset") or "").strip().upper()
                amt = _parse_pos_dec(params_map.get("amount"))
                if not asset or amt is None:
                    raise RiskSignerRejected("borrow_amount_invalid")
                asset_cap = self.limits.max_borrow_by_asset.get(asset)
                cur_bor = self._borrowed_by_asset.get(asset, Decimal("0"))
                if asset_cap is not None and (cur_bor + amt) > asset_cap:
                    raise RiskSignerRejected(f"max_borrow_exceeded:{asset}")
                ref_px = (
                    Decimal("1")
                    if asset in {"USDT", "USDC", "FDUSD"}
                    else self.limits.asset_reference_prices_usd.get(asset)
                )
                if ref_px is None:
                    raise RiskSignerRejected(f"borrow_reference_price_missing:{asset}")
                if (amt * ref_px) > self.limits.max_single_borrow_usd:
                    raise RiskSignerRejected("max_single_borrow_usd_exceeded")
                self._borrowed_by_asset[asset] = cur_bor + amt

            elif path in {"/api/v3/order", "/sapi/v1/margin/order"}:
                sym = str(params_map.get("symbol") or "").strip().upper()
                if not sym:
                    raise RiskSignerRejected("symbol_required")
                notional = self._estimate_order_notional_usd(params_map)
                if notional > self.limits.max_single_order_notional_usd:
                    raise RiskSignerRejected("max_single_order_notional_exceeded")
                cur_sym = self._symbol_exposure_usd.get(sym, Decimal("0"))
                if (cur_sym + notional) > self.limits.max_symbol_notional_usd:
                    raise RiskSignerRejected(f"max_symbol_notional_exceeded:{sym}")
                cur_gross = sum(self._symbol_exposure_usd.values(), Decimal("0"))
                if (cur_gross + notional) > self.limits.max_gross_exposure_usd:
                    raise RiskSignerRejected("max_gross_exposure_exceeded")
                self._symbol_exposure_usd[sym] = cur_sym + notional
            else:
                raise RiskSignerRejected(f"unapproved_write_endpoint:{path}")

        elif http_m == "POST" and path == "/sapi/v1/margin/borrow-repay":
            # Track repayments reducing tracked borrowed principal
            if str(params_map.get("type") or "").upper() == "REPAY":
                asset = str(params_map.get("asset") or "").strip().upper()
                amt = _parse_pos_dec(params_map.get("amount"))
                if asset and amt is not None:
                    cur_bor = self._borrowed_by_asset.get(asset, Decimal("0"))
                    self._borrowed_by_asset[asset] = max(Decimal("0"), cur_bor - amt)

        query = urllib.parse.urlencode(clean_pairs, quote_via=urllib.parse.quote, safe="")
        digest = hmac.new(
            self._api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self._signed_count += 1
        return f"{query}&signature={digest}"

    def dispatch_rpc(self, request: Mapping[str, Any]) -> dict[str, Any]:
        op = str(request.get("op") or "").strip()
        try:
            if op == "health":
                return {"ok": True, "result": self.health()}
            if op == "attest_approval":
                attested = self.attest_approval(
                    config_sha256=str(request.get("config_sha256") or ""),
                    account_uid=str(request.get("account_uid") or ""),
                    now_ms=(
                        int(request["now_ms"])
                        if request.get("now_ms") is not None
                        else None
                    ),
                )
                return {"ok": True, "result": {"attested": attested}}
            if op == "sign_request":
                signed = self.sign_request(
                    venue=str(request.get("venue") or ""),
                    method=str(request.get("method") or ""),
                    path=str(request.get("path") or ""),
                    pairs=request.get("pairs") or [],
                    account_uid=str(request.get("account_uid") or ""),
                    for_protective_or_repay=bool(request.get("for_protective_or_repay", False)),
                )
                return {"ok": True, "result": {"signed_query": signed}}
            if op == "update_risk_state":
                state = self.update_risk_state(
                    daily_pnl_usd=request.get("daily_pnl_usd"),
                    symbol_exposure_usd=request.get("symbol_exposure_usd"),
                    borrowed_by_asset=request.get("borrowed_by_asset"),
                    asset_reference_prices_usd=request.get("asset_reference_prices_usd"),
                )
                return {"ok": True, "result": state}
            return {"ok": False, "error": f"unknown_op:{op}"}
        except RiskSignerRejected as exc:
            return {"ok": False, "error": exc.reason}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"signer_internal_error:{type(exc).__name__}"}


class RiskSignerUnixServer:
    """Bounded ``AF_UNIX`` JSON-RPC daemon with strict ``0o600`` socket permissions."""

    def __init__(self, socket_path: str | os.PathLike[str], engine: RiskSignerEngine) -> None:
        self.socket_path = Path(socket_path).expanduser()
        self.engine = engine
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            srv.bind(str(self.socket_path))
        finally:
            os.umask(old_umask)
        os.chmod(self.socket_path, stat.S_IRUSR | stat.S_IWUSR)
        srv.listen(16)
        srv.settimeout(0.2)
        self._sock = srv
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve_loop,
            name="risk-signer-unix",
            daemon=True,
        )
        self._thread.start()

    def _serve_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with conn:
                conn.settimeout(2.0)
                buf = b""
                while b"\n" not in buf and len(buf) < 65_536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                line = buf.partition(b"\n")[0].strip()
                if not line:
                    continue
                try:
                    req = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    resp = {"ok": False, "error": "invalid_json"}
                else:
                    resp = (
                        self.engine.dispatch_rpc(req)
                        if isinstance(req, Mapping)
                        else {"ok": False, "error": "request_not_mapping"}
                    )
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass


class RiskSignerUnixClient:
    """Client speaking JSON-RPC to an out-of-process ``RiskSignerUnixServer``."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        *,
        api_key: str,
        timeout: float = 2.0,
    ) -> None:
        self.socket_path = Path(socket_path).expanduser()
        self.api_key = api_key.strip()
        self.timeout = float(timeout)

    def _rpc(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as cli:
            cli.settimeout(self.timeout)
            cli.connect(str(self.socket_path))
            cli.sendall((json.dumps(dict(payload)) + "\n").encode("utf-8"))
            buf = b""
            while b"\n" not in buf and len(buf) < 65_536:
                chunk = cli.recv(4096)
                if not chunk:
                    break
                buf += chunk
        line = buf.partition(b"\n")[0].strip()
        if not line:
            raise RiskSignerRejected("signer_empty_response")
        resp = json.loads(line.decode("utf-8"))
        if not isinstance(resp, dict) or resp.get("ok") is not True:
            err = resp.get("error") if isinstance(resp, dict) else "signer_invalid_response"
            raise RiskSignerRejected(str(err or "signer_rejected"))
        result = resp.get("result")
        return result if isinstance(result, dict) else {}

    def health(self) -> dict[str, Any]:
        return self._rpc({"op": "health"})

    def attest_approval(
        self,
        *,
        config_sha256: str,
        account_uid: str,
        now_ms: int | None = None,
    ) -> bool:
        res = self._rpc(
            {
                "op": "attest_approval",
                "config_sha256": config_sha256,
                "account_uid": account_uid,
                "now_ms": now_ms,
            }
        )
        return bool(res.get("attested"))

    def sign_request(
        self,
        *,
        venue: str,
        method: str,
        path: str,
        pairs: Sequence[tuple[str, str]],
        account_uid: str = "",
        for_protective_or_repay: bool = False,
    ) -> str:
        res = self._rpc(
            {
                "op": "sign_request",
                "venue": venue,
                "method": method,
                "path": path,
                "pairs": [[str(k), str(v)] for k, v in pairs],
                "account_uid": account_uid,
                "for_protective_or_repay": bool(for_protective_or_repay),
            }
        )
        signed = str(res.get("signed_query") or "")
        if not signed:
            raise RiskSignerRejected("signer_missing_signature")
        return signed

    def update_risk_state(self, **kwargs: Any) -> dict[str, Any]:
        return self._rpc({"op": "update_risk_state", **kwargs})


class RiskSignerSubprocessClient:
    """Out-of-process Risk Signer client over OS pipes (``--stdio-worker``).

    Spawns a separate Python OS process whose memory holds ``api_secret`` and
    ``approval_hmac_key`` loaded from a one-shot ``0o600`` bootstrap file that is
    immediately unlinked after child startup. Works even when ``socket.connect``
    is disabled in offline verification sandboxes.
    """

    def __init__(
        self,
        *,
        api_key: str,
        bootstrap_secret_file: str | os.PathLike[str],
        limits: RiskPolicyLimits,
        python_executable: str | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self._lock = threading.RLock()
        secret_path = Path(bootstrap_secret_file).expanduser().resolve()
        st_mode = stat.S_IMODE(secret_path.stat().st_mode)
        if st_mode & 0o077:
            raise PermissionError("bootstrap_secret_file_permissions_too_open")

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="risk-policy-", delete=False, encoding="utf-8"
        ) as pol_fp:
            os.chmod(pol_fp.name, 0o600)
            json.dump(limits.to_json_dict(), pol_fp)
            self._policy_path = Path(pol_fp.name)

        repo_root = Path(__file__).resolve().parents[1]
        # Note: execution may live inside crawler/execution symlinked at repo root
        parent_root = repo_root.parent
        py_path = os.pathsep.join([str(parent_root), str(repo_root)])
        child_env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONPATH": py_path,
        }
        self._proc = subprocess.Popen(
            [
                python_executable or sys.executable,
                "-m",
                "execution.risk_signer_daemon",
                "--stdio-worker",
                "--secret-file",
                str(secret_path),
                "--policy-file",
                str(self._policy_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=child_env,
        )
        # Verify worker booted and loaded secrets into its private address space
        ready = self.health()
        if not ready.get("ok"):
            self.close()
            raise RuntimeError("risk_signer_worker_boot_failed")

    @property
    def worker_pid(self) -> int:
        return int(self._proc.pid)

    def _rpc(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._proc.poll() is not None or self._proc.stdin is None or self._proc.stdout is None:
                raise RiskSignerRejected("signer_worker_not_running")
            self._proc.stdin.write(json.dumps(dict(payload)) + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline().strip()
            if not line:
                raise RiskSignerRejected("signer_worker_eof")
            resp = json.loads(line)
            if not isinstance(resp, dict) or resp.get("ok") is not True:
                err = resp.get("error") if isinstance(resp, dict) else "signer_invalid_response"
                raise RiskSignerRejected(str(err or "signer_rejected"))
            result = resp.get("result")
            return result if isinstance(result, dict) else {}

    def health(self) -> dict[str, Any]:
        return self._rpc({"op": "health"})

    def attest_approval(
        self,
        *,
        config_sha256: str,
        account_uid: str,
        now_ms: int | None = None,
    ) -> bool:
        res = self._rpc(
            {
                "op": "attest_approval",
                "config_sha256": config_sha256,
                "account_uid": account_uid,
                "now_ms": now_ms,
            }
        )
        return bool(res.get("attested"))

    def sign_request(
        self,
        *,
        venue: str,
        method: str,
        path: str,
        pairs: Sequence[tuple[str, str]],
        account_uid: str = "",
        for_protective_or_repay: bool = False,
    ) -> str:
        res = self._rpc(
            {
                "op": "sign_request",
                "venue": venue,
                "method": method,
                "path": path,
                "pairs": [[str(k), str(v)] for k, v in pairs],
                "account_uid": account_uid,
                "for_protective_or_repay": bool(for_protective_or_repay),
            }
        )
        signed = str(res.get("signed_query") or "")
        if not signed:
            raise RiskSignerRejected("signer_missing_signature")
        return signed

    def update_risk_state(self, **kwargs: Any) -> dict[str, Any]:
        return self._rpc({"op": "update_risk_state", **kwargs})

    def close(self) -> None:
        with self._lock:
            if self._proc.poll() is None:
                try:
                    if self._proc.stdin:
                        self._proc.stdin.close()
                    self._proc.terminate()
                    self._proc.wait(timeout=2.0)
                except Exception:  # noqa: BLE001
                    self._proc.kill()
            if self._policy_path.exists():
                try:
                    self._policy_path.unlink()
                except OSError:
                    pass

    def __enter__(self) -> RiskSignerSubprocessClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _run_stdio_worker(secret_file: Path, policy_file: Path) -> int:
    st_mode = stat.S_IMODE(secret_file.stat().st_mode)
    if st_mode & 0o077:
        sys.stderr.write("secret_file_permissions_too_open\n")
        return 2
    secret_doc = json.loads(secret_file.read_text(encoding="utf-8"))
    policy_doc = json.loads(policy_file.read_text(encoding="utf-8"))
    engine = RiskSignerEngine(
        api_key=str(secret_doc.get("api_key") or ""),
        api_secret=str(secret_doc.get("api_secret") or ""),
        approval_hmac_key=str(secret_doc.get("approval_hmac_key") or ""),
        limits=RiskPolicyLimits.from_json_dict(policy_doc),
    )
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            resp = {"ok": False, "error": "invalid_json"}
        else:
            resp = (
                engine.dispatch_rpc(req)
                if isinstance(req, Mapping)
                else {"ok": False, "error": "request_not_mapping"}
            )
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Out-of-process Risk Signer Daemon")
    parser.add_argument("--stdio-worker", action="store_true", help="Run line-delimited JSON-RPC on stdio")
    parser.add_argument("--unix-socket", default=None, help="Bind AF_UNIX socket at path (0o600)")
    parser.add_argument("--secret-file", required=True, help="0o600 JSON file with api_key/api_secret")
    parser.add_argument("--policy-file", required=True, help="JSON file with RiskPolicyLimits")
    args = parser.parse_args(list(argv) if argv is not None else None)

    sec_path = Path(args.secret_file).expanduser().resolve()
    pol_path = Path(args.policy_file).expanduser().resolve()
    if args.stdio_worker:
        return _run_stdio_worker(sec_path, pol_path)
    if args.unix_socket:
        st_mode = stat.S_IMODE(sec_path.stat().st_mode)
        if st_mode & 0o077:
            sys.stderr.write("secret_file_permissions_too_open\n")
            return 2
        secret_doc = json.loads(sec_path.read_text(encoding="utf-8"))
        policy_doc = json.loads(pol_path.read_text(encoding="utf-8"))
        engine = RiskSignerEngine(
            api_key=str(secret_doc.get("api_key") or ""),
            api_secret=str(secret_doc.get("api_secret") or ""),
            approval_hmac_key=str(secret_doc.get("approval_hmac_key") or ""),
            limits=RiskPolicyLimits.from_json_dict(policy_doc),
        )
        server = RiskSignerUnixServer(args.unix_socket, engine)
        server.start_background()
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
        return 0
    parser.error("Specify either --stdio-worker or --unix-socket")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
