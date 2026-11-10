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
   - Isolates OTM put-wing crash-insurance spikes. Only a leave-one-out residual
     (at least 5 points, each wing point scored on a fit that excludes it) can
     set the warning. A 4-point in-sample residual is kept separately and cannot
     raise that warning or the State 4 smile route.
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
    # Leave-one-out wing residual. None unless ``residual_provenance`` is
    # ``leave_one_out``. An in-sample number is never stored here.
    max_put_wing_residual_pct: float | None
    max_call_wing_residual_pct: float | None
    put_crash_insurance_spike: bool
    call_squeeze_demand_spike: bool
    n_strikes: int
    residual_provenance: str
    in_sample_max_put_wing_residual_pct: float | None
    in_sample_max_call_wing_residual_pct: float | None


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
    That heuristic is not an explicit unit. A fit exists from 4 points, but
    that residual includes each point in its own parabola. It is stored on
    ``in_sample_max_*`` with provenance ``in_sample`` and both spike flags
    stay false. Leave-one-out wing residuals, and the crash-insurance flags,
    exist only when at least 5 points produce a jackknife wing residual.
    No event time is read.
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

    # A 4-point residual is in-sample: the point helps fit the parabola that
    # is then used to score it. Leave-one-out refits without that point, and
    # only those wing residuals can raise a crash-insurance flag. A failed
    # jackknife is omitted. It is not replaced with the in-sample residual.
    in_put: list[float] = []
    in_call: list[float] = []
    loo_put: list[float] = []
    loo_call: list[float] = []
    for idx, (m, iv) in enumerate(pts):
        in_res = iv - (a1 * m * m + b1 * m + c1)
        loo_res: float | None = None
        if len(pts) >= 5:
            others = pts[:idx] + pts[idx + 1 :]
            loo = _fit_quad(others)
            if loo is not None:
                c_fit, b_fit, a_fit = loo
                loo_res = iv - (a_fit * m * m + b_fit * m + c_fit)
        if m <= -0.05:
            in_put.append(in_res)
            if loo_res is not None:
                loo_put.append(loo_res)
        elif m >= 0.05:
            in_call.append(in_res)
            if loo_res is not None:
                loo_call.append(loo_res)

    def _peak(values: list[float]) -> float | None:
        if not values:
            return None
        return round(max(values), 4)

    loo_put_peak = _peak(loo_put)
    loo_call_peak = _peak(loo_call)
    leave_one_out = loo_put_peak is not None or loo_call_peak is not None
    return SmileResidualSnapshot(
        atm_fitted_iv_pct=round(c1, 4),
        smile_curvature_a=round(a1, 4),
        smile_skew_b=round(b1, 4),
        max_put_wing_residual_pct=loo_put_peak,
        max_call_wing_residual_pct=loo_call_peak,
        put_crash_insurance_spike=(
            leave_one_out
            and loo_put_peak is not None
            and loo_put_peak >= residual_spike_pct
        ),
        call_squeeze_demand_spike=(
            leave_one_out
            and loo_call_peak is not None
            and loo_call_peak >= residual_spike_pct
        ),
        n_strikes=len(pts),
        residual_provenance="leave_one_out" if leave_one_out else "in_sample",
        in_sample_max_put_wing_residual_pct=_peak(in_put),
        in_sample_max_call_wing_residual_pct=_peak(in_call),
    )
