# -*- coding: utf-8 -*-
"""consistency_check.py：多源冲突检出与 fail closed"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from trading_team import consistency_check as cc
from trading_team.decision_pipeline import canonical_plan_hash


@pytest.fixture
def env(tmp_path, monkeypatch):
    team = tmp_path / "trading_team"
    (team / "loops").mkdir(parents=True)
    (team / "outputs" / "2026-08-23").mkdir(parents=True)
    (team / "decisions" / "2026-08-23").mkdir(parents=True)
    monkeypatch.setattr(cc, "TEAM", team)
    monkeypatch.setattr(cc, "DECISIONS_DIR", team / "decisions")
    monkeypatch.setattr(cc, "OUTPUTS_DIR", team / "outputs")
    monkeypatch.setattr(cc, "APPROVALS_FILE", team / "loops" / "approvals.json")
    monkeypatch.setattr(cc, "TEAM_DB", team / "team.db")

    plan_doc = {"date": "2026-08-23", "data_asof": "2026-08-22",
                "market_ok": True, "proposals": [
        {"code": "601318", "name": "中国平安", "direction": "买入",
         "position_pct": 0.15, "entry": [53.5], "entry_type": "limit", "stop": 50.6}]}
    plan_hash = canonical_plan_hash(plan_doc)
    decision = {
        "date": "2026-08-23", "market_ok": True, "pm_status": "approved",
        "plan_hash": plan_hash,
        "plans": [
            {"code": "601318", "name": "中国平安", "direction": "买入",
             "position_pct": 0.15, "entry": [53.5], "entry_type": "limit", "stop": 50.6,
             "approval": "approved", "approved_plan_hash": plan_hash,
             "executable": True},
        ],
    }
    (team / "decisions" / "2026-08-23" / "decision.json").write_text(
        json.dumps(decision, ensure_ascii=False), encoding="utf-8")
    (team / "outputs" / "2026-08-23" / "plan.json").write_text(
        json.dumps(plan_doc, ensure_ascii=False), encoding="utf-8")
    return team


def _write(team, rel, obj):
    p = team / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _make_team_db(team, pm_status="approved"):
    conn = sqlite3.connect(team / "team.db")
    conn.execute("CREATE TABLE decisions (date TEXT PRIMARY KEY, pm_status TEXT)")
    conn.execute("INSERT INTO decisions VALUES ('2026-08-23', ?)", (pm_status,))
    conn.commit()
    conn.close()


def test_direct_script_entrypoint_loads_shared_contract_without_pythonpath(tmp_path):
    script = Path(__file__).parents[1] / "trading_team/consistency_check.py"
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, str(script), "2099-01-01"], cwd=tmp_path,
                            env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 1
    assert "主事实源缺失" in result.stdout
    assert "Traceback" not in result.stderr


class TestConsistency:
    def test_executable_cannot_bypass_unmodeled_condition(self, env):
        path = env / "decisions/2026-08-23/decision.json"
        dec = json.loads(path.read_text())
        dec["plans"][0]["condition"] = "Only after news confirmation"
        _write(env, "decisions/2026-08-23/decision.json", dec)
        result = cc.check_day("2026-08-23")
        assert not result["ok"]
        assert any("entry contract" in conflict for conflict in result["conflicts"])

    def test_clean_pass(self, env):
        _make_team_db(env)
        _write(env, "loops/approvals.json", {"decisions": [
            {"date": "2026-08-23", "code": "601318", "decision": "approved",
             "plan_hash": canonical_plan_hash(json.loads((env / "outputs/2026-08-23/plan.json").read_text()))}]})
        r = cc.check_day("2026-08-23")
        assert r["ok"], r["conflicts"]

    def test_missing_master_fails(self, env):
        (env / "decisions" / "2026-08-23" / "decision.json").unlink()
        r = cc.check_day("2026-08-23")
        assert not r["ok"] and any("主事实源缺失" in c for c in r["conflicts"])

    def test_unsynced_approval_fails(self, env):
        _make_team_db(env)
        # 主事实源里 approval 仍是 pending，approvals.json 却 approved
        dec = json.loads((env / "decisions" / "2026-08-23" / "decision.json")
                         .read_text(encoding="utf-8"))
        dec["plans"][0].pop("approval")
        dec["plans"][0].pop("executable")
        _write(env, "decisions/2026-08-23/decision.json", dec)
        _write(env, "loops/approvals.json", {"decisions": [
            {"date": "2026-08-23", "code": "601318", "decision": "approved",
             "plan_hash": dec["plan_hash"]}]})
        r = cc.check_day("2026-08-23")
        assert not r["ok"]
        assert any("审批未回写主事实源" in c for c in r["conflicts"])

    def test_market_not_ok_executable_fails(self, env):
        _make_team_db(env)
        dec = json.loads((env / "decisions" / "2026-08-23" / "decision.json")
                         .read_text(encoding="utf-8"))
        dec["market_ok"] = False  # 大盘破位，但买入计划仍 executable
        _write(env, "decisions/2026-08-23/decision.json", dec)
        r = cc.check_day("2026-08-23")
        assert not r["ok"]
        assert any("market_ok=false" in c for c in r["conflicts"])

    def test_draft_direction_conflict_fails(self, env):
        _make_team_db(env)
        _write(env, "outputs/2026-08-23/plan.json", {"proposals": [
            {"code": "601318", "direction": "观望", "position_pct": 0}]})
        r = cc.check_day("2026-08-23")
        assert not r["ok"]
        assert any("买卖性质冲突" in c for c in r["conflicts"])

    def test_team_db_mismatch_fails(self, env):
        _make_team_db(env, pm_status="pending")
        r = cc.check_day("2026-08-23")
        assert not r["ok"]
        assert any("team.db" in c for c in r["conflicts"])

    def test_changed_plan_hash_fails(self, env):
        _make_team_db(env)
        draft = json.loads((env / "outputs/2026-08-23/plan.json").read_text())
        draft["proposals"][0]["entry"] = [52.2]
        _write(env, "outputs/2026-08-23/plan.json", draft)
        r = cc.check_day("2026-08-23")
        assert not r["ok"]
        assert any("plan_hash" in c for c in r["conflicts"])

    def test_string_pct_draft_is_warning_not_conflict(self, env):
        _make_team_db(env)
        draft = {"proposals": [
            {"code": "601318", "direction": "买入",
             "position_pct": "15%（8%+7% 分批）"}]}
        _write(env, "outputs/2026-08-23/plan.json", draft)
        dec = json.loads((env / "decisions/2026-08-23/decision.json").read_text())
        dec["plan_hash"] = canonical_plan_hash(draft)
        dec["plans"][0]["approved_plan_hash"] = dec["plan_hash"]
        _write(env, "decisions/2026-08-23/decision.json", dec)
        r = cc.check_day("2026-08-23")
        assert any("position_pct 含非数字内容" in w for w in r["warnings"])
        assert r["ok"]  # 买卖性质一致，仅 schema 警告
