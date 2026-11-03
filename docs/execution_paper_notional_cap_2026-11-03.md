# Paper notional cap + jump-reject PnL (2026-11-03)

Yang口令「把这个问题修好」— fix `tech_fade` paper sizing/PnL explosion (~$51M–$80M notional / ~$111M single TP from ~9490× price jump).

## What changed

File: `execution/executors/paper_executor.py`

1. **Open-size cap** (after `requested_usd = before_mult * paper_size_mult()`):
   - `requested_usd = min(requested_usd, equity_usd * size_pct * mult, HARD_CAP, liquidity_usd * LIQ_FRAC)`
   - Reject buy with `error=no_liquidity` when mid is present but liquidity is missing/non-positive (prevents unbounded size when liq cap cannot bind).
   - Cap audit fields recorded on receipt/meta: `hard_cap_usd`, `liq_frac`, `equity_pct_mult_cap_usd`, `liq_cap_usd`, `requested_usd_after_cap`, `notional_capped`.

2. **Jump-reject / clip close PnL**:
   - Helper `close_pnl_jump_verdict`: reject when exit/entry ratio `> JUMP_RATIO` or `< 1/JUMP_RATIO`, or when `|pnl| > HARD_CAP`, or `|pnl| > 100%` of pre-trade equity.
   - Signal sell (`reject_close=True`): **do not close**; return `ok=False` with `jump_reject:*` reason; cash/equity unchanged.
   - Tick TP/SL/hold (`reject_close=False`): **clip** — close flat at `entry_price` so cash restores `size_usd` and booked PnL≈0; `exit_reason` annotated with `jump_reject…`.

3. Tests: `execution/tests/test_paper_size_mult.py` extended (cap + jump classes).

## Env knobs (defaults)

| Env | Default | Role |
|-----|---------|------|
| `EXECUTION_PAPER_MAX_NOTIONAL_USD` | **20000** | Hard USD notional cap per open; also PnL jump hard cap |
| `EXECUTION_PAPER_MAX_LIQ_FRAC` | **0.25** | Max fraction of pair `liquidity_usd` on open |
| `EXECUTION_PAPER_JUMP_RATIO` | **50** | Exit/entry absurd-jump threshold (and reciprocal) |
| `EXECUTION_PAPER_SIZE_MULT` | (unchanged, race peers use **20**) | Still allowed; now hard-capped |

No live/#71 enablement. No real-exchange capital injection. No crowning.

## Test results

```
cd /workspace/dexscreener-crawler
.venv/bin/python -m unittest execution.tests.test_paper_size_mult -v
Ran 19 tests in ~0.19s — OK
```

Includes: `MULT=20` on $10M equity cannot request > HARD_CAP; missing liquidity rejects; signal sell rejects ~100× jump; tick clips absurd TP flat.

## tech_fade peer status (Asia/Hong_Kong 2026-11-03 ≈12:42)

- **Dirty** `paper_eq_tech_fade.db` + `execution_ledger_race_tech_fade.db`: **KEPT as archive** (not deleted / not reset to fake clean PnL). Still holds open ~$51.15M size row + historical ~$111M jump TP.
- Launcher line remains `# DIRTY/OUTLIER` in `scripts/start_multi_paper_race_peers.sh`.
- **Dirty exec peer**: STOPPED (no PID for `accept-purpose race_tech_fade` / `paper_eq_tech_fade`).
- **Decision `race_tech_fade`**: NOT running (no PID; last log tick ≈12:37 HKT). Purpose **stays dark** — no clean `paper_eq_tech_fade_v2` peer stood (decision not emitting). When decision is restarted for `race_tech_fade`, stand a **fresh** $10k DB+ledger+peer (`paper_eq_tech_fade_v2` / new ledger) with `--accept-purpose race_tech_fade`; do **not** reuse dirty DB.

## Constraints honored

- live/#71 OFF / `LIVE_TRADING_DISABLED` untouched
- SIZE_MULT=20 still allowed but HARD_CAP binds
- Zero real capital / no crowning
