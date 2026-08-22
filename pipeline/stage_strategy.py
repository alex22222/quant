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
    "demo_dual_ma": "retired",        # 环境验证用途，不进生产
    "momentum_rotation": "candidate", # 待回测门禁
    "momentum_stops": "live",         # 已通过验证（见 reviews 与 README 结论）
}
DEFAULT_PARAMS = {
    "momentum_stops": {"stop": "none"},
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
                "params": DEFAULT_PARAMS.get(name, {}),
                "registered_at": now(),
            }
        else:  # 刷新摘要与文件路径，状态以注册表为准
            reg[name]["summary"] = _doc_summary(f)
            reg[name]["file"] = f"strategies/{f.name}"
    # 清理已删除的策略文件
    for name in list(reg):
        if not (STRAT_DIR / f"{name}.py").exists():
            reg[name]["state"] = "retired"
            reg[name]["note"] = "文件已删除"
    st[REGISTRY_KEY] = reg
    save_status(st)
    return {
        "total": len(reg),
        "by_state": {s: [k for k, v in reg.items() if v["state"] == s]
                     for s in ["research", "candidate", "live", "retired"]},
        "hint": "修改策略状态：编辑 status.json 的 strategy_registry 段落",
    }
