"""Collection-layer CODE SWEEP checks. No live keys, network, or production DBs."""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import httpx

from alerts_telegram import TelegramAlerter
from logging_setup import _RedactFilter, redact_secrets
from sources.ccxt.core import candle_is_closed, write_ohlcv
from sources.solana_helius import (
    HeliusListener,
    choose_mint_hints,
    extract_mints_from_logs,
    format_get_tx_failure,
)
from store import Store, has_dex_profile


_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
_DEMO_MINT = "He1iusDemoMintAAAA1111111111111111111111111"


class RedactFilterTests(unittest.TestCase):
    def test_handler_filter_hides_api_key_and_bot_path(self) -> None:
        logger = logging.getLogger("dexscreener.sweep.redact")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.INFO)
        lines: list[str] = []

        class _Cap(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                lines.append(self.format(record))

        handler = _Cap()
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.addFilter(_RedactFilter())
        logger.addHandler(handler)

        secret = "DUMMYVALUE"
        try:
            raise RuntimeError(
                f"Client error for url 'https://mainnet.example/?api-key={secret}'"
            )
        except RuntimeError as exc:
            logger.warning("helius session error err=%s", exc)
        logger.error("telegram url https://api.telegram.org/bot123456:ABCDEF/sendMessage")

        self.assertEqual(len(lines), 2)
        joined = "\n".join(lines)
        self.assertNotIn(secret, joined)
        self.assertNotIn("123456:ABCDEF", joined)
        self.assertIn("api-key=<REDACTED>", joined)
        self.assertIn("/bot<REDACTED>/", joined)
        self.assertEqual(
            redact_secrets(f"api_key={secret}&x=1"),
            "api_key=<REDACTED>&x=1",
        )

    def test_get_transaction_failure_omits_url(self) -> None:
        secret = "DUMMYVALUE"
        request = httpx.Request(
            "POST", f"https://mainnet.example/?api-key={secret}"
        )
        response = httpx.Response(429, request=request)
        exc = httpx.HTTPStatusError("boom", request=request, response=response)
        text = format_get_tx_failure(exc)
        self.assertEqual(text, "status=429")
        self.assertNotIn(secret, text)
        self.assertNotIn("http", text)
        other = format_get_tx_failure(RuntimeError(f"api-key={secret}"))
        self.assertEqual(other, "RuntimeError")
        self.assertNotIn(secret, other)


class ProfilePreserveTests(unittest.TestCase):
    def test_clue_does_not_overwrite_dex_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            profile = {
                "chainId": "solana",
                "tokenAddress": "mint",
                "name": "Demo",
                "symbol": "DEMO",
                "description": "A real token blurb",
                "links": [{"type": "website", "url": "https://example.invalid"}],
            }
            self.assertTrue(has_dex_profile(profile))
            store.upsert_token("solana", "Mint", profile, "dexscreener")
            before = store._conn.execute(
                "SELECT raw_json, source, last_seen_at FROM tokens"
            ).fetchone()
            result = store.upsert_token(
                "solana",
                "mint",
                {
                    "description": "helius logsSubscribe hint sig=abc",
                    "source_hint": "helius",
                },
                "source:helius",
                clue=True,
            )
            self.assertEqual(result, "preserved")
            after = store._conn.execute(
                "SELECT raw_json, source, last_seen_at, clue_note FROM tokens"
            ).fetchone()
            self.assertEqual(after["raw_json"], before["raw_json"])
            self.assertEqual(after["source"], "dexscreener")
            self.assertEqual(after["last_seen_at"], before["last_seen_at"])
            self.assertIn("source:helius", after["clue_note"])
            store.add_pending_enrich("solana", "mint")
            self.assertEqual(store.count_pending_enrich(), 1)
            store.close()


class TierMigrationTests(unittest.TestCase):
    def test_backfill_runs_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            store = Store(path)
            store.close()
            conn = sqlite3.connect(path)
            conn.execute(
                """
                INSERT INTO tokens (
                    chain_id, token_address, raw_json, source, updated_at, last_seen_at
                ) VALUES ('solana', 'late', '{}', 'dex', '2020-01-01T00:00:00Z', NULL)
                """
            )
            conn.commit()
            conn.close()
            again = Store(path)
            row = again._conn.execute(
                "SELECT last_seen_at FROM tokens WHERE token_address = 'late'"
            ).fetchone()
            flag = again.get_meta("tier_ts_backfill_v1")
            again.close()
            self.assertIsNone(row["last_seen_at"])
            self.assertTrue(flag)

    def test_open_ro_skips_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.db"
            with self.assertRaises(FileNotFoundError):
                Store.open_ro(missing)
            self.assertFalse(missing.exists())
            path = Path(tmp) / "t.db"
            Store(path).close()
            ro = Store.open_ro(path)
            self.assertTrue(ro.readonly)
            self.assertFalse(ro._conn.in_transaction)
            ro.close()


class CounterTests(unittest.TestCase):
    def test_bump_is_upsert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            store._conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('counter_cycles_ok', 'nope')"
            )
            store._conn.commit()
            self.assertEqual(store.bump_counter("cycles_ok", 2), 2)
            self.assertEqual(store.bump_counter("cycles_ok", 3), 5)
            self.assertEqual(store.bump_cycle_number(), 1)
            self.assertEqual(store.bump_cycle_number(), 2)
            store.close()


class PendingQueueTests(unittest.TestCase):
    def test_load_cap_and_set_dedupe(self) -> None:
        from pipeline import _build_queues

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            for i in range(5):
                store.add_pending_enrich("solana", f"addr{i}")
            os.environ["PENDING_ENRICH_LOAD_CAP"] = "2"
            import config

            old = config.PENDING_ENRICH_LOAD_CAP
            config.PENDING_ENRICH_LOAD_CAP = 2
            import pipeline

            pipeline.PENDING_ENRICH_LOAD_CAP = 2
            try:
                summary: dict = {}
                queues = _build_queues(
                    store,
                    {"solana": ["addr0", "addr9"]},
                    summary,
                )
            finally:
                config.PENDING_ENRICH_LOAD_CAP = old
                pipeline.PENDING_ENRICH_LOAD_CAP = old
            self.assertEqual(summary["enrich_from_pending"], 5)
            self.assertLessEqual(summary["enrich_pending_loaded"], 2)
            self.assertEqual(len(queues["solana"]), len(set(queues["solana"])))
            self.assertIn("addr9", queues["solana"])
            store.close()


class XWatchTests(unittest.TestCase):
    def test_new_token_not_stuck_behind_fifo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["X_WATCH_MAX_ROWS"] = "50"
            store = Store(Path(tmp) / "t.db")
            for i in range(3):
                store.upsert_x_watch("solana", f"old{i}", query_key=f"q{i}", mode="early")
                store._conn.execute(
                    """
                    UPDATE x_watch
                    SET last_searched_at = '2020-01-01T00:00:00Z',
                        mention_count_total = 1
                    WHERE token_address = ?
                    """,
                    (f"old{i}",),
                )
            store._conn.commit()
            store.upsert_x_watch("solana", "brandnew", query_key="new", mode="early")
            rows = store.list_x_watch(limit=1)
            self.assertEqual(rows[0]["token_address"], "brandnew")
            store._conn.execute(
                "UPDATE x_watch SET expires_at = '2000-01-01T00:00:00Z' WHERE token_address = 'brandnew'"
            )
            store._conn.commit()
            left = store.list_x_watch(limit=10)
            self.assertNotIn("brandnew", [row["token_address"] for row in left])
            store.close()


class CcxtOhlcvTests(unittest.TestCase):
    def test_unclosed_bar_is_dropped_and_closed_updates(self) -> None:
        self.assertFalse(candle_is_closed(1_000_000, "5m", 1_000_000 + 299_999))
        self.assertTrue(candle_is_closed(1_000_000, "5m", 1_000_000 + 300_000))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ccxt.db"
            conn = sqlite3.connect(path)
            from sources.ccxt.core import _SCHEMA

            conn.executescript(_SCHEMA)
            open_ts = 1_700_000_000_000
            now = open_ts + 300_000
            n = write_ohlcv(
                conn,
                "binance",
                "DEMO/USDT",
                "5m",
                [[open_ts, 1, 2, 0.5, 1.5, 10], [now, 9, 9, 9, 9, 1]],
                now,
            )
            self.assertEqual(n, 1)
            row = conn.execute(
                "SELECT close FROM ccxt_ohlcv WHERE ts = ?", (open_ts,)
            ).fetchone()
            self.assertEqual(row[0], 1.5)
            self.assertIsNone(
                conn.execute("SELECT 1 FROM ccxt_ohlcv WHERE ts = ?", (now,)).fetchone()
            )
            write_ohlcv(
                conn,
                "binance",
                "DEMO/USDT",
                "5m",
                [[open_ts, 1, 3, 0.4, 2.5, 12]],
                now + 300_000,
            )
            updated = conn.execute(
                "SELECT close FROM ccxt_ohlcv WHERE ts = ?", (open_ts,)
            ).fetchone()
            self.assertEqual(updated[0], 2.5)
            conn.close()


class HeliusHintTests(unittest.TestCase):
    def test_log_regex_requires_mint_field_and_program(self) -> None:
        noise = "NotAMintNoise111111111111111111111111111111"
        logs = [
            f"Program {_PROGRAM} invoke [1]",
            f"Program log: initialize2 owner={noise}",
            f"Program log: mint {_DEMO_MINT}",
        ]
        found = extract_mints_from_logs(logs, program_ids=[_PROGRAM])
        self.assertEqual(found, [_DEMO_MINT])
        self.assertEqual(
            extract_mints_from_logs(logs, program_ids=["NotWhitelisted111111111111111111111111111"]),
            [],
        )
        mints, emit = choose_mint_hints(["FromTxMint11111111111111111111111111111111"], found)
        self.assertTrue(emit)
        self.assertEqual(len(mints), 1)
        hints, emit_logs = choose_mint_hints([], found)
        self.assertFalse(emit_logs)
        self.assertEqual(hints, found)

    def test_writer_queue_inserts_without_blocking_caller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            listener = HeliusListener(db_path=str(path), api_key="not-used")
            listener._jobs.put(("mints", [_DEMO_MINT], "sig", False))
            listener._jobs.put(None)
            listener._writer_loop()
            store = Store(path)
            self.assertTrue(store.token_exists("solana", _DEMO_MINT))
            self.assertEqual(store.count_alerts(), 0)
            store.close()


class TelegramBanTests(unittest.TestCase):
    def test_ban_survives_new_alerter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ban_db = str(Path(tmp) / "tg_ban.sqlite")
            body = {
                "ok": False,
                "error_code": 429,
                "parameters": {"retry_after": 5000},
            }

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(429, json=body, request=request)

            transport = httpx.MockTransport(handler)
            first = TelegramAlerter(
                token="000:TEST",
                transport=transport,
                sleep_fn=lambda _s: None,
                ban_db=ban_db,
            )
            import alerts_telegram as mod

            old = mod.TG_429_SLEEP_CAP_SEC
            mod.TG_429_SLEEP_CAP_SEC = 30.0
            try:
                self.assertFalse(first.send_message("-100", "hi"))
            finally:
                mod.TG_429_SLEEP_CAP_SEC = old
            second = TelegramAlerter(
                token="000:TEST",
                transport=transport,
                sleep_fn=lambda _s: None,
                ban_db=ban_db,
            )
            remaining = second._gate("-100").banned_remaining()
            self.assertGreater(remaining, 1000)
            calls = {"n": 0}

            def counting(request: httpx.Request) -> httpx.Response:
                calls["n"] += 1
                return httpx.Response(200, json={"ok": True, "result": {}}, request=request)

            second._transport = httpx.MockTransport(counting)
            mod.TG_429_SLEEP_CAP_SEC = 30.0
            try:
                second.send_message("-100", "again")
            finally:
                mod.TG_429_SLEEP_CAP_SEC = old
            self.assertEqual(calls["n"], 0)


class BackupAndApiTests(unittest.TestCase):
    def test_backup_skips_missing_sidecars(self) -> None:
        import backup

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = root / "dexscreener.db"
            conn = sqlite3.connect(main)
            conn.execute("CREATE TABLE t (id INTEGER)")
            conn.commit()
            conn.close()
            paper = root / "paper.db"
            conn = sqlite3.connect(paper)
            conn.execute("CREATE TABLE t (id INTEGER)")
            conn.commit()
            conn.close()
            old_paper = backup.PAPER_DB_PATH
            old_prior = os.environ.get("PRIOR_DB_PATH")
            old_ledger = os.environ.get("EXECUTION_LEDGER_PATH")
            backup.PAPER_DB_PATH = str(paper)
            os.environ["PRIOR_DB_PATH"] = str(root / "missing-prior.db")
            os.environ["EXECUTION_LEDGER_PATH"] = str(root / "missing-ledger.db")
            try:
                dest = backup.backup_db(main, root / "backups", retain=2)
            finally:
                backup.PAPER_DB_PATH = old_paper
                if old_prior is None:
                    os.environ.pop("PRIOR_DB_PATH", None)
                else:
                    os.environ["PRIOR_DB_PATH"] = old_prior
                if old_ledger is None:
                    os.environ.pop("EXECUTION_LEDGER_PATH", None)
                else:
                    os.environ["EXECUTION_LEDGER_PATH"] = old_ledger
            names = sorted(p.name for p in (root / "backups").glob("*.db"))
            self.assertTrue(dest.name.startswith("dexscreener-"))
            self.assertTrue(any(name.startswith("paper-") for name in names))
            self.assertFalse(any(name.startswith("prior-") for name in names))
            self.assertFalse(any(name.startswith("execution_ledger-") for name in names))

    def test_api_key_compare_bytes(self) -> None:
        from api_server import api_keys_match

        self.assertTrue(api_keys_match("café-key", "café-key"))
        self.assertFalse(api_keys_match("café-key", "cafe-key"))
        self.assertFalse(api_keys_match(None, "x"))


if __name__ == "__main__":
    unittest.main()
