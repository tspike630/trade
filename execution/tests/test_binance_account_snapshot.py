"""Production REST account snapshot. No live HTTP and no orders.

    python -m unittest execution.tests.test_binance_account_snapshot
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawler"))

from execution.tests.isolated_env import patch_isolated  # noqa: E402
from execution.binance_account_snapshot import (  # noqa: E402
    load_snapshot_file,
    main,
    seal_snapshot,
    write_snapshot_file,
)
from execution.executors.binance_spot_gate import (  # noqa: E402
    MCP_POSITION_UID,
    bind_rest_book,
    funding_block,
    permission_kind,
)
from execution.executors.binance_spot_protect import ENTRY_MIN_NOTIONAL_USDT  # noqa: E402
from execution.gateway import ExecutionGateway  # noqa: E402
from execution.ledger import ExecutionLedger, initialize_spot_ledger_binding  # noqa: E402
from execution.live_bnb_watchdog import preflight  # noqa: E402

SECRET = "snapshot-secret-not-a-key"
NOW_MS = 1_700_000_000_000
_RESTRICTIONS = {
    "enableSpotAndMarginTrading": True,
    "enableReading": True,
    "ipRestrict": True,
    "enableMargin": False,
    "enableFutures": False,
    "enableWithdrawals": False,
    "enableInternalTransfer": True,
    "enableVanillaOptions": False,
    "permitsUniversalTransfer": True,
    "createTime": 1793901903000,
}
_ACCOUNT = {
    "accountType": "SPOT",
    "canTrade": True,
    "canWithdraw": False,
    "canDeposit": True,
    "brokered": False,
    "updateTime": 1_700_000_000_000,
    "permissions": ["TRD_GRP_080"],
    "uid": 1275399777,
    "balances": [
        {"asset": "USDT", "free": "250.50", "locked": "0.00"},
        {"asset": "FET", "free": "0.00000000", "locked": "0.00000000"},
    ],
}


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _expected() -> dict[str, str]:
    return {"account_type": "SPOT", "usdt_free": "250.50"}


def _install(root: Path, payload: dict[str, Any] | None = None) -> Path:
    path = root / "account.json"
    sealed = seal_snapshot(
        payload or _ACCOUNT,
        secret=SECRET,
        fetched_at_ms=NOW_MS,
        restrictions=_RESTRICTIONS,
    )
    error = write_snapshot_file(path, sealed)
    if error:
        raise AssertionError(error)
    return path


def _env(root: Path, path: Path | None) -> dict[str, str]:
    return {
        "BINANCE_SPOT_PRODUCTION_BOOK": "rest_api_key",
        "BINANCE_SPOT_REST_USDT_FREE": "250.50",
        "BINANCE_SPOT_REST_ACCOUNT_TYPE": "SPOT",
        "BINANCE_SPOT_ACCOUNT_SNAPSHOT_PATH": "" if path is None else str(path),
        "BINANCE_API_KEY": "test-key",
        "BINANCE_API_SECRET": SECRET,
        "LIVE_EXCHANGE_API_KEY": "",
        "LIVE_EXCHANGE_API_SECRET": "",
        "BINANCE_SPOT_LIVE_ENABLED": "1",
        "BINANCE_SPOT_BOOK_USDT": "1000",
        "LIVE_KILL_SWITCH_PATH": str(root / "no-kill"),
        "EXECUTION_PAPER_ENABLED": "",
        "PAPER_DB_PATH": "",
        "EXECUTION_PAPER_DB": "",
    }


class SnapshotFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_sealed_file_binds_without_uid_and_rejects_tampering(self) -> None:
        path = _install(self.root)
        text = path.read_text(encoding="utf-8")
        self.assertNotIn(SECRET, text)
        self.assertNotIn("test-key", text)
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode & 0o077, 0)
        payload, error = load_snapshot_file(
            path,
            secret=SECRET,
            now_ms=NOW_MS,
            max_age_ms=3600_000,
            expected=_expected(),
        )
        self.assertIsNone(error)
        assert payload is not None
        self.assertEqual(payload["uid"], 1275399777)
        self.assertNotEqual(str(payload["uid"]), MCP_POSITION_UID)
        self.assertEqual(payload["permissions"], ["TRD_GRP_080"])
        self.assertIs(payload["restrictions"]["enableSpotAndMarginTrading"], True)
        broken = json.loads(text)
        broken["payload"]["balances"][0]["free"] = "1"
        path.write_text(json.dumps(broken), encoding="utf-8")
        os_chmod = path.chmod
        path.chmod(0o600)
        del os_chmod
        _payload, tampered = load_snapshot_file(
            path,
            secret=SECRET,
            now_ms=NOW_MS,
            max_age_ms=3600_000,
            expected=_expected(),
        )
        self.assertEqual(tampered, "snapshot_unauthenticated")

    def test_missing_bad_stale_permission_usdt_and_mcp_uid(self) -> None:
        missing, missing_error = load_snapshot_file(
            self.root / "absent.json",
            secret=SECRET,
            now_ms=NOW_MS,
            max_age_ms=3600_000,
            expected=_expected(),
        )
        self.assertIsNone(missing)
        self.assertEqual(missing_error, "snapshot_missing")
        bad = self.root / "bad.json"
        bad.write_text("{", encoding="utf-8")
        bad.chmod(0o600)
        self.assertEqual(
            load_snapshot_file(
                bad, secret=SECRET, now_ms=NOW_MS, max_age_ms=3600_000, expected=_expected()
            )[1],
            "snapshot_bad_json",
        )
        stale = _install(self.root)
        self.assertEqual(
            load_snapshot_file(
                stale,
                secret=SECRET,
                now_ms=NOW_MS + 3_600_001,
                max_age_ms=3600_000,
                expected=_expected(),
            )[1],
            "snapshot_stale",
        )
        loose = _install(self.root)
        loose.chmod(0o644)
        self.assertEqual(
            load_snapshot_file(
                loose, secret=SECRET, now_ms=NOW_MS, max_age_ms=3600_000, expected=_expected()
            )[1],
            "snapshot_path_insecure",
        )
        mismatch = _install(self.root)
        self.assertEqual(
            load_snapshot_file(
                mismatch,
                secret=SECRET,
                now_ms=NOW_MS,
                max_age_ms=3600_000,
                expected={"account_type": "SPOT", "usdt_free": "999"},
            )[1],
            "rest_balance_mismatch",
        )
        claimed = dict(_ACCOUNT)
        claimed["uid"] = int(MCP_POSITION_UID)
        mcp = self.root / "mcp.json"
        sealed = seal_snapshot(
            claimed, secret=SECRET, fetched_at_ms=NOW_MS, restrictions=_RESTRICTIONS
        )
        self.assertIsNone(write_snapshot_file(mcp, sealed))
        self.assertEqual(
            load_snapshot_file(
                mcp, secret=SECRET, now_ms=NOW_MS, max_age_ms=3600_000, expected=_expected()
            )[1],
            "account_book_mismatch",
        )

    def test_writer_uses_a_mocked_get_and_does_not_touch_the_kill_file(self) -> None:
        kill = self.root / "LIVE_TRADING_DISABLED"
        kill.write_text("keep\n", encoding="utf-8")
        out = self.root / "from-get.json"
        seen: list[str] = []

        def urlopen(request: urllib.request.Request, timeout: float) -> _Response:
            del timeout
            seen.append(request.full_url)
            path = request.full_url.split("?", 1)[0]
            if request.get_method() != "GET" or "/order" in path:
                raise AssertionError(request.full_url)
            if path.endswith("/api/v3/account"):
                return _Response(200, json.dumps(_ACCOUNT).encode("utf-8"))
            if path.endswith("/sapi/v1/account/apiRestrictions"):
                return _Response(200, json.dumps(_RESTRICTIONS).encode("utf-8"))
            raise AssertionError(request.full_url)

        env = _env(self.root, None)
        env["BINANCE_SPOT_LIVE_ENABLED"] = "0"
        with patch_isolated(env), mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen", urlopen
        ), mock.patch(
            "execution.binance_account_snapshot.time.time", return_value=1_700_000_000
        ):
            code = main(["--output", str(out)])
            self.assertEqual(os.environ.get("BINANCE_SPOT_LIVE_ENABLED"), "0")
        self.assertEqual(code, 0, seen)
        self.assertEqual(len(seen), 2)
        self.assertEqual(kill.read_text(encoding="utf-8"), "keep\n")
        text = out.read_text(encoding="utf-8")
        self.assertNotIn(SECRET, text)
        payload, error = load_snapshot_file(
            out, secret=SECRET, now_ms=NOW_MS, max_age_ms=3600_000, expected=_expected()
        )
        self.assertIsNone(error)
        assert payload is not None
        self.assertEqual(payload["uid"], 1275399777)
        self.assertEqual(payload["restrictions"]["enableSpotAndMarginTrading"], True)
        self.assertEqual(payload["restrictions"]["ipRestrict"], True)

    def test_permission_shapes_and_zero_usdt_do_not_fund(self) -> None:
        self.assertEqual(permission_kind("TRD_GRP_080"), "exact_group")
        self.assertEqual(permission_kind("TRD_GRP_004"), "documented_group")
        self.assertEqual(permission_kind("SPOT"), "legacy_spot")
        for bad in ("TRD_GRP_", "TRD_GRP_08", "TRD_GRP_080extra", "TRD_GRP_ABC", "SPOT "):
            self.assertEqual(permission_kind(bad), "rejected", bad)
        expected = {"account_type": "SPOT", "usdt_free": "0"}
        account = {
            "accountType": "SPOT",
            "canTrade": True,
            "permissions": ["TRD_GRP_080"],
            "uid": 1275399777,
            "balances": [{"asset": "USDT", "free": "0", "locked": "0"}],
            "restrictions": _RESTRICTIONS,
        }
        bound, error = bind_rest_book(account, expected)
        self.assertIsNone(error)
        assert bound is not None
        self.assertEqual(bound["uid"], "1275399777")
        self.assertFalse(bound["same_as_mcp_uid"])
        self.assertNotEqual(bound["uid"], MCP_POSITION_UID)
        self.assertEqual(
            funding_block(bound["usdt_free"], ENTRY_MIN_NOTIONAL_USDT),
            "funding_block",
        )
        self.assertEqual(ENTRY_MIN_NOTIONAL_USDT, 20)
        empty = dict(account)
        empty["permissions"] = []
        self.assertEqual(bind_rest_book(empty, expected)[1], "rest_permission_missing")
        junk = dict(account)
        junk["permissions"] = ["TRD_GRP_080extra"]
        self.assertEqual(bind_rest_book(junk, expected)[1], "rest_permission_rejected")
        disabled = dict(account)
        disabled["restrictions"] = dict(_RESTRICTIONS)
        disabled["restrictions"]["enableSpotAndMarginTrading"] = False
        self.assertEqual(bind_rest_book(disabled, expected)[1], "rest_capability_disabled")
        missing = dict(account)
        missing.pop("restrictions")
        self.assertEqual(bind_rest_book(missing, expected)[1], "rest_capability_missing")
        legacy = dict(account)
        legacy["permissions"] = ["SPOT"]
        legacy.pop("uid")
        self.assertIsNone(bind_rest_book(legacy, expected)[1])
        funded = dict(account)
        funded["balances"] = [{"asset": "USDT", "free": "20", "locked": "0"}]
        funded_bound, funded_error = bind_rest_book(
            funded, {"account_type": "SPOT", "usdt_free": "20"}
        )
        self.assertIsNone(funded_error)
        assert funded_bound is not None
        self.assertIsNone(funding_block(funded_bound["usdt_free"], ENTRY_MIN_NOTIONAL_USDT, "20"))
        self.assertEqual(
            funding_block(funded_bound["usdt_free"], ENTRY_MIN_NOTIONAL_USDT, "50"),
            "funding_block",
        )


class ProductionAssemblyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._net = mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("live binance HTTP is disabled"),
        )
        self._net.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = _install(self.root)
        self.env = _env(self.root, self.path)

    def tearDown(self) -> None:
        self._net.stop()
        self._tmp.cleanup()

    def _row(self) -> dict[str, Any]:
        raw = {
            "pair_address": "BNB/USDT",
            "purpose": "live_bnb_boll_fade",
            "extras": {
                "binance_symbol": "BNBUSDT",
                "order_type": "MARKET",
                "quote_order_qty": "25",
                "price_usd": "102",
                "stop_price": "99",
                "take_profit_price": "110",
                "entry_reason": "large_trade_market",
            },
        }
        return {
            "id": 147841,
            "ts": "2026-11-06T00:00:00Z",
            "strategy_id": "tech_boll_fade_v1",
            "action": "buy",
            "chain_id": "binance",
            "token_address": "BNBUSDT",
            "size_pct": 0.05,
            "reason": "entry-zone",
            "risk_passed": 1,
            "mode": "live_stub",
            "purpose": "live_bnb_boll_fade",
            "raw_json": json.dumps(raw),
        }

    def test_gateway_without_an_injected_adapter_reads_the_file(self) -> None:
        kill = self.root / "LIVE_TRADING_DISABLED"
        kill.write_text("off\n", encoding="utf-8")
        env = dict(self.env)
        env["LIVE_KILL_SWITCH_PATH"] = str(kill)
        with patch_isolated(env), mock.patch(
            "execution.binance_account_snapshot.time.time", return_value=1_700_000_000
        ):
            with ExecutionGateway(
                decision_db=self.root / "decision.db",
                ledger_path=self.root / "execution_ledger_live_bnb_boll_fade.db",
                paper_enabled=False,
                pm_enabled=False,
                accept_purposes=frozenset({"live_bnb_boll_fade"}),
            ) as gw:
                refused = gw.process_one(self._row())
        self.assertEqual(refused["executed"], 0)
        self.assertTrue(str(refused["error"]).startswith("kill_switch:"), refused)
        self.assertNotIn(SECRET, json.dumps(refused))

    def test_missing_snapshot_stops_before_http(self) -> None:
        env = dict(self.env)
        env["BINANCE_SPOT_ACCOUNT_SNAPSHOT_PATH"] = str(self.root / "missing.json")
        with patch_isolated(env):
            with ExecutionGateway(
                decision_db=self.root / "decision.db",
                ledger_path=self.root / "execution_ledger_live_bnb_boll_fade.db",
                paper_enabled=False,
                pm_enabled=False,
                accept_purposes=frozenset({"live_bnb_boll_fade"}),
            ) as gw:
                refused = gw.process_one(self._row())
        self.assertEqual(refused["error"], "snapshot_missing")
        self.assertEqual(refused["executed"], 0)

    def test_open_gates_reach_exchange_info_and_not_an_order(self) -> None:
        seen: list[str] = []

        def urlopen(request: urllib.request.Request, timeout: float) -> _Response:
            del timeout
            seen.append(request.full_url)
            if request.get_method() != "GET" or "/api/v3/order" in request.full_url:
                raise AssertionError(request.full_url)
            body = json.dumps(
                {"symbol": "BNBUSDT", "status": "HALT", "quoteAsset": "USDT"}
            ).encode("utf-8")
            return _Response(200, body)

        self._net.stop()
        try:
            with patch_isolated(self.env), mock.patch(
                "execution.binance_account_snapshot.time.time", return_value=1_700_000_000
            ), mock.patch(
                "execution.executors.binance_spot.urllib.request.urlopen", urlopen
            ):
                ledger_path = self.root / "execution_ledger_live_bnb_boll_fade.db"
                registry = self.root / "registry.json"
                classic = self.root / "execution_ledger_live_bnb_boll_classic.db"
                if not classic.is_file():
                    ExecutionLedger(classic).close()
                initialize_spot_ledger_binding(classic, "1275399777")
                registry.write_text(
                    json.dumps(
                        {
                            "required_purposes": [
                                "live_bnb_boll_fade",
                                "live_bnb_boll_classic",
                            ],
                            "purpose_ledgers": {
                                "live_bnb_boll_fade": str(ledger_path.resolve()),
                                "live_bnb_boll_classic": str(classic.resolve()),
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                self.env["BINANCE_SPOT_LEDGER_REGISTRY"] = str(registry)
                os.environ["BINANCE_SPOT_LEDGER_REGISTRY"] = str(registry)
                with ExecutionGateway(
                    decision_db=self.root / "decision.db",
                    ledger_path=ledger_path,
                    paper_enabled=False,
                    pm_enabled=False,
                    accept_purposes=frozenset({"live_bnb_boll_fade"}),
                ) as gw:
                    initialize_spot_ledger_binding(ledger_path, "1275399777")
                    refused = gw.process_one(self._row())
        finally:
            self._net.start()
        self.assertEqual(refused["executed"], 0, refused)
        self.assertEqual(refused["error"], "symbol_not_trading")
        self.assertTrue(seen)
        self.assertTrue(all(url.endswith("/api/v3/exchangeInfo?symbol=BNBUSDT") for url in seen))
        self.assertNotIn(SECRET, json.dumps(refused))

    def test_watchdog_rejects_a_stale_snapshot_and_does_not_spawn(self) -> None:
        env = dict(self.env)
        with patch_isolated(env), mock.patch(
            "execution.binance_account_snapshot.time.time",
            return_value=1_700_003_700,
        ), mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("watchdog must not call Binance"),
        ):
            report = preflight()
        self.assertFalse(report["would_start"])
        self.assertIn("snapshot_stale", report["reasons"])
        self.assertIsNone(report["argv"])
        self.assertNotIn(SECRET, json.dumps(report))


if __name__ == "__main__":
    unittest.main()
