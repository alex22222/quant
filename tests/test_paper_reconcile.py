import json
import sqlite3
from datetime import datetime

import pytest

from paper.execution_model import Bar
from trading_team.decision_pipeline import canonical_plan_hash
from trading_team.paper_pipeline import _execute_portfolio, materialize_paper_decision, paper_db_paths
from trading_team.risk_contract import build_reviewed_plan

DAY = "2026-09-21"


@pytest.fixture
def account_team(tmp_path, execution_cache, finalize_calendar):
    team = tmp_path / "trading_team"
    out = team / "outputs" / DAY
    out.mkdir(parents=True)
    plan = {"date": DAY, "data_asof": "2026-09-18", "market_ok": True,
            "proposals": [{"code": "600036", "direction": "条件买入", "position_pct": 0.1,
                           "entry": [40], "entry_type": "limit", "stop": 38}]}
    plan = build_reviewed_plan(DAY, plan["data_asof"], True, plan["proposals"],
                               [{"code": "600036", "decision": "通过", "reason_codes": []}])
    (out / "plan.json").write_text(json.dumps(plan))
    decision = materialize_paper_decision(DAY, team)
    bar = {"open": 40, "high": 41, "low": 39, "close": 40.5, "prev_close": 40}
    execution_cache(team, DAY, {code: bar for code in ("600036", "000300")})
    for name, path in paper_db_paths(team).items():
        _execute_portfolio(decision, name, path, {"600036": Bar(**bar)}, True)
    return team


def audit(team):
    from trading_team.paper_reconcile import reconcile_accounts
    return reconcile_accounts(team=team, now=datetime(2026, 9, 22, 8))


def issues(item):
    return {issue["code"] for issue in item["issues"]}


def test_clean_book_matches_arithmetic_without_claiming_forward_certification(account_team):
    before = {str(p): p.read_bytes() for p in account_team.parent.rglob("*") if p.is_file()}
    result = audit(account_team)
    for item in result["portfolios"].values():
        assert item["checks"]["cash"] == "matched"
        assert item["checks"]["positions"] == "matched"
        assert item["checks"]["order_trade"] == "matched"
        assert item["execution_certified"] is False
        assert item["days"][DAY]["price_replay"] == "matched_recorded_quantity"
    assert {str(p): p.read_bytes() for p in account_team.parent.rglob("*") if p.is_file()} == before


def test_legacy_price_rounding_and_amount_mismatch_are_not_hidden(account_team):
    path = paper_db_paths(account_team)["no_gate"]
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE execution_receipts")
        conn.execute("UPDATE trades SET price=54.829,qty=100,amount=5482.94,fee=5.11")
        conn.execute("UPDATE pending_orders SET fill_price=54.829,qty=100,fee=5.11")
        conn.execute("UPDATE account SET cash=94511.95 WHERE day=?", (DAY,))
        conn.execute("UPDATE positions SET qty=100,avg_cost=54.8805")
    item = audit(account_team)["portfolios"]["no_gate"]
    assert item["status"] == "conflict"
    assert {"price_tick", "amount_price", "receipt_missing"} <= issues(item)
    assert item["checks"]["cash"] == "matched"
    conflict = next(i for i in item["issues"] if i["code"] == "amount_price")
    assert conflict["expected"] == 5482.90
    assert conflict["actual"] == 5482.94


@pytest.mark.parametrize("mutation,code", [
    ("UPDATE account SET cash=cash+1 WHERE day!='init'", "cash_ledger"),
    ("UPDATE positions SET qty=qty+100", "positions_ledger"),
    ("DELETE FROM pending_orders", "order_trade"),
    ("UPDATE trades SET signal_day=day", "signal_causality"),
    ("UPDATE order_events SET fee=fee+2", "order_event"),
    ("UPDATE trades SET fee=fee+1", "fee_policy"),
])
def test_changed_account_facts_are_reported(account_team, mutation, code):
    with sqlite3.connect(paper_db_paths(account_team)["gate"]) as conn:
        conn.execute(mutation)
    assert code in issues(audit(account_team)["portfolios"]["gate"])


def test_qfq_cache_is_missing_evidence_not_a_successful_replay(account_team):
    path = account_team / "replay_data" / f"{DAY}.json"
    path.write_text(json.dumps({"date": DAY, "source": "tencent_qfq_kline", "bars": {}}))
    before = path.read_bytes()
    item = audit(account_team)["portfolios"]["no_gate"]
    assert item["days"][DAY]["price_replay"] == "unavailable"
    assert "market_evidence" in issues(item)
    assert path.read_bytes() == before


def test_plan_change_does_not_rebind_history(account_team):
    path = account_team / "outputs" / DAY / "plan.json"
    plan = json.loads(path.read_text())
    plan["proposals"][0]["entry"] = [39]
    path.write_text(json.dumps(plan))
    item = audit(account_team)["portfolios"]["gate"]
    assert "plan_binding" in issues(item)
    assert item["days"][DAY]["price_replay"] == "unavailable"


def test_untouched_limit_cannot_match_filled_record(account_team, execution_cache):
    bar = {"open": 41, "high": 42, "low": 40.5, "close": 41, "prev_close": 40}
    execution_cache(account_team, DAY, {code: bar for code in ("600036", "000300")})
    item = audit(account_team)["portfolios"]["gate"]
    assert item["days"][DAY]["price_replay"] == "conflict"
    mismatch = next(i for i in item["issues"] if i["code"] == "fill_replay")
    assert mismatch["expected"]["status"] == "unfilled"


def test_missing_account_not_reported_as_zero_or_created(tmp_path):
    result = audit(tmp_path / "trading_team")
    assert result["portfolios"]["gate"]["status"] == "missing"
    assert not (tmp_path / "paper").exists()
    assert not (tmp_path / "trading_team").exists()


def test_bad_account_does_not_hide_other_account(account_team):
    paper_db_paths(account_team)["gate"].write_bytes(b"broken")
    result = audit(account_team)
    assert result["portfolios"]["gate"]["status"] == "invalid"
    assert result["portfolios"]["no_gate"]["checks"]["cash"] == "matched"


def test_free_text_condition_is_not_claimed_as_verified(account_team):
    plan_path = account_team / "outputs" / DAY / "plan.json"
    plan = json.loads(plan_path.read_text())
    for key in ("risk_contract", "trader_proposals", "risk_reviews"):
        plan.pop(key)
    plan["proposals"][0]["condition"] = "Only enter after independent confirmation"
    plan["paper_proposals"][0]["condition"] = plan["proposals"][0]["condition"]
    plan_path.write_text(json.dumps(plan))
    materialize_paper_decision(DAY, account_team)
    for path in paper_db_paths(account_team).values():
        with sqlite3.connect(path) as conn:
            for table in ("pending_orders", "trades", "order_events", "execution_receipts"):
                conn.execute(f"UPDATE {table} SET plan_hash=?", (canonical_plan_hash(plan),))
    assert "unmodeled_condition" in issues(audit(account_team)["portfolios"]["gate"])


def test_tampered_receipt_hash_is_conflict_not_missing_evidence(account_team):
    path = paper_db_paths(account_team)["gate"]
    with sqlite3.connect(path) as conn:
        previous = json.loads(conn.execute("SELECT result_json FROM execution_receipts").fetchone()[0])
        previous["execution_inputs_hash"] = "0" * 64
        conn.execute("UPDATE execution_receipts SET result_json=?", (json.dumps(previous),))
    item = audit(account_team)["portfolios"]["gate"]
    assert item["status"] == "conflict"
    assert "receipt_conflict" in issues(item)


def test_orphan_trade_date_is_not_omitted_from_evidence_checks(account_team):
    with sqlite3.connect(paper_db_paths(account_team)["gate"]) as conn:
        conn.execute("UPDATE trades SET day='2026-09-18',signal_day='2026-09-17'")
    item = audit(account_team)["portfolios"]["gate"]
    assert "2026-09-18" in item["days"]


def test_unknown_market_state_is_not_mutated_or_fetched(account_team, monkeypatch):
    monkeypatch.setattr("trading_team.execution_bars.fetch_snapshot_tx", lambda *_: pytest.fail("network"))
    (account_team / "replay_data" / f"{DAY}.json").unlink()
    before = {str(p): p.read_bytes() for p in account_team.parent.rglob("*") if p.is_file()}
    item = audit(account_team)["portfolios"]["gate"]
    assert item["days"][DAY]["price_replay"] == "unavailable"
    assert {str(p): p.read_bytes() for p in account_team.parent.rglob("*") if p.is_file()} == before


def test_api_exposes_conflicts_and_uses_scoped_root(account_team, monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("reconcile_console", Path(__file__).parents[1] / "console/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", account_team.parent)
    handler = object.__new__(module.H)
    handler.path = "/api/paper-audit?_=test"
    output = []
    handler._send_json = lambda status, result: output.append((status, result))
    monkeypatch.setattr("http.server.SimpleHTTPRequestHandler.do_GET", lambda _: pytest.fail("API missing"))
    handler.do_GET()
    assert output[0][0] == 200
    assert output[0][1]["portfolios"]["gate"]["recorded_trade_count"] == 1


@pytest.mark.parametrize("timestamp", ["2026-09-21T15:30:00+08:00", "2026-09-21T08:00:00"])
def test_late_or_timezone_unknown_publication_is_not_forward_evidence(account_team, timestamp):
    path = account_team / "decisions" / DAY / "paper_decision.json"
    decision = json.loads(path.read_text())
    decision["materialized_at"] = timestamp
    path.write_text(json.dumps(decision))
    assert "publication_timing" in issues(audit(account_team)["portfolios"]["gate"])


def test_cli_returns_nonzero_for_incomplete_evidence(account_team, monkeypatch, capsys):
    from trading_team.paper_reconcile import main
    monkeypatch.setattr("sys.argv", ["paper_reconcile", "--team", str(account_team)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out)["portfolios"]["gate"]["execution_certified"] is False


def test_zero_order_receipt_without_balance_is_not_cash_consistent(account_team):
    with sqlite3.connect(paper_db_paths(account_team)["gate"]) as conn:
        conn.execute("INSERT INTO execution_receipts SELECT '2026-09-22',plan_hash,result_json,evidence_kind,created_at FROM execution_receipts")
    item = audit(account_team)["portfolios"]["gate"]
    assert item["checks"]["cash"] == "conflict"


def test_account_ownership_mismatch_is_reported(account_team):
    with sqlite3.connect(paper_db_paths(account_team)["gate"]) as conn:
        conn.execute("UPDATE trades SET signal_source='llm_no_gate'")
        conn.execute("UPDATE pending_orders SET signal_source='llm_no_gate'")
    assert "account_ownership" in issues(audit(account_team)["portfolios"]["gate"])


@pytest.mark.parametrize("legacy", [True, False])
def test_eligibility_drift_is_separate_from_plan_binding(account_team, legacy):
    from trading_team.paper_pipeline import _execution_inputs_hash

    decision_path = account_team / "decisions" / DAY / "paper_decision.json"
    decision = json.loads(decision_path.read_text())
    decision["portfolios"]["gate"]["plans"][0]["executable"] = False
    decision_path.write_text(json.dumps(decision))
    if legacy:
        with sqlite3.connect(paper_db_paths(account_team)["gate"]) as conn:
            previous = json.loads(conn.execute("SELECT result_json FROM execution_receipts").fetchone()[0])
            previous["execution_inputs"].pop("entry_contract", None)
            previous["execution_inputs_hash"] = _execution_inputs_hash(previous["execution_inputs"])
            conn.execute("UPDATE execution_receipts SET result_json=?", (json.dumps(previous),))
    item = audit(account_team)["portfolios"]["gate"]
    assert item["days"][DAY]["plan_binding"] == "matched"
    assert item["days"][DAY]["price_replay"] == "unavailable"
    code = "decision_policy_changed" if legacy else "decision_policy_conflict"
    assert code in issues(item)
    assert "plan_binding" not in issues(item)


@pytest.fixture
def exit_team(account_team, execution_cache):
    day = "2026-09-22"
    output = account_team / "outputs" / day
    output.mkdir()
    plan = {"date": day, "data_asof": DAY, "market_ok": False,
            "proposals": [{"code": "600036", "direction": "减持", "position_pct": 0,
                           "exit": {"type": "limit", "qty": 100, "price": 42,
                                    "time_in_force": "DAY"}}]}
    (output / "plan.json").write_text(json.dumps(plan))
    return account_team


def execute_exit_day(team, execution_cache, kind="limit", price=42):
    day = "2026-09-22"
    path = team / "outputs" / day / "plan.json"
    plan = json.loads(path.read_text())
    plan["proposals"][0]["exit"].update(type=kind, price=price)
    plan = build_reviewed_plan(day, DAY, False, plan["proposals"],
                               [{"code": "600036", "decision": "通过", "reason_codes": []}])
    path.write_text(json.dumps(plan))
    decision = materialize_paper_decision(day, team)
    bar = {"open": 40, "high": 41, "low": 39, "close": 40.5, "prev_close": 40.5}
    execution_cache(team, day, {code: bar for code in ("600036", "000300")})
    for name, db in paper_db_paths(team).items():
        _execute_portfolio(decision, name, db, {"600036": Bar(**bar)}, True)


def audit_exit(team, calendar):
    from trading_team.paper_reconcile import reconcile_accounts
    return reconcile_accounts(team=team, calendar_path=calendar, now=datetime(2026, 9, 23, 8))


@pytest.mark.parametrize("kind,price", [("limit", 42), ("limit", 40), ("stop", 39.5)])
def test_exit_replay_uses_intent_and_opening_inventory(exit_team, execution_cache, finalize_calendar, kind, price):
    execute_exit_day(exit_team, execution_cache, kind, price)
    result = audit_exit(exit_team, finalize_calendar)
    assert result["exit_contract"] == "a_share_exit.v1"
    assert result["portfolio_policy"] == "explicit_intent.v1"
    for item in result["portfolios"].values():
        assert item["conflict_count"] == 0
        assert item["days"]["2026-09-22"]["price_replay"] == "matched_recorded_quantity"
        assert item["days"]["2026-09-22"]["exit_inventory"] == "matched_ledger"
        assert item["execution_certified"] is False


@pytest.mark.parametrize("change,expected", [("missing", "exit_inventory_evidence"),
                                            ("quantity", "exit_inventory_conflict"),
                                            ("date", "exit_inventory_conflict")])
def test_unverified_exit_inventory_is_not_certified(exit_team, execution_cache, finalize_calendar, change, expected):
    execute_exit_day(exit_team, execution_cache)
    with sqlite3.connect(paper_db_paths(exit_team)["gate"]) as conn:
        previous = json.loads(conn.execute(
            "SELECT result_json FROM execution_receipts WHERE execution_day='2026-09-22'").fetchone()[0])
        pos = previous["sizing_basis"]["positions"]["600036"]
        if change == "missing":
            pos.pop("buy_day")
        elif change == "quantity":
            pos["qty"] += 100
        else:
            pos["buy_day"] = "2026-09-18"
        conn.execute("UPDATE execution_receipts SET result_json=? WHERE execution_day='2026-09-22'",
                     (json.dumps(previous),))
    item = audit_exit(exit_team, finalize_calendar)["portfolios"]["gate"]
    assert expected in issues(item)
    assert item["days"]["2026-09-22"]["price_replay"] == "unavailable"


def test_exit_quantity_is_checked_in_replay(exit_team, execution_cache, finalize_calendar):
    execute_exit_day(exit_team, execution_cache)
    with sqlite3.connect(paper_db_paths(exit_team)["gate"]) as conn:
        conn.execute("UPDATE pending_orders SET qty=200 WHERE execution_day='2026-09-22'")
    item = audit_exit(exit_team, finalize_calendar)["portfolios"]["gate"]
    mismatch = next(i for i in item["issues"] if i["code"] == "fill_replay" and i["date"] == "2026-09-22")
    assert mismatch["actual"]["qty"] == 200
    assert mismatch["expected"]["qty"] == 100


@pytest.mark.parametrize("field", ["inventory_basis", "sizing_basis"])
def test_acquisition_lot_tampering_is_not_matched_exit_evidence(exit_team, execution_cache, finalize_calendar, field):
    execute_exit_day(exit_team, execution_cache)
    with sqlite3.connect(paper_db_paths(exit_team)["gate"]) as conn:
        previous = json.loads(conn.execute(
            "SELECT result_json FROM execution_receipts WHERE execution_day='2026-09-22'").fetchone()[0])
        previous[field]["positions"]["600036"]["lots"][0]["qty"] += 100
        conn.execute("UPDATE execution_receipts SET result_json=? WHERE execution_day='2026-09-22'", (json.dumps(previous),))
    item = audit_exit(exit_team, finalize_calendar)["portfolios"]["gate"]
    assert "exit_inventory_conflict" in issues(item)
    assert item["days"]["2026-09-22"]["price_replay"] == "unavailable"


@pytest.mark.parametrize("legacy", [False, True])
def test_implicit_sale_never_receives_matched_exit_replay(account_team, legacy):
    from trading_team.paper_pipeline import _execution_inputs_hash

    with sqlite3.connect(paper_db_paths(account_team)["gate"]) as conn:
        conn.execute("UPDATE pending_orders SET side='sell'")
        if legacy:
            previous = json.loads(conn.execute("SELECT result_json FROM execution_receipts").fetchone()[0])
            previous["execution_inputs"].pop("portfolio_policy")
            previous["execution_inputs_hash"] = _execution_inputs_hash(previous["execution_inputs"])
            conn.execute("UPDATE execution_receipts SET result_json=?", (json.dumps(previous),))
    item = audit(account_team)["portfolios"]["gate"]
    issue = next(i for i in item["issues"] if i["code"] == "exit_intent_missing")
    assert issue["severity"] == ("missing" if legacy else "conflict")
    assert item["days"][DAY]["price_replay"] == "unavailable"


@pytest.mark.parametrize("legacy", [True, False])
def test_historical_buy_only_selection_is_not_a_plan_hash_conflict(account_team, legacy):
    from trading_team.paper_pipeline import _execution_inputs_hash

    plan_path = account_team / "outputs" / DAY / "plan.json"
    plan = json.loads(plan_path.read_text())
    for key in ("risk_contract", "trader_proposals", "risk_reviews"):
        plan.pop(key)
    plan["paper_proposals"].append({"code": "600519", "direction": "卖出", "position_pct": 0,
                                     "exit": {"type": "market", "qty": "all", "time_in_force": "DAY"}})
    plan_path.write_text(json.dumps(plan))
    plan_hash = canonical_plan_hash(plan)
    path = account_team / "decisions" / DAY / "paper_decision.json"
    decision = json.loads(path.read_text())
    decision["plan_hash"] = plan_hash
    if legacy:
        decision.pop("risk_policy")
        decision.pop("risk_contract")
    for item in decision["portfolios"].values():
        item["plans"][0]["approved_plan_hash"] = plan_hash
    path.write_text(json.dumps(decision))
    with sqlite3.connect(paper_db_paths(account_team)["gate"]) as conn:
        for table in ("pending_orders", "trades", "order_events", "execution_receipts"):
            conn.execute(f"UPDATE {table} SET plan_hash=?", (plan_hash,))
        if legacy:
            previous = json.loads(conn.execute("SELECT result_json FROM execution_receipts").fetchone()[0])
            previous["execution_inputs"].pop("portfolio_policy")
            previous["execution_inputs"].pop("risk_policy")
            previous["execution_inputs_hash"] = _execution_inputs_hash(previous["execution_inputs"])
            conn.execute("UPDATE execution_receipts SET result_json=?", (json.dumps(previous),))
    item = audit(account_team)["portfolios"]["gate"]
    if legacy:
        assert "plan_binding" not in issues(item)
        assert "decision_selection_changed" in issues(item)
        assert item["days"][DAY]["plan_binding"] == "matched"
    else:
        assert "plan_binding" in issues(item)
        assert "decision_selection_changed" not in issues(item)
    assert item["days"][DAY]["price_replay"] == "unavailable"
