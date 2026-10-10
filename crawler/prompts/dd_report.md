# Deep due-diligence task

Produce a deep research note for the subject token using ONLY the attached context.

Sections (use these headings):
1. Snapshot — chain, address, symbol, quality score, tags
2. Risk — risk_* tags, deductions, thin liq / honeypot-like / tax flags; unknown = say unknown
3. 技术面 Technical — change windows (5m/15m/1h/4h/24h), volatility_24h, volume_strength; brief trend note from signs of changes (若 context 无指标写「暂无数据」)
4. 资金面 Funding — JEV score / signal / confidence / strength（若无写「暂无数据」）
5. 舆情面 Sentiment — mention growth (1h/6h/24h), heat_score, polarity / sentiment_score（若 context 无 sentiment_* 写「暂无数据」）
6. Market structure — liquidity, volume, txns, pair_history path (direction, volatility)
7. Social / alerts — X mentions if present, recent alert types
8. Cross-venue — any arb_opportunity or multi-chain price notes
9. Verdict — Watch / Avoid / Needs data (one choice) + 3 bullets why
10. Open questions — what the crawler cannot see (LP lock, mint authority, etc.)

Rules: no invented prices; cite numbers from context; Chinese OK if operator prefers; missing indicator blocks must say 暂无数据 — do not fail the whole report.
