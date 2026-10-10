"""构造模拟四库，冒烟 review.run（本地验证，不碰用户仓库）。"""
import json
import sqlite3
import shutil
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
ex.commit()
ex.close()

# ---- 纸面库（0 持仓） ----
paper = sqlite3.connect(ROOT / "paper_trading.db")
paper.execute("CREATE TABLE account (id INTEGER PRIMARY KEY, cash_usd REAL, updated_at TEXT)")
paper.execute("INSERT INTO account VALUES (1, 10000.0, '2026-10-10T00:00:00Z')")
paper.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, chain_id TEXT, pair_address TEXT, "
              "token_address TEXT, size_usd REAL, entry_price REAL, open_ts TEXT, "
              "tp_pct REAL, sl_pct REAL, max_hold_min REAL, status TEXT, "
              "meta_json TEXT, pnl_usd REAL)")
paper.commit()
paper.close()

# ---- 回测账本（R1 可选，建空） ----
bt = sqlite3.connect(ROOT / "backtest_ledger.db")
bt.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")
bt.commit()
bt.close()

print("smoke dbs ready at", ROOT)
