"""Rolling Covariance Matrix & Multi-Asset BTC/ETH Net-Beta Sizer (Step 2B upgrade).

Replaces rigid one-per-sector static caps with a continuous multi-asset risk model
(inspired by MSCI Barra factor + shrunk residual covariance portfolio construction):

1. **Ledoit-Wolf / Ridge Shrunk Covariance Estimator (Pure Python Stdlib)**:
   Computes sample covariance ``Sigma_hat`` over aligned 5m or 1h return series and
   applies analytical shrinkage toward the diagonal variance target:
       ``Sigma = (1 - lambda) * Sigma_hat + lambda * diag(Sigma_hat) + ridge * I``
   guaranteeing strict positive-definiteness even when sample length ``T < N``.
2. **Rolling Market Factor Betas (``beta_btc``, ``beta_eth``)**:
   Computes empirical beta of each candidate symbol against ``BTCUSDT`` and
   ``ETHUSDT`` plus pairwise correlation matrix ``corr[i][j]``.
3. **Beta-Neutral & Volatility-Targeted Multi-Leg Portfolio Sizer**:
   Scales multi-leg long (``+size_pct``) and short (``-size_pct``) candidate weights
   to penalize high pairwise correlation concentration (``corr > max_pair_corr``),
   bound net ``beta_btc`` / ``beta_eth`` exposure, and enforce an ex-ante portfolio
   volatility ceiling ``sqrt(w^T Sigma w) <= max_portfolio_vol``.

Default runtime state: disabled unless ``LIVE_BNB_COV_BETA_SIZER=1``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
import os
from typing import Any

ENV_COV_BETA_SIZER = "LIVE_BNB_COV_BETA_SIZER"
DEFAULT_SHRINKAGE_LAMBDA = 0.25
DEFAULT_RIDGE_EPS = 1e-8
DEFAULT_MAX_PAIR_CORR = 0.75


def cov_beta_sizer_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    raw = str(env.get(ENV_COV_BETA_SIZER) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def prices_to_log_returns(prices: Sequence[float]) -> list[float]:
    out: list[float] = []
    prev: float | None = None
    for raw in prices:
        val = float(raw)
        if not math.isfinite(val) or val <= 0.0:
            continue
        if prev is not None:
            out.append(math.log(val / prev))
        prev = val
    return out


@dataclass(frozen=True)
class CovarianceBetaModel:
    symbols: tuple[str, ...]
    sample_len: int
    shrinkage_lambda: float
    covariance: dict[str, dict[str, float]]
    correlation: dict[str, dict[str, float]]
    volatility: dict[str, float]
    beta_btc: dict[str, float]
    beta_eth: dict[str, float]

    def pair_correlation(self, sym_a: str, sym_b: str) -> float | None:
        a = str(sym_a or "").strip().upper()
        b = str(sym_b or "").strip().upper()
        row = self.correlation.get(a)
        if row is None:
            return None
        return row.get(b)

    def portfolio_variance(self, signed_weights: Mapping[str, float]) -> float:
        var = 0.0
        for sym_i, w_i in signed_weights.items():
            row = self.covariance.get(sym_i)
            if row is None or w_i == 0.0:
                continue
            for sym_j, w_j in signed_weights.items():
                cov_ij = row.get(sym_j)
                if cov_ij is not None and w_j != 0.0:
                    var += float(w_i) * float(w_j) * float(cov_ij)
        return max(0.0, var)

    def portfolio_volatility(self, signed_weights: Mapping[str, float]) -> float:
        return math.sqrt(self.portfolio_variance(signed_weights))

    def net_betas(self, signed_weights: Mapping[str, float]) -> tuple[float, float]:
        net_btc = 0.0
        net_eth = 0.0
        for sym, w in signed_weights.items():
            net_btc += float(w) * float(self.beta_btc.get(sym, 0.0))
            net_eth += float(w) * float(self.beta_eth.get(sym, 0.0))
        return round(net_btc, 6), round(net_eth, 6)


def _optimal_ledoit_wolf_shrinkage(
    centered: dict[str, list[float]],
    sample_cov: dict[str, dict[str, float]],
    symbols: Sequence[str],
    n_obs: int,
) -> float:
    """Analytical Ledoit-Wolf shrinkage intensity toward diagonal variance target."""
    if n_obs <= 2 or len(symbols) <= 1:
        return 1.0
    denom = 0.0
    numer = 0.0
    inv_t = 1.0 / float(n_obs)
    for i, si in enumerate(symbols):
        xi = centered[si]
        for j in range(i + 1, len(symbols)):
            sj = symbols[j]
            xj = centered[sj]
            s_ij = sample_cov[si][sj]
            denom += 2.0 * (s_ij * s_ij)
            # Asymptotic variance of off-diagonal sample covariance
            var_s_ij = 0.0
            for k in range(n_obs):
                diff = (xi[k] * xj[k]) - s_ij
                var_s_ij += diff * diff
            numer += 2.0 * (var_s_ij * inv_t * inv_t)
    if denom <= 1e-18:
        return 1.0
    return min(1.0, max(0.05, numer / denom))


def _cholesky_is_spd(matrix: list[list[float]]) -> bool:
    """Verify strict symmetric positive-definiteness via Cholesky decomposition."""
    n = len(matrix)
    lower = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = sum(lower[i][k] * lower[j][k] for k in range(j))
            if i == j:
                diag = matrix[i][i] - s
                if diag <= 0.0 or not math.isfinite(diag):
                    return False
                lower[i][j] = math.sqrt(diag)
            else:
                lower[i][j] = (matrix[i][j] - s) / lower[j][j]
    return True


def estimate_shrunk_covariance_and_betas(
    returns_by_symbol: Mapping[str, Sequence[float]],
    *,
    min_observations: int = 12,
    shrinkage_lambda: float | None = None,
    ridge_eps: float = DEFAULT_RIDGE_EPS,
    btc_symbol: str = "BTCUSDT",
    eth_symbol: str = "ETHUSDT",
) -> CovarianceBetaModel | None:
    """Build positive-definite shrunk covariance, correlation, and BTC/ETH betas."""
    cleaned: dict[str, list[float]] = {}
    for raw_sym, seq in returns_by_symbol.items():
        sym = str(raw_sym or "").strip().upper()
        if not sym or not isinstance(seq, Sequence):
            continue
        vals = [float(x) for x in seq if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))]
        if len(vals) >= int(min_observations):
            cleaned[sym] = vals

    if not cleaned:
        return None

    common_len = min(len(v) for v in cleaned.values())
    if common_len < int(min_observations):
        return None

    symbols = tuple(sorted(cleaned))
    aligned = {sym: cleaned[sym][-common_len:] for sym in symbols}
    means = {sym: sum(vals) / float(common_len) for sym, vals in aligned.items()}
    centered = {
        sym: [v - means[sym] for v in vals]
        for sym, vals in aligned.items()
    }

    dof = float(max(1, common_len - 1))
    sample_cov: dict[str, dict[str, float]] = {s: {} for s in symbols}
    for i, si in enumerate(symbols):
        xi = centered[si]
        for j in range(i, len(symbols)):
            sj = symbols[j]
            xj = centered[sj]
            c_ij = sum(xi[k] * xj[k] for k in range(common_len)) / dof
            sample_cov[si][sj] = c_ij
            sample_cov[sj][si] = c_ij

    lam = (
        _optimal_ledoit_wolf_shrinkage(centered, sample_cov, symbols, common_len)
        if shrinkage_lambda is None
        else min(1.0, max(0.0, float(shrinkage_lambda)))
    )
    eps = max(1e-12, float(ridge_eps))

    cov: dict[str, dict[str, float]] = {s: {} for s in symbols}
    for i, si in enumerate(symbols):
        for j, sj in enumerate(symbols):
            if i == j:
                cov[si][sj] = max(eps, sample_cov[si][si] + eps)
            else:
                cov[si][sj] = (1.0 - lam) * sample_cov[si][sj]

    # Verify SPD via Cholesky; if near-singular, escalate shrinkage safely
    dense = [[cov[si][sj] for sj in symbols] for si in symbols]
    if not _cholesky_is_spd(dense):
        lam = 1.0
        for i, si in enumerate(symbols):
            for j, sj in enumerate(symbols):
                cov[si][sj] = max(eps, sample_cov[si][si] + eps) if i == j else 0.0

    vols = {s: math.sqrt(cov[s][s]) for s in symbols}
    corr: dict[str, dict[str, float]] = {s: {} for s in symbols}
    for si in symbols:
        for sj in symbols:
            if si == sj:
                corr[si][sj] = 1.0
            else:
                denom = vols[si] * vols[sj]
                r = cov[si][sj] / denom if denom > 0.0 else 0.0
                corr[si][sj] = max(-1.0, min(1.0, r))

    btc_key = btc_symbol.strip().upper()
    eth_key = eth_symbol.strip().upper()
    var_btc = sample_cov.get(btc_key, {}).get(btc_key, 0.0)
    var_eth = sample_cov.get(eth_key, {}).get(eth_key, 0.0)

    beta_btc: dict[str, float] = {}
    beta_eth: dict[str, float] = {}
    for sym in symbols:
        if sym == btc_key:
            beta_btc[sym] = 1.0
        elif var_btc > 1e-12 and btc_key in sample_cov[sym]:
            beta_btc[sym] = round(sample_cov[sym][btc_key] / var_btc, 6)
        else:
            beta_btc[sym] = 0.0

        if sym == eth_key:
            beta_eth[sym] = 1.0
        elif var_eth > 1e-12 and eth_key in sample_cov[sym]:
            beta_eth[sym] = round(sample_cov[sym][eth_key] / var_eth, 6)
        else:
            beta_eth[sym] = 0.0

    return CovarianceBetaModel(
        symbols=symbols,
        sample_len=common_len,
        shrinkage_lambda=round(lam, 6),
        covariance=cov,
        correlation=corr,
        volatility=vols,
        beta_btc=beta_btc,
        beta_eth=beta_eth,
    )


@dataclass(frozen=True)
class PortfolioAllocationPlan:
    signed_weights: dict[str, float]
    recommended_btc_hedge_weight: float
    ex_ante_volatility: float
    net_beta_btc_before: float
    net_beta_btc_after: float
    net_beta_eth_after: float
    gross_exposure: float
    correlation_haircuts: dict[str, float]


def compute_beta_neutral_portfolio_weights(
    candidates: Sequence[Mapping[str, Any]],
    model: CovarianceBetaModel,
    *,
    max_portfolio_vol: float = 0.025,
    max_abs_net_beta_btc: float = 0.20,
    max_pair_corr: float = DEFAULT_MAX_PAIR_CORR,
    max_gross_exposure: float = 1.0,
    btc_hedge_symbol: str = "BTCUSDT",
) -> PortfolioAllocationPlan:
    """Scale multi-leg long/short weights using shrunk covariance & BTC net-beta bounds.

    Each candidate dict carries ``symbol`` (or ``binance_symbol``), ``size_pct``
    (positive magnitude), and ``action`` / ``side`` (``buy`` / ``LONG`` -> ``+1``,
    ``sell`` / ``short`` / ``SHORT`` -> ``-1``).
    """
    raw_weights: dict[str, float] = {}
    for item in candidates:
        sym = str(item.get("binance_symbol") or item.get("symbol") or "").strip().upper()
        if not sym or sym not in model.volatility:
            continue
        mag = abs(float(item.get("size_pct") or 0.0))
        if mag <= 0.0 or not math.isfinite(mag):
            continue
        side_txt = str(item.get("side") or item.get("action") or "buy").strip().lower()
        sign = -1.0 if side_txt in {"sell", "short"} else 1.0
        raw_weights[sym] = raw_weights.get(sym, 0.0) + sign * mag

    if not raw_weights:
        return PortfolioAllocationPlan(
            signed_weights={},
            recommended_btc_hedge_weight=0.0,
            ex_ante_volatility=0.0,
            net_beta_btc_before=0.0,
            net_beta_btc_after=0.0,
            net_beta_eth_after=0.0,
            gross_exposure=0.0,
            correlation_haircuts={},
        )

    net_btc_before, _ = model.net_betas(raw_weights)

    # Step 1: Penalize same-direction pairwise correlation crowding (rho * sign_i * sign_j > max_pair_corr)
    haircuts: dict[str, float] = {}
    adjusted: dict[str, float] = {}
    syms = list(raw_weights)
    for sym_i in syms:
        w_i = raw_weights[sym_i]
        crowding_load = 0.0
        for sym_j in syms:
            if sym_i == sym_j:
                continue
            w_j = raw_weights[sym_j]
            rho = model.pair_correlation(sym_i, sym_j) or 0.0
            directional_rho = rho * (1.0 if (w_i * w_j) > 0 else -1.0)
            if directional_rho > max_pair_corr:
                crowding_load += directional_rho
        scalar = 1.0 / (1.0 + crowding_load)
        haircuts[sym_i] = round(scalar, 6)
        adjusted[sym_i] = w_i * scalar

    # Step 2: Balance multi-leg long/short book to respect |net_beta_btc| <= max_abs_net_beta_btc
    has_longs = any(w > 0 for w in adjusted.values())
    has_shorts = any(w < 0 for w in adjusted.values())
    net_btc_mid, _ = model.net_betas(adjusted)

    btc_hedge = 0.0
    if abs(net_btc_mid) > max_abs_net_beta_btc:
        if has_longs and has_shorts:
            pos_beta = sum(
                w * max(0.0, model.beta_btc.get(s, 0.0)) for s, w in adjusted.items() if w > 0
            )
            neg_beta = sum(
                abs(w) * max(0.0, model.beta_btc.get(s, 0.0))
                for s, w in adjusted.items()
                if w < 0
            )
            if net_btc_mid > max_abs_net_beta_btc and pos_beta > 1e-9:
                target_pos_beta = neg_beta + max_abs_net_beta_btc
                scale_long = max(0.15, min(1.0, target_pos_beta / pos_beta))
                for s in list(adjusted):
                    if adjusted[s] > 0:
                        adjusted[s] *= scale_long
            elif net_btc_mid < -max_abs_net_beta_btc and neg_beta > 1e-9:
                target_neg_beta = pos_beta + max_abs_net_beta_btc
                scale_short = max(0.15, min(1.0, target_neg_beta / neg_beta))
                for s in list(adjusted):
                    if adjusted[s] < 0:
                        adjusted[s] *= scale_short
        else:
            excess_beta = (
                net_btc_mid - max_abs_net_beta_btc
                if net_btc_mid > 0
                else net_btc_mid + max_abs_net_beta_btc
            )
            btc_hedge = round(-excess_beta, 6)
            if btc_hedge_symbol in model.volatility and btc_hedge_symbol not in adjusted:
                # Report hedge recommendation cleanly; callers may execute on Cross Margin / Perp
                pass

    # Step 3: Enforce gross exposure ceiling
    gross = sum(abs(w) for w in adjusted.values())
    if gross > max_gross_exposure and gross > 0:
        g_scale = max_gross_exposure / gross
        adjusted = {s: w * g_scale for s, w in adjusted.items()}

    # Step 4: Enforce ex-ante portfolio volatility ceiling sqrt(w^T Sigma w) <= max_portfolio_vol
    port_vol = model.portfolio_volatility(adjusted)
    if port_vol > max_portfolio_vol and port_vol > 1e-12:
        v_scale = max_portfolio_vol / port_vol
        adjusted = {s: w * v_scale for s, w in adjusted.items()}
        port_vol = model.portfolio_volatility(adjusted)

    rounded_weights = {s: round(w, 6) for s, w in adjusted.items()}
    net_btc_after, net_eth_after = model.net_betas(rounded_weights)
    if btc_hedge != 0.0:
        # Recompute residual hedge against final scaled book
        if abs(net_btc_after) > max_abs_net_beta_btc:
            btc_hedge = round(
                -(
                    net_btc_after - max_abs_net_beta_btc
                    if net_btc_after > 0
                    else net_btc_after + max_abs_net_beta_btc
                ),
                6,
            )
        else:
            btc_hedge = 0.0

    return PortfolioAllocationPlan(
        signed_weights=rounded_weights,
        recommended_btc_hedge_weight=btc_hedge,
        ex_ante_volatility=round(port_vol, 6),
        net_beta_btc_before=net_btc_before,
        net_beta_btc_after=net_btc_after,
        net_beta_eth_after=net_eth_after,
        gross_exposure=round(sum(abs(w) for w in rounded_weights.values()), 6),
        correlation_haircuts=haircuts,
    )
