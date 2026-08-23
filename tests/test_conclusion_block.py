# -*- coding: utf-8 -*-
"""trading_team/conclusion_block.py：结构化结论块提取与 accuracy 回填"""
import json

import pytest

from trading_team.conclusion_block import (
    backfill_accuracy,
    enrich_plan,
    extract_block,
    news_temperature,
)

VALID_BLOCK = """### 研究团队每日结论
- 综合评级：平安看多……

```json
{
  "role": "researcher",
  "date": "2026-08-23",
  "market": {"view": "平衡", "confidence": 3},
  "ratings": [
    {"code": "601318", "name": "中国平安", "rating": "看多", "confidence": 4,
     "one_line": "中报超预期"},
    {"code": "600519", "name": "贵州茅台", "rating": "看空", "confidence": 3,
     "one_line": "Q2失速"}
  ]
}
```
"""

NEWS_BLOCK = """### 新闻团队每日结论
```json
{
  "role": "news", "date": "2026-08-23",
  "items": [
    {"title": "美联储放鸽", "scope": "宏观", "code": null,
     "sentiment": "Positive", "score": 0.6, "horizon": "本周"},
    {"title": "地缘升温", "scope": "宏观", "code": null,
     "sentiment": "Negative", "score": -0.4, "horizon": "当日"}
  ],
  "temperature": 0.1,
  "ratings": [{"code": "601318", "name": "中国平安", "rating": "中性偏多",
               "confidence": 3, "one_line": "事件面偏多"}]
}
```
"""


class TestExtract:
    def test_valid_researcher_block(self):
        d = extract_block(VALID_BLOCK, "researcher")
        assert d is not None and len(d["ratings"]) == 2
        assert d["market"]["view"] == "平衡"

    def test_news_block_with_temperature(self):
        d = extract_block(NEWS_BLOCK, "news")
        assert d is not None and d["temperature"] == 0.1
        assert len(d["items"]) == 2

    def test_role_mismatch_rejected(self):
        assert extract_block(VALID_BLOCK, "technical") is None

    def test_invalid_rating_rejected(self):
        bad = VALID_BLOCK.replace('"rating": "看多"', '"rating": "强烈推荐"')
        assert extract_block(bad, "researcher") is None

    def test_invalid_confidence_rejected(self):
        bad = VALID_BLOCK.replace('"confidence": 4', '"confidence": 9')
        assert extract_block(bad, "researcher") is None

    def test_broken_json_rejected(self):
        assert extract_block("```json\n{not json}\n```", "researcher") is None

    def test_missing_block(self):
        assert extract_block("纯文本报告，没有结论块", "researcher") is None

    def test_takes_last_block(self):
        text = "```json\n{\"role\":\"researcher\",\"date\":\"x\",\"ratings\":[]}\n```\n" + VALID_BLOCK
        d = extract_block(text, "researcher")
        assert d is not None and d["date"] == "2026-08-23"

    def test_news_bad_temperature_rejected(self):
        bad = NEWS_BLOCK.replace('"temperature": 0.1', '"temperature": 5')
        assert extract_block(bad, "news") is None


class TestBackfill:
    def _setup(self, tmp_path):
        (tmp_path / "outputs" / "2026-08-23").mkdir(parents=True)
        (tmp_path / "outputs" / "2026-08-23" / "05_researcher.md").write_text(
            VALID_BLOCK, encoding="utf-8")
        (tmp_path / "context" / "2026-08-23").mkdir(parents=True)
        (tmp_path / "context" / "2026-08-23" / "technical_601318.json").write_text(
            json.dumps({"close": 53.35}), encoding="utf-8")
        (tmp_path / "loops").mkdir()
        (tmp_path / "loops" / "accuracy.json").write_text(
            json.dumps({"entries": []}), encoding="utf-8")
        return tmp_path

    def test_backfill_creates_entry(self, tmp_path):
        team = self._setup(tmp_path)
        r = backfill_accuracy("2026-08-23", team_dir=team)
        assert r["updated"] and r["ratings"] == 2
        acc = json.loads((team / "loops" / "accuracy.json").read_text())
        e = acc["entries"][0]
        assert e["ratings"]["601318"]["rating"] == "看多"
        assert e["ratings"]["601318"]["ref_price"] == 53.35
        assert e["ratings"]["600519"]["ref_price"] is None  # 无 context → None

    def test_backfill_idempotent_verified_kept(self, tmp_path):
        team = self._setup(tmp_path)
        acc_path = team / "loops" / "accuracy.json"
        acc = json.loads(acc_path.read_text())
        acc["entries"].append({"date": "2026-08-23", "verified": True,
                               "ratings": {"601318": {"rating": "看空"}}})
        acc_path.write_text(json.dumps(acc), encoding="utf-8")
        r = backfill_accuracy("2026-08-23", team_dir=team)
        assert not r["updated"]
        acc = json.loads(acc_path.read_text())
        assert acc["entries"][0]["ratings"]["601318"]["rating"] == "看空"  # 未被覆盖

    def test_backfill_missing_block_fails_closed(self, tmp_path):
        (tmp_path / "outputs" / "2026-08-23").mkdir(parents=True)
        (tmp_path / "outputs" / "2026-08-23" / "05_researcher.md").write_text(
            "没有结论块的报告", encoding="utf-8")
        (tmp_path / "loops").mkdir()
        (tmp_path / "loops" / "accuracy.json").write_text(
            json.dumps({"entries": []}), encoding="utf-8")
        r = backfill_accuracy("2026-08-23", team_dir=tmp_path)
        assert not r["updated"] and "结论块" in r["reason"]


SENTIMENT_BLOCK = """### 情绪团队每日结论
```json
{
  "role": "sentiment", "date": "2026-08-23",
  "market": "冰点期",
  "ratings": [{"code": "601318", "name": "中国平安", "rating": "中性",
               "confidence": 2, "one_line": "情绪中性"}]
}
```
"""

NEWS_NO_TEMP_BLOCK = """```json
{"role": "news", "date": "2026-08-23",
 "items": [{"title": "x", "sentiment": "Neutral", "score": 0}],
 "ratings": []}
```
"""


class TestEnrichPlan:
    def _setup(self, tmp_path, news=NEWS_BLOCK, sentiment=SENTIMENT_BLOCK):
        out = tmp_path / "outputs" / "2026-08-23"
        out.mkdir(parents=True)
        if news is not None:
            (out / "03_news.md").write_text(news, encoding="utf-8")
        if sentiment is not None:
            (out / "02_sentiment.md").write_text(sentiment, encoding="utf-8")
        (out / "plan.json").write_text(json.dumps(
            {"date": "2026-08-23", "data_asof": "2026-08-23",
             "proposals": [{"code": "601318"}]},
            ensure_ascii=False), encoding="utf-8")
        return tmp_path

    def test_news_temperature_extracted(self, tmp_path):
        team = self._setup(tmp_path)
        assert news_temperature("2026-08-23", team_dir=team) == 0.1

    def test_news_temperature_missing_report(self, tmp_path):
        team = self._setup(tmp_path, news=None)
        assert news_temperature("2026-08-23", team_dir=team) is None

    def test_enrich_writes_both_fields(self, tmp_path):
        team = self._setup(tmp_path)
        r = enrich_plan("2026-08-23", team_dir=team)
        assert r["updated"] and r["news_temperature"] == 0.1
        assert r["sentiment_cycle"] == "冰点期"
        plan = json.loads((team / "outputs" / "2026-08-23" / "plan.json").read_text())
        assert plan["news_temperature"] == 0.1 and plan["sentiment_cycle"] == "冰点期"
        assert plan["proposals"] == [{"code": "601318"}]  # 原字段不动

    def test_enrich_idempotent(self, tmp_path):
        team = self._setup(tmp_path)
        enrich_plan("2026-08-23", team_dir=team)
        first = (team / "outputs" / "2026-08-23" / "plan.json").read_text()
        enrich_plan("2026-08-23", team_dir=team)
        assert (team / "outputs" / "2026-08-23" / "plan.json").read_text() == first

    def test_enrich_partial_news_only(self, tmp_path):
        team = self._setup(tmp_path, sentiment=None)
        r = enrich_plan("2026-08-23", team_dir=team)
        assert r["updated"]
        plan = json.loads((team / "outputs" / "2026-08-23" / "plan.json").read_text())
        assert plan["news_temperature"] == 0.1 and "sentiment_cycle" not in plan

    def test_enrich_fail_closed_no_blocks(self, tmp_path):
        team = self._setup(tmp_path, news=NEWS_NO_TEMP_BLOCK, sentiment=None)
        # news 块不合法（缺 temperature）且情绪报告不存在 → 不改 plan.json
        before = (team / "outputs" / "2026-08-23" / "plan.json").read_text()
        r = enrich_plan("2026-08-23", team_dir=team)
        assert not r["updated"]
        assert (team / "outputs" / "2026-08-23" / "plan.json").read_text() == before

    def test_enrich_missing_plan(self, tmp_path):
        team = self._setup(tmp_path)
        (team / "outputs" / "2026-08-23" / "plan.json").unlink()
        r = enrich_plan("2026-08-23", team_dir=team)
        assert not r["updated"] and "plan.json" in r["reason"]
