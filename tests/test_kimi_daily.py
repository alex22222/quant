# -*- coding: utf-8 -*-
import json
import sys

import pytest

from trading_team.kimi_daily import (
    DailyMeetingRunner,
    KimiClient,
    _apply_risk_reviews,
    _market_gate,
    _normalize_proposals,
    _validate_manifest,
    direct_network_env,
)
from trading_team.run_ledger import RunLedger


def _proposal(code="601318", pct=0.15):
    return {
        "code": code,
        "name": "中国平安",
        "direction": "条件买入",
        "position_pct": pct,
        "entry": [53.5],
        "entry_type": "limit",
        "stop": 50.6,
        "condition": None,
        "execution_conditions": [{"field": "open_gap_pct", "op": "lte", "value": 0.03}],
        "horizon": "2-4周",
        "confidence": 3,
    }


def test_normalize_proposals_requires_full_watchlist_coverage():
    with pytest.raises(ValueError, match="未完整覆盖观察池"):
        _normalize_proposals([_proposal()], {"601318", "600036"})


@pytest.mark.parametrize("changes", [{"condition": "闸门恢复后执行"}, {"entry_type": "limit_pullback"},
                                      {"entry": [53.5, 52.2]}, {"entry": [True]}])
def test_model_output_cannot_weaken_entry_contract(changes):
    with pytest.raises(ValueError):
        _normalize_proposals([dict(_proposal(), **changes)], {"601318"})


def test_risk_reduction_preserves_structured_guards():
    plan = _normalize_proposals([_proposal()], {"601318"})[0]
    final = _apply_risk_reviews([plan], [{"code": "601318", "decision": "降仓通过", "position_pct": .08,
                                         "reason_codes": ["position_limit"], "reason": "size limit"}])
    assert final[0]["execution_conditions"] == plan["execution_conditions"]


def test_apply_risk_reviews_reduces_and_rejects_deterministically():
    proposals = [_proposal(), _proposal("600036", 0.10)]
    reviews = [
        {"code": "601318", "decision": "降仓通过", "position_pct": 0.08,
         "reason_codes": ["position_limit"], "reason": "集中度偏高"},
        {"code": "600036", "decision": "否决", "clause": "条款4自动冻结",
         "reason_codes": ["stop_distance"], "reason": "止损无效"},
    ]
    final = _apply_risk_reviews(proposals, reviews)
    assert final[0]["position_pct"] == 0.08
    assert final[1]["direction"] == "回避"
    assert final[1]["position_pct"] == 0.0
    assert final[1]["entry"] is None
    assert final[1]["risk_clause"] == "条款4自动冻结"


def test_apply_risk_reviews_rejects_fake_reduction():
    with pytest.raises(ValueError, match="降仓后仓位未降低"):
        _apply_risk_reviews(
            [_proposal()],
            [{"code": "601318", "decision": "降仓通过", "position_pct": 0.20,
              "reason_codes": ["position_limit"], "reason": "test invalid reduction"}],
        )


def test_market_gate_uses_ma60(tmp_path):
    context = tmp_path / "context"
    context.mkdir()
    (context / "technical_000300.json").write_text(
        '{"close": 101, "ma20": 100, "ma60": 102}', encoding="utf-8"
    )
    gate = _market_gate(context)
    assert gate == {"market_ok": False, "close": 101.0, "ma60": 102.0}


def test_direct_network_env_removes_inherited_proxies(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7890")
    monkeypatch.setenv("KEEP_ME", "yes")
    env = direct_network_env()
    assert "HTTP_PROXY" not in env
    assert "https_proxy" not in env
    assert env["KEEP_ME"] == "yes"


def test_validate_manifest_fails_closed_on_partial_source():
    with pytest.raises(ValueError, match="失败数据源"):
        _validate_manifest({
            "date": "2026-09-21",
            "ok": True,
            "latest_trade_date": "2026-09-18",
            "sources": {"news": {"ok": False}},
        }, "2026-09-21")


def test_validate_manifest_requires_source_details():
    with pytest.raises(ValueError, match="缺数据源明细"):
        _validate_manifest({
            "date": "2026-09-21",
            "ok": True,
            "latest_trade_date": "2026-09-18",
            "sources": {},
        }, "2026-09-21")


def test_existing_report_cannot_bypass_manifest_validation(tmp_path, monkeypatch):
    team = tmp_path / "trading_team"
    output = team / "outputs" / "2026-09-21"
    context = team / "context" / "2026-09-21"
    decision = team / "decisions" / "2026-09-21"
    for path in (output, context, decision):
        path.mkdir(parents=True)
    (output / "daily_conclusion.md").write_text("existing", encoding="utf-8")
    (context / "manifest.json").write_text(
        json.dumps({"latest_trade_date": "2026-09-18"}), encoding="utf-8"
    )
    (decision / "decision.json").write_text(
        json.dumps({"plan_hash": "plan-v1"}), encoding="utf-8"
    )

    client = KimiClient("fixture-key", "https://fixture.invalid/v1", "test-model")
    ledger = RunLedger(tmp_path / "ledger.db")
    runner = DailyMeetingRunner(client, team=team, ledger=ledger)
    monkeypatch.setattr(
        runner,
        "_postprocess",
        lambda run_id, day, push: {"finalize": [], "notification": {"status": "disabled"}},
    )
    with pytest.raises(ValueError, match="manifest"):
        runner.run("2026-09-21", collect=False, push=False)
    run = ledger.get_run(ledger.list_runs()[0]["run_id"])

    assert run["status"] == "failed"
    assert run["steps"][0]["status"] == "failed"


def test_postprocess_fails_when_any_finalize_workflow_failed(tmp_path, monkeypatch):
    team = tmp_path / "trading_team"
    (team / "outputs").mkdir(parents=True)
    runner = DailyMeetingRunner(
        type("Client", (), {"model": "test-model"})(),
        team=team,
        ledger=RunLedger(tmp_path / "ledger.db"),
    )
    run_id = runner.ledger.start_run(
        "daily_research", "2026-09-21", trigger_source="test"
    )
    monkeypatch.setattr(
        "trading_team.kimi_daily.enrich_plan", lambda day, root: {"updated": True}
    )
    monkeypatch.setattr(
        "trading_team.kimi_daily.backfill_accuracy", lambda day, root: {"updated": True}
    )
    monkeypatch.setattr(
        "trading_team.daily_finalize.finalize_available_tracked",
        lambda **kwargs: [{"date": "2026-09-20", "status": "failed"}],
    )

    with pytest.raises(RuntimeError, match="2026-09-20"):
        runner._postprocess(run_id, "2026-09-21", push=False)

    step = runner.ledger.latest_step(run_id, "finalize_available")
    assert step["status"] == "failed"


def test_postprocess_records_current_paper_valuation_after_finalize(tmp_path, monkeypatch):
    team = tmp_path / "trading_team"
    output = team / "outputs" / "2026-09-21"
    output.mkdir(parents=True)
    (output / "plan.json").write_text(json.dumps({
        "date": "2026-09-21", "data_asof": "2026-09-18", "market_ok": False,
        "proposals": [], "paper_proposals": [],
    }), encoding="utf-8")
    from trading_team.daily_finalize import materialize_day
    materialize_day("2026-09-21", team)
    runner = DailyMeetingRunner(
        type("Client", (), {"model": "test-model"})(),
        team=team,
        ledger=RunLedger(tmp_path / "ledger.db"),
    )
    run_id = runner.ledger.start_run(
        "daily_research", "2026-09-21", trigger_source="test"
    )
    monkeypatch.setattr(
        "trading_team.kimi_daily.enrich_plan", lambda day, root: {"updated": True}
    )
    monkeypatch.setattr(
        "trading_team.kimi_daily.backfill_accuracy", lambda day, root: {"updated": True}
    )
    monkeypatch.setattr(
        "trading_team.daily_finalize.finalize_available_tracked",
        lambda **kwargs: [{"date": "2026-09-20", "status": "succeeded"}],
    )
    monkeypatch.setattr(
        "trading_team.paper_pipeline.record_current_valuations",
        lambda *args, **kwargs: {
            "quote_asof": "2026-09-18",
            "portfolios": {"gate": {"status": "inserted"}},
        },
    )
    monkeypatch.setattr(
        "trading_team.build_index.build_index", lambda root: ["2026-09-21"]
    )

    result = runner._postprocess(run_id, "2026-09-21", push=False)

    assert result["paper_valuation"]["quote_asof"] == "2026-09-18"
    step = runner.ledger.latest_step(run_id, "paper_valuation")
    assert step["status"] == "succeeded"


@pytest.mark.parametrize("flags", [[], ["--no-collect"], ["--no-push"]])
def test_isolated_cli_requires_no_collect_and_no_push(tmp_path, monkeypatch, capsys, flags):
    from trading_team import kimi_daily
    team = tmp_path / "trading_team"
    team.mkdir()
    monkeypatch.setattr(sys, "argv", ["kimi_daily", "--team", str(team), *flags])
    monkeypatch.setattr(kimi_daily, "load_local_env", lambda: pytest.fail("must not load credentials"))
    with pytest.raises(SystemExit) as error:
        kimi_daily.main()
    assert error.value.code == 2
    assert "requires --no-collect and --no-push" in capsys.readouterr().err
    assert not (team / ".runs").exists()


@pytest.mark.parametrize("alias", [False, True])
def test_isolated_cli_rejects_production_root(tmp_path, monkeypatch, capsys, alias):
    from trading_team import kimi_daily
    team = kimi_daily.TEAM
    if alias:
        team = tmp_path / "trading_team"
        team.symlink_to(kimi_daily.TEAM, target_is_directory=True)
    monkeypatch.setattr(sys, "argv", ["kimi_daily", "--team", str(team), "--no-collect", "--no-push"])
    monkeypatch.setattr(kimi_daily, "load_local_env", lambda: pytest.fail("must not load credentials"))
    with pytest.raises(SystemExit) as error:
        kimi_daily.main()
    assert error.value.code == 2
    assert "outside the production project" in capsys.readouterr().err


def test_isolated_cli_rejects_shared_state_symlinks(tmp_path, monkeypatch, capsys):
    from trading_team import kimi_daily
    team = tmp_path / "trading_team"
    team.mkdir()
    (team / "outputs").symlink_to(kimi_daily.TEAM / "outputs", target_is_directory=True)
    monkeypatch.setattr(sys, "argv", ["kimi_daily", "--team", str(team), "--no-collect", "--no-push"])
    with pytest.raises(SystemExit) as error:
        kimi_daily.main()
    assert error.value.code == 2
    assert "must not contain symlinks" in capsys.readouterr().err


def test_isolated_cli_failure_only_writes_selected_ledger(tmp_path, monkeypatch):
    from trading_team import kimi_daily
    day = "2026-09-22"
    team = tmp_path / "trading_team"
    context = team / "context" / day
    context.mkdir(parents=True)
    (context / "manifest.json").write_text(json.dumps({"date": day, "ok": False}))
    monkeypatch.setattr(sys, "argv", ["kimi_daily", "--team", str(team), "--date", day,
                                      "--no-collect", "--no-push"])
    monkeypatch.setenv("MOONSHOT_API_KEY", "offline-test-only")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://fixture.invalid/v1")
    monkeypatch.setattr(kimi_daily, "load_local_env", lambda: pytest.fail("isolated CLI must not load production credentials"))
    monkeypatch.setattr(KimiClient, "complete", lambda *a, **kw: pytest.fail("bad manifest must not call model"))
    with pytest.raises(ValueError, match="manifest"):
        kimi_daily.main()
    ledger = RunLedger(team / ".runs" / "ledger.db")
    runs = ledger.list_runs()
    assert len(runs) == 1 and runs[0]["status"] == "failed"
    assert not (tmp_path / "paper").exists()
    assert not (team / "outputs").exists()


@pytest.mark.parametrize("publish_only", [False, True])
def test_default_cli_keeps_production_entrypoint(monkeypatch, capsys, publish_only):
    from trading_team import kimi_daily
    calls = []
    flags = ["--publish-only", "--no-push"] if publish_only else []
    monkeypatch.setattr(sys, "argv", ["kimi_daily", "--date", "2026-09-22", *flags])
    monkeypatch.setattr(kimi_daily, "load_local_env", lambda: calls.append("load_env"))
    monkeypatch.setattr(kimi_daily, "KimiClient", lambda *args: object())

    class Runner:
        def __init__(self, client, team=kimi_daily.TEAM):
            calls.append(team)

        def run(self, day, **kwargs):
            assert kwargs["collect"] is True and kwargs["push"] is not publish_only
            assert kwargs["publish_only"] is publish_only
            return {"date": day}

    monkeypatch.setattr(kimi_daily, "DailyMeetingRunner", Runner)
    kimi_daily.main()
    assert calls == ["load_env", kimi_daily.TEAM]
    assert json.loads(capsys.readouterr().out)["date"] == "2026-09-22"
