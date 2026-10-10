# P5 Deep Result — Live skeleton, strategies, arb signals, DD reports

**Time (HK / UTC+8):** 2026-10-10 ~02:40+ HKT

## Safety first (live trading)

| Gate | Default |
|---|---|
| `LIVE_TRADING_ENABLED` | **0** |
| Kill-switch file | `LIVE_TRADING_DISABLED` (shipped in repo) |
| Confirm phrase | `LIVE_TRADING_CONFIRM=I_UNDERSTAND_THE_RISKS` |
| Size caps | `LIVE_MAX_ORDER_USD=25`, `LIVE_MAX_DAILY_NOTIONAL_USD=100` |
| Secrets | env only — never logged / never in git |

`python live_trade.py intent …` logs every intent to `logs/live_trade_intents.jsonl` and **dry-runs** unless all gates open.  
`python live_trade.py checklist` prints paper → small live steps.

Verified: `gates_open=false`, dry-run fill OK, oversize rejected.

## Strategies (backtest + paper)

| Strategy | CLI |
|---|---|
| DCA | `--strategy dca --dca-steps 3 --dca-interval 10` |
| Martingale (capped) | `--strategy martingale --martingale-max-adds 3 --martingale-drop 5` |
| Trend-follow | `--strategy trend --trend-lookback 3` |

```bash
python backtest.py --strategy dca --seed-demo
python paper_trade.py simulate --strategy dca --size-usd 50
```

## Arb scan (not executable)

```bash
python arb_scan.py --dry-run
python arb_scan.py   # emit type=arb_opportunity (cooldown applies)
```

Compares DB prices by symbol across chains/pairs; documents bridge/fee risk — **no router calls**.

## Grok deep DD

```bash
python scripts/grok_dd_report.py --chain solana --token <addr>
# → exports/reports/dd-*.md (+ optional --telegram)
```

Prompt: `prompts/dd_report.md`. Assembles quality/risk/history/social/alerts.

## Verification

```bash
python demo_p5.py   # DRY_RUN_OK
```

Live trading remains **disabled** by default after this PR.
