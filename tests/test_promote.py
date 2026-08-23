# -*- coding: utf-8 -*-
"""pipeline/promote.py：晋级门禁（live 证据 + candidate 因子区分度，建议 6）"""
import pytest

import pipeline.promote as pm


@pytest.fixture
def fake_status(monkeypatch):
    """内存版 status.json：绕过文件系统与策略代码 hash。"""
    st = {
        "strategy_registry": {
            "mom_live": {"state": "live", "file": "strategies/mom_live.py",
                         "params": {}},
            "new_strat": {"state": "research", "file": "strategies/new_strat.py",
                          "params": {}},
        },
        "stages": {},
    }
    monkeypatch.setattr(pm, "load_status", lambda: st)
    monkeypatch.setattr(pm, "save_status", lambda s: None)
    monkeypatch.setattr(pm, "_evidence", lambda name, st: {
        "strategy_file": f"strategies/{name}.py", "strategy_hash": "h",
        "params_hash": "p", "gate_version": "v2", "gate_hash": "g",
        "data_version": "d", "report": f"reports/{name}"})
    return st


class TestDifferentiationGate:
    def test_candidate_without_differentiation_rejected(self, fake_status):
        with pytest.raises(SystemExit, match="因子区分度"):
            pm.promote("new_strat", "candidate", "回测达标", "老板")
        # fail closed：状态未被改动
        assert fake_status["strategy_registry"]["new_strat"]["state"] == "research"

    def test_candidate_with_differentiation_ok(self, fake_status):
        out = pm.promote("new_strat", "candidate", "回测达标", "老板",
                         differentiation="RSI-2 均值回归，与动量族信号结构相反，相关性低")
        entry = fake_status["strategy_registry"]["new_strat"]
        assert entry["state"] == "candidate"
        assert "均值回归" in entry["differentiation"]
        assert out["promotion"]["to"] == "candidate"

    def test_existing_differentiation_field_suffices(self, fake_status):
        fake_status["strategy_registry"]["new_strat"]["differentiation"] = "已登记"
        pm.promote("new_strat", "candidate", "回测达标", "老板")
        assert fake_status["strategy_registry"]["new_strat"]["differentiation"] == "已登记"

    def test_blank_differentiation_rejected(self, fake_status):
        with pytest.raises(SystemExit, match="因子区分度"):
            pm.promote("new_strat", "candidate", "回测达标", "老板",
                       differentiation="   ")

    def test_live_and_retired_not_gated(self, fake_status):
        # 区分度门禁只拦 candidate；retired 不需要
        pm.promote("new_strat", "retired", "废弃", "老板")
        assert fake_status["strategy_registry"]["new_strat"]["state"] == "retired"

    def test_error_message_lists_incumbents(self, fake_status):
        with pytest.raises(SystemExit, match="mom_live"):
            pm.promote("new_strat", "candidate", "回测达标", "老板")
