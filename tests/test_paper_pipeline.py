# -*- coding: utf-8 -*-
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from paper.execution_model import Bar
from trading_team.decision_pipeline import materialize_decision
from trading_team.paper_pipeline import (
    _db,
    _execute_portfolio,
    account_snapshot,
    dual_paper_snapshot,
    materialize_paper_decision,
    record_current_valuations,
    run_paper_day,
)
from trading_team.paper_plans import derive_paper_proposals
from trading_team.risk_contract import apply_risk_reviews, build_reviewed_plan

pytestmark = pytest.mark.usefixtures("stub_research_reviews")


def _proposal(code, pct=0.12, confidence=4):
    price = {"600900": 28.3, "601318": 56.0, "600036": 40.2,
             "601899": 31.8}[code]
    return {
        "code": code, "name": code, "direction": "条件买入",
        "position_pct": pct, "entry": [price], "entry_type": "limit",
        "stop": price * 0.94, "confidence": confidence,
    }


def _write_plan(team, market_ok=False):
    out = team / "outputs" / "2026-09-03"
    out.mkdir(parents=True)
    candidates = [
        _proposal("600900"), _proposal("601318"),
        _proposal("600036", 0.10), _proposal("601899", 0.08, 3),
    ]
    reviews = [{"code": p["code"], "decision": "通过" if market_ok else "否决",
                "reason_codes": [] if market_ok else ["market_gate"], "reason": "market review"}
               for p in candidates]
    plan = build_reviewed_plan("2026-09-03", "2026-09-02", market_ok, candidates, reviews)
    (out / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8",
    )
    return plan


def test_paper_auto_approval_is_separate_hash_bound_and_capped(tmp_path):
    team = tmp_path / "trading_team"
    _write_plan(team, market_ok=False)

    live = materialize_decision("2026-09-03", team)
    paper = materialize_paper_decision("2026-09-03", team)

    assert live["pm_status"] == "pending"
    assert all(p["approval"] == "pending" for p in live["plans"])
    assert paper["plan_hash"] == live["plan_hash"]
    assert paper["approval_mode"] == "paper_auto"
    assert paper["eligible_execution_day"] == "2026-09-03"

    gate = paper["portfolios"]["gate"]
    no_gate = paper["portfolios"]["no_gate"]
    assert len(gate["plans"]) == len(no_gate["plans"]) == 3
    assert all(p["position_pct"] <= 0.10 for p in no_gate["plans"])
    assert all(p["approved_plan_hash"] == paper["plan_hash"] for p in no_gate["plans"])
    assert all(p["executable"] is False for p in gate["plans"])
    assert all(p["executable"] is True for p in no_gate["plans"])


def test_plan_change_invalidates_paper_auto_approval_version(tmp_path):
    team = tmp_path / "trading_team"
    plan = _write_plan(team)
    first = materialize_paper_decision("2026-09-03", team)
    plan["trader_proposals"][0]["entry"] = [27.9]
    plan = build_reviewed_plan(plan["date"], plan["data_asof"], plan["market_ok"],
                               plan["trader_proposals"], plan["risk_reviews"])
    (team / "outputs" / "2026-09-03" / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8",
    )
    second = materialize_paper_decision("2026-09-03", team)
    assert second["plan_hash"] != first["plan_hash"]
    assert all(
        p["approved_plan_hash"] == second["plan_hash"]
        for p in second["portfolios"]["no_gate"]["plans"]
    )


def test_dual_paper_accounts_execute_without_live_approval(tmp_path, monkeypatch):
    team = tmp_path / "trading_team"
    _write_plan(team, market_ok=False)
    materialize_paper_decision("2026-09-03", team)
    bars = {
        "600900": Bar(open=28.2, high=28.5, low=28.0, close=28.4, prev_close=28.1),
        "601318": Bar(open=55.8, high=56.2, low=55.5, close=56.1, prev_close=55.7),
        "600036": Bar(open=40.0, high=40.5, low=39.8, close=40.3, prev_close=39.9),
        "000300": Bar(open=4550, high=4580, low=4530, close=4570, prev_close=4548),
    }
    monkeypatch.setattr(
        "trading_team.paper_pipeline.load_execution_bars",
        lambda day, codes, root: {code: bars[code] for code in [*codes, "000300"]},
    )
    db_paths = {
        "gate": tmp_path / "paper" / "paper_gate.db",
        "no_gate": tmp_path / "paper" / "paper_no_gate.db",
    }
    result = run_paper_day(
        "2026-09-03", team=team, db_paths=db_paths, confirm=True,
    )
    assert result["gate"]["fills"] == 0
    assert result["no_gate"]["fills"] == 3

    gate = sqlite3.connect(db_paths["gate"])
    no_gate = sqlite3.connect(db_paths["no_gate"])
    assert gate.execute(
        "SELECT COUNT(*) FROM pending_orders WHERE status='blocked'"
    ).fetchone()[0] == 3
    assert gate.execute("SELECT COUNT(*) FROM order_events").fetchone()[0] == 3
    assert gate.execute(
        "SELECT portfolio,equity,missing_marks_json FROM account_valuations"
    ).fetchone() == ("gate", 100000.0, "[]")
    assert no_gate.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3
    assert no_gate.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 3
    assert no_gate.execute("SELECT COUNT(*) FROM order_events").fetchone()[0] == 3
    valuation = no_gate.execute(
        "SELECT portfolio,valuation_day,quote_asof,equity,position_count "
        "FROM account_valuations"
    ).fetchone()
    assert valuation[0:3] == ("no_gate", "2026-09-03", "2026-09-03")
    assert valuation[3] is not None
    assert valuation[4] == 3
    gate.close()
    no_gate.close()


def test_same_day_close_generated_plan_is_not_backfilled(tmp_path):
    team = tmp_path / "trading_team"
    plan = _write_plan(team)
    plan["date"] = plan["data_asof"] = "2026-09-03"
    out = team / "outputs" / "2026-09-03" / "plan.json"
    out.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    paper = materialize_paper_decision("2026-09-03", team)
    assert paper["eligible_execution_day"] is None


def test_only_gate_rejections_are_restored_for_paper():
    raw = [_proposal("600900"), _proposal("601318")]
    final = apply_risk_reviews(raw, [
        {"code": "600900", "decision": "否决", "reason_codes": ["market_gate"], "reason": "市场闸门关闭"},
        {"code": "601318", "decision": "否决", "reason_codes": ["thesis_invalidated"], "reason": "基本面失效"},
    ], market_ok=False)
    restored = derive_paper_proposals(raw, final, market_ok=False)
    assert restored[0]["direction"] == "条件买入"
    assert restored[1]["direction"] == "回避"


def test_zero_order_execution_is_committed_once(tmp_path):
    path = tmp_path / "empty.db"
    decision = {
        "plan_hash": "empty-v1", "data_asof": "2026-09-02",
        "eligible_execution_day": "2026-09-03",
        "portfolios": {"gate": {"plans": []}},
    }
    first = _execute_portfolio(decision, "gate", path, {}, True)
    second = _execute_portfolio(decision, "gate", path, {}, True)
    assert first == {"status": "succeeded", "orders": 0, "fills": 0}
    assert second["status"] == "skipped"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_receipts").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["price", "missing", "model", "fees", "legacy"])
def test_settlement_receipt_cannot_recertify_changed_execution_inputs(tmp_path, monkeypatch, change):
    from trading_team import paper_pipeline

    path = tmp_path / "receipt.db"
    decision = {
        "plan_hash": "v1", "data_asof": "2026-09-02",
        "eligible_execution_day": "2026-09-03",
        "portfolios": {"gate": {"plans": []}},
    }
    bars = {"600036": Bar(open=40, high=41, low=39, close=40.5, prev_close=40)}
    _execute_portfolio(decision, "gate", path, bars, True)
    if change == "price":
        bars["600036"] = replace(bars["600036"], close=40.6)
    elif change == "missing":
        bars.clear()
    elif change == "model":
        monkeypatch.setattr(paper_pipeline, "EXECUTION_MODEL_VERSION", "changed", raising=False)
    elif change == "fees":
        monkeypatch.setattr(paper_pipeline, "SLIPPAGE_RATE", 0.05)
    else:
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE execution_receipts SET result_json=?", (
                json.dumps({"status": "succeeded", "orders": 0, "fills": 0}),
            ))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="execution evidence|reconcile"):
        _execute_portfolio(decision, "gate", path, bars, True)
    assert path.read_bytes() == before


def test_receipt_binds_bar_inputs_and_allows_unrelated_cache_extension(tmp_path):
    path = tmp_path / "receipt.db"
    decision = {
        "plan_hash": "v1", "data_asof": "2026-09-02",
        "eligible_execution_day": "2026-09-03",
        "portfolios": {"gate": {"plans": []}},
    }
    bars = {"600036": Bar(open=40, high=41, low=39, close=40.5, prev_close=40)}
    _execute_portfolio(decision, "gate", path, bars, True)
    with sqlite3.connect(path) as conn:
        stored = json.loads(conn.execute("SELECT result_json FROM execution_receipts").fetchone()[0])
    assert stored["execution_inputs"]["bars"]["600036"]["close"] == 40.5
    assert len(stored["execution_inputs_hash"]) == 64
    bars["600900"] = Bar(open=28, close=28, prev_close=28)
    assert _execute_portfolio(decision, "gate", path, bars, True)["status"] == "skipped"


def test_legacy_orders_without_input_receipt_require_reconciliation(tmp_path):
    path = tmp_path / "legacy.db"
    conn = _db(path)
    conn.execute("INSERT INTO pending_orders(execution_day,plan_hash,status) VALUES ('2026-09-03','v1','unfilled')")
    conn.commit()
    conn.close()
    decision = {
        "plan_hash": "v1", "data_asof": "2026-09-02",
        "eligible_execution_day": "2026-09-03",
        "portfolios": {"gate": {"plans": []}},
    }
    with pytest.raises(ValueError, match="reconcile"):
        _execute_portfolio(decision, "gate", path, {}, True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_receipts").fetchone()[0] == 0


def test_retry_loads_input_bars_for_positions_already_sold(tmp_path, monkeypatch):
    team = tmp_path / "trading_team"
    _write_plan(team, market_ok=False)
    plan_path = team / "outputs" / "2026-09-03" / "plan.json"
    plan = json.loads(plan_path.read_text())
    plan["trader_proposals"].append({"code": "600111", "direction": "卖出", "position_pct": 0,
                              "exit": {"type": "market", "qty": "all", "time_in_force": "DAY"}})
    plan["risk_reviews"].append({"code": "600111", "decision": "通过", "reason_codes": []})
    plan = build_reviewed_plan(plan["date"], plan["data_asof"], plan["market_ok"],
                               plan["trader_proposals"], plan["risk_reviews"])
    plan_path.write_text(json.dumps(plan))
    paths = {name: tmp_path / f"{name}.db" for name in ("gate", "no_gate")}
    for path in paths.values():
        conn = _db(path)
        conn.execute("INSERT INTO positions VALUES ('600111',100,20,'2026-09-02')")
        conn.execute("INSERT INTO trades(day,code,side,qty) VALUES ('2026-09-02','600111','buy',100)")
        conn.commit()
        conn.close()
    requested_codes = []

    def load(_day, codes, _team):
        requested_codes.append(set(codes))
        return {code: Bar(open=20, high=21, low=19, close=20.5, prev_close=20) for code in codes}

    monkeypatch.setattr("trading_team.paper_pipeline.load_execution_bars", load)
    run_paper_day("2026-09-03", team=team, db_paths=paths)
    with sqlite3.connect(paths["no_gate"]) as conn:
        assert conn.execute("SELECT qty FROM positions WHERE code='600111'").fetchone() is None
        assert conn.execute("SELECT qty FROM trades WHERE code='600111' AND side='sell'").fetchone() == (100,)
    assert run_paper_day("2026-09-03", team=team, db_paths=paths)["no_gate"]["status"] == "skipped"
    assert "600111" in requested_codes[-1]


def test_settled_day_cannot_be_reexecuted_with_another_plan(tmp_path):
    path = tmp_path / "empty.db"
    decision = {
        "plan_hash": "v1", "data_asof": "2026-09-02",
        "eligible_execution_day": "2026-09-03",
        "portfolios": {"gate": {"plans": []}},
    }
    _execute_portfolio(decision, "gate", path, {}, True)
    decision["plan_hash"] = "v2"
    with pytest.raises(ValueError, match="already settled"):
        _execute_portfolio(decision, "gate", path, {}, True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT plan_hash FROM execution_receipts").fetchone()[0] == "v1"


def test_execution_cannot_apply_an_older_day_to_current_positions(tmp_path):
    path = tmp_path / "account.db"
    conn = _db(path)
    conn.execute("INSERT INTO account VALUES ('2026-09-07',90000,100000,'later state')")
    conn.commit()
    conn.close()
    decision = {
        "plan_hash": "v1", "data_asof": "2026-09-02",
        "eligible_execution_day": "2026-09-03",
        "portfolios": {"gate": {"plans": []}},
    }
    with pytest.raises(ValueError, match="already settled"):
        _execute_portfolio(decision, "gate", path, {}, True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT cash FROM account ORDER BY rowid DESC LIMIT 1").fetchone()[0] == 90000


def test_concurrent_execution_commits_one_receipt(tmp_path):
    path = tmp_path / "concurrent.db"
    _db(path).close()
    decision = {
        "plan_hash": "v1", "data_asof": "2026-09-02",
        "eligible_execution_day": "2026-09-03",
        "portfolios": {"gate": {"plans": []}},
    }
    barrier = Barrier(2)

    def execute():
        barrier.wait(timeout=5)
        return _execute_portfolio(decision, "gate", path, {}, True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(execute) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(item["status"] for item in results) == ["skipped", "succeeded"]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_receipts").fetchone()[0] == 1


def test_partial_execution_rolls_back_before_receipt(tmp_path, monkeypatch):
    path = tmp_path / "rollback.db"
    decision = {
        "plan_hash": "v1", "data_asof": "2026-09-02",
        "eligible_execution_day": "2026-09-03",
        "portfolios": {"gate": {"plans": []}},
    }

    def fail(*_args):
        raise RuntimeError("valuation write interrupted")

    monkeypatch.setattr("trading_team.paper_pipeline._valuation_row", fail)
    with pytest.raises(RuntimeError, match="valuation write interrupted"):
        _execute_portfolio(decision, "gate", path, {}, True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT day FROM account").fetchall() == [("init",)]
        assert conn.execute("SELECT COUNT(*) FROM execution_receipts").fetchone()[0] == 0


def test_dual_paper_snapshot_exposes_positions_orders_and_trades(tmp_path, execution_cache, finalize_calendar):
    gate_path = tmp_path / "gate.db"
    no_gate_path = tmp_path / "no_gate.db"
    conn = _db(no_gate_path)
    conn.execute("INSERT OR REPLACE INTO account VALUES ('2026-09-21',90000,101000,'test')")
    conn.execute("INSERT INTO positions VALUES ('600036',200,40.5,'2026-09-21')")
    conn.execute(
        """
        INSERT INTO pending_orders(
            signal_day,execution_day,code,side,qty,planned_price,signal_source,
            status,reason,plan_hash,fee,fill_price
        ) VALUES ('2026-09-20','2026-09-21','600036','buy',200,40.2,
                  'llm_no_gate','filled','','plan-v1',5,40.5)
        """
    )
    conn.execute(
        """
        INSERT INTO order_events(
            portfolio,order_id,event_type,event_at,signal_day,execution_day,
            code,side,qty,planned_price,status,reason,plan_hash,fee,fill_price
        ) VALUES ('no_gate',1,'order_finalized','2026-09-21T15:00:00+08:00',
                  '2026-09-20','2026-09-21','600036','buy',200,40.2,
                  'filled','','plan-v1',5,40.5)
        """
    )
    conn.execute(
        """
        INSERT INTO account_valuations(
            portfolio,valuation_day,quote_asof,cash,market_value,equity,
            recorded_equity,cost_exposure,unrealized_pnl,position_count,
            missing_marks_json,positions_json,plan_hash,source,created_at
        ) VALUES ('no_gate','2026-09-21','2026-09-18',90000,8400,98400,
                  101000,8100,300,1,'[]','[]','plan-v1','completed_close',
                  '2026-09-21T15:00:00+08:00')
        """
    )
    conn.execute(
        """
        INSERT INTO trades(day,signal_day,code,side,qty,price,amount,fee,
                           signal_source,plan_hash,planned_price,price_type)
        VALUES ('2026-09-21','2026-09-20','600036','buy',200,40.5,8100,5,
                'llm_no_gate','plan-v1',40.2,'limit')
        """
    )
    conn.commit()
    conn.close()

    team = tmp_path / "trading_team"
    context = team / "context" / "2026-09-21"
    context.mkdir(parents=True)
    (team / "watchlist.json").write_text(json.dumps({
        "stocks": [{"code": "600036", "sector": "银行"}],
        "benchmark": {"code": "000300"},
    }), encoding="utf-8")
    (context / "manifest.json").write_text(json.dumps({
        "ok": True,
        "latest_trade_date": "2026-09-18",
    }), encoding="utf-8")
    (context / "technical_600036.json").write_text(json.dumps({
        "as_of": "2026-09-18",
        "close": 42.0,
    }), encoding="utf-8")

    execution_cache(team, "2026-09-21", {"600036": {
        "open": 40, "high": 42, "low": 39, "close": 42, "prev_close": 40,
    }})
    snapshot = dual_paper_snapshot(
        {"gate": gate_path, "no_gate": no_gate_path},
        team=team,
        as_of_day="2026-09-21",
    )
    no_gate = snapshot["portfolios"]["no_gate"]

    assert snapshot["portfolios"]["gate"]["exists"] is False
    assert snapshot["portfolios"]["gate"]["schema_version"] == "paper_account.v1"
    assert no_gate["schema_version"] == "paper_account.v1"
    assert no_gate["portfolio"] == "no_gate"
    assert no_gate["cash"] == 90000
    assert no_gate["positions"][0]["code"] == "600036"
    assert no_gate["order_counts"] == {"filled": 1}
    assert no_gate["recent_trades"][0]["plan_hash"] == "plan-v1"
    assert no_gate["cost_exposure"] == 8100
    assert no_gate["market_exposure"] == 8400
    assert no_gate["valuation_equity"] == 98400
    assert no_gate["unrealized_pnl"] == 300
    assert no_gate["quote_asof"] == "2026-09-21"
    assert no_gate["positions"][0]["sector"] == "银行"
    assert no_gate["positions"][0]["frozen_qty"] == 200
    assert no_gate["sector_exposure"] == [{
        "sector": "银行",
        "market_value": 8400.0,
        "equity_pct": 0.085366,
    }]
    assert no_gate["account"]["valuation_equity"] == 98400
    assert no_gate["orders"]["counts"] == {"filled": 1}
    assert no_gate["orders"]["events"][0]["event_type"] == "order_finalized"
    assert no_gate["valuation_history"][0]["quote_asof"] == "2026-09-18"
    assert no_gate["valuation_history"][0]["missing_marks"] == []


def test_account_snapshot_reports_invalid_database_without_guessing(tmp_path):
    path = tmp_path / "invalid.db"
    sqlite3.connect(path).close()
    snapshot = account_snapshot(path)
    assert snapshot["exists"] is True
    assert snapshot["equity"] is None
    assert "error" in snapshot


def test_account_snapshot_does_not_value_missing_marks_at_cost(tmp_path):
    path = tmp_path / "paper.db"
    conn = _db(path)
    conn.execute("INSERT OR REPLACE INTO account VALUES ('2026-09-21',90000,98100,'test')")
    conn.execute("INSERT INTO positions VALUES ('600036',200,40.5,'2026-09-20')")
    conn.commit()
    conn.close()

    snapshot = account_snapshot(
        path,
        valuation={
            "source_date": "2026-09-21",
            "quote_asof": "2026-09-18",
            "prices": {},
        },
        as_of_day="2026-09-21",
        sectors={"600036": "银行"},
    )

    assert snapshot["known_market_exposure"] == 0
    assert snapshot["market_exposure"] is None
    assert snapshot["valuation_equity"] is None
    assert snapshot["unrealized_pnl"] is None
    assert snapshot["missing_marks"] == ["600036"]


def test_record_current_valuations_is_forward_only_and_idempotent(tmp_path, execution_cache, finalize_calendar):
    paths = {
        "gate": tmp_path / "gate.db",
        "no_gate": tmp_path / "no_gate.db",
    }
    conn = _db(paths["no_gate"])
    conn.execute("INSERT OR REPLACE INTO account VALUES ('2026-09-17',90000,98100,'test')")
    conn.execute("INSERT INTO positions VALUES ('600036',200,40.5,'2026-09-17')")
    conn.execute(
        """
        INSERT INTO pending_orders(
            signal_day,execution_day,code,side,qty,planned_price,signal_source,
            status,reason,plan_hash,fee,fill_price
        ) VALUES ('2026-09-16','2026-09-17','600036','buy',200,40.2,
                  'llm_no_gate','filled','','plan-v1',5,40.5)
        """
    )
    conn.commit()
    conn.close()

    team = tmp_path / "trading_team"
    context = team / "context" / "2026-09-21"
    context.mkdir(parents=True)
    (team / "watchlist.json").write_text(json.dumps({
        "stocks": [{"code": "600036", "sector": "银行"}],
        "benchmark": {"code": "000300"},
    }), encoding="utf-8")
    (context / "manifest.json").write_text(json.dumps({
        "ok": True,
        "latest_trade_date": "2026-09-18",
    }), encoding="utf-8")
    (context / "technical_600036.json").write_text(json.dumps({
        "as_of": "2026-09-18",
        "close": 42.0,
    }), encoding="utf-8")

    execution_cache(team, "2026-09-18", {"600036": {
        "open": 40, "high": 42, "low": 39, "close": 42, "prev_close": 40,
    }})
    first = record_current_valuations(paths, team=team, as_of_day="2026-09-21")
    second = record_current_valuations(paths, team=team, as_of_day="2026-09-21")

    assert first["quote_asof"] == "2026-09-18"
    assert first["portfolios"]["gate"]["status"] == "skipped"
    assert first["portfolios"]["no_gate"]["status"] == "inserted"
    assert first["portfolios"]["no_gate"]["order_events_imported"] == 1
    assert second["portfolios"]["no_gate"]["status"] == "reused"
    assert second["portfolios"]["no_gate"]["order_events_imported"] == 0
    conn = sqlite3.connect(paths["no_gate"])
    row = conn.execute(
        "SELECT valuation_day,quote_asof,equity,source FROM account_valuations"
    ).fetchone()
    event = conn.execute(
        "SELECT event_type,execution_day,status FROM order_events"
    ).fetchone()
    conn.close()
    assert row == ("2026-09-18", "2026-09-18", 98400.0,
                   "validated_unadjusted_close.v1")
    assert event == ("legacy_import", "2026-09-17", "filled")
