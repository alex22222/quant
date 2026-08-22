# -*- coding: utf-8 -*-
"""阶段3·风控委员会：对 candidate/live 策略跑统一回测 + 门禁判定"""
import json
import subprocess
from pathlib import Path

from .common import ROOT, load_status

RQALPHA = ROOT / ".venv" / "bin" / "rqalpha"
GATE = {"annual_min": 0.05, "maxdd_max": 0.30, "sharpe_min": 0.30}
BT_START, BT_END, CASH = "2020-01-01", "2026-08-01", "100000"
BENCH = "000300.XSHG"


def _read_summary(xlsx):
    import pandas as pd
    df = pd.read_excel(xlsx, header=None)
    return dict(zip(df[0], df[1]))


def _gate(m):
    reasons = []
    if m["annual"] < GATE["annual_min"]:
        reasons.append(f"年化 {m['annual']:.1%} < {GATE['annual_min']:.0%}")
    if m["max_dd"] > GATE["maxdd_max"]:
        reasons.append(f"回撤 {m['max_dd']:.1%} > {GATE['maxdd_max']:.0%}")
    if m["sharpe"] < GATE["sharpe_min"]:
        reasons.append(f"夏普 {m['sharpe']:.2f} < {GATE['sharpe_min']}")
    return ("pass" if not reasons else "reject"), reasons


def main():
    st = load_status()
    reg = st.get("strategy_registry", {})
    targets = {k: v for k, v in reg.items() if v["state"] in ("candidate", "live")}
    if not targets:
        return {"note": "没有 candidate/live 策略", "results": {}}

    results = {}
    for name, info in targets.items():
        out_dir = ROOT / "reports" / name
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [str(RQALPHA), "run", "-f", info["file"],
               "-s", BT_START, "-e", BT_END, "-bm", BENCH,
               "--account", "stock", CASH,
               "--report", str(out_dir), "--plot-save", str(out_dir / "equity.png")]
        if info.get("params"):
            cmd += ["--extra-vars", json.dumps(info["params"])]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=ROOT)
        xlsx = out_dir / "summary.xlsx"
        if r.returncode != 0 or not xlsx.exists():
            results[name] = {"gate": "error", "stderr": (r.stderr or "")[-300:]}
            continue
        d = _read_summary(xlsx)
        m = {"total_ret": float(d["收益率"]), "annual": float(d["年化收益率"]),
             "max_dd": float(d["最大回撤"]), "sharpe": float(d["夏普比率"])}
        gate, reasons = _gate(m)
        results[name] = {**m, "gate": gate, "reasons": reasons,
                         "report": f"reports/{name}"}
    return {"gate_thresholds": GATE, "window": f"{BT_START}~{BT_END}",
            "benchmark": "沪深300", "results": results}
