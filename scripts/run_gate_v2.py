# -*- coding: utf-8 -*-
"""门禁 v2 全量重跑 runner：逐策略运行并把完整 JSON 落盘到 reports/gate_v2/<name>.json"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.stage_backtest import main  # noqa: E402


def run_one(name: str) -> dict:
    out = main(only=name)
    d = ROOT / "reports" / "gate_v2"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    res = out["results"].get(name, {})
    print(f"{name}: gate={res.get('gate')} reasons={len(res.get('reasons', []))} "
          f"err={res.get('error', '')[:80]}")
    return out


if __name__ == "__main__":
    run_one(sys.argv[1])
