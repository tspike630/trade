"""构造模拟四库，冒烟 review.run（本地验证，不碰用户仓库）。"""
import json
import sqlite3
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path("/tmp/review_smoke")
shutil.rmtree(ROOT, ignore_errors=True)
ROOT.mkdir(parents=True)

# ---- 主库 dexscreener.db ----
main = sqlite3.connect(ROOT / "dexscreener.db")
main.execute("CREATE TABLE decision_log (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, "
             "strategy_id TEXT, action TEXT, chain_id TEXT, token_address TEXT, "
             "size_pct REAL, reason TEXT, risk_passed INTEGER, mode TEXT, raw_json TEXT)")
main.execute("CREATE TABLE pair_history (chain_id TEXT, pair_address TEXT, "
             "recorded_at TEXT, price_usd REAL, liquidity_usd REAL)")
main.execute("CREATE TABLE indicator_history (chain_id TEXT, token_address TEXT, "
             "recorded_at TEXT, tier TEXT, change_1h REAL, change_4h REAL)")
main.execute("CREATE TABLE jev_signals (id INTEGER PRIMARY KEY, token_address TEXT, "
             "coverage_pct REAL, updated_at TEXT)")
main.execute("CREATE TABLE token_indicators (id INTEGER PRIMARY KEY, token_address TEXT, "
             "jev_confidence REAL, updated_at TEXT)")

# jev_signals：token 最新 coverage（0–100，分布 5..40）
for i in range(40):
    cov = 5 + (i % 8) * 5  # 5,10,15,...,40
    main.execute("INSERT INTO jev_signals (token_address, coverage_pct, updated_at) VALUES (?,?,?)",
                 (f"TOKEN{i:02d}", float(cov), "2026-10-09T23:59:00Z"))

# 40 条信号，时间从 2026-10-09T00:00Z 起每 30 分钟一条
pairs = [f"PAIR{i:02d}" for i in range(1, 41)]
for i in range(40):
    ts = f"2026-10-09T{(i * 30) // 60:02d}:{(i * 30) % 60:02d}:00Z"
    raw = {"pair_address": pairs[i], "priceUsd": 1e-5 + i * 1e-6,
           "extras": {"jev_strength": round(0.1 + (i % 5) * 0.02 + (i % 3) * 0.01, 4)}}
    main.execute("INSERT INTO decision_log VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (i + 1, ts, "jev_strength_v1", "buy", "solana", f"TOKEN{i:02d}",
                  0.05, f"jev_strength={raw['extras']['jev_strength']}", 1, "dry_run",
                  json.dumps(raw)))

# pair_history：每条 pair 从信号前 30 分钟到信号后 5 小时，每 30 分钟一个价
# 流动性分级：10k/40k/70k/100k/130k → 部分 <50k 用于验证 liq 过滤
base_price = 1e-5
for i in range(40):
    hh, mm = (i * 30) // 60, (i * 30) % 60
    px = base_price + i * 1e-6
    liq = float(10_000 + (i % 5) * 30_000)
    rows = []
    for step in range(-1, 11):  # -30min .. +5h
        total_min = hh * 60 + mm + step * 30
        rh, rm = total_min // 60, total_min % 60
        if rh < 24:
            rows.append((f"2026-10-09T{rh:02d}:{rm:02d}:00Z", round(px * (1 + 0.01 * step), 12)))
    for rt, rp in rows:
        main.execute("INSERT INTO pair_history VALUES (?,?,?,?,?)",
                     ("solana", pairs[i], rt, rp, liq))

# indicator_history：hot 基准
for i in range(48):
    main.execute("INSERT INTO indicator_history VALUES (?,?,?,?,?,?)",
                 ("solana", f"BASE{i}", f"2026-10-09T{(i * 30) // 60:02d}:{(i * 30) % 60:02d}:00Z",
                  "hot", 0.01 * (i % 7 - 3) / 100, 0.02 * (i % 5 - 2) / 100))
main.commit()
main.close()

# ---- 执行账本 ----
ex = sqlite3.connect(ROOT / "execution_ledger.db")
ex.execute("CREATE TABLE execution_log (id INTEGER PRIMARY KEY, ts TEXT, decision_log_id INTEGER, "
           "strategy_id TEXT, action TEXT, chain_id TEXT, token_address TEXT, pair_address TEXT, "
           "size_pct REAL, reason TEXT, mode TEXT, validation_ok INTEGER, "
           "validation_errors TEXT, executed INTEGER, executor TEXT, receipt_json TEXT)")
for i in range(40):
    ts = f"2026-10-09T{(i * 30) // 60:02d}:{(i * 30) % 60 + 1:02d}:00Z"
    ex.execute("INSERT INTO execution_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (i + 1, ts, i + 1, "jev_strength_v1", "buy", "solana", f"TOKEN{i:02d}",
                pairs[i], 0.05, "jev_strength=0.15", "dry_run", 1, None, 0,
                "step_a_noop", json.dumps({"validated": True})))
ex.execute(
    "UPDATE execution_log SET validation_ok=0, validation_errors=? WHERE id=1",
    (json.dumps(["liq_too_low", "wide_spread"]),),
)
ex.execute(
    "UPDATE execution_log SET validation_ok=0, validation_errors=? WHERE id=2",
    ("legacy_code; other",),
)
ex.commit()
ex.close()

# ---- 纸面库：positions.id 为 TEXT UUID，不是 INTEGER ----
CLOSED_POS = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OPEN_POS = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
BAD_POS = "cccccccc-dddd-4eee-8fff-000000000000"
paper = sqlite3.connect(ROOT / "paper_trading.db")
paper.execute("CREATE TABLE account (id INTEGER PRIMARY KEY, cash_usd REAL, updated_at TEXT)")
paper.execute("INSERT INTO account VALUES (1, 10000.0, '2026-10-10T00:00:00Z')")
paper.execute(
    "CREATE TABLE positions ("
    "id TEXT PRIMARY KEY, chain_id TEXT, pair_address TEXT, token_address TEXT, "
    "size_usd REAL, entry_price REAL, open_ts TEXT, tp_pct REAL, sl_pct REAL, "
    "max_hold_min REAL, status TEXT, meta_json TEXT, pnl_usd REAL)"
)
paper.execute(
    "CREATE TABLE fills ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT, side TEXT, price REAL)"
)
paper.execute(
    "INSERT INTO positions (id, chain_id, pair_address, token_address, size_usd, "
    "entry_price, status, meta_json, pnl_usd) VALUES (?,?,?,?,?,?,?,?,?)",
    (
        CLOSED_POS, "solana", "PAIR00", "TOKEN00", 100.0, 1.0, "closed",
        json.dumps({"decision_log_id": 1}), 1.25,
    ),
)
paper.execute(
    "INSERT INTO fills (position_id, side, price) VALUES (?,?,?)",
    (CLOSED_POS, "sell", 1.1),
)
paper.execute(
    "INSERT INTO positions (id, chain_id, pair_address, token_address, size_usd, "
    "entry_price, status, meta_json, pnl_usd) VALUES (?,?,?,?,?,?,?,?,?)",
    (
        OPEN_POS, "solana", "PAIR01", "TOKEN01", 100.0, 1.0, "open",
        json.dumps({"decision_log_id": "2"}), 9.5,
    ),
)
paper.execute(
    "INSERT INTO fills (position_id, side, price) VALUES (?,?,?)",
    (OPEN_POS, "buy", 1.0),
)
paper.execute(
    "INSERT INTO positions (id, chain_id, pair_address, token_address, size_usd, "
    "entry_price, status, meta_json, pnl_usd) VALUES (?,?,?,?,?,?,?,?,?)",
    (
        BAD_POS, "solana", "PAIR02", "TOKEN02", 50.0, 1.0, "closed",
        json.dumps({"decision_log_id": "not-a-number"}), 3.0,
    ),
)
paper.commit()
paper.close()

# ---- 回测账本（R1 可选，建空） ----
bt = sqlite3.connect(ROOT / "backtest_ledger.db")
bt.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")
bt.commit()
bt.close()

print("smoke dbs ready at", ROOT)

_CHECK = r"""
import json
from pathlib import Path
from review.attribution import fill_attribution
from review.data import (
    DbPaths,
    load_signal_chain,
    parse_decision_log_id,
    summarize_execution,
    validation_error_bucket_key,
)
from review.factor_loop import GATE_UP_1H_DEGRADE, gate_candidate
from review.gates import UP_1H_DEGRADE
from review.run import _normalize_iso

root = Path("/tmp/review_smoke")
rows = load_signal_chain(DbPaths(
    main=str(root / "dexscreener.db"),
    execution=str(root / "execution_ledger.db"),
    paper=str(root / "paper_trading.db"),
    backtest=str(root / "backtest_ledger.db"),
))
by_id = {r.decision_id: r for r in rows}
uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
assert by_id[1].position_id == uuid, by_id[1].position_id
assert isinstance(by_id[1].position_id, str)
assert by_id[1].position_status == "closed"
assert by_id[2].position_id == "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
assert by_id[2].position_status == "open"
assert all(r.position_id != "cccccccc-dddd-4eee-8fff-000000000000" for r in rows)
assert parse_decision_log_id("nope") is None
assert parse_decision_log_id("12") == 12
assert parse_decision_log_id(1.5) is None
assert parse_decision_log_id(True) is None
assert validation_error_bucket_key(json.dumps(["liq_too_low", "wide_spread"])) == "liq_too_low"
assert validation_error_bucket_key("legacy_code; other") == "legacy_code"
summary = summarize_execution(rows)
assert summary["validation_error_buckets"].get("liq_too_low") == 1
assert summary["validation_error_buckets"].get("legacy_code") == 1
assert _normalize_iso("2026-10-09T08:00:00+08:00") == "2026-10-09T00:00:00Z"
assert _normalize_iso("2026-10-09") == "2026-10-09T00:00:00Z"
assert _normalize_iso("2026-10-09T00:00:00+00:00") == "2026-10-09T00:00:00Z"
low = gate_candidate({"n_raw": 20, "unique_token": 12, "up_1h": 0.42}, week="2026-W41")
mid = gate_candidate({"n_raw": 20, "unique_token": 12, "up_1h": 0.52}, week="2026-W41")
high = gate_candidate({"n_raw": 20, "unique_token": 12, "up_1h": 0.60}, week="2026-W41")
assert low["status"] == "rejected", low
assert mid["status"] == "pending", mid
assert high["status"] == "accepted", high
assert "activated" not in high["status"]
assert GATE_UP_1H_DEGRADE == UP_1H_DEGRADE == 0.50
fa = fill_attribution(rows)
assert fa["realized_pnl_usd"] == 1.25, fa
assert fa["mtm_pnl_usd"] == 9.5, fa
assert fa["pnl_basis"] == "realized"
assert fa["total_pnl_usd"] == fa["realized_pnl_usd"]
print("uuid position_id ok")
"""

_REPO = Path(__file__).resolve().parents[2]
_CRAWLER = Path(__file__).resolve().parents[1]
for tree in (_REPO, _CRAWLER):
    subprocess.check_call([sys.executable, "-c", _CHECK], cwd=str(tree))
    print("checked", tree)
