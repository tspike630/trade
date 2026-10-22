---
title: Paper Notional Size Multiplier - Plan
type: feat
date: 2026-10-22
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-plan-bootstrap
execution: code
---

# Paper Notional Size Multiplier - Plan

## Goal Capsule

**Objective:** An operator can scale main paper-book buy notionals by a reversible environment setting, and every fill still spends only cash the book already has.

**Means:** Multiply the paper buy's equity notional by `EXECUTION_PAPER_SIZE_MULT` inside `PaperExecutor` before the existing cash cap, and record the multiplier for audit (KTD1).

**Authority:** The product decisions in Key Decisions outrank this plan. If implementing the multiplier requires a live gate, the c_exit shadow size switch, a step2 bypass, or a committed secret, stop.

**Execution profile:** Code change on the main paper buy path. Ship as a pull request. After merge, the operator sets `EXECUTION_PAPER_SIZE_MULT=2` in the paper process environment. The code default stays 1.0.

## Product Contract

### Summary

The main paper book gains one environment knob that scales buy notional after `size_pct * equity_usd` and before the cash truncate. Unset or unusable values leave today's 1x behavior. Live trading and the c_exit shadow book are unchanged.

### Problem Frame

Tonight's paper book should observe a nominal 2x size without inventing margin and without opening live trading. The buy path already computes an equity notional and then refuses to spend more than cash. What is missing is a reversible multiplier on that notional, plus an audit trail so a later read can tell a 2x fill from a larger `size_pct`.

### Key Decisions

- **Paper book only.** (session-settled: user-directed — chosen over applying the multiplier on live paths or the c_exit shadow book: the correction is paper notional only.) Governs R1, R5.
- **Code default stays 1.0.** (session-settled: user-directed — chosen over baking 2 into the code default: 2x is an operator setting after merge, and an unset process must stay 1x.) Governs R2.
- **Cash is the hard ceiling.** (session-settled: user-directed — chosen over margin or borrow: the multiplier may ask for more notional, and the fill still stops at cash on hand.) Governs R3.
- **Step2 gates stay as they are.** (session-settled: user-directed — chosen over bypassing `#71` / `step2_blocked` in jev validity or the weekly check: paper opens do not go through that gate.) Governs R5.

### Requirements

- R1. A main-book paper buy spends `min(size_pct * equity_usd * paper_size_mult, cash)` and does not change sell sizing.
- R2. `EXECUTION_PAPER_SIZE_MULT` applies as a float. Unset, blank, non-numeric, non-finite, or `≤ 0` applies `1.0`.
- R3. The multiplier never increases cash and never lets a fill exceed cash.
- R4. The position meta and the buy result both record `paper_size_mult` and the pre-multiplier requested notional. The stored `size_pct` stays the signal fraction.
- R5. Live trading flags and gates, `execution/c_exit_size_mult.py`, jev validity, the weekly step2 check, and `PAPER_OPEN_PAUSED` are not modified. No `.env` or secret is committed.
- R6. `docs/RECOVERY_EXECUTION.md` documents the variable in one short paragraph: code default `1.0`, cash cap still applies, live and the c_exit shadow book are untouched, and `2` is the operator setting for tonight's paper book rather than a committed default.

### Scope Boundaries

In scope: `PaperExecutor._buy`, one unit test module, and the recovery paragraph.

Out of scope: unpausing paper opens, changing PM pct caps, scaling stored `size_pct`, enabling shadow `size_mult`, and any live path.

### Deferred to Follow-Up Work

None in this change. A later change could teach PM exposure to read filled USD. This plan does not do that.

### Acceptance Examples

- AE1. Equity `$10,000`, cash `$10,000`, `size_pct` `0.1`, multiplier `2`: the buy requests `$2,000` and fills `$2,000`.
- AE2. Same book, `size_pct` `0.8`, multiplier `2`: the buy requests `$16,000` and fills `$10,000` with truncation set.
- AE3. Same book, `size_pct` `0.1`, variable unset: the buy requests `$1,000` and fills `$1,000` with multiplier `1.0`.

### Sources

`execution/executors/paper_executor.py` `_buy` sets `requested_usd = size_pct * equity_usd`, then assigns `size_usd = cash` when the request exceeds cash (`execution/executors/paper_executor.py` around the buy sizing block). `requested_usd` is written only there. `crawler/execution` is a symlink to `execution/`. `execution/c_exit_size_mult.py` states that shadow state never touches the main paper book. PM caps compare signal `size_pct` with `meta.size_pct` (`execution/position_manager.py`). New paper ledgers start at `PAPER_START_CASH` (`crawler/config.py`, default `10000`).

## Planning Contract

### Key Technical Decisions

- KTD1. Apply the multiplier only in `PaperExecutor._buy`, after `size_pct * equity_usd` and before the cash comparison. Sells stay a fraction of the open position. (session-settled: user-directed — chosen over a gateway or PM size rewrite: the hypothesized buy-path insertion matches the code, and PM 全仓 accounting stays on the signal fraction.) Governs R1, R5.
- KTD2. After the multiply, `requested_usd` means the post-multiplier amount presented to the cash cap. `requested_usd_before_mult` is `size_pct * equity_usd`. Both the top-level buy result and `meta` carry `paper_size_mult` and `requested_usd_before_mult`, including the `insufficient_cash` result. The existing receipt already lifts `requested_usd` and `truncated` beside `meta`; the new fields follow that shape. `meta.size_pct` is not multiplied, so PM pct caps keep counting the signal fraction.
- KTD3. Read `EXECUTION_PAPER_SIZE_MULT` on each buy. Do not snapshot it at import time, and do not reuse `execution/config.py` risk parsers. Their `≤ 0` result means "rule off", which is a different default than "size multiplier 1.0".

### Assumptions

- The audit field name is `requested_usd_before_mult`.
- Non-finite values (`nan`, infinities) count as invalid and apply `1.0`.
- PM will keep under-counting notional while `meta.size_pct` stays the signal fraction. That is accepted so this change does not alter 全仓 pct caps. The cash cap is the bound on deployed USD.
- Operators change the knob by restarting the paper process with a new environment. This plan does not hot-reload a `.env` file.

### Sequencing

U1 lands the behavior and tests. U2 documents the variable with the field names U1 uses.

## Implementation Units

### U1. Paper buy notional multiplier

**Goal:** Scale main-book paper buy notional by the environment multiplier, then keep the cash cap, and persist the audit fields.

**Requirements:** R1, R2, R3, R4, R5.

**Dependencies:** None.

**Files:**
- `execution/executors/paper_executor.py`
- `execution/tests/test_paper_size_mult.py`

**Approach:**
- Add a buy-time reader that returns `1.0` for unset, blank, non-numeric, non-finite, and `≤ 0` values, otherwise the parsed float (KTD3, R2).
- In `_buy`, keep `requested_usd_before_mult = size_pct * equity_usd`, set `paper_size_mult` from that reader, then set `requested_usd` to the product. The existing `size_usd > cash` truncate stays on that post-multiplier `requested_usd` (KTD1, R1, R3).
- Copy `paper_size_mult` and `requested_usd_before_mult` into `meta` and into both the success result and the `insufficient_cash` result. Leave `meta.size_pct` equal to the signal `size_pct` (KTD2, R4).
- Do not import `execution.c_exit_size_mult`, and do not edit live modules, PM, gateway purpose gates, jev validity, or the weekly check (R5).

**Patterns to follow:** The cash truncate and receipt fields already in `_buy`. Sibling tests build a temp crawler DB plus `PaperExecutor` (`execution/tests/test_f1_f3_g3.py` `_PriceDB`, `execution/tests/test_purpose_gate.py` path setup). High liquidity in the fixture keeps the test about notional, not slippage. `open_position` spends `size_usd` as cash (`crawler/paper_trade.py`).

**Test scenarios:**
- Unset `EXECUTION_PAPER_SIZE_MULT`, equity and cash `$10,000`, `size_pct` `0.1`: `requested_usd_before_mult` is `1000`, `requested_usd` is `1000`, `size_usd` is `1000`, `paper_size_mult` is `1.0`, `truncated` is false, and the same values are in `positions.meta_json`. Covers AE3.
- Multiplier `2`, same book, `size_pct` `0.1`: pre-multiplier request is `1000`, `requested_usd` and `size_usd` are `2000`, cash left is `8000`, `truncated` is false, meta matches the result. Covers AE1.
- Multiplier `2`, same book, `size_pct` `0.8`: pre-multiplier request is `8000`, `requested_usd` is `16000`, `size_usd` is `10000`, `truncated` is true, cash left is `0`, and the ledger does not reject the open. Covers AE2.
- Blank, `0`, `-1`, and `nope` each fill as multiplier `1.0` on the `size_pct` `0.1` book.
- A sell signal with multiplier `2` still uses the open position fraction and does not multiply a second time.

**Verification:** The new tests pass, and an existing paper fill with the variable unset still opens one position (`execution.tests.test_purpose_gate.PurposeGateTests.test_real_paper_fills_live_candidate_only`).

### U2. Document the paper size knob

**Goal:** Leave one paragraph an operator can read after a restart without opening the executor.

**Requirements:** R6.

**Dependencies:** U1.

**Files:**
- `docs/RECOVERY_EXECUTION.md`

**Approach:**
- Add one short paragraph near the paper gate notes in section 5. State the variable name, the code default `1.0`, the invalid-value fallback, the cash cap, that PM `size_pct` accounting is unchanged, and that live trading and c_exit shadow `size_mult` are not this knob. Say that `2` is the agreed paper-process setting after this change merges, and that it is not written into a repo `.env`.

**Test expectation:** none — documentation only. U1 covers the behavior the paragraph describes.

**Verification:** The paragraph names `EXECUTION_PAPER_SIZE_MULT` and does not instruct a commit of `.env` or a live-flag change.

## Verification Contract

From the repo root, with the test module inserting the repo root and `crawler/` on `sys.path` the way `execution/tests/test_purpose_gate.py` does:

```
python -m unittest execution.tests.test_paper_size_mult -v
python -m unittest execution.tests.test_purpose_gate.PurposeGateTests.test_real_paper_fills_live_candidate_only -v
```

No live order path, no shadow `size_mult` test change, and no `.env` diff.

## Definition of Done

- U1 tests cover AE1–AE3, the invalid-value fallback, and the cash truncate.
- U2's paragraph matches the implemented field and default.
- The diff does not touch `LIVE_TRADING_*`, `live_trade.py`, `execution/c_exit_size_mult.py`, jev validity, the weekly step2 check, or `PAPER_OPEN_PAUSED`.
- No `.env`, key, or database file is committed.
- Experimental code from abandoned approaches is not left in the diff.
- The pull request body tells the operator to set `EXECUTION_PAPER_SIZE_MULT=2` after merge for tonight's paper 2x, and states that live trading is untouched.
