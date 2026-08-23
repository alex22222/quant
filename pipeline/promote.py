# -*- coding: utf-8 -*-
"""原子晋级与晋级审计（策略库审查 P0 治理修复）

问题背景（docs/STRATEGY_LIBRARY_REVIEW_A_SHARE.md）：
- status.json 曾被手工编辑晋级，与门禁报告脱节（registry=live 而 gate=reject）；
- 一次晋级提交同时放宽了门禁阈值——"策略没通过门禁，于是调整门禁直到通过"。

本模块是策略状态变更的**唯一合法入口**：

  python -m pipeline.promote promote <name> --to live --reason "..." --approver "..."
      原子写入 state + promotion 证据记录（策略代码 hash / 参数 hash /
      门禁版本 hash / 数据版本 / 报告路径 / 批准人 / 时间），一次 save_status 完成。
      晋级 live 硬性前置：最新 backtest 阶段该策略 gate==pass，
      且 gate.py 自该次通过以来未被修改（hash 一致）。
      入 candidate 硬性前置（防"工业化 HARKing"，quant-wiki 建议 6）：
      必须提供 --differentiation 因子区分度说明（与在库策略的相关性/增量逻辑），
      写入注册表 entry.differentiation，缺失即拒绝（fail closed）。
  python -m pipeline.promote demote <name> --reason "..."
      降级并记录原因（不清空历史，原 promotion 标记为 superseded）。
  python -m pipeline.promote audit
      自检自愈环路：逐个校验所有 live 策略的晋级证据
      （代码/参数/门禁 hash 是否仍匹配、最新门禁是否仍为 pass），
      失配即自动降回 candidate 并写 note（fail closed），返回冲突清单。
      作为流水线阶段（stage_audit）每次 pipeline.run 自动执行。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys

from .common import ROOT, load_status, now, save_status

STRAT_DIR = ROOT / "strategies"
GATE_FILE = ROOT / "pipeline" / "gate.py"
# 注册表 params 里的注解键不参与参数 hash（它们不是策略参数）
PARAM_ANNOTATION_KEYS = {"note_params", "frozen"}


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _load_module_params(name: str) -> dict:
    """读取策略模块的 PARAMS（唯一参数存放处）；失败返回 {}。"""
    f = STRAT_DIR / f"{name}.py"
    if not f.exists():
        return {}
    try:
        spec = importlib.util.spec_from_file_location(f"strategies.{name}", f)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return dict(getattr(mod, "PARAMS", {}) or {})
    except Exception:
        return {}


def _params_hash(name: str, registry_params: dict) -> str:
    """策略参数指纹 = 模块 PARAMS + 注册表覆盖参数（剔除注解键）。"""
    effective = _load_module_params(name)
    effective.update({k: v for k, v in (registry_params or {}).items()
                      if k not in PARAM_ANNOTATION_KEYS})
    blob = json.dumps(effective, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _evidence(name: str, st: dict) -> dict:
    reg = st["strategy_registry"][name]
    data_res = st.get("stages", {}).get("data", {}).get("result", {}) or {}
    return {
        "strategy_file": reg.get("file"),
        "strategy_hash": _sha256(ROOT / reg["file"]),
        "params_hash": _params_hash(name, reg.get("params")),
        "gate_version": "v2",
        "gate_hash": _sha256(GATE_FILE),
        "data_version": data_res.get("latest_trading_day", "unknown"),
        "report": f"reports/{name}",
    }


def _latest_gate(st: dict, name: str):
    """最近一次 backtest 阶段对该策略的门禁判定（pass/reject/None）。"""
    res = st.get("stages", {}).get("backtest", {}).get("result", {}) or {}
    entry = (res.get("results") or {}).get(name)
    return (entry or {}).get("gate")


def promote(name: str, to: str, reason: str, approver: str,
            differentiation: str | None = None) -> dict:
    st = load_status()
    reg = st.get("strategy_registry", {})
    if name not in reg:
        raise SystemExit(f"策略未注册: {name}")
    if to not in ("research", "candidate", "live", "retired"):
        raise SystemExit(f"非法状态: {to}")

    entry = reg[name]
    if to == "candidate":
        # 防"工业化 HARKing"门禁（quant-wiki 建议 6）：入候选必须写明与在库策略的
        # 相关性/增量逻辑（对应文 4"与已有数百异象去重"），防止同义策略重复入库。
        diff = (differentiation or entry.get("differentiation") or "").strip()
        if not diff:
            others = "、".join(k for k, v in reg.items()
                              if k != name and v.get("state") in ("candidate", "live")) or "（空库）"
            raise SystemExit(
                f"拒绝入候选：缺少因子区分度说明。请用 --differentiation 写明 "
                f"{name} 与在库策略（{others}）的相关性/增量逻辑"
                f"（新信息源？新信号结构？新持有期？相关性为何低？）")
        entry["differentiation"] = diff

    ev = _evidence(name, st)
    if to == "live":
        # 硬性前置：最新门禁必须 pass，且回测跑的就是当前这份代码/门禁
        gate = _latest_gate(st, name)
        if gate != "pass":
            raise SystemExit(
                f"拒绝晋级：最新 backtest 门禁={gate!r}（须为 pass）。"
                f"先跑 pipeline.run --stages backtest")
        bt_entry = (st["stages"]["backtest"]["result"]["results"] or {})[name]
        if bt_entry.get("strategy_hash") and bt_entry["strategy_hash"] != ev["strategy_hash"]:
            raise SystemExit("拒绝晋级：门禁通过后策略代码已变更，需重跑回测")

    entry = reg[name]
    old_promotion = entry.get("promotion")
    entry["state"] = to
    entry["promotion"] = {
        **ev, "to": to, "reason": reason, "approver": approver,
        "promoted_at": now(),
    }
    if old_promotion:
        entry["promotion"]["supersedes"] = {
            "promoted_at": old_promotion.get("promoted_at"),
            "to": old_promotion.get("to"),
        }
    # 清理历史手工字段，统一由 promotion 记录承载
    for k in ("promoted_at", "promote_reason", "demoted_at", "demote_reason"):
        entry.pop(k, None)
    save_status(st)  # 原子：state 与证据记录同一次写盘
    return {"name": name, "to": to, "promotion": entry["promotion"]}


def demote(name: str, reason: str) -> dict:
    st = load_status()
    reg = st.get("strategy_registry", {})
    if name not in reg:
        raise SystemExit(f"策略未注册: {name}")
    entry = reg[name]
    old = entry.get("state")
    entry["state"] = "candidate" if old == "live" else entry["state"]
    if entry.get("promotion"):
        entry["promotion"]["superseded"] = True
        entry["promotion"]["supersede_reason"] = reason
    entry["note"] = f"{now()[:10]} 降级（{old}→{entry['state']}）：{reason}"
    save_status(st)
    return {"name": name, "from": old, "to": entry["state"], "reason": reason}


def audit() -> dict:
    """自检自愈环路：校验所有 live 策略晋级证据，失配自动降级。

    返回 {"checked": n, "healthy": [...], "auto_demoted": [{name, conflicts}]}。
    """
    st = load_status()
    reg = st.get("strategy_registry", {})
    healthy, auto_demoted = [], []
    for name, entry in sorted(reg.items()):
        if entry.get("state") != "live":
            continue
        conflicts = []
        p = entry.get("promotion")
        if not p:
            conflicts.append("缺少 promotion 证据记录（疑似手工编辑晋级）")
        else:
            ev = _evidence(name, st)
            for key, label in (("strategy_hash", "策略代码"),
                               ("params_hash", "策略参数"),
                               ("gate_hash", "门禁规则")):
                if p.get(key) != ev[key]:
                    conflicts.append(f"{label} hash 已变更（{p.get(key)}→{ev[key]}），原通过状态失效")
            if _latest_gate(st, name) != "pass":
                conflicts.append(f"最新 backtest 门禁={_latest_gate(st, name)!r}，非 pass")
        if conflicts:
            demote(name, "audit 自检发现证据失效：" + "；".join(conflicts))
            auto_demoted.append({"name": name, "conflicts": conflicts})
        else:
            healthy.append(name)
    return {"checked": len(healthy) + len(auto_demoted),
            "healthy": healthy, "auto_demoted": auto_demoted,
            "audited_at": now()}


def main():
    ap = argparse.ArgumentParser(prog="pipeline.promote")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("promote")
    p.add_argument("name")
    p.add_argument("--to", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--approver", default="老板")
    p.add_argument("--differentiation", default=None,
                   help="入 candidate 必填：与在库策略的相关性/增量逻辑说明（防同义策略重复入库）")
    d = sub.add_parser("demote")
    d.add_argument("name")
    d.add_argument("--reason", required=True)
    sub.add_parser("audit")
    args = ap.parse_args()

    if args.cmd == "promote":
        out = promote(args.name, args.to, args.reason, args.approver,
                      differentiation=args.differentiation)
    elif args.cmd == "demote":
        out = demote(args.name, args.reason)
    else:
        out = audit()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    if args.cmd == "audit" and out["auto_demoted"]:
        sys.exit(1)  # 让调用方（CI/cron）明确感知发生过自愈降级


if __name__ == "__main__":
    main()
