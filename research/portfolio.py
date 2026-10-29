"""Index-relative optimization and purged chronological validation primitives."""
from __future__ import annotations

import numpy as np
import pandas as pd


def walk_forward(dates, train: int, test: int, embargo: int, horizon: int):
    dates = pd.DatetimeIndex(dates)
    if min(train, test, horizon) < 1 or embargo < horizon + 1:
        raise ValueError("embargo must cover T+1 entry and complete forward label")
    if not dates.is_monotonic_increasing or dates.has_duplicates:
        raise ValueError("chronological unique session index required")
    for edge in range(train + embargo, len(dates) - test + 1, test):
        yield dates[edge - embargo - train:edge - embargo], dates[edge:edge + test]


def optimize(alpha, benchmark, previous, covariance, exposures, *,
             max_active=0.02, exposure_limit=0.02, te_limit=0.08,
             turnover_limit=0.20, risk_aversion=10.0):
    """Long-only, fully invested; annual covariance; L1 turnover (both sides)."""
    import cvxpy as cp
    a, b, p, cov, x = map(lambda v: np.asarray(v, dtype=float),
                          (alpha, benchmark, previous, covariance, exposures))
    n = len(a)
    if b.shape != (n,) or p.shape != (n,) or cov.shape != (n, n) or x.ndim != 2 or x.shape[0] != n:
        raise ValueError("unaligned portfolio inputs")
    if not all(np.isfinite(v).all() for v in (a, b, p, cov, x)):
        raise ValueError("nonfinite portfolio input")
    if (b < 0).any() or (p < 0).any() or not np.isclose(b.sum(), 1) or not np.isclose(p.sum(), 1):
        raise ValueError("benchmark and previous must be fully invested nonnegative weights")
    if not np.allclose(cov, cov.T) or np.linalg.eigvalsh(cov).min() < -1e-10:
        raise ValueError("covariance must be symmetric positive semidefinite")
    if min(max_active, exposure_limit, te_limit, turnover_limit, risk_aversion) < 0:
        raise ValueError("negative constraint")
    w = cp.Variable(n)
    active = w - b
    risk = cp.quad_form(active, cp.psd_wrap(cov))
    constraints = [w >= 0, cp.sum(w) == 1, cp.abs(active) <= max_active,
                   cp.abs(x.T @ active) <= exposure_limit,
                   risk <= te_limit ** 2, cp.norm1(w - p) <= turnover_limit]
    problem = cp.Problem(cp.Maximize(a @ active - risk_aversion * risk), constraints)
    problem.solve()
    if problem.status != cp.OPTIMAL or w.value is None:
        raise ValueError(f"optimizer refused portfolio: {problem.status}")
    result = np.asarray(w.value)
    if any(np.max(np.asarray(c.violation())) > 1e-6 for c in constraints):
        raise ValueError("optimizer constraint residual exceeded tolerance")
    return result


def excess_metrics(portfolio: pd.Series, benchmark: pd.Series) -> dict:
    if not portfolio.index.equals(benchmark.index) or len(portfolio) < 2:
        raise ValueError("aligned realized daily returns required")
    if not np.isfinite(portfolio).all() or not np.isfinite(benchmark).all() or (portfolio <= -1).any() or (benchmark <= -1).any():
        raise ValueError("invalid realized returns")
    active = portfolio - benchmark
    te = float(active.std(ddof=1) * np.sqrt(252))
    relative = (1 + portfolio).prod() / (1 + benchmark).prod()
    return {"excess_annual": float(relative ** (252 / len(active)) - 1),
            "information_ratio": float(active.mean() * 252 / te) if te > 0 else None,
            "tracking_error": te}
