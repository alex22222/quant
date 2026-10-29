import json

from trading_team import collect_global


def test_us_provider_snapshot_is_not_a_verified_close(monkeypatch):
    fields = [""] * 50
    fields[1], fields[3], fields[30], fields[31], fields[32] = (
        "Dow", "51814.77", "2026-09-22 13:11:20", "-234.06", "-0.45",
    )
    raw = 'v_usDJI="' + "~".join(fields) + '";'
    monkeypatch.setattr(collect_global, "_req", lambda *a, **k: raw)

    result = collect_global.fetch_overnight_us()

    assert result["quote_type"] == "snapshot"
    assert result["session_status"] == "unverified"
    assert result["asof"] == "2026-09-22 13:11:20"
    row = next(iter(result["data"].values()))
    assert row["last"] == 51814.77
    assert "close" not in row


def test_undated_asia_quotes_do_not_invent_market_date(monkeypatch):
    monkeypatch.setattr(collect_global, "_req_gbk", lambda *a, **k: 'var hq_str_int_nikkei="Nikkei,44946.64,-408.35,-0.90";')
    monkeypatch.setattr(collect_global, "_req", lambda *a, **k: json.dumps({"data": {"diff": []}}))

    result = collect_global.fetch_asia()

    assert result["asof"] is None
    assert result["observed_at"]
    assert result["quote_type"] == "snapshot"
    assert result["data"]["Nikkei"]["last"] == 44946.64
    assert result["data"]["韩国KOSPI"] is None


def test_commodity_snapshot_retains_provider_date_not_capture_day(monkeypatch):
    fields = [""] * 14
    fields[0], fields[7], fields[12], fields[13] = "4380.992", "4383.9", "2026-09-22", "Gold"
    monkeypatch.setattr(collect_global, "_req_gbk", lambda *a, **k: 'var hq_str_hf_GC="' + ",".join(fields) + '";')

    result = collect_global.fetch_commodities()

    assert result["asof"] == "2026-09-22"
    assert result["quote_type"] == "snapshot"
    assert result["data"]["Gold"]["last"] == 4380.992
    assert result["observed_at"]


def test_undated_forex_and_boards_remain_undated(monkeypatch, tmp_path):
    monkeypatch.setattr(collect_global, "_req", lambda *a, **k: json.dumps({
        "data": {"diff": [{"f2": 7.1, "f3": 0.2, "f14": "sample"}]},
    }))

    forex = collect_global.fetch_forex()
    boards = collect_global.fetch_hotspots(tmp_path)

    assert forex["asof"] is None and boards["asof"] is None
    assert forex["observed_at"] and boards["observed_at"]
    assert forex["quote_type"] == boards["quote_type"] == "snapshot"
    assert forex["data"]["USDCNH"]["last"] == 7.1
