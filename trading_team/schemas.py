# -*- coding: utf-8 -*-
"""决策/计划结构化 schema 与解析工具（唯一权威定义）

改进计划 Phase 0：
- position_pct 必须是数字（0-1），禁止 "15%（8%+7% 分批）" 这类自由文本；
- entry 必须是数字数组（限价/回踩价列表），条件说明放 condition 字段；
- 所有读写 decision.json / plan.json 的模块统一从这里校验与解析。

plan 字段（decision.json 的 plans[] 与 plan.json 的 proposals[] 共用）：

    {
      "code": "601318",              # 必填，6 位代码
      "name": "中国平安",
      "direction": "买入",            # 买入/条件买入/卖出/减持/持有/观望/回避
      "position_pct": 0.15,          # 数字 0-1；观望/回避为 0
      "entry": [53.5, 52.2],         # 数字数组，可为 null
      "entry_type": "limit_or_pullback",  # market/limit/pullback/limit_or_pullback，可为 null
      "stop": 50.6,                  # 数字或 null
      "condition": "高开超过3%不追",   # 自由文本条件只准放这里
      "invalid_if": "...",           # 失效条件（decision.json 用）
      "horizon": "2-4 周",
      "confidence": 3,               # 1-5
      # —— 审批写回字段（由 approvals.record 维护，LLM 不得生成）——
      "approval": "approved",        # pending/approved/rejected/reduced
      "approval_note": "",
      "approved_at": "2026-08-22T23:53:13",
      "executable": false,           # 是否可进入执行器（确定性计算）
      "blocked_reason": "market_ok=false"
    }
"""
from __future__ import annotations

import re

BUY_WORDS = ("买入", "加仓")
SELL_WORDS = ("卖出", "减持", "回避")
HOLD_WORDS = ("持有",)
WATCH_WORDS = ("观望",)


class SchemaError(ValueError):
    pass


def parse_pct(value, field="position_pct"):
    """把 position_pct 严格解析成 0-1 的 float。

    接受：float/int（0-1 或 0-100 百分数）、纯数字字符串、"15%"。
    拒绝：含中文/说明文字的字符串（如 "15%（8%+7% 分批）"）→ SchemaError。
    """
    if value is None:
        return 0.0
    if isinstance(value, bool):
        raise SchemaError(f"{field} 不能是布尔值")
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1:  # 容忍 15 → 0.15 的百分数写法
            v = v / 100.0
        if not (0.0 <= v <= 1.0):
            raise SchemaError(f"{field}={value} 超出 [0,1] 范围")
        return v
    if isinstance(value, str):
        s = value.strip()
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%?", s)
        if not m:
            raise SchemaError(
                f"{field} 含非数字内容: {value!r}；"
                f"仓位必须是数字，条件说明请放 condition 字段"
            )
        v = float(m.group(1))
        if "%" in s or v > 1:
            v = v / 100.0
        if not (0.0 <= v <= 1.0):
            raise SchemaError(f"{field}={value} 超出 [0,1] 范围")
        return v
    raise SchemaError(f"{field} 类型不支持: {type(value).__name__}")


def parse_entry(value):
    """entry 解析为 float 列表；null/"—"/"" → None。字符串数字列表逗号分隔可解析。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, list):
        out = []
        for x in value:
            try:
                out.append(float(x))
            except (TypeError, ValueError):
                raise SchemaError(f"entry 含非数字项: {x!r}")
        return out or None
    if isinstance(value, str):
        s = value.strip()
        if s in ("", "—", "-", "null"):
            return None
        parts = re.split(r"[,，/、\s]+", s)
        nums = []
        for p in parts:
            if not p:
                continue
            try:
                nums.append(float(p))
            except ValueError:
                raise SchemaError(
                    f"entry 含非数字内容: {value!r}；入场条件说明请放 condition 字段"
                )
        return nums or None
    raise SchemaError(f"entry 类型不支持: {type(value).__name__}")


def is_buy(direction: str, position_pct: float = 0.0) -> bool:
    if any(w in direction for w in BUY_WORDS):
        return True
    if any(w in direction for w in SELL_WORDS + WATCH_WORDS):
        return False
    return position_pct > 0


def is_sell(direction: str) -> bool:
    return any(w in direction for w in SELL_WORDS)


def validate_plan(plan: dict, strict: bool = False) -> list[str]:
    """校验单条 plan，返回问题列表（空 = 通过）。strict=True 时字符串仓位也算错误。"""
    problems = []
    code = plan.get("code")
    if not code or not re.fullmatch(r"\d{6}(\.(XSHG|XSHE))?", str(code)):
        problems.append(f"code 非法: {code!r}")
    direction = str(plan.get("direction", ""))
    if not direction:
        problems.append("direction 缺失")
    try:
        pct = parse_pct(plan.get("position_pct"))
    except SchemaError as e:
        problems.append(str(e))
        pct = None
    if pct is not None and pct > 0 and not is_buy(direction, pct):
        problems.append(f"direction={direction!r} 与 position_pct={pct} 矛盾")
    try:
        parse_entry(plan.get("entry"))
    except SchemaError as e:
        problems.append(str(e))
    if strict:
        if not isinstance(plan.get("position_pct"), (int, float)):
            problems.append("strict 模式: position_pct 必须是数字类型")
        entry = plan.get("entry")
        if entry is not None and not isinstance(entry, list):
            problems.append("strict 模式: entry 必须是数组或 null")
    return problems


def compute_executable(plan: dict, market_ok: bool) -> tuple[bool, str]:
    """确定性计算一条 plan 是否可执行。返回 (executable, blocked_reason)。

    规则（fail closed）：
    - approval 必须是 approved（reduced 由执行器减半处理，视为可执行）
    - market_ok=false 时禁止任何买入类计划可执行
    - position_pct 解析失败一律不可执行
    """
    approval = plan.get("approval", "pending")
    if approval not in ("approved", "reduced"):
        return False, f"approval={approval}"
    direction = str(plan.get("direction", ""))
    try:
        pct = parse_pct(plan.get("position_pct"))
    except SchemaError as e:
        return False, str(e)
    if is_buy(direction, pct) and pct > 0 and not market_ok:
        return False, "market_ok=false 禁止买入"
    return True, ""
