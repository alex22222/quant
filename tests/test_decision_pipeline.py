# -*- coding: utf-8 -*-
"""plan -> decision 版本绑定。"""
import json

import pytest

from trading_team.decision_pipeline import canonical_plan_hash, materialize_decision


def _plan_doc(entry=53.5):
    return {
        "date": "2026-08-24",
        "data_asof": "2026-08-21",
        "market_ok": True,
        "proposals": [{
            "code": "601318", "name": "中国平安", "direction": "条件买入",
            "position_pct": 0.15, "entry": [entry], "entry_type": "limit",
            "stop": 50.6,
        }],
    }


def test_materialize_binds_hash_and_preserves_same_version_approval(tmp_path):
    team = tmp_path / "trading_team"
    out = team / "outputs" / "2026-08-24"
    out.mkdir(parents=True)
    doc = _plan_doc()
    (out / "plan.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    dec = materialize_decision("2026-08-24", team=team)
    assert dec["plan_hash"] == canonical_plan_hash(doc)
    assert dec["plans"][0]["approval"] == "pending"

    dec["plans"][0].update({
        "approval": "approved", "approved_plan_hash": dec["plan_hash"],
        "approved_at": "2026-08-24T08:00:00", "executable": True,
    })
    path = team / "decisions" / "2026-08-24" / "decision.json"
    path.write_text(json.dumps(dec, ensure_ascii=False), encoding="utf-8")
    rebuilt = materialize_decision("2026-08-24", team=team)
    assert rebuilt["plans"][0]["approval"] == "approved"


def test_materialize_preserves_all_rejected_summary(tmp_path):
    team = tmp_path / "trading_team"
    out = team / "outputs" / "2026-08-24"
    out.mkdir(parents=True)
    (out / "plan.json").write_text(json.dumps(_plan_doc(), ensure_ascii=False))
    decision = materialize_decision("2026-08-24", team=team)
    decision["plans"][0].update(approval="rejected", approved_plan_hash=decision["plan_hash"])
    (team / "decisions" / "2026-08-24" / "decision.json").write_text(json.dumps(decision))

    rebuilt = materialize_decision("2026-08-24", team=team)
    assert rebuilt["pm_status"] == "rejected"
    assert rebuilt["plans"][0]["approval"] == "rejected"


@pytest.mark.parametrize("change", ["price", "condition"])
def test_changed_plan_invalidates_old_approval(tmp_path, change):
    team = tmp_path / "trading_team"
    out = team / "outputs" / "2026-08-24"
    out.mkdir(parents=True)
    plan_path = out / "plan.json"
    plan_path.write_text(json.dumps(_plan_doc(), ensure_ascii=False), encoding="utf-8")
    dec = materialize_decision("2026-08-24", team=team)
    dec["plans"][0].update({
        "approval": "approved", "approved_plan_hash": dec["plan_hash"],
        "approved_at": "2026-08-24T08:00:00", "executable": True,
    })
    decision_path = team / "decisions" / "2026-08-24" / "decision.json"
    decision_path.write_text(json.dumps(dec, ensure_ascii=False), encoding="utf-8")

    changed = _plan_doc(entry=52.2) if change == "price" else _plan_doc()
    if change == "condition":
        changed["proposals"][0]["execution_conditions"] = [
            {"field": "open_gap_pct", "op": "lte", "value": 0.03},
        ]
    plan_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    rebuilt = materialize_decision("2026-08-24", team=team)
    assert rebuilt["plan_hash"] != dec["plan_hash"]
    assert rebuilt["plans"][0]["approval"] == "pending"
    assert rebuilt["plans"][0]["executable"] is False
    assert "approved_plan_hash" not in rebuilt["plans"][0]


@pytest.mark.parametrize("field,value", [("price", 54), ("qty", 200), ("type", "stop")])
def test_exit_change_invalidates_approval(tmp_path, field, value):
    team = tmp_path / "trading_team"
    out = team / "outputs" / "2026-08-24"
    out.mkdir(parents=True)
    plan = _plan_doc()
    plan["proposals"] = [{"code": "601318", "direction": "减持", "position_pct": 0,
                          "exit": {"type": "limit", "qty": 100, "price": 53.5, "time_in_force": "DAY"}}]
    (out / "plan.json").write_text(json.dumps(plan))
    decision = materialize_decision("2026-08-24", team=team)
    decision["plans"][0].update(approval="approved", approved_plan_hash=decision["plan_hash"], executable=True)
    (team / "decisions/2026-08-24/decision.json").write_text(json.dumps(decision))
    assert materialize_decision("2026-08-24", team=team)["plans"][0]["executable"] is True
    plan["proposals"][0]["exit"][field] = value
    (out / "plan.json").write_text(json.dumps(plan))
    rebuilt = materialize_decision("2026-08-24", team=team)
    assert rebuilt["plan_hash"] != decision["plan_hash"]
    assert rebuilt["plans"][0]["approval"] == "pending"
    assert rebuilt["plans"][0]["executable"] is False
