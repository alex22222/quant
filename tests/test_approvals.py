# -*- coding: utf-8 -*-
"""approvals.py：审批状态流转 + 回写主事实源"""
import json

import pytest

from trading_team import approvals


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离的 TEAM 目录环境：decisions/ + loops/approvals.json。"""
    team = tmp_path / "trading_team"
    (team / "loops").mkdir(parents=True)
    (team / "outputs").mkdir()
    dec_dir = team / "decisions" / "2026-08-23"
    dec_dir.mkdir(parents=True)
    decision = {
        "date": "2026-08-23", "market_ok": False, "pm_status": "pending",
        "plans": [
            {"code": "601318", "name": "中国平安", "direction": "买入",
             "position_pct": 0.15, "entry": [53.5], "stop": 50.6},
            {"code": "600036", "name": "招商银行", "direction": "观望",
             "position_pct": 0, "entry": None, "stop": None},
        ],
    }
    (dec_dir / "decision.json").write_text(
        json.dumps(decision, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(approvals, "TEAM", team)
    monkeypatch.setattr(approvals, "FILE", team / "loops" / "approvals.json")
    monkeypatch.setattr(approvals, "DECISIONS_DIR", team / "decisions")
    monkeypatch.setattr(approvals, "log_to_agent_md", lambda e: None)  # 不动 AGENT.md
    return team


def _read_decision(team):
    return json.loads((team / "decisions" / "2026-08-23" / "decision.json")
                      .read_text(encoding="utf-8"))


class TestWriteBack:
    def test_approval_synced_to_master(self, env):
        approvals.record("2026-08-23", "600036", "招商银行", "approved")
        dec = _read_decision(env)
        plan = [p for p in dec["plans"] if p["code"] == "600036"][0]
        assert plan["approval"] == "approved"
        assert plan["approved_at"]

    def test_pm_status_partial_then_approved(self, env):
        approvals.record("2026-08-23", "601318", "中国平安", "approved")
        assert _read_decision(env)["pm_status"] == "partial"
        approvals.record("2026-08-23", "600036", "招商银行", "rejected")
        assert _read_decision(env)["pm_status"] == "approved"

    def test_same_day_overwrite(self, env):
        approvals.record("2026-08-23", "601318", "中国平安", "approved")
        approvals.record("2026-08-23", "601318", "中国平安", "rejected", note="太贵")
        data = approvals.load()
        entries = [d for d in data["decisions"]
                   if d["date"] == "2026-08-23" and d["code"] == "601318"]
        assert len(entries) == 1 and entries[0]["decision"] == "rejected"
        plan = [p for p in _read_decision(env)["plans"] if p["code"] == "601318"][0]
        assert plan["approval"] == "rejected"

    def test_market_not_ok_blocks_buy_executable(self, env):
        """market_ok=false 时买入计划批复 approved 也不得 executable"""
        entry = approvals.record("2026-08-23", "601318", "中国平安", "approved")
        plan = [p for p in _read_decision(env)["plans"] if p["code"] == "601318"][0]
        assert plan["approval"] == "approved"
        assert plan["executable"] is False
        assert "market_ok" in plan["blocked_reason"]
        assert entry["master_synced"] is True

    def test_zero_pct_plan_executable_when_approved(self, env):
        approvals.record("2026-08-23", "600036", "招商银行", "approved")
        plan = [p for p in _read_decision(env)["plans"] if p["code"] == "600036"][0]
        assert plan["executable"] is True  # 观望 0%：无买入动作，不受 market_ok 限制

    def test_missing_master_marks_unsynced(self, env):
        entry = approvals.record("2026-08-23", "999999", "不存在", "approved")
        assert entry["master_synced"] is False

    def test_invalid_decision_rejected(self, env):
        with pytest.raises(ValueError):
            approvals.record("2026-08-23", "601318", "中国平安", "maybe")
