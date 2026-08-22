# -*- coding: utf-8 -*-
"""pipeline/gate.py：门禁 v2 拒绝逻辑"""
from pipeline.gate import evaluate


def _metrics(annual=0.10, max_dd=0.20, sharpe=0.6, excess=0.05, ir=0.5,
             excess_dd=0.25, turnover=10.0, dd_days=500):
    return {"annual": annual, "max_dd": max_dd, "sharpe": sharpe,
            "excess_annual": excess, "ir": ir, "excess_max_dd": excess_dd,
            "turnover_annual": turnover, "longest_dd_days": dd_days}


class TestGate:
    def test_all_pass(self):
        r = evaluate(_metrics(), _metrics(), [_metrics()], {"family_dup": False})
        assert r["gate"] == "pass", r["reasons"]

    def test_oos_negative_excess_rejects(self):
        r = evaluate(_metrics(), _metrics(excess=-0.04), [_metrics()],
                     {"family_dup": False})
        assert r["gate"] == "reject"
        assert any("样本外" in x and "excess_annual" in x for x in r["reasons"])

    def test_perturbation_flip_rejects(self):
        r = evaluate(_metrics(), _metrics(), [_metrics(max_dd=0.45)],
                     {"family_dup": False})
        assert r["gate"] == "reject"
        assert any("扰动1" in x for x in r["reasons"])

    def test_family_dup_rejects(self):
        r = evaluate(_metrics(), _metrics(), [_metrics()],
                     {"family_dup": True, "family": "momentum_rotation",
                      "family_representative": "momentum_stops"})
        assert r["gate"] == "reject"
        assert any("策略族" in x for x in r["reasons"])

    def test_missing_oos_rejects(self):
        r = evaluate(_metrics(), None, [], {"family_dup": False})
        assert r["gate"] == "reject"

    def test_high_turnover_rejects(self):
        r = evaluate(_metrics(turnover=60.0), _metrics(), [_metrics()],
                     {"family_dup": False})
        assert r["gate"] == "reject"
        assert any("turnover" in x for x in r["reasons"])

    def test_longest_dd_days_rejects(self):
        r = evaluate(_metrics(dd_days=1707), _metrics(), [_metrics()],
                     {"family_dup": False})
        assert r["gate"] == "reject"
        assert any("longest_dd_days" in x for x in r["reasons"])

    def test_missing_metric_rejects(self):
        m = _metrics()
        m["ir"] = None
        r = evaluate(m, _metrics(), [_metrics()], {"family_dup": False})
        assert r["gate"] == "reject"
