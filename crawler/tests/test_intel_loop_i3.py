"""INTEL_LOOP I-3 bot markers on the collection side. Synthetic fixtures only.

No live Helius key, network, or production DB. Counts are observe-only.
They do not count toward strategy validity and do not skip orders.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from daily_check import (
    INTEL_I1_CHECK_NAME,
    INTEL_I3_CHECK_NAME,
    CheckReport,
    check_intel_loop_i1,
    check_intel_loop_i3,
)
from sources.bot_markers import (
    ABNORMAL_CU_PRICE_MICROLAMPORTS,
    BOT_SUSPECT_LABEL,
    classify_bot_fills,
    extract_bot_observations,
)
from sources.holder_snapshot import flag_enabled
from sources.solana_helius import HeliusListener, enqueue_solana_mints
from store import Store, format_bot_observe_line, norm_address
from tools.grok_context import build_context, to_markdown

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "helius_get_transaction.json"
_MINT = "BotMint111111111111111111111111111111111111"
_OTHER = "OtherMint22222222222222222222222222222222"
_WALLET = "BotWallet333333333333333333333333333333333"
_RECORDED = "2026-10-14T00:00:00Z"
_COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"


def _load_tx() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _swap_tx(
    *,
    wallet: str = _WALLET,
    mint: str = _MINT,
    slot: int | None = 100,
    block_time: int | None = 1_700_000_000,
    cu_price: int | str | None = None,
    cu_price_values: list[object] | None = None,
    inner_cu_price: int | None = None,
    pre: str | None = "0",
    post: str | None = "10",
    fee: int | None = 5000,
    consumed: int | None = None,
    signer: bool | None = True,
    owner: str | None = None,
) -> dict:
    instructions: list[dict] = []
    prices = cu_price_values
    if prices is None and cu_price is not None:
        prices = [cu_price]
    for price in prices or []:
        instructions.append(
            {
                "programId": _COMPUTE_BUDGET,
                "parsed": {
                    "type": "setComputeUnitPrice",
                    "info": {"microLamports": price},
                },
            }
        )
    inner: list[dict] = []
    if inner_cu_price is not None:
        inner.append(
            {
                "instructions": [
                    {
                        "programId": _COMPUTE_BUDGET,
                        "parsed": {
                            "type": "setComputeUnitPrice",
                            "info": {"microLamports": inner_cu_price},
                        },
                    }
                ]
            }
        )
    key: dict[str, object] = {"pubkey": wallet, "writable": True}
    if signer is not None:
        key["signer"] = signer
    pre_rows = []
    post_rows = []
    if pre is not None:
        pre_rows.append(
            {
                "mint": mint,
                "owner": owner or wallet,
                "uiTokenAmount": {"amount": pre, "decimals": 6},
            }
        )
    if post is not None:
        post_rows.append(
            {
                "mint": mint,
                "owner": owner or wallet,
                "uiTokenAmount": {"amount": post, "decimals": 6},
            }
        )
    meta: dict[str, object] = {
        "fee": fee,
        "preTokenBalances": pre_rows,
        "postTokenBalances": post_rows,
        "innerInstructions": inner,
    }
    if consumed is not None:
        meta["computeUnitsConsumed"] = consumed
    tx: dict[str, object] = {
        "transaction": {
            "signatures": ["sig"],
            "message": {"accountKeys": [key], "instructions": instructions},
        },
        "meta": meta,
    }
    if slot is not None:
        tx["slot"] = slot
    if block_time is not None:
        tx["blockTime"] = block_time
    return tx


def _obs(**overrides: object) -> dict:
    row: dict[str, object] = {
        "chain_id": "solana",
        "signature": "sig-a",
        "wallet": _WALLET,
        "token_address": _MINT,
        "slot": None,
        "block_time": None,
        "recorded_at": _RECORDED,
        "fee_lamports": 9_000_000,
        "compute_unit_price": None,
        "compute_unit_limit": None,
        "priority_fee_lamports": None,
        "source": "helius:getTransaction",
    }
    row.update(overrides)
    return row


def _edge(signature: str, *, token: str = _MINT) -> dict:
    return {
        "chain_id": "solana",
        "token_address": token,
        "from_address": "FromAcct",
        "to_address": "ToAcct",
        "amount_raw": "1",
        "amount_ui": 1.0,
        "signature": signature,
        "recorded_at": _RECORDED,
        "source": "helius:getTransaction",
    }


def _enqueue(
    store: Store,
    observations: list[dict],
    edges: list[dict] | None = None,
    mints: list[str] | None = None,
) -> None:
    enqueue_solana_mints(
        store,
        mints or [_MINT],
        signature="batch",
        emit_new_listing=False,
        intel={"transfers": edges or [], "bot_observations": observations},
    )


class FlagAndExtractTests(unittest.TestCase):
    def test_flag_defaults_on_like_holder_snapshot(self) -> None:
        self.assertTrue(flag_enabled(None))
        self.assertFalse(flag_enabled("0"))
        self.assertEqual(
            config.BOT_MARKERS_ENABLED,
            flag_enabled(os.environ.get("BOT_MARKERS_ENABLED")),
        )

    def test_i1_fixture_has_no_fee_payer_so_no_observation(self) -> None:
        self.assertEqual(
            extract_bot_observations(
                _load_tx(),
                signature="sig-i1",
                recorded_at=_RECORDED,
                mints=["He1iusTxMintBBBB222222222222222222222222222"],
            ),
            [],
        )

    def test_missing_balance_signer_or_amount_writes_nothing(self) -> None:
        base = dict(
            signature="sig",
            recorded_at=_RECORDED,
            mints=[_MINT],
        )
        self.assertEqual(extract_bot_observations(_swap_tx(pre="5", post="5"), **base), [])
        self.assertEqual(extract_bot_observations(_swap_tx(signer=False), **base), [])
        self.assertEqual(
            extract_bot_observations(_swap_tx(pre="nope", post="10"), **base),
            [],
        )
        self.assertEqual(
            extract_bot_observations(_swap_tx(owner="SomeoneElse"), **base),
            [],
        )
        self.assertEqual(
            extract_bot_observations(
                _swap_tx(),
                skip_addresses={_WALLET},
                **base,
            ),
            [],
        )

    def test_priority_fields_fail_closed(self) -> None:
        low = extract_bot_observations(
            _swap_tx(cu_price=ABNORMAL_CU_PRICE_MICROLAMPORTS - 1, fee=50_000_000),
            signature="sig-low",
            recorded_at=_RECORDED,
            mints=[_MINT],
        )
        self.assertEqual(len(low), 1)
        self.assertEqual(low[0]["compute_unit_price"], ABNORMAL_CU_PRICE_MICROLAMPORTS - 1)
        self.assertIsNone(low[0]["priority_fee_lamports"])
        self.assertEqual(
            classify_bot_fills(low),
            {},
        )

        priced = extract_bot_observations(
            _swap_tx(
                cu_price=str(ABNORMAL_CU_PRICE_MICROLAMPORTS),
                consumed=200_000,
            ),
            signature="sig-high",
            recorded_at=_RECORDED,
            mints=[_MINT],
        )
        self.assertEqual(priced[0]["compute_unit_price"], ABNORMAL_CU_PRICE_MICROLAMPORTS)
        self.assertEqual(priced[0]["priority_fee_lamports"], 200_000)
        self.assertEqual(priced[0]["wallet"], norm_address(_WALLET))
        self.assertEqual(
            classify_bot_fills(priced),
            {"sig-high": "priority_fee"},
        )

        last_wins = extract_bot_observations(
            _swap_tx(
                cu_price_values=[
                    ABNORMAL_CU_PRICE_MICROLAMPORTS,
                    ABNORMAL_CU_PRICE_MICROLAMPORTS - 1,
                ]
            ),
            signature="sig-last",
            recorded_at=_RECORDED,
            mints=[_MINT],
        )
        self.assertEqual(last_wins[0]["compute_unit_price"], ABNORMAL_CU_PRICE_MICROLAMPORTS - 1)
        self.assertEqual(classify_bot_fills(last_wins), {})

        unparsed_last = extract_bot_observations(
            _swap_tx(cu_price_values=[ABNORMAL_CU_PRICE_MICROLAMPORTS, "nope"]),
            signature="sig-bad",
            recorded_at=_RECORDED,
            mints=[_MINT],
        )
        self.assertIsNone(unparsed_last[0]["compute_unit_price"])
        self.assertEqual(classify_bot_fills(unparsed_last), {})

        inner_only = extract_bot_observations(
            _swap_tx(inner_cu_price=ABNORMAL_CU_PRICE_MICROLAMPORTS),
            signature="sig-inner",
            recorded_at=_RECORDED,
            mints=[_MINT],
        )
        self.assertIsNone(inner_only[0]["compute_unit_price"])
        self.assertEqual(classify_bot_fills(inner_only), {})

        fee_only = extract_bot_observations(
            _swap_tx(fee=50_000_000),
            signature="sig-fee",
            recorded_at=_RECORDED,
            mints=[_MINT],
        )
        self.assertEqual(fee_only[0]["fee_lamports"], 50_000_000)
        self.assertIsNone(fee_only[0]["compute_unit_price"])
        self.assertEqual(classify_bot_fills(fee_only), {})

    def test_float_and_bool_are_not_coerced(self) -> None:
        floated = extract_bot_observations(
            _swap_tx(cu_price_values=[float(ABNORMAL_CU_PRICE_MICROLAMPORTS)]),
            signature="sig-float",
            recorded_at=_RECORDED,
            mints=[_MINT],
        )
        self.assertIsNone(floated[0]["compute_unit_price"])
        self.assertEqual(
            classify_bot_fills(
                [
                    {"signature": "a", "slot": True},
                    {"signature": "b", "slot": True},
                ]
            ),
            {},
        )


class PatternTests(unittest.TestCase):
    def test_same_slot_needs_two_signatures_and_a_slot(self) -> None:
        self.assertEqual(
            classify_bot_fills(
                [
                    _obs(signature="a", slot=7),
                    _obs(signature="b", slot=7),
                ]
            ),
            {"a": "same_slot_repeat", "b": "same_slot_repeat"},
        )
        self.assertEqual(
            classify_bot_fills([_obs(signature="only", slot=7)]),
            {},
        )
        self.assertEqual(
            classify_bot_fills(
                [
                    _obs(signature="a", slot=None),
                    _obs(signature="b", slot=None),
                ]
            ),
            {},
        )

    def test_one_second_gap_is_not_a_millisecond_repeat(self) -> None:
        self.assertEqual(
            classify_bot_fills(
                [
                    _obs(signature="a", slot=1, block_time=1_000),
                    _obs(signature="b", slot=2, block_time=1_001),
                ]
            ),
            {},
        )

    def test_fixed_interval_is_exact_and_windowed(self) -> None:
        self.assertEqual(
            classify_bot_fills(
                [
                    _obs(signature="a", block_time=1_000),
                    _obs(signature="b", block_time=1_010),
                    _obs(signature="c", block_time=1_020),
                ]
            ),
            {
                "a": "fixed_interval",
                "b": "fixed_interval",
                "c": "fixed_interval",
            },
        )
        self.assertEqual(
            classify_bot_fills(
                [
                    _obs(signature="a", block_time=1_000),
                    _obs(signature="b", block_time=1_010),
                    _obs(signature="c", block_time=1_021),
                ]
            ),
            {},
        )
        self.assertEqual(
            classify_bot_fills(
                [
                    _obs(signature="a", block_time=1_000),
                    _obs(signature="b", block_time=1_010),
                ]
            ),
            {},
        )
        self.assertEqual(
            classify_bot_fills(
                [
                    _obs(signature="a", recorded_at="2026-10-14T00:00:00Z"),
                    _obs(signature="b", recorded_at="2026-10-14T00:00:10Z"),
                    _obs(signature="c", recorded_at="2026-10-14T00:00:20Z"),
                ]
            ),
            {},
        )
        outside = classify_bot_fills(
            [
                _obs(signature="old", block_time=0),
                _obs(signature="mid", block_time=1_800),
                _obs(signature="new", block_time=3_601),
            ]
        )
        self.assertEqual(outside, {})
        inside = classify_bot_fills(
            [
                _obs(signature="a", block_time=0),
                _obs(signature="b", block_time=1_800),
                _obs(signature="c", block_time=3_600),
            ]
        )
        self.assertEqual(set(inside), {"a", "b", "c"})

    def test_reasons_merge_without_a_human_label(self) -> None:
        flagged = classify_bot_fills(
            [
                _obs(
                    signature="a",
                    slot=4,
                    compute_unit_price=ABNORMAL_CU_PRICE_MICROLAMPORTS,
                ),
                _obs(signature="b", slot=4),
            ]
        )
        self.assertEqual(flagged["a"], "priority_fee,same_slot_repeat")
        self.assertEqual(flagged["b"], "same_slot_repeat")
        self.assertNotIn("not_bot", flagged["a"])


class StoreFillTests(unittest.TestCase):
    def test_schema_and_priority_fee_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            names = {
                row[0]
                for row in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            cols = {row[1] for row in store._conn.execute("PRAGMA table_info(token_transfer_edges)")}
            self.assertIn("bot_fill_observations", names)
            self.assertIn("bot_reason", cols)
            empty = store.bot_observe_counts()
            self.assertTrue(empty["available"])
            self.assertEqual(empty["bot_suspect_n"], 0)
            self.assertEqual(empty["bot_edge_n"], 0)
            self.assertIsNone(store.bot_share_inputs("solana", _MINT)["share"])

            obs = extract_bot_observations(
                _swap_tx(cu_price=ABNORMAL_CU_PRICE_MICROLAMPORTS, slot=None, block_time=None),
                signature="sig-fee",
                recorded_at=_RECORDED,
                mints=[_MINT],
            )
            _enqueue(store, obs, [_edge("sig-fee")])
            labels = store.list_address_labels(
                "solana",
                token_mint=norm_address(_MINT),
                label_type=BOT_SUSPECT_LABEL,
            )
            self.assertEqual(len(labels), 1)
            self.assertEqual(labels[0]["address"], norm_address(_WALLET))
            self.assertEqual(labels[0]["source"], "helius:bot_suspect:priority_fee")
            counts = store.bot_observe_counts()
            self.assertEqual(counts["bot_suspect_n"], 1)
            self.assertEqual(counts["bot_edge_n"], 1)
            share = store.bot_share_inputs("solana", _MINT)
            self.assertEqual(share["total_fills"], 1)
            self.assertEqual(share["bot_fills"], 1)
            self.assertEqual(share["share"], 1.0)
            self.assertEqual(store.list_address_labels("solana", label_type="dev"), [])
            store.close()

    def test_same_slot_stamps_edges_and_ignores_other_mints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            _enqueue(
                store,
                [
                    _obs(signature="a", slot=9),
                    _obs(signature="b", slot=9),
                    _obs(signature="c", slot=9, token_address=_OTHER),
                ],
                [_edge("a"), _edge("b"), _edge("c", token=_OTHER)],
                mints=[_MINT, _OTHER],
            )
            labels = store.list_address_labels("solana", label_type=BOT_SUSPECT_LABEL)
            self.assertEqual(len(labels), 1)
            self.assertEqual(labels[0]["token_mint"], norm_address(_MINT))
            self.assertEqual(labels[0]["source"], "helius:bot_suspect:same_slot_repeat")
            edges = store._conn.execute(
                "SELECT signature, token_address, bot_reason FROM token_transfer_edges ORDER BY signature"
            ).fetchall()
            by_sig = {row["signature"]: row["bot_reason"] for row in edges}
            self.assertEqual(by_sig["a"], "same_slot_repeat")
            self.assertEqual(by_sig["b"], "same_slot_repeat")
            self.assertIsNone(by_sig["c"])
            self.assertEqual(store.bot_observe_counts()["bot_edge_n"], 2)
            quiet = store.bot_share_inputs("solana", _MINT, since_recorded_at="2026-10-15T00:00:00Z")
            self.assertEqual(quiet["total_fills"], 0)
            self.assertIsNone(quiet["share"])
            store.close()

    def test_unflagged_fill_keeps_null_reason_and_zero_share(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            _enqueue(store, [_obs(signature="once", slot=1, fee_lamports=80_000_000)])
            self.assertEqual(store.list_address_labels("solana", label_type=BOT_SUSPECT_LABEL), [])
            row = store.list_bot_observations("solana", _WALLET, _MINT)[0]
            self.assertIsNone(row["bot_reason"])
            self.assertIsNone(row["block_time"])
            self.assertEqual(row["slot"], 1)
            share = store.bot_share_inputs("solana", _MINT)
            self.assertEqual(share["total_fills"], 1)
            self.assertEqual(share["bot_fills"], 0)
            self.assertEqual(share["share"], 0.0)
            again = store.insert_bot_observations([_obs(signature="once", slot=1)])
            self.assertEqual(again, 1)
            self.assertEqual(len(store.list_bot_observations("solana", _WALLET, _MINT)), 1)
            store.close()

    def test_fixed_interval_and_repeat_upsert_keeps_first_seen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            rows = [
                _obs(signature="a", block_time=2_000, recorded_at="2026-10-14T00:00:00Z"),
                _obs(signature="b", block_time=2_030, recorded_at="2026-10-14T00:00:30Z"),
                _obs(signature="c", block_time=2_060, recorded_at="2026-10-14T00:01:00Z"),
            ]
            _enqueue(store, rows)
            label = store.list_address_labels("solana", label_type=BOT_SUSPECT_LABEL)[0]
            self.assertEqual(label["source"], "helius:bot_suspect:fixed_interval")
            first = label["first_seen_at"]
            _enqueue(store, rows)
            again = store.list_address_labels("solana", label_type=BOT_SUSPECT_LABEL)[0]
            self.assertEqual(again["first_seen_at"], first)
            self.assertEqual(store.bot_share_inputs("solana", _MINT)["total_fills"], 3)
            self.assertEqual(store.bot_observe_counts()["bot_suspect_n"], 1)
            store.close()

    def test_listener_flag_adds_no_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            listener = HeliusListener(db_path=str(Path(tmp) / "t.db"), api_key="not-a-real-key")
            tx = _swap_tx(cu_price=ABNORMAL_CU_PRICE_MICROLAMPORTS)
            calls: list[str] = []

            async def fake_fetch(mint: str, **_kwargs: object) -> None:
                calls.append(mint)
                return None

            with patch("sources.solana_helius.fetch_holder_snapshot", fake_fetch):
                with patch("sources.solana_helius.HOLDER_SNAPSHOT_ENABLED", False):
                    with patch("sources.solana_helius.BOT_MARKERS_ENABLED", False):
                        off = asyncio.run(listener._intel_side(tx, [_MINT], "sig-off"))
                    with patch("sources.solana_helius.BOT_MARKERS_ENABLED", True):
                        on = asyncio.run(listener._intel_side(tx, [_MINT], "sig-on"))
            self.assertEqual(calls, [])
            self.assertNotIn("bot_observations", off)
            self.assertEqual(len(on["bot_observations"]), 1)
            self.assertEqual(on["bot_observations"][0]["signature"], "sig-on")
            self.assertEqual(on["deployers"], {})


class ObserveTests(unittest.TestCase):
    def test_daily_check_and_hourly_line_never_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            store = Store(path)
            empty = CheckReport()
            check_intel_loop_i3(store, empty)
            _enqueue(
                store,
                [
                    _obs(signature="a", slot=3),
                    _obs(signature="b", slot=3),
                ],
                [_edge("a")],
            )
            filled = CheckReport()
            check_intel_loop_i3(store, filled)
            i1 = CheckReport()
            check_intel_loop_i1(store, i1)
            store._conn.execute("DROP TABLE bot_fill_observations")
            store._conn.commit()
            broken = CheckReport()
            check_intel_loop_i3(store, broken)
            i1_after = CheckReport()
            check_intel_loop_i1(store, i1_after)
            fresh = Store(Path(tmp) / "fresh.db")
            ctx = build_context(fresh, top_n=1, alerts_n=1, history_pairs=1)
            fresh.close()
            store.close()

        self.assertEqual(empty.checks[0]["name"], INTEL_I3_CHECK_NAME)
        self.assertEqual(empty.checks[0]["level"], "PASS")
        self.assertFalse(empty.checks[0]["metrics"]["counts_toward_strategy_validity"])
        self.assertFalse(empty.checks[0]["metrics"]["skips_orders"])
        self.assertEqual(empty.checks[0]["metrics"]["evidence_role"], "10-19旁证")
        self.assertEqual(empty.checks[0]["metrics"]["bot_suspect_n"], 0)
        self.assertEqual(empty.checks[0]["metrics"]["bot_edge_n"], 0)
        self.assertEqual(filled.checks[0]["level"], "PASS")
        self.assertEqual(filled.checks[0]["metrics"]["bot_suspect_n"], 1)
        self.assertEqual(filled.checks[0]["metrics"]["bot_edge_n"], 1)
        self.assertNotEqual(filled.checks[0]["level"], "FAIL")
        self.assertEqual(broken.checks[0]["level"], "WARN")
        self.assertNotEqual(broken.checks[0]["level"], "FAIL")
        self.assertEqual(i1.checks[0]["name"], INTEL_I1_CHECK_NAME)
        self.assertEqual(i1.checks[0]["level"], "PASS")
        self.assertEqual(i1_after.checks[0]["level"], "PASS")
        self.assertEqual(ctx["stats"]["bot_suspect_n"], 0)
        self.assertEqual(ctx["stats"]["bot_edge_n"], 0)
        self.assertIn("bot_suspect_n=0", format_bot_observe_line(ctx["bot_observe"]))

        class Boom:
            def bot_observe_counts(self) -> dict:
                raise RuntimeError("boom")

        raised = CheckReport()
        check_intel_loop_i3(Boom(), raised)  # type: ignore[arg-type]
        self.assertEqual(raised.checks[0]["level"], "WARN")
        self.assertNotEqual(raised.checks[0]["level"], "FAIL")

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
                "bot_suspect_n": 4,
                "bot_edge_n": 5,
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
            "bot_observe": {"available": True, "bot_suspect_n": 4, "bot_edge_n": 5},
            "top_scores": [],
            "low_scores": [],
            "history_samples": [],
            "recent_alerts": [],
        }
        md = to_markdown(ctx)
        self.assertLess(md.index("I-1 observe"), md.index("I-3 observe"))
        self.assertLess(md.index("I-3 observe"), md.index("## Stats"))
        self.assertIn("bot_suspect_n=4", md)
        self.assertIn("bot_edge_n=5", md)
        self.assertIn("不计有效性", md)
        self.assertIn("unavailable", format_bot_observe_line(None))
        self.assertIn("unavailable", to_markdown({**ctx, "bot_observe": None}))


if __name__ == "__main__":
    unittest.main()
