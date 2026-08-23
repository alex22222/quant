# -*- coding: utf-8 -*-
"""turtle 攻关快速实验：全样本单次回测 + 关键指标打印

用法: .venv/bin/python scripts/turtle_experiment.py '{"ma_slope_days":20}' [标签]
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RQALPHA = ROOT / ".venv" / "bin" / "rqalpha"


def run(extra: dict, tag: str, start="2020-01-01", end="2026-08-01"):
    out = ROOT / "reports" / "turtle_exp" / tag
    out.mkdir(parents=True, exist_ok=True)
    cmd = [str(RQALPHA), "run", "-f", "strategies/turtle_bluechip.py",
           "-s", start, "-e", end, "-bm", "000300.XSHG",
           "--account", "stock", "100000",
           "-sp", "0.002", "--pit-tax", "-cnsmc", "5",
           "--report", str(out)]
    if extra:
        cmd += ["--extra-vars", json.dumps(extra)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=280,
                       cwd=ROOT, env={"QUANT_ROOT": str(ROOT), **__import__("os").environ})
    xlsx = out / "summary.xlsx"
    if r.returncode != 0 or not xlsx.exists():
        print(f"{tag}: ERROR {(r.stderr or '')[-200:]}")
        return
    import pandas as pd
    d = dict(zip(*(lambda df: (df[0], df[1]))(pd.read_excel(xlsx, header=None))))
    print(f"{tag}: 年化 {float(d['年化收益率']):.1%} 回撤 {float(d['最大回撤']):.1%} "
          f"夏普 {float(d['夏普比率']):.2f} 超额 {float(d['年化超额收益（几何）']):.1%} "
          f"IR {float(d['信息比率']):.2f} 最长回撤 {int(d['最长回撤持续天数'])}天 "
          f"[{d['最长回撤开始日期']}~{d['最长回撤结束日期']}]")


if __name__ == "__main__":
    extra = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    tag = sys.argv[2] if len(sys.argv) > 2 else "exp"
    run(extra, tag)
