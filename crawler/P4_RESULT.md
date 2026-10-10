# P4 Result — Grok reports, GoPlus risk, multi-strategy backtest, paper trade

**Time (HK / UTC+8):** 2026-10-10 ~02:32+ HKT

## 1) Grok report automation

| Artifact | Role |
|---|---|
| `GROK_SYSTEM_PROMPT.md` | System prompt for 蜘蛛爬虫 / Grok Bot |
| `prompts/hourly_report.md` / `daily_report.md` | Task prompts |
| `scripts/grok_report.py` | Hourly/daily export → markdown+JSON; `--incremental`; optional Telegram |

```bash
python scripts/grok_report.py --mode hourly --incremental
python scripts/grok_report.py --mode daily
# optional: --telegram or GROK_REPORT_TELEGRAM=1 → ADMIN chat
```

State cursor: `exports/grok_report_state.json`. Cron tips in `deploy/crontab.example`.

## 2) Third-party risk (GoPlus-style)

- `GoPlusRiskScanner` builds `/api/v1/token_security/{chainId}` or solana endpoint from `RISK_API_URL` base.
- Enabled **only when `RISK_API_URL` is set**; otherwise heuristic-only (demo without key).
- HTTP failure → `fallback_heuristic(...)` (graceful).
- `RISK_API_KEY` never logged.

```bash
export RISK_API_URL=https://api.gopluslabs.io
export RISK_API_KEY='...'   # optional
python demo_p4.py
```

## 3) Multi-strategy backtest

```bash
python backtest.py --strategy hold --buy-after 5 --hold 30 --seed-demo
python backtest.py --strategy tpsl --tp 10 --sl 5 --hold 60 --seed-demo
python backtest.py --strategy grid --grid-step 3 --grid-levels 3 --hold 60 --seed-demo
```

Exit reasons reported for tpsl (`take_profit` / `stop_loss` / `hold_expiry`).

## 4) Paper trading

SQLite ledger `PAPER_DB_PATH` (default `paper_trading.db`): cash, positions, fills; TP/SL/max-hold on `tick`.

```bash
python paper_trade.py init
python paper_trade.py demo
python paper_trade.py status
```

## Verification

```bash
python demo_p4.py   # DRY_RUN_OK
```

Observed: hourly report written; GoPlus fixture → `risk_high_tax`; remote 429 → fallback; tpsl/grid trades; paper position closed on SL.

Pipeline enrich path unchanged (risk still inside scoring only when URL configured for remote).
