# -*- coding: utf-8 -*-
"""turtle 攻关快速实验：全样本单次回测 + 关键指标打印

用法: .venv/bin/python scripts/turtle_experiment.py '{"ma_slope_days":20}' [标签]
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.stage_backtest import _run
RQALPHA = ROOT / ".venv" / "bin" / "rqalpha"


def run(extra: dict, tag: str, start="2020-01-01", end="2026-08-01"):
    out = ROOT / "reports" / "turtle_exp" / tag
    out.mkdir(parents=True, exist_ok=True)
    metrics, error = _run({"file": "strategies/turtle_bluechip.py"}, start, end, extra, out, tag)
    if error:
        print(f"{tag}: ERROR {error[-200:]}")
    else:
        print(json.dumps({"tag": tag, **metrics}, ensure_ascii=False))
    return


if __name__ == "__main__":
    extra = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    tag = sys.argv[2] if len(sys.argv) > 2 else "exp"
    run(extra, tag)
