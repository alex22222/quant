# -*- coding: utf-8 -*-
"""参数敏感性扫描器：对单策略跑参数网格，输出门禁成绩对照表

用法：
  .venv/bin/python pipeline/sweep.py strategies/turtle_bluechip.py \
      '{"exit_atr":[2.0,2.5],"n_entry":[15,20,25]}'

输出：
  reports/sweep/<strategy>__<参数组合>/   每组参数的回测报告与净值图
  reports/sweep/<strategy>_sweep.json    汇总（含门禁判定，供控制台展示）
"""
import itertools
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RQALPHA = ROOT / ".venv" / "bin" / "rqalpha"
GATE = {"annual_min": 0.05, "maxdd_max": 0.30, "sharpe_min": 0.30}
BT_START, BT_END, CASH = "2020-01-01", "2026-08-01", "100000"
BENCH = "000300.XSHG"


def gate(m):
    reasons = []
    if m["annual"] < GATE["annual_min"]:
        reasons.append(f"年化 {m['annual']:.1%} < {GATE['annual_min']:.0%}")
    if m["max_dd"] > GATE["maxdd_max"]:
        reasons.append(f"回撤 {m['max_dd']:.1%} > {GATE['maxdd_max']:.0%}")
    if m["sharpe"] < GATE["sharpe_min"]:
        reasons.append(f"夏普 {m['sharpe']:.2f} < {GATE['sharpe_min']}")
    return ("pass" if not reasons else "reject"), reasons


def main():
    strat = sys.argv[1]
    grid = json.loads(sys.argv[2])
    name = Path(strat).stem
    keys = list(grid)
    combos = [dict(zip(keys, vs)) for vs in itertools.product(*(grid[k] for k in keys))]

    out_root = ROOT / "reports" / "sweep"
    out_root.mkdir(parents=True, exist_ok=True)

    rows = []
    for combo in combos:
        tag = "_".join(f"{k}{v}" for k, v in combo.items()).replace(".", "p")
        out_dir = out_root / f"{name}__{tag}"
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [str(RQALPHA), "run", "-f", strat, "-s", BT_START, "-e", BT_END,
               "-bm", BENCH, "--account", "stock", CASH,
               "--report", str(out_dir), "--plot-save", str(out_dir / "equity.png"),
               "--extra-vars", json.dumps(combo)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=ROOT)
        xlsx = out_dir / "summary.xlsx"
        if r.returncode != 0 or not xlsx.exists():
            rows.append({"params": combo, "gate": "error",
                         "stderr": (r.stderr or "")[-200:]})
            continue
        import pandas as pd
        d = dict(zip(*(pd.read_excel(xlsx, header=None)[i] for i in (0, 1))))
        m = {"total_ret": float(d["收益率"]), "annual": float(d["年化收益率"]),
             "max_dd": float(d["最大回撤"]), "sharpe": float(d["夏普比率"])}
        g, reasons = gate(m)
        rows.append({"params": combo, **m, "gate": g, "reasons": reasons,
                     "report": f"reports/sweep/{name}__{tag}"})

    rows.sort(key=lambda r: (r.get("gate") != "pass", -(r.get("sharpe") or -9)))
    summary = {"strategy": name, "window": f"{BT_START}~{BT_END}",
               "gate_thresholds": GATE, "rows": rows}
    (out_root / f"{name}_sweep.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    hdr = f"{'参数':<28} {'总收益':>7} {'年化':>7} {'回撤':>7} {'夏普':>6}  门禁"
    print(hdr)
    for r in rows:
        if r["gate"] == "error":
            print(f"{json.dumps(r['params']):<28}  ERROR {r['stderr'][-60:]}")
        else:
            print(f"{json.dumps(r['params']):<28} {r['total_ret']:>7.1%} "
                  f"{r['annual']:>7.1%} {r['max_dd']:>7.1%} {r['sharpe']:>6.2f}  {r['gate']}")
    best = next((r for r in rows if r["gate"] == "pass"), None)
    print(f"\n最优组合: {json.dumps(best['params']) if best else '无过门禁组合'}")


if __name__ == "__main__":
    main()
