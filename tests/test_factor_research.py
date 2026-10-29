import json

import numpy as np
import pandas as pd
import pytest

from research.data import validate_observations, select_asof, ParquetStore
from research.experiments import ExperimentLedger
from research.factors import price_factors, preprocess, forward_returns, evaluate_factor, fundamental_factors
from research.portfolio import walk_forward, excess_metrics
from research.tushare import normalize, TushareSource


def observations():
    return pd.DataFrame([
        {"ts_code": "old", "data_date": "2024-03-31", "available_date": "2024-04-30",
         "observed_at": "2024-05-01T00:00:00Z", "source": "test", "vintage": "original", "roe": 10},
        {"ts_code": "old", "data_date": "2024-03-31", "available_date": "2024-06-01",
         "observed_at": "2024-06-02T00:00:00Z", "source": "test", "vintage": "revision", "roe": 20}])


def test_two_clocks_exclude_late_disclosures_and_revisions():
    frame = validate_observations("fundamentals", observations())
    assert select_asof(frame, "fundamentals", "2024-04-29", "2024-08-01").empty
    assert select_asof(frame, "fundamentals", "2024-05-10", "2024-08-01").roe.item() == 10
    assert select_asof(frame, "fundamentals", "2024-06-10", "2024-05-02").roe.item() == 10
    assert select_asof(frame, "fundamentals", "2024-06-10", "2024-08-01").roe.item() == 20


def test_invalid_visibility_and_duplicate_rows_rejected():
    bad = observations().iloc[:1].copy()
    bad["available_date"] = "2024-03-01"
    with pytest.raises(ValueError, match="precedes"):
        validate_observations("fundamentals", bad)
    with pytest.raises(ValueError, match="duplicate"):
        validate_observations("fundamentals", pd.concat([observations()] * 2))


def test_vendor_financial_restatement_not_backdated():
    frame = pd.DataFrame([{"ts_code": "600000.SH", "end_date": "20240331", "ann_date": "20240429"}])
    result = normalize("fina_indicator", frame, "2026-10-22T10:00:00Z")
    assert result.available_date.item() == "2026-10-22"
    assert result.vintage.item() == "retrospective_vendor_snapshot"


def test_financial_revisions_collected_together_have_deterministic_order():
    source = pd.DataFrame([
        {"ts_code": "A", "end_date": "20240331", "ann_date": "20240429", "roe": 10},
        {"ts_code": "A", "end_date": "20240331", "ann_date": "20240601", "roe": 20}])
    rows = validate_observations("fundamentals", normalize("fina_indicator", source, "2026-10-22T10:00:00Z"))
    assert select_asof(rows, "fundamentals", "2024-06-10", "2026-10-23").empty
    assert select_asof(rows, "fundamentals", "2026-10-22", "2026-10-23").roe.item() == 20


def test_index_weight_trade_date_does_not_prove_historical_publication_date():
    rows = pd.DataFrame([{"index_code": "I", "con_code": "A", "trade_date": "20240131", "weight": 100}])
    normalized = normalize("index_weight", rows, "2026-10-22T10:00:00Z")
    assert normalized.available_date.item() == "2026-10-22"


def test_index_rebalance_does_not_keep_removed_constituents():
    from research.panel import index_membership
    rows = pd.DataFrame([
        {"index_code": "I", "con_code": "removed", "data_date": "2024-01-01", "available_date": "2024-01-01", "weight": 100},
        {"index_code": "I", "con_code": "new", "data_date": "2024-02-01", "available_date": "2024-02-01", "weight": 100}])
    assert index_membership(rows, "2024-01-31", "I").con_code.tolist() == ["removed"]
    assert index_membership(rows, "2024-02-02", "I").con_code.tolist() == ["new"]
    rows.loc[1, "weight"] = 50
    with pytest.raises(ValueError, match="incomplete"):
        index_membership(rows, "2024-02-02", "I")


def test_missing_factor_entry_matches_rqalpha_no_adjustment_semantics():
    from research.run import adjusted_bars
    from types import SimpleNamespace
    stock = np.array([(20240101000000, 10., 9., 100., 1000.),
                      (20240102000000, 20., 19., 0., 0.)],
                     dtype=[("datetime", "i8"), ("close", "f8"), ("open", "f8"),
                            ("volume", "f8"), ("total_turnover", "f8")])
    bundle = SimpleNamespace(_stocks={"A": stock}, _factors={})
    result = adjusted_bars(bundle, "A")
    assert result.adj_close.iloc[0] == 10
    assert np.isnan(result.adj_close.iloc[1])


def test_vendor_missing_token_and_repeated_page_fail(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="missing"):
        TushareSource()
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"code": 0, "data": {"fields": ["ts_code"], "items": [["A"]]}}
    class Session:
        def post(self, *args, **kwargs):
            return Response()
    with pytest.raises(RuntimeError, match="pagination"):
        TushareSource("fake", Session()).fetch("daily", {}, page_size=1)


def test_parquet_duckdb_matches_reference(tmp_path):
    pytest.importorskip("pyarrow", reason="Parquet integration requires requirements-research.txt")
    pytest.importorskip("duckdb", reason="DuckDB integration requires requirements-research.txt")
    frame = observations()
    store = ParquetStore(tmp_path)
    receipt = store.append("fundamentals", frame)
    assert store.append("fundamentals", frame) == receipt
    assert len(list(tmp_path.glob("fundamentals/*.parquet"))) == 1
    actual = store.asof("fundamentals", "2024-05-10", "2024-08-01")
    assert actual.roe.item() == 10


def panel():
    rng = np.random.default_rng(42)
    dates, codes = pd.bdate_range("2024-01-01", periods=90), [str(i) for i in range(30)]
    values = np.exp(np.cumsum(rng.normal(0, .015, (90, 30)), axis=0)) * 100
    rows = [{"date": day, "code": code, "adj_close": values[i, j],
             "adj_open": values[i, j] * .999, "turnover": 10000000 + i * 100 + j}
            for i, day in enumerate(dates) for j, code in enumerate(codes)]
    return pd.DataFrame(rows), dates, codes


def test_price_factors_unchanged_by_future_prices():
    bars, dates, _ = panel()
    original = price_factors(bars)
    bars.loc[bars.date > dates[50], "adj_close"] *= 7
    changed = price_factors(bars)
    for name in original:
        pd.testing.assert_frame_equal(original[name].loc[:dates[50]], changed[name].loc[:dates[50]])


def test_t_plus_one_label_and_missing_session_not_compressed():
    opens = pd.DataFrame({"A": [10, 20, np.nan, 40, 50]}, index=pd.bdate_range("2024-01-01", periods=5))
    label = forward_returns(opens, 1)
    assert np.isnan(label.A.iloc[0])  # missing exit does not jump to next stock row
    assert np.isnan(label.A.iloc[1])
    assert label.A.iloc[2] == pytest.approx(.25)


def test_industry_size_neutralization_removes_exposures():
    rng = np.random.default_rng(1)
    industry = pd.Series(["a"] * 20 + ["b"] * 20)
    cap = pd.Series(np.exp(rng.normal(10, 1, 40)))
    values = cap.map(np.log) * 3 + industry.eq("b") * 10 + rng.normal(0, .1, 40)
    score = preprocess(values, industry, cap)
    assert abs(score.corr(np.log(cap))) < 1e-9
    assert score.groupby(industry).mean().abs().max() < 1e-9


def test_nonoverlap_quantiles_and_missing_outcomes_disclosed():
    bars, _, _ = panel()
    scores = price_factors(bars)["reversal_5d"]
    opens = bars.pivot(index="date", columns="code", values="adj_open")
    eligible = pd.DataFrame(True, index=opens.index, columns=opens.columns)
    result = evaluate_factor(scores, opens, eligible, horizon=5)
    days = [pd.Timestamp(row["date"]) for row in result["rows"]]
    assert all((b - a).days >= 5 for a, b in zip(days, days[1:]))
    assert result["incomplete_cohorts"] > 0
    assert result["execution_validated"] is False
    assert result["quantiles"].keys() == {"q1", "q2", "q3", "q4", "q5"}


def test_fundamentals_need_publication_alignment():
    frame = pd.DataFrame([{key: 1 for key in ("pe_ttm", "pb", "roe", "ocf_to_profit", "total_mv")}])
    frame["date"], frame["available_date"] = "2024-01-01", "2024-02-01"
    with pytest.raises(ValueError, match="future"):
        fundamental_factors(frame)


def test_walk_forward_purges_entire_forward_label():
    days = pd.bdate_range("2024-01-01", periods=100)
    splits = list(walk_forward(days, train=30, test=10, embargo=6, horizon=5))
    for train, test in splits:
        assert days.get_loc(test[0]) - days.get_loc(train[-1]) == 7
    with pytest.raises(ValueError, match="embargo"):
        list(walk_forward(days, 30, 10, 5, 5))


def test_experiment_counts_failures_and_distinct_specs(tmp_path):
    ledger = ExperimentLedger(tmp_path / "experiments.jsonl")
    a = ledger.begin("factor", {"window": ["a", "b"], "parameter": 1})
    ledger.finish(a, {"sr_monthly": .1})
    b = ledger.begin("factor", {"window": ["a", "b"], "parameter": 2})
    ledger.finish(b, error="failed")
    ledger.begin("factor", {"window": ["a", "b"], "parameter": 1})
    evidence = ledger.evidence("factor", ["a", "b"])
    assert evidence == {"n_trials": 2, "monthly_sharpes": [.1], "missing_results": 1}
    assert len([json.loads(line) for line in ledger.path.read_text().splitlines()]) == 5


def test_dsr_missing_history_or_zero_variance_cannot_pass():
    from pipeline.gate import evaluate
    metrics = {"sr_monthly": .3, "months": 80}
    missing = evaluate(metrics, metrics, [], {}, n_trials=30)
    assert any("真实" in reason for reason in missing["reasons"])
    zero = evaluate(metrics, metrics, [], {}, n_trials=2, trial_sharpes=[.3, .3])
    assert any("不可估计" in reason for reason in zero["reasons"])


def test_excess_metrics_against_benchmark():
    benchmark = pd.Series([.01, -.02, .005, .01])
    result = excess_metrics(benchmark + pd.Series([.001, .002, .001, -.001]), benchmark)
    assert result["tracking_error"] > 0
    assert result["excess_annual"] > 0


def test_optimizer_respects_risk_and_turnover():
    pytest.importorskip("cvxpy", reason="Optimizer integration requires requirements-research.txt")
    from research.portfolio import optimize
    b = np.full(10, .1)
    weights = optimize(np.arange(10) / 100, b, b, np.eye(10) * .04,
                       np.ones((10, 1)), max_active=.02, turnover_limit=.1)
    assert np.isclose(weights.sum(), 1)
    assert abs(weights - b).sum() <= .100001
    assert abs(weights - b).max() <= .020001
