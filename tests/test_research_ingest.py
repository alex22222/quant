# -*- coding: utf-8 -*-
"""pipeline/research_ingest.py：研究线索分类与策略草案生成（建议 4）"""
import json
import py_compile

import pytest

from pipeline.research_ingest import classify_item, ingest, make_draft, pick_template


def _item(title, summary="", url="https://x.com/a/status/1", author="某博主"):
    return {"platform": "x", "author": author, "title": title,
            "url": url, "publish_time": "1天前", "summary": summary}


class TestClassify:
    def test_trading_signal(self):
        assert classify_item(_item("动量轮动策略回测分享")) == "trading_signal"
        assert classify_item(_item("RSI 超卖反弹信号")) == "trading_signal"

    def test_risk_management(self):
        assert classify_item(_item("谈谈回撤控制与仓位管理")) == "risk_management"

    def test_noise_ignored(self):
        assert classify_item(_item("RT @xxx: 转发抽奖")) is None
        assert classify_item(_item("今晚八点直播")) is None

    def test_unrelated_ignored(self):
        assert classify_item(_item("今天天气不错")) is None
        assert classify_item(_item("")) is None

    def test_template_pick(self):
        assert pick_template(_item("RSI 超卖反弹"))[0] == "mean_reversion"
        assert pick_template(_item("通道突破进场"))[0] == "breakout"
        assert pick_template(_item("均线金叉死叉"))[0] == "ma_cross"
        assert pick_template(_item("动量因子"))[0] == "momentum"
        assert pick_template(_item("量化策略"))[0] == "momentum"  # 兜底


@pytest.fixture
def dirs(tmp_path):
    reports = tmp_path / "reports"
    strat = tmp_path / "strategies"
    ledger = tmp_path / "ledger.jsonl"
    reports.mkdir()
    strat.mkdir()
    (reports / "2026-08-21.json").write_text(json.dumps([
        _item("动量轮动策略回测分享", url="https://x.com/a/1"),
        _item("RSI 超卖反弹机会", url="https://x.com/a/2"),
        _item("谈谈回撤控制", url="https://x.com/a/3"),
        _item("今天天气不错", url="https://x.com/a/4"),
    ], ensure_ascii=False), encoding="utf-8")
    return reports, strat, ledger


class TestIngest:
    def test_end_to_end(self, dirs):
        reports, strat, ledger = dirs
        out = ingest(date="2026-08-21", reports_dir=reports,
                     strat_dir=strat, ledger_path=ledger)
        assert out["processed"] == 4
        assert out["classified"] == {"trading_signal": 2, "risk_management": 1,
                                     "ignored": 1}
        assert len(out["drafts"]) == 2
        for name in out["drafts"]:
            assert (strat / f"{name}.py").exists()
        assert ledger.exists()

    def test_idempotent_second_run(self, dirs):
        reports, strat, ledger = dirs
        ingest(date="2026-08-21", reports_dir=reports, strat_dir=strat,
               ledger_path=ledger)
        out = ingest(date="2026-08-21", reports_dir=reports, strat_dir=strat,
                     ledger_path=ledger)
        assert out["processed"] == 0 and out["drafts"] == []

    def test_dry_run_writes_nothing(self, dirs):
        reports, strat, ledger = dirs
        out = ingest(date="2026-08-21", reports_dir=reports, strat_dir=strat,
                     ledger_path=ledger, dry_run=True)
        assert out["classified"]["trading_signal"] == 2
        assert out["drafts"] == [] and not ledger.exists()
        assert list(strat.glob("*.py")) == []

    def test_missing_date_fail_closed(self, dirs):
        reports, strat, ledger = dirs
        out = ingest(date="2099-01-01", reports_dir=reports, strat_dir=strat,
                     ledger_path=ledger)
        assert out["processed"] == 0 and "不存在" in out["reason"]

    def test_draft_is_valid_python_with_interface(self, dirs):
        reports, strat, ledger = dirs
        out = ingest(date="2026-08-21", reports_dir=reports, strat_dir=strat,
                     ledger_path=ledger)
        for name in out["drafts"]:
            f = strat / f"{name}.py"
            py_compile.compile(str(f), doraise=True)  # 语法必须有效
            text = f.read_text(encoding="utf-8")
            assert "FAMILY" in text and "PARAMS" in text
            assert "def generate_targets" in text
            assert "https://x.com/a/" in text  # 来源留痕

    def test_max_drafts_cap(self, dirs):
        reports, strat, ledger = dirs
        (reports / "2026-08-22.json").write_text(json.dumps([
            _item(f"动量策略分享 {i}", url=f"https://x.com/b/{i}")
            for i in range(10)
        ], ensure_ascii=False), encoding="utf-8")
        out = ingest(date="2026-08-22", reports_dir=reports, strat_dir=strat,
                     ledger_path=ledger)
        assert len(out["drafts"]) == 5  # MAX_DRAFTS_PER_RUN 封顶
        assert out["classified"]["trading_signal"] == 10  # 分类仍全量记账
