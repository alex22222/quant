import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from paper.execution_model import Bar, simulate_fill
from trading_team import counterfactual


DAY = "2026-09-21"


def row(**changes):
    return {"date": DAY, "price_basis": "unadjusted", "open": 10, "high": 10.2,
            "low": 9.8, "close": 10.1, "prev_close": 10, "volume": 100000,
            "suspended": False, "limit_up": 11, "limit_down": 9, **changes}


def test_existing_reader_must_preserve_explicit_suspension():
    bar = counterfactual._bar_from_row("600036", row(suspended=True, volume=0))
    assert bar.suspended is True
    assert not simulate_fill("buy", 100, bar).filled


def test_explicit_daily_limits_override_code_prefix_assumption():
    bar = counterfactual._bar_from_row("600036", row(open=10.5, high=10.5, close=10.5,
                                                         limit_up=10.5, limit_down=9.5))
    assert not simulate_fill("buy", 100, bar).filled


@pytest.mark.parametrize("changes", [
    {"open": float("nan")}, {"close": float("inf")}, {"prev_close": 0},
    {"low": 11}, {"high": 9}, {"close": 12}, {"volume": None},
    {"suspended": "false"}, {"suspended": None}, {"volume": 0},
    {"limit_up": None}, {"limit_down": None}, {"limit_up": 9},
    {"price_basis": "qfq"},
])
def test_bad_execution_rows_fail_closed(changes):
    with pytest.raises(ValueError):
        counterfactual._bar_from_row("600036", row(**changes))


def test_cache_day_mismatch_is_rejected_without_rewrite_or_network(tmp_path):
    path = tmp_path / "replay_data" / f"{DAY}.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"date": "2026-09-18", "bars": {"600036": row()}}))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        counterfactual._cached_bars(DAY, tmp_path)
    assert path.read_bytes() == before


def test_legacy_adjusted_cache_cannot_be_promoted_to_execution(tmp_path):
    path = tmp_path / "replay_data" / f"{DAY}.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"date": DAY, "source": "tencent_qfq_kline", "bars": {
        "600036": row(), "000300": row(),
    }}))
    with pytest.raises(ValueError, match="legacy|schema"):
        counterfactual._cached_bars(DAY, tmp_path)


def test_half_cent_limit_uses_exchange_tick_rounding():
    assert not simulate_fill("buy", 100, Bar(open=11.03, close=11.03, prev_close=10.03)).filled
    assert not simulate_fill("sell", 100, Bar(open=9.03, close=9.03, prev_close=10.03)).filled


def test_cache_roundtrip_and_tamper_detection(tmp_path, execution_cache):
    path = execution_cache(tmp_path, DAY, {"600036": row(), "000300": row()})
    assert counterfactual._cached_bars(DAY, tmp_path)["600036"].limit_up == 11
    payload = json.loads(path.read_text())
    payload["bars"]["600036"]["close"] = 9.9
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="hash"):
        counterfactual._cached_bars(DAY, tmp_path)


def test_resealed_row_still_must_match_source_record(tmp_path, execution_cache):
    from trading_team.execution_bars import _hash

    path = execution_cache(tmp_path, DAY, {"600036": row()})
    payload = json.loads(path.read_text())
    payload["bars"]["600036"]["close"] = 9.9
    payload["payload_hash"] = _hash({k: v for k, v in payload.items() if k != "payload_hash"})
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="source record"):
        counterfactual._cached_bars(DAY, tmp_path)


def test_loader_reuses_valid_cache_without_network(tmp_path, execution_cache, finalize_calendar, monkeypatch):
    from trading_team.execution_bars import load_execution_bars

    path = execution_cache(tmp_path, DAY, {"600036": row(), "000300": row()})
    before = path.read_bytes()
    monkeypatch.setattr("trading_team.execution_bars.fetch_snapshot_tx", lambda *_: pytest.fail("network called"))
    bars = load_execution_bars(DAY, ["600036"], tmp_path, calendar_path=finalize_calendar)
    assert bars["600036"].limit_down == 9
    assert path.read_bytes() == before


@pytest.mark.parametrize("stamp", ["20260918150000", "20260921103000"])
def test_snapshot_requires_exact_closing_date(tmp_path, finalize_calendar, monkeypatch, stamp):
    from trading_team.execution_bars import load_execution_bars

    snapshot = {code: {**row(), "quote_time": stamp} for code in ("600036", "000300")}
    monkeypatch.setattr("trading_team.execution_bars.fetch_snapshot_tx", lambda *_: snapshot)
    with pytest.raises(ValueError, match="closing data"):
        load_execution_bars(DAY, ["600036"], tmp_path, bundle=tmp_path / "empty",
                            calendar_path=finalize_calendar)
    assert not (tmp_path / "replay_data" / f"{DAY}.json").exists()


def test_closing_snapshot_is_cached_with_actual_limits_and_source(tmp_path, finalize_calendar, monkeypatch):
    from trading_team.execution_bars import load_execution_bars

    snapshot = {code: {**row(), "quote_time": "20260921150003"} for code in ("600036", "000300")}
    snapshot["600036"].update(limit_up=10.5, limit_down=9.5)
    monkeypatch.setattr("trading_team.execution_bars.fetch_snapshot_tx", lambda *_: snapshot)
    bars = load_execution_bars(DAY, ["600036"], tmp_path, bundle=tmp_path / "empty",
                                calendar_path=finalize_calendar)
    assert bars["600036"].limit_up == 10.5
    assert bars["600036"].volume == 10000000
    cached = counterfactual._cached_bars(DAY, tmp_path)
    assert cached == bars
    payload = json.loads((tmp_path / "replay_data" / f"{DAY}.json").read_text())
    assert payload["sources"]["600036"]["record"]["quote_time"] == "20260921150003"


def test_local_raw_bundle_uses_reference_price_and_daily_limits(tmp_path, finalize_calendar, monkeypatch):
    import h5py
    import numpy as np
    from trading_team.execution_bars import load_execution_bars

    fields = [("datetime", "i8")] + [(key, "f8") for key in (
        "open", "high", "low", "close", "prev_close", "volume", "limit_up", "limit_down")]
    for filename, symbol in (("stocks.h5", "600036.XSHG"), ("indexes.h5", "000300.XSHG")):
        with h5py.File(tmp_path / filename, "w") as handle:
            handle.create_dataset(symbol, data=np.array([
                (20260921000000, 9, 9.2, 8.8, 9.1, 9, 100000, 9.9, 8.1),
            ], dtype=fields))
    monkeypatch.setattr("trading_team.execution_bars.fetch_snapshot_tx", lambda *_: pytest.fail("network called"))
    bars = load_execution_bars(DAY, ["600036"], tmp_path / "team", bundle=tmp_path,
                                calendar_path=finalize_calendar)
    assert bars["600036"].prev_close == 9
    assert bars["600036"].limit_up == 9.9


def test_direct_loader_cannot_accept_intraday_or_holiday(tmp_path, finalize_calendar, monkeypatch):
    from trading_team.execution_bars import load_execution_bars

    monkeypatch.setattr("trading_team.execution_bars.fetch_snapshot_tx", lambda *_: pytest.fail("network called"))
    now = datetime(2026, 9, 21, 14, tzinfo=ZoneInfo("Asia/Shanghai"))
    for day in (DAY, "2026-09-20", "2026-09-22"):
        with pytest.raises(ValueError, match="not completed"):
            load_execution_bars(day, ["600036"], tmp_path, now=now,
                                calendar_path=finalize_calendar)


def test_breakout_keeps_explicit_limit_prices():
    from trading_team.execute import _simulate_buy
    bar = counterfactual._bar_from_row("600036", row(high=10.5, limit_up=10.5, limit_down=9.5))
    fill, _, _ = _simulate_buy({"direction": "买入", "entry": [10.5], "entry_type": "breakout"}, 100, bar)
    assert not fill.filled
    assert "涨停" in fill.reject_reason


def test_slippage_cannot_fill_outside_explicit_exchange_limits():
    bar = counterfactual._bar_from_row("600036", row(open=10.49, high=10.5, close=10.49,
                                                         limit_up=10.5, limit_down=9.5))
    assert not simulate_fill("buy", 100, bar, slippage_rate=0.002).filled


def test_invalid_limit_order_price_is_not_a_price_improvement_fill():
    from paper.execution_model import simulate_limit_fill

    bar = counterfactual._bar_from_row("600036", row(limit_up=10.5, limit_down=9.5))
    assert not simulate_limit_fill("buy", 100, 10.6, bar).filled


def test_tencent_parser_retains_closing_time_limits_and_basis(monkeypatch):
    from trading_team.collect import fetch_snapshot_tx

    fields = [""] * 50
    for index, value in {1: "CMB", 2: "600036", 3: "10.1", 4: "10", 5: "10",
                         30: "20260921150003", 33: "10.2", 34: "9.8", 36: "100",
                         47: "10.5", 48: "9.5"}.items():
        fields[index] = value
    class Response:
        text = 'v_sh600036="' + "~".join(fields) + '";'
        def raise_for_status(self):
            pass

    monkeypatch.setattr("trading_team.collect.requests.get", lambda *_a, **_k: Response())
    result = fetch_snapshot_tx(["1.600036"])["600036"]
    assert result["quote_time"] == "20260921150003"
    assert result["limit_up"] == 10.5
    assert result["limit_down"] == 9.5
    assert result["price_basis"] == "unadjusted"


def test_index_sentinel_limits_are_not_interpreted_as_stock_limits():
    bar = counterfactual._bar_from_row("000300", row(limit_up=-1, limit_down=-1))
    assert bar.limit_up is None and bar.limit_down is None


def test_strict_stock_execution_uses_cent_prices_and_matching_amounts():
    from paper.execution_model import simulate_limit_fill

    bar = counterfactual._bar_from_row("600036", row(open=10.01))
    fill = simulate_fill("buy", 100, bar, slippage_rate=0.002)
    assert fill.price == 10.04
    assert fill.amount == round(fill.price * fill.qty, 2)
    assert not simulate_limit_fill("buy", 100, 10.031, bar).filled


def test_zero_volume_snapshot_is_not_executable_even_with_a_close():
    from trading_team.execution_bars import _snapshot_row

    item = row(volume=0, open=0, high=0, low=0, quote_time="20260921150000")
    parsed = _snapshot_row("600036", item, DAY)
    bar = counterfactual._bar_from_row("600036", parsed, DAY)
    assert bar.suspended
    assert not simulate_fill("buy", 100, bar).filled


def test_pure_counterfactual_does_not_certify_supplied_bars():
    report = counterfactual.build_counterfactual({
        "date": DAY, "data_asof": "2026-09-18", "proposals": [],
    }, {"000300": Bar(open=10, close=10, prev_close=10)})
    assert report.get("market_data_schema") is None
    assert "未验证来源" in counterfactual.render_markdown(report)


def test_written_counterfactual_binds_validated_execution_cache(tmp_path, execution_cache, finalize_calendar):
    cache = execution_cache(tmp_path, DAY, {"000300": row()})
    output = tmp_path / "outputs" / DAY
    output.mkdir(parents=True)
    (output / "plan.json").write_text(json.dumps({
        "date": DAY, "data_asof": "2026-09-18", "proposals": [],
    }))
    report = counterfactual.write_counterfactual(DAY, tmp_path)
    assert report["market_data_schema"] == "execution_bars.v2"
    assert report["market_data_hash"] == json.loads(cache.read_text())["payload_hash"]
