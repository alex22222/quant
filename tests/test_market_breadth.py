from datetime import datetime
import hashlib
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests

from trading_team import collect


def quote(code, change, day="2026-09-18"):
    return {"f12": code, "f13": 0, "f3": change,
            "f124": int(datetime.fromisoformat(day + "T15:01:00")
                        .replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())}


def test_breadth_counts_quotes_not_unfiltered_total(monkeypatch):
    rows = [quote(f"{i:06d}", change) for i, change in enumerate(
        [6, 5, 0.01, 0, -0.01, -5, -6, "-"], start=1)]
    monkeypatch.setattr(collect, "_get", lambda url, params: {
        "rc": 0, "data": {"total": 8, "diff": rows},
    })

    result = collect.fetch_market_breadth("2026-09-18")

    assert {key: result[key] for key in ("up_gt5", "up", "down", "down_gt5", "flat")} == {
        "up_gt5": 1, "up": 3, "down": 3, "down_gt5": 1, "flat": 1,
    }
    assert result["unavailable"] == 1
    assert result["total"] == 8
    assert result["date"] == "2026-09-18"
    assert result["pages"][0]["response"]["data"]["diff"] == rows


def test_breadth_fetches_complete_pages_in_stable_order(monkeypatch):
    calls = []

    def get(url, params):
        calls.append(params.copy())
        assert "filter" not in params
        assert params["fid"] == "f12"
        start = (params["pn"] - 1) * 100
        rows = [quote(f"{i:06d}", 1 if i < 100 else -1)
                for i in range(start, min(start + 100, 101))]
        return {"rc": 0, "data": {"total": 101, "diff": rows}}

    monkeypatch.setattr(collect, "_get", get)
    result = collect.fetch_market_breadth("2026-09-18")

    assert [p["pn"] for p in calls] == [1, 2]
    assert result["up"] == 100
    assert result["down"] == 1
    assert len(result["pages"]) == 2


@pytest.mark.parametrize("defect", ["truncated", "duplicate", "changed_total", "empty"])
def test_breadth_rejects_incomplete_or_moving_universe(monkeypatch, defect):
    def get(url, params):
        if params["pn"] == 1:
            rows = [quote(f"{i:06d}", 1) for i in range(100)]
            if defect == "truncated":
                rows.pop()
            return {"rc": 0, "data": {"total": 101, "diff": rows}}
        rows = [] if defect == "empty" else [quote("000000" if defect == "duplicate" else "000100", -1)]
        return {"rc": 0, "data": {"total": 102 if defect == "changed_total" else 101, "diff": rows}}

    monkeypatch.setattr(collect, "_get", get)
    with pytest.raises(ValueError, match="breadth"):
        collect.fetch_market_breadth("2026-09-18")


@pytest.mark.parametrize("change", [True, "garbage", float("nan"), float("inf"), None, "-"])
def test_breadth_rejects_invalid_or_entirely_unavailable_data(monkeypatch, change):
    monkeypatch.setattr(collect, "_get", lambda url, params: {
        "rc": 0, "data": {"total": 1, "diff": [quote("000001", change)]},
    })
    with pytest.raises(ValueError, match="breadth"):
        collect.fetch_market_breadth("2026-09-18")


@pytest.mark.parametrize("payload", [None, {"total": 0, "diff": []}, {"total": True, "diff": []}])
def test_breadth_does_not_convert_missing_payload_to_zero(monkeypatch, payload):
    monkeypatch.setattr(collect, "_get", lambda url, params: {"rc": 0, "data": payload})
    with pytest.raises(ValueError, match="breadth"):
        collect.fetch_market_breadth("2026-09-18")


def test_breadth_rejects_wrong_session(monkeypatch):
    monkeypatch.setattr(collect, "_get", lambda url, params: {
        "rc": 0, "data": {"total": 1, "diff": [quote("000001", 1, "2026-09-21")]},
    })
    with pytest.raises(ValueError, match="breadth.*session"):
        collect.fetch_market_breadth("2026-09-18")


def test_breadth_propagates_network_failure(monkeypatch):
    def get(url, params):
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(collect, "_get", get)
    with pytest.raises(RuntimeError, match="source unavailable"):
        collect.fetch_market_breadth("2026-09-18")


def fallback_sources(monkeypatch, defect=None, size=3, first_quote=None):
    calls = []
    symbols = [f"sz{i:06d}" for i in range(1, size + 1)] + ["bj920001"]
    if defect == "unexpected_market":
        symbols[-1] = "sh900001"

    def primary(url, params):
        raise requests.ConnectionError("primary disconnected")

    def get(url, params=None, **kwargs):
        calls.append((url, params))
        if defect == "network":
            raise requests.Timeout("fallback timed out")
        if "getHQNodeStockCount" in url:
            n = len(symbols) + (defect == "changed_total" and len(calls) > 1)
            value = str(n)
        elif "getHQNodeData" in url:
            start = (params["page"] - 1) * 80
            value = [{"symbol": s, "code": s[2:]} for s in symbols[start:start + 80]]
            if defect == "truncated":
                value.pop()
            elif defect == "duplicate_universe":
                value[-1] = value[0]
        else:
            lines = []
            for i, symbol in enumerate(url.split("q=")[1].split(",")):
                fields = [""] * 50
                fields[1:5] = ["sample", symbol[2:], ["10.6", "9.4", "10"][i % 3], "10"]
                fields[30], fields[32] = "20260918150100", ["6", "-6", "0"][i % 3]
                if i == 0 and first_quote is not None:
                    fields[3], fields[4], fields[32] = first_quote
                if defect == "stale":
                    fields[30] = "20260917150100"
                elif defect == "intraday":
                    fields[30] = "20260918113000"
                elif defect == "inconsistent":
                    fields[32] = "1"
                elif defect == "nonnumeric":
                    fields[3] = "NaN"
                elif defect == "quote_identity":
                    fields[2] = "999999"
                lines.append('v_' + symbol + '="' + "~".join(fields) + '";')
            if defect == "missing_quote":
                lines.pop()
            elif defect == "duplicate_quote":
                lines.append(lines[0])
            raw = "\n".join(lines).encode("gbk")
            return SimpleNamespace(content=raw, raise_for_status=lambda: None)
        return SimpleNamespace(json=lambda: value, raise_for_status=lambda: None)

    monkeypatch.setattr(collect, "_get", primary)
    monkeypatch.setattr(collect.requests, "get", get)
    monkeypatch.setattr(collect.time, "sleep", lambda seconds: None)
    return calls


def test_breadth_fallback_preserves_scope_dates_raw_and_primary_failure(monkeypatch):
    fallback_sources(monkeypatch)
    result = collect.fetch_market_breadth("2026-09-18")

    assert {k: result[k] for k in ("up", "down", "flat", "up_gt5", "down_gt5")} == {
        "up": 1, "down": 1, "flat": 1, "up_gt5": 1, "down_gt5": 1,
    }
    assert result["total"] == 3
    assert result["universe_total"] == 4
    assert result["excluded_beijing"] == 1
    assert result["unavailable"] == 0
    assert result["date"] == "2026-09-18"
    assert result["primary_failure"]["type"] == "ConnectionError"
    assert "primary disconnected" in result["primary_failure"]["detail"]
    assert "sina" in result["universe_source"]
    assert result["source"] == "https://qt.gtimg.cn/q="
    raw = result["pages"][-1]
    assert hashlib.sha256(raw["response"].encode("gbk")).hexdigest() == raw["sha256"]
    assert len(result["pages"]) == 4  # count, universe page, count, quotes
    assert json.loads(json.dumps(result)) == result


@pytest.mark.parametrize("first_quote,expected", [
    (("331.99", "332.00", "-0.00"), {"up": 0, "down": 1, "flat": 0, "up_gt5": 0, "down_gt5": 0}),
    (("332.01", "332.00", "0.00"), {"up": 1, "down": 0, "flat": 0, "up_gt5": 0, "down_gt5": 0}),
    (("20.99", "19.99", "5.00"), {"up": 1, "down": 0, "flat": 0, "up_gt5": 1, "down_gt5": 0}),
    (("18.99", "19.99", "-5.00"), {"up": 0, "down": 1, "flat": 0, "up_gt5": 0, "down_gt5": 1}),
])
def test_breadth_fallback_classifies_unrounded_price_change(monkeypatch, first_quote, expected):
    fallback_sources(monkeypatch, size=1, first_quote=first_quote)

    result = collect.fetch_market_breadth("2026-09-18")

    assert {key: result[key] for key in expected} == expected


def test_breadth_fallback_fetches_all_universe_and_quote_pages(monkeypatch):
    calls = fallback_sources(monkeypatch, size=81)
    result = collect.fetch_market_breadth("2026-09-18")

    assert result["total"] == 81
    assert sum(result[k] for k in ("up", "down", "flat")) == 81
    assert [p["page"] for u, p in calls if "getHQNodeData" in u] == [1, 2]
    assert len([u for u, p in calls if "qt.gtimg.cn" in u]) == 2


@pytest.mark.parametrize("defect", [
    "truncated", "changed_total", "duplicate_universe", "unexpected_market",
    "missing_quote", "duplicate_quote", "stale", "intraday", "inconsistent",
    "nonnumeric", "quote_identity", "network",
])
def test_breadth_fallback_rejects_unverified_data(monkeypatch, defect):
    fallback_sources(monkeypatch, defect)
    with pytest.raises(ValueError, match="breadth.*fallback"):
        collect.fetch_market_breadth("2026-09-18")


def test_invalid_primary_data_does_not_trigger_fallback(monkeypatch):
    fallback_sources(monkeypatch)
    monkeypatch.setattr(collect, "_get", lambda url, params: {"rc": 0, "data": None})
    monkeypatch.setattr(collect.requests, "get", lambda *a, **k: pytest.fail("unexpected fallback"))
    with pytest.raises(ValueError, match="breadth source payload unavailable"):
        collect.fetch_market_breadth("2026-09-18")
