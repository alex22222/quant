"""OKX 截图研究目录：完整覆盖，但不得注册为可执行策略。"""

import json
from pathlib import Path
from urllib.parse import urlparse

import pipeline.stage_strategy as ss


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "strategies/research/okx_catalog.json"
EXPECTED = {
    "okx_flywheel": 3, "okx_coin_pair_grid": 2, "okx_gold_grid": 2,
    "okx_martingale": 3, "okx_wide_grid": 2, "okx_metals_futures_grid": 2,
    "okx_universe_grid": 3, "okx_smart_arbitrage": 2,
    "okx_equity_futures_grid": 3, "okx_preipo_futures_grid": 3,
    "okx_coin_margin_hedge_grid": 1,
}


def test_catalog_covers_screenshots_without_inventing_performance():
    catalog = json.loads(CATALOG.read_text())
    rows = catalog["strategies"]
    assert len(rows) == len(EXPECTED)
    assert {row["id"]: len(row["variants"]) for row in rows} == EXPECTED
    assert catalog["screenshot_captured_at"] is None
    assert catalog["performance_policy"] == "screenshot_apr_not_imported_as_performance"
    assert {row["screenshot"] for row in rows} == {1, 2, 3, 4}
    for row in rows:
        assert row["state"] == "research"
        assert row["execution_enabled"] is False
        assert row["implementation_status"] == "not_implemented"
        assert row["verified_metrics"] is None
        assert row["template_parameters"] is None
        for key in ("principle", "profit_source", "risks", "evidence_boundary",
                    "parameters_required", "validation", "assessment"):
            assert row[key]
        assert row["sources"]
        for source_id in row["sources"]:
            source = catalog["sources"][source_id]
            assert urlparse(source["url"]).hostname == "www.okx.com"
            assert source["checked_at"] == "2026-09-26"
    equity = next(row for row in rows if row["id"] == "okx_equity_futures_grid")
    assert "SPCX/USDT" in equity["variants"]
    assert "SPCX" in equity["evidence_boundary"]


def test_research_catalog_is_not_discovered_by_execution_registry(monkeypatch):
    catalog = json.loads(CATALOG.read_text())
    status = {"strategy_registry": {}}
    monkeypatch.setattr(ss, "STRAT_DIR", ROOT / "strategies")
    monkeypatch.setattr(ss, "load_status", lambda: status)
    monkeypatch.setattr(ss, "save_status", lambda value: None)
    ss.main()
    assert "turtle_bluechip" in status["strategy_registry"]
    assert not set(EXPECTED).intersection(status["strategy_registry"])
    assert all(not (ROOT / "strategies" / (row["id"] + ".py")).exists()
               for row in catalog["strategies"])


def test_console_links_separate_research_catalog():
    page = (ROOT / "console/index.html").read_text()
    script = (ROOT / "console/research-library.js").read_text()
    assert 'id="okx-research-library"' in page
    assert 'src="/console/research-library.js"' in page
    assert "/strategies/research/okx_catalog.json" in script
    assert "innerHTML" not in script
    assert "status.json" not in script
