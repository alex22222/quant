import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def legacy_policy_sandbox(request, monkeypatch, tmp_path_factory):
    """Historical contracts run in an explicit test-only policy; freeze tests use real policy."""
    if request.node.path.name == "test_research_policy.py":
        return
    import json
    from pipeline import policy
    path = tmp_path_factory.mktemp("legacy-policy") / "test-policy.json"
    path.write_text(json.dumps({"capabilities": {key: True for key in (
        "llm_meeting", "llm_plan", "llm_execution", "multica_dispatch", "multica_review")}}))
    monkeypatch.setattr(policy, "POLICY_FILE", path)


@pytest.fixture(autouse=True)
def isolate_multica_identity(monkeypatch):
    for key in ("TASK", "AGENT", "WORKSPACE", "ISSUE"):
        monkeypatch.delenv(f"MULTICA_{key}_ID", raising=False)


@pytest.fixture
def stub_research_reviews(monkeypatch):
    """Unit tests of other contracts exclude remote review; test_review_gate covers the real chain."""
    from trading_team import multica_reviews
    monkeypatch.setattr(multica_reviews, "require_reviews", lambda *args, **kwargs: None)


@pytest.fixture
def finalize_calendar(tmp_path, monkeypatch):
    from trading_team import daily_finalize, execution_bars

    calendar = tmp_path / "finalize-calendar.npy"
    np.save(calendar, np.array([20260902, 20260903, 20260918, 20260921, 20260922,
                                20260930, 20261009]))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 9, 22, 8, tzinfo=daily_finalize.SHANGHAI)
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(daily_finalize, "DEFAULT_TRADING_CALENDAR", calendar)
    monkeypatch.setattr(execution_bars, "DEFAULT_TRADING_CALENDAR", calendar)
    monkeypatch.setattr(daily_finalize, "datetime", Clock)
    monkeypatch.setattr(execution_bars, "datetime", Clock)
    return calendar


@pytest.fixture
def execution_cache():
    import json
    from trading_team.execution_bars import _hash, seal_document

    def write(team, day, bars):
        rows, sources = {}, {}
        for code, item in bars.items():
            row = {**item, "date": day, "price_basis": "unadjusted"}
            row.setdefault("suspended", False)
            row.setdefault("volume", 1000000)
            if code != "000300":
                row.setdefault("limit_up", round(row["prev_close"] * 1.1, 2))
                row.setdefault("limit_down", round(row["prev_close"] * 0.9, 2))
            raw = {key: value for key, value in row.items() if key not in ("date", "price_basis", "suspended")}
            raw["datetime"] = int(day.replace("-", "")) * 1000000
            rows[code] = row
            sources[code] = {"kind": "rqalpha_bundle", "record": raw, "source_hash": _hash(raw)}
        payload = seal_document(day, rows, sources)
        path = team / "replay_data" / f"{day}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        return path

    return write
