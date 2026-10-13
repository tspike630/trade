# Grok crawler report (hourly)

generated_at: `2026-10-13T01:47:03Z`
incremental: `True` since=`2026-10-12T09:52:03Z`

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

# Hourly report task

Using the attached crawler context export:

1. List up to 5 noteworthy tokens (score, chain, risk tags).
2. Call out new/high-severity alerts in the last window.
3. Note any dual price+social or elevated risk.
4. End with one recommended operator action (e.g. raise ALERT_MIN_SCORE, trim ENABLED_CHAINS).

## Context export

# DexScreener crawler context

Generated: `2026-10-13T01:47:03Z`
Chains: `solana, ethereum, bsc, base`
Health: **OK** — OK: age=101s status=ok cycle_id=20261013T014519Z-d120e20f

## Stats

```json
{
  "tokens": 67573,
  "pairs": 31063,
  "alerts": 129633,
  "pair_history_rows": 1131648,
  "pending_enrich": 13750,
  "cycle_number": 2780
}
```

## Top quality scores

- `solana` `8timkgvsrat9…` score=**92.9** tags=`["high_liq", "high_social"]`
- `solana` `eqawfcwutts6…` score=**92.9** tags=`["high_liq", "high_social"]`
- `solana` `7k52ayqw9rwg…` score=**92.0** tags=`["high_liq", "high_social"]`
- `solana` `2j5sas7xy776…` score=**91.2** tags=`["high_liq", "high_social"]`
- `solana` `aq2y8twm9eeb…` score=**91.2** tags=`["high_liq", "high_social"]`
- `solana` `9twd5vxapysw…` score=**91.2** tags=`["high_liq", "high_social"]`
- `solana` `6mix12lihrqf…` score=**91.2** tags=`["high_liq", "high_social"]`
- `solana` `2kbrlsbvexdf…` score=**91.2** tags=`["high_liq", "high_social"]`
- `solana` `aepbdj3rvimj…` score=**90.6** tags=`["high_liq", "high_social"]`
- `solana` `hzychqan2uoh…` score=**90.3** tags=`["high_social"]`
- `solana` `7vertkgf9klh…` score=**89.9** tags=`["high_liq", "high_social"]`
- `solana` `ripsrnkeflpk…` score=**89.6** tags=`["high_liq", "high_social"]`

## Low scores (< ALERT_MIN_SCORE)


## History samples

### solana `91kvls6amtyckg…` (8 pts)
- 2026-10-09T18:21:34Z: price=0.0008699 liq=93640.29 tier=hot
- 2026-10-09T18:47:15Z: price=0.0008699 liq=93640.29 tier=hot
- 2026-10-09T18:52:49Z: price=0.0008699 liq=93640.29 tier=hot
- 2026-10-09T18:58:37Z: price=0.0008699 liq=93640.29 tier=hot
- 2026-10-09T19:04:13Z: price=0.0008699 liq=93640.29 tier=hot
- 2026-10-09T19:09:53Z: price=0.0008699 liq=93640.29 tier=hot
- 2026-10-09T19:15:33Z: price=0.0008699 liq=93640.29 tier=hot
- 2026-10-09T19:21:10Z: price=0.0008699 liq=93640.29 tier=hot
### solana `bofa2viusudpbt…` (8 pts)
- 2026-10-09T18:21:35Z: price=0.005414 liq=2138392.89 tier=hot
- 2026-10-09T18:47:16Z: price=0.005318 liq=2138528.49 tier=hot
- 2026-10-09T18:52:49Z: price=0.005337 liq=2144724.04 tier=hot
- 2026-10-09T18:58:37Z: price=0.005329 liq=2143138.24 tier=hot
- 2026-10-09T19:04:12Z: price=0.005283 liq=2133120.06 tier=hot
- 2026-10-09T19:09:53Z: price=0.005313 liq=2139694.16 tier=hot
- 2026-10-09T19:15:33Z: price=0.005297 liq=2135522.99 tier=hot
- 2026-10-09T19:21:10Z: price=0.005301 liq=2135901.92 tier=hot
### solana `2axxcn6on9bbt5…` (8 pts)
- 2026-10-09T18:21:32Z: price=2.36 liq=4139136.45 tier=hot
- 2026-10-09T18:47:21Z: price=2.34 liq=4119968.93 tier=hot
- 2026-10-09T18:52:49Z: price=2.34 liq=4122019.78 tier=hot
- 2026-10-09T18:58:37Z: price=2.34 liq=4119823.66 tier=hot
- 2026-10-09T19:04:13Z: price=2.33 liq=4113623.79 tier=hot
- 2026-10-09T19:09:53Z: price=2.33 liq=4116691.74 tier=hot
- 2026-10-09T19:15:33Z: price=2.33 liq=4119461.08 tier=hot
- 2026-10-09T19:21:10Z: price=2.33 liq=4118681.11 tier=hot
### solana `c2bw9dvjeywm9z…` (8 pts)
- 2026-10-09T18:51:42Z: price=0.04119 liq=561756.21 tier=warm
- 2026-10-09T18:57:32Z: price=0.04141 liq=563266.47 tier=hot
- 2026-10-09T19:03:08Z: price=0.04141 liq=563137.3 tier=hot
- 2026-10-09T19:08:41Z: price=0.04199 liq=567419.96 tier=hot
- 2026-10-09T19:14:28Z: price=0.04221 liq=568472.77 tier=hot
- 2026-10-09T19:20:04Z: price=0.04249 liq=570243.7 tier=hot
- 2026-10-09T19:25:39Z: price=0.04275 liq=571769.28 tier=hot
- 2026-10-09T19:31:23Z: price=0.04299 liq=572970.48 tier=hot
### solana `evaonvtn4xg64w…` (8 pts)
- 2026-10-09T18:50:40Z: price=0.0002728 liq=46127.55 tier=warm
- 2026-10-09T18:56:24Z: price=0.0002738 liq=46252.0 tier=hot
- 2026-10-09T19:02:02Z: price=0.0002748 liq=46330.43 tier=hot
- 2026-10-09T19:07:28Z: price=0.0002742 liq=46305.58 tier=hot
- 2026-10-09T19:13:20Z: price=0.0002782 liq=46618.24 tier=hot
- 2026-10-09T19:18:54Z: price=0.0002791 liq=46695.25 tier=hot
- 2026-10-09T19:24:31Z: price=0.00028 liq=46752.86 tier=hot
- 2026-10-09T19:30:16Z: price=0.0002817 liq=46914.17 tier=hot

## Recent alerts

- `2026-10-13T01:47:03Z` **new_listing** (info) helius new mint hint: solana/1v7bhrjdknkzs66aisyet881holnemdg8gzarvdtkaly
- `2026-10-13T01:47:02Z` **liquidity_drop** (high) liquidity.usd dropped 100.0% (68373674.12 → 13851.68) threshold=30.0%
- `2026-10-13T01:47:01Z` **price_change** (high) priceUsd changed -100.0% (0.06537 → 1.327e-05) threshold=20.0%
- `2026-10-13T01:46:59Z` **new_listing** (info) helius new mint hint: solana/4nrsznzqtdy3p3sszywgpzgoahmxvqosk2p7nbf1dyav
- `2026-10-13T01:46:59Z` **liquidity_drop** (high) liquidity.usd dropped 49.8% (6690.82 → 3358.13) threshold=30.0%
- `2026-10-13T01:46:57Z` **liquidity_drop** (high) liquidity.usd dropped 100.0% (2844142860.2 → 573786.0) threshold=30.0%
- `2026-10-13T01:46:55Z` **price_change** (high) priceUsd changed -100.0% (1406560.63 → 283.78) threshold=20.0%
- `2026-10-13T01:46:55Z` **volume_h24_jump** (medium) volume.h24 changed +645.7% (2.91 → 21.7) threshold=50.0%
- `2026-10-13T01:46:53Z` **price_change** (high) priceUsd changed +116.2% (0.0001063 → 0.0002298) threshold=20.0%
- `2026-10-13T01:46:53Z` **price_change** (high) priceUsd changed -83.3% (1.947e-06 → 3.243e-07) threshold=20.0%
- `2026-10-13T01:46:53Z` **volume_h24_jump** (medium) volume.h24 changed +71.9% (1361.4 → 2340.22) threshold=50.0%
- `2026-10-13T01:46:53Z` **price_change** (high) priceUsd changed -64.1% (6.402e-07 → 2.3e-07) threshold=20.0%
- `2026-10-13T01:46:53Z` **price_change** (high) priceUsd changed -98.5% (4.935e-07 → 7.288e-09) threshold=20.0%
- `2026-10-13T01:46:51Z` **liquidity_drop** (high) liquidity.usd dropped 96.8% (59090.59 → 1893.39) threshold=30.0%
- `2026-10-13T01:46:51Z` **volume_h24_jump** (medium) volume.h24 changed +57.7% (50828.59 → 80173.38) threshold=50.0%
- `2026-10-13T01:46:51Z` **price_change** (high) priceUsd changed -99.6% (0.0005008 → 1.877e-06) threshold=20.0%
- `2026-10-13T01:46:39Z` **new_listing** (info) helius new mint hint: solana/6yngscm4xnssuhjhzr98s7vgnjvvvxvfwnemy2hwahhc
- `2026-10-13T01:46:37Z` **new_listing** (info) helius new mint hint: solana/wxtrcaws9ydf78azjotg3w78nvj2jqrhp3tefmbfs6p
- `2026-10-13T01:46:34Z` **new_listing** (info) helius new mint hint: solana/ejgt5gvujt4gdzwzovos8pi9xyse2yv1ms6x92mw1jmk
- `2026-10-13T01:46:34Z` **new_listing** (info) helius new mint hint: solana/91h5m4sjdakfyfvnajpwnqo3qqa79cfgsdtmt5vd7nsd
- `2026-10-13T01:46:31Z` **new_listing** (info) helius new mint hint: solana/59sag6gegcghrnpun3spvawv8fz9bm2nugfhpay5ifj4
- `2026-10-13T01:46:31Z` **new_listing** (info) helius new mint hint: solana/2jg2qt4p7rkkew5dqxggxvlzsv2l4vfxf3ad8bvtzaqf
- `2026-10-13T01:46:27Z` **new_listing** (info) helius new mint hint: solana/cnatrehexuibzkww8s5cbljq5ugzvmgt9eulehhgbtsb
- `2026-10-13T01:46:27Z` **new_listing** (info) helius new mint hint: solana/8vtc9999ygs9dqm6s17jmyhxt9euyzup83cdp8hhes6k
- `2026-10-13T01:46:27Z` **new_listing** (info) helius new mint hint: solana/dbqy1hpjekwa1actbesx7bhxwmh5xgtrajqn81yyt5nj
- `2026-10-13T01:46:26Z` **new_listing** (info) helius new mint hint: solana/cctitifinrp2kbwgwqdsinxxy5ezrj667v33vdrdoyt4
- `2026-10-13T01:46:24Z` **new_listing** (info) helius new mint hint: solana/4kpne258q5krexrpscq4rhenune8pe7sshxz19qp7foq
- `2026-10-13T01:46:22Z` **new_listing** (info) helius new mint hint: solana/hr4xstayhwpq5ckpcbrl1cz7nydbyf9ughrwqdgckn1h
- `2026-10-13T01:46:22Z` **new_listing** (info) helius new mint hint: solana/717d5ge8qg5fctqcjjdcjwqbr3bzuma2angejyuhiqs7
- `2026-10-13T01:46:22Z` **new_listing** (info) helius new mint hint: solana/4qdcpubwsihknhvwmphu4ya7zsgpudqhby2d4qnp2phq
- `2026-10-13T01:46:22Z` **new_listing** (info) helius new mint hint: solana/3cr48y9vankds9t3hrlatuo2puhnuj5r5tttrh1w7nwm
- `2026-10-13T01:46:21Z` **new_listing** (info) helius new mint hint: solana/gvqtptneav2x7bejaf6azrley4coetnw4b3ujuhl9eqt
- `2026-10-13T01:46:21Z` **new_listing** (info) helius new mint hint: solana/9wrpyb5alsjjg15ntfutgrqypjab7a3hlggot67zvh79
- `2026-10-13T01:46:21Z` **new_listing** (info) helius new mint hint: solana/bcwrb2hjr6jjvggd39pkehufrmb18yva7r5h4kgsqr8b
- `2026-10-13T01:46:19Z` **new_listing** (info) helius new mint hint: solana/1xv9coi7lbt3zfzjuzfdrhoqostmwvr7z6sulscmvyua
- `2026-10-13T01:46:17Z` **new_listing** (info) helius new mint hint: solana/3ljkysjhnrtgdtu4k5i9af68opnq3dxoc2fggsnszgks
- `2026-10-13T01:46:16Z` **new_listing** (info) helius new mint hint: solana/2mdqqnttkzcmhlmksqz3lua8bpkoe68qvjayfrmwiga7
- `2026-10-13T01:46:12Z` **new_listing** (info) helius new mint hint: solana/b8v13wuctxgqekffo8zfrcuvupda5njykcjcuyzgxji7
- `2026-10-13T01:46:12Z` **new_listing** (info) helius new mint hint: solana/3fhzzyzhpembwd9rajyhmo8t7tfdt9d7luiztuw8qsh1
- `2026-10-13T01:46:11Z` **new_listing** (info) helius new mint hint: solana/65zxww5fcphv6ney6tb5uf63p2ca74bbismvyeg9dm1u

