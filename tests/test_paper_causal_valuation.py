import json
import sqlite3

import pytest

from paper.execution_model import Bar
from trading_team.paper_pipeline import (
    _db, _execute_portfolio, account_snapshot, load_reliable_marks,
    paper_db_paths, record_current_valuations,
)

pytestmark = pytest.mark.usefixtures("finalize_calendar")
DAY = "2026-09-21"


def price_row(close=40):
    return {"open": 40, "high": 44, "low": 36, "close": close, "prev_close": 40}


def seed(path, day="2026-09-18", cash=90000):
    with _db(path) as conn:
        conn.execute("INSERT INTO account VALUES (?,?,100000,'fixture')", (day, cash))
        conn.execute("INSERT INTO positions VALUES ('600036',200,40,?)", (day,))
        conn.execute("INSERT INTO trades(day,code,side,qty) VALUES (?,'600036','buy',200)", (day,))


def decision():
    return {"plan_hash": "v1", "data_asof": "2026-09-18", "eligible_execution_day": DAY,
            "portfolios": {"no_gate": {"plans": [
                {"code": "601318", "direction": "买入", "entry": [10], "entry_type": "limit",
                 "position_pct": 0.1, "executable": True},
            ]}}}


def test_order_size_never_uses_execution_day_closing_price(tmp_path):
    quantities = []
    for close in (36, 44):
        path = tmp_path / f"{close}.db"
        seed(path, cash=92000)
        bars = {"600036": Bar(**price_row(close)),
                "601318": Bar(open=10, high=11, low=9, close=10, prev_close=10)}
        _execute_portfolio(decision(), "no_gate", path, bars, True)
        with sqlite3.connect(path) as conn:
            quantities.append(conn.execute("SELECT qty FROM trades WHERE side='buy' AND day=?", (DAY,)).fetchone()[0])
    assert quantities == [1000, 1000]


@pytest.mark.parametrize("missing", ["bar", "reference"])
def test_unknown_pretrade_position_value_does_not_fall_back_to_cost(tmp_path, missing):
    path = tmp_path / "paper.db"
    seed(path)
    bars = {"601318": Bar(open=10, high=11, low=9, close=10, prev_close=10)}
    if missing == "reference":
        bars["600036"] = Bar(open=40, close=40, prev_close=None)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="reference|pre-trade"):
        _execute_portfolio(decision(), "no_gate", path, bars, True)
    assert path.read_bytes() == before


def test_sizing_basis_is_recorded_in_execution_receipt(tmp_path):
    path = tmp_path / "paper.db"
    seed(path, cash=92000)
    _execute_portfolio(decision(), "no_gate", path, {
        "600036": Bar(**price_row(44)),
        "601318": Bar(open=10, high=11, low=9, close=10, prev_close=10),
    }, True)
    with sqlite3.connect(path) as conn:
        receipt = json.loads(conn.execute("SELECT result_json FROM execution_receipts").fetchone()[0])
    assert receipt["sizing_basis"]["equity"] == 100000
    assert receipt["sizing_basis"]["price_field"] == "prev_close"
    assert receipt["sizing_basis"]["positions"]["600036"]["reference_price"] == 40


def test_technical_adjusted_close_cannot_be_used_as_account_mark(tmp_path):
    context = tmp_path / "context" / DAY
    context.mkdir(parents=True)
    (context / "manifest.json").write_text(json.dumps({"ok": True, "latest_trade_date": DAY}))
    (context / "technical_600036.json").write_text(json.dumps({"as_of": DAY, "close": 20}))
    marks = load_reliable_marks(tmp_path, DAY)
    assert marks["prices"] == {}


def test_validated_cache_prices_are_read_without_mutation_or_network(tmp_path, execution_cache, monkeypatch):
    path = execution_cache(tmp_path, DAY, {"600036": price_row(42)})
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.setattr("trading_team.execution_bars.fetch_snapshot_tx", lambda *_: pytest.fail("network"))
    marks = load_reliable_marks(tmp_path, DAY)
    assert marks["prices"] == {"600036": 42}
    assert marks["price_basis"] == "unadjusted"
    assert marks["evidence"]["600036"]["source_hash"]
    assert path.exists()
    assert {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_invalid_cache_does_not_advertise_a_verified_quote_date(tmp_path):
    path = tmp_path / "replay_data" / f"{DAY}.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"date": DAY, "bars": {"600036": price_row()}}))
    marks = load_reliable_marks(tmp_path, DAY)
    assert marks["quote_asof"] is None
    assert marks["source"] is None
    assert marks["price_basis"] is None
    assert marks["error"]


def test_intraday_cache_cannot_value_current_positions(tmp_path, execution_cache):
    execution_cache(tmp_path, "2026-09-22", {"600036": price_row(42)})
    marks = load_reliable_marks(tmp_path, "2026-09-22")
    assert marks["prices"] == {}


@pytest.mark.parametrize("mark", [float("nan"), float("inf"), 0, -1])
def test_account_rejects_invalid_mark_numbers(tmp_path, mark):
    path = tmp_path / "paper.db"
    seed(path)
    item = account_snapshot(path, valuation={
        "quote_asof": DAY, "source_date": DAY, "prices": {"600036": mark},
    }, as_of_day=DAY)
    assert item["valuation_equity"] is None
    assert item["positions"][0]["mark_price"] is None
    assert item["missing_marks"] == ["600036"]


def test_current_positions_cannot_be_marked_before_account_state(tmp_path):
    path = tmp_path / "paper.db"
    seed(path, day=DAY)
    item = account_snapshot(path, valuation={
        "quote_asof": "2026-09-18", "prices": {"600036": 42},
    }, as_of_day=DAY)
    assert item["valuation_equity"] is None
    assert "predates" in item["valuation_error"]


def test_valuation_writer_does_not_backdate_current_cash_and_positions(tmp_path, execution_cache):
    team = tmp_path / "team"
    paths = paper_db_paths(team)
    seed(paths["no_gate"], day=DAY)
    execution_cache(team, "2026-09-18", {"600036": price_row(42)})
    result = record_current_valuations(paths, team=team, as_of_day=DAY)
    assert result["portfolios"]["no_gate"]["status"] == "skipped"
    assert "predates" in result["portfolios"]["no_gate"]["reason"]
    with sqlite3.connect(paths["no_gate"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM account_valuations").fetchone()[0] == 0


def test_valuation_writer_uses_isolated_team_paths_by_default(tmp_path, monkeypatch):
    from trading_team import paper_pipeline
    team = tmp_path / "team"
    protected = tmp_path / "protected.db"
    seed(protected)
    before = protected.read_bytes()
    monkeypatch.setattr(paper_pipeline, "DEFAULT_DB_PATHS", {"gate": protected, "no_gate": protected})
    result = record_current_valuations(team=team, as_of_day=DAY)
    assert all(item["reason"] == "database missing" for item in result["portfolios"].values())
    assert protected.read_bytes() == before


def test_valuation_conflict_is_not_silently_reused(tmp_path, execution_cache):
    team = tmp_path / "team"
    paths = paper_db_paths(team)
    seed(paths["no_gate"])
    execution_cache(team, DAY, {"600036": price_row(42)})
    first = record_current_valuations(paths, team=team, as_of_day=DAY)
    assert first["portfolios"]["no_gate"]["status"] == "inserted"
    with sqlite3.connect(paths["no_gate"]) as conn:
        conn.execute("UPDATE account_valuations SET equity=123")
    with pytest.raises(ValueError, match="valuation.*changed|valuation.*conflict"):
        record_current_valuations(paths, team=team, as_of_day=DAY)
    with sqlite3.connect(paths["no_gate"]) as conn:
        assert conn.execute("SELECT equity FROM account_valuations").fetchone()[0] == 123


def test_postexecution_valuation_reuses_the_same_raw_prices(tmp_path, execution_cache):
    from trading_team.execution_bars import _cached_bars
    team = tmp_path / "team"
    paths = paper_db_paths(team)
    seed(paths["no_gate"], cash=92000)
    execution_cache(team, DAY, {"600036": price_row(42), "601318": {
        "open": 10, "high": 11, "low": 9, "close": 10.5, "prev_close": 10,
    }})
    _execute_portfolio(decision(), "no_gate", paths["no_gate"], _cached_bars(DAY, team), True)
    result = record_current_valuations(paths, team=team, as_of_day=DAY)
    assert result["portfolios"]["no_gate"]["status"] == "reused"


def test_valuation_does_not_initialize_an_empty_existing_account(tmp_path, execution_cache):
    team = tmp_path / "team"
    paths = paper_db_paths(team)
    paths["no_gate"].parent.mkdir(parents=True)
    sqlite3.connect(paths["no_gate"]).close()
    execution_cache(team, DAY, {"600036": price_row()})
    before = paths["no_gate"].read_bytes()
    with pytest.raises(RuntimeError, match="invalid|balance"):
        record_current_valuations(paths, team=team, as_of_day=DAY)
    assert paths["no_gate"].read_bytes() == before
