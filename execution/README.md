# Execution management — Step A + B (Paper) + C (PositionManager)

Consume `decision_log` from the main crawl DB (**read-only**), validate signals, write
receipts into an **independent** `execution_ledger.db`.

| Step | Gate | Behavior |
|------|------|----------|
| **A** (default) | `EXECUTION_PAPER_ENABLED=0` | validate + ledger only; `executed=0`, `executor=step_a_noop` |
| **B** | `EXECUTION_PAPER_ENABLED=1` / `--paper-enabled` | validated `buy`/`sell` → `PaperExecutor` → `PaperLedger`; success `executed=1, executor=paper` |
| **C** | `PM_ENABLED=1` (+ rule envs) | after validation, before executor: `PositionManager.check_open`; deny → `executed=0`, `error=pm_rejected:<rule>` |

**Never** calls `LiveExecutor` / `live_trade`. Failures are not retried (`{ok:false,error:...}` → `executed=0` + error on receipt).

## What it does

1. Incremental read: `decision_log` where `id > last_processed_id`
2. Parse `pair_address` only from `json.loads(raw_json)["pair_address"]` (extras under `raw["extras"]`)
3. Validate: `strategy_id`, `action∈{buy,sell}`, `chain_id`, `token_address`, `pair_address`, `size_pct∈(0,1]`
4. Write one `execution_log` row per decision (always includes `strategy_id`)
5. Advance cursor `last_processed_id` with **MAX** only (`--since-id` does not rewind it). A `decision_log_id` is claimed (`status=claimed`, unique) before execution; receipt, cursor, and `order_events` commit in one transaction. A bad row is stored as `rejected` and the cursor moves past it.
6. **Step C:** `PositionManager.check_open` on buys (default pass-through when `PM_ENABLED=0`)
7. **Step B only:** Paper fill with liquidity slippage; meta stores `strategy_id` + `decision_log_id`

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
When it is on, SELL skips `max_open` so a full book can still close. `EXECUTION_DAILY_LOSS_USD` is unset by default (daily-loss check stays off). A missing book fails closed only while the risk gate is on.

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
- **Tick events:** tick closes update the paper position and `fills` only. They do not append `execution_log` / `order_events`, because `decision_log_id` is unique on the entry receipt.
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

## Daemon (box / stabilize)

Resident loop must stay **paper gate closed** unless explicitly approved:

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
- Step B smoke: temp paper DB BUY→SELL→EQUITY as above; production `paper_trading.db` untouched; resident loop still without `--paper-enabled`.
- Step C smoke: temp paper DB — strategy A already open on token → strategy C buy same token with `PM_ENABLED=1 PM_TOKEN_DEDUP=1` → `pm_rejected:token_dedup`; with `PM_ENABLED=0` → pass-through.
