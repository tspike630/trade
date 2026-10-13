# Grok crawler report (daily)

generated_at: `2026-10-09T18:35:43Z`
incremental: `False` since=``

## System prompt (excerpt)

```
# Grok Bot — DexScreener Crawler System Prompt

You are an assistant for the **蜘蛛爬虫 / dexscreener-crawler** operator (user 杨).
You ingest periodic context exports and produce concise, actionable market notes.

## Data you receive
- `exports/reports/report-*.md` or `GET /export/context` JSON/Markdown
- Fields: health, stats, top/low quality scores (incl. `risk_*` tags), pair_history samples, recent alerts

## Rules
1. Prefer **high quality_score** and avoid tokens tagged `risk_honeypot*`, `risk_high_tax`, `risk_elevated`, `low_quality`.
2. Treat social spikes + price alerts as **dual signals**; still check liquidity and risk tags.
3. Never invent prices — only cite numbers present in the export.
4. If heartbeat is stale, say the crawler may be offline before drawing conclusions.
5. Output in Chinese unless asked otherwise: 3–8 bullets + one “下一步” line.
6. Do not expose API keys, bearer tokens, or private env values if they appear by mistake — redact.

## Hourly vs daily
- **Hourly:** focus on new alerts, score movers, hot history deltas.
- **Daily:** summarize win themes, risk clusters by chain, backlog (pending_enrich), backup/health.
```

## Task prompt

# Daily report task

Using the attached crawler context export (+ prior report if provided):

1. Chain breakdown: activity and risk concentration.
2. Score distribution themes (high_liq / new / risk_*).
3. History coverage health (pairs with multi-point series).
4. Overnight ERROR/alert volume if mentioned.
5. Three priorities for tomorrow.

## Context export

# DexScreener crawler context

Generated: `2026-10-09T18:35:43Z`
Chains: `solana, ethereum, bsc, base`
Health: **FAIL** — FAIL: heartbeat stale age=843s > 120s status=ok

## Stats

```json
{
  "tokens": 156,
  "pairs": 126,
  "alerts": 152,
  "pair_history_rows": 409,
  "pending_enrich": 1,
  "cycle_number": 18
}
```

## Top quality scores

- `solana` `2j5sas7xy776…` score=**85.0** tags=`["high_liq"]`
- `solana` `7k52ayqw9rwg…` score=**85.0** tags=`["high_liq"]`
- `robinhood` `0xfeb1ab438e…` score=**83.5** tags=`["high_liq"]`
- `solana` `cbcyno7m1amf…` score=**82.0** tags=`["high_liq"]`
- `solana` `8k4sbteek4pf…` score=**82.0** tags=`["high_liq"]`
- `solana` `7vertkgf9klh…` score=**82.0** tags=`["high_liq"]`
- `solana` `4qv31uejjyuv…` score=**80.1** tags=`[]`
- `robinhood` `0x413b3965fa…` score=**80.0** tags=`["new", "high_liq"]`
- `bsc` `0xe39d1870e9…` score=**80.0** tags=`["new", "high_liq"]`
- `solana` `bsjtbwdokgtn…` score=**79.7** tags=`["new"]`
- `solana` `2x3kudsxqcwd…` score=**79.0** tags=`["new", "high_liq"]`
- `solana` `cmvdererz8fr…` score=**78.2** tags=`["new", "high_liq"]`
- `solana` `cbytnf7upzve…` score=**78.0** tags=`["high_liq"]`
- `solana` `pumpcmxqmfrs…` score=**78.0** tags=`["high_liq"]`
- `solana` `himsszzwkzkr…` score=**78.0** tags=`["high_liq"]`
- `solana` `spcxxcqxj6e5…` score=**78.0** tags=`["high_liq"]`
- `solana` `bcdwqban8dyb…` score=**78.0** tags=`["high_liq"]`
- `solana` `ukhh6c7mmyiw…` score=**78.0** tags=`["high_liq"]`
- `solana` `4k3dyjzvzp8e…` score=**78.0** tags=`["high_liq"]`
- `solana` `9vhpldy6wa9z…` score=**77.9** tags=`["new"]`

## Low scores (< ALERT_MIN_SCORE)

- `solana` `8lrmcsjaqqgg…` score=**12.9**
- `solana` `gtj5fqvm5jps…` score=**18.0**
- `solana` `fkyv9v7ahy6c…` score=**20.4**
- `solana` `2tnhwbezhep7…` score=**22.6**

## History samples

### solana `btpair00001111…` (8 pts)
- 2026-10-09T12:00:00Z: price=0.02967986814901928 liq=20000.0 tier=hot
- 2026-10-09T12:00:00Z: price=0.02967986814901928 liq=20000.0 tier=hot
- 2026-10-09T12:00:00Z: price=0.02967986814901928 liq=20000.0 tier=hot
- 2026-10-09T12:00:00Z: price=0.02967986814901928 liq=20000.0 tier=hot
- 2026-10-09T12:05:00Z: price=0.028788449658590527 liq=20000.0 tier=hot
- 2026-10-09T12:05:00Z: price=0.028788449658590527 liq=20000.0 tier=hot
- 2026-10-09T12:05:00Z: price=0.028788449658590527 liq=20000.0 tier=hot
- 2026-10-09T12:05:00Z: price=0.028788449658590527 liq=20000.0 tier=hot
### solana `btpair00011111…` (8 pts)
- 2026-10-09T12:00:00Z: price=0.010399541852516984 liq=20000.0 tier=hot
- 2026-10-09T12:00:00Z: price=0.03150780997309248 liq=20000.0 tier=hot
- 2026-10-09T12:00:00Z: price=0.010399541852516984 liq=20000.0 tier=hot
- 2026-10-09T12:00:00Z: price=0.010399541852516984 liq=20000.0 tier=hot
- 2026-10-09T12:05:00Z: price=0.010589375837754383 liq=20000.0 tier=hot
- 2026-10-09T12:05:00Z: price=0.029119417727191305 liq=20000.0 tier=hot
- 2026-10-09T12:05:00Z: price=0.010589375837754383 liq=20000.0 tier=hot
- 2026-10-09T12:05:00Z: price=0.010589375837754383 liq=20000.0 tier=hot
### solana `btpair00021111…` (8 pts)
- 2026-10-09T12:00:00Z: price=0.004441157084850193 liq=20000.0 tier=hot
- 2026-10-09T12:00:00Z: price=0.004441157084850193 liq=20000.0 tier=hot
- 2026-10-09T12:00:00Z: price=0.004441157084850193 liq=20000.0 tier=hot
- 2026-10-09T12:05:00Z: price=0.004775334066858067 liq=20000.0 tier=hot
- 2026-10-09T12:05:00Z: price=0.004775334066858067 liq=20000.0 tier=hot
- 2026-10-09T12:05:00Z: price=0.004775334066858067 liq=20000.0 tier=hot
- 2026-10-09T12:10:00Z: price=0.004914227743026038 liq=20000.0 tier=hot
- 2026-10-09T12:10:00Z: price=0.004914227743026038 liq=20000.0 tier=hot
### solana `btpair00031111…` (8 pts)
- 2026-10-09T12:00:00Z: price=0.03029570825319876 liq=20000.0 tier=hot
- 2026-10-09T12:00:00Z: price=0.03029570825319876 liq=20000.0 tier=hot
- 2026-10-09T12:00:00Z: price=0.03029570825319876 liq=20000.0 tier=hot
- 2026-10-09T12:05:00Z: price=0.028198095768731224 liq=20000.0 tier=hot
- 2026-10-09T12:05:00Z: price=0.028198095768731224 liq=20000.0 tier=hot
- 2026-10-09T12:05:00Z: price=0.028198095768731224 liq=20000.0 tier=hot
- 2026-10-09T12:10:00Z: price=0.027120126820946562 liq=20000.0 tier=hot
- 2026-10-09T12:10:00Z: price=0.027120126820946562 liq=20000.0 tier=hot
### solana `btpair00041111…` (8 pts)
- 2026-10-09T12:00:00Z: price=0.014665640555209058 liq=20000.0 tier=hot
- 2026-10-09T12:00:00Z: price=0.014665640555209058 liq=20000.0 tier=hot
- 2026-10-09T12:00:00Z: price=0.014665640555209058 liq=20000.0 tier=hot
- 2026-10-09T12:05:00Z: price=0.015213052097330898 liq=20000.0 tier=hot
- 2026-10-09T12:05:00Z: price=0.015213052097330898 liq=20000.0 tier=hot
- 2026-10-09T12:05:00Z: price=0.015213052097330898 liq=20000.0 tier=hot
- 2026-10-09T12:10:00Z: price=0.01567077215285752 liq=20000.0 tier=hot
- 2026-10-09T12:10:00Z: price=0.01567077215285752 liq=20000.0 tier=hot

## Recent alerts

- `2026-10-09T18:21:40Z` **price_change** (medium) priceUsd changed +24.9% (1.73 → 2.16) threshold=20.0%
- `2026-10-09T18:21:40Z` **price_change** (medium) priceUsd changed -35.1% (0.001139 → 0.0007388) threshold=20.0%
- `2026-10-09T18:21:39Z` **liquidity_drop** (high) liquidity.usd dropped 31.5% (4267.2 → 2924.22) threshold=30.0%
- `2026-10-09T18:21:39Z` **price_change** (medium) priceUsd changed -38.7% (4.9e-06 → 3.005e-06) threshold=20.0%
- `2026-10-09T18:21:38Z` **price_change** (medium) priceUsd changed -28.6% (3.399e-06 → 2.426e-06) threshold=20.0%
- `2026-10-09T18:21:37Z` **liquidity_drop** (high) liquidity.usd dropped 94.9% (44107.05 → 2245.21) threshold=30.0%
- `2026-10-09T18:21:37Z` **price_change** (high) priceUsd changed -99.2% (0.0002618 → 2.146e-06) threshold=20.0%
- `2026-10-09T18:21:36Z` **liquidity_drop** (high) liquidity.usd dropped 58.0% (59676.9 → 25083.13) threshold=30.0%
- `2026-10-09T18:21:36Z` **volume_h24_jump** (medium) volume.h24 changed +193.6% (21208.89 → 62268.4) threshold=50.0%
- `2026-10-09T18:21:35Z` **price_change** (high) priceUsd changed -80.8% (0.0004907 → 9.407e-05) threshold=20.0%
- `2026-10-09T18:21:35Z` **volume_h24_jump** (medium) volume.h24 changed +138.5% (5111.64 → 12189.94) threshold=50.0%
- `2026-10-09T18:21:34Z` **liquidity_drop** (high) liquidity.usd dropped 33.1% (4619.63 → 3091.75) threshold=30.0%
- `2026-10-09T18:21:34Z` **price_change** (high) priceUsd changed -41.2% (5.452e-06 → 3.208e-06) threshold=20.0%
- `2026-10-09T18:21:33Z` **volume_h24_jump** (medium) volume.h24 changed +124.4% (6525.08 → 14643.14) threshold=50.0%
- `2026-10-09T18:21:33Z` **price_change** (high) priceUsd changed -85.6% (2.142e-05 → 3.085e-06) threshold=20.0%
- `2026-10-09T18:21:32Z` **price_change** (medium) priceUsd changed +22.1% (0.0004272 → 0.0005215) threshold=20.0%
- `2026-10-09T18:21:32Z` **liquidity_drop** (high) liquidity.usd dropped 97.0% (64664.29 → 1949.8) threshold=30.0%
- `2026-10-09T18:21:31Z` **volume_h24_jump** (medium) volume.h24 changed +146.9% (22525.65 → 55616.42) threshold=50.0%
- `2026-10-09T18:21:30Z` **price_change** (high) priceUsd changed -99.7% (0.0005733 → 1.936e-06) threshold=20.0%
- `2026-10-09T18:21:30Z` **liquidity_drop** (high) liquidity.usd dropped 73.4% (7427.9 → 1979.21) threshold=30.0%
- `2026-10-09T18:21:29Z` **price_change** (high) priceUsd changed -82.5% (1.119e-05 → 1.953e-06) threshold=20.0%
- `2026-10-09T18:21:28Z` **volume_h24_jump** (medium) volume.h24 changed +271.2% (770555.44 → 2859946.38) threshold=50.0%
- `2026-10-09T18:21:28Z` **price_change** (high) priceUsd changed +77.0% (8.053e-06 → 1.425e-05) threshold=20.0%
- `2026-10-09T18:21:27Z` **price_change** (medium) priceUsd changed -29.0% (0.0001008 → 7.156e-05) threshold=20.0%
- `2026-10-09T18:21:27Z` **liquidity_drop** (high) liquidity.usd dropped 100.0% (49349.34 → 20.81) threshold=30.0%
- `2026-10-09T18:21:26Z` **volume_h24_jump** (medium) volume.h24 changed +262.5% (527759.52 → 1912939.17) threshold=50.0%
- `2026-10-09T18:21:26Z` **price_change** (high) priceUsd changed -99.0% (0.0002691 → 2.605e-06) threshold=20.0%
- `2026-10-09T18:21:25Z` **liquidity_drop** (high) liquidity.usd dropped 97.2% (88655.84 → 2488.11) threshold=30.0%
- `2026-10-09T18:21:25Z` **price_change** (high) priceUsd changed -99.7% (0.000832 → 2.484e-06) threshold=20.0%
- `2026-10-09T18:12:11Z` **price_change** (medium) priceUsd changed -23.7% (5.743e-05 → 4.381e-05) threshold=20.0%
- `2026-10-09T18:12:11Z` **price_change** (high) priceUsd changed +67.1% (2.932e-06 → 4.9e-06) threshold=20.0%
- `2026-10-09T18:12:10Z` **liquidity_drop** (high) liquidity.usd dropped 32.2% (4397.84 → 2983.68) threshold=30.0%
- `2026-10-09T18:12:10Z` **price_change** (high) priceUsd changed -40.0% (5.116e-06 → 3.069e-06) threshold=20.0%
- `2026-10-09T18:12:09Z` **price_change** (medium) priceUsd changed +38.4% (0.0002724 → 0.0003769) threshold=20.0%
- `2026-10-09T18:12:09Z` **volume_h24_jump** (medium) volume.h24 changed +150.0% (32.06 → 80.15) threshold=50.0%
- `2026-10-09T18:12:08Z` **price_change** (high) priceUsd changed -86.8% (6.096e-06 → 8.075e-07) threshold=20.0%
- `2026-10-09T18:12:08Z` **price_change** (high) priceUsd changed +51.0% (5.481e-06 → 8.276e-06) threshold=20.0%
- `2026-10-09T18:12:07Z` **price_change** (medium) priceUsd changed -26.2% (7.5e-06 → 5.536e-06) threshold=20.0%
- `2026-10-09T18:12:06Z` **price_change** (medium) priceUsd changed -37.4% (8.086e-05 → 5.061e-05) threshold=20.0%
- `2026-10-09T18:12:06Z` **price_change** (high) priceUsd changed -50.8% (8.86e-06 → 4.355e-06) threshold=20.0%

