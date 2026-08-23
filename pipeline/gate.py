# -*- coding: utf-8 -*-
"""回测门禁 v2（改进计划 Phase 2）

旧门禁（年化>5%、回撤<30%、夏普>0.3）只能证明"没崩"，不能证明有可交易 alpha。
门禁 v2 要求：

1. 全样本（FULL）与验证集（VALIDATION）都达标；
2. 超额收益为正（几何年化超额 > 0），信息比率达标；
3. 换手率封顶（防止成本敏感型策略伪装 alpha；rqalpha 回测已含佣金/印花税，
   另加固定滑点近似冲击成本）；
4. 最长回撤持续天数封顶（套牢时间也是风险）；
5. 参数扰动：动量窗口 ±25%、持仓数 ±1，扰动后基础门禁不得反转；
6. 策略族去重：同族（FAMILY 相同）只允许一个代表通过门禁；
7. 多重试验修正（2026-08-23 审查新增）：尝试越多参数组合，偶然通过概率越高。
   采用简化版 Deflated Sharpe Ratio（Bailey & López de Prado, 2014）：
   以扰动变体的夏普方差估计"试验簇"的夏普离散度，n_trials 取
   实验台账中该策略的不同参数组合数 + 本次扰动数；DSR < 0.95 拒绝。

指标来源：rqalpha report summary.xlsx（含基准对照指标）。

⚠️ 数据分区语义（2026-08-23 策略库审查后更正）：
2024-01-01~2026-08-01 窗口已被 E3-E18 等参数实验反复用于调参，
它是 **validation（验证集）**，不是独立 OOS。本文件保留 "oos" 键名仅为兼容
历史报告，展示口径一律称"验证集"。真正的 test set 是门禁冻结后的纯前向数据。
门禁规则与策略参数必须分离提交；门禁改动须独立 commit 并对全部策略重跑生效，
禁止与任何策略晋级出现在同一提交（见 docs/STRATEGY_LIBRARY_REVIEW_A_SHARE.md）。
"""
from __future__ import annotations

# 数据分区声明（报告与复盘统一引用此处，禁止各处自定义）
DATA_PARTITION = {
    "train": "2020-01-01~2023-12-31",
    "validation": "2024-01-01~2026-08-01（已被调参使用，非独立样本外）",
    "test": "纯前向数据（2026-08-23 起累积，尚未存在）",
}

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
    # 验证集门槛（更宽，但必须为正）
    # ⚠️ 语义：2024-01 起的窗口是 validation（已被调参使用），不是独立 OOS
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
    "n_entry": lambda v: sorted({max(int(v * 0.75), 5), v, int(v * 1.25)}),
    "n_exit": lambda v: sorted({max(int(v * 0.75), 3), v, int(v * 1.25)}),
    "hold_num": lambda v: sorted({max(v - 1, 1), v, v + 1}),
}

DSR_MIN = 0.95  # 简化 DSR 门槛：在多重试验下仍有 95% 置信度夏普>期望最大伪夏普


def deflated_sharpe(sr: float, n_trials: int, sr_var: float, T: int,
                    skew: float = 0.0, kurt: float = 3.0) -> float | None:
    """简化 Deflated Sharpe Ratio（Bailey & López de Prado）。

    sr: 全样本夏普；n_trials: 试验次数；sr_var: 试验簇夏普方差；
    T: 收益观测数（月）；skew/kurt: 月收益偏度/峰度。
    返回 0~1 的置信度；输入不足返回 None（不参与判定）。"""
    if n_trials < 2 or sr_var <= 0 or T < 12 or sr is None:
        return None
    from math import exp, log, pi, sqrt

    from statistics import NormalDist
    norm = NormalDist()
    gamma = 0.5772156649  # Euler-Mascheroni
    # 期望最大伪夏普 E[max SR]（N 次独立试验，SR~N(0, sr_var)）
    z1 = norm.inv_cdf(1 - 1 / n_trials)
    z2 = norm.inv_cdf(1 - 1 / (n_trials * exp(1)))
    sr0 = sqrt(sr_var) * ((1 - gamma) * z1 + gamma * z2)
    denom = sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr * sr, 1e-8))
    return norm.cdf((sr - sr0) * sqrt(T - 1) / denom)

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
             family_status: dict | None,
             n_trials: int = 1) -> dict:
    """综合判定。返回 {"gate": pass/reject, "reasons": [...], "detail": {...}}"""
    reasons = []

    # 1) 全样本
    reasons += _check(full_metrics, GATE_V2["full"], "全样本")

    # 2) 验证集（键名 oos 仅为历史兼容；2024-2026 已被调参，非独立样本外）
    if oos_metrics is None:
        reasons.append("验证集: 未运行")
    else:
        reasons += _check(oos_metrics, GATE_V2["oos"], "验证集")

    # 3) 参数扰动：结论不得反转（仍有 alpha），允许风险指标在容差带内劣化
    #    容差带设计：回撤 ≤ 门禁×1.2（趋势策略集中持仓变体的回撤/收益同比例放大，
    #    属风险-收益权衡而非结论反转）；年化/夏普/超额收益仍按门禁严格判定
    base_gate = {
        "annual_min": GATE_V2["full"]["annual_min"],
        "max_dd_max": GATE_V2["full"]["max_dd_max"] * 1.2,  # 回撤容差带 +20%
        "sharpe_min": GATE_V2["full"]["sharpe_min"],
        "excess_annual_min": GATE_V2["full"]["excess_annual_min"],
    }
    for i, pm in enumerate(perturb_metrics or []):
        sub = _check(pm, base_gate, f"扰动{i+1}({pm.get('variant', '?')})")
        reasons += sub

    # 4) 多重试验修正：简化 DSR（试验簇夏普方差由扰动变体估计）
    perts = perturb_metrics or []
    sharpes = [p["sharpe"] for p in perts if p.get("sharpe") is not None]
    sharpes = [full_metrics.get("sharpe")] + sharpes if full_metrics.get("sharpe") is not None else sharpes
    dsr = None
    if len(sharpes) >= 2:
        import statistics
        sr_var = statistics.pvariance(sharpes)
        dsr = deflated_sharpe(full_metrics.get("sharpe"),
                              n_trials=max(n_trials, len(sharpes)),
                              sr_var=sr_var,
                              T=int(full_metrics.get("months") or 0),
                              skew=full_metrics.get("skew") or 0.0,
                              kurt=full_metrics.get("kurt") or 3.0)
        if dsr is not None and dsr < DSR_MIN:
            reasons.append(f"多重试验: DSR={dsr:.2f} 未满足 >= {DSR_MIN:.2f}"
                           f"（n_trials={max(n_trials, len(sharpes))}，试验越多门槛越高）")

    # 5) 策略族去重
    if family_status and family_status.get("family_dup"):
        reasons.append(
            f"策略族 {family_status.get('family')} 已有代表 "
            f"{family_status.get('family_representative')}，同族不重复通过")

    return {
        "gate": "reject" if reasons else "pass",
        "reasons": reasons,
        "detail": {"full": full_metrics, "oos": oos_metrics,
                   "perturbations": perturb_metrics or [],
                   "dsr": dsr, "n_trials": n_trials},
    }
