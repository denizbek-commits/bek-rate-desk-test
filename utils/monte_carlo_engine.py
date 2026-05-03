"""
Bek Rate Desk — Monte Carlo Engine
====================================
Probability-weighted NII simulation using stochastic rate paths.

Cox-Ingersoll-Ross (CIR) mean-reverting short-rate model:
  dr = κ(θ - r)dt + σ√r·dW

  κ = mean reversion speed
  θ = long-run mean rate (%)
  σ = volatility of volatility (scaled to rate level)
  dW = Brownian increment

Why CIR over Vasicek for Turkish rates:
  - Vasicek allows negative rates (Gaussian increments unconstrained).
    At r=42.5% with σ=5% this floors ~15% of paths per step.
  - CIR variance is proportional to the level (σ²r·dt), so tails are
    asymmetric in the correct direction: high rates → higher absolute vol.
  - Feller condition 2κθ > σ² ensures zero is never reached.

Discretisation: exact non-central chi-squared transition (Glasserman 2004
p.124) rather than Euler, avoiding reflection bias at the boundary.

1000 paths, monthly steps, NII computed per path.
Output: NII distribution, VaR, CVaR, fan chart, stress grid.
"""

import numpy as np
from typing import Optional


def _cir_paths(r0: float, kappa: float, theta: float, sigma: float,
               n_paths: int, n_steps: int, dt: float,
               seed: int = 42) -> np.ndarray:
    """
    Exact CIR simulation via non-central chi-squared sampling.
    Returns (n_paths, n_steps+1) array of rate levels (%).

    Feller condition: 2κθ > σ² — checked and warned if violated.
    All inputs in % (e.g. r0=42.5, theta=35.0, sigma=5.0).
    """
    # Convert % → decimal for the SDE, then convert back
    r0_d     = r0     / 100.0
    theta_d  = theta  / 100.0
    sigma_d  = sigma  / 100.0

    feller = 2.0 * kappa * theta_d
    if feller <= sigma_d ** 2:
        # Warn but continue — paths will be non-negative but may touch zero
        import warnings
        warnings.warn(
            f"CIR Feller condition violated (2κθ={feller:.4f} ≤ σ²={sigma_d**2:.4f}). "
            "Zero boundary is reachable. Consider increasing κ or θ.",
            RuntimeWarning, stacklevel=2
        )

    rng   = np.random.default_rng(seed)
    paths = np.zeros((n_paths, n_steps + 1))
    paths[:, 0] = r0_d

    # Pre-compute constants that don't change step-to-step
    exp_kdt = np.exp(-kappa * dt)
    c       = sigma_d**2 * (1 - exp_kdt) / (4 * kappa)   # scale
    df      = 4 * kappa * theta_d / sigma_d**2            # degrees of freedom

    for t in range(n_steps):
        r_t  = paths[:, t]
        # Non-centrality parameter λ = r_t * exp(-κ dt) / c
        lam  = r_t * exp_kdt / c
        # Sample non-central chi-squared, scale back to rate level
        draws = rng.noncentral_chisquare(df=df, nonc=lam)
        paths[:, t + 1] = np.maximum(c * draws, 0.0)

    # Convert back to %
    return paths * 100.0


def _nii_for_path(rate_path: np.ndarray, assets: list,
                  liabilities: list, dt: float) -> float:
    """
    Compute NII for a single simulated rate path.

    Intra-period repricing: a fixed instrument that reprices at month rp
    earns its contractual rate for steps t < rp and the simulated rate
    thereafter — consistent with _compute_nii in alm_engine.py.
    """
    nii     = 0.0
    n_steps = len(rate_path) - 1

    for a in assets:
        amount = float(a.get("amount", 0))
        rate   = float(a.get("rate",   0)) / 100.0
        itype  = a.get("type", "fixed").lower()
        rp     = float(a.get("repricing_months", 9999))
        income = 0.0
        for t in range(n_steps):
            step_month = t + 1   # month number at end of this step
            if itype == "floating" or step_month > rp:
                r_eff = rate_path[t] / 100.0
            else:
                r_eff = rate
            income += amount * r_eff * dt
        nii += income

    for l in liabilities:
        amount = float(l.get("amount", 0))
        rate   = float(l.get("rate",   0)) / 100.0
        itype  = l.get("type", "fixed").lower()
        rp     = float(l.get("repricing_months", 9999))
        beta   = float(l.get("beta", 1.0))
        cost   = 0.0
        for t in range(n_steps):
            step_month = t + 1
            if itype == "floating" or step_month > rp:
                r_eff = rate_path[t] / 100.0 * beta
            else:
                r_eff = rate
            cost += amount * r_eff * dt
        nii -= cost

    return nii


def run_monte_carlo(data: dict) -> dict:
    """
    Monte Carlo NII simulation with CIR rate model.

    Input:
      assets, liabilities: same format as NII module (type, rate, amount, repricing_months)
                           liabilities can optionally include beta (deposit beta, default 1.0)
      n_paths:        int   (default 1000)
      horizon_months: int   (default 12)
      r0:             float — current short rate % (e.g. 45.0)
      kappa:          float — mean reversion speed (default 0.30)
      theta:          float — long-run rate % (default 35.0)
      sigma:          float — annual rate vol % (default 5.0)
      seed:           int   (default 42, for reproducibility)
      confidence:     float — VaR confidence (default 0.95)
    """
    assets     = data.get("assets",       [])
    liabilities= data.get("liabilities",  [])
    n_paths    = int(data.get("n_paths",        1000))
    horizon    = int(data.get("horizon_months",   12))
    from utils.tcmb_evds import get_policy_rate as _get_pr
    _live_rate = _get_pr()
    r0         = float(data.get("r0")     or _live_rate)
    kappa      = float(data.get("kappa",         0.30))
    theta      = float(data.get("theta")  or max(_live_rate - 7.5, 20.0))  # long-run mean ≈ current − 750bps
    sigma      = float(data.get("sigma",         5.0))
    seed       = int(data.get("seed",              42))
    confidence = float(data.get("confidence",    0.95))
    dt         = 1.0 / 12.0   # monthly steps

    paths = _cir_paths(r0, kappa, theta, sigma, n_paths, horizon, dt, seed)

    nii_outcomes = np.array([
        _nii_for_path(paths[i], assets, liabilities, dt)
        for i in range(n_paths)
    ])

    nii_sorted = np.sort(nii_outcomes)
    var_idx    = int((1 - confidence) * n_paths)
    var_nii    = float(nii_sorted[var_idx])
    cvar_nii   = float(np.mean(nii_sorted[:var_idx + 1]))

    pcts = [1, 5, 10, 25, 50, 75, 90, 95, 99]
    percentiles = {f"p{p}": round(float(np.percentile(nii_outcomes, p)), 2) for p in pcts}

    # Histogram (20 bins)
    hist_counts, hist_edges = np.histogram(nii_outcomes, bins=20)
    histogram = [
        {"bin_left":  round(float(hist_edges[i]),   2),
         "bin_right": round(float(hist_edges[i+1]), 2),
         "count":     int(hist_counts[i]),
         "freq_pct":  round(int(hist_counts[i]) / n_paths * 100, 2)}
        for i in range(len(hist_counts))
    ]

    # Fan chart — rate percentile bands over time
    fan_chart = []
    for t in range(horizon + 1):
        r_t = paths[:, t]
        fan_chart.append({
            "month": t,
            "p5":  round(float(np.percentile(r_t, 5)),  3),
            "p25": round(float(np.percentile(r_t, 25)), 3),
            "p50": round(float(np.percentile(r_t, 50)), 3),
            "p75": round(float(np.percentile(r_t, 75)), 3),
            "p95": round(float(np.percentile(r_t, 95)), 3),
        })

    # 20 sample paths for chart
    sample_idx   = np.linspace(0, n_paths - 1, 20, dtype=int)
    sample_paths = [
        {"path_id":   int(i),
         "nii":       round(float(nii_outcomes[i]), 2),
         "rate_path": [round(float(r), 3) for r in paths[i]]}
        for i in sample_idx
    ]

    return {
        "n_paths":        n_paths,
        "horizon_months": horizon,
        "model_params":   {"model": "CIR", "r0": r0, "kappa": kappa, "theta": theta, "sigma": sigma,
                           "feller_ok": 2 * kappa * (theta / 100.0) > (sigma / 100.0) ** 2},
        "mean_nii":       round(float(np.mean(nii_outcomes)),   2),
        "median_nii":     round(float(np.median(nii_outcomes)), 2),
        "std_nii":        round(float(np.std(nii_outcomes)),    2),
        "min_nii":        round(float(np.min(nii_outcomes)),    2),
        "max_nii":        round(float(np.max(nii_outcomes)),    2),
        "var_nii":        round(var_nii,  2),
        "cvar_nii":       round(cvar_nii, 2),
        "confidence":     confidence,
        "percentiles":    percentiles,
        "histogram":      histogram,
        "fan_chart":      fan_chart,
        "sample_paths":   sample_paths,
    }


def run_stress_grid(data: dict) -> dict:
    """
    2D stress grid: NII across parallel shift × slope shock combinations.
    Classic ALM stress test table — rows=level, cols=slope.
    """
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from utils.alm_engine import _compute_nii

    assets      = data.get("assets",      [])
    liabilities = data.get("liabilities", [])
    horizon     = int(data.get("horizon_months", 12))

    parallel_shocks = [-200, -150, -100, -50, 0, +50, +100, +150, +200]
    slope_shocks    = [-150, -100, -50,   0, +50, +100, +150]

    base_nii = _compute_nii(assets, liabilities, {"parallel": 0.0}, horizon)

    grid = []
    for par in parallel_shocks:
        row = []
        for slope in slope_shocks:
            short = (par - slope / 2) / 10000.0
            long  = (par + slope / 2) / 10000.0
            shocks = {
                "Overnight": short,
                "1M":        short,
                "3M":        short * 0.85 + long * 0.15,
                "6M":        short * 0.65 + long * 0.35,
                "1Y":        short * 0.50 + long * 0.50,
                "2Y":        short * 0.30 + long * 0.70,
                "5Y+":       long,
            }
            nii_val = _compute_nii(assets, liabilities, shocks, horizon)
            row.append({
                "parallel_bps": par,
                "slope_bps":    slope,
                "nii":          round(nii_val, 2),
                "delta_nii":    round(nii_val - base_nii, 2),
            })
        grid.append(row)

    return {
        "base_nii":        round(base_nii, 2),
        "parallel_shocks": parallel_shocks,
        "slope_shocks":    slope_shocks,
        "grid":            grid,
    }
