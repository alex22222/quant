# -*- coding: utf-8 -*-
"""回测门禁 v2（改进计划 Phase 2）

旧门禁（年化>5%、回撤<30%、夏普>0.3）只能证明"没崩"，不能证明有可交易 alpha。
门禁 v2 要求：

1. 全样本（FULL）与样本外（OOS）都达标；
2. 超额收益为正（几何年化超额 > 0），信息比率达标；
3. 换手率封顶（防止成本敏感型策略伪装 alpha；rqalpha 回测已含佣金/印花税，
   另加固定滑点近似冲击成本）；
4. 最长回撤持续天数封顶（套牢时间也是风险）；
5. 参数扰动：动量窗口 ±25%、持仓数 ±1，扰动后基础门禁不得反转；
6. 策略族去重：同族（FAMILY 相同）只允许一个代表通过门禁。

指标来源：rqalpha report summary.xlsx（含基准对照指标）。
"""
from __future__ import annotations

GATE_V2 = {
    # 全样本门槛
    "full": {
        "annual_min": 0.05,
        "max_dd_max": 0.30,
        "sharpe_min": 0.30,
        "excess_annual_min": 0.0,      # 几何年化超额必须为正
        "ir_min": 0.30,                # 信息比率
        "excess_max_dd_max": 0.40,     # 超额收益最大回撤
        "turnover_annual_max": 25.0,   # 年化双边换手封顶
        "longest_dd_days_max": 1500,   # 最长回撤持续天数
    },
    # 样本外门槛（更宽，但必须为正）
    "oos": {
        "annual_min": 0.0,
        "excess_annual_min": 0.0,
        "max_dd_max": 0.35,
        "sharpe_min": 0.0,
    },
}

# 扰动规则：参数名 → 扰动倍数/增量列表
PERTURB_RULES = {
    "momentum_days": lambda v: sorted({max(int(v * 0.75), 5), v, int(v * 1.25)}),
    "hold_num": lambda v: sorted({max(v - 1, 1), v, v + 1}),
}

METRIC_MAP = {
    "annual": "年化收益率",
    "max_dd": "最大回撤",
    "sharpe": "夏普比率",
    "excess_annual": "年化超额收益（几何）",
    "ir": "信息比率",
    "excess_max_dd": "超额收益最大回撤（几何）",
    "turnover_annual": "年化双边换手",
    "longest_dd_days": "最长回撤持续天数",
    "total_ret": "收益率",
}


def extract_metrics(summary: dict) -> dict:
    """summary.xlsx 读出的 dict → 标准指标 dict（缺失字段记 None）。"""
    out = {}
    for key, col in METRIC_MAP.items():
        v = summary.get(col)
        try:
            out[key] = float(v) if v is not None else None
        except (TypeError, ValueError):
            out[key] = None
    return out


def _check(m: dict, gate: dict, label: str) -> list[str]:
    reasons = []

    def need(metric, op, threshold, fmt="{:.2f}"):
        v = m.get(metric)
        if v is None:
            reasons.append(f"{label}: 缺少指标 {metric}")
            return
        ok = (v >= threshold) if op == ">=" else (v <= threshold)
        if not ok:
            reasons.append(f"{label}: {metric}={fmt.format(v)} 未满足 {op} {fmt.format(threshold)}")

    need("annual", ">=", gate["annual_min"], "{:.1%}")
    need("max_dd", "<=", gate["max_dd_max"], "{:.1%}")
    need("sharpe", ">=", gate["sharpe_min"])
    if "excess_annual_min" in gate:
        need("excess_annual", ">=", gate["excess_annual_min"], "{:.1%}")
    if "ir_min" in gate:
        need("ir", ">=", gate["ir_min"])
    if "excess_max_dd_max" in gate:
        need("excess_max_dd", "<=", gate["excess_max_dd_max"], "{:.1%}")
    if "turnover_annual_max" in gate:
        need("turnover_annual", "<=", gate["turnover_annual_max"], "{:.1f}")
    if "longest_dd_days_max" in gate:
        need("longest_dd_days", "<=", gate["longest_dd_days_max"], "{:.0f}")
    return reasons


def evaluate(full_metrics: dict, oos_metrics: dict | None,
             perturb_metrics: list[dict] | None,
             family_status: dict | None) -> dict:
    """综合判定。返回 {"gate": pass/reject, "reasons": [...], "detail": {...}}"""
    reasons = []

    # 1) 全样本
    reasons += _check(full_metrics, GATE_V2["full"], "全样本")

    # 2) 样本外
    if oos_metrics is None:
        reasons.append("样本外: 未运行")
    else:
        reasons += _check(oos_metrics, GATE_V2["oos"], "样本外")

    # 3) 参数扰动：每个扰动变体必须过全样本基础四项（年化/回撤/夏普/超额）
    base_gate = {k: GATE_V2["full"][k] for k in
                 ("annual_min", "max_dd_max", "sharpe_min", "excess_annual_min")}
    for i, pm in enumerate(perturb_metrics or []):
        sub = _check(pm, base_gate, f"扰动{i+1}({pm.get('variant', '?')})")
        reasons += sub

    # 4) 策略族去重
    if family_status and family_status.get("family_dup"):
        reasons.append(
            f"策略族 {family_status.get('family')} 已有代表 "
            f"{family_status.get('family_representative')}，同族不重复通过")

    return {
        "gate": "reject" if reasons else "pass",
        "reasons": reasons,
        "detail": {"full": full_metrics, "oos": oos_metrics,
                   "perturbations": perturb_metrics or []},
    }
