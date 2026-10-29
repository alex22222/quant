# -*- coding: utf-8 -*-
"""pipeline/stage_strategy.py：策略注册表与磁盘代码保持一致。"""

import pipeline.stage_strategy as ss


def test_deleted_strategy_is_removed_from_registry(tmp_path, monkeypatch):
    strat_dir = tmp_path / "strategies"
    strat_dir.mkdir()
    (strat_dir / "keep.py").write_text('"""keep"""\n', encoding="utf-8")
    status = {
        "strategy_registry": {
            "keep": {"state": "candidate", "file": "strategies/keep.py"},
            "gone": {"state": "retired", "file": "strategies/gone.py"},
        },
        "stages": {
            "backtest": {"result": {"results": {"keep": {}, "gone": {}}}},
        },
    }

    monkeypatch.setattr(ss, "STRAT_DIR", strat_dir)
    monkeypatch.setattr(ss, "load_status", lambda: status)
    monkeypatch.setattr(ss, "save_status", lambda value: None)

    result = ss.main()

    assert list(status["strategy_registry"]) == ["keep"]
    assert list(status["stages"]["backtest"]["result"]["results"]) == ["keep"]
    assert result["total"] == 1
