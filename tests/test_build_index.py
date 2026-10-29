import json

from trading_team.build_index import build_index


def test_build_index_only_indexes_and_supports_isolated_team(tmp_path):
    team = tmp_path / "trading_team"
    output = team / "outputs" / "2026-09-21"
    context = team / "context" / "2026-09-21"
    output.mkdir(parents=True)
    context.mkdir(parents=True)
    (output / "daily_conclusion.md").write_text(
        "## 一句话总览\n**测试结论**", encoding="utf-8"
    )
    (context / "manifest.json").write_text(json.dumps({
        "latest_trade_date": "2026-09-18", "ok": True,
    }), encoding="utf-8")

    items = build_index(team)

    assert items[0]["headline"] == "测试结论"
    assert items[0]["data_asof"] == "2026-09-18"
    assert json.loads((team / "outputs" / "index.json").read_text(encoding="utf-8")) == items
    assert not (team / "decisions").exists()


def test_direct_script_help_keeps_working_without_package_path(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    script = Path(__file__).resolve().parents[1] / "trading_team/build_index.py"
    result = subprocess.run([sys.executable, str(script), "--help"], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "--finalize-available" in result.stdout
    assert not list(tmp_path.iterdir())
