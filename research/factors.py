"""Causal price factors and cross-sectional diagnostics. No claim of executable P&L."""
from __future__ import annotations

import numpy as np
import pandas as pd


def price_factors(bars: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Input: complete exchange-session grid, including NaN on missing/suspended rows.

    adj_close/adj_open must use same per-security adjustment basis. No implicit fill.
    Index=date, columns=historical union of securities, never today's survivors only.
    """
    close = bars.pivot(index="date", columns="code", values="adj_close").sort_index()
    turnover = bars.pivot(index="date", columns="code", values="turnover").reindex_like(close)
    ret = close.pct_change(fill_method=None)
    return {
        "reversal_5d": -close.pct_change(5, fill_method=None),
        "low_vol_20d": -ret.rolling(20, min_periods=20).std(),
        "abnormal_turnover": -(turnover / turnover.rolling(20, min_periods=20).mean() - 1),
    }


def fundamental_factors(panel: pd.DataFrame) -> pd.DataFrame:
    """Only use a panel already joined by available_date, never end_date alone."""
    required = {"date", "available_date", "pe_ttm", "pb", "roe", "ocf_to_profit", "total_mv"}
    if required - set(panel):
        raise ValueError(f"missing visible fundamentals: {sorted(required - set(panel))}")
    if (pd.to_datetime(panel.available_date) > pd.to_datetime(panel.date)).any():
        raise ValueError("future financial disclosure in factor input")
    out = pd.DataFrame(index=panel.index)
    out["ep"] = 1 / panel.pe_ttm.where(panel.pe_ttm > 0)
    out["bp"] = 1 / panel.pb.where(panel.pb > 0)
    out["roe"] = panel.roe
    out["quality"] = panel.ocf_to_profit
    out["small_size"] = -np.log(panel.total_mv.where(panel.total_mv > 0))
    return out


def preprocess(values: pd.Series, industry: pd.Series | None = None,
               market_cap: pd.Series | None = None) -> pd.Series:
    """MAD winsorize, OLS industry/log-size residual, standardize within ONE date."""
    x = values.replace([np.inf, -np.inf], np.nan).dropna().astype(float)
    if industry is not None:
        x = x.loc[industry.reindex(x.index).notna()]
    if market_cap is not None:
        cap = market_cap.reindex(x.index)
        x = x.loc[cap.gt(0) & np.isfinite(cap)]
    if len(x) < 3:
        return pd.Series(np.nan, index=values.index)
    mad = (x - x.median()).abs().median()
    if mad > 0:
        x = x.clip(x.median() - 5 * 1.4826 * mad, x.median() + 5 * 1.4826 * mad)
    design = pd.DataFrame({"intercept": 1.0}, index=x.index)
    if industry is not None:
        design = pd.concat([design, pd.get_dummies(industry.reindex(x.index), drop_first=True, dtype=float)], axis=1)
    if market_cap is not None:
        logcap = np.log(market_cap.reindex(x.index))
        design["size"] = logcap - logcap.mean()
    matrix = design.to_numpy(dtype=float)
    if len(x) <= np.linalg.matrix_rank(matrix) + 1:
        return pd.Series(np.nan, index=values.index)
    residual = x - matrix @ np.linalg.lstsq(matrix, x.to_numpy(), rcond=None)[0]
    scale = residual.std(ddof=0)
    result = residual / scale if scale > 1e-12 else residual * 0
    return result.reindex(values.index)


def forward_returns(opens: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """T-close signal, T+1 open entry, T+h+1 open exit, h>=1 exchange sessions."""
    if horizon < 1 or not opens.index.is_monotonic_increasing or opens.index.has_duplicates:
        raise ValueError("invalid horizon or session grid")
    return opens.shift(-(horizon + 1)) / opens.shift(-1) - 1


def evaluate_factor(scores: pd.DataFrame, opens: pd.DataFrame, eligible: pd.DataFrame,
                    horizon: int = 5, groups: int = 5, cost_bps: float = 20) -> dict:
    """Non-overlapping cohorts; missing selected outcomes invalidate the WHOLE cohort.

    Quantile forward returns and a turnover cost proxy are diagnostics. A-share fills,
    halts, limit prices, and delisting cashflows belong in the existing Paper matcher.
    """
    if not scores.index.equals(opens.index) or not scores.columns.equals(opens.columns):
        raise ValueError("scores/labels must share the exchange grid")
    if groups < 2 or cost_bps < 0:
        raise ValueError("invalid groups or costs")
    labels = forward_returns(opens, horizon)
    rows, previous, skipped = [], pd.Series(0.0, index=scores.columns), 0
    # Use a fixed calendar cadence; never pick dates based on whether returns exist.
    for day in scores.index[::horizon]:
        known = eligible.reindex_like(scores).loc[day].eq(True)
        score = preprocess(scores.loc[day].where(known)).dropna()
        if len(score) < groups * 2 or score.nunique() < groups:
            continue
        bucket = pd.qcut(score.rank(method="first"), groups, labels=False) + 1
        outcome = labels.loc[day].reindex(score.index)
        if not np.isfinite(outcome).all():
            skipped += 1
            continue
        top = score.index[bucket.eq(groups)]
        weights = pd.Series(0.0, index=scores.columns)
        weights.loc[top] = 1 / len(top)
        # Drift the last cohort's weights using only its now-realized holding return.
        turnover = float((weights - previous).abs().sum())
        top_ret = float(outcome.loc[top].mean())
        record = {"date": str(day), "ic": float(score.corr(outcome)) if outcome.nunique() > 1 else None,
                  "rank_ic": float(score.corr(outcome, method="spearman")) if outcome.nunique() > 1 else None,
                  "turnover_l1": turnover,
                  "top_net_proxy": top_ret - turnover * cost_bps / 10000}
        record.update({f"q{i}": float(outcome.loc[bucket.eq(i)].mean()) for i in range(1, groups + 1)})
        rows.append(record)
        grown = weights * (1 + labels.loc[day].fillna(0))
        previous = grown / grown.sum() if grown.sum() > 0 else weights
    if not rows:
        raise ValueError("no complete non-overlapping cohorts")
    detail = pd.DataFrame(rows)
    mean = lambda col: float(detail[col].mean()) if detail[col].notna().any() else None
    return {"horizon": horizon, "cohorts": len(rows), "incomplete_cohorts": skipped,
            "mean_ic": mean("ic"), "mean_rank_ic": mean("rank_ic"),
            "mean_turnover_l1": float(detail.turnover_l1.mean()),
            "quantiles": {f"q{i}": float(detail[f"q{i}"].mean()) for i in range(1, groups + 1)},
            "rows": rows, "execution_validated": False,
            "warning": "Diagnostic returns, not tradable alpha; costs are a proxy; no promotion."}
