"""S2C-FW scaffold: registry, sparse cells, dual stop-loss fields, shared token age."""

from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from review.attribution import resolve_token_age_hours
from review.data import DbPaths, SignalChainRow, parse_ts
from review.report import render_weekly_md
from review.run import main
from review.structure_check import (
    EXECUTION_METRIC_KEYS,
    load_registry,
    structure_check,
)
from review.structure_history import (
    FP_LOG_COLUMNS,
    HISTORY_COLUMNS,
    ensure_fp_log,
    fp_log_row_count,
    load_history,
    upsert_layer_rows,
)

SIGNAL = "2026-10-19T00:00:00Z"
SIGNAL_MS = int(parse_ts(SIGNAL).timestamp() * 1000)
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
START = "2026-10-12T00:00:00Z"
END = "2026-10-19T00:00:00Z"
OPENED = "2026-10-14T00:00:00Z"
CLOSED = "2026-10-14T01:00:00Z"
ROOT = Path(__file__).resolve().parents[1]


def _signal(**overrides: object) -> SignalChainRow:
    fields: dict[str, object] = {
        "decision_id": 1,
        "ts": SIGNAL,
        "strategy_id": "jev_strength_v1",
        "action": "buy",
        "chain_id": "solana",
        "token_address": "token",
        "pair_address": "pair",
        "size_pct": None,
        "reason": None,
        "risk_passed": 1,
        "mode": "dry_run",
        "raw": {},
        "jev_strength": None,
        "exec_id": None,
        "exec_ts": None,
        "validation_ok": None,
        "validation_errors": None,
        "executed": None,
        "executor": None,
        "receipt": {},
        "position_id": None,
        "fill_side": None,
        "fill_price": None,
        "pnl_usd": None,
    }
    fields.update(overrides)
    return SignalChainRow(**fields)  # type: ignore[arg-type]


def _paths(root: Path) -> DbPaths:
    return DbPaths(
        main=str(root / "dexscreener.db"),
        execution=str(root / "execution_ledger.db"),
        paper=str(root / "paper_trading.db"),
        backtest=str(root / "backtest_ledger.db"),
    )


def _touch_dbs(root: Path) -> DbPaths:
    for name in (
        "dexscreener.db",
        "execution_ledger.db",
        "paper_trading.db",
        "backtest_ledger.db",
    ):
        path = root / name
        if not path.exists():
            sqlite3.connect(path).close()
    return _paths(root)


def _paper(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE positions (
            id TEXT, status TEXT, pnl_usd REAL, exit_reason TEXT,
            closed_at TEXT, opened_at TEXT, size_usd REAL,
            max_hold_min REAL, meta_json TEXT, entry_price REAL
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO positions (
            id, status, pnl_usd, exit_reason, closed_at, opened_at,
            size_usd, max_hold_min, meta_json
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


def _meta(slippage: float = 0.01) -> str:
    return json.dumps({"strategy_id": "alpha", "slippage": slippage})


def _closed(
    position_id: str,
    pnl: float,
    reason: str,
    *,
    slippage: float = 0.01,
    size: float = 400,
) -> tuple:
    return (
        position_id,
        "closed",
        pnl,
        reason,
        CLOSED,
        OPENED,
        size,
        60,
        _meta(slippage),
    )


def _run_check(
    root: Path,
    *,
    chain: list[SignalChainRow] | None = None,
    jev_rows: list[SignalChainRow] | None = None,
    history: list[dict] | None = None,
    registry: dict | None = None,
    period_end: str = END,
):
    return structure_check(
        chain=chain or [],
        jev_rows=jev_rows or [],
        paths=_paths(root),
        period_start=START,
        period_end=period_end,
        history=history,
        registry=registry,
    )


def _axis(result: dict, name: str) -> dict:
    for axis in result["layers"]["execution"]["axes"]:
        if axis["id"] == name:
            return axis
    raise AssertionError(name)


def _bucket(axis: dict, label: str) -> dict:
    for bucket in axis["buckets"]:
        if bucket["bucket"] == label:
            return bucket
    raise AssertionError(label)


class RegistryTest(unittest.TestCase):
    def test_five_layers_only_decision_and_execution_active(self) -> None:
        registry = load_registry()
        ids = [layer["id"] for layer in registry["layers"]]
        self.assertEqual(
            ids, ["collection", "indicator", "decision", "execution", "meta"]
        )
        status = {layer["id"]: layer["status"] for layer in registry["layers"]}
        self.assertEqual(status["decision"], "active")
        self.assertEqual(status["execution"], "active")
        for name in ("collection", "indicator", "meta"):
            self.assertEqual(status[name], "pending")
        execution = registry["layers"][3]
        self.assertEqual(execution["cell_metrics"][-2:], ["stop_loss_share", "stop_loss_share_of_neg"])
        self.assertIn("up_1h", execution["forbid_fields"])
        self.assertIn("fwd_1h", execution["forbid_fields"])
        axes = {axis["id"]: axis for axis in execution["axes"]}
        self.assertIn("no_shadow", axes["peak_giveback"]["folded_buckets"])
        self.assertIn("peak_nonpositive", axes["peak_giveback"]["folded_buckets"])
        self.assertTrue(axes["slippage"]["yellow_frozen"])
        self.assertIn(">2h", axes["hold_duration"]["buckets"])

    def test_fp_log_scaffold_is_header_only(self) -> None:
        path = ROOT / "exports" / "structure_fp_log.csv"
        text = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(text, [",".join(FP_LOG_COLUMNS)])
        self.assertEqual(fp_log_row_count(path), 0)
        history = (ROOT / "exports" / "structure_history.csv").read_text(encoding="utf-8")
        self.assertEqual(history.strip(), ",".join(HISTORY_COLUMNS))


class StructureCheckTest(unittest.TestCase):
    def test_pending_layers_have_no_metrics_and_no_activation_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _touch_dbs(root)
            result = _run_check(root)
        for name in ("collection", "indicator", "meta"):
            layer = result["layers"][name]
            self.assertEqual(layer["status"], "pending")
            self.assertFalse(layer["activated"])
            self.assertIsNone(layer["metrics"])
        registry = copy.deepcopy(load_registry())
        for layer in registry["layers"]:
            if layer["id"] == "collection":
                layer["status"] = "active"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _touch_dbs(root)
            forced = _run_check(root, registry=registry)
        self.assertFalse(forced["layers"]["collection"]["activated"])
        self.assertIsNone(forced["layers"]["collection"]["metrics"])
        self.assertIn("不写激活路径", forced["layers"]["collection"]["note"])

    def test_sparse_cell_is_not_interpreted_and_one_week_is_not_red(self) -> None:
        rows: list[SignalChainRow] = []
        for index in range(15):
            rows.append(
                _signal(
                    decision_id=index,
                    token_address=f"a{index % 10}",
                    jev_strength=0.2,
                    liquidity_at_signal=600_000,
                    fwd_1h=1.0,
                    pair_created_at_ms=SIGNAL_MS - 10 * DAY_MS,
                )
            )
        for index in range(20):
            rows.append(
                _signal(
                    decision_id=100 + index,
                    token_address=f"b{index % 3}",
                    jev_strength=0.1,
                    liquidity_at_signal=600_000,
                    fwd_1h=-0.2,
                    pair_created_at_ms=SIGNAL_MS - 10 * DAY_MS,
                )
            )
        for index in range(4):
            rows.append(
                _signal(
                    decision_id=200 + index,
                    token_address=f"s{index}",
                    jev_strength=0.2,
                    liquidity_at_signal=10_000,
                    fwd_1h=1.0,
                    pair_created_at_ms=SIGNAL_MS - 2 * HOUR_MS,
                )
            )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _touch_dbs(root)
            result = _run_check(root, chain=rows, jev_rows=rows)
        decision = result["layers"]["decision"]
        sparse = [
            cell
            for cell in decision["core_cross"]
            if cell["age"] == "<1d" and cell["liq"] == "<50k"
        ]
        self.assertEqual(len(sparse), 1)
        self.assertEqual(sparse[0]["n"], 4)
        self.assertTrue(sparse[0]["sparse"])
        self.assertFalse(sparse[0]["interpret"])
        self.assertFalse(sparse[0]["yellow"])
        self.assertEqual(decision["verdict"], "yellow")
        self.assertNotEqual(decision["verdict"], "red")
        self.assertIsNone(decision["review_ticket"])
        self.assertIn("单周不算数", decision["conclusion"])
        self.assertFalse(decision["experimental"]["token_age_fine"]["yellow_eligible"])

    def test_age_reuses_resolve_token_age_hours(self) -> None:
        raw_wins = _signal(
            decision_id=1,
            token_address="raw-wins",
            raw={"extras": {"pairCreatedAt": SIGNAL_MS - 2 * HOUR_MS}},
            pair_created_at_ms=SIGNAL_MS - 30 * DAY_MS,
        )
        pairs_only = _signal(
            decision_id=2,
            token_address="pairs-only",
            raw={},
            pair_created_at_ms=SIGNAL_MS - 10 * DAY_MS,
        )
        missing = _signal(
            decision_id=3,
            token_address="missing",
            raw={},
            pair_created_at_ms=None,
        )
        rows = [raw_wins, pairs_only, missing]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _touch_dbs(root)
            with mock.patch(
                "review.structure_check.resolve_token_age_hours",
                wraps=resolve_token_age_hours,
            ) as spy:
                result = _run_check(root, chain=rows, jev_rows=rows)
        self.assertGreater(spy.call_count, 0)
        coverage = result["layers"]["decision"]["age_coverage"]["buckets"]
        self.assertEqual(coverage["<1d"], 1)
        self.assertEqual(coverage[">7d"], 1)
        self.assertEqual(coverage["unknown"], 1)
        ages = {cell["age"] for cell in result["layers"]["decision"]["core_cross"]}
        self.assertIn("<1d", ages)
        self.assertIn(">7d", ages)
        self.assertIn("unknown", ages)
        self.assertEqual(
            result["layers"]["decision"]["age_resolver"],
            "review.attribution.resolve_token_age_hours",
        )

    def test_stop_loss_fields_coexist_and_do_not_merge(self) -> None:
        rows = [
            _closed("s1", -100, "stop_loss"),
            _closed("s2", -50, "stop_loss"),
            _closed("m1", -30, "max_hold"),
            _closed("t1", 40, "take_profit"),
            _closed("t2", 20, "take_profit"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _touch_dbs(root)
            _paper(Path(paths.paper), rows)
            result = _run_check(root)
        metrics = result["layers"]["execution"]["metrics"]
        self.assertEqual(tuple(metrics), EXECUTION_METRIC_KEYS)
        self.assertAlmostEqual(metrics["stop_loss_share"], 1.0)
        self.assertAlmostEqual(metrics["stop_loss_share_of_neg"], 150 / 180)
        self.assertNotEqual(metrics["stop_loss_share"], metrics["stop_loss_share_of_neg"])
        manual = _bucket(_axis(result, "exit_reason"), "manual")
        self.assertEqual(manual["metrics"]["n"], 0)
        self.assertIsNone(manual["metrics"]["stop_loss_share"])
        self.assertIsNone(manual["metrics"]["stop_loss_share_of_neg"])
        self.assertIn("stop_loss_share", manual["metrics"])
        self.assertIn("stop_loss_share_of_neg", manual["metrics"])
        execution = result["layers"]["execution"]
        self.assertEqual(execution["verdict"], "yellow")
        self.assertIsNone(execution["review_ticket"])
        self.assertIn("单周不算数", execution["conclusion"])
        self.assertIn("dashboard", execution["conclusion"])
        self.assertIn("占负", execution["conclusion"])
        self.assertNotIn("up_1h", json.dumps(execution))
        self.assertNotIn("fwd_", json.dumps(execution))
        hold = _bucket(_axis(result, "hold_duration"), ">2h")
        self.assertEqual(hold["metrics"]["n"], 0)
        self.assertTrue(hold["empty_by_design"])
        self.assertEqual(_axis(result, "size_band")["screen"], "folded")

    def test_stop_fields_stay_distinct_when_book_is_net_positive(self) -> None:
        rows = [
            _closed("s1", -10, "stop_loss"),
            _closed("t1", 30, "take_profit"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _touch_dbs(root)
            _paper(Path(paths.paper), rows)
            result = _run_check(root)
        metrics = result["layers"]["execution"]["metrics"]
        self.assertIsNone(metrics["stop_loss_share"])
        self.assertAlmostEqual(metrics["stop_loss_share_of_neg"], 1.0)
        self.assertNotIn("stop_loss_share", result["layers"]["execution"]["triggers"])

    def test_missing_shadow_mean_peak_is_null_and_nonpositive_bucket_kept(self) -> None:
        rows = [
            _closed("bare", -1, "max_hold"),
            _closed("flat", -1, "max_hold"),
            _closed("up", 2, "take_profit"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _touch_dbs(root)
            _paper(Path(paths.paper), rows)
            conn = sqlite3.connect(paths.execution)
            conn.execute(
                """
                CREATE TABLE shadow_c3 (
                    id INTEGER PRIMARY KEY,
                    position_id TEXT,
                    peak_pnl_pct REAL,
                    giveback_from_peak_pct REAL
                )
                """
            )
            conn.executemany(
                "INSERT INTO shadow_c3 (position_id, peak_pnl_pct, giveback_from_peak_pct) VALUES (?,?,?)",
                [("flat", -1.0, 0.4), ("up", 4.0, 1.0)],
            )
            conn.commit()
            conn.close()
            result = _run_check(root)
        peak_axis = _axis(result, "peak_giveback")
        bare = _bucket(peak_axis, "no_shadow")
        nonpositive = _bucket(peak_axis, "peak_nonpositive")
        below = _bucket(peak_axis, "<1x")
        self.assertEqual(bare["metrics"]["n"], 1)
        self.assertIsNone(bare["metrics"]["mean_peak_pnl_pct"])
        self.assertIsNone(bare["metrics"]["mean_giveback_from_peak"])
        self.assertEqual(nonpositive["metrics"]["n"], 1)
        self.assertAlmostEqual(nonpositive["metrics"]["mean_peak_pnl_pct"], -1.0)
        self.assertAlmostEqual(below["metrics"]["mean_peak_pnl_pct"], 4.0)
        # Filling the bare row with 0 would pull the layer mean from 1.5 to 1.0.
        self.assertAlmostEqual(result["layers"]["execution"]["metrics"]["mean_peak_pnl_pct"], 1.5)

    def test_slippage_yellow_stays_frozen(self) -> None:
        rows = [_closed("wide", 5, "take_profit", slippage=0.20)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _touch_dbs(root)
            _paper(Path(paths.paper), rows)
            result = _run_check(root)
        execution = result["layers"]["execution"]
        self.assertAlmostEqual(execution["slippage_gt_15_share"], 1.0)
        self.assertTrue(execution["slippage_yellow_frozen"])
        self.assertEqual(execution["frozen_would_trigger"], ["slippage_gt_15"])
        self.assertNotIn("slippage_gt_15", execution["triggers"])
        self.assertEqual(execution["verdict"], "green")
        self.assertIn("黄冻结", execution["conclusion"])

    def test_two_consecutive_weeks_raise_red_inside_freeze(self) -> None:
        rows = [
            _closed("s1", -100, "stop_loss"),
            _closed("s2", -50, "stop_loss"),
            _closed("m1", -30, "max_hold"),
            _closed("t1", 40, "take_profit"),
            _closed("t2", 20, "take_profit"),
        ]
        history = [
            {
                "period_end": "2026-10-12T00:00:00Z",
                "layer": "execution",
                "candidate": "yellow",
                "verdict": "yellow",
                "direction": "stop_loss_share",
                "n": "5",
                "note": "yellow",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _touch_dbs(root)
            _paper(Path(paths.paper), rows)
            red = _run_check(root, history=history)
            stale = _run_check(
                root,
                history=[
                    {
                        **history[0],
                        "period_end": "2026-10-01T00:00:00Z",
                    }
                ],
            )
        execution = red["layers"]["execution"]
        self.assertEqual(execution["verdict"], "red")
        self.assertIn("冻结期内只建议", execution["conclusion"])
        self.assertIn("冻结期内只建议", execution["review_ticket"])
        self.assertIn("旁证≠改闸", execution["conclusion"])
        self.assertIn("#71≠#72", execution["conclusion"])
        self.assertNotIn("up_1h", execution["conclusion"])
        self.assertEqual(stale["layers"]["execution"]["verdict"], "yellow")

    def test_sqlite_opens_are_read_only_and_check_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _touch_dbs(root)
            _paper(
                Path(paths.paper),
                [_closed("s1", -5, "stop_loss")],
            )
            before = {
                name: Path(getattr(paths, name)).read_bytes()
                for name in ("main", "execution", "paper", "backtest")
            }
            names_before = sorted(path.name for path in root.iterdir())
            real_connect = sqlite3.connect

            def guard(database: str, *args: object, **kwargs: object) -> sqlite3.Connection:
                if "mode=ro" not in str(database):
                    raise AssertionError(f"writable sqlite open: {database}")
                return real_connect(database, *args, **kwargs)

            with mock.patch("review.data.sqlite3.connect", guard):
                result = _run_check(root)
            after = {
                name: Path(getattr(paths, name)).read_bytes()
                for name in ("main", "execution", "paper", "backtest")
            }
            names_after = sorted(path.name for path in root.iterdir())
        self.assertEqual(before, after)
        self.assertEqual(names_before, names_after)
        self.assertTrue(result["read_only"])
        self.assertGreaterEqual(result["layers"]["execution"]["n_closed"], 1)


class HistoryAndReportTest(unittest.TestCase):
    def test_history_upsert_does_not_duplicate_a_week(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "structure_history.csv"
            row = {
                "period_end": END,
                "layer": "execution",
                "candidate": "yellow",
                "verdict": "yellow",
                "direction": "stop_loss_share",
                "n": 5,
                "note": "yellow",
            }
            upsert_layer_rows(path, [row])
            upsert_layer_rows(path, [{**row, "verdict": "red", "note": "red"}])
            upsert_layer_rows(
                path,
                [{**row, "layer": "decision", "direction": "", "verdict": "green"}],
            )
            loaded = load_history(path)
        self.assertEqual(len(loaded), 2)
        execution = next(item for item in loaded if item["layer"] == "execution")
        self.assertEqual(execution["verdict"], "red")
        fp = Path(tmp) / "structure_fp_log.csv"
        ensure_fp_log(fp)
        ensure_fp_log(fp)
        self.assertEqual(fp_log_row_count(fp), 0)

    def test_report_omits_section_13_unless_structure_check_is_passed(self) -> None:
        plain = render_weekly_md(
            period_start=START,
            period_end=END,
            chain=[],
            metrics={},
            baseline={},
            exec_summary={},
        )
        self.assertNotIn("## 13.", plain)
        shown = render_weekly_md(
            period_start=START,
            period_end=END,
            chain=[],
            metrics={},
            baseline={},
            exec_summary={},
            structure_check={
                "footnotes": ["旁证≠改闸。#71≠#72。"],
                "conclusions": {
                    "decision": "决策句。",
                    "execution": "执行句。旁证≠改闸；#71≠#72。",
                },
                "layers": {
                    "collection": {"status": "pending", "activated": False},
                    "indicator": {"status": "pending", "activated": False},
                    "meta": {"status": "pending", "activated": False},
                    "decision": {
                        "id": "decision",
                        "verdict": "green",
                        "baseline_name": "jev_filtered_up_1h",
                        "baseline_up_1h": None,
                        "core_cross": [],
                        "experimental": {
                            "token_age_fine": {
                                "note": "细桶只记覆盖",
                                "yellow_eligible": False,
                                "buckets": [],
                            }
                        },
                        "review_ticket": None,
                    },
                    "execution": {
                        "id": "execution",
                        "verdict": "yellow",
                        "open_count": 0,
                        "metrics": {
                            "stop_loss_share": 1.0,
                            "stop_loss_share_of_neg": 0.725,
                        },
                        "axes": [],
                        "note_71": "Step2 硬旁证：stop_loss_share（dashboard）=100.0%；日开峰=43。≠改闸。",
                        "note_72": "退出质量：可算比 >2x 占比=29.7%（19/64）。不并进 Step2 否决。",
                        "review_ticket": None,
                    },
                },
            },
        )
        self.assertIn("## 13. 信号结构检查", shown)
        self.assertIn("`stop_loss_share`", shown)
        self.assertIn("`stop_loss_share_of_neg`", shown)
        self.assertIn("<details>", shown)
        self.assertIn("旁证≠改闸", shown)
        for line in shown.splitlines():
            if line.startswith("- #71"):
                self.assertNotIn("giveback", line)
                self.assertNotIn("up_1h", line)
            if line.startswith("- #72"):
                self.assertNotIn("stop_loss_share", line)
                self.assertNotIn("日开峰", line)
                self.assertNotIn("up_1h", line)

    def test_cli_flag_defaults_off_and_section_13_is_opt_in(self) -> None:
        help_proc = subprocess.run(
            [sys.executable, "-m", "review.run", "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(help_proc.returncode, 0)
        self.assertIn("--structure-check", help_proc.stdout)
        self.assertIn("default off", help_proc.stdout)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main_db = sqlite3.connect(root / "dexscreener.db")
            main_db.executescript(
                """
                CREATE TABLE decision_log (
                    id INTEGER PRIMARY KEY, ts TEXT, strategy_id TEXT, action TEXT,
                    chain_id TEXT, token_address TEXT, size_pct REAL, reason TEXT,
                    risk_passed INTEGER, mode TEXT, raw_json TEXT
                );
                CREATE TABLE indicator_history (
                    change_1h REAL, change_4h REAL, tier TEXT, recorded_at TEXT
                );
                """
            )
            main_db.commit()
            main_db.close()
            sqlite3.connect(root / "execution_ledger.db").close()
            sqlite3.connect(root / "backtest_ledger.db").close()
            _paper(
                root / "paper_trading.db",
                [
                    _closed("s1", -100, "stop_loss"),
                    _closed("s2", -50, "stop_loss"),
                    _closed("m1", -30, "max_hold"),
                    _closed("t1", 40, "take_profit"),
                    _closed("t2", 20, "take_profit"),
                ],
            )
            db_bytes = {
                name: (root / name).read_bytes()
                for name in (
                    "dexscreener.db",
                    "execution_ledger.db",
                    "paper_trading.db",
                    "backtest_ledger.db",
                )
            }
            plain = root / "plain.md"
            code = main(
                [
                    "--db-root",
                    str(root),
                    "--start",
                    START,
                    "--end",
                    END,
                    "--report",
                    "--out",
                    str(plain),
                ]
            )
            self.assertEqual(code, 0)
            plain_text = plain.read_text(encoding="utf-8")
            self.assertNotIn("## 13.", plain_text)
            self.assertFalse((root / "exports" / "structure_history.csv").exists())

            checked = root / "checked.md"
            code = main(
                [
                    "--db-root",
                    str(root),
                    "--start",
                    START,
                    "--end",
                    END,
                    "--report",
                    "--out",
                    str(checked),
                    "--structure-check",
                ]
            )
            self.assertEqual(code, 0)
            text = checked.read_text(encoding="utf-8")
            self.assertIn("## 13. 信号结构检查", text)
            self.assertIn("stop_loss_share", text)
            self.assertIn("stop_loss_share_of_neg", text)
            self.assertIn("旁证≠改闸", text)
            self.assertIn("#71≠#72", text)
            execution_lines = [
                line for line in text.splitlines() if line.startswith("**结论（执行）：**")
            ]
            self.assertEqual(len(execution_lines), 1)
            self.assertNotIn("up_1h", execution_lines[0])
            self.assertIn("占负", execution_lines[0])
            history = load_history(root / "exports" / "structure_history.csv")
            self.assertEqual({row["layer"] for row in history}, {"decision", "execution"})
            self.assertEqual(fp_log_row_count(root / "exports" / "structure_fp_log.csv"), 0)
            after = {
                name: (root / name).read_bytes()
                for name in db_bytes
            }
            self.assertEqual(db_bytes, after)


if __name__ == "__main__":
    unittest.main()
