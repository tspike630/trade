"""Crawl pipeline: tiers + async enrich + validation (Phase 1–3)."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from alerts import detect_pair_anomalies, emit_alert
from chains import chain_enabled
from history import maybe_snapshot_pair
from scoring import score_token_from_store
from auto_hooks import (
    maybe_auto_dd,
    maybe_paper_auto_open,
    note_new_listing_for_dd,
    note_new_listing_for_jev,
)
from client import AsyncDexScreenerClient
from config import (
    ASYNC_MAX_CONCURRENCY,
    BATCH_SIZE,
    DB_PATH,
    PENDING_ENRICH_LOAD_CAP,
    HOT_ENRICH_MIN_INTERVAL_SEC,
    HOT_SEEN_WINDOW_SEC,
    PROFILE_POLL_INTERVAL_SEC,
    RECHECK_ZERO_PAIR_EVERY_CYCLES,
    RECHECK_ZERO_PAIR_SECONDS,
    WARM_ENRICH_EVERY_CYCLES,
    WARM_ENRICH_MIN_INTERVAL_SEC,
)
from heartbeat import write_heartbeat
from proxy_pool import ProxyPool
from sources import build_sources, parse_sources_arg
from logging_setup import get_logger
from store import Store, norm_address, norm_chain
from validate import validate_pair, validate_token_profile

log = get_logger("pipeline")

StopFlag = Callable[[], bool]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _age_seconds(ts: str | None, now: datetime | None = None) -> float | None:
    dt = _parse_iso(ts)
    if dt is None:
        return None
    now = now or _utc_now()
    return max(0.0, (now - dt).total_seconds())


def _new_cycle_id() -> str:
    ts = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{uuid.uuid4().hex[:8]}"


def _pairs_for_token(pairs: list[dict[str, Any]], token_address: str) -> list[dict[str, Any]]:
    want = norm_address(token_address)
    matched: list[dict[str, Any]] = []
    for pair in pairs:
        base = (pair.get("baseToken") or {}).get("address") or ""
        quote = (pair.get("quoteToken") or {}).get("address") or ""
        if norm_address(str(base)) == want or norm_address(str(quote)) == want:
            matched.append(pair)
    return matched


def _empty_summary(
    flush_pending: bool,
    cycle_id: str,
    resumed: bool,
    *,
    fetch_lists: bool,
    cycle_number: int,
) -> dict[str, Any]:
    return {
        "cycle_id": cycle_id,
        "resumed": resumed,
        "fetch_lists": fetch_lists,
        "cycle_number": cycle_number,
        "profiles_latest": 0,
        "profiles_recent": 0,
        "cto_latest": 0,
        "unique_tokens": 0,
        "tokens_inserted": 0,
        "tokens_updated": 0,
        "token_upsert_skipped": 0,
        "validation_errors": 0,
        "validation_token_errors": 0,
        "validation_pair_errors": 0,
        "alerts_emitted": 0,
        "alerts_suppressed": 0,
        "sources_extra": [],
        "sources_tokens_upserted": 0,
        "tier_hot": 0,
        "tier_warm": 0,
        "tier_cold": 0,
        "tier_new": 0,
        "enrich_candidates": 0,
        "enrich_skipped_no_pairs": 0,
        "enrich_skipped_already": 0,
        "enrich_skipped_not_due": 0,
        "enrich_from_pending": 0,
        "enrich_batches": 0,
        "enrich_batch_sizes": [],
        "enrich_batch_errors": [],
        "enrich_pending_remaining": 0,
        "enrich_async_concurrency": ASYNC_MAX_CONCURRENCY,
        "flush_pending": flush_pending,
        "pairs_fetched": 0,
        "pairs_inserted": 0,
        "pairs_updated": 0,
        "pair_upsert_skipped": 0,
        "tokens_marked_no_pairs": 0,
        "tokens_marked_has_pairs": 0,
        "tokens_total": 0,
        "pairs_total": 0,
        "tokens_has_pairs_0": 0,
        "tokens_has_pairs_1": 0,
        "tokens_unenriched": 0,
        "interrupted": False,
        "errors": [],
    }


def classify_token_tier(
    row: dict[str, Any],
    *,
    cycle_number: int,
    now: datetime | None = None,
    seen_this_cycle: bool = False,
    pinned: bool = False,
) -> str | None:
    """
    Return 'new' | 'hot' | 'warm' | 'cold' if enrich is due this cycle, else None.
    """
    now = now or _utc_now()
    hp = row.get("has_pairs")
    last_enriched = row.get("last_enriched_at")
    last_seen = row.get("last_seen_at")
    enriched_age = _age_seconds(last_enriched, now)
    seen_age = _age_seconds(last_seen, now)

    # Never enriched
    if hp is None:
        return "new"

    if hp == 0:
        # Cold rare re-check
        time_due = enriched_age is None or enriched_age >= RECHECK_ZERO_PAIR_SECONDS
        cycle_due = (
            RECHECK_ZERO_PAIR_EVERY_CYCLES > 0
            and cycle_number > 0
            and cycle_number % RECHECK_ZERO_PAIR_EVERY_CYCLES == 0
        )
        if time_due or cycle_due:
            return "cold"
        return None

    # has_pairs == 1
    # pinned: open paper position → always hot cadence (PIN_OPEN_POSITIONS_HOT)
    is_hot = pinned or seen_this_cycle or (seen_age is not None and seen_age <= HOT_SEEN_WINDOW_SEC)
    if is_hot:
        if enriched_age is None or enriched_age >= HOT_ENRICH_MIN_INTERVAL_SEC:
            return "hot"
        return None

    # Warm
    cycle_due = (
        WARM_ENRICH_EVERY_CYCLES > 0
        and cycle_number > 0
        and cycle_number % WARM_ENRICH_EVERY_CYCLES == 0
    )
    time_ok = enriched_age is None or enriched_age >= WARM_ENRICH_MIN_INTERVAL_SEC
    if cycle_due and time_ok:
        return "warm"
    return None


def should_fetch_lists(store: Store, *, force_lists: bool = False) -> bool:
    if force_lists:
        return True
    last = store.get_last_profile_poll_at()
    age = _age_seconds(last)
    if age is None:
        return True
    return age >= PROFILE_POLL_INTERVAL_SEC


def _process_batch(
    store: Store,
    chain_id: str,
    batch: list[str],
    pairs: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    summary["pairs_fetched"] += len(pairs)
    seen_pair: set[tuple[str, str]] = set()
    valid_pairs: list[dict[str, Any]] = []

    for pair in pairs:
        cleaned, reason = validate_pair(pair, default_chain=chain_id)
        if cleaned is None:
            summary["validation_errors"] += 1
            summary["validation_pair_errors"] += 1
            summary["pair_upsert_skipped"] += 1
            log.warning(
                "pair validation failed cycle_id=%s chain=%s reason=%s",
                summary["cycle_id"],
                chain_id,
                reason,
            )
            continue
        valid_pairs.append(cleaned)

        p_chain = norm_chain(cleaned["chainId"])
        p_addr = norm_address(cleaned["pairAddress"])
        pk = (p_chain, p_addr)
        if pk in seen_pair:
            continue
        seen_pair.add(pk)
        token_addr = cleaned["baseToken"]["address"]
        try:
            prev_raw = store.get_pair_raw(p_chain, p_addr)
            result = store.upsert_pair(
                p_chain,
                p_addr,
                cleaned,
                "tokens/v1",
                token_address=token_addr,
            )
            if result == "inserted":
                summary["pairs_inserted"] += 1
            else:
                summary["pairs_updated"] += 1
            for alert in detect_pair_anomalies(prev_raw, cleaned):
                if emit_alert(store, alert, cycle_id=summary["cycle_id"]):
                    summary["alerts_emitted"] += 1
                else:
                    summary["alerts_suppressed"] += 1
            # P2: history snapshot when due
            try:
                if maybe_snapshot_pair(
                    store, p_chain, p_addr, cleaned, token_address=token_addr
                ):
                    summary["history_snapshots"] = summary.get("history_snapshots", 0) + 1
            except Exception as hexc:  # noqa: BLE001
                log.warning("pair_history failed chain=%s pair=%s err=%s", p_chain, p_addr[:12], hexc)
            # P2: quality score (+ optional auto DD / paper open; default OFF)
            try:
                scored = score_token_from_store(store, p_chain, token_addr)
                summary["tokens_scored"] = summary.get("tokens_scored", 0) + 1
                sc = float((scored or {}).get("score") or 0)
                maybe_auto_dd(store, p_chain, token_addr, sc)
                maybe_paper_auto_open(store, p_chain, token_addr, p_addr, sc)
            except Exception as sexc:  # noqa: BLE001
                log.warning("scoring failed chain=%s token=%s err=%s", p_chain, str(token_addr)[:12], sexc)
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"upsert_pair ({p_chain}, {p_addr}): {exc}")
            log.error(
                "upsert_pair failed cycle_id=%s chain=%s pair=%s err=%s",
                summary["cycle_id"],
                p_chain,
                p_addr,
                exc,
            )

    for addr in batch:
        matched = _pairs_for_token(valid_pairs, addr)
        count = len(matched)
        try:
            store.set_token_pair_stats(chain_id, addr, count)
            if count == 0:
                summary["tokens_marked_no_pairs"] += 1
            else:
                summary["tokens_marked_has_pairs"] += 1
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"set_token_pair_stats ({chain_id}, {addr}): {exc}")
            log.error(
                "set_token_pair_stats failed cycle_id=%s err=%s",
                summary["cycle_id"],
                exc,
            )

    store.remove_pending_enrich(chain_id, batch)


async def _fetch_and_upsert_lists(
    client: AsyncDexScreenerClient,
    store: Store,
    summary: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    lists: list[tuple[str, list[Any]]] = []
    fetchers = [
        ("profiles_latest", client.fetch_token_profiles_latest),
        ("profiles_recent", client.fetch_token_profiles_recent_updates),
        ("cto_latest", client.fetch_community_takeovers_latest),
    ]
    for name, fn in fetchers:
        try:
            items = await fn()
            lists.append((name, items))
            summary[name] = len(items) if isinstance(items, list) else 0
            log.info(
                "list fetched cycle_id=%s endpoint=%s count=%s",
                summary["cycle_id"],
                name,
                summary[name],
            )
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"{name}: {exc}")
            log.error(
                "list fetch failed cycle_id=%s endpoint=%s err=%s",
                summary["cycle_id"],
                name,
                exc,
            )
            lists.append((name, []))

    source_map = {
        "profiles_latest": "token-profiles/latest",
        "profiles_recent": "token-profiles/recent-updates",
        "cto_latest": "community-takeovers/latest",
    }
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for list_name, items in lists:
        source = source_map[list_name]
        if not isinstance(items, list):
            continue
        for item in items:
            cleaned, reason = validate_token_profile(item)
            if cleaned is None:
                summary["token_upsert_skipped"] += 1
                summary["validation_errors"] += 1
                summary["validation_token_errors"] += 1
                log.warning(
                    "token validation failed cycle_id=%s source=%s reason=%s",
                    summary["cycle_id"],
                    source,
                    reason,
                )
                continue
            key = (
                norm_chain(cleaned["chainId"]),
                norm_address(cleaned["tokenAddress"]),
            )
            if not chain_enabled(key[0]):
                summary["tokens_skipped_chain"] = summary.get("tokens_skipped_chain", 0) + 1
                continue
            seen[key] = {"item": cleaned, "source": source}

    summary["unique_tokens"] = len(seen)
    for key, payload in seen.items():
        chain_id, token_address = key
        source = payload["source"]
        try:
            result = store.upsert_token(
                chain_id, token_address, payload["item"], source
            )
            if result == "inserted":
                summary["tokens_inserted"] += 1
                if source == "token-profiles/latest":
                    alert = {
                        "type": "new_listing",
                        "severity": "info",
                        "chain_id": chain_id,
                        "token_address": token_address,
                        "pair_address": "",
                        "message": f"new token in profiles/latest: {chain_id}/{token_address}",
                        "payload": {"source": source},
                    }
                    if emit_alert(store, alert, cycle_id=summary["cycle_id"]):
                        summary["alerts_emitted"] += 1
                    else:
                        summary["alerts_suppressed"] += 1
                    try:
                        note_new_listing_for_dd(chain_id, token_address)
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        note_new_listing_for_jev(chain_id, token_address)
                    except Exception:  # noqa: BLE001
                        pass
                if source == "community-takeovers/latest":
                    alert = {
                        "type": "cto_new",
                        "severity": "medium",
                        "chain_id": chain_id,
                        "token_address": token_address,
                        "pair_address": "",
                        "message": f"new CTO token: {chain_id}/{token_address}",
                        "payload": {"source": source},
                    }
                    if emit_alert(store, alert, cycle_id=summary["cycle_id"]):
                        summary["alerts_emitted"] += 1
                    else:
                        summary["alerts_suppressed"] += 1
            else:
                summary["tokens_updated"] += 1
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"upsert_token {key}: {exc}")
            log.error(
                "upsert_token failed cycle_id=%s key=%s err=%s",
                summary["cycle_id"],
                key,
                exc,
            )

    store.set_last_profile_poll_at(_utc_now_iso())
    return seen


def _queue_tiered(
    store: Store,
    summary: dict[str, Any],
    *,
    cycle_number: int,
    seen_keys: set[tuple[str, str]],
) -> dict[str, list[str]]:
    """Select due tokens by tier and add to pending_enrich."""
    now = _utc_now()
    by_chain: dict[str, list[str]] = defaultdict(list)
    from position_pin import is_pinned, pin_enabled, pinned_count

    if pin_enabled():
        summary["pinned_open_positions"] = pinned_count()
        log.info(
            "position_pin hot pinned_open_positions=%s cycle_id=%s",
            summary["pinned_open_positions"],
            summary.get("cycle_id"),
        )
    for row in store.list_tokens_for_tiering():
        if not chain_enabled(row["chain_id"]):
            continue
        key = (row["chain_id"], row["token_address"])
        seen_this = key in seen_keys
        tier = classify_token_tier(
            row,
            cycle_number=cycle_number,
            now=now,
            seen_this_cycle=seen_this,
            pinned=is_pinned(row["chain_id"], row["token_address"]),
        )
        if tier is None:
            hp = row.get("has_pairs")
            if hp == 0:
                summary["enrich_skipped_no_pairs"] += 1
            elif hp == 1:
                summary["enrich_skipped_not_due"] += 1
            else:
                summary["enrich_skipped_already"] += 1
            continue

        summary[f"tier_{tier}"] = summary.get(f"tier_{tier}", 0) + 1
        by_chain[row["chain_id"]].append(row["token_address"])
        store.add_pending_enrich(row["chain_id"], row["token_address"])

    return by_chain


def _build_queues(
    store: Store,
    new_by_chain: dict[str, list[str]],
    summary: dict[str, Any],
) -> dict[str, list[str]]:
    pending_map = store.list_pending_enrich(limit=PENDING_ENRICH_LOAD_CAP)
    summary["enrich_from_pending"] = store.count_pending_enrich()
    summary["enrich_pending_loaded"] = sum(len(v) for v in pending_map.values())
    queues: dict[str, list[str]] = {}
    all_chains = set(pending_map) | set(new_by_chain)
    for chain_id in sorted(all_chains):
        if not chain_enabled(chain_id):
            summary["tokens_skipped_chain"] = summary.get("tokens_skipped_chain", 0) + len(
                pending_map.get(chain_id, [])
            ) + len(new_by_chain.get(chain_id, []))
            continue
        seen: set[str] = set()
        merged: list[str] = []
        for addr in pending_map.get(chain_id, []):
            if addr in seen:
                continue
            seen.add(addr)
            merged.append(addr)
        for addr in new_by_chain.get(chain_id, []):
            if addr in seen:
                continue
            seen.add(addr)
            merged.append(addr)
        queues[chain_id] = merged
    summary["enrich_candidates"] = sum(len(v) for v in queues.values())
    return queues


def _plan_batches(
    queues: dict[str, list[str]],
    flush_pending: bool,
) -> tuple[list[tuple[str, list[str], bool]], dict[str, list[str]]]:
    """Return (batches to send, remainders to keep in pending)."""
    planned: list[tuple[str, list[str], bool]] = []
    remainders: dict[str, list[str]] = {}
    for chain_id, queue in queues.items():
        q = list(queue)
        while len(q) >= BATCH_SIZE:
            batch = q[:BATCH_SIZE]
            q = q[BATCH_SIZE:]
            planned.append((chain_id, batch, False))
        if q and flush_pending:
            planned.append((chain_id, list(q), True))
            q = []
        if q:
            remainders[chain_id] = q
    return planned, remainders


async def _run_enrich_async(
    client: AsyncDexScreenerClient,
    store: Store,
    queues: dict[str, list[str]],
    summary: dict[str, Any],
    flush_pending: bool,
    stop_flag: StopFlag | None,
) -> bool:
    planned, remainders = _plan_batches(queues, flush_pending)
    for chain_id, addrs in remainders.items():
        for addr in addrs:
            store.add_pending_enrich(chain_id, addr)

    if not planned:
        return True

    if stop_flag and stop_flag():
        summary["interrupted"] = True
        return False

    sem = asyncio.Semaphore(ASYNC_MAX_CONCURRENCY)
    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def fetch_one(
        idx: int, chain_id: str, batch: list[str], is_flush: bool
    ) -> tuple[int, str, list[str], list[dict[str, Any]] | None, str | None, bool]:
        nonlocal in_flight, max_in_flight
        async with sem:
            async with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            log.info(
                "async enrich start cycle_id=%s idx=%s chain=%s batch_size=%s flush=%s in_flight=%s",
                summary["cycle_id"],
                idx,
                chain_id,
                len(batch),
                is_flush,
                in_flight,
            )
            try:
                pairs = await client.fetch_tokens_batch(chain_id, batch)
                return idx, chain_id, batch, pairs, None, is_flush
            except Exception as exc:  # noqa: BLE001
                return idx, chain_id, batch, None, str(exc), is_flush
            finally:
                async with lock:
                    in_flight -= 1

    tasks = [
        fetch_one(i + 1, chain_id, batch, is_flush)
        for i, (chain_id, batch, is_flush) in enumerate(planned)
    ]
    results = await asyncio.gather(*tasks)

    summary["enrich_async_peak_in_flight"] = max_in_flight

    # Process in index order for stable checkpoints
    for idx, chain_id, batch, pairs, err, is_flush in sorted(results, key=lambda r: r[0]):
        if stop_flag and stop_flag():
            summary["interrupted"] = True
            return False
        summary["enrich_batches"] += 1
        summary["enrich_batch_sizes"].append(len(batch))
        if err is not None:
            msg = f"{'flush ' if is_flush else ''}batch {chain_id} size={len(batch)}: {err}"
            log.error(
                "batch failed after retries cycle_id=%s chain=%s batch_size=%s err=%s",
                summary["cycle_id"],
                chain_id,
                len(batch),
                err,
            )
            summary["enrich_batch_errors"].append(msg)
            summary["errors"].append(msg)
            store.update_checkpoint(
                summary["cycle_id"],
                step="enriching",
                last_batch_meta={
                    "chain_id": chain_id,
                    "batch_size": len(batch),
                    "batch_index": idx,
                    "ok": False,
                    "error": err,
                    "flush": is_flush,
                },
                stats=summary,
            )
            continue

        assert pairs is not None
        _process_batch(store, chain_id, batch, pairs, summary)
        store.update_checkpoint(
            summary["cycle_id"],
            step="enriching",
            last_batch_meta={
                "chain_id": chain_id,
                "batch_size": len(batch),
                "batch_index": idx,
                "pairs": len(pairs),
                "ok": True,
                "flush": is_flush,
            },
            stats=summary,
        )
    return True


async def _run_once_async(
    db_path: str = DB_PATH,
    flush_pending: bool = False,
    stop_flag: StopFlag | None = None,
    force_new: bool = False,
    fetch_lists: bool | None = None,
    extra_sources: list[str] | None = None,
) -> dict[str, Any]:
    with Store(db_path) as store:
        cycle_number = store.bump_cycle_number()
        if fetch_lists is True:
            do_lists = True
        elif fetch_lists is False:
            do_lists = False
        else:
            do_lists = should_fetch_lists(store)

        resume_cp = None if force_new else store.get_incomplete_checkpoint()
        resumed = False
        if resume_cp and resume_cp["step"] in ("lists_done", "enriching"):
            cycle_id = resume_cp["cycle_id"]
            flush_pending = bool(resume_cp["flush_pending"]) or flush_pending
            resumed = True
            summary = _empty_summary(
                flush_pending, cycle_id, True, fetch_lists=False, cycle_number=cycle_number
            )
            try:
                prev = json.loads(resume_cp["stats_json"] or "{}")
                for k in (
                    "profiles_latest",
                    "profiles_recent",
                    "cto_latest",
                    "unique_tokens",
                    "tokens_inserted",
                    "tokens_updated",
                    "validation_errors",
                    "validation_token_errors",
                ):
                    if k in prev:
                        summary[k] = prev[k]
            except Exception:  # noqa: BLE001
                pass
            store.update_checkpoint(cycle_id, status="running", step="enriching", stats=summary)
            log.info(
                "resuming cycle_id=%s from step=%s pending=%s flush_pending=%s",
                cycle_id,
                resume_cp["step"],
                store.count_pending_enrich(),
                flush_pending,
            )
            do_lists = False
        else:
            if resume_cp and resume_cp["step"] == "started":
                store.update_checkpoint(
                    resume_cp["cycle_id"],
                    status="failed",
                    step="started",
                    finished=True,
                )
                log.warning(
                    "abandon incomplete started cycle_id=%s; starting new",
                    resume_cp["cycle_id"],
                )
            cycle_id = _new_cycle_id()
            summary = _empty_summary(
                flush_pending,
                cycle_id,
                False,
                fetch_lists=do_lists,
                cycle_number=cycle_number,
            )
            store.create_checkpoint(cycle_id, flush_pending=flush_pending, stats=summary)
            log.info(
                "start cycle_id=%s cycle_number=%s fetch_lists=%s flush_pending=%s",
                cycle_id,
                cycle_number,
                do_lists,
                flush_pending,
            )

        try:
            proxy_pool = ProxyPool.from_config()
            async with AsyncDexScreenerClient(cycle_id=cycle_id, proxy_pool=proxy_pool) as client:
                seen_keys: set[tuple[str, str]] = set()
                if resumed:
                    log.info("skip list fetch (resume) cycle_id=%s", cycle_id)
                    new_by_chain: dict[str, list[str]] = {}
                elif do_lists:
                    if stop_flag and stop_flag():
                        summary["interrupted"] = True
                        store.update_checkpoint(
                            cycle_id,
                            status="interrupted",
                            step="started",
                            stats=summary,
                            finished=True,
                        )
                        _finalize_ops(store, summary, status="interrupted")
                        return summary
                    seen = await _fetch_and_upsert_lists(client, store, summary)
                    seen_keys = set(seen.keys())
                    store.update_checkpoint(cycle_id, step="lists_done", stats=summary)
                    log.info(
                        "lists_done cycle_id=%s unique_tokens=%s inserted=%s updated=%s "
                        "validation_errors=%s",
                        cycle_id,
                        summary["unique_tokens"],
                        summary["tokens_inserted"],
                        summary["tokens_updated"],
                        summary["validation_errors"],
                    )
                    new_by_chain = _queue_tiered(
                        store, summary, cycle_number=cycle_number, seen_keys=seen_keys
                    )
                else:
                    log.info(
                        "skip list fetch (scheduler) cycle_id=%s last_poll=%s",
                        cycle_id,
                        store.get_last_profile_poll_at(),
                    )
                    new_by_chain = _queue_tiered(
                        store, summary, cycle_number=cycle_number, seen_keys=set()
                    )

                store.update_checkpoint(cycle_id, step="enriching", stats=summary)
                queues = _build_queues(store, new_by_chain, summary)
                log.info(
                    "enrich begin cycle_id=%s candidates=%s tiers hot=%s warm=%s cold=%s new=%s "
                    "skipped_no_pairs=%s skipped_not_due=%s concurrency=%s",
                    cycle_id,
                    summary["enrich_candidates"],
                    summary["tier_hot"],
                    summary["tier_warm"],
                    summary["tier_cold"],
                    summary["tier_new"],
                    summary["enrich_skipped_no_pairs"],
                    summary["enrich_skipped_not_due"],
                    ASYNC_MAX_CONCURRENCY,
                )

                ok = await _run_enrich_async(
                    client, store, queues, summary, flush_pending, stop_flag
                )

                # Phase 5: optional supplemental sources (after primary enrich)
                src_names = extra_sources if extra_sources is not None else []
                if src_names:
                    await _run_extra_sources(store, summary, src_names, client=client)

                summary["enrich_pending_remaining"] = store.count_pending_enrich()
                summary["tokens_total"] = store.count_tokens()
                summary["pairs_total"] = store.count_pairs()
                summary["tokens_has_pairs_0"] = store.count_tokens_has_pairs(0)
                summary["tokens_has_pairs_1"] = store.count_tokens_has_pairs(1)
                summary["tokens_unenriched"] = store.count_tokens_unenriched()

                if not ok or summary["interrupted"]:
                    store.update_checkpoint(
                        cycle_id,
                        status="interrupted",
                        step="enriching",
                        stats=summary,
                        finished=True,
                    )
                    log.warning(
                        "cycle interrupted cycle_id=%s pending=%s",
                        cycle_id,
                        summary["enrich_pending_remaining"],
                    )
                    _finalize_ops(store, summary, status="interrupted")
                    return summary

                store.update_checkpoint(
                    cycle_id,
                    status="completed",
                    step="done",
                    stats=summary,
                    finished=True,
                )
                hb_status = "degraded" if summary["errors"] else "ok"
                log.info(
                    "cycle done cycle_id=%s tokens=%s pairs=%s pending=%s "
                    "batches=%s validation_errors=%s alerts=%s peak_in_flight=%s errors=%s",
                    cycle_id,
                    summary["tokens_total"],
                    summary["pairs_total"],
                    summary["enrich_pending_remaining"],
                    summary["enrich_batches"],
                    summary["validation_errors"],
                    summary.get("alerts_emitted", 0),
                    summary.get("enrich_async_peak_in_flight", 0),
                    len(summary["errors"]),
                )
                _finalize_ops(store, summary, status=hb_status)
                return summary

        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"cycle fatal: {exc}")
            log.exception("cycle fatal cycle_id=%s err=%s", cycle_id, exc)
            store.update_checkpoint(
                cycle_id,
                status="failed",
                stats=summary,
                finished=True,
            )
            _finalize_ops(store, summary, status="error")
            return summary


async def _run_extra_sources(
    store: Store,
    summary: dict[str, Any],
    source_names: list[str],
    client: AsyncDexScreenerClient | None = None,
) -> None:
    """Run supplemental sources (non-dexscreener by default) and upsert tokens/pairs."""
    # Skip pure dexscreener-only list to avoid double primary work unless alone with client peek
    names = [n for n in source_names if n and n.lower() != "dexscreener"]
    if not names:
        summary["sources_extra"] = []
        return
    sources = build_sources(names, client=client, store=store)
    summary["sources_extra"] = [s.name for s in sources]
    for src in sources:
        try:
            result = await src.fetch()
            log.info(
                "extra source done cycle_id=%s source=%s tokens=%s pairs=%s notes=%s",
                summary["cycle_id"],
                result.source_name,
                len(result.tokens),
                len(result.pairs),
                result.notes,
            )
            tag = f"source:{result.source_name}"
            for item in result.tokens:
                cleaned, reason = validate_token_profile(item)
                if cleaned is None:
                    summary["validation_errors"] += 1
                    summary["validation_token_errors"] += 1
                    log.warning(
                        "extra source token invalid source=%s reason=%s",
                        result.source_name,
                        reason,
                    )
                    continue
                chain_id = norm_chain(cleaned["chainId"])
                token_address = norm_address(cleaned["tokenAddress"])
                if not chain_enabled(chain_id):
                    summary["tokens_skipped_chain"] = summary.get("tokens_skipped_chain", 0) + 1
                    continue
                store.upsert_token(
                    chain_id,
                    token_address,
                    cleaned,
                    tag,
                    clue=tag.startswith("source:"),
                )
                summary["sources_tokens_upserted"] += 1
            for pair in result.pairs:
                cleaned_p, reason = validate_pair(pair)
                if cleaned_p is None:
                    summary["validation_errors"] += 1
                    summary["validation_pair_errors"] += 1
                    continue
                store.upsert_pair(
                    cleaned_p["chainId"],
                    cleaned_p["pairAddress"],
                    cleaned_p,
                    tag,
                    token_address=cleaned_p["baseToken"]["address"],
                )
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"source {src.name}: {exc}")
            log.error("extra source failed source=%s err=%s", src.name, exc)



def _finalize_ops(
    store: Store,
    summary: dict[str, Any],
    *,
    status: str,
) -> None:
    """Update counters, last_success_at, and heartbeat file."""
    last_error = None
    if summary.get("errors"):
        last_error = str(summary["errors"][-1])[:500]
    if status == "ok":
        store.bump_counter("cycles_ok")
        store.set_last_success_at(_utc_now_iso())
    elif status in ("error", "failed"):
        store.bump_counter("cycles_fail")
    elif status == "degraded":
        store.bump_counter("cycles_ok")  # completed with non-fatal issues
        store.set_last_success_at(_utc_now_iso())
    elif status == "interrupted":
        store.bump_counter("cycles_fail")

    # Accumulate validation errors into durable counter (delta this cycle)
    ve = int(summary.get("validation_errors") or 0)
    if ve:
        # bump by delta once per cycle via meta snapshot would double-count;
        # store absolute increment for this cycle only
        store.bump_counter("validation_errors", ve)

    counters = store.get_counters()
    summary["counters"] = counters
    write_heartbeat(
        cycle_id=summary.get("cycle_id") or "-",
        status=status,
        stats=summary,
        last_error=last_error,
        counters=counters,
    )



def run_once(
    db_path: str = DB_PATH,
    flush_pending: bool = False,
    stop_flag: StopFlag | None = None,
    force_new: bool = False,
    fetch_lists: bool | None = None,
    extra_sources: list[str] | None = None,
) -> dict[str, Any]:
    """Sync CLI entry — runs the async pipeline via asyncio.run."""
    return asyncio.run(
        _run_once_async(
            db_path=db_path,
            flush_pending=flush_pending,
            stop_flag=stop_flag,
            force_new=force_new,
            fetch_lists=fetch_lists,
            extra_sources=extra_sources,
        )
    )


class StopController:
    """Thread-safe stop flag for SIGINT/SIGTERM and --loop."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def request_stop(self) -> None:
        self._event.set()

    def should_stop(self) -> bool:
        return self._event.is_set()
