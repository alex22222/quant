import json
import sqlite3
from copy import deepcopy

import pytest

from paper.execution_model import Bar
from trading_team.exit_contract import simulate_exit
from trading_team.paper_pipeline import _db, _execute_portfolio, account_snapshot

DAY = '2026-09-21'


def trade(identity, day, side, qty):
    return {'id': identity, 'day': day, 'code': '600036', 'side': side, 'qty': qty}


def trades():
    return [trade(1, '2026-09-18', 'buy', 500), trade(2, DAY, 'buy', 200)]


def position():
    return {'qty': 700, 'buy_day': DAY, 'cost': 10,
            'lots': [{'trade_id': 1, 'buy_day': '2026-09-18', 'qty': 500},
                     {'trade_id': 2, 'buy_day': DAY, 'qty': 200}]}


def plan(qty=300):
    return {'code': '600036', 'direction': '减持', 'position_pct': 0,
            'exit': {'type': 'market', 'qty': qty, 'time_in_force': 'DAY'}, 'executable': True}


def bar():
    return Bar(open=10, high=11, low=9, close=10, prev_close=10)


def seed(path, records=None, *, qty=700, buy_day=DAY):
    conn = _db(path)
    with conn:
        conn.execute("INSERT INTO positions VALUES ('600036',?,10,?)", (qty, buy_day))
        for row in records if records is not None else trades():
            conn.execute('INSERT INTO trades(id,day,code,side,qty) VALUES (:id,:day,:code,:side,:qty)', row)
    conn.close()


def test_today_addition_does_not_freeze_previous_lots():
    original = position()
    fill, _ = simulate_exit(plan(500), original, bar(), DAY, slippage_rate=.002)
    assert fill.filled and fill.qty == 500
    assert original == position()


def test_exit_never_silently_shrinks_to_available_quantity():
    for instruction in (plan(600), {**plan(), 'direction': '卖出', 'exit': {'type': 'market', 'qty': 'all', 'time_in_force': 'DAY'}}):
        fill, _ = simulate_exit(instruction, position(), bar(), DAY, slippage_rate=.002)
        assert not fill.filled and 'T+1' in fill.reject_reason


def test_fifo_uses_only_older_lots_and_keeps_cost():
    from trading_team.paper_inventory import consume_position
    value = position()
    consume_position(value, 300, DAY)
    assert value['qty'] == 400 and value['cost'] == 10
    assert value['lots'] == [{'trade_id': 1, 'buy_day': '2026-09-18', 'qty': 200},
                             {'trade_id': 2, 'buy_day': DAY, 'qty': 200}]


def test_ledger_reconstructs_residual_lots_and_stable_hash():
    from trading_team.paper_inventory import inventory_basis
    records = trades() + [trade(3, DAY, 'sell', 300)]
    before = deepcopy(records)
    result = inventory_basis(records, DAY)
    assert result == inventory_basis(records, DAY)
    assert records == before
    assert result['positions']['600036']['qty'] == 400
    assert [lot['qty'] for lot in result['positions']['600036']['lots']] == [200, 200]
    assert result['policy'] == 'fifo_acquisition.v1'


@pytest.mark.parametrize('records', [
    trades() + [trade(3, DAY, 'sell', 600)],
    [trade(1, DAY, 'buy', 200), trade(2, DAY, 'sell', 100)],
    [trade(1, 'invalid', 'buy', 100)],
    [trade(1, DAY, 'buy', True)],
    [trade(1, DAY, 'buy', 100.5)],
    [trade(1, DAY, 'buy', -100)],
    [trade(1, DAY, 'buy', float('inf'))],
    [trade(1, DAY, 'transfer', 100)],
    [trade(1, DAY, 'buy', 200), trade(1, DAY, 'buy', 100)],
    [trade(2, DAY, 'buy', 200), trade(1, DAY, 'buy', 100)],
    [trade(1, DAY, 'buy', 200), trade(2, '2026-09-18', 'buy', 100)],
    [trade(1, '2026-09-22', 'buy', 200)],
])
def test_invalid_or_oversold_ledger_cannot_become_inventory(records):
    from trading_team.paper_inventory import inventory_basis
    with pytest.raises(ValueError, match='inventory'):
        inventory_basis(records, DAY)


@pytest.mark.parametrize('change', ['missing', 'quantity', 'duplicate', 'future'])
def test_malformed_lots_fail_closed(change):
    value = position()
    if change == 'missing':
        value['lots'] = []
    elif change == 'quantity':
        value['lots'][0]['qty'] = 900
    elif change == 'duplicate':
        value['lots'][1]['trade_id'] = 1
    else:
        value['lots'][0]['buy_day'] = '2026-09-22'
    fill, _ = simulate_exit(plan(), value, bar(), DAY, slippage_rate=.002)
    assert not fill.filled and fill.fee == 0


def test_snapshot_reports_split_inventory_read_only(tmp_path):
    path = tmp_path / 'account.db'
    seed(path)
    before = path.read_bytes()
    result = account_snapshot(path, as_of_day=DAY, valuation={'quote_asof': DAY, 'prices': {'600036': 10}})
    row = result['positions'][0]
    assert (row['available_qty'], row['frozen_qty']) == (500, 200)
    assert result['frozen_market_value'] == 2000
    assert result['inventory']['status'] == 'matched_trade_ledger'
    assert result['inventory']['execution_certified'] is False
    assert path.read_bytes() == before


def test_missing_ledger_is_unknown_not_all_available(tmp_path):
    path = tmp_path / 'account.db'
    seed(path, [], qty=500, buy_day='2026-09-18')
    before = path.read_bytes()
    result = account_snapshot(path, as_of_day=DAY)
    assert result['positions'][0]['available_qty'] is None
    assert result['positions'][0]['frozen_qty'] is None
    assert result['inventory']['status'] == 'unverified'
    assert path.read_bytes() == before


def test_missing_mark_does_not_turn_frozen_market_value_into_zero(tmp_path):
    path = tmp_path / 'account.db'
    seed(path)
    result = account_snapshot(path, as_of_day=DAY)
    assert result['frozen_qty'] == 200
    assert result['frozen_market_value'] is None


def test_execution_refuses_unreconstructable_opening_inventory(tmp_path):
    path = tmp_path / 'account.db'
    seed(path, [], qty=500, buy_day='2026-09-18')
    before = path.read_bytes()
    decision = {'eligible_execution_day': DAY, 'data_asof': '2026-09-18', 'plan_hash': 'fixture',
                'portfolios': {'no_gate': {'plans': [plan()]}}}
    with pytest.raises(ValueError, match='inventory'):
        _execute_portfolio(decision, 'no_gate', path, {'600036': bar()}, True)
    assert path.read_bytes() == before


def test_receipt_pins_opening_lots_and_retry_detects_ledger_change(tmp_path):
    path = tmp_path / 'account.db'
    seed(path, [trade(1, '2026-09-17', 'buy', 500), trade(2, '2026-09-18', 'buy', 200)], buy_day='2026-09-18')
    decision = {'eligible_execution_day': DAY, 'data_asof': '2026-09-18', 'plan_hash': 'fixture',
                'portfolios': {'no_gate': {'plans': [plan()]}}}
    assert _execute_portfolio(decision, 'no_gate', path, {'600036': bar()}, True)['fills'] == 1
    with sqlite3.connect(path) as conn:
        receipt = json.loads(conn.execute('SELECT result_json FROM execution_receipts').fetchone()[0])
        assert receipt['execution_inputs']['inventory_policy'] == 'fifo_acquisition.v1'
        assert [lot['qty'] for lot in receipt['inventory_basis']['positions']['600036']['lots']] == [500, 200]
    before = path.read_bytes()
    assert _execute_portfolio(decision, 'no_gate', path, {'600036': bar()}, True)['status'] == 'skipped'
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE trades SET day='2026-09-16' WHERE id=1")
    before = path.read_bytes()
    with pytest.raises(ValueError, match='inventory'):
        _execute_portfolio(decision, 'no_gate', path, {'600036': bar()}, True)
    assert path.read_bytes() == before


def test_audit_does_not_freeze_old_lots_after_same_day_buy():
    from trading_team.paper_reconcile import _arithmetic
    from paper.execution_model import total_fee
    facts = {'positions': [{'code': '600036', 'qty': 400, 'avg_cost': 10.01469, 'buy_day': DAY}],
             'trades': [], 'account': [{'day': 'init', 'cash': 100000, 'equity': 100000}],
             'pending_orders': [], 'order_events': [], 'execution_receipts': []}
    cash = 100000
    for row in trades() + [trade(3, DAY, 'sell', 300)]:
        amount = row['qty'] * 10
        fee = total_fee(row['side'], amount)
        facts['trades'].append({**row, 'price': 10, 'amount': amount, 'fee': fee, 'signal_day': '2026-09-17'})
        cash += -amount-fee if row['side'] == 'buy' else amount-fee
        facts['account'].append({'day': row['day'], 'cash': round(cash, 2), 'equity': 100000})
    issues = []
    _arithmetic(facts, issues)
    assert not any(issue['code'] == 'sell_inventory_t1' for issue in issues)


@pytest.mark.parametrize('change', ['position', 'same_day_trade', 'opening_receipt'])
def test_retry_requires_current_inventory_and_original_receipt(tmp_path, change):
    path = tmp_path / 'account.db'
    seed(path, [trade(1, '2026-09-18', 'buy', 700)], buy_day='2026-09-18')
    decision = {'eligible_execution_day': DAY, 'data_asof': '2026-09-18', 'plan_hash': 'fixture',
                'portfolios': {'no_gate': {'plans': [plan()]}}}
    _execute_portfolio(decision, 'no_gate', path, {'600036': bar()}, True)
    with sqlite3.connect(path) as conn:
        if change == 'position':
            conn.execute('UPDATE positions SET qty=400.5')
        elif change == 'same_day_trade':
            conn.execute("UPDATE trades SET qty=200 WHERE side='sell'")
        else:
            receipt = json.loads(conn.execute('SELECT result_json FROM execution_receipts').fetchone()[0])
            receipt['inventory_basis']['positions']['600036']['lots'][0]['qty'] += 100
            conn.execute('UPDATE execution_receipts SET result_json=?', (json.dumps(receipt),))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='inventory'):
        _execute_portfolio(decision, 'no_gate', path, {'600036': bar()}, True)
    assert path.read_bytes() == before


def test_same_day_unreceipted_trade_is_not_an_opening_lot(tmp_path):
    path = tmp_path / 'account.db'
    seed(path)
    decision = {'eligible_execution_day': DAY, 'data_asof': '2026-09-18', 'plan_hash': 'fixture',
                'portfolios': {'no_gate': {'plans': [plan()]}}}
    before = path.read_bytes()
    with pytest.raises(ValueError, match='inventory'):
        _execute_portfolio(decision, 'no_gate', path, {'600036': bar()}, True)
    assert path.read_bytes() == before


def test_closing_inventory_failure_rolls_back_account_and_trade_writes(tmp_path, monkeypatch):
    from trading_team import paper_pipeline
    path = tmp_path / 'account.db'
    seed(path, [trade(1, '2026-09-18', 'buy', 700)], buy_day='2026-09-18')
    decision = {'eligible_execution_day': DAY, 'data_asof': '2026-09-18', 'plan_hash': 'fixture',
                'portfolios': {'no_gate': {'plans': [plan()]}}}
    original = paper_pipeline.inventory_basis
    def fail_closing(records, day):
        if records[-1]['day'] == DAY:
            raise ValueError('inventory closing interrupted')
        return original(records, day)
    monkeypatch.setattr(paper_pipeline, 'inventory_basis', fail_closing)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='closing interrupted'):
        _execute_portfolio(decision, 'no_gate', path, {'600036': bar()}, True)
    assert path.read_bytes() == before
