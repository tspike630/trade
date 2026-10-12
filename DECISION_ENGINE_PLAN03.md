# Decision engine plan 03 — buy/sell factor decoupling

Factor plugin layer on top of the existing strategy registry. Legacy `jev_strength_v1` / `tech_momentum_v1` / `combined_jev_tech_v1` stay the regression baseline and the `a|b|c` aliases. `factor_*` combos use the same buy predicates and reason strings, with sell fixed to `noop`.

Both trees are updated together: `crawler/decision_engine/` and root `decision_engine/`.

## What landed

New (each tree):

- `factors/__init__.py`, `factors/base.py`
- `factors/buy/__init__.py`, `jev_strength.py`, `tech_momentum.py`, `combined_jev_tech.py`
- `factors/sell/__init__.py`, `noop.py`, `time_exit.py`, `trail_stop.py`
- `strategies/factor_strategy.py`

Patched:

- `strategies/__init__.py` — `_make_factor_strategy` factory registers the three `factor_*` ids. `STRATEGY_ALIASES` `a|b|c` still point at the old ids.
- `run.py` and `backtest/run.py` `build_strategies` — when a `factor_*` id is selected, it receives the same CLI thresholds as its legacy twin (`--min-jev-strength`, and the backtest tech/combined flags). `DECISION_DEFAULT_STRATEGY` is still `jev_strength_v1`. `time_exit` / `trail_stop` are registry-only; they are not attached to the three combos.

Not touched: `core/engine.py`, `core/data_adapter.py`, `core/signal_bus.py`, `core/risk_manager.py`, `jev_strategy.py`, `tech_strategy.py`, `combined_strategy.py`, cron/hooks.

`FactorStrategy` forwards constructor kwargs (`min_jev_strength`, `min_change_5m`, `min_volume_strength`, …) onto the buy factor, with explicit `buy_params` winning. Buy predicates use the legacy `>=` checks (NaN does not pass).

## Acceptance

| # | Check | Result |
|---|---|---|
| 1 | Files + `import decision_engine.factors` | Pass. Import works from repo root and from `crawler/`. |
| 2 | V1 signal equivalence at 0.15 | Pass on a temp fixture (no production DB in this environment). `decision_log`: both strategies **n=3**, **distinct tokens=3** (HIT, MID, EQ). Reason and `raw_json` (except `strategy_id` / `ts`) **diffs=0**. In-process compare of all 3 pairs: **9 old / 9 new signals, diffs=0**. |
| 3 | V2 backtest equivalence | Pass on the same fixture (`--days 7` used the only available window, 4 bars / 16 indicator rows). **trades=6, win_rate=50.0%, total_pnl_usd=213.94, final_equity=10213.94** on both ledgers. Closed position rows match aside from `strategy_id`. |
| 4 | V3 sell unit checks | Pass. `time_exit` age 2h hits; `trail_stop` dd 30% hits; dd 10% is `None`. `noop` stays `None`. |
| 5 | V4 registry | Pass. `list_strategies()` = **6**: the 3 legacy ids + `factor_jev_strength_v1`, `factor_tech_momentum_v1`, `factor_combined_jev_tech_v1`. `get_strategy("factor_jev_strength_v1", min_jev_strength=0.15)` instantiates with buy `['jev_strength']`, sell `['noop']`, threshold 0.15. |
| 6 | Isolation | Core four modules and the three legacy strategy files: **empty `git diff`**. Aliases `a|b|c` unchanged. Diff beyond new files + `strategies/__init__.py` is only the CLI threshold bridge in `run.py` and `backtest/run.py` (both trees). |
| 7 | Live default unchanged | Default strategy id still `jev_strength_v1`. `factor_*` is not referenced by cron or hooks. Sell on the three combos is `noop`. |

## Blocker

This checkout has no `dexscreener.db` and no `indicator_history`. V1/V2 numbers above are from `/tmp/plan03_fixture.db` (not committed). Re-run on the real DB:

```bash
cd /workspace/crawler   # or repo root; both trees match
.venv/bin/python -m decision_engine.run --strategy jev_strength_v1 --dry-run --once --min-jev-strength 0.15 --db /path/to/dexscreener.db
.venv/bin/python -m decision_engine.run --strategy factor_jev_strength_v1 --dry-run --once --min-jev-strength 0.15 --db /path/to/dexscreener.db
.venv/bin/python -m decision_engine.backtest.run --strategy jev_strength_v1 --days 7 --dry-run --db /path/to/dexscreener.db --ledger /tmp/bt_old.db
.venv/bin/python -m decision_engine.backtest.run --strategy factor_jev_strength_v1 --days 7 --dry-run --db /path/to/dexscreener.db --ledger /tmp/bt_new.db
```

Compare `decision_log` counts, distinct tokens, and `reason` for the two strategy ids. Backtest `trades` / `win_rate` / `total_pnl_usd` should match on the same window.

## Step 2/3 (sell mount + tech buy factors)

Registry-only additions. `DECISION_DEFAULT_STRATEGY` is still `jev_strength_v1`. No cron or hook wiring.

New combos: `factor_jev_timeexit_v1` (`time_exit` 3600s), `factor_jev_trail_v1` (`trail_stop` 20%), `factor_rsi_bounce_v1`, `factor_macd_cross_v1`, `factor_boll_breakout_v1`, `factor_jev_rsi_and_v1` (`mode=and`).

`list_strategies()` length is **12** (3 legacy + 3 original factor + 2 sell + 4 tech). The plan text says 10; the listed ids sum to 12.

Backtest ticks pass `open_positions` from the ledger. `time_exit` also accepts ledger `opened_at` and replay `bar_ts`. The engine's own max-hold (default 60 min) runs before the sell factor, so a `time_exit` close shows up when `--max-hold-min` is above 60 and price has not already hit tp/sl. Sample reason: `time_exit age=3600s>=3600s`. `trail_stop` stays quiet: ledger rows have no `peak_price`.

`macd_cross` is a single-bar approximation (`macd > signal` and `macd > 0`). `boll_breakout` returns None on live hot rows that have no `price_usd`. Column name is `rsi_14`. Missing technical columns are dropped via PRAGMA and do not crash the SELECT.
