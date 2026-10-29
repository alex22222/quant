import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from trading_team.decision_pipeline import canonical_plan_hash
from trading_team.paper_pipeline import _db, paper_db_paths
from trading_team.run_ledger import RunLedger


NOW = datetime(2026, 9, 22, 8, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def workspace(tmp_path):
    team = tmp_path / "trading_team"
    calendar = tmp_path / "calendar.npy"
    np.save(calendar, np.array([20260918, 20260921, 20260922, 20260923]))
    return team, calendar


def snapshot(workspace, now=NOW):
    from trading_team.operational_snapshot import operational_snapshot
    team, calendar = workspace
    return operational_snapshot(team=team, calendar_path=calendar, now=now)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def research(team, day="2026-09-21"):
    plan = {"date": day, "data_asof": "2026-09-18", "market_ok": False, "proposals": []}
    write_json(team / "outputs" / day / "plan.json", plan)
    write_json(team / "context" / day / "manifest.json", {
        "date": day, "ok": True, "latest_trade_date": "2026-09-18",
        "sources": {"technical": {"ok": True}},
    })
    return plan


def test_missing_workspace_is_unknown_and_not_created(workspace):
    result = snapshot(workspace)
    assert not workspace[0].exists()
    assert not workspace[0].parent.joinpath("paper").exists()
    assert result["calendar"]["expected_session"] == "2026-09-21"
    assert result["research"]["status"] == "missing"
    assert result["runs"] == []
    for account in result["portfolios"].values():
        assert account["status"] == "missing"
        assert account["cash"] is None
        assert account["valuation_equity"] is None


def test_successful_run_is_not_verified_generation(workspace):
    team, _ = workspace
    research(team)
    ledger = RunLedger(team / ".runs/ledger.db")
    run_id = ledger.start_run("daily_research", "2026-09-21", trigger_source="test")
    ledger.finish_run(run_id, status="succeeded")
    result = snapshot(workspace)
    assert result["runs"][0]["status"] == "succeeded"
    assert result["research"]["generation"]["status"] == "missing"
    assert result["data"]["manifest_ok"] is False
    assert result["data"]["freshness"] == "unknown"
    assert "archive" in result["data"]["error"]
    assert result["research"]["risk_review"]["status"] == "legacy_missing"
    assert result["research"]["risk_review"]["reviewed_count"] is None


def test_risk_summary_distinguishes_gate_only_and_mixed_rejection(workspace):
    from trading_team.risk_contract import build_reviewed_plan
    team, _ = workspace
    proposals = [{"code": code, "direction": "买入", "position_pct": .1,
                  "entry": [40], "entry_type": "limit", "stop": 38} for code in ("600036", "601318")]
    reviews = [{"code": "600036", "decision": "否决", "reason_codes": ["market_gate"], "reason": "gate closed"},
               {"code": "601318", "decision": "否决", "reason_codes": ["market_gate", "liquidity"], "reason": "two risks"}]
    plan = build_reviewed_plan("2026-09-21", "2026-09-18", False, proposals, reviews)
    path = team / "outputs/2026-09-21/plan.json"
    write_json(path, plan)
    before = path.read_bytes()
    summary = snapshot(workspace)["research"]["risk_review"]
    assert summary["status"] == "validated_contract"
    assert summary["reviewed_count"] == 2 and summary["gate_only_count"] == 1
    assert summary["risk_rejected_count"] == 1
    assert path.read_bytes() == before
    plan["paper_proposals"][1]["direction"] = "买入"
    write_json(path, plan)
    summary = snapshot(workspace)["research"]["risk_review"]
    assert summary["status"] == "invalid" and summary["reviewed_count"] is None


def test_corrupt_latest_plan_does_not_fall_back(workspace):
    team, _ = workspace
    research(team, "2026-09-18")
    path = team / "outputs/2026-09-21/plan.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    result = snapshot(workspace)
    assert result["research"]["date"] == "2026-09-21"
    assert result["research"]["status"] == "invalid"
    assert result["research"]["plan_hash"] is None


def test_changed_plan_invalidates_displayed_approval_binding(workspace):
    team, _ = workspace
    plan = research(team)
    write_json(team / "decisions/2026-09-21/decision.json", {
        "date": plan["date"], "plan_hash": canonical_plan_hash(plan),
        "plans": [{"approval": "approved", "approved_plan_hash": canonical_plan_hash(plan)}],
    })
    assert snapshot(workspace)["research"]["decision"]["approved_count"] == 1
    plan["market_ok"] = True
    write_json(team / "outputs/2026-09-21/plan.json", plan)
    binding = snapshot(workspace)["research"]["decision"]
    assert binding["status"] == "mismatch"
    assert binding["approved_count"] == 0


def test_real_accounts_are_isolated_and_missing_quotes_not_zero(workspace):
    team, _ = workspace
    path = paper_db_paths(team)["no_gate"]
    with _db(path) as conn:
        conn.execute("UPDATE account SET cash=90000,equity=100000")
        conn.execute("INSERT INTO positions VALUES ('601318',200,50,'2026-09-18')")
    before = path.read_bytes()
    result = snapshot(workspace)
    account = result["portfolios"]["no_gate"]
    assert result["portfolios"]["gate"]["status"] == "missing"
    assert account["cash"] == 90000
    assert account["position_count"] == 1
    assert account["valuation_equity"] is None
    assert account["missing_marks"] == ["601318"]
    assert path.read_bytes() == before


def test_empty_or_broken_account_does_not_invent_initial_capital(workspace):
    team, _ = workspace
    paths = paper_db_paths(team)
    with _db(paths["gate"]) as conn:
        conn.execute("DELETE FROM account")
    paths["no_gate"].write_text("not sqlite")
    result = snapshot(workspace)
    for account in result["portfolios"].values():
        assert account["status"] == "invalid"
        assert account["cash"] is None


@pytest.mark.parametrize("hour,minute,expected", [(15, 9, "2026-09-21"), (15, 10, "2026-09-22")])
def test_completed_session_cutoff(workspace, hour, minute, expected):
    result = snapshot(workspace, NOW.replace(hour=hour, minute=minute))
    assert result["calendar"]["expected_session"] == expected


def test_missing_calendar_never_claims_fresh(workspace):
    research(workspace[0])
    workspace[1].unlink()
    result = snapshot(workspace)
    assert result["calendar"]["status"] == "unavailable"
    assert result["data"]["freshness"] == "unknown"


def test_readonly_ledger_cannot_write_or_create(workspace):
    team, _ = workspace
    path = team / ".runs/ledger.db"
    with pytest.raises(sqlite3.OperationalError):
        RunLedger(path, readonly=True).list_runs()
    assert not team.exists()
    RunLedger(path)
    with pytest.raises(sqlite3.OperationalError):
        RunLedger(path, readonly=True).start_run("test", "2026-09-21", trigger_source="test")


def test_broken_latest_manifest_does_not_hide_behind_old_prices(workspace):
    team, _ = workspace
    research(team, "2026-09-18")
    write_json(team / "context/2026-09-18/technical_601318.json", {
        "close": 50, "as_of": "2026-09-18",
    })
    write_json(team / "context/2026-09-21/manifest.json", [])
    result = snapshot(workspace)
    assert result["data"]["status"] == "invalid"
    assert result["data"]["date"] == "2026-09-21"
    assert result["data"]["manifest_ok"] is False


def test_bad_calendar_coverage_does_not_assume_weekdays(workspace):
    result = snapshot(workspace, NOW.replace(day=30))
    assert result["calendar"]["status"] == "unavailable"


def test_isolated_dual_snapshot_does_not_read_production(workspace):
    from trading_team.paper_pipeline import dual_paper_snapshot

    result = dual_paper_snapshot(team=workspace[0])
    assert all(not account["exists"] for account in result["portfolios"].values())


def test_console_snapshot_route_with_query_has_no_initialization_side_effect(workspace, monkeypatch):
    import io
    from console import serve

    monkeypatch.setattr(serve, "ROOT", workspace[0].parent)
    handler = serve.H.__new__(serve.H)
    handler.path = "/api/operational-snapshot?_=test"
    handler.wfile = io.BytesIO()
    statuses = []
    handler.send_response = statuses.append
    handler.send_header = lambda *_args: None
    handler.end_headers = lambda: None
    handler.do_GET()
    payload = json.loads(handler.wfile.getvalue())
    assert statuses == [200]
    assert payload["ok"] is True
    assert payload["schema"] == "operational_snapshot.v1"
    assert payload["portfolios"]["gate"]["cash"] is None
    assert not workspace[0].exists()


def test_today_collection_can_value_previous_completed_session(workspace):
    team, _ = workspace
    with _db(paper_db_paths(team)["no_gate"]) as conn:
        conn.execute("UPDATE account SET cash=90000")
        conn.execute("INSERT INTO positions VALUES ('601318',200,50,'2026-09-18')")
    write_json(team / "context/2026-09-22/manifest.json", {
        "date": "2026-09-22", "latest_trade_date": "2026-09-21", "ok": True,
        "sources": {"technical": {"ok": True}},
    })
    write_json(team / "context/2026-09-22/technical_601318.json", {
        "close": 51, "as_of": "2026-09-21",
    })
    write_json(team / "context/2026-09-22/snapshot.json", {"601318": {
        "price_basis": "unadjusted", "quote_time": "20260921150003",
        "open": 50, "high": 52, "low": 49, "close": 51, "prev_close": 50,
        "volume": 100000, "limit_up": 55, "limit_down": 45,
    }})
    account = snapshot(workspace)["portfolios"]["no_gate"]
    assert account["quote_asof"] == "2026-09-21"
    assert account["valuation_equity"] == 100200
    assert account["freshness"] == "current"


def test_not_yet_completed_prices_cannot_value_account(workspace):
    team, _ = workspace
    with _db(paper_db_paths(team)["no_gate"]) as conn:
        conn.execute("INSERT INTO positions VALUES ('601318',200,50,'2026-09-18')")
    write_json(team / "context/2026-09-22/manifest.json", {
        "date": "2026-09-22", "latest_trade_date": "2026-09-22", "ok": True,
        "sources": {"technical": {"ok": True}},
    })
    write_json(team / "context/2026-09-22/technical_601318.json", {
        "close": 52, "as_of": "2026-09-22",
    })
    result = snapshot(workspace)
    assert result["data"]["freshness"] == "unknown"
    assert result["data"]["manifest_ok"] is False
    assert result["portfolios"]["no_gate"]["valuation_equity"] is None


def test_snapshot_exposes_unresolved_old_notification_independent_of_latest_plan(workspace):
    from trading_team.notification_outbox import push_once, DeliveryUnknown
    team, _ = workspace
    research(team)
    with pytest.raises(DeliveryUnknown):
        push_once("2026-09-18", "older-plan", team=team, sender=lambda _: {"pushed": False})
    result = snapshot(workspace)
    assert result["notifications"]["counts"]["unknown"] == 1
    assert result["notifications"]["issues"][0]["date"] == "2026-09-18"


def test_notification_api_is_readonly_without_creating_outbox(workspace, monkeypatch):
    import io
    from console import serve
    monkeypatch.setattr(serve, "ROOT", workspace[0].parent)
    handler = serve.H.__new__(serve.H)
    handler.path = "/api/notifications"
    handler.wfile = io.BytesIO()
    statuses = []
    handler.send_response = statuses.append
    handler.send_header = lambda *_: None
    handler.end_headers = lambda: None
    handler.do_GET()
    assert statuses == [200]
    assert json.loads(handler.wfile.getvalue())["notifications"]["status"] == "missing"
    assert not workspace[0].exists()
