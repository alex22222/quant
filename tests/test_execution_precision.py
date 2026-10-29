import json
import sqlite3
from decimal import Decimal

import pytest

from paper.execution_model import Bar, simulate_fill, simulate_limit_fill, total_fee


@pytest.mark.parametrize("explicit_limits", [False, True])
@pytest.mark.parametrize("side,expected", [("buy", 54.83), ("sell", 54.61)])
@pytest.mark.parametrize("path", ["open", "close", "limit"])
def test_execution_price_and_amount_share_the_same_cent_basis(path, side, expected, explicit_limits):
    bar = Bar(open=54.72, high=55.59, low=54.59, close=54.72, prev_close=54.72)
    if explicit_limits:
        bar.limit_up, bar.limit_down = 60.19, 49.25
    if path == "close":
        bar.open = None
    if path == "limit":
        fill = simulate_limit_fill(side, 137, 55.4 if side == "buy" else 54.6, bar, slippage_rate=0.002)
    else:
        fill = simulate_fill(side, 137, bar, slippage_rate=0.002)
    assert fill.filled
    assert fill.price == expected
    assert fill.qty == (100 if side == "buy" else 137)
    assert Decimal(str(fill.price)) % Decimal("0.01") == 0
    assert Decimal(str(fill.amount)) == fill.qty * Decimal(str(fill.price))
    assert fill.fee == total_fee(side, fill.amount)


@pytest.mark.parametrize("path", ["open", "limit"])
@pytest.mark.parametrize("side,price,expected", [
    ("buy", 0.2, 0.21), ("buy", 0.4, 0.42), ("sell", 1.4, 1.33), ("sell", 2.6, 2.47),
])
def test_float_multiplication_does_not_add_an_extra_tick(path, side, price, expected):
    upper = float(Decimal(str(price)) * Decimal("1.5"))
    lower = float(Decimal(str(price)) * Decimal("0.5"))
    bar = Bar(open=price, high=upper, low=lower, close=price, prev_close=price,
              limit_up=upper, limit_down=lower)
    if path == "limit":
        fill = simulate_limit_fill(side, 100, upper if side == "buy" else lower, bar, slippage_rate=0.05)
    else:
        fill = simulate_fill(side, 100, bar, slippage_rate=0.05)
    assert fill.filled
    assert fill.price == expected
    assert Decimal(str(fill.amount)) == 100 * Decimal(str(expected))


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("explicit_limits", [False, True])
def test_non_tick_limit_is_rejected_without_silently_rounding_the_order(side, explicit_limits):
    bar = Bar(open=10, high=10.5, low=9.5, close=10, prev_close=10)
    if explicit_limits:
        bar.limit_up, bar.limit_down = 11, 9
    fill = simulate_limit_fill(side, 100, 10.005, bar)
    assert fill.status == "rejected"
    assert "0.01" in fill.reject_reason


@pytest.mark.parametrize("side,limit", [("buy", 54.82), ("sell", 54.62)])
def test_cent_rounding_never_crosses_the_order_limit(side, limit):
    bar = Bar(open=54.72, high=55.59, low=54.59, close=54.72, prev_close=54.72)
    fill = simulate_limit_fill(side, 100, limit, bar, slippage_rate=0.002)
    assert fill.filled and fill.price == limit
    assert Decimal(str(fill.amount)) == 100 * Decimal(str(limit))


@pytest.mark.parametrize("path", ["open", "close", "limit"])
@pytest.mark.parametrize("price", [0, -1, float("nan"), float("inf")])
def test_invalid_execution_price_is_rejected_not_filled_or_unfilled(path, price):
    bar = Bar(open=10, high=11, low=9, close=10, prev_close=10)
    if path == "limit":
        fill = simulate_limit_fill("buy", 100, price, bar)
    else:
        bar.open = price if path == "open" else None
        bar.close = price if path == "close" else 10
        fill = simulate_fill("buy", 100, bar)
    assert fill.status == "rejected"
    assert not fill.filled and fill.amount == 0


@pytest.mark.parametrize("path", ["open", "limit"])
@pytest.mark.parametrize("slippage", [-0.002, float("nan"), float("inf"), 1])
def test_invalid_slippage_is_rejected(path, slippage):
    bar = Bar(open=10, high=11, low=9, close=10, prev_close=10)
    fill = (simulate_fill("buy", 100, bar, slippage_rate=slippage) if path == "open"
            else simulate_limit_fill("buy", 100, 10.5, bar, slippage_rate=slippage))
    assert fill.status == "rejected"
    assert not fill.filled


def test_sell_rounding_to_zero_is_rejected():
    fill = simulate_fill("sell", 100, Bar(open=0.01, close=0.01), slippage_rate=0.002)
    assert fill.status == "rejected"
    assert fill.amount == 0


def test_model_identity_changes_with_execution_semantics():
    from paper.execution_model import EXECUTION_MODEL_VERSION
    assert EXECUTION_MODEL_VERSION == "daily_bar_t1.v4"


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("path", ["open", "limit"])
def test_slipped_price_outside_observed_daily_range_cannot_fill(side, path):
    bar = Bar(open=10, high=10.01, low=9.99, close=10, prev_close=10, limit_up=11, limit_down=9)
    fill = (simulate_fill(side, 100, bar, slippage_rate=0.002) if path == "open" else
            simulate_limit_fill(side, 100, 10.5 if side == "buy" else 9.5, bar, slippage_rate=0.002))
    assert fill.status == "rejected"
    assert "日内" in fill.reject_reason


@pytest.fixture
def precision_workspace(tmp_path, execution_cache, finalize_calendar, stub_research_reviews):
    from trading_team.risk_contract import build_reviewed_plan
    team = tmp_path / "trading_team"
    output = team / "outputs/2026-09-21"
    output.mkdir(parents=True)
    proposal = {"code": "601318", "direction": "条件买入", "position_pct": 0.1,
                "entry": [55.4], "entry_type": "limit", "stop": 50, "confidence": 4}
    (output / "plan.json").write_text(json.dumps(build_reviewed_plan(
        "2026-09-21", "2026-09-18", False, [proposal],
        [{"code": "601318", "decision": "通过", "reason_codes": []}])))
    execution_cache(team, "2026-09-21", {
        "601318": {"open": 54.72, "high": 55.59, "low": 54.59, "close": 55.28, "prev_close": 54.72},
        "000300": {"open": 4550, "high": 4560, "low": 4540, "close": 4555, "prev_close": 4550},
    })
    return team


def test_versioned_finalize_replay_account_and_receipt_use_identical_prices(precision_workspace):
    from trading_team.daily_finalize import run_finalize_workflow
    from trading_team.paper_pipeline import paper_db_paths
    from trading_team.run_ledger import RunLedger

    team = precision_workspace
    ledger = RunLedger(team / ".runs/ledger.db")
    first = run_finalize_workflow("2026-09-21", team=team, ledger=ledger, paper=True)
    assert first["paper"]["gate"]["fills"] == 0
    assert first["paper"]["no_gate"]["fills"] == 1
    report = json.loads((team / "outputs/2026-09-21/counterfactual.json").read_text())
    order = report["portfolios"]["no_gate"]["orders"][0]
    assert (order["qty"], order["fill_price"], order["amount"], order["fee"]) == (100, 54.83, 5483.0, 5.11)
    assert report["execution_model"]["version"] == "daily_bar_t1.v4"
    paths = paper_db_paths(team)
    with sqlite3.connect(paths["no_gate"]) as conn:
        assert conn.execute("SELECT qty,price,amount,fee FROM trades").fetchone() == (100, 54.83, 5483.0, 5.11)
        assert conn.execute("SELECT cash FROM account WHERE day='2026-09-21'").fetchone()[0] == 94511.89
        receipt = json.loads(conn.execute("SELECT result_json FROM execution_receipts").fetchone()[0])
        assert receipt["execution_inputs"]["model"] == "daily_bar_t1.v4"
    before = {key: path.read_bytes() for key, path in paths.items()}
    assert run_finalize_workflow("2026-09-21", team=team, ledger=ledger, paper=True)["status"] == "reused"
    assert {key: path.read_bytes() for key, path in paths.items()} == before


def test_previous_model_receipt_is_visible_and_never_silently_recertified(precision_workspace):
    from trading_team.paper_pipeline import run_paper_day, paper_db_paths, _execution_inputs_hash
    from trading_team.paper_reconcile import reconcile_accounts

    team = precision_workspace
    run_paper_day("2026-09-21", team=team, confirm=True)
    paths = paper_db_paths(team)
    for path in paths.values():
        with sqlite3.connect(path) as conn:
            receipt = json.loads(conn.execute("SELECT result_json FROM execution_receipts").fetchone()[0])
            receipt["execution_inputs"]["model"] = "daily_bar_t1.v3"
            receipt["execution_inputs_hash"] = _execution_inputs_hash(receipt["execution_inputs"])
            conn.execute("UPDATE execution_receipts SET result_json=?", (json.dumps(receipt),))
    before = {key: path.read_bytes() for key, path in paths.items()}
    with pytest.raises(ValueError, match="execution evidence changed"):
        run_paper_day("2026-09-21", team=team, confirm=True)
    report = reconcile_accounts(team=team)
    for account in report["portfolios"].values():
        assert account["days"]["2026-09-21"]["recorded_model"] == "daily_bar_t1.v3"
        assert "execution_policy_changed" in {issue["code"] for issue in account["issues"]}
        assert account["execution_certified"] is False
    assert {key: path.read_bytes() for key, path in paths.items()} == before


@pytest.mark.parametrize("inputs", [None, []])
def test_bad_policy_receipt_does_not_hide_the_other_account(precision_workspace, inputs):
    from trading_team.paper_pipeline import run_paper_day, paper_db_paths, _execution_inputs_hash
    from trading_team.paper_reconcile import reconcile_accounts

    team = precision_workspace
    run_paper_day("2026-09-21", team=team, confirm=True)
    with sqlite3.connect(paper_db_paths(team)["gate"]) as conn:
        receipt = json.loads(conn.execute("SELECT result_json FROM execution_receipts").fetchone()[0])
        receipt.update(execution_inputs=inputs, execution_inputs_hash=_execution_inputs_hash(inputs))
        conn.execute("UPDATE execution_receipts SET result_json=?", (json.dumps(receipt),))
    report = reconcile_accounts(team=team)
    assert "receipt_conflict" in {issue["code"] for issue in report["portfolios"]["gate"]["issues"]}
    assert report["portfolios"]["no_gate"]["checks"]["cash"] == "matched"
