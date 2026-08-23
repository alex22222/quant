# -*- coding: utf-8 -*-
"""阶段3·风控委员会：统一回测 + 门禁 v2 判定（改进计划 Phase 2）

对每个 candidate/live 策略：
  1. 全样本回测（BT_START ~ BT_END）
  2. 验证集回测（OOS_START ~ BT_END；⚠️ 2024-01 起窗口已被调参使用，
     语义为 validation，非独立样本外——见 pipeline/gate.py 数据分区声明）
  3. 参数扰动回测（策略暴露 PARAMS 且含 momentum_days/hold_num 时）
  4. 门禁 v2 判定（pipeline/gate.py），同族只留一个代表

成本口径：rqalpha 默认佣金/印花税 + --pit-tax（历史真实印花税）+ 固定滑点 0.2% 近似冲击成本。
结果与拒绝原因全部写入 results，供复盘报告展示失败样本。
"""
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

from .common import ROOT, load_status, now
from .gate import DATA_PARTITION, GATE_V2, PERTURB_RULES, evaluate, extract_metrics
from .promote import GATE_FILE, _params_hash, _sha256

RQALPHA = ROOT / ".venv" / "bin" / "rqalpha"
BT_START, BT_END, CASH = "2020-01-01", "2026-08-01", "100000"
OOS_START = "2024-01-01"
# 市场阶段窗口（门禁 v2 要求至少 3 个阶段分别统计：2020-21 结构牛 / 2022-23 熊 / 2024-26 震荡修复）
STAGE_WINDOWS = [
    ("2020-01-01", "2021-12-31"),
    ("2022-01-01", "2023-12-31"),
    ("2024-01-01", "2026-08-01"),
]
BENCH = "000300.XSHG"
SLIPPAGE = "0.002"  # 固定滑点近似冲击成本


def _read_summary(xlsx):
    import pandas as pd
    df = pd.read_excel(xlsx, header=None)
    return dict(zip(df[0], df[1]))


def _monthly_stats(portfolio_csv: Path) -> dict:
    """从 portfolio.csv 计算月度收益分布指标：最差单月、月度胜率、偏度、峰度。"""
    try:
        import pandas as pd
        df = pd.read_csv(portfolio_csv, index_col=0, parse_dates=True)
        nav = df["unit_net_value"]
        monthly = nav.resample("ME").last().pct_change().dropna()
        if monthly.empty:
            return {}
        return {
            "worst_month": round(float(monthly.min()), 4),
            "best_month": round(float(monthly.max()), 4),
            "monthly_win_rate": round(float((monthly > 0).mean()), 4),
            "months": int(len(monthly)),
            "skew": round(float(monthly.skew()), 4),
            "kurt": round(float(monthly.kurt() + 3), 4),  # pandas 为超额峰度，转回峰度
        }
    except Exception:
        return {}


LEDGER = ROOT / "pipeline" / "experiment_ledger.jsonl"


def _ledger_trials(name: str, params_hash: str) -> int:
    """实验台账：记录本次参数组合，返回该策略累计不同参数组合数（多重试验修正用）。

    台账落 git（pipeline/experiment_ledger.jsonl），跨轮审计可追溯——
    "试了多少次"本身是门禁输入，必须可复现。"""
    seen = set()
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("strategy") == name:
                seen.add(rec.get("params_hash"))
    seen.add(params_hash)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now(), "strategy": name,
                            "params_hash": params_hash}, ensure_ascii=False) + "\n")
    return len(seen)


def _run(info, start, end, extra_vars, out_dir, tag):
    """跑一次 rqalpha 回测，返回 (metrics dict | None, error str | None)"""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(RQALPHA), "run", "-f", info["file"],
           "-s", start, "-e", end, "-bm", BENCH,
           "--account", "stock", CASH,
           "-sp", SLIPPAGE, "--pit-tax", "-cnsmc", "5",
           "--report", str(out_dir)]
    ev = dict(info.get("params") or {})
    ev.update(extra_vars or {})
    if ev:
        cmd += ["--extra-vars", json.dumps(ev)]
    try:
        env = dict(os.environ, QUANT_ROOT=str(ROOT))
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                           cwd=ROOT, env=env)
    except subprocess.TimeoutExpired:
        return None, "timeout(600s)"
    xlsx = out_dir / "summary.xlsx"
    if r.returncode != 0 or not xlsx.exists():
        return None, (r.stderr or "")[-300:]
    metrics = extract_metrics(_read_summary(xlsx))
    metrics.update(_monthly_stats(out_dir / "portfolio.csv"))
    return metrics, None


def _family_of(name):
    """读取策略 FAMILY（无则按名字本身成族）。失败按独立族处理。"""
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        mod = importlib.import_module(f"strategies.{name}")
        return getattr(mod, "FAMILY", name)
    except Exception:
        return name


def _perturb_variants(name):
    """按 PERTURB_RULES 生成参数扰动变体（笛卡尔积，去掉基准本身）。"""
    try:
        mod = importlib.import_module(f"strategies.{name}")
        params = getattr(mod, "PARAMS", {})
    except Exception:
        return []
    axes = []
    for key, rule in PERTURB_RULES.items():
        v = params.get(key)
        if isinstance(v, int):
            axes.append([(key, x) for x in rule(v) if x != v])
    if not axes:
        return []
    variants = [{}]
    for axis in axes:
        variants = [{**base, k: v} for base in variants for (k, v) in axis]
    return variants[:8]  # 上限 8 个扰动组合，控制耗时


def main(only=None):
    st = load_status()
    reg = st.get("strategy_registry", {})
    targets = {k: v for k, v in reg.items() if v["state"] in ("candidate", "live")}
    if only:
        targets = {k: v for k, v in targets.items() if k == only}
    if not targets:
        return {"note": "没有 candidate/live 策略", "results": {}}

    # 族去重：同族已有 live 的，其余成员标记 family_dup（其代表为最早注册者/live 优先）
    families = {}
    for name in targets:
        families.setdefault(_family_of(name), []).append(name)
    family_rep = {}
    for fam, members in families.items():
        rep = next((m for m in members if reg[m]["state"] == "live"),
                   sorted(members)[0])
        for m in members:
            family_rep[m] = {"family": fam, "family_representative": rep,
                             "family_dup": m != rep}

    results = {}
    for name, info in targets.items():
        entry = {"report": f"reports/{name}"}

        full, err = _run(info, BT_START, BT_END, None, ROOT / "reports" / name / "full", "full")
        if err:
            results[name] = {**entry, "gate": "error", "error": f"全样本: {err}"}
            continue
        oos, err = _run(info, OOS_START, BT_END, None, ROOT / "reports" / name / "oos", "oos")
        if err:
            oos = None
            entry["oos_error"] = err

        perturbs = []
        if not family_rep[name]["family_dup"]:
            for i, var in enumerate(_perturb_variants(name)):
                tag = f"perturb{i+1}"
                pm, err = _run(info, BT_START, BT_END, var,
                               ROOT / "reports" / name / tag, tag)
                if pm is not None:
                    pm["variant"] = json.dumps(var, ensure_ascii=False)
                    perturbs.append(pm)

        # 市场阶段统计（只报告、不单独否决；门禁由全样本+OOS+扰动决定）
        stage_stats = []
        for j, (ws, we) in enumerate(STAGE_WINDOWS):
            sm, err = _run(info, ws, we, None,
                           ROOT / "reports" / name / f"stage{j+1}", f"stage{j+1}")
            if sm is not None:
                stage_stats.append({"window": f"{ws}~{we}", "annual": sm["annual"],
                                    "max_dd": sm["max_dd"], "sharpe": sm["sharpe"],
                                    "excess_annual": sm.get("excess_annual")})

        n_trials = _ledger_trials(name, _params_hash(name, info.get("params"))) + len(perturbs)
        verdict = evaluate(full, oos, perturbs, family_rep[name], n_trials=n_trials)
        results[name] = {**entry, **full, "oos": oos,
                         "strategy_hash": _sha256(ROOT / info["file"]),
                         "params_hash": _params_hash(name, info.get("params")),
                         "gate_hash": _sha256(GATE_FILE),
                         "stage_windows": stage_stats,
                         "gate": verdict["gate"], "reasons": verdict["reasons"],
                         "dsr": verdict["detail"].get("dsr"),
                         "n_trials": verdict["detail"].get("n_trials"),
                         "family": family_rep[name],
                         "perturbations": [
                             {"variant": p.pop("variant"),
                              "annual": p["annual"], "max_dd": p["max_dd"],
                              "sharpe": p["sharpe"], "excess_annual": p["excess_annual"]}
                             for p in verdict["detail"]["perturbations"]],
                         }

    return {"gate_version": "v2", "gate_thresholds": GATE_V2,
            "window": f"{BT_START}~{BT_END}", "oos_window": f"{OOS_START}~{BT_END}",
            "data_partition": DATA_PARTITION,
            "cost_model": "rqalpha 默认佣金 + pit-tax 历史印花税 + 固定滑点0.2% + 最低佣金5元",
            "benchmark": "沪深300", "results": results}


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else None
    out = main(only=only)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:4000])
