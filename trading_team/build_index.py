"""扫描 trading_team/outputs/ 重建 index.json（控制台日报列表的数据源）。

用法:  .venv/bin/python -m trading_team.build_index
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

try:
    from trading_team.state_lock import atomic_write_json, state_lock
except ImportError:
    from state_lock import atomic_write_json, state_lock

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "outputs"


def summarize(day_dir: Path, team: Path = ROOT) -> dict:
    date = day_dir.name
    item = {"date": date, "reports": [], "headline": "", "ratings": {}, "data_asof": None}

    concl = day_dir / "daily_conclusion.md"
    if concl.exists():
        text = concl.read_text(encoding="utf-8")
        m = re.search(r"## 一句话总览\s*\n+\*\*(.+?)\*\*", text)
        if not m:
            m = re.search(r"## 一句话总览\s*\n+(.+)", text)
        item["headline"] = (m.group(1).strip() if m else "")[:120]

    for f in sorted(day_dir.glob("0*.md")):
        item["reports"].append(f.name)
    if (day_dir / "counterfactual.md").exists():
        item["reports"].append("counterfactual.md")
    if (day_dir / "continuous_counterfactual.md").exists():
        item["reports"].append("continuous_counterfactual.md")

    # 研究员评级表：| 代码/名称 ... | 多空 ｜信心 |
    rfile = day_dir / "05_researcher.md"
    if rfile.exists():
        text = rfile.read_text(encoding="utf-8")
        m = re.search(r"综合评级\*{0,2}[：:]\s*(.+)", text)
        if m:
            item["ratings_line"] = m.group(1).strip()[:200]

    manifest = team / "context" / date / "manifest.json"
    if manifest.exists():
        mj = json.loads(manifest.read_text(encoding="utf-8"))
        item["data_asof"] = mj.get("latest_trade_date")
        item["collect_ok"] = mj.get("ok")
    return item


def build_index(team: Path = ROOT) -> list[dict]:
    with state_lock(team):
        return _build_index(team)


def _build_index(team: Path) -> list[dict]:
    out = team / "outputs"
    out.mkdir(parents=True, exist_ok=True)
    days = sorted([d for d in out.iterdir() if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name)],
                  key=lambda d: d.name, reverse=True)
    items = [summarize(d, team) for d in days]
    idx_path = out / "index.json"
    atomic_write_json(idx_path, items)
    return items


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--finalize-available",
        action="store_true",
        help="Explicitly run deterministic finalization before rebuilding the index",
    )
    args = parser.parse_args()
    if args.finalize_available:
        from trading_team.daily_finalize import finalize_available_tracked
        finalized = finalize_available_tracked()
        print(f"daily_finalize -> {len(finalized)} days")
    items = build_index()
    print(f"index.json -> {len(items)} days")


if __name__ == "__main__":
    main()
