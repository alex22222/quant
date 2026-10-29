# -*- coding: utf-8 -*-
"""闸门组合 / 无闸门组合 / 沪深300 严格反事实。"""
from paper.execution_model import Bar
from trading_team.counterfactual import build_counterfactual


def test_untouched_limits_are_unfilled_and_have_no_opportunity_cost():
    plan = {
        "date": "2026-08-24", "data_asof": "2026-08-21", "market_ok": False,
        "proposals": [{
            "code": "601318", "name": "中国平安", "direction": "条件买入",
            "position_pct": 0.15, "entry": [53.0], "entry_type": "limit",
            "stop": 50.6,
        }],
    }
    bars = {
        "601318": Bar(open=53.34, high=55.22, low=53.30, close=54.92, prev_close=53.35),
        "000300": Bar(open=4600, high=4620, low=4550, close=4580, prev_close=4610),
    }
    report = build_counterfactual(plan, bars, initial_cash=100_000)

    gate_order = report["portfolios"]["gate"]["orders"][0]
    no_gate_order = report["portfolios"]["no_gate"]["orders"][0]
    assert gate_order["status"] == "blocked"
    assert no_gate_order["status"] == "unfilled"
    assert report["portfolios"]["gate"]["return"] == 0
    assert report["portfolios"]["no_gate"]["return"] == 0
    assert "opportunity_cost" not in report
    assert report["sample_type"] == "forward_eligible"


def test_touched_limit_uses_fee_slippage_and_board_lot():
    plan = {
        "date": "2026-08-25", "data_asof": "2026-08-24", "market_ok": True,
        "proposals": [{
            "code": "600036", "name": "招商银行", "direction": "条件买入",
            "position_pct": 0.10, "entry": [39.2], "entry_type": "limit",
            "stop": 37.9,
        }],
    }
    bars = {
        "600036": Bar(open=39.0, high=39.8, low=38.9, close=39.5, prev_close=38.9),
        "000300": Bar(open=4600, high=4620, low=4550, close=4580, prev_close=4610),
    }
    report = build_counterfactual(plan, bars, initial_cash=100_000, slippage_rate=0.002)
    order = report["portfolios"]["no_gate"]["orders"][0]
    assert order["status"] == "filled"
    assert order["qty"] % 100 == 0
    assert order["fee"] > 0
    assert order["fill_price"] > bars["600036"].open


def test_paper_candidates_are_capped_to_three_buys_and_ten_percent_each():
    proposals = []
    bars = {
        "000300": Bar(open=4600, high=4620, low=4550, close=4580, prev_close=4610),
    }
    for index, code in enumerate(("600900", "601318", "600036", "601899")):
        price = 20.0 + index
        proposals.append({
            "code": code, "name": code, "direction": "条件买入",
            "position_pct": 0.20, "entry": [price], "entry_type": "limit",
            "confidence": 4 - index,
        })
        bars[code] = Bar(
            open=price, high=price + 1, low=price - 1, close=price,
            prev_close=price - 0.2,
        )
    plan = {
        "date": "2026-09-03", "data_asof": "2026-09-02",
        "market_ok": True, "proposals": [], "paper_proposals": proposals,
    }
    report = build_counterfactual(plan, bars)
    orders = report["portfolios"]["no_gate"]["orders"]
    assert len(orders) == 3
    assert all(order["amount"] <= 10_000 for order in orders)


def test_same_day_plan_is_labeled_retrospective():
    report = build_counterfactual({
        "date": "2026-09-02", "data_asof": "2026-09-02",
        "market_ok": False, "proposals": [],
    }, {"000300": Bar(open=1, high=1, low=1, close=1, prev_close=1)})
    assert report["sample_type"] == "same_day_retrospective"


def test_unfilled_exit_retains_existing_inventory_pnl_and_labels_seed():
    from trading_team.counterfactual import render_markdown

    plan = {"date": "2026-09-21", "data_asof": "2026-09-18", "market_ok": False,
            "proposals": [{"code": "600036", "direction": "卖出", "position_pct": 0,
                           "exit": {"type": "limit", "qty": "all", "price": 12, "time_in_force": "DAY"}}]}
    bars = {"600036": Bar(open=10, high=11, low=9, close=11, prev_close=10)}
    report = build_counterfactual(plan, bars, initial_cash=95000,
                                  initial_positions={"600036": {"qty": 500, "buy_day": "2026-09-18"}})
    for item in report["portfolios"].values():
        if "orders" in item:
            assert item["orders"][0]["status"] == "unfilled"
            assert item["equity"] == 100500 and item["return"] == .005
    assert "显式提供的现金与持仓" in render_markdown(report)
    cash_only = build_counterfactual(plan, bars)
    assert cash_only["portfolios"]["gate"]["orders"][0]["status"] == "rejected"
    assert "每日现金重置" in render_markdown(cash_only)
