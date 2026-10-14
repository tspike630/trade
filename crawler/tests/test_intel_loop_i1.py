"""INTEL_LOOP I-1 labels and holder snapshots. Synthetic fixtures only.

No live keys, network, or production DBs. Counts are observe-only and do not
count toward strategy validity.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import config
from daily_check import INTEL_I1_CHECK_NAME, CheckReport, check_intel_loop_i1
from sources.holder_snapshot import (
    TOP_HOLDER_N,
    assemble_holder_snapshot,
    fetch_holder_snapshot,
    flag_enabled,
    parse_largest_account_rows,
    rpc_result,
)
from sources.solana_helius import (
    HeliusListener,
    apply_helius_intel,
    extract_dev_authorities,
    extract_transfer_edges,
    enqueue_solana_mints,
)
from store import Store, format_intel_observe_line, norm_address
from tools.grok_context import build_context, to_markdown

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "helius_get_transaction.json"
_PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_MINT = "He1iusTxMintBBBB222222222222222222222222222"
_AUTH = "Auth111111111111111111111111111111111111111"


def _load_tx() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _snapshot(
    *,
    snapshot_id: str = "snap-1",
    recorded_at: str = "2026-10-14T00:00:00Z",
    supply_raw: str | None = "100",
    supply_ui: float | None = 100.0,
    rows: list[dict] | None = None,
    pct: float | None = None,
) -> dict:
    body = {
        "snapshot_id": snapshot_id,
        "recorded_at": recorded_at,
        "total_supply_raw": supply_raw,
        "total_supply_ui": supply_ui,
        "source": "helius:getTokenLargestAccounts",
        "rows": rows
        if rows is not None
        else [
            {
                "rank": 1,
                "holder_address": "WalletBig",
                "address_kind": "owner",
                "amount_raw": "90",
                "amount_ui": 90.0,
                "pct_of_supply": pct if pct is not None else 0.9,
            },
            {
                "rank": 2,
                "holder_address": "TokenAcctOnly",
                "address_kind": "token_account",
                "amount_raw": "10",
                "amount_ui": 10.0,
                "pct_of_supply": 0.1,
            },
        ],
    }
    return body


class FlagAndSchemaTests(unittest.TestCase):
    def test_flag_parser_matches_config_default(self) -> None:
        self.assertTrue(flag_enabled(None))
        self.assertTrue(flag_enabled("1"))
        self.assertTrue(flag_enabled("on"))
        self.assertFalse(flag_enabled("0"))
        self.assertFalse(flag_enabled("off"))
        self.assertFalse(flag_enabled("false"))
        self.assertEqual(
            config.HOLDER_SNAPSHOT_ENABLED,
            flag_enabled(os.environ.get("HOLDER_SNAPSHOT_ENABLED")),
        )
        self.assertEqual(TOP_HOLDER_N, 10)

    def test_tables_are_additive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            store = Store(path)
            names = {
                row[0]
                for row in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            store.close()
            self.assertTrue(
                {"address_labels", "token_holder_snapshots", "token_transfer_edges"} <= names
            )
            ro = Store.open_ro(path)
            counts = ro.intel_observe_counts()
            ro.close()
            self.assertTrue(counts["available"])
            self.assertEqual(counts["labeled_dev_n"], 0)
            self.assertEqual(counts["holder_snapshot_n"], 0)
            self.assertEqual(counts["tokens_with_top10"], 0)


class DevLabelTests(unittest.TestCase):
    def test_fixture_initialize_mint_is_dev(self) -> None:
        found = extract_dev_authorities(_load_tx())
        self.assertEqual(found, {norm_address(_MINT): norm_address(_AUTH)})

    def test_missing_authority_and_program_authority_stay_unlabeled(self) -> None:
        missing = _load_tx()
        missing["transaction"]["message"]["instructions"][0]["parsed"]["info"].pop(
            "mintAuthority"
        )
        self.assertEqual(extract_dev_authorities(missing), {})

        program = _load_tx()
        program["transaction"]["message"]["instructions"][0]["parsed"]["info"][
            "mintAuthority"
        ] = _PUMP
        self.assertEqual(extract_dev_authorities(program), {})

        inner_only = {
            "transaction": {"message": {"instructions": []}},
            "meta": {
                "innerInstructions": [
                    {
                        "instructions": [
                            {
                                "parsed": {
                                    "type": "initializeMint2",
                                    "info": {
                                        "mint": "InnerMint",
                                        "mintAuthority": "InnerDev",
                                    },
                                }
                            }
                        ]
                    }
                ]
            },
        }
        self.assertEqual(
            extract_dev_authorities(inner_only),
            {norm_address("InnerMint"): norm_address("InnerDev")},
        )

    def test_enqueue_writes_dev_only_when_known(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            mint = norm_address(_MINT)
            wrote = enqueue_solana_mints(
                store,
                [_MINT],
                signature="sig-dev",
                emit_new_listing=False,
                intel={"deployers": extract_dev_authorities(_load_tx())},
            )
            self.assertEqual(wrote, 1)
            labels = store.list_address_labels("solana", token_mint=mint, label_type="dev")
            self.assertEqual(len(labels), 1)
            self.assertEqual(labels[0]["address"], norm_address(_AUTH))
            self.assertEqual(labels[0]["source"], "helius:initializeMint")
            first = labels[0]["first_seen_at"]
            again = store.upsert_address_label(
                "solana",
                _AUTH,
                "dev",
                token_mint=mint,
                source="helius:initializeMint",
                now="2026-10-14T03:00:00Z",
            )
            self.assertEqual(again, "updated")
            refreshed = store.list_address_labels("solana", token_mint=mint, label_type="dev")
            self.assertEqual(refreshed[0]["first_seen_at"], first)
            self.assertEqual(refreshed[0]["updated_at"], "2026-10-14T03:00:00Z")

            other = Store(Path(tmp) / "bare.db")
            enqueue_solana_mints(
                other,
                ["NoAuthorityMint"],
                signature="sig-none",
                emit_new_listing=False,
            )
            self.assertEqual(other.list_address_labels("solana"), [])
            apply_helius_intel(
                other,
                ["SomeOtherMint"],
                {"deployers": {norm_address(_MINT): norm_address(_AUTH)}},
            )
            self.assertEqual(other.list_address_labels("solana"), [])
            store.close()
            other.close()

    def test_writer_job_with_intel_labels_dev(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            listener = HeliusListener(db_path=str(path), api_key="not-used")
            listener._jobs.put(
                (
                    "mints",
                    [_MINT],
                    "sig-job",
                    False,
                    {"deployers": {_MINT: _AUTH}},
                )
            )
            listener._jobs.put(("mints", ["LogOnlyMint"], "sig-log", False))
            listener._jobs.put(None)
            listener._writer_loop()
            store = Store(path)
            devs = store.list_address_labels("solana", label_type="dev")
            store.close()
            self.assertEqual(len(devs), 1)
            self.assertEqual(devs[0]["token_mint"], norm_address(_MINT))
            self.assertEqual(devs[0]["address"], norm_address(_AUTH))


class HolderSnapshotTests(unittest.TestCase):
    def test_assemble_same_timestamp_and_null_supply(self) -> None:
        largest = {
            "value": [
                {"address": "SmallAcc", "amount": "10", "uiAmount": 10.0},
                {"address": "BigAcc", "amount": "90", "uiAmount": 90.0},
                {"address": "", "amount": "999", "uiAmount": 999.0},
            ]
        }
        owners = {
            "value": [
                {
                    "data": {
                        "parsed": {
                            "type": "account",
                            "info": {"owner": "WalletBig"},
                        }
                    }
                },
                None,
            ]
        }
        snap = assemble_holder_snapshot(
            largest=largest,
            supply={"value": {"amount": "100", "uiAmount": 100.0}},
            owners=owners,
            recorded_at="2026-10-14T00:00:00Z",
            snapshot_id="snap-a",
        )
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertEqual([row["rank"] for row in snap["rows"]], [1, 2])
        self.assertEqual(snap["rows"][0]["holder_address"], norm_address("WalletBig"))
        self.assertEqual(snap["rows"][0]["address_kind"], "owner")
        self.assertEqual(snap["rows"][0]["pct_of_supply"], 0.9)
        self.assertEqual(snap["rows"][1]["holder_address"], "SmallAcc")
        self.assertEqual(snap["rows"][1]["address_kind"], "token_account")
        self.assertEqual(snap["rows"][1]["pct_of_supply"], 0.1)
        stamps = {snap["recorded_at"]}
        supplies = {snap["total_supply_raw"]}
        self.assertEqual(stamps, {"2026-10-14T00:00:00Z"})
        self.assertEqual(supplies, {"100"})

        bare = assemble_holder_snapshot(
            largest=largest,
            supply=None,
            owners=owners,
            recorded_at="2026-10-14T00:00:00Z",
            snapshot_id="snap-b",
        )
        assert bare is not None
        self.assertIsNone(bare["total_supply_raw"])
        self.assertIsNone(bare["total_supply_ui"])
        self.assertTrue(all(row["pct_of_supply"] is None for row in bare["rows"]))
        self.assertEqual(bare["rows"][0]["amount_raw"], "90")
        self.assertIsNone(assemble_holder_snapshot(
            largest={"value": []},
            supply={"value": {"amount": "1", "uiAmount": 1.0}},
            owners=None,
            recorded_at="2026-10-14T00:00:00Z",
            snapshot_id="empty",
        ))

    def test_top10_cap_drops_the_smallest(self) -> None:
        value = [
            {"address": f"Acct{i:02d}", "amount": str(i), "uiAmount": float(i)}
            for i in range(1, 13)
        ]
        rows = parse_largest_account_rows({"value": value})
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["amount_raw"], "12")
        self.assertEqual(rows[-1]["amount_raw"], "3")

    def test_store_recomputes_pct_and_keeps_one_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            minted = norm_address("MintSnap")
            fake = _snapshot(supply_raw=None, supply_ui=None, pct=0.99)
            fake["rows"] = [
                {
                    "rank": 1,
                    "holder_address": "WalletBig",
                    "address_kind": "owner",
                    "amount_raw": "90",
                    "amount_ui": 90.0,
                    "pct_of_supply": 0.99,
                }
            ]
            self.assertEqual(store.insert_holder_snapshot("solana", minted, fake), 1)
            stored = store.latest_holder_snapshot("solana", minted)
            self.assertEqual(len(stored), 1)
            self.assertIsNone(stored[0]["pct_of_supply"])
            self.assertIsNone(stored[0]["total_supply_ui"])
            self.assertEqual(stored[0]["amount_raw"], "90")

            full = _snapshot()
            self.assertEqual(store.insert_holder_snapshot("solana", minted, full), 2)
            older = _snapshot(
                snapshot_id="snap-old",
                recorded_at="2026-10-13T00:00:00Z",
                rows=[
                    {
                        "rank": 1,
                        "holder_address": "OldWallet",
                        "address_kind": "owner",
                        "amount_raw": "1",
                        "amount_ui": 1.0,
                    }
                ],
            )
            store.insert_holder_snapshot("solana", minted, older)
            latest = store.latest_holder_snapshot("solana", minted)
            self.assertEqual({row["snapshot_id"] for row in latest}, {"snap-1"})
            self.assertEqual({row["recorded_at"] for row in latest}, {"2026-10-14T00:00:00Z"})
            self.assertEqual(
                {row["total_supply_raw"] for row in latest},
                {"100"},
            )
            self.assertEqual(latest[0]["pct_of_supply"], 0.9)
            self.assertEqual(store.insert_holder_snapshot("solana", minted, {"rows": []}), 0)
            store.close()


class TransferAndApplyTests(unittest.TestCase):
    def test_transfer_edges_skip_incomplete_and_other_mints(self) -> None:
        tx = {
            "transaction": {
                "message": {
                    "instructions": [
                        {
                            "parsed": {
                                "type": "transferChecked",
                                "info": {
                                    "mint": _MINT,
                                    "source": "FromWallet",
                                    "destination": "ToWallet",
                                    "tokenAmount": {"amount": "5", "uiAmount": 5.0},
                                },
                            }
                        },
                        {
                            "parsed": {
                                "type": "transfer",
                                "info": {
                                    "source": "FromWallet",
                                    "destination": "ToWallet",
                                    "amount": "9",
                                },
                            }
                        },
                        {
                            "parsed": {
                                "type": "transferChecked",
                                "info": {
                                    "mint": "QuoteMint",
                                    "source": "FromWallet",
                                    "destination": "Pool",
                                    "tokenAmount": {"amount": "1", "uiAmount": 1.0},
                                },
                            }
                        },
                    ]
                }
            }
        }
        edges = extract_transfer_edges(
            tx, signature="sig-x", recorded_at="2026-10-14T00:00:00Z"
        )
        self.assertEqual(len(edges), 2)
        self.assertTrue(all(edge["signature"] == "sig-x" for edge in edges))
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            mint = norm_address(_MINT)
            snap = _snapshot()
            snap["rows"][0]["holder_address"] = "WalletBig"
            snap["rows"][1]["holder_address"] = _PUMP
            snap["rows"][1]["address_kind"] = "owner"
            apply_helius_intel(
                store,
                [_MINT],
                {
                    "deployers": {mint: norm_address(_AUTH)},
                    "snapshots": {mint: snap},
                    "transfers": edges,
                },
            )
            self.assertEqual(store.intel_observe_counts()["labeled_dev_n"], 1)
            self.assertEqual(store.intel_observe_counts()["holder_snapshot_n"], 1)
            self.assertEqual(store.intel_observe_counts()["tokens_with_top10"], 1)
            self.assertEqual(store.intel_observe_counts()["transfer_edge_n"], 1)
            whales = store.list_address_labels(
                "solana", token_mint=mint, label_type="whale_top10"
            )
            self.assertEqual([row["address"] for row in whales], [norm_address("WalletBig")])
            again = store.insert_transfer_edges(edges[:1])
            self.assertEqual(again, 0)
            store.close()


class RpcHelperTests(unittest.TestCase):
    def test_disabled_or_empty_does_not_invent_rows(self) -> None:
        calls: list[str] = []

        async def rpc(method: str, _params: list) -> dict | None:
            calls.append(method)
            if method == "getTokenLargestAccounts":
                return {"value": []}
            raise AssertionError(method)

        skipped = asyncio.run(
            fetch_holder_snapshot(
                "Mint",
                http_url="http://rpc.invalid",
                enabled=False,
                rpc=rpc,
            )
        )
        self.assertIsNone(skipped)
        self.assertEqual(calls, [])

        empty = asyncio.run(
            fetch_holder_snapshot(
                "Mint",
                http_url="http://rpc.invalid",
                enabled=True,
                rpc=rpc,
            )
        )
        self.assertIsNone(empty)
        self.assertEqual(calls, ["getTokenLargestAccounts"])
        self.assertIsNone(rpc_result({"error": {"code": 1}}))
        self.assertEqual(rpc_result({"result": {"value": []}}), {"value": []})

    def test_fetch_wires_supply_and_owner_on_one_timestamp(self) -> None:
        recorded = "2026-10-14T01:02:03Z"

        async def rpc(method: str, params: list) -> dict | None:
            if method == "getTokenLargestAccounts":
                return {
                    "value": [
                        {"address": "SmallAcc", "amount": "10", "uiAmount": 10.0},
                        {"address": "BigAcc", "amount": "40", "uiAmount": 40.0},
                    ]
                }
            if method == "getTokenSupply":
                self.assertEqual(params[0], "MintWire")
                return {"value": {"amount": "50", "uiAmount": 50.0}}
            if method == "getMultipleAccounts":
                requested = params[0]
                self.assertEqual(requested, ["BigAcc", "SmallAcc"])
                return {
                    "value": [
                        {
                            "data": {
                                "parsed": {
                                    "type": "account",
                                    "info": {"owner": "WalletBig"},
                                }
                            }
                        },
                        {
                            "data": {
                                "parsed": {
                                    "type": "account",
                                    "info": {"owner": "WalletSmall"},
                                }
                            }
                        },
                    ]
                }
            raise AssertionError(method)

        snap = asyncio.run(
            fetch_holder_snapshot(
                "MintWire",
                http_url="http://rpc.invalid",
                enabled=True,
                rpc=rpc,
                recorded_at=recorded,
                snapshot_id="snap-wire",
            )
        )
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertEqual(snap["recorded_at"], recorded)
        self.assertEqual(snap["snapshot_id"], "snap-wire")
        self.assertEqual(snap["total_supply_raw"], "50")
        self.assertEqual(
            [row["holder_address"] for row in snap["rows"]],
            [norm_address("WalletBig"), norm_address("WalletSmall")],
        )
        self.assertTrue(all(row["address_kind"] == "owner" for row in snap["rows"]))
        self.assertEqual(snap["rows"][0]["pct_of_supply"], 0.8)
        self.assertTrue(all(row["pct_of_supply"] is not None for row in snap["rows"]))

    def test_rpc_error_returns_none(self) -> None:
        request = httpx.Request("POST", "http://rpc.invalid/secret")
        response = httpx.Response(429, request=request)

        async def rpc(method: str, _params: list) -> dict | None:
            raise httpx.HTTPStatusError("boom", request=request, response=response)

        result = asyncio.run(
            fetch_holder_snapshot(
                "Mint",
                http_url="http://rpc.invalid/secret",
                enabled=True,
                rpc=rpc,
            )
        )
        self.assertIsNone(result)

    def test_listener_side_keeps_dev_when_snapshot_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            listener = HeliusListener(db_path=str(Path(tmp) / "t.db"), api_key="not-a-real-key")
            calls: list[str] = []

            async def fake_fetch(mint: str, **_kwargs: object) -> None:
                calls.append(mint)
                return None

            tx = _load_tx()
            with patch("sources.solana_helius.fetch_holder_snapshot", fake_fetch):
                with patch("sources.solana_helius.HOLDER_SNAPSHOT_ENABLED", False):
                    side = asyncio.run(listener._intel_side(tx, [_MINT], "sig-listen"))
            self.assertEqual(calls, [])
            self.assertEqual(
                side["deployers"].get(norm_address(_MINT)),
                norm_address(_AUTH),
            )
            self.assertEqual(side["snapshots"], {})

            with patch("sources.solana_helius.fetch_holder_snapshot", fake_fetch):
                with patch("sources.solana_helius.HOLDER_SNAPSHOT_ENABLED", True):
                    side_on = asyncio.run(listener._intel_side(tx, [_MINT], "sig-listen"))
            self.assertEqual(calls, [_MINT])
            self.assertEqual(side_on["snapshots"], {})


class ObserveTests(unittest.TestCase):
    def test_daily_check_never_fails_on_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            store = Store(path)
            empty = CheckReport()
            check_intel_loop_i1(store, empty)
            enqueue_solana_mints(
                store,
                [_MINT],
                signature="sig",
                emit_new_listing=False,
                intel={
                    "deployers": {_MINT: _AUTH},
                    "snapshots": {_MINT: _snapshot()},
                },
            )
            filled = CheckReport()
            check_intel_loop_i1(store, filled)
            store._conn.execute("DROP TABLE token_holder_snapshots")
            store._conn.commit()
            broken = CheckReport()
            check_intel_loop_i1(store, broken)
            fresh = Store(Path(tmp) / "fresh.db")
            ctx = build_context(fresh, top_n=1, alerts_n=1, history_pairs=1)
            fresh.close()
            store.close()

        self.assertEqual(empty.checks[0]["level"], "PASS")
        self.assertEqual(empty.checks[0]["name"], INTEL_I1_CHECK_NAME)
        self.assertFalse(empty.checks[0]["metrics"]["counts_toward_strategy_validity"])
        self.assertEqual(empty.checks[0]["metrics"]["evidence_role"], "10-19旁证")
        self.assertEqual(empty.checks[0]["metrics"]["labeled_dev_n"], 0)
        self.assertEqual(filled.checks[0]["level"], "PASS")
        self.assertEqual(filled.checks[0]["metrics"]["labeled_dev_n"], 1)
        self.assertEqual(filled.checks[0]["metrics"]["holder_snapshot_n"], 1)
        self.assertEqual(filled.checks[0]["metrics"]["tokens_with_top10"], 1)
        self.assertNotEqual(filled.checks[0]["level"], "FAIL")
        self.assertEqual(broken.checks[0]["level"], "WARN")
        self.assertNotEqual(broken.checks[0]["level"], "FAIL")
        self.assertEqual(ctx["stats"]["labeled_dev_n"], 0)
        self.assertEqual(ctx["stats"]["holder_snapshot_n"], 0)
        self.assertEqual(ctx["stats"]["tokens_with_top10"], 0)
        self.assertIn("labeled_dev_n=0", format_intel_observe_line(ctx["intel_observe"]))

    def test_hourly_line_is_side_evidence(self) -> None:
        ctx = {
            "generated_at": "2026-10-14T00:00:00Z",
            "enabled_chains": ["solana"],
            "health": {"ok": True, "message": "OK"},
            "stats": {
                "pending_enrich": 1,
                "g13_high_apy_n": 0,
                "labeled_dev_n": 2,
                "holder_snapshot_n": 3,
                "tokens_with_top10": 3,
            },
            "hot_liquidity_1h": {
                "n": 1,
                "missing_rate": 0.0,
                "below_min_rate": 0.0,
                "ge_min_rate": 1.0,
            },
            "intel_observe": {
                "available": True,
                "labeled_dev_n": 2,
                "holder_snapshot_n": 3,
                "tokens_with_top10": 3,
            },
            "top_scores": [],
            "low_scores": [],
            "history_samples": [],
            "recent_alerts": [],
        }
        md = to_markdown(ctx)
        self.assertLess(md.index("G-13 high APY"), md.index("I-1 observe"))
        self.assertLess(md.index("I-1 observe"), md.index("## Stats"))
        self.assertIn("labeled_dev_n=2", md)
        self.assertIn("holder_snapshot_n=3", md)
        self.assertIn("tokens_with_top10=3", md)
        self.assertIn("不计有效性", md)
        self.assertIn("unavailable", format_intel_observe_line(None))
        self.assertIn("unavailable", to_markdown({**ctx, "intel_observe": None}))


if __name__ == "__main__":
    unittest.main()
