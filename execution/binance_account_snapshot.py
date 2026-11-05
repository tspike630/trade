"""Read-only REST account snapshot for ``live_bnb_boll_fade``.

The gateway and the watchdog load a file. They do not call Binance.
``python -m execution.binance_account_snapshot --output PATH`` is the only
writer. It signs ``GET /api/v3/account`` with ``BINANCE_API_KEY`` and
``BINANCE_API_SECRET``, then stores the body plus an HMAC. It does not
place an order, and it does not change the kill file or the live flag.

A hand-edited USDT balance fails the HMAC. ``uid`` ``1289917075`` is the
MCP Agentic sub-account and is not accepted as this REST book.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from execution.executors.binance_spot import (  # noqa: E402
    RECV_WINDOW_MS,
    BinanceSpotAdapter,
    BinanceSpotHttpClient,
    _env_secret,
    sign_query,
)
from execution.executors.binance_spot import API_KEY_ENV, API_SECRET_ENV  # noqa: E402
from execution.executors.binance_spot_gate import (  # noqa: E402
    REST_BOOK,
    SPOT_ACCOUNT_ENDPOINT,
    bind_rest_book,
)

SNAPSHOT_PATH_ENV = "BINANCE_SPOT_ACCOUNT_SNAPSHOT_PATH"
SNAPSHOT_MAX_AGE_ENV = "BINANCE_SPOT_ACCOUNT_SNAPSHOT_MAX_AGE_SEC"
DEFAULT_MAX_AGE_SEC = 3600
SNAPSHOT_SOURCE = "signed_get_account"
_FUTURE_SKEW_MS = 120_000
_PRIVATE_MODE = 0o077


def _repo_root() -> Path:
    return _ROOT


def _canonical(body: dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def snapshot_signature(body: dict[str, Any], secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), _canonical(body), hashlib.sha256).hexdigest()


def _max_age_ms() -> tuple[int | None, str | None]:
    raw = os.environ.get(SNAPSHOT_MAX_AGE_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_AGE_SEC * 1000, None
    if not raw.isdigit() or int(raw) <= 0:
        return None, "snapshot_max_age_invalid"
    return int(raw) * 1000, None


def _path_mode_error(path: Path) -> str | None:
    if path.is_symlink():
        return "snapshot_path_insecure"
    try:
        info = path.lstat()
    except OSError:
        return "snapshot_missing"
    if not stat.S_ISREG(info.st_mode):
        return "snapshot_path_insecure"
    if info.st_mode & _PRIVATE_MODE:
        return "snapshot_path_insecure"
    try:
        resolved = path.resolve()
        resolved.relative_to(_repo_root())
    except ValueError:
        return None
    return "snapshot_path_inside_repo"


def seal_snapshot(payload: dict[str, Any], *, secret: str, fetched_at_ms: int) -> dict[str, Any]:
    body = {
        "source": SNAPSHOT_SOURCE,
        "endpoint": SPOT_ACCOUNT_ENDPOINT,
        "fetched_at_ms": int(fetched_at_ms),
        "payload": payload,
    }
    sealed = dict(body)
    sealed["signature"] = snapshot_signature(body, secret)
    return sealed


def write_snapshot_file(path: Path, sealed: dict[str, Any]) -> str | None:
    """Write mode 0600. Refuse the repo tree and an insecure parent."""
    if path.is_symlink():
        return "snapshot_path_insecure"
    try:
        path.resolve().relative_to(_repo_root())
    except ValueError:
        pass
    else:
        return "snapshot_path_inside_repo"
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return "snapshot_path_insecure"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        return "snapshot_path_insecure"
    try:
        os.write(fd, json.dumps(sealed, sort_keys=True).encode("utf-8"))
        os.fchmod(fd, 0o600)
    except OSError:
        return "snapshot_path_insecure"
    finally:
        os.close(fd)
    return _path_mode_error(path)


def load_snapshot_file(
    path: Path,
    *,
    secret: str,
    now_ms: int,
    max_age_ms: int,
    expected: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Verify path, HMAC, age, and REST binding. No network."""
    mode_error = _path_mode_error(path)
    if mode_error:
        return None, mode_error
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None, "snapshot_missing"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None, "snapshot_bad_json"
    if not isinstance(parsed, dict):
        return None, "snapshot_bad_json"
    signature = parsed.get("signature")
    if not isinstance(signature, str) or not signature:
        return None, "snapshot_unauthenticated"
    body = {
        "source": parsed.get("source"),
        "endpoint": parsed.get("endpoint"),
        "fetched_at_ms": parsed.get("fetched_at_ms"),
        "payload": parsed.get("payload"),
    }
    if body["source"] != SNAPSHOT_SOURCE or body["endpoint"] != SPOT_ACCOUNT_ENDPOINT:
        return None, "snapshot_untrusted_source"
    if not hmac.compare_digest(snapshot_signature(body, secret), signature):
        return None, "snapshot_unauthenticated"
    fetched = body["fetched_at_ms"]
    if isinstance(fetched, bool) or not isinstance(fetched, int):
        return None, "snapshot_bad_json"
    if fetched > int(now_ms) + _FUTURE_SKEW_MS or int(now_ms) - fetched > max_age_ms:
        return None, "snapshot_stale"
    payload = body["payload"]
    if not isinstance(payload, dict):
        return None, "snapshot_bad_json"
    _bound, error = bind_rest_book(payload, expected)
    if error:
        return None, error
    return payload, None


def production_snapshot(
    *,
    now_ms: int | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Load the env path when the selected book is the REST book. No network."""
    book = os.environ.get("BINANCE_SPOT_PRODUCTION_BOOK", "").strip()
    if book != REST_BOOK:
        return None, None
    path_raw = os.environ.get(SNAPSHOT_PATH_ENV, "").strip()
    if not path_raw:
        return None, "snapshot_missing"
    secret = _env_secret(API_SECRET_ENV)
    if not secret:
        return None, "missing_api_credentials"
    max_age_ms, age_error = _max_age_ms()
    if age_error or max_age_ms is None:
        return None, age_error or "snapshot_max_age_invalid"
    usdt = os.environ.get("BINANCE_SPOT_REST_USDT_FREE", "").strip()
    account_type = os.environ.get("BINANCE_SPOT_REST_ACCOUNT_TYPE", "SPOT").strip() or "SPOT"
    clock = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return load_snapshot_file(
        Path(path_raw),
        secret=secret,
        now_ms=clock,
        max_age_ms=max_age_ms,
        expected={"account_type": account_type, "usdt_free": usdt or None},
    )


def default_live_bnb_adapter() -> BinanceSpotAdapter:
    """Adapter for the uninjected live arm. Reads the snapshot file only."""
    payload, error = production_snapshot()
    book = os.environ.get("BINANCE_SPOT_PRODUCTION_BOOK", "").strip() or None
    return BinanceSpotAdapter(
        account_snapshot=payload,
        production_book=book,
        snapshot_error=error,
        allow_rest_http=error is None and payload is not None and book == REST_BOOK,
    )


def fetch_account_payload(
    client: BinanceSpotHttpClient,
    *,
    api_key: str,
    api_secret: str,
    now_ms: int,
) -> dict[str, Any]:
    """One signed account read. The query is not an order."""
    signed = sign_query(
        [
            ("omitZeroBalances", "false"),
            ("recvWindow", RECV_WINDOW_MS),
            ("timestamp", str(int(now_ms))),
        ],
        api_secret,
    )
    payload = client.get_account(signed, api_key=api_key)
    if not isinstance(payload, dict):
        raise ValueError("account_payload_invalid")
    return payload


def refresh_snapshot_file(
    path: Path,
    *,
    client: BinanceSpotHttpClient | None = None,
    now_ms: int | None = None,
) -> str | None:
    """GET the account and write the sealed file. Does not enable trading."""
    key = _env_secret(API_KEY_ENV)
    secret = _env_secret(API_SECRET_ENV)
    if not key or not secret:
        return "missing_api_credentials"
    clock = int(time.time() * 1000) if now_ms is None else int(now_ms)
    http = client or BinanceSpotHttpClient()
    try:
        payload = fetch_account_payload(http, api_key=key, api_secret=secret, now_ms=clock)
    except Exception:
        return "account_read_failed"
    sealed = seal_snapshot(payload, secret=secret, fetched_at_ms=clock)
    return write_snapshot_file(path, sealed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write a read-only Binance spot account snapshot. Does not trade."
    )
    parser.add_argument("--output", required=True, help="snapshot path outside the repo")
    args = parser.parse_args(argv)
    error = refresh_snapshot_file(Path(args.output))
    if error:
        print(error, file=sys.stderr)
        return 2
    print("snapshot_written", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
