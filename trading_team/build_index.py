"""扫描 trading_team/outputs/ 重建 index.json（控制台日报列表的数据源）。

用法:  .venv/bin/python -m trading_team.build_index
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "outputs"


def summarize(day_dir: Path) -> dict:
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

    # 研究员评级表：| 代码/名称 ... | 多空 ｜信心 |
    rfile = day_dir / "05_researcher.md"
    if rfile.exists():
        text = rfile.read_text(encoding="utf-8")
        m = re.search(r"综合评级\*{0,2}[：:]\s*(.+)", text)
        if m:
            item["ratings_line"] = m.group(1).strip()[:200]

    manifest = ROOT / "context" / date / "manifest.json"
    if manifest.exists():
        mj = json.loads(manifest.read_text(encoding="utf-8"))
        item["data_asof"] = mj.get("latest_trade_date")
        item["collect_ok"] = mj.get("ok")
    return item


def main() -> None:
    days = sorted([d for d in OUT.iterdir() if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name)],
                  key=lambda d: d.name, reverse=True)
    items = [summarize(d) for d in days]
    idx_path = OUT / "index.json"
    idx_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"index.json -> {len(items)} 天")


if __name__ == "__main__":
    main()
