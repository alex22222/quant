# -*- coding: utf-8 -*-
"""阶段2·技术分析师：策略注册表（状态机 research→candidate→live→retired）"""
import json
import re
from pathlib import Path

from .common import ROOT, load_status, save_status, now

STRAT_DIR = ROOT / "strategies"
REGISTRY_KEY = "strategy_registry"

# 首次扫描时的默认状态；之后以 status.json 中人工修改为准
DEFAULT_STATE = {
    "momentum_rotation": "candidate", # 待回测门禁
    "turtle_bluechip": "candidate",   # 参数冻结，只做纯前向 Paper 观察
}


def _doc_summary(path):
    text = path.read_text(encoding="utf-8")
    m = re.search(r'"""(.*?)"""', text, re.S)
    return " ".join(m.group(1).split())[:120] if m else ""


def main():
    st = load_status()
    reg = st.get(REGISTRY_KEY, {})
    for f in sorted(STRAT_DIR.glob("*.py")):
        name = f.stem
        if name not in reg:
            reg[name] = {
                "state": DEFAULT_STATE.get(name, "research"),
                "file": f"strategies/{f.name}",
                "summary": _doc_summary(f),
                "params": {},
                "registered_at": now(),
            }
        else:  # 刷新摘要与文件路径，状态以注册表为准
            reg[name]["summary"] = _doc_summary(f)
            reg[name]["file"] = f"strategies/{f.name}"
    # 代码已删除的策略不应继续污染活动注册表；历史证据由 reports/ 保留。
    for name in list(reg):
        if not (STRAT_DIR / f"{name}.py").exists():
            del reg[name]

    # 活动状态只展示仍在库的策略；完整历史继续留在 reports/ 中。
    backtest_results = (
        st.get("stages", {}).get("backtest", {}).get("result", {}).get("results")
    )
    if isinstance(backtest_results, dict):
        for name in list(backtest_results):
            if name not in reg:
                del backtest_results[name]

    st[REGISTRY_KEY] = reg
    save_status(st)
    return {
        "total": len(reg),
        "by_state": {s: [k for k, v in reg.items() if v["state"] == s]
                     for s in ["research", "candidate", "live", "retired"]},
        "hint": "修改策略状态：编辑 status.json 的 strategy_registry 段落",
    }
