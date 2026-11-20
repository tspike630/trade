"""End-to-end PR222 Cross-Margin + Out-of-Process Risk Signer zero-secret test."""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from execution.margin_cross.errors import LiveNetworkDisabled
from execution.margin_cross.http_client import BinanceCrossMarginHttpClient
from execution.risk_signer_daemon import RiskPolicyLimits, RiskSignerSubprocessClient


class CrossMarginOutOfProcessSignerTests(unittest.TestCase):
    def test_cross_margin_http_client_with_out_of_process_risk_signer_holds_zero_in_process_secrets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            secret_file = tmp_dir / "margin_signer_secret.json"
            secret_file.write_text(
                json.dumps(
                    {
                        "api_key": "MARGIN_PUB_KEY",
                        "api_secret": "MARGIN_DAEMON_ONLY_SECRET_999",
                        "approval_hmac_key": "MARGIN_DAEMON_ONLY_HMAC_888",
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(secret_file, 0o600)

            now_ms = int(__import__("time").time() * 1000)
            cfg_doc = {
                "approval_scope": "binance_cross_margin_only",
                "approved_by": "yang_operator_review",
                "approved_account_uid": "888000111",
                "approved_at_ms": now_ms - 10_000,
                "expires_at_ms": now_ms + 3_600_000,
                "max_leverage": "2.0",
                "max_effective_leverage": "1.5",
                "max_total_liability_net_ratio": "0.5",
                "max_single_borrow_net_ratio": "0.25",
                "max_borrowable_pool_ratio": "0.2",
                "min_entry_margin_level": "2.5",
                "warn_margin_level": "2.0",
                "emergency_margin_level": "1.5",
                "min_order_notional_usdt": "10",
                "max_snapshot_age_ms": 5000,
                "interest_horizon_hours": "24",
                "base_position_pct": "0.10",
                "approved_strategies": ["regime4_crowded_long_carry_short"],
            }
            cfg_path = tmp_dir / "approved_margin_cfg.json"
            raw_cfg = json.dumps(cfg_doc, sort_keys=True).encode("utf-8")
            cfg_path.write_bytes(raw_cfg)
            cfg_sha = hashlib.sha256(raw_cfg).hexdigest()

            limits = RiskPolicyLimits(
                approved_account_uid="888000111",
                approved_config_sha256=cfg_sha,
                approval_expires_at_ms=now_ms + 3_600_000,
                max_single_borrow_usd=Decimal("300"),
                max_borrow_by_asset={"SOL": Decimal("2.0")},
                asset_reference_prices_usd={"SOL": Decimal("100")},
                kill_switch_path=str(tmp_dir / "NO_KILL"),
            )

            seen_urls: list[str] = []

            def fake_opener(req, _timeout):
                seen_urls.append(req.full_url)
                if req.get_method() == "POST":
                    return 200, b'{"tranId": 12345}'
                return (
                    200,
                    b'{"created": true, "tradeEnabled": true, "borrowEnabled": true, "marginLevel": "999", "marginLevelStatus": "EXCESSIVE", "totalLiabilityOfBtc": "0", "userAssets": []}',
                )

            # Parent env has NO api_secret and NO CROSS_MARGIN_APPROVAL_HMAC_KEY!
            parent_env = {
                "CROSS_MARGIN_PARAMETERS_APPROVED": "1",
                "CROSS_MARGIN_LIVE_ENABLED": "1",
                "CROSS_MARGIN_APPROVED_CONFIG_JSON": str(cfg_path),
                "CROSS_MARGIN_APPROVED_CONFIG_SHA256": cfg_sha,
            }

            with RiskSignerSubprocessClient(
                api_key="MARGIN_PUB_KEY",
                bootstrap_secret_file=secret_file,
                limits=limits,
                python_executable=sys.executable,
            ) as signer_cli:
                secret_file.unlink()
                http_cli = BinanceCrossMarginHttpClient(
                    api_key="MARGIN_PUB_KEY",
                    account_uid="888000111",
                    opener=fake_opener,
                    clock_ms=lambda: now_ms,
                    environ=parent_env,
                    kill_switch_path=tmp_dir / "NO_KILL",
                    signer=signer_cli,
                )
                bor_res = http_cli.borrow(
                    {"asset": "SOL", "isIsolated": "FALSE", "amount": "1.5", "type": "BORROW"}
                )
                self.assertEqual(bor_res["tranId"], 12345)
                self.assertEqual(len(seen_urls), 1)

                with self.assertRaises(LiveNetworkDisabled) as ctx_over:
                    http_cli.borrow(
                        {"asset": "SOL", "isIsolated": "FALSE", "amount": "1.0", "type": "BORROW"}
                    )
                self.assertIn("max_borrow_exceeded:SOL", str(ctx_over.exception))
                self.assertEqual(len(seen_urls), 1)


if __name__ == "__main__":
    unittest.main()
