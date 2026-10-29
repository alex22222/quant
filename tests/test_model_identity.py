import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_daily_research_recovery import DAY, ROLE_NAMES, meeting
from trading_team.kimi_daily import KimiClient, inspect_generation


def test_fundamental_charter_requires_archived_support_for_causality_and_valuation():
    charter = (Path(__file__).resolve().parents[1] / "trading_team/prompts/01_fundamental.md").read_text()
    assert "因果解释必须有归档输入依据" in charter
    assert "缺失 PE/PB 或可核验参照时，估值状态写「不可判」" in charter
    assert "公告标题不能证明公告正文未归档的业务原因" in charter


def test_fundamental_charter_requires_same_period_comparator():
    charter = (Path(__file__).resolve().parents[1] / "trading_team/prompts/01_fundamental.md").read_text()
    assert "跨年同期比较" in charter
    assert "对应报告期数值" in charter
    assert "全年同比" in charter


def test_fundamental_charter_blocks_mixed_period_trend_narratives():
    charter = (Path(__file__).resolve().parents[1] / "trading_team/prompts/01_fundamental.md").read_text()
    assert "全年同比与半年/季度同比只能分别陈述" in charter
    assert "不得据此写低基数反弹、恢复或趋势延续" in charter
    assert "角色输入仅提供最新报告期的财务数值" in charter


def test_sentiment_charter_does_not_infer_participant_behavior_from_volume():
    charter = (Path(__file__).resolve().parents[1] / "trading_team/prompts/02_sentiment.md").read_text()
    assert "不得据量比、换手或涨跌推断抛压、承接或交易者意愿" in charter
    assert "正文和 JSON one_line" in charter


def test_news_charter_does_not_turn_snapshot_into_historical_high():
    charter = (Path(__file__).resolve().parents[1] / "trading_team/prompts/03_news.md").read_text()
    assert "单次报价和前结算价不能证明创历史新高或持续趋势" in charter
    assert "未认证快照" in charter


def test_news_charter_requires_company_exposure_evidence_for_stock_impacts():
    charter = (Path(__file__).resolve().parents[1] / "trading_team/prompts/03_news.md").read_text()
    assert "行业新闻不能单凭观察池行业标签推导个股受益或受损" in charter
    assert "对美出口占比、库卡业务映射、财富管理中收影响" in charter
    assert "正文及 JSON items/ratings" in charter
    assert "缺少公司级归档证据时只写行业层影响" in charter


def test_technical_charter_distinguishes_60_day_return_from_ma60_distance():
    charter = (Path(__file__).resolve().parents[1] / "trading_team/prompts/04_technical.md").read_text()
    assert "ret_60d 是近 60 日收益率，不是收盘价相对 MA60 的偏离幅度" in charter
    assert "未提供相对 MA60 的百分比时，只写线上/线下关系" in charter


def test_technical_charter_requires_evidence_for_exclusive_peer_claims():
    charter = (Path(__file__).resolve().parents[1] / "trading_team/prompts/04_technical.md").read_text()
    assert "唯一、最强、仅有等排他性同业比较，须逐只核对同一指标与时点" in charter


def test_researcher_charter_requires_counter_signal_reconciliation():
    charter = (Path(__file__).resolve().parents[1] / "trading_team/prompts/05_researcher.md").read_text()
    assert "逐项回应已列出的空方核心" in charter
    assert "不得写“四方无反向信号”" in charter


def test_fundamental_role_receives_archived_valuation_snapshot(meeting):
    runner, _, _ = meeting
    context = runner.team / "context" / DAY
    (context / "snapshot.json").write_text(json.dumps({
        "601318": {"pe_ttm": 6.19, "pb": 0.98, "total_mktcap": 986142183048},
    }))

    runner.run(DAY, collect=False, push=False, publish_only=True)

    role_input = (runner.team / "outputs" / DAY / "01_fundamental.input.txt").read_text()
    assert "===== snapshot.json =====" in role_input
    assert '"pe_ttm": 6.19' in role_input
    assert '"pb": 0.98' in role_input


def test_fundamental_role_input_identifies_missing_same_period_report(meeting):
    runner, _, _ = meeting
    context = runner.team / "context" / DAY
    (context / "fundamental_601318.json").write_text(json.dumps({
        "financials": {"reports": [
            {"report_date": "2026-06-30"},
            {"report_date": "2025-12-31"},
        ]},
    }))

    runner.run(DAY, collect=False, push=False, publish_only=True)

    role_input = (runner.team / "outputs" / DAY / "01_fundamental.input.txt").read_text()
    assert "===== 报告期覆盖索引 =====" in role_input
    assert '"archived_report_dates": [' in role_input
    assert '"2026-06-30": "2025-06-30"' in role_input


def test_fundamental_role_excludes_older_numeric_reports_without_changing_archive(meeting):
    runner, _, _ = meeting
    path = runner.team / "context" / DAY / "fundamental_601318.json"
    original = json.dumps({"financials": {"reports": [
        {"report_date": "2025-12-31", "revenue_yi": 98765},
        {"report_date": "2026-06-30", "revenue_yi": 12345},
    ]}, "announcements": [{"title": "archived announcement"}]})
    path.write_text(original)

    runner.run(DAY, collect=False, push=False, publish_only=True)

    role_input = (runner.team / "outputs" / DAY / "01_fundamental.input.txt").read_text()
    assert '"revenue_yi": 12345' in role_input
    assert '"revenue_yi": 98765' not in role_input
    assert '"2025-12-31"' in role_input
    assert "archived announcement" in role_input
    assert path.read_text() == original


@pytest.mark.parametrize("claim", [
    "- **判断**：2026H1 营收同比 +4.83%，与 2025 全年近零增长相比略有改善。",
    "- **判断**：2026H1 扣非同比 -25.31%，与 2025 全年 +15.46% 相比明显走弱。",
    "- **判断**：2026H1 毛利率较 2025 全年口径明显抬升。",
    "- **判断**：2026H1 与 Q1 营收增速均超 50%，较 2025 年报营收 +17% 明显加速。",
    "- **判断**：2026H1 净利同比下滑，与 2025 三季报相比明显走弱。",
    "- **判断**：2026H1 净利高增，延续 2025 年报的高增长。",
])
def test_fundamental_mixed_period_comparison_is_rejected_before_publication(meeting, monkeypatch, claim):
    runner, client, _ = meeting
    complete = client.complete

    def invalid(system, user):
        result = complete(system, user)
        if system.splitlines()[0] == "fundamental":
            result["report_markdown"] = claim + "\n" + result["report_markdown"]
        return result

    monkeypatch.setattr(client, "complete", invalid)
    with pytest.raises(ValueError, match="fundamental period comparison"):
        runner.run(DAY, collect=False, push=False, publish_only=True)

    run = runner.ledger.list_runs()[0]
    step = runner.ledger.latest_step(run["run_id"], "role_01")
    assert step["status"] == "failed" and step["artifact_path"]
    assert claim in json.loads((runner.root / step["artifact_path"]).read_text())["report_markdown"]
    assert not (runner.team / "outputs" / DAY).exists()
    assert client.calls["fundamental"] == 1 and sum(client.calls.values()) == 1


def test_fundamental_separate_period_facts_are_not_treated_as_comparison(meeting, monkeypatch):
    runner, client, _ = meeting
    complete = client.complete

    def valid(system, user):
        result = complete(system, user)
        if system.splitlines()[0] == "fundamental":
            result["report_markdown"] = (
                "- **事实**：2026H1 净利同比 +89%，2025 全年净利同比 -72%。\n"
                "- **判断**：2026H1 高增长；低基数反弹仅是待核验假设，不计分。\n"
                + result["report_markdown"]
            )
        return result

    monkeypatch.setattr(client, "complete", valid)
    result = runner.run(DAY, collect=False, push=False, publish_only=True)
    assert result["status"] == "succeeded" and client.calls["fundamental"] == 1


@pytest.mark.parametrize("claim", [
    "- **一句话结论**：半年报营收净利双降、盈利率偏薄，单期走弱，9 月产销数据正文未归档暂无法更新判断。",
    "- **一句话结论**：半年报亏损 36.84 亿元、毛利率仅 1.3%，基本面显著恶化信号明确，拐点无从核验。",
])
def test_fundamental_half_year_report_is_not_annual_report(claim):
    from trading_team.kimi_daily import _validate_fundamental_period_claims

    _validate_fundamental_period_claims(claim)


@pytest.mark.parametrize("claim", [
    "量比变化不能证明「资金净流入」。",
    "本报告未将量比证据升级为「资金净流入」结论。",
    "本报告未将量价共振升级为「放量推动上涨」的确定性因果。",
])
def test_quoted_negated_flow_and_causality_are_not_positive_claims(claim):
    from trading_team.kimi_daily import _validate_unverified_fund_flow

    _validate_unverified_fund_flow(claim)


@pytest.mark.parametrize("claim,error", [
    ("未将量比升级为「资金净流入」，但资金真实流入是事实。", "unverified fund flow"),
    ("未将量价升级为「放量推动上涨」，但放量推动上涨是事实。", "unverified volume causality"),
])
def test_quoted_negation_does_not_mask_later_positive_claim(claim, error):
    from trading_team.kimi_daily import _validate_unverified_fund_flow

    with pytest.raises(ValueError, match=error):
        _validate_unverified_fund_flow(claim)


def test_independent_inspection_rejects_rehashed_mixed_period_report(meeting):
    from trading_team.kimi_daily import _sha256, generation_outputs

    runner, _, _ = meeting
    run = runner.run(DAY, collect=False, push=False, publish_only=True)
    output = runner.team / "outputs" / DAY
    report = output / "01_fundamental.md"
    report.write_text(
        "- **判断**：2026H1 同比 +4%，较 2025 年报明显加速。\n" + report.read_text()
    )
    role_meta = output / "01_fundamental.run.json"
    meta = json.loads(role_meta.read_text())
    meta["output_hash"] = _sha256(report.read_text())
    role_meta.write_text(json.dumps(meta))
    receipt_path = output / "generation.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["outputs"] = generation_outputs(output)
    receipt_path.write_text(json.dumps(receipt))
    digest = _sha256(receipt_path.read_text())
    with runner.ledger._connect() as conn:
        conn.execute("UPDATE step_runs SET input_hash=?,output_hash=? WHERE run_id=? AND step_name='publish_generation'",
                     (digest, digest, run["run_id"]))

    with pytest.raises(ValueError, match="fundamental period comparison"):
        inspect_generation(runner.team, DAY, runner.ledger)


@pytest.mark.parametrize("role,claim", [
    ("sentiment", "资金明显向放量上涨的美的和比亚迪集中。"),
    ("researcher", "技术和情绪看多，资金真实流入是事实。"),
    ("researcher", "主力资金净流入2亿元。"),
    ("researcher", "资金真实净流入已被证实。"),
    ("sentiment", "不构成资金净流入证据但资金真实流入是事实。"),
    ("researcher", "本报告未将量比证据升级为资金净流入但资金真实流入是事实。"),
    ("researcher", "本报告将量比证据升级为资金净流入。"),
    ("researcher", "资金净流入不可判但资金真实流入是事实。"),
    ("researcher", "不得升级为资金净流入结论但资金真实流入是事实。"),
    ("researcher", "不断言资金净流入但资金真实流入是事实。"),
    ("researcher", "不将回购/增持升级为资金流入结论但资金真实流入是事实。"),
    ("researcher", "不得升级为已证实的资金流入，但资金真实流入是事实。"),
    ("researcher", "未升级为资金净流入/流出结论，但资金真实流入是事实。"),
    ("sentiment", "资金确实向美的集团集中。"),
    ("sentiment", "资金净流入或流出方向不可判，但资金真实流入是事实。"),
    ("sentiment", "不可判定资金净流入/流出，但资金真实流入是事实。"),
])
def test_unverified_fund_flow_claim_is_rejected_before_publication(meeting, monkeypatch, role, claim):
    runner, client, _ = meeting
    complete = client.complete

    def invalid(system, user):
        result = complete(system, user)
        if system.splitlines()[0] == role:
            result["report_markdown"] = claim + "\n" + result["report_markdown"]
        return result

    monkeypatch.setattr(client, "complete", invalid)
    with pytest.raises(ValueError, match="unverified fund flow"):
        runner.run(DAY, collect=False, push=False, publish_only=True)

    run = runner.ledger.list_runs()[0]
    number = "02" if role == "sentiment" else "05"
    step = runner.ledger.latest_step(run["run_id"], f"role_{number}")
    assert step["status"] == "failed" and step["artifact_path"]
    assert claim in json.loads((runner.root / step["artifact_path"]).read_text())["report_markdown"]
    assert not (runner.team / "outputs" / DAY).exists()


def test_unverified_fund_flow_in_structured_sentiment_is_rejected(meeting, monkeypatch):
    runner, client, _ = meeting
    complete = client.complete

    def invalid(system, user):
        result = complete(system, user)
        if system.splitlines()[0] == "sentiment":
            report = result["report_markdown"]
            block = json.loads(report.split("```json\n", 1)[1].split("\n```", 1)[0])
            block["ratings"][0]["one_line"] = "量比1.43全池最高，增量资金介入"
            result["report_markdown"] = report.split("```json\n", 1)[0] + "```json\n" + json.dumps(block, ensure_ascii=False) + "\n```"
        return result

    monkeypatch.setattr(client, "complete", invalid)
    with pytest.raises(ValueError, match="unverified fund flow"):
        runner.run(DAY, collect=False, push=False, publish_only=True)
    assert client.calls["sentiment"] == 1 and client.calls["researcher"] == 0


@pytest.mark.parametrize("role,claim", [
    ("sentiment", "宁德时代上涨由存量筹码换手推动，未见明显增量放量。"),
    ("sentiment", "宁德时代上涨由存量筹码换手推动且资金流向不可判。"),
    ("researcher", "换手率上升推动股价上涨。"),
    ("researcher", "不得升级为放量推动上涨但放量推动上涨是事实。"),
    ("researcher", "未升级为放量推动上涨但放量推动上涨是事实。"),
])
def test_unverified_volume_causality_is_rejected_before_publication(meeting, monkeypatch, role, claim):
    runner, client, _ = meeting
    complete = client.complete

    def invalid(system, user):
        result = complete(system, user)
        if system.splitlines()[0] == role:
            result["report_markdown"] = claim + "\n" + result["report_markdown"]
        return result

    monkeypatch.setattr(client, "complete", invalid)
    with pytest.raises(ValueError, match="unverified volume causality"):
        runner.run(DAY, collect=False, push=False, publish_only=True)

    run = runner.ledger.list_runs()[0]
    number = "02" if role == "sentiment" else "05"
    step = runner.ledger.latest_step(run["run_id"], f"role_{number}")
    assert step["status"] == "failed" and step["artifact_path"]
    assert claim in json.loads((runner.root / step["artifact_path"]).read_text())["report_markdown"]
    assert not (runner.team / "outputs" / DAY).exists()


def test_researcher_cannot_claim_no_counter_signal_after_listing_one(meeting, monkeypatch):
    runner, client, _ = meeting
    complete = client.complete
    claim = (
        "### 中国平安 601318\n"
        "- **空方核心**：量比 0.85，价格低于 MA20（54.95）与 VWAP20（55.12）；"
        "高利率对保险估值有轻微压制。\n"
        "- **辩论结论**：**看多，信心度 3。** 四方无反向信号；"
        "唯量能与价格确认不足。\n"
    )

    def contradictory(system, user):
        result = complete(system, user)
        if system.splitlines()[0] == "researcher":
            result["report_markdown"] = claim + result["report_markdown"]
        return result

    monkeypatch.setattr(client, "complete", contradictory)
    with pytest.raises(ValueError, match="researcher counter-signal contradiction"):
        runner.run(DAY, collect=False, push=False, publish_only=True)

    run = runner.ledger.list_runs()[0]
    step = runner.ledger.latest_step(run["run_id"], "role_05")
    assert step["status"] == "failed" and step["artifact_path"]
    assert claim in json.loads((runner.root / step["artifact_path"]).read_text())["report_markdown"]
    assert not (runner.team / "outputs" / DAY).exists()


def test_negated_counter_signal_claim_is_not_rejected(meeting, monkeypatch):
    runner, client, _ = meeting
    complete = client.complete

    def valid(system, user):
        result = complete(system, user)
        if system.splitlines()[0] == "researcher":
            result["report_markdown"] = "平安并非无反向信号，量能仍待确认。\n" + result["report_markdown"]
        return result

    monkeypatch.setattr(client, "complete", valid)
    assert runner.run(DAY, collect=False, push=False, publish_only=True)["status"] == "succeeded"
    assert inspect_generation(runner.team, DAY, runner.ledger)["plan_hash"]


@pytest.mark.parametrize("claim", [
    "宁德时代上涨伴随换手率上升，不代表换手推动了上涨。",
    "宁德时代上涨可能由换手推动，仍需逐笔数据核验。",
    "宁德时代上涨并非由换手推动。",
])
def test_bounded_volume_causality_is_not_rejected(meeting, monkeypatch, claim):
    runner, client, _ = meeting
    complete = client.complete

    def valid(system, user):
        result = complete(system, user)
        if system.splitlines()[0] == "sentiment":
            result["report_markdown"] = claim + "\n" + result["report_markdown"]
        return result

    monkeypatch.setattr(client, "complete", valid)
    assert runner.run(DAY, collect=False, push=False, publish_only=True)["status"] == "succeeded"


@pytest.mark.parametrize("role,claim", [
    ("sentiment", "量比1.43，有增量资金介入迹象。"),
    ("sentiment", "量比仅证明活跃度，不构成资金净流入证据。"),
    ("researcher", "量比提高但不能证明资金净流入。"),
    ("researcher", "成交额增加是事实。"),
    ("researcher", "不得升级为放量推动上涨。"),
    ("researcher", "美的「放量长下影反弹」为量价并现陈述，未升级为「放量推动上涨」的确定性因果。"),
    ("sentiment", "资金净流入不可判。"),
    ("sentiment", "量比/换手仅证明活跃度，资金净流入或流出方向不可判（缺资金流原始序列）。"),
    ("sentiment", "全池无资金流原始序列，所有个股仅可描述活跃度，不可判定资金净流入/流出。"),
    ("sentiment", "资金可能向美的集团集中。"),
])
def test_bounded_volume_interpretation_is_not_treated_as_fund_flow_fact(meeting, monkeypatch, role, claim):
    runner, client, _ = meeting
    complete = client.complete

    def valid(system, user):
        result = complete(system, user)
        if system.splitlines()[0] == role:
            result["report_markdown"] = claim + "\n" + result["report_markdown"]
        return result

    monkeypatch.setattr(client, "complete", valid)
    assert runner.run(DAY, collect=False, push=False, publish_only=True)["status"] == "succeeded"


@pytest.mark.parametrize("claim", [
    "本报告未将任何量比、换手、成交额证据升级为资金净流入/流出或主力介入结论。",
    "量比放大仅证明交易活跃，不得升级为资金净流入结论。",
    "活跃度与价格同步观察，不断言资金净流入。",
    "量比只是活跃度，不应认定资金净流入。",
    "不将回购/增持升级为资金流入结论。",
    "但依纪律不得升级为已证实的资金流入。",
    "量比/换手仅可确认活跃度异动，未升级为资金净流入/流出或主力行为结论。",
])
def test_negated_researcher_fund_flow_statement_survives_publication_and_inspection(meeting, monkeypatch, claim):
    runner, client, _ = meeting
    complete = client.complete

    def valid(system, user):
        result = complete(system, user)
        if system.splitlines()[0] == "researcher":
            result["report_markdown"] = claim + "\n" + result["report_markdown"]
        return result

    monkeypatch.setattr(client, "complete", valid)
    result = runner.run(DAY, collect=False, push=False, publish_only=True)
    assert result["status"] == "succeeded"
    assert inspect_generation(runner.team, DAY, runner.ledger)["plan_hash"]


@pytest.mark.parametrize("number,role,claim,error", [
    ("02", "sentiment", "资金真实流入是事实。", "unverified fund flow"),
    ("05", "researcher", "资金真实流入是事实。", "unverified fund flow"),
    ("02", "sentiment", "宁德时代上涨由存量筹码换手推动。", "unverified volume causality"),
    ("05", "researcher", "换手率上升推动股价上涨。", "unverified volume causality"),
])
def test_independent_inspection_rejects_rehashed_fund_flow_claim(meeting, number, role, claim, error):
    from trading_team.kimi_daily import _sha256, generation_outputs

    runner, _, _ = meeting
    run = runner.run(DAY, collect=False, push=False, publish_only=True)
    output = runner.team / "outputs" / DAY
    report = output / f"{number}_{role}.md"
    report.write_text(claim + "\n" + report.read_text())
    role_meta = output / f"{number}_{role}.run.json"
    meta = json.loads(role_meta.read_text())
    meta["output_hash"] = _sha256(report.read_text())
    role_meta.write_text(json.dumps(meta))
    receipt_path = output / "generation.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["outputs"] = generation_outputs(output)
    receipt_path.write_text(json.dumps(receipt))
    digest = _sha256(receipt_path.read_text())
    with runner.ledger._connect() as conn:
        conn.execute("UPDATE step_runs SET input_hash=?,output_hash=? WHERE run_id=? AND step_name='publish_generation'",
                     (digest, digest, run["run_id"]))

    with pytest.raises(ValueError, match=error):
        inspect_generation(runner.team, DAY, runner.ledger)


def test_independent_inspection_rejects_rehashed_researcher_contradiction(meeting):
    from trading_team.kimi_daily import _sha256, generation_outputs

    runner, _, _ = meeting
    run = runner.run(DAY, collect=False, push=False, publish_only=True)
    output = runner.team / "outputs" / DAY
    report = output / "05_researcher.md"
    report.write_text("空方核心：量比0.85，价格低于MA20。辩论结论：四方无反向信号。\n" + report.read_text())
    role_meta = output / "05_researcher.run.json"
    meta = json.loads(role_meta.read_text())
    meta["output_hash"] = _sha256(report.read_text())
    role_meta.write_text(json.dumps(meta))
    receipt_path = output / "generation.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["outputs"] = generation_outputs(output)
    receipt_path.write_text(json.dumps(receipt))
    digest = _sha256(receipt_path.read_text())
    with runner.ledger._connect() as conn:
        conn.execute("UPDATE step_runs SET input_hash=?,output_hash=? WHERE run_id=? AND step_name='publish_generation'",
                     (digest, digest, run["run_id"]))

    with pytest.raises(ValueError, match="researcher counter-signal contradiction"):
        inspect_generation(runner.team, DAY, runner.ledger)


def test_charter_change_cannot_silently_reuse_published_generation(meeting):
    runner, client, _ = meeting
    runner.run(DAY, collect=False, push=False, publish_only=True)
    output = runner.team / "outputs" / DAY
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    charter = runner.team / "prompts/01_fundamental.md"
    charter.write_text(charter.read_text() + "\nNew evidence rule")

    with pytest.raises(ValueError, match="generation input"):
        runner.run(DAY, collect=False, push=False, publish_only=True)

    assert sum(client.calls.values()) == 7
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}


@pytest.mark.parametrize("change", ["endpoint", "parameters"])
def test_same_model_different_transport_cannot_reuse_published_generation(meeting, change):
    runner, client, postprocess = meeting
    runner.run(DAY, collect=False, push=False)
    output = runner.team / "outputs" / DAY
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    if change == "endpoint":
        client.base_url = "https://other.invalid/v1"
    else:
        client.temperature = 0.2
    with pytest.raises(ValueError, match="transport"):
        runner.run(DAY, collect=False, push=False)
    assert len(postprocess) == 1 and sum(client.calls.values()) == 7
    assert before == {p.name: p.read_bytes() for p in output.iterdir()}


def test_interrupted_cache_is_not_shared_across_transport_endpoints(meeting):
    runner, client, _ = meeting
    client.fail_role = "technical"
    with pytest.raises(RuntimeError, match="interruption"):
        runner.run(DAY, collect=False, push=False)
    client.base_url = "https://other.invalid/v1"
    before_calls = dict(client.calls)
    with pytest.raises(ValueError, match="transport"):
        runner.run(DAY, collect=False, push=False)
    assert dict(client.calls) == before_calls
    result = runner.run(DAY, collect=False, force=True, push=False)
    archived = runner.team / ".runs/archive" / DAY / result["run_id"] / "staging/01_fundamental.run.json"
    assert json.loads(archived.read_text())["transport"]["endpoint"] == "https://fixture.invalid/v1"
    assert all(client.calls[role] == 2 for role in ("fundamental", "sentiment", "news"))
    for number, role in ROLE_NAMES.items():
        meta = json.loads((runner.team / "outputs" / DAY / f"{number}_{role}.run.json").read_text())
        assert meta["transport"]["endpoint"] == client.base_url
        assert meta["request"]["transport"] == meta["transport"]
        assert "provider" not in meta


@pytest.mark.parametrize("change", ["before_response", "reported_request"])
def test_transport_changed_during_role_blocks_publication(meeting, monkeypatch, change):
    runner, client, postprocess = meeting
    complete = client.complete
    def mutate(system, user):
        if change == "before_response":
            client.base_url = "https://other.invalid/v1"
        result = complete(system, user)
        if change == "reported_request":
            client.last_meta["transport"]["parameters"]["temperature"] = .1
        return result
    monkeypatch.setattr(client, "complete", mutate)
    with pytest.raises(ValueError, match="transport"):
        runner.run(DAY, collect=False, push=False)
    assert not postprocess
    assert not (runner.team / "outputs" / DAY / "generation.json").exists()
    assert runner.ledger.latest_step(runner.ledger.list_runs()[0]["run_id"], "role_01")["status"] == "failed"


def test_receipt_role_and_actual_request_share_transport_identity(meeting):
    runner, client, _ = meeting
    run = runner.run(DAY, collect=False, push=False)
    output = runner.team / "outputs" / DAY
    receipt = json.loads((output / "generation.json").read_text())
    assert receipt["schema"] == "daily_research.v4"
    assert receipt["transport"] == client.transport_identity
    evidence = inspect_generation(runner.team, DAY, runner.ledger)
    assert evidence["transport"] == client.transport_identity
    for number, role in ROLE_NAMES.items():
        meta = json.loads((output / f"{number}_{role}.run.json").read_text())
        assert meta["transport"] == receipt["transport"] == meta["request"]["transport"]
        assert meta["request"]["reported_model"] == "offline-fixture-revision"
        step = runner.ledger.latest_step(run["run_id"], f"role_{number}")
        assert step["metadata"]["transport"] == receipt["transport"]


def test_old_receipt_is_not_silently_upgraded(meeting):
    from trading_team.kimi_daily import _sha256
    runner, _, postprocess = meeting
    run = runner.run(DAY, collect=False, push=False)
    path = runner.team / "outputs" / DAY / "generation.json"
    receipt = json.loads(path.read_text())
    receipt["schema"] = "daily_research.v3"
    receipt.pop("transport", None)
    path.write_text(json.dumps(receipt))
    before = path.read_bytes()
    digest = _sha256(path.read_text())
    with runner.ledger._connect() as conn:
        conn.execute("UPDATE step_runs SET input_hash=?,output_hash=? WHERE run_id=? AND step_name='publish_generation'",
                     (digest, digest, run["run_id"]))
    with pytest.raises(ValueError, match="generation|transport"):
        runner.run(DAY, collect=False, push=False)
    assert path.read_bytes() == before and len(postprocess) == 1


def test_transport_evidence_is_required_not_guessed(meeting, monkeypatch):
    runner, client, postprocess = meeting
    monkeypatch.setattr(type(client), "transport_identity", property(lambda self: None))
    with pytest.raises(ValueError, match="transport"):
        runner.run(DAY, collect=False, push=False)
    assert not client.calls and not postprocess


@pytest.mark.parametrize("endpoint", ["https://user:PRIVATE@example.com/v1", "https://example.com/v1?key=PRIVATE",
                                     "https://example.com/v1#PRIVATE", "file:///private/tmp/secret", "not a URL"])
def test_transport_endpoint_rejects_credentials_and_unsafe_url_without_echo(endpoint):
    with pytest.raises(ValueError, match="endpoint") as error:
        KimiClient("PRIVATE-KEY", endpoint)
    assert "PRIVATE" not in str(error.value)


def test_client_records_actual_parameters_and_server_reported_model_without_secrets(monkeypatch):
    client = KimiClient("PRIVATE-KEY", "https://api.moonshot.cn/v1/")
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {
            "id": "request-1", "model": "kimi-k3-revision", "usage": {"total_tokens": 42},
            "choices": [{"finish_reason": "stop", "message": {"content": '{"report_markdown":"fixture"}'}}]})
    monkeypatch.setattr(client.session, "post", post)
    original = deepcopy(client.transport_identity)
    client.complete("system", "user", max_tokens=4096)
    sent = calls[0][1]["json"]
    assert calls[0][1]["allow_redirects"] is False
    identity = client.last_meta["transport"]
    assert identity["endpoint"] == "https://api.moonshot.cn/v1"
    assert identity["parameters"]["max_tokens"] == sent["max_tokens"] == 4096
    assert identity["parameters"]["temperature"] == sent["temperature"]
    assert identity["model"] == sent["model"] == client.model
    assert client.last_meta["reported_model"] == "kimi-k3-revision"
    assert "PRIVATE" not in json.dumps(client.last_meta)
    assert client.transport_identity == original


def test_key_rotation_does_not_change_nonsecret_transport_identity():
    first = KimiClient("old-key", "https://api.moonshot.cn/v1")
    second = KimiClient("new-key", "https://api.moonshot.cn/v1")
    assert first.transport_identity == second.transport_identity


@pytest.mark.parametrize("changed_business_fact", [False, True])
def test_resume_ignores_snapshot_observation_time_but_not_account_facts(meeting, monkeypatch, changed_business_fact):
    runner, client, _ = meeting
    current = {"generated_at": "2026-09-21T08:00:00+08:00", "portfolios": {"gate": {"cash": 100000}},
               "quote_asof": "2026-09-18"}
    monkeypatch.setattr("trading_team.kimi_daily.dual_paper_snapshot", lambda *args, **kwargs: deepcopy(current))
    client.fail_role = "risk_manager"
    with pytest.raises(RuntimeError, match="interruption"):
        runner.run(DAY, collect=False, push=False)
    current["generated_at"] = "2026-09-21T09:00:00+08:00"
    if changed_business_fact:
        current["portfolios"]["gate"]["cash"] = 90000
    runner.run(DAY, collect=False, push=False)
    assert client.calls["trader"] == (2 if changed_business_fact else 1)
    assert client.calls["risk_manager"] == 2
    text = (runner.team / "outputs" / DAY / "06_trader.input.txt").read_text()
    assert "generated_at" not in text and "2026-09-18" in text


@pytest.mark.parametrize("damage", ["request", "role", "missing"])
def test_independent_inspection_rejects_inconsistent_role_transports(meeting, damage):
    from trading_team.kimi_daily import _sha256, generation_outputs
    runner, _, _ = meeting
    run = runner.run(DAY, collect=False, push=False)
    output = runner.team / "outputs" / DAY
    path = output / "01_fundamental.run.json"
    meta = json.loads(path.read_text())
    if damage == "missing":
        meta.pop("transport")
    elif damage == "role":
        meta["transport"]["endpoint"] = "https://other.invalid/v1"
    else:
        meta["request"]["transport"]["endpoint"] = "https://other.invalid/v1"
    path.write_text(json.dumps(meta))
    receipt_path = output / "generation.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["outputs"] = generation_outputs(output)
    receipt_path.write_text(json.dumps(receipt))
    digest = _sha256(receipt_path.read_text())
    with runner.ledger._connect() as conn:
        conn.execute("UPDATE step_runs SET input_hash=?,output_hash=? WHERE run_id=? AND step_name='publish_generation'",
                     (digest, digest, run["run_id"]))
    with pytest.raises(ValueError, match="transport"):
        inspect_generation(runner.team, DAY, runner.ledger)


def test_redirect_response_cannot_certify_a_different_service(monkeypatch):
    client = KimiClient("fixture-key", "https://api.moonshot.cn/v1")
    monkeypatch.setattr("trading_team.kimi_daily.time.sleep", lambda _: None)
    monkeypatch.setattr(client.session, "post", lambda *args, **kwargs: SimpleNamespace(
        status_code=302, raise_for_status=lambda: None, json=lambda: {
            "choices": [{"finish_reason": "stop", "message": {"content": '{"report_markdown":"fixture"}'}}]}))
    with pytest.raises(RuntimeError, match="HTTP 302"):
        client.complete("system", "user")
    assert client.last_meta == {}


@pytest.mark.parametrize("change", ["extra", "model", "nan", "bool", "missing"])
def test_invalid_transport_identity_blocks_calls_without_echoing_secrets(meeting, monkeypatch, change):
    runner, client, _ = meeting
    identity = client.transport_identity
    if change == "extra":
        identity["api_key"] = "PRIVATE-KEY"
    elif change == "model":
        identity["model"] = "other"
    elif change == "nan":
        identity["parameters"]["temperature"] = float("nan")
    elif change == "bool":
        identity["parameters"]["max_tokens"] = True
    else:
        identity["parameters"].pop("thinking")
    monkeypatch.setattr(type(client), "transport_identity", property(lambda self: identity))
    with pytest.raises(ValueError, match="transport") as error:
        runner.run(DAY, collect=False, push=False)
    assert not client.calls and "PRIVATE" not in str(error.value)


def test_request_identity_is_durable_before_call_and_after_interruption(meeting, monkeypatch):
    runner, client, _ = meeting
    def interrupt(*_args):
        run = runner.ledger.list_runs()[0]
        step = runner.ledger.latest_step(run["run_id"], "role_01")
        assert step["status"] == "running"
        assert step["metadata"]["transport"] == client.transport_identity
        raise RuntimeError("injected interruption")
    monkeypatch.setattr(client, "complete", interrupt)
    with pytest.raises(RuntimeError, match="interruption"):
        runner.run(DAY, collect=False, push=False)
    run = runner.ledger.get_run(runner.ledger.list_runs()[0]["run_id"])
    assert run["metadata"]["transport"] == client.transport_identity
    assert run["steps"][0]["metadata"]["transport"] == client.transport_identity
