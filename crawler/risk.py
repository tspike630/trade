"""
P3 on-chain risk scoring — pluggable scanners.

Default: HeuristicRiskScanner (metadata only).
Optional: HttpRiskScanner when RISK_API_URL is set (GoPlus / RugCheck style).
Real GoPlus/RugCheck need an API key — set RISK_API_KEY; never logged.

Deduction applied in scoring.score_token_from_store; tags prefixed risk_*.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from config import (
    RISK_API_KEY,
    RISK_API_URL,
    RISK_ENABLED,
    RISK_MAX_DEDUCTION,
    RISK_PROVIDER,
    RISK_TIMEOUT_SEC,
    USER_AGENT,
)
from logging_setup import get_logger

log = get_logger("risk")


@dataclass
class RiskResult:
    deduction: float = 0.0
    tags: list[str] = field(default_factory=list)
    flags: dict[str, Any] = field(default_factory=dict)
    source: str = "none"
    notes: str = ""

    def clamp(self) -> RiskResult:
        self.deduction = max(0.0, min(float(RISK_MAX_DEDUCTION), float(self.deduction)))
        return self


class RiskScanner(ABC):
    name: str = "base"

    @abstractmethod
    def scan(
        self,
        chain_id: str,
        token_address: str,
        *,
        pair: dict[str, Any] | None = None,
        token_raw: dict[str, Any] | None = None,
    ) -> RiskResult:
        ...


class HeuristicRiskScanner(RiskScanner):
    """
    Best-effort flags from DexScreener-like pair/token metadata (no external key).
    Not a substitute for GoPlus/RugCheck.
    """

    name = "heuristic"

    def scan(
        self,
        chain_id: str,
        token_address: str,
        *,
        pair: dict[str, Any] | None = None,
        token_raw: dict[str, Any] | None = None,
    ) -> RiskResult:
        tags: list[str] = []
        flags: dict[str, Any] = {}
        deduction = 0.0
        pair = pair or {}
        token_raw = token_raw or {}

        liq = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
        liq_usd = float(liq.get("usd") or 0) if liq else 0.0
        vol = pair.get("volume") if isinstance(pair.get("volume"), dict) else {}
        vol_h24 = float(vol.get("h24") or 0) if vol else 0.0
        txns = pair.get("txns") if isinstance(pair.get("txns"), dict) else {}
        h1 = txns.get("h1") if isinstance(txns.get("h1"), dict) else {}
        buys = int(h1.get("buys") or 0)
        sells = int(h1.get("sells") or 0)

        # Extremely thin liquidity
        if 0 < liq_usd < 500:
            tags.append("risk_thin_liq")
            flags["thin_liq"] = liq_usd
            deduction += 15

        # Buy/sell asymmetry (honeypot-ish heuristic)
        if buys + sells >= 20:
            sell_ratio = sells / max(1, buys + sells)
            if sell_ratio < 0.05 and buys >= 20:
                tags.append("risk_honeypot_like")
                flags["sell_ratio_h1"] = round(sell_ratio, 4)
                flags["buys_h1"] = buys
                flags["sells_h1"] = sells
                deduction += 25
            elif sell_ratio < 0.15 and buys >= 30:
                tags.append("risk_low_sells")
                flags["sell_ratio_h1"] = round(sell_ratio, 4)
                deduction += 12

        # Volume >> liquidity (possible wash / tax trap noise)
        if liq_usd > 0 and vol_h24 > liq_usd * 50:
            tags.append("risk_high_turnover")
            flags["vol_liq_ratio"] = round(vol_h24 / liq_usd, 2)
            deduction += 8

        # Metadata hints (labels / info from profile)
        blob = json.dumps({**token_raw, **{k: pair.get(k) for k in ("labels", "info", "boosts")}}, default=str).lower()
        if "honeypot" in blob:
            tags.append("risk_honeypot_flag")
            deduction += 30
        if "tax" in blob and ("100" in blob or "high tax" in blob):
            tags.append("risk_high_tax")
            deduction += 20
        # Renounce-like: if description claims renounced — soft positive (reduce risk)
        if "renounc" in blob or "ownership renounced" in blob:
            tags.append("risk_renounce_claimed")
            flags["renounce_claimed"] = True
            deduction = max(0.0, deduction - 5)

        # Missing socials on brand-new thin pairs
        links = token_raw.get("links") if isinstance(token_raw.get("links"), list) else []
        if not links and liq_usd < 5000:
            tags.append("risk_no_socials")
            deduction += 5

        return RiskResult(
            deduction=deduction,
            tags=sorted(set(tags)),
            flags=flags,
            source=self.name,
            notes="heuristic_only",
        ).clamp()


class HttpRiskScanner(RiskScanner):
    """
    POST/GET configurable RISK_API_URL.
    Expected JSON (flexible):
      { "honeypot": bool, "high_tax": bool, "buy_tax": float, "sell_tax": float,
        "renounced": bool, "risk_score": 0-100, "flags": {...} }
    or GoPlus-like nested result — best-effort parse.
    """

    name = "http"

    def __init__(self, url: str | None = None, api_key: str | None = None):
        self.url = url or RISK_API_URL
        self.api_key = api_key if api_key is not None else RISK_API_KEY

    def scan(
        self,
        chain_id: str,
        token_address: str,
        *,
        pair: dict[str, Any] | None = None,
        token_raw: dict[str, Any] | None = None,
    ) -> RiskResult:
        if not self.url:
            return RiskResult(source="http", notes="no_url")
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key
        params = {"chain": chain_id, "address": token_address, "token": token_address}
        try:
            with httpx.Client(timeout=RISK_TIMEOUT_SEC) as client:
                # Prefer GET; fall back POST JSON body
                resp = client.get(self.url, params=params, headers=headers)
                if resp.status_code >= 400:
                    resp = client.post(
                        self.url,
                        json={"chain_id": chain_id, "token_address": token_address},
                        headers=headers,
                    )
            if resp.status_code >= 400:
                # Do not include Authorization in logs
                log.warning("risk HTTP %s body=%s", resp.status_code, resp.text[:120])
                return RiskResult(source="http", notes=f"http_{resp.status_code}")
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("risk HTTP error: %s", type(exc).__name__)
            return RiskResult(source="http", notes=f"error:{type(exc).__name__}")

        return _parse_external_risk(data).clamp()


def _parse_external_risk(data: Any) -> RiskResult:
    tags: list[str] = []
    flags: dict[str, Any] = {}
    deduction = 0.0
    if not isinstance(data, dict):
        return RiskResult(source="http", notes="bad_json")

    # Unwrap common envelopes
    body = data.get("result") or data.get("data") or data
    if isinstance(body, dict) and len(body) == 1:
        inner = next(iter(body.values()))
        if isinstance(inner, dict):
            body = inner
    if not isinstance(body, dict):
        body = data

    def _truth(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "y")
        return False

    if _truth(body.get("honeypot") or body.get("is_honeypot") or body.get("isHoneypot")):
        tags.append("risk_honeypot")
        deduction += 35
        flags["honeypot"] = True

    buy_tax = body.get("buy_tax", body.get("buyTax"))
    sell_tax = body.get("sell_tax", body.get("sellTax"))
    try:
        bt = float(buy_tax) if buy_tax is not None else None
        st = float(sell_tax) if sell_tax is not None else None
    except (TypeError, ValueError):
        bt = st = None
    # taxes may be 0-1 or 0-100
    def _pct(x: float | None) -> float | None:
        if x is None:
            return None
        return x * 100 if x <= 1 else x

    bt_p, st_p = _pct(bt), _pct(st)
    if bt_p is not None and bt_p >= 10:
        tags.append("risk_high_tax")
        flags["buy_tax_pct"] = bt_p
        deduction += min(20, bt_p / 2)
    if st_p is not None and st_p >= 10:
        tags.append("risk_high_tax")
        flags["sell_tax_pct"] = st_p
        deduction += min(20, st_p / 2)

    if _truth(body.get("high_tax") or body.get("highTax")):
        tags.append("risk_high_tax")
        deduction += 15

    if _truth(body.get("renounced") or body.get("is_renounced") or body.get("owner_renounced")):
        tags.append("risk_renounced")
        flags["renounced"] = True
        deduction = max(0.0, deduction - 8)

    rs = body.get("risk_score", body.get("riskScore"))
    try:
        if rs is not None:
            # external 0-100 risk → map into deduction boost
            rsi = float(rs)
            flags["external_risk_score"] = rsi
            deduction += min(25, rsi / 4)
            if rsi >= 70:
                tags.append("risk_external_high")
    except (TypeError, ValueError):
        pass

    if isinstance(body.get("flags"), dict):
        flags.update(body["flags"])

    return RiskResult(
        deduction=deduction,
        tags=sorted(set(tags)),
        flags=flags,
        source="http",
        notes="external",
    )



# EVM chainId for GoPlus token_security/{id}
_GOPLUS_CHAIN_IDS = {
    "ethereum": "1",
    "eth": "1",
    "bsc": "56",
    "bnb": "56",
    "base": "8453",
    "polygon": "137",
    "matic": "137",
    "arbitrum": "42161",
    "optimism": "10",
    "avalanche": "43114",
    "fantom": "250",
}


class GoPlusRiskScanner(RiskScanner):
    """
    GoPlus-style HTTP:
      GET {base}/api/v1/token_security/{chainId}?contract_addresses=ADDR
      GET {base}/api/v1/solana/token_security?contract_addresses=ADDR
    base = RISK_API_URL or https://api.gopluslabs.io
    RISK_API_KEY sent as Authorization if set (never logged).
    """

    name = "goplus"

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base = (base_url or RISK_API_URL or "https://api.gopluslabs.io").rstrip("/")
        self.api_key = api_key if api_key is not None else RISK_API_KEY

    def _endpoint(self, chain_id: str, token_address: str) -> tuple[str, dict[str, str]] | None:
        c = chain_id.strip().lower()
        addr = token_address.strip()
        if c in ("solana", "sol"):
            return (
                f"{self.base}/api/v1/solana/token_security",
                {"contract_addresses": addr},
            )
        cid = _GOPLUS_CHAIN_IDS.get(c)
        if not cid:
            return None
        return (
            f"{self.base}/api/v1/token_security/{cid}",
            {"contract_addresses": addr},
        )

    def scan(
        self,
        chain_id: str,
        token_address: str,
        *,
        pair: dict[str, Any] | None = None,
        token_raw: dict[str, Any] | None = None,
    ) -> RiskResult:
        ep = self._endpoint(chain_id, token_address)
        if ep is None:
            return RiskResult(source="goplus", notes="unsupported_chain")
        url, params = ep
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            # some gateways use query key — never log
            params = dict(params)
            params.setdefault("api_key", self.api_key)
        try:
            with httpx.Client(timeout=RISK_TIMEOUT_SEC) as client:
                resp = client.get(url, params=params, headers=headers)
            if resp.status_code >= 400:
                log.warning("goplus HTTP %s", resp.status_code)
                return RiskResult(source="goplus", notes=f"http_{resp.status_code}")
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("goplus error: %s", type(exc).__name__)
            return RiskResult(source="goplus", notes=f"error:{type(exc).__name__}")
        parsed = _parse_external_risk(data)
        parsed.source = "goplus"
        # GoPlus often uses string "0"/"1" for is_honeypot etc. — already in _truth
        # Also map common GoPlus fields if nested under result[address]
        if not parsed.tags and isinstance(data, dict):
            result = data.get("result")
            if isinstance(result, dict):
                for _addr, body in result.items():
                    if isinstance(body, dict):
                        parsed = _parse_external_risk({"result": body})
                        parsed.source = "goplus"
                        break
        parsed.notes = parsed.notes or "goplus"
        return parsed.clamp()


def _http_failed(r: RiskResult) -> bool:
    n = (r.notes or "").lower()
    return (
        n.startswith("error")
        or n.startswith("http_")
        or n in ("no_url", "unsupported_chain", "bad_json")
        or n.startswith("fallback")
    )


class CompositeRiskScanner(RiskScanner):
    """
    Heuristic always. If RISK_API_URL set or RISK_PROVIDER=goplus, try HTTP/GoPlus.
    On HTTP failure → graceful fallback to heuristics only (demo-safe without key).
    """

    name = "composite"

    def __init__(self) -> None:
        self.heuristic = HeuristicRiskScanner()
        self.remote: RiskScanner | None = None
        # Remote only when RISK_API_URL is set (demo without key stays heuristic-only).
        provider = (RISK_PROVIDER or "goplus").lower()
        if RISK_API_URL:
            if provider == "goplus" or "goplus" in RISK_API_URL.lower():
                self.remote = GoPlusRiskScanner()
            else:
                self.remote = HttpRiskScanner()

    def scan(
        self,
        chain_id: str,
        token_address: str,
        *,
        pair: dict[str, Any] | None = None,
        token_raw: dict[str, Any] | None = None,
    ) -> RiskResult:
        h = self.heuristic.scan(
            chain_id, token_address, pair=pair, token_raw=token_raw
        )
        if self.remote is None:
            return h
        e = self.remote.scan(
            chain_id, token_address, pair=pair, token_raw=token_raw
        )
        if _http_failed(e):
            h.notes = f"fallback_heuristic({e.source}:{e.notes})"
            h.flags = {**h.flags, "remote_fallback": e.notes}
            return h
        tags = sorted(set(h.tags + e.tags))
        flags = {**h.flags, **e.flags}
        return RiskResult(
            deduction=min(RISK_MAX_DEDUCTION, h.deduction + e.deduction),
            tags=tags,
            flags=flags,
            source="composite",
            notes=f"heuristic+{e.source}:{e.notes}",
        ).clamp()


_scanner: RiskScanner | None = None


def get_risk_scanner() -> RiskScanner:
    global _scanner
    if _scanner is None:
        _scanner = CompositeRiskScanner()
    return _scanner


def assess_risk(
    chain_id: str,
    token_address: str,
    *,
    pair: dict[str, Any] | None = None,
    token_raw: dict[str, Any] | None = None,
) -> RiskResult:
    if not RISK_ENABLED:
        return RiskResult(source="disabled", notes="RISK_ENABLED=0")
    return get_risk_scanner().scan(
        chain_id, token_address, pair=pair, token_raw=token_raw
    )


def apply_risk_to_score(score: float, tags: list[str], risk: RiskResult) -> tuple[float, list[str]]:
    new_score = max(0.0, round(float(score) - risk.deduction, 1))
    merged = list(tags)
    for t in risk.tags:
        if t not in merged:
            merged.append(t)
    if risk.deduction >= 20 and "risk_elevated" not in merged:
        merged.append("risk_elevated")
    return new_score, merged
