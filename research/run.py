"""One research loop: historical universe -> factors -> diagnostics -> experiment ledger.

Legacy bundle mode is an offline PRICE-ONLY feasibility baseline, not an index
enhancement backtest. It cannot promote strategies or touch Paper accounts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline.universe import UniverseBuilder
from .experiments import ExperimentLedger
from .factors import evaluate_factor, price_factors


def adjusted_bars(bundle, code: str) -> pd.DataFrame:
    """RQAlpha's missing-factor entry means no corporate action, not missing bars.

    Use raw * cumulative factor, without an end-of-sample normalization constant.
    """
    frame = pd.DataFrame(bundle._stocks[code][:])
    frame.index = pd.to_datetime(frame.datetime, format="%Y%m%d%H%M%S")
    factor = pd.Series(1.0, index=frame.index)
    if code in bundle._factors:
        raw = pd.DataFrame(bundle._factors[code][:])
        raw = raw.loc[raw.start_date > 0]
        if not raw.empty:
            series = pd.Series(raw.ex_cum_factor.to_numpy(),
                               index=pd.to_datetime(raw.start_date, format="%Y%m%d%H%M%S")).sort_index()
            factor = series.reindex(frame.index, method="ffill").fillna(1.0)
    result = pd.DataFrame({"adj_close": frame.close * factor,
                           "adj_open": frame.open * factor,
                           "turnover": frame.total_turnover}, index=frame.index)
    result.loc[frame.volume <= 0, ["adj_close", "adj_open", "turnover"]] = np.nan
    return result


def bundle_panel(start: str, end: str, n: int):
    from kde_levels.levels import BundleData
    builder, bundle = UniverseBuilder(), BundleData()
    try:
        index = bundle.load_index("000300.XSHG")
        days = index.loc[start:end].index
        if days.empty:
            raise ValueError("no covered sessions")
        # Membership at the first REAL exchange session of each month, even when
        # the requested evaluation window starts mid-month.
        months = index.loc[:end].index.to_series().groupby(index.loc[:end].index.to_period("M")).first()
        months = months.loc[months.dt.to_period("M") >= days[0].to_period("M")]
        universes = {day: builder.build(day.date().isoformat(), n=n) for day in months}
        codes = sorted({code for members in universes.values() for code in members})
        if not codes:
            raise ValueError("empty historical universe")
        # Warm-up and labels share a full exchange calendar, never per-stock row shifts.
        warmup = index.index[max(0, index.index.get_loc(days[0]) - 30)]
        grid = index.loc[warmup:end].index
        frames = []
        for code in codes:
            prices = adjusted_bars(bundle, code).reindex(grid)
            frames.append(prices[["adj_close", "adj_open", "turnover"]].assign(code=code, date=grid))
        panel = pd.concat(frames, ignore_index=True)
        eligible = pd.DataFrame(False, index=grid, columns=codes)
        starts = sorted(universes)
        for i, day in enumerate(starts):
            period = grid >= day
            if i + 1 < len(starts):
                period &= grid < starts[i + 1]
            for current in grid[period]:
                day_int = int(current.strftime("%Y%m%d"))
                valid = [code for code in universes[day] if not builder.is_st(code, day_int)
                         and not builder.is_suspended(code, day_int)]
                eligible.loc[current, valid] = True
        spec = {"universe": {str(day.date()): members for day, members in universes.items()},
                "data_hash": hashlib.sha256(pd.util.hash_pandas_object(panel, index=False).values.tobytes()).hexdigest(),
                "eligibility_hash": hashlib.sha256(pd.util.hash_pandas_object(eligible).values.tobytes()).hexdigest(),
                "latest_bundle_session": str(index.index[-1].date()), "n": n}
        return panel, eligible, spec
    finally:
        builder.close()
        for stream in (bundle._stocks, bundle._factors, bundle._indexes):
            stream.close()


def run(start: str, end: str, n: int, output: Path, ledger_path: Path | None = None):
    if start > end or n < 10:
        raise ValueError("invalid research window/universe")
    panel, eligible, data_spec = bundle_panel(start, end, n)
    opens = panel.pivot(index="date", columns="code", values="adj_open").sort_index()
    factors = price_factors(panel)
    ledger = ExperimentLedger(ledger_path) if ledger_path else ExperimentLedger()
    report = {"mode": "legacy_bundle_price_diagnostics", "window": [start, end],
              "data": data_spec, "factors": {}, "strategy_promotion": False,
              "limitations": ["No historical industry/size neutralization data",
                               "No index enhancement claim; no fill simulation",
                               "Incomplete return cohorts excluded and explicitly counted",
                               "Historical vendor revisions not original observed vintages"]}
    code_hash = hashlib.sha256(Path(__file__).with_name("factors.py").read_bytes()).hexdigest()
    for name, scores in factors.items():
        for horizon in (1, 5, 20):
            trial = ledger.begin(name, {"window": [start, end], "horizon": horizon,
                                       "cost_bps": 20, "code_hash": code_hash, **data_spec})
            try:
                result = evaluate_factor(scores.loc[start:end], opens.loc[start:end],
                                         eligible.loc[start:end], horizon=horizon)
            except Exception as exc:
                ledger.finish(trial, error=f"{type(exc).__name__}: {exc}")
                report["factors"][f"{name}/{horizon}"] = {"error": str(exc)}
            else:
                ledger.finish(trial, {key: value for key, value in result.items() if isinstance(value, (int, float))})
                report["factors"][f"{name}/{horizon}"] = result
    report["status"] = "failed" if any("error" in result for result in report["factors"].values()) else "complete_diagnostics"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2026-08-31")
    parser.add_argument("--universe-size", type=int, default=100)
    parser.add_argument("--output", type=Path, default=Path("artifacts/research/latest.json"))
    args = parser.parse_args()
    report = run(args.start, args.end, args.universe_size, args.output)
    print(json.dumps({"output": str(args.output), "factors": {
        name: {key: value for key, value in result.items() if key != "rows"}
        for name, result in report["factors"].items()}}, ensure_ascii=False, indent=2))
    if report["status"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
