"""Reversible enrich batch cap + segmented heartbeat. Defaults stay off."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import pipeline
from heartbeat import check_health, write_heartbeat
from main import _print_summary
from pipeline import _empty_summary, _finalize_ops, _run_enrich_async
from store import Store

_WATCH_PATH = Path(__file__).resolve().parent.parent / "scripts" / "heartbeat_gap_watch.py"
_spec = importlib.util.spec_from_file_location("hgw_cap", _WATCH_PATH)
assert _spec is not None and _spec.loader is not None
hgw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hgw)


class _Client:
    def __init__(self, delay: float = 0.0, writes: list | None = None) -> None:
        self.delay = delay
        self.batches: list[list[str]] = []
        self.writes = writes
        self.writes_during_fetch = 0

    async def fetch_tokens_batch(self, chain_id: str, batch: list[str]) -> list[dict]:
        before = len(self.writes) if self.writes is not None else 0
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.writes is not None:
            self.writes_during_fetch += len(self.writes) - before
        self.batches.append(list(batch))
        return []


def _summary() -> dict:
    summary = _empty_summary(False, "cycle-test", False, fetch_lists=False, cycle_number=1)
    return summary


def _addrs(n: int) -> list[str]:
    return [f"addr{i:04d}" for i in range(n)]


class EnvParseTests(unittest.TestCase):
    def test_int_and_flag_safe_when_unset_blank_or_garbage(self) -> None:
        saved = {k: os.environ.get(k) for k in (
            "ENRICH_BATCHES_PER_CYCLE_CAP",
            "ENRICH_SEGMENT_HEARTBEAT",
            "ENRICH_HEARTBEAT_MIN_INTERVAL_SEC",
            "ENRICH_HEARTBEAT_EVERY_K_BATCHES",
        )}
        try:
            os.environ.pop("ENRICH_BATCHES_PER_CYCLE_CAP", None)
            self.assertEqual(config._env_int("ENRICH_BATCHES_PER_CYCLE_CAP", 0), 0)
            os.environ["ENRICH_BATCHES_PER_CYCLE_CAP"] = "  "
            self.assertEqual(config._env_int("ENRICH_BATCHES_PER_CYCLE_CAP", 0), 0)
            os.environ["ENRICH_BATCHES_PER_CYCLE_CAP"] = "nope"
            self.assertEqual(config._env_int("ENRICH_BATCHES_PER_CYCLE_CAP", 0), 0)
            os.environ["ENRICH_BATCHES_PER_CYCLE_CAP"] = "20"
            self.assertEqual(config._env_int("ENRICH_BATCHES_PER_CYCLE_CAP", 0), 20)

            os.environ.pop("ENRICH_SEGMENT_HEARTBEAT", None)
            self.assertFalse(config._env_flag("ENRICH_SEGMENT_HEARTBEAT", False))
            os.environ["ENRICH_SEGMENT_HEARTBEAT"] = ""
            self.assertFalse(config._env_flag("ENRICH_SEGMENT_HEARTBEAT", False))
            os.environ["ENRICH_SEGMENT_HEARTBEAT"] = "1"
            self.assertTrue(config._env_flag("ENRICH_SEGMENT_HEARTBEAT", False))
            os.environ["ENRICH_SEGMENT_HEARTBEAT"] = "yes"
            self.assertTrue(config._env_flag("ENRICH_SEGMENT_HEARTBEAT", False))

            os.environ.pop("ENRICH_HEARTBEAT_MIN_INTERVAL_SEC", None)
            self.assertEqual(config._env_int("ENRICH_HEARTBEAT_MIN_INTERVAL_SEC", 30), 30)
            os.environ.pop("ENRICH_HEARTBEAT_EVERY_K_BATCHES", None)
            self.assertEqual(config._env_int("ENRICH_HEARTBEAT_EVERY_K_BATCHES", 10), 10)
            os.environ["ENRICH_HEARTBEAT_EVERY_K_BATCHES"] = "0"
            self.assertEqual(config._env_int("ENRICH_HEARTBEAT_EVERY_K_BATCHES", 10), 0)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_imported_defaults_match_draft_and_stay_off(self) -> None:
        if os.environ.get("ENRICH_BATCHES_PER_CYCLE_CAP", "").strip():
            self.skipTest("operator env overrides cap default")
        if os.environ.get("ENRICH_SEGMENT_HEARTBEAT", "").strip():
            self.skipTest("operator env overrides segment default")
        self.assertEqual(config.ENRICH_BATCHES_PER_CYCLE_CAP, 0)
        self.assertFalse(config.ENRICH_SEGMENT_HEARTBEAT)
        if not os.environ.get("ENRICH_HEARTBEAT_MIN_INTERVAL_SEC", "").strip():
            self.assertEqual(config.ENRICH_HEARTBEAT_MIN_INTERVAL_SEC, 30)
        if not os.environ.get("ENRICH_HEARTBEAT_EVERY_K_BATCHES", "").strip():
            self.assertEqual(config.ENRICH_HEARTBEAT_EVERY_K_BATCHES, 10)
        self.assertEqual(config.PENDING_ENRICH_LOAD_CAP, int(os.environ.get("PENDING_ENRICH_LOAD_CAP", "300")))


class HeartbeatPayloadTests(unittest.TestCase):
    def test_default_payload_has_no_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heartbeat.json"
            write_heartbeat(
                cycle_id="c1",
                status="ok",
                stats={"cycle_number": 3, "enrich_batches": 2},
                path=path,
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("phase", data)
            self.assertEqual(data["status"], "ok")
            self.assertEqual(
                set(data["stats"]),
                {
                    "cycle_number",
                    "tokens_total",
                    "pairs_total",
                    "enrich_batches",
                    "validation_errors",
                    "alerts_emitted",
                    "enrich_pending_remaining",
                    "interrupted",
                },
            )
            self.assertEqual(data["stats"]["enrich_batches"], 2)
            self.assertEqual(data["stats"]["cycle_number"], 3)

    def test_phase_and_batch_progress_are_optional(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heartbeat.json"
            write_heartbeat(
                cycle_id="c1",
                status="ok",
                stats={"enrich_batches": 4, "cycle_number": 1},
                phase="enriching",
                extra_stats={"batches_done": 4, "batches_total_est": 9},
                path=path,
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["phase"], "enriching")
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["stats"]["batches_done"], 4)
            self.assertEqual(data["stats"]["batches_total_est"], 9)
            self.assertEqual(data["stats"]["enrich_batches"], 4)

            write_heartbeat(
                cycle_id="c1",
                status="ok",
                stats={"enrich_batches": 0},
                phase="enriching",
                extra_stats={"batches_done": 0, "batches_total_est": None},
                path=path,
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["stats"]["batches_done"], 0)
            self.assertNotIn("batches_total_est", data["stats"])

    def test_check_health_ignores_phase_and_still_uses_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heartbeat.json"
            write_heartbeat(
                cycle_id="c1",
                status="ok",
                stats={"enrich_batches": 1},
                phase="enriching",
                extra_stats={"batches_done": 1, "batches_total_est": 3},
                path=path,
            )
            ok, message, hb = check_health(stale_sec=600, path=path)
            self.assertTrue(ok)
            self.assertIn("status=ok", message)
            self.assertEqual(hb["phase"], "enriching")

            stale = {
                "timestamp": "2020-01-01T00:00:00Z",
                "cycle_id": "old",
                "status": "ok",
                "phase": "enriching",
                "stats": {"batches_done": 1},
            }
            path.write_text(json.dumps(stale), encoding="utf-8")
            ok, message, _hb = check_health(stale_sec=600, path=path)
            self.assertFalse(ok)
            self.assertIn("stale", message)

    def test_gap_watch_uses_timestamp_not_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / "heartbeat.json"
            csv = Path(tmp) / "gaps.csv"
            write_heartbeat(
                cycle_id="c1",
                status="ok",
                phase="enriching",
                extra_stats={"batches_done": 2, "batches_total_est": 4},
                path=hb,
            )
            now = datetime.now(timezone.utc)
            self.assertEqual(hgw.check_once(hb, csv, 600, now + timedelta(seconds=30)), "ok")
            hb.write_text(
                json.dumps(
                    {
                        "timestamp": (now - timedelta(seconds=700)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "status": "ok",
                        "phase": "enriching",
                        "stats": {"batches_done": 2},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                hgw.check_once(hb, csv, 600, now, Path(tmp) / "state.json"),
                "opened",
            )


class EnrichCapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            "BATCH_SIZE": pipeline.BATCH_SIZE,
            "ENRICH_BATCHES_PER_CYCLE_CAP": getattr(pipeline, "ENRICH_BATCHES_PER_CYCLE_CAP", None),
            "ENRICH_SEGMENT_HEARTBEAT": getattr(pipeline, "ENRICH_SEGMENT_HEARTBEAT", None),
            "ENRICH_HEARTBEAT_MIN_INTERVAL_SEC": getattr(pipeline, "ENRICH_HEARTBEAT_MIN_INTERVAL_SEC", None),
            "ENRICH_HEARTBEAT_EVERY_K_BATCHES": getattr(pipeline, "ENRICH_HEARTBEAT_EVERY_K_BATCHES", None),
            "write_heartbeat": pipeline.write_heartbeat,
        }
        pipeline.ENRICH_SEGMENT_HEARTBEAT = False
        pipeline.ENRICH_HEARTBEAT_MIN_INTERVAL_SEC = 30
        pipeline.ENRICH_HEARTBEAT_EVERY_K_BATCHES = 10

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if key == "write_heartbeat":
                pipeline.write_heartbeat = value
            elif value is not None or hasattr(pipeline, key):
                setattr(pipeline, key, value)

    def _run(self, n: int, *, batch_size: int, cap: int, flush: bool, client: _Client | None = None):
        pipeline.BATCH_SIZE = batch_size
        pipeline.ENRICH_BATCHES_PER_CYCLE_CAP = cap
        pipeline.ENRICH_SEGMENT_HEARTBEAT = False
        addrs = _addrs(n)
        client = client or _Client()
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            try:
                for addr in addrs:
                    store.add_pending_enrich("solana", addr)
                summary = _summary()
                summary["flush_pending"] = flush
                ok = asyncio.run(
                    _run_enrich_async(
                        client,
                        store,
                        {"solana": list(addrs)},
                        summary,
                        flush,
                        None,
                    )
                )
                pending = store.list_pending_enrich()
                pending_addrs = pending.get("solana", [])
                return ok, summary, client, pending_addrs
            finally:
                store.close()

    def test_cap_off_sends_every_planned_batch(self) -> None:
        ok, summary, client, pending = self._run(5, batch_size=2, cap=0, flush=False)
        self.assertTrue(ok)
        self.assertEqual(len(client.batches), 2)
        self.assertEqual([len(b) for b in client.batches], [2, 2])
        self.assertEqual(summary["enrich_batches"], 2)
        self.assertEqual(summary["enrich_batches_deferred"], 0)
        self.assertEqual(summary["enrich_batches_capped"], 0)
        self.assertEqual(pending, ["addr0004"])

    def test_cap_truncates_planned_batches_and_keeps_overflow_pending(self) -> None:
        ok, summary, client, pending = self._run(10, batch_size=2, cap=2, flush=False)
        self.assertTrue(ok)
        self.assertEqual(len(client.batches), 2)
        self.assertTrue(all(len(b) == 2 for b in client.batches))
        fetched = [a for batch in client.batches for a in batch]
        self.assertEqual(len(fetched), 4)
        self.assertEqual(summary["enrich_batches"], 2)
        self.assertEqual(summary["enrich_batches_deferred"], 3)
        self.assertEqual(summary["enrich_batches_capped"], 1)
        self.assertEqual(len(pending), 6)
        self.assertTrue(set(fetched).isdisjoint(pending))
        self.assertEqual(set(fetched) | set(pending), set(_addrs(10)))

    def test_flush_pending_still_respects_cap(self) -> None:
        ok, summary, client, pending = self._run(5, batch_size=2, cap=1, flush=True)
        self.assertTrue(ok)
        self.assertEqual(len(client.batches), 1)
        self.assertEqual(len(client.batches[0]), 2)
        self.assertEqual(summary["enrich_batches_deferred"], 2)
        self.assertEqual(len(pending), 3)
        self.assertTrue(set(client.batches[0]).isdisjoint(pending))


class SegmentHeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            "BATCH_SIZE": pipeline.BATCH_SIZE,
            "ENRICH_BATCHES_PER_CYCLE_CAP": getattr(pipeline, "ENRICH_BATCHES_PER_CYCLE_CAP", None),
            "ENRICH_SEGMENT_HEARTBEAT": getattr(pipeline, "ENRICH_SEGMENT_HEARTBEAT", None),
            "ENRICH_HEARTBEAT_MIN_INTERVAL_SEC": getattr(pipeline, "ENRICH_HEARTBEAT_MIN_INTERVAL_SEC", None),
            "ENRICH_HEARTBEAT_EVERY_K_BATCHES": getattr(pipeline, "ENRICH_HEARTBEAT_EVERY_K_BATCHES", None),
            "write_heartbeat": pipeline.write_heartbeat,
        }

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if key == "write_heartbeat":
                pipeline.write_heartbeat = value
            elif value is not None or hasattr(pipeline, key):
                setattr(pipeline, key, value)

    def test_segment_off_does_not_write_mid_enrich(self) -> None:
        calls: list[dict] = []

        def _spy(**kwargs):
            calls.append(kwargs)
            return Path("/tmp/unused-heartbeat.json")

        pipeline.write_heartbeat = _spy
        pipeline.BATCH_SIZE = 2
        pipeline.ENRICH_BATCHES_PER_CYCLE_CAP = 0
        pipeline.ENRICH_SEGMENT_HEARTBEAT = False
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            try:
                for addr in _addrs(4):
                    store.add_pending_enrich("solana", addr)
                summary = _summary()
                ok = asyncio.run(
                    _run_enrich_async(
                        _Client(),
                        store,
                        {"solana": _addrs(4)},
                        summary,
                        False,
                        None,
                    )
                )
            finally:
                store.close()
        self.assertTrue(ok)
        self.assertEqual(summary["enrich_batches"], 2)
        self.assertEqual(calls, [])

    def test_every_k_writes_phase_and_monotonic_batches_done(self) -> None:
        pipeline.BATCH_SIZE = 2
        pipeline.ENRICH_BATCHES_PER_CYCLE_CAP = 2
        pipeline.ENRICH_SEGMENT_HEARTBEAT = True
        pipeline.ENRICH_HEARTBEAT_MIN_INTERVAL_SEC = 3600
        pipeline.ENRICH_HEARTBEAT_EVERY_K_BATCHES = 1
        written: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heartbeat.json"

            def _spy(**kwargs):
                written.append(kwargs)
                return write_heartbeat(**kwargs, path=path)

            pipeline.write_heartbeat = _spy
            store = Store(Path(tmp) / "t.db")
            try:
                addrs = _addrs(10)
                for addr in addrs:
                    store.add_pending_enrich("solana", addr)
                summary = _summary()
                ok = asyncio.run(
                    _run_enrich_async(
                        _Client(),
                        store,
                        {"solana": addrs},
                        summary,
                        False,
                        None,
                    )
                )
            finally:
                store.close()
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(ok)
        self.assertEqual(summary["enrich_batches"], 2)
        self.assertEqual(summary["enrich_batches_deferred"], 3)
        phases = [row.get("phase") for row in written]
        self.assertTrue(phases)
        self.assertTrue(all(p == "enriching" for p in phases))
        done = [row["extra_stats"]["batches_done"] for row in written]
        self.assertEqual(done, sorted(done))
        self.assertEqual(done[-1], 2)
        self.assertTrue(all(row["extra_stats"]["batches_total_est"] == 2 for row in written))
        self.assertTrue(all(row["status"] == "ok" for row in written))
        self.assertEqual(data["phase"], "enriching")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["stats"]["batches_done"], 2)
        self.assertEqual(data["stats"]["batches_total_est"], 2)

    def test_interval_advances_timestamp_during_slow_fetch(self) -> None:
        pipeline.BATCH_SIZE = 2
        pipeline.ENRICH_BATCHES_PER_CYCLE_CAP = 0
        pipeline.ENRICH_SEGMENT_HEARTBEAT = True
        pipeline.ENRICH_HEARTBEAT_MIN_INTERVAL_SEC = 0.05
        pipeline.ENRICH_HEARTBEAT_EVERY_K_BATCHES = 0
        writes: list[float] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heartbeat.json"

            def _spy(**kwargs):
                writes.append(time.monotonic())
                return write_heartbeat(**kwargs, path=path)

            pipeline.write_heartbeat = _spy
            client = _Client(delay=0.25, writes=writes)
            store = Store(Path(tmp) / "t.db")
            try:
                for addr in _addrs(2):
                    store.add_pending_enrich("solana", addr)
                summary = _summary()
                ok = asyncio.run(
                    _run_enrich_async(
                        client,
                        store,
                        {"solana": _addrs(2)},
                        summary,
                        False,
                        None,
                    )
                )
            finally:
                store.close()
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(ok)
        self.assertGreaterEqual(client.writes_during_fetch, 1)
        self.assertEqual(data["phase"], "enriching")
        self.assertEqual(data["status"], "ok")
        self.assertIn("batches_done", data["stats"])
        age = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
        ).total_seconds()
        self.assertLess(age, 600)

    def test_finalize_clears_enriching_phase_only_when_segment_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heartbeat.json"
            store = Store(Path(tmp) / "t.db")
            try:
                summary = _summary()
                summary["enrich_batches"] = 4
                summary["enrich_batches_total_est"] = 4

                def _spy(**kwargs):
                    return write_heartbeat(**kwargs, path=path)

                pipeline.write_heartbeat = _spy
                pipeline.ENRICH_SEGMENT_HEARTBEAT = False
                _finalize_ops(store, summary, status="ok")
                off = json.loads(path.read_text(encoding="utf-8"))
                self.assertNotIn("phase", off)

                pipeline.ENRICH_SEGMENT_HEARTBEAT = True
                _finalize_ops(store, summary, status="ok")
                on = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(on["phase"], "done")
                self.assertEqual(on["status"], "ok")
                self.assertEqual(on["stats"]["batches_done"], 4)
                self.assertEqual(on["stats"]["batches_total_est"], 4)
            finally:
                store.close()


class SummaryPrintTests(unittest.TestCase):
    def test_deferred_line_only_when_cap_fired(self) -> None:
        summary = _empty_summary(False, "c", False, fetch_lists=False, cycle_number=1)
        summary["enrich_batches_deferred"] = 0
        with tempfile.TemporaryDirectory():
            buf = io.StringIO()
            with redirect_stdout(buf):
                _print_summary(summary)
            self.assertNotIn("batches deferred", buf.getvalue())
            summary["enrich_batches_deferred"] = 3
            summary["enrich_batches_capped"] = 1
            buf = io.StringIO()
            with redirect_stdout(buf):
                _print_summary(summary)
            self.assertIn("batches deferred (cap): 3", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
