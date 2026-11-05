# Execution management — Step A + B (Paper) + C (PositionManager)

Consume `decision_log` from the main crawl DB (**read-only**), validate signals, write
receipts into an **independent** `execution_ledger.db`.

| Step | Gate | Behavior |
|------|------|----------|
| **A** (default) | `EXECUTION_PAPER_ENABLED=0` | validate + ledger only; `executed=0`, `executor=step_a_noop` |
| **B** | `EXECUTION_PAPER_ENABLED=1` / `--paper-enabled` | validated `buy`/`sell` → `PaperExecutor` → `PaperLedger`; success `executed=1, executor=paper` |
| **C** | `PM_ENABLED=1` (+ rule envs) | after validation, before executor: `PositionManager.check_open`; deny → `executed=0`, `error=pm_rejected:<rule>` |

**Never** calls `LiveExecutor` / `live_trade`. Failures are not retried (`{ok:false,error:...}` → `executed=0` + error on receipt).

Purpose `live_bnb_boll_fade` is the only Binance spot arm. It does not use PaperExecutor. `mode=dry_run` never submits. A live BUY is consumed only after a verified native OCO (`POST /api/v3/orderList/oco`, `orderListId >= 0` and both leg order ids). The loop does not market-sell from a watched mark. `LIVE_TRADING_DISABLED` blocks a new buy only. A ledger protective sell still goes out unless `BINANCE_SPOT_BLOCK_SELL_ON_KILL=1`. `BINANCE_SPOT_LIVE_ENABLED` stays off unless set in the process environment. A strategy buy that only carries `size_pct` is sized as `size_pct * BINANCE_SPOT_BOOK_USDT` (an operator book, not a wallet). Unset book is `missing_book_usdt`. Explicit `quote_order_qty` / `quantity` wins. Planned entry notional under 20 USDT is refused (a viability floor, not a cap). The 5% size and the 20 USDT floor are unchanged. The production supervisor is `scripts/live_bnb_execution_watchdog.sh` (one instance, restart within 3s). It does not change the kill file, the live flag, or keys. `scripts/live_bnb_watchdog_dry.sh` only prints that preflight.

### REST account snapshot

Refreshing this file is not opening the gate. The command only signs `GET /api/v3/account` and writes a mode `0600` JSON file outside the repo. It does not place an order and does not change `LIVE_TRADING_DISABLED` or `BINANCE_SPOT_LIVE_ENABLED`.

```bash
# key and secret stay in the process environment. Do not put them on the command line.
export BINANCE_API_KEY=...
export BINANCE_API_SECRET=...
python -m execution.binance_account_snapshot --output /secure/live-bnb-account.json
```

The gateway and the watchdog read that file. They reject a missing file, bad JSON, a mode other than owner-only, a symlink, a path inside this repo, a bad HMAC, a snapshot older than `BINANCE_SPOT_ACCOUNT_SNAPSHOT_MAX_AGE_SEC` (default 3600), a USDT `free` balance that differs from `BINANCE_SPOT_REST_USDT_FREE`, or `uid` `1289917075` (the MCP Agentic sub-account). A payload without `uid` can still bind when `accountType` is `SPOT`, `canTrade` is true, and `permissions` contains `SPOT`.

Startup still needs paper off and does not by itself remove the kill file:

```bash
export BINANCE_SPOT_PRODUCTION_BOOK=rest_api_key
export BINANCE_SPOT_REST_USDT_FREE=<USDT free from that GET>
export BINANCE_SPOT_ACCOUNT_SNAPSHOT_PATH=/secure/live-bnb-account.json
export BINANCE_SPOT_BOOK_USDT=<operator sizing book>
export BINANCE_SPOT_LIVE_ENABLED=1
export EXECUTION_PAPER_ENABLED=
export PAPER_DB_PATH=
export EXECUTION_PAPER_DB=
# LIVE_TRADING_DISABLED must already be absent. This command does not remove it.
python -m execution.run --loop \
  --ledger execution_ledger_live_bnb_boll_fade.db \
  --accept-purpose live_bnb_boll_fade \
  --no-paper-enabled
```

`BINANCE_SPOT_BOOK_USDT` sizes the 5% order. `BINANCE_SPOT_REST_USDT_FREE` is the wallet balance the snapshot must match. They are not the same field. `live_ready` stays false in code: paper-off, the fixed egress, and the running consumer are environment checks.

## What it does

1. Incremental read: `decision_log` where `id > last_processed_id`. The SELECT adds `purpose` when `PRAGMA table_info` shows that column; older DBs omit it and purpose is read from `raw_json` only.
2. Parse `pair_address` only from `json.loads(raw_json)["pair_address"]` (extras under `raw["extras"]`)
3. **Purpose gate** (before any fill): only `live_candidate` continues to validation. Other purposes are rejected with a receipt and the cursor advances, using the same claim pattern as other rejected rows. See below.
4. Validate: `strategy_id`, `action∈{buy,sell}`, `chain_id`, `token_address`, `pair_address`, `size_pct∈(0,1]`
5. Write one `execution_log` row per decision (always includes `strategy_id`)
6. Advance cursor `last_processed_id` with **MAX** only (`--since-id` does not rewind it). A `decision_log_id` is claimed (`status=claimed`, unique) before execution; receipt, cursor, and `order_events` commit in one transaction. A bad row is stored as `rejected` and the cursor moves past it.
7. **Step C:** `PositionManager.check_open` on buys (default pass-through when `PM_ENABLED=0`)
8. **Step B only:** Paper fill with liquidity slippage; meta stores `strategy_id` + `decision_log_id`

### Purpose gate (P2-38)

Decision rows carry `purpose` (decision PR #37): `live_candidate` is the paper-eligible dry-run; `sample` is weekly JEV sampling and is not executable. `mode` stays `dry_run` / `live_stub` and is not the gate.

Read order:

1. `decision_log.purpose` when the SELECT included the column (NULL stays missing — historical rows are not backfilled and are not executable).
2. Otherwise top-level `json.loads(raw_json)["purpose"]`.

Compare after strip, case-sensitive. The only executable value is `live_candidate`. Anything else is a receipt with `status=rejected`, `executed=0`, `executor=step_a_noop`, and `reason` / `validation_errors`:

| purpose | reason |
|---------|--------|
| `sample` | `purpose_sample` |
| NULL, absent, or blank | `purpose_missing` |
| any other value (`SAMPLE`, `Live_Candidate`, …) | `purpose_invalid` |

The cursor advances. `order_events` (when `EXECUTION_EVENTS_ENABLED`) are `new` then `rejected` with `stage=purpose`, in the same transaction as the receipt. Dry-run (`EXECUTION_PAPER_ENABLED=0`) still writes that receipt — a sample row is not skipped silently. This does not turn on paper, PM, or risk.

**Peer allowlist (optional):** `--accept-purpose` / `EXECUTION_ACCEPT_PURPOSE` switches this process to an allowlist gate (e.g. `race_rsi_bounce`). Only listed purposes pass; `live_candidate` does not auto-pass unless listed. Peer mode requires an isolated `--ledger` (not shared `execution_ledger.db`) and a `--paper-db` that is not `paper_trading.db`. See `scripts/multi_paper_peer_launcher.md` and `scripts/start_multi_paper_race_peers.sh`.

### PositionManager rules (Step C)

Master switch **`PM_ENABLED=0`** (default) → all buys allowed (pass-through).

| Env | Default | When on |
|-----|---------|---------|
| `PM_TOKEN_DEDUP` | 0 | deny buy if same chain+token already open (token missing → pair match). Arbitration: `PM_STRATEGY_PRIORITY` whitelist or FCFS |
| `PM_STRATEGY_MUTEX` | 0 | deny if another strategy already has opens (same arbitration) |
| `PM_MAX_TOTAL_PCT` | off | deny if open size_pct sum + new > limit. **Percent points only** (30 → 0.30, 1 → 0.01) |
| `PM_MAX_PER_TOKEN_PCT` | off | deny if token exposure + new > limit. Percent points only (suggest **10**) |
| `PM_MAX_OPEN_PER_STRATEGY` | off | deny if strategy open count ≥ limit (suggest **5**) |
| `PM_STRATEGY_PRIORITY` | empty | comma list, earlier = higher priority for conflict arbitration |

Reject reason format: `pm_rejected:<rule_name>` (e.g. `pm_rejected:token_dedup`).
`PM_ENABLED=1` with no paper book is `pm_rejected:no_ledger` (fail closed). `PM_ENABLED=0` still passes through.
Startup logs the parsed fractions (`PM_MAX_*` are percent points: `1` means 1%, not 100%).

Pre-trade risk stays **off** (`EXECUTION_RISK_ENABLED` accepts `1/true/yes/on`, default off).
When it is on, SELL skips `max_open` so a full book can still close. `EXECUTION_DAILY_LOSS_USD` is unset by default (daily-loss check stays off). When it is set, realized PnL sums closed positions whose `closed_at` (UTC ISO) falls on the current Asia/Hong_Kong date, and the order is rejected only when that sum is `< -limit`. A missing book fails closed only while the risk gate is on.

### Paper fill rules (Step B)

- **Price:** `paper_trade.latest_price` — `pair_history` `ORDER BY recorded_at DESC LIMIT 1`, compared with `pairs.priceUsd` / `pairs.updated_at`; the newer fresh quote wins. Older than `PAPER_PRICE_MAX_AGE_SEC` (default **3600**) → `None` / `no_price` (no fill).
- **Slippage** from `store.get_pair_raw().liquidity.usd`: `<50k→2%`, `50k~500k→1%`, `≥500k→0.3%`, unknown→`2%`
  - buy fill: mid × (1+slip); signal sell and tick close: mid × (1−slip)
  - TP/SL **trigger** compares mid to the entry **fill** (entry already includes buy slippage)
  - equity marks use **mid**, not the slipped fill
- **Size:** `size_pct * equity(marks)`; if over cash → **truncate** to cash (record actual), do not raise
- **Sell match:** `chain+token` else `chain+pair`, and `meta.strategy_id` must match. A different strategy's position is `strategy_mismatch` (not closed).
- **Partial sell:** `size_pct` on a sell is the fraction of the position to close. `size_pct < 1` → `partial_sell_rejected` (position left open). Full close requires `size_pct=1`. Copying a buy's equity fraction (for example 0.05) onto a sell is rejected.
- **Idempotent buy:** `meta.decision_log_id` already on a position → `duplicate_decision_log_id` (no second open)
- **TP/SL/max_hold:** from signal (`tp_pct`/`sl_pct`/`max_hold_min` or extras), else `10%` / `5%` / `60min`. Tick calls `execution.exits.scan_exits` (same rules as backtest). `opened_at` may end in `Z`.
- **Tick events:** tick closes update the paper position and `fills`. They do not append `execution_log` / `order_events`, because `decision_log_id` is unique on the entry receipt. `PaperExecutor` also appends observe rows (`strategy_streak`, `streak_events`, `shadow_c3`) on the execution ledger after a tick close or a filled sell. Those rows do not reject orders. `peak_price` on the open position only rises. Exit rules stay 10% / 5% / 60min.
- **One tick writer** per paper DB: `paper_trade.py tick` / `paper_tick_loop.sh` **or** `execution.run --paper-tick`, not both.
- Main crawler DB opened **read-only** inside `PaperExecutor`

## Paths / env

| Item | Default |
|------|---------|
| Main / decision DB | `decision_engine.config.DB_PATH` |
| Ledger DB | `execution_ledger.db` / `EXECUTION_LEDGER_PATH` |
| Paper gate | **`EXECUTION_PAPER_ENABLED=0`** (off) |
| Paper DB | `EXECUTION_PAPER_DB` or `PAPER_DB_PATH` (use a **temp** DB for smoke; do not casually open production `paper_trading.db`) |
| Paper tick in loop | `EXECUTION_PAPER_TICK=0` |
| PM master | **`PM_ENABLED=0`** (off → pass-through) |
| PM rules | `PM_TOKEN_DEDUP` / `PM_STRATEGY_MUTEX` / `PM_MAX_*` default off |
| Poll interval | `5` s (`EXECUTION_POLL_SEC`) |
| Quote max age | `PAPER_PRICE_MAX_AGE_SEC` default **3600** |
| Risk gate | **`EXECUTION_RISK_ENABLED=0`** |
| Daily loss limit | `EXECUTION_DAILY_LOSS_USD` unset (off) |
| F-1 streak fuse | `EXECUTION_F1_CIRCUIT` unset = off. The order path does not read it |

## CLI

```bash
cd /workspace/dexscreener-crawler

# Step A (default — safe for resident loop)
python -m execution.run --once
python -m execution.run --loop

# Step B smoke (TEMP paper db only)
python -m execution.run --once \
  --paper-enabled \
  --paper-db /tmp/paper_exec_smoke.db \
  --ledger /tmp/execution_ledger_smoke.db \
  --since-id 0 --limit 1

# Loop with paper + TP/SL/hold tick (explicit; do not enable on prod paper DB by accident)
python -m execution.run --loop --paper-enabled --paper-db /path/to/paper.db --paper-tick

# Read-only observe reports (do not reject orders; do not change TP/SL/hold)
python -m execution.f1_streak_report --ledger execution_ledger.db --paper paper_trading.db
python -m execution.shadow_c3_report --ledger execution_ledger.db
python -m execution.risk.bankruptcy
python -m execution.slippage_report --ledger execution_ledger.db --paper paper_trading.db --crawler-db dexscreener.db
```

`--once` is the default when neither `--once` nor `--loop` is given.
`--no-paper-enabled` / `--no-paper-tick` force gates off even if env is set.

## Smoke (BUY → SELL → EQUITY)

Use a **temporary** paper DB + real crawler DB. Do **not** point at production `paper_trading.db`.

```bash
cd /workspace/dexscreener-crawler
SMOKE_PAPER=/tmp/paper_exec_smoke_$$.db
rm -f "$SMOKE_PAPER"
.venv/bin/python - <<PY
from execution.executors.paper_executor import PaperExecutor
from paper_trade import PaperLedger
from pathlib import Path
import json, os

paper = os.environ["SMOKE_PAPER"]
# seed account
PaperLedger(paper).close()

# pick a live decision_log buy with price
import sqlite3
c = sqlite3.connect("file:dexscreener.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
row = c.execute(
    "SELECT id, strategy_id, action, chain_id, token_address, size_pct, mode, raw_json "
    "FROM decision_log WHERE action='buy' ORDER BY id DESC LIMIT 1"
).fetchone()
raw = json.loads(row["raw_json"] or "{}")
sig_buy = {
    "decision_log_id": int(row["id"]),
    "strategy_id": row["strategy_id"],
    "action": "buy",
    "chain_id": row["chain_id"],
    "token_address": row["token_address"],
    "pair_address": raw["pair_address"],
    "size_pct": float(row["size_pct"] or 0.05),
    "extras": raw.get("extras") or {},
    "raw": raw,
}
sig_sell = dict(sig_buy)
sig_sell["action"] = "sell"
sig_sell["decision_log_id"] = int(row["id"]) + 1  # distinct id; buy id stays on the position
sig_sell["size_pct"] = 1.0  # full close; size_pct<1 is partial_sell_rejected

with PaperExecutor(paper_db=paper, crawler_db="dexscreener.db") as ex:
    buy = ex.execute(sig_buy)
    print("BUY", json.dumps({k: buy.get(k) for k in ("ok","position_id","fill_price","size_usd","slippage","truncated","error")}, default=str))
    sell = ex.execute(sig_sell)
    print("SELL", json.dumps({k: sell.get(k) for k in ("ok","position_id","fill_price","pnl_usd","slippage","error")}, default=str))
    print("EQUITY", json.dumps(ex.ledger.equity(), default=str))
PY
```

Expect: `BUY ok=true`, `SELL ok=true` with `pnl_usd`, `EQUITY` with `open_count=0`.

## Smoke (Step C — TOKEN_DEDUP)

Use a **temporary** paper DB. Do **not** point at production `paper_trading.db`.

```bash
cd /workspace/dexscreener-crawler
export SMOKE_PAPER=/tmp/pm_smoke_$$.db
rm -f "$SMOKE_PAPER"
PM_ENABLED=1 PM_TOKEN_DEDUP=1 .venv/bin/python - <<'PY'
from paper_trade import PaperLedger
from execution.position_manager import PositionManager, PositionManagerConfig
import os, json

paper = os.environ["SMOKE_PAPER"]
led = PaperLedger(paper)
led.open_position(
    chain_id="solana",
    pair_address="pair_aaa",
    token_address="token_t",
    price=1.0,
    size_usd=100.0,
    meta={"strategy_id": "strat_A", "size_pct": 0.05},
)
cfg = PositionManagerConfig.from_env()
pm = PositionManager(ledger=led, config=cfg)
r = pm.check_open({
    "action": "buy",
    "strategy_id": "strat_C",
    "chain_id": "solana",
    "token_address": "token_t",
    "pair_address": "pair_bbb",
    "size_pct": 0.05,
})
print("DEDUP", json.dumps(r.as_dict()))
cfg2 = PositionManagerConfig(enabled=False, token_dedup=True)
r2 = PositionManager(ledger=led, config=cfg2).check_open({
    "action": "buy", "strategy_id": "strat_C", "chain_id": "solana",
    "token_address": "token_t", "pair_address": "pair_bbb", "size_pct": 0.05,
})
print("PASSTHROUGH", json.dumps(r2.as_dict()))
led.close()
PY
```

Expect: `DEDUP allowed=false reason=pm_rejected:token_dedup`; `PASSTHROUGH allowed=true`.

## Schema (`execution_ledger.db`)

**`execution_log`**: `id`, `ts`, `decision_log_id`, `strategy_id`, `action`, `chain_id`,
`token_address`, `pair_address`, `size_pct`, `reason`, `mode`, `validation_ok`,
`validation_errors`, `executed`, `executor`, `receipt_json`

**`execution_cursor`**: key `last_processed_id`

## Modules

- `execution/ledger.py` — ledger path + schema + cursor + append
- `execution/gateway.py` — fetch / parse / validate / PM / optional paper execute / receipt
- `execution/position_manager.py` — global open view + buy rules (`check_open` / `snapshot`)
- `execution/executors/paper_executor.py` — PaperLedger fills + slippage
- `execution/run.py` — CLI
- `execution/slippage_report.py` — read-only I-3 `slippage_1h` (does not skip orders)

## Daemon (box / stabilize)

Resident loop must stay **paper gate closed** unless explicitly approved.
The 2026-10-14 paper authorization is that approval for paper only:
`--paper-enabled --paper-tick --pm-enabled` plus the env set in
[P1_PM_ROADMAP.md](P1_PM_ROADMAP.md). It does not change the code defaults
in this file, and it does not open live. The unit file
`deploy/dexscreener-execution.service` still starts Step A `--loop`.

```bash
cd /workspace/dexscreener-crawler
mkdir -p logs
# default: NO --paper-enabled
pgrep -af 'python -m execution.run --loop' || \
  nohup .venv/bin/python -m execution.run --loop >> logs/execution_loop.log 2>&1 &
pgrep -f 'python -m execution.run --loop' | head -1 > logs/execution_loop.pid
ps -p "$(cat logs/execution_loop.pid)" -o pid,etime,cmd
```

| Item | Path / value |
|------|----------------|
| PID file | `logs/execution_loop.pid` |
| Log | `logs/execution_loop.log` |
| Default interval | 5s |
| Paper gate default | **off** (`EXECUTION_PAPER_ENABLED=0`) |
| PM gate default | **off** (`PM_ENABLED=0`) |
| systemd (prod) | `deploy/dexscreener-execution.service` |

`daily_check.py` reports process liveness, today's `execution_log` stats from
`execution_ledger.db`, and a **position overview** from paper DB (read-only; does
not require `PM_ENABLED`).

## Success check

- Step A: after `--once`, ledger rows with `executed=0`, `strategy_id` set.
- Step B smoke: temp paper DB BUY→SELL→EQUITY as above; production `paper_trading.db` untouched; do not restart the resident loop from this smoke. The authorized paper command is in [P1_PM_ROADMAP.md](P1_PM_ROADMAP.md); code default remains paper off.
- Step C smoke: temp paper DB — strategy A already open on token → strategy C buy same token with `PM_ENABLED=1 PM_TOKEN_DEDUP=1` → `pm_rejected:token_dedup`; with `PM_ENABLED=0` → pass-through.

## 警示事例与 P-1（docs only）

Code defaults in the tables above stay as written. The 2026-10-14 paper
authorization, and the still-missing net-edge / account-drawdown / four-tier
heat items, are in [P1_PM_ROADMAP.md](P1_PM_ROADMAP.md). Warning-case mapping
（半胜仍亏、薄池打穿、$0.95 黑天鹅、周检拒因走 `order_events`）is in
[WARNING_CASES.md](WARNING_CASES.md). Research mapping（M-4 服务层硬检查、
M-5 先选择后仓位且 Kelly / 方案 7 冻结、M-7 滑点档仍是成本模型、单市场 15%
保持 R2-pending）is in [RESEARCH_MAPPING.md](RESEARCH_MAPPING.md). These notes
are **不算有效性** until the 2026-10-19 weekly check. They do not change
TP/SL/hold, slippage tiers, or PM numbers, and they do not enable
`PM_MAX_PER_TOKEN_PCT`. E-2 / C4 `target_band_failover` is registered as
WAITING / observe-only in [WAITING_ITEMS.md](WAITING_ITEMS.md); it does not
change runtime. I-3 `slippage_1h` is the read-only command
`python -m execution.slippage_report` (`bot_trade_share` reads collection #81
`bot_fill_observations`; receipt tags are a secondary fallback; high-bot skip
is not wired). I-4 and INTEL_LOOP are registered only; a
candidate is not an activation. See that same file.
