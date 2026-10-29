import json
from datetime import datetime

import pytest

from trading_team.collect import collect_day, validate_completed_session
from trading_team.run_ledger import RunLedger


@pytest.fixture(autouse=True)
def trading_calendar(monkeypatch):
    monkeypatch.setattr(
        "trading_team.collect.load_trading_days",
        lambda path: {"2026-09-17", "2026-09-18", "2026-09-21"},
    )


def test_completed_session_rejects_mismatch_and_partial_intraday():
    mismatch = validate_completed_session(
        "2026-09-21",
        {"000300": "2026-09-18", "600036": "2026-09-17"},
        now=datetime(2026, 9, 21, 16, 0),
    )
    partial = validate_completed_session(
        "2026-09-21",
        {"000300": "2026-09-21", "600036": "2026-09-21"},
        now=datetime(2026, 9, 21, 10, 0),
    )

    assert mismatch["ok"] is False
    assert "日期不一致" in mismatch["detail"]
    assert partial["ok"] is False
    assert "尚未完成收盘" in partial["detail"]


def test_completed_session_rejects_missing_expected_symbol():
    result = validate_completed_session(
        "2026-09-21",
        {"000300": "2026-09-18"},
        expected_codes={"000300", "600036"},
        now=datetime(2026, 9, 21, 16, 0),
    )

    assert result["ok"] is False
    assert "600036" in result["detail"]


def test_completed_session_rejects_weekday_exchange_holiday():
    result = validate_completed_session(
        "2026-10-01",
        {"000300": "2026-10-01", "600036": "2026-10-01"},
        expected_codes={"000300", "600036"},
        trading_days={"2026-09-30", "2026-10-09"},
        now=datetime(2026, 10, 1, 16, 0),
    )

    assert result["ok"] is False
    assert "不在 A 股交易日历" in result["detail"]


@pytest.mark.parametrize("failed_source", ["news", "market_breadth"])
def test_collect_retry_reuses_successful_sources(tmp_path, monkeypatch, execution_cache, finalize_calendar, failed_source):
    team = tmp_path / "trading_team"
    team.mkdir()
    (team / "watchlist.json").write_text(json.dumps({
        "stocks": [{"code": "600036", "secid": "1.600036", "sector": "银行"}],
        "benchmark": {"code": "000300", "secid": "1.000300"},
    }), encoding="utf-8")
    calls = {
        "snapshot": 0, "kline": 0, "financials": 0, "announcements": 0,
        "news": 0, "zt": 0, "dt": 0, "breadth": 0,
    }

    def snapshot(secids):
        calls["snapshot"] += 1
        return {
            "600036": {"close": 40.0, "turnover_rate": 1.0},
            "000300": {"close": 4500.0},
        }

    def kline(secid):
        calls["kline"] += 1
        return [{"secid": secid}]

    def news():
        calls["news"] += 1
        if failed_source == "news" and calls["news"] == 1:
            raise RuntimeError("temporary news failure")
        return [{"title": "recovered"}]

    monkeypatch.setattr("trading_team.collect.fetch_snapshot", snapshot)
    monkeypatch.setattr("trading_team.collect.fetch_snapshot_tx", lambda secids: {})
    monkeypatch.setattr("trading_team.collect.fetch_kline", kline)
    monkeypatch.setattr(
        "trading_team.collect.compute_indicators",
        lambda rows: {"as_of": "2026-09-18", "close": 1},
    )
    monkeypatch.setattr(
        "trading_team.collect.fetch_financials",
        lambda code, secid: calls.__setitem__("financials", calls["financials"] + 1) or {"reports": []},
    )
    monkeypatch.setattr(
        "trading_team.collect.fetch_announcements",
        lambda code: calls.__setitem__("announcements", calls["announcements"] + 1) or [],
    )
    monkeypatch.setattr("trading_team.collect.fetch_fastnews", news)
    monkeypatch.setattr(
        "trading_team.collect.fetch_zt_pool",
        lambda day: calls.__setitem__("zt", calls["zt"] + 1) or {"zt_count": 1},
    )
    monkeypatch.setattr(
        "trading_team.collect.fetch_dt_pool",
        lambda day: calls.__setitem__("dt", calls["dt"] + 1) or {"dt_count": 2},
    )
    def breadth(day):
        assert day == "2026-09-18"
        calls["breadth"] += 1
        if failed_source == "market_breadth" and calls["breadth"] == 1:
            raise ValueError("breadth incomplete page")
        return {"up": 10, "pages": [{"response": {"data": {"total": 10}}}]}

    monkeypatch.setattr("trading_team.collect.fetch_market_breadth", breadth)
    ledger = RunLedger(tmp_path / "ledger.db")
    bar = {"open": 10, "high": 10.2, "low": 9.8, "close": 10.1, "prev_close": 10}
    execution_cache(team, "2026-09-18", {code: bar for code in ("600036", "000300")})

    first = collect_day(
        "2026-09-21", root=team, ledger=ledger, now=datetime(2026, 9, 21, 8, 0),
        calendar_path=finalize_calendar,
    )
    second = collect_day(
        "2026-09-21", root=team, ledger=ledger, now=datetime(2026, 9, 21, 8, 1),
        calendar_path=finalize_calendar,
    )

    assert first["ok"] is False
    assert first["sources"][failed_source]["ok"] is False
    if failed_source == "market_breadth":
        assert first["sources"]["sentiment"]["ok"] is False
    assert second["ok"] is True
    assert first["collector_run_id"] == second["collector_run_id"]
    assert calls == {
        "snapshot": 1, "kline": 2, "financials": 1, "announcements": 1,
        "news": 2 if failed_source == "news" else 1, "zt": 1, "dt": 1,
        "breadth": 2 if failed_source == "market_breadth" else 1,
    }
    run = ledger.get_run(second["collector_run_id"])
    assert run["attempt"] == 2
    news_attempts = [
        step for step in run["steps"] if step["step_name"] == failed_source
    ]
    assert [step["status"] for step in news_attempts] == ["failed", "succeeded"]
    session_attempts = [
        step for step in run["steps"] if step["step_name"] == "session_alignment"
    ]
    assert len(session_attempts) == 1
    assert second["latest_trade_date"] == "2026-09-18"
    sentiment = json.loads((team / "context/2026-09-21/sentiment.json").read_text())
    assert "pages" not in sentiment["breadth"]
    raw = team / "context/2026-09-21/market_breadth.json"
    assert json.loads(raw.read_text())["pages"]
    from trading_team.collect import _file_hash
    assert sentiment["breadth"]["source_hash"] == _file_hash(raw)


@pytest.mark.parametrize("technical_day", ["2026-09-18", "2026-09-17"])
def test_collect_uses_complete_fallback_for_partial_primary_snapshot(
    tmp_path, monkeypatch, execution_cache, finalize_calendar, technical_day
):
    team = tmp_path / "trading_team"
    team.mkdir()
    (team / "watchlist.json").write_text(json.dumps({
        "stocks": [{"code": "600036", "secid": "1.600036", "sector": "银行"}],
        "benchmark": {"code": "000300", "secid": "1.000300"},
    }), encoding="utf-8")
    calls = {"primary": 0, "fallback": 0}

    def partial_snapshot(secids):
        calls["primary"] += 1
        return {"600036": {"close": 40.0}}

    def complete_snapshot(secids):
        calls["fallback"] += 1
        return {
            "600036": {"close": 40.0},
            "000300": {"close": 4500.0},
        }

    monkeypatch.setattr("trading_team.collect.fetch_snapshot", partial_snapshot)
    monkeypatch.setattr("trading_team.collect.fetch_snapshot_tx", complete_snapshot)
    def kline(secid):
        assert list((team / "market_archive/receipts").glob("*.json"))
        return [{}]

    monkeypatch.setattr("trading_team.collect.fetch_kline", kline)
    monkeypatch.setattr(
        "trading_team.collect.compute_indicators",
        lambda rows: {"as_of": technical_day, "close": 1},
    )
    monkeypatch.setattr(
        "trading_team.collect.fetch_financials",
        lambda code, secid: {"reports": []},
    )
    monkeypatch.setattr("trading_team.collect.fetch_announcements", lambda code: [])
    monkeypatch.setattr("trading_team.collect.fetch_fastnews", lambda: [])
    monkeypatch.setattr(
        "trading_team.collect.fetch_zt_pool", lambda day: {"zt_count": 0}
    )
    monkeypatch.setattr(
        "trading_team.collect.fetch_dt_pool", lambda day: {"dt_count": 0}
    )
    monkeypatch.setattr("trading_team.collect.fetch_market_breadth", lambda day: {})
    bar = {"open": 10, "high": 10.2, "low": 9.8, "close": 10.1, "prev_close": 10}
    execution_cache(team, "2026-09-18", {code: bar for code in ("600036", "000300")})

    result = collect_day(
        "2026-09-21",
        root=team,
        ledger=RunLedger(tmp_path / "ledger.db"),
        now=datetime(2026, 9, 21, 8, 0),
        calendar_path=finalize_calendar,
    )

    assert result["ok"] is (technical_day == "2026-09-18")
    assert result["execution_archive"]["date"] == "2026-09-18"
    assert calls == {"primary": 1, "fallback": 1}
    assert "腾讯兜底" in result["sources"]["snapshot"]["detail"]


def test_collect_records_skipped_sentiment_when_session_is_incomplete(
    tmp_path, monkeypatch, execution_cache, finalize_calendar
):
    team = tmp_path / "trading_team"
    team.mkdir()
    (team / "watchlist.json").write_text(json.dumps({
        "stocks": [{"code": "600036", "secid": "1.600036", "sector": "银行"}],
        "benchmark": {"code": "000300", "secid": "1.000300"},
    }), encoding="utf-8")
    monkeypatch.setattr(
        "trading_team.collect.fetch_snapshot",
        lambda secids: {"600036": {"close": 40.0}, "000300": {"close": 4500.0}},
    )
    monkeypatch.setattr(
        "trading_team.collect.compute_indicators",
        lambda rows: {
            "as_of": "2026-09-18" if "600036" in rows[0].get("secid", "") else "2026-09-17",
            "close": 1,
        },
    )
    monkeypatch.setattr(
        "trading_team.collect.fetch_kline",
        lambda secid: [{"secid": secid}],
    )
    monkeypatch.setattr(
        "trading_team.collect.fetch_financials",
        lambda code, secid: {"reports": []},
    )
    monkeypatch.setattr("trading_team.collect.fetch_announcements", lambda code: [])
    monkeypatch.setattr("trading_team.collect.fetch_fastnews", lambda: [])
    ledger = RunLedger(tmp_path / "ledger.db")
    bar = {"open": 10, "high": 10.2, "low": 9.8, "close": 10.1, "prev_close": 10}
    execution_cache(team, "2026-09-21", {code: bar for code in ("600036", "000300")})

    result = collect_day(
        "2026-09-21",
        root=team,
        ledger=ledger,
        now=datetime(2026, 9, 21, 16, 0),
        calendar_path=finalize_calendar,
    )

    assert result["ok"] is False
    run = ledger.get_run(result["collector_run_id"])
    sentiment = [step for step in run["steps"] if step["step_name"] == "sentiment"]
    assert [step["status"] for step in sentiment] == ["skipped"]
