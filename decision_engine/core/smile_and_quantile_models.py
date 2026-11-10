"""Move #3 (Rolling Quantile Adaptive Thresholds) & Move #4 (Volatility Smile Parabola Residual).

Implements two institutional/competition microstructure & cross-asset engines in pure stdlib Python:
1. Rolling Quantile Adaptive Thresholds (Jane Street competition online adaptation):
   - Replaces static constants (e.g., ``nvda_1h >= +1.20%`` or ``vol_z >= 3.8``) with empirical
     rolling quantiles (e.g., P95 for breakout ignition, P99 for climax blow-off) over a sliding
     historical window when >= ``min_samples`` observations are supplied, falling back cleanly
     to static baseline constants during cold start.
2. Option Volatility Smile Parabola Residual (IMC Global #2 Round 3 ``vt - v_hat(mt)``):
   - Fits ``IV(m) = a * m^2 + b * m + c`` via exact 3x3 Gaussian elimination over log-moneyness
     ``m = ln(K / S) / sqrt(T)`` across option strikes.
   - Isolates OTM put-wing crash-insurance spikes (``put_wing_iv_residual_pct >= +4.0`` -> early
     warning that dealers/whales are bidding up OTM puts ahead of a spot dump -> State 4 defensive)
     and OTM call-wing squeeze residuals.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Sequence


@dataclass(frozen=True)
class RollingAdaptiveThresholds:
    nvda_breakout_1h_pct: float
    smh_breakout_1h_pct: float
    climax_vol_z: float
    used_empirical_nvda: bool
    used_empirical_smh: bool
    used_empirical_vol_z: bool


@dataclass(frozen=True)
class SmileResidualSnapshot:
    atm_fitted_iv_pct: float
    smile_curvature_a: float
    smile_skew_b: float
    max_put_wing_residual_pct: float
    max_call_wing_residual_pct: float
    put_crash_insurance_spike: bool
    call_squeeze_demand_spike: bool
    n_strikes: int


def _finite(val: object) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def empirical_quantile(
    values: Sequence[float],
    q: float,
    *,
    min_samples: int = 30,
    fallback: float,
    clamp_min: float | None = None,
    clamp_max: float | None = None,
) -> tuple[float, bool]:
    """Linear-interpolated empirical quantile with a cold-start fallback.

    Every finite value in ``values`` is part of the sample. There is no event
    time, so this function cannot drop the current bar, a future point, or an
    old point, and it does not apply a maximum window. ``min_samples`` is only
    a count. A live caller has to pass an already closed, bounded history.
    The length of that window is not chosen here. Non-finite values are
    omitted and are not replaced with zero.
    """
    clean = sorted(v for v in (_finite(x) for x in values) if v is not None)
    if len(clean) < min_samples:
        return fallback, False
    q_clamped = min(1.0, max(0.0, float(q)))
    pos = q_clamped * (len(clean) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    frac = pos - lo
    est = clean[lo] * (1.0 - frac) + clean[hi] * frac
    if clamp_min is not None:
        est = max(clamp_min, est)
    if clamp_max is not None:
        est = min(clamp_max, est)
    return round(est, 6), True


def resolve_rolling_thresholds(row: dict[str, Any]) -> RollingAdaptiveThresholds:
    """Thresholds from history arrays already stored on ``row``, or the static defaults.

    The arrays are not a point-in-time query. This function does not read a
    clock, exclude the current bar, or cap the window.
    """
    nvda_hist = row.get("nvda_1h_ret_history") or ()
    smh_hist = row.get("smh_1h_ret_history") or ()
    vol_z_hist = row.get("vol_z_1h_history") or ()

    nvda_thr, ok_nvda = empirical_quantile(
        nvda_hist if isinstance(nvda_hist, Sequence) and not isinstance(nvda_hist, (str, bytes)) else (),
        0.95,
        min_samples=30,
        fallback=1.20,
        clamp_min=0.55,
        clamp_max=2.80,
    )
    smh_thr, ok_smh = empirical_quantile(
        smh_hist if isinstance(smh_hist, Sequence) and not isinstance(smh_hist, (str, bytes)) else (),
        0.95,
        min_samples=30,
        fallback=1.20,
        clamp_min=0.50,
        clamp_max=2.50,
    )
    vol_thr, ok_vol = empirical_quantile(
        vol_z_hist if isinstance(vol_z_hist, Sequence) and not isinstance(vol_z_hist, (str, bytes)) else (),
        0.985,
        min_samples=30,
        fallback=3.80,
        clamp_min=2.60,
        clamp_max=6.00,
    )
    return RollingAdaptiveThresholds(
        nvda_breakout_1h_pct=nvda_thr,
        smh_breakout_1h_pct=smh_thr,
        climax_vol_z=vol_thr,
        used_empirical_nvda=ok_nvda,
        used_empirical_smh=ok_smh,
        used_empirical_vol_z=ok_vol,
    )


def _solve_3x3(mat: list[list[float]], rhs: list[float]) -> tuple[float, float, float] | None:
    """Solves a 3x3 linear system via partial-pivoting Gaussian elimination."""
    a = [row[:] + [rhs[i]] for i, row in enumerate(mat)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            return None
        a[col], a[piv] = a[piv], a[col]
        div = a[col][col]
        for j in range(col, 4):
            a[col][j] /= div
        for r in range(3):
            if r != col:
                factor = a[r][col]
                for j in range(col, 4):
                    a[r][j] -= factor * a[col][j]
    return a[0][3], a[1][3], a[2][3]


def _text_label(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _contract_label(chain: Sequence[Any], key: str, *, casefold: bool = False) -> tuple[str, str | None]:
    """Shared label, or ``conflict`` when the dicts do not all carry the same one.

    Omitting the key on every dict is ``ok`` with a null label. A label on only
    some dicts is a conflict. Non-dicts are ignored.
    """
    seen: list[str] = []
    dicts = 0
    for item in chain:
        if not isinstance(item, dict):
            continue
        dicts += 1
        label = _text_label(item.get(key))
        if label is None:
            continue
        seen.append(label.casefold() if casefold else label)
    if not seen:
        return "ok", None
    if len(seen) != dicts or len(set(seen)) != 1:
        return "conflict", None
    return "ok", seen[0]


def fit_volatility_smile_residuals(
    option_chain: Sequence[dict[str, Any]],
    spot_price: float,
    *,
    residual_spike_pct: float = 4.0,
) -> SmileResidualSnapshot | None:
    """Fit IV(m) = a*m^2 + b*m + c on m = ln(K/S)/sqrt(T) and return wing residuals.

    Each item needs ``strike`` and ``iv_pct`` or ``iv``. ``tte_years`` defaults
    to 7/365 only when that field is absent. Zero and negative time are dropped.
    When ``underlying``, ``expiry``, or ``iv_unit`` is present, every dict must
    carry the same value. ``iv_unit`` ``percent`` or ``decimal`` selects the
    scale. Without ``iv_unit``, a value below 3 is still multiplied by 100.
    That heuristic is not an explicit unit. Leave-one-out starts at 5 points;
    a 4-point residual includes the point in its own fit. No event time is read.
    """
    s = _finite(spot_price)
    if s is None or s <= 0 or not isinstance(option_chain, Sequence) or isinstance(option_chain, (str, bytes)):
        return None
    for key in ("underlying", "expiry"):
        status, _label = _contract_label(option_chain, key)
        if status == "conflict":
            return None
    status, iv_unit = _contract_label(option_chain, "iv_unit", casefold=True)
    if status == "conflict":
        return None
    if iv_unit not in (None, "percent", "decimal"):
        return None

    pts: list[tuple[float, float]] = []
    for opt in option_chain:
        if not isinstance(opt, dict):
            continue
        k = _finite(opt.get("strike"))
        iv = _finite(opt.get("iv_pct") if opt.get("iv_pct") is not None else opt.get("iv"))
        raw_tte = opt.get("tte_years")
        if raw_tte is None:
            tte = 7.0 / 365.0
        else:
            parsed_tte = _finite(raw_tte)
            if parsed_tte is None or parsed_tte <= 1e-6:
                continue
            tte = parsed_tte
        if k is None or k <= 0 or iv is None or iv <= 0:
            continue
        if iv_unit == "percent":
            iv_pct = iv
        elif iv_unit == "decimal":
            iv_pct = iv * 100.0
        else:
            iv_pct = iv * 100.0 if iv < 3.0 else iv
        m = math.log(k / s) / math.sqrt(tte)
        if abs(m) <= 2.5:
            pts.append((m, iv_pct))

    if len(pts) < 4:
        return None

    # Least-squares normal equations for c + b*m + a*m^2 (using Huber-like trimming via 2-pass fit
    # so a single extreme wing spike doesn't pull the baseline parabola up with it!)
    def _fit_quad(sample: list[tuple[float, float]]) -> tuple[float, float, float] | None:
        n = float(len(sample))
        s1 = sum(m for m, _ in sample)
        s2 = sum(m * m for m, _ in sample)
        s3 = sum(m ** 3 for m, _ in sample)
        s4 = sum(m ** 4 for m, _ in sample)
        y0 = sum(y for _, y in sample)
        y1 = sum(m * y for m, y in sample)
        y2 = sum((m * m) * y for m, y in sample)
        res = _solve_3x3(
            [
                [n, s1, s2],
                [s1, s2, s3],
                [s2, s3, s4],
            ],
            [y0, y1, y2],
        )
        return res  # (c, b, a)

    pass1 = _fit_quad(pts)
    if pass1 is None:
        return None
    c1, b1, a1 = pass1

    # Leave-One-Out (Jackknife) wing residuals prevent high-leverage edge strikes
    # from pulling the parabola up and masking their own IV spike.
    put_res: list[float] = []
    call_res: list[float] = []
    for idx, (m, iv) in enumerate(pts):
        if len(pts) >= 5:
            others = pts[:idx] + pts[idx + 1 :]
            loo = _fit_quad(others)
            c_fit, b_fit, a_fit = loo if loo is not None else (c1, b1, a1)
        else:
            c_fit, b_fit, a_fit = c1, b1, a1
        res_iv = iv - (a_fit * m * m + b_fit * m + c_fit)
        if m <= -0.05:
            put_res.append(res_iv)
        elif m >= 0.05:
            call_res.append(res_iv)

    max_put_res = max(put_res) if put_res else 0.0
    max_call_res = max(call_res) if call_res else 0.0

    return SmileResidualSnapshot(
        atm_fitted_iv_pct=round(c1, 4),
        smile_curvature_a=round(a1, 4),
        smile_skew_b=round(b1, 4),
        max_put_wing_residual_pct=round(max_put_res, 4),
        max_call_wing_residual_pct=round(max_call_res, 4),
        put_crash_insurance_spike=max_put_res >= residual_spike_pct,
        call_squeeze_demand_spike=max_call_res >= residual_spike_pct,
        n_strikes=len(pts),
    )
