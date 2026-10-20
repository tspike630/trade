# Phase0 影子打标（双列只读）

Observe-only bypass on the indicators side. It tags closed and open paper positions and writes a runtime CSV. It does **not** soft-skip, does **not** change `INDICATORS_WEIGHT_*` (they stay **0**), and does **not** wire a decision or execution gate. It does not write `paper_trading.db` or `.env`.

## Flags

Both columns are written every run. Decision code must not read them.

| Column | Rule |
|---|---|
| `shadow_R3_vol_chg_rsi` | `volatility_24h > 2.5` AND (`change_1h < 0` OR `rsi_14 < 30` OR `rsi_14 >= 70`) |
| `shadow_liq_lt_150k` | **1** only when `liq_usd` is non-NULL **and** `liq_usd < 150000`. NULL or a missing measurement is **not** low liquidity, so the flag stays **0**. |

CSV alias `shadow_skip_stop_prone` mirrors `shadow_R3_vol_chg_rsi` for older readers. It is not a skip.

Features (no look-ahead): `indicator_history` as-of `opened_at` (`recorded_at <= opened_at`, lag ≤ 2h, prefer a row with `rsi_14` set). `liq_usd` comes from `positions.meta_json.liquidity_usd`.

## How to run

From `crawler/`:

```bash
python scripts/shadow_stop_prone_tag.py
python -m indicators.shadow_stop_prone
python scripts/shadow_stop_prone_tag.py --json-stats
```

Read-only inputs: `paper_trading.db`, `dexscreener.db` (`mode=ro`).

Writes: `exports/shadow_skip_stop_prone_live.csv` (runtime export, not committed). Optional `--shadow-db` must be an independent sqlite and must not be `paper_trading.db`.
