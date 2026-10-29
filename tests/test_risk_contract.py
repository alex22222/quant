import json

import pytest

from trading_team.decision_pipeline import canonical_plan_hash
from trading_team.kimi_daily import _apply_risk_reviews
from trading_team.paper_plans import derive_paper_proposals


def buy(code="600036", **changes):
    return {"code": code, "direction": "条件买入", "position_pct": .1, "entry": [40],
            "entry_type": "limit", "stop": 38, "condition": None,
            "execution_conditions": [{"field": "open_gap_pct", "op": "lte", "value": .03}], **changes}


def review(code="600036", decision="否决", reasons=None, **changes):
    return {"code": code, "decision": decision,
            "reason_codes": ["market_gate"] if reasons is None else reasons,
            "reason": "offline risk assessment", **changes}


@pytest.mark.parametrize("reasons,restored", [(["market_gate"], True),
    (["market_gate", "stop_distance"], False), (["liquidity"], False),
    (["data_quality", "market_gate"], False), (["other"], False)])
def test_only_explicit_single_market_reason_can_restore(reasons, restored):
    raw = [buy()]
    final = _apply_risk_reviews(raw, [review(reasons=reasons, clause="条款4", reason="market_ok=false; risk")], market_ok=False)
    candidates = derive_paper_proposals(raw, final, market_ok=False)
    assert (candidates[0]["direction"] == "条件买入") is restored
    if restored:
        for key in ("entry", "entry_type", "stop", "execution_conditions", "position_pct"):
            assert candidates[0][key] == raw[0][key]
    assert candidates[0]["risk_review"]["reason_codes"] == reasons


@pytest.mark.parametrize("note", ["market_ok=false", "条款4：止损无效", "市场闸门关闭"])
def test_legacy_prose_never_authorizes_restoration(note):
    raw = [buy()]
    final = [buy(direction="回避", position_pct=0, entry=None, stop=None,
                 risk_clause="条款4", note=note)]
    assert derive_paper_proposals(raw, final) == final


@pytest.mark.parametrize("reviews", [None, {}, [None],
    [review(), review()], [review(code="601318")],
    [review(reason_codes=None)], [review(reason_codes="market_gate")],
    [review(reasons=[])], [review(reasons=["market_gate", "market_gate"])],
    [review(reasons=["unknown"])], [review(reasons=[True])],
    [review(decision="通过", reasons=["market_gate"])],
    [review(decision="降仓通过", reasons=["market_gate"], position_pct=.05)],
    [review(decision="降仓通过", reasons=["position_limit"], position_pct=True)],
    [review(decision="降仓通过", reasons=["position_limit"], position_pct=0)],
    [review(decision="降仓通过", reasons=["position_limit"], position_pct=float("nan"))],
    [review(reason="")], [review(direction="卖出")], [review(entry=[30])],
])
def test_invalid_or_ambiguous_review_fails_closed(reviews):
    with pytest.raises(ValueError, match="risk contract"):
        _apply_risk_reviews([buy()], reviews, market_ok=False)


def test_market_reason_cannot_override_open_gate_or_reject_exit():
    with pytest.raises(ValueError, match="risk contract"):
        _apply_risk_reviews([buy()], [review()], market_ok=True)
    exit_plan = {"code": "600036", "direction": "卖出", "position_pct": 0,
                 "exit": {"type": "market", "qty": "all", "time_in_force": "DAY"}}
    with pytest.raises(ValueError, match="risk contract"):
        _apply_risk_reviews([exit_plan], [review()], market_ok=False)


def test_reduction_survives_no_gate_without_restoring_original_size():
    raw = [buy()]
    final = _apply_risk_reviews(raw, [review(decision="降仓通过", reasons=["position_limit"],
                 position_pct=.05, clause="条款4", reason="market_ok=false")], market_ok=False)
    assert derive_paper_proposals(raw, final, market_ok=False)[0]["position_pct"] == .05
    assert raw[0]["position_pct"] == .1


def reviewed_document():
    from trading_team.risk_contract import build_reviewed_plan
    return build_reviewed_plan("2026-09-21", "2026-09-18", False, [buy()], [review()])


@pytest.mark.parametrize("field", ["review", "original", "paper", "final", "version"])
def test_versioned_risk_document_rejects_derived_drift(field):
    from trading_team.risk_contract import validate_risk_document
    doc = reviewed_document()
    before = canonical_plan_hash(doc)
    if field == "review":
        doc["risk_reviews"][0]["reason_codes"].append("data_quality")
    elif field == "original":
        doc["trader_proposals"][0]["entry"] = [39]
    elif field == "paper":
        doc["paper_proposals"][0]["position_pct"] = .2
    elif field == "final":
        doc["proposals"][0]["direction"] = "买入"
    else:
        doc["risk_contract"] = "unknown"
    assert canonical_plan_hash(doc) != before
    with pytest.raises(ValueError, match="risk contract"):
        validate_risk_document(doc)


def test_materialization_rejects_forged_paper_candidates_before_writes(tmp_path):
    from trading_team.paper_pipeline import materialize_paper_decision
    doc = reviewed_document()
    doc["paper_proposals"][0]["entry"] = [39]
    team = tmp_path / "trading_team"
    out = team / "outputs/2026-09-21"
    out.mkdir(parents=True)
    (out / "plan.json").write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="risk contract"):
        materialize_paper_decision("2026-09-21", team)
    assert not (team / "decisions").exists()


def test_auto_paper_quarantines_unreviewed_legacy_buy(tmp_path):
    from trading_team.paper_pipeline import materialize_paper_decision
    team = tmp_path / "trading_team"
    out = team / "outputs/2026-09-21"
    out.mkdir(parents=True)
    (out / "plan.json").write_text(json.dumps({"date": "2026-09-21", "data_asof": "2026-09-18",
                                             "market_ok": True, "proposals": [buy()]}))
    decision = materialize_paper_decision("2026-09-21", team)
    for item in decision["portfolios"].values():
        assert item["pm_status"] == "pending"
        assert item["plans"][0]["approval"] == "pending"
        assert "approved_plan_hash" not in item["plans"][0]
        assert not item["plans"][0]["executable"]
        assert "risk contract" in item["plans"][0]["blocked_reason"]


def test_source_risk_fields_cannot_self_authorize_model_proposal():
    from trading_team.kimi_daily import _normalize_proposals
    with pytest.raises(ValueError, match="risk contract"):
        _normalize_proposals([buy(risk_review={"decision": "通过"})], {"600036"})


def test_missing_risk_cannot_be_bypassed_by_forged_auto_approval(tmp_path):
    from trading_team.paper_pipeline import materialize_paper_decision, run_paper_day
    team = tmp_path / "trading_team"
    output = team / "outputs/2026-09-21"
    output.mkdir(parents=True)
    (output / "plan.json").write_text(json.dumps({"date": "2026-09-21", "data_asof": "2026-09-18",
                                                "market_ok": True, "proposals": [buy()]}))
    decision = materialize_paper_decision("2026-09-21", team)
    for item in decision["portfolios"].values():
        item["plans"][0].update(approval="approved", approval_source="paper_auto",
                                approved_plan_hash=decision["plan_hash"], executable=True)
    (team / "decisions/2026-09-21/paper_decision.json").write_text(json.dumps(decision))
    with pytest.raises(ValueError, match="approval or execution policy"):
        run_paper_day("2026-09-21", team)
    assert not list((tmp_path / "paper").glob("*.db"))


def test_quarantined_plan_has_version_binding_without_approval(tmp_path, execution_cache, finalize_calendar, monkeypatch, stub_research_reviews):
    from trading_team.paper_pipeline import run_paper_day
    from trading_team.paper_reconcile import reconcile_accounts
    team = tmp_path / "trading_team"
    output = team / "outputs/2026-09-21"
    output.mkdir(parents=True)
    (output / "plan.json").write_text(json.dumps({"date": "2026-09-21", "data_asof": "2026-09-18",
                                                "market_ok": True, "proposals": [buy()]}))
    monkeypatch.setattr("trading_team.execution_bars.fetch_snapshot_tx", lambda *_: pytest.fail("network"))
    execution_cache(team, "2026-09-21", {code: {
        "open": 40, "high": 41, "low": 39, "close": 40, "prev_close": 40} for code in ("600036", "000300")})
    run_paper_day("2026-09-21", team)
    audit = reconcile_accounts(team=team, calendar_path=finalize_calendar)
    for portfolio in audit["portfolios"].values():
        assert portfolio["conflict_count"] == 0
        assert portfolio["days"]["2026-09-21"]["plan_binding"] == "matched"
        assert "risk_review_evidence" in {i["code"] for i in portfolio["issues"]}


def test_legacy_counterfactual_cannot_claim_auto_paper_risk_evidence():
    from paper.execution_model import Bar
    from trading_team.counterfactual import build_counterfactual, render_markdown
    report = build_counterfactual({"date": "2026-09-21", "data_asof": "2026-09-18",
                                   "market_ok": True, "proposals": [buy()]},
                                  {"600036": Bar(open=40, high=41, low=39, close=40, prev_close=40)})
    assert report["risk_review"]["status"] == "legacy_missing"
    assert report["risk_scope"] == "unreviewed_hypothesis_no_auto_paper"
    assert "不能据此批准自动 Paper" in render_markdown(report)
