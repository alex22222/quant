# -*- coding: utf-8 -*-
"""老板批复台账：trading_team/loops/approvals.json

批复来源：console 审批面板（POST /api/approve）或 Kimi 对话（agent 代记）。
事实源仅此文件 + AGENT.md Loop「人工决策」留痕；console/飞书只读它。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

TEAM = Path(__file__).resolve().parent
FILE = TEAM / "loops" / "approvals.json"
VALID = ("approved", "rejected", "reduced")


def load() -> dict:
    if FILE.exists():
        return json.loads(FILE.read_text(encoding="utf-8"))
    return {"decisions": []}


def save(data: dict) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def record(day: str, code: str, name: str, decision: str, note: str = "",
           source: str = "console") -> dict:
    if decision not in VALID:
        raise ValueError(f"decision 必须是 {VALID}")
    data = load()
    # 同日同标的重复批复 → 覆盖（以最新为准）
    data["decisions"] = [d for d in data["decisions"]
                         if not (d["date"] == day and d["code"] == code)]
    entry = {"date": day, "code": code, "name": name, "decision": decision,
             "note": note, "source": source,
             "decided_at": datetime.now().isoformat(timespec="seconds")}
    data["decisions"].append(entry)
    save(data)
    log_to_agent_md(entry)
    return entry


def log_to_agent_md(entry: dict) -> None:
    """在 AGENT.md 最新 Loop 条目的「人工决策」追加留痕。"""
    agent_md = TEAM.parent / "AGENT.md"
    if not agent_md.exists():
        return
    text = agent_md.read_text(encoding="utf-8")
    last_loop = text.rfind("\n## Loop #")
    if last_loop == -1:
        return
    label = {"approved": "批准", "rejected": "否决", "reduced": "降仓批准"}[entry["decision"]]
    line = f"{entry['decided_at'][:10]} 批复 {entry['name']}({entry['code']}): {label}（{entry['source']}）{('；' + entry['note']) if entry['note'] else ''}"
    head, tail = text[:last_loop], text[last_loop:]
    import re
    m = re.search(r"(- \*\*人工决策\*\*: )(.*?)\n", tail)
    if not m:
        return
    old = m.group(2).strip()
    new = line if old in ("（待填写）", "", "（待填写——含 Loop #5 首日提案：平安 15% 分批买入计划，待批复）") \
        else f"{old}；{line}"
    tail = tail[:m.start()] + m.group(1) + new + "\n" + tail[m.end():]
    agent_md.write_text(head + tail, encoding="utf-8")


def decision_for(day: str, code: str) -> dict | None:
    for d in reversed(load()["decisions"]):
        if d["date"] == day and d["code"] == code:
            return d
    return None


def pending(day: str) -> list[dict]:
    """当日提案中尚未批复的（观望/回避类 0% 仓位提案也需要批复确认）。"""
    plan_file = TEAM / "outputs" / day / "plan.json"
    if not plan_file.exists():
        return []
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    return [p for p in plan.get("proposals", []) if decision_for(day, p["code"]) is None]
