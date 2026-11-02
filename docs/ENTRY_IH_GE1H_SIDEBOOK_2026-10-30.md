# Entry ih ≥ 1h sidebook (2026-10-30)

Decision-side arm B1. The resident arm stays `purpose=live_candidate` + `selfbuilt_momentum_a_v1` with no ih hard filter. This path does not open live/#71 and does not change live admission.

`entry_ih_age_h` is `now − latest indicator_history.recorded_at` for that token (pair only when history is not token-keyed). Missing history is reject `ih_hist_missing` and is not written as 0. Buys emit only when `entry_ih_age_h >= 1.0`.

## Decision (this change)

Flag `DECISION_ENTRY_IH_SIDEBOOK` defaults to `0`. At `0` the watchdog does not start this loop. At `1` either of these starts it:

```
DECISION_ENTRY_IH_SIDEBOOK=1 scripts/decision_entry_ih_sidebook.sh
```

or the sibling inside `scripts/decision_watchdog.sh`. Command:

```
python -m decision_engine.run --strategy selfbuilt_momentum_a_v1 --dry-run \
  --interval 60 --purpose shadow_entry_ih_ge1h --limit 2000 \
  --token-cooldown-min 60 --position-pct 0.05 --entry-ih-min-hours 1.0 \
  --output-db decision_entry_ih_ge1h.db
```

Indicators still come from the main DB (`--db` / `DECISION_DB_PATH`). Signals go only to `decision_entry_ih_ge1h.db`. `entry_ih_age_h` is on `raw_json.extras`. `--entry-ih-min-hours` is refused for `live_candidate`.

## Execution (not started here)

Do not point main `execution.run` at this store. The main gateway rejects every `shadow_*` purpose, and it refuses a decision DB that already contains those rows.

Consume `purpose=shadow_entry_ih_ge1h` into an isolated paper book:

| Item | Value |
|---|---|
| Decision DB | `decision_entry_ih_ge1h.db` |
| Accept purpose | `shadow_entry_ih_ge1h` only |
| Paper DB | `paper_trading_entry_ih_ge1h.db` |
| Starting cash | `10000` |
| Size | `SIZE_MULT=20` (`EXECUTION_PAPER_SIZE_MULT=20`; decision `size_pct=0.05`) |
| Open cap | `MAX_OPEN=1` (`EXECUTION_MAX_OPEN=1`) |
| Exits | TP10 / SL5 / hold60, same as the main book |

```
python paper_trade.py init --paper-db paper_trading_entry_ih_ge1h.db --cash 10000
```

Keep this book off the main paper DB. Promote only after a full window (`n_closed >= 15`) if expectancy beats the main book and the stop-loss share of losses does not worsen.
