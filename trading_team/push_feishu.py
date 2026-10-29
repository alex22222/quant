# -*- coding: utf-8 -*-
"""交易团队日报 → 飞书卡片推送（复用 quant-x-monitor 的飞书应用凭证）。

用法:  .venv/bin/python -m trading_team.push_feishu [--date YYYY-MM-DD]

推送内容：一句话总览 + 各成员结论速览 + 最终放行计划 + 决策入口指引。
凭证只从 quant-x-monitor/config.json 读取，永不打印。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "quant-x-monitor"))
from feishu_pusher import get_feishu_token  # noqa: E402

from trading_team.notification_outbox import NotSent, notification_id, push_once  # noqa: E402
from trading_team.state_lock import state_lock  # noqa: E402

TEAM = Path(__file__).resolve().parent


def _config() -> dict:
    return json.loads((ROOT / "quant-x-monitor/config.json").read_text(encoding="utf-8"))["feishu"]


def extract_section(md: str, title_pat: str) -> str:
    """抽取 '## <title>' 到下一个 '## ' 之间的正文。"""
    m = re.search(title_pat + r"\s*\n([\s\S]*?)(?=\n## |\Z)", md)
    return m.group(1).strip() if m else ""


def build_card(day: str, team: Path | None = None) -> dict | None:
    team = team or TEAM
    day_dir = team / "outputs" / day
    concl = day_dir / "daily_conclusion.md"
    if not concl.exists():
        return None
    md = concl.read_text(encoding="utf-8")

    headline = extract_section(md, r"## 一句话总览").strip("*").strip() or "（无总览）"
    plan = extract_section(md, r"## 最终放行计划[^\n]*")
    verify = extract_section(md, r"## 下[^\n]*验证点[^\n]*")

    plan_doc = json.loads((day_dir / "plan.json").read_text(encoding="utf-8"))
    manifest = json.loads((team / "context" / day / "manifest.json").read_text(encoding="utf-8"))
    data_asof = plan_doc["data_asof"]
    collect_ok = manifest["ok"]

    elements = [
        {"tag": "div", "text": {"tag": "lark_md",
         "content": f"**{headline}**"}},
        {"tag": "div", "text": {"tag": "plain_text",
         "content": f"数据截至 {data_asof} · 采集{'正常' if collect_ok else '部分源异常'}",
         "style": {"font_size": 12}}},
        {"tag": "hr"},
    ]
    if plan:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": "**📋 最终放行计划（风控已审）**\n" + plan}})
    if verify:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": "**🔭 下一交易日验证点**\n" + verify}})

    # 未批复提案清单
    decision = json.loads((team / "decisions" / day / "decision.json").read_text(encoding="utf-8"))
    pend = [p for p in decision["plans"] if p.get("approval", "pending") == "pending"]
    if pend:
        items = "、".join(f"{p['name']}({p['direction']} {p.get('position_pct', '')})" for p in pend)
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": f"**⏳ 待你批复（{len(pend)} 项）**：{items}"}})
    else:
        counts = {state: sum(p.get("approval") == state for p in decision["plans"])
                  for state in ("approved", "reduced", "rejected")}
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": (f"当日提案已全部处理：批准 {counts['approved']}、"
                                     f"降仓批准 {counts['reduced']}、否决 {counts['rejected']}"
                                     if decision["plans"] else "今日无提案")}})

    elements += [
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md",
         "content": "**🖐 决策入口**：本卡为团队提案。批复方式——① 控制台「交易团队日报」审批面板点按钮："
                    "http://localhost:7100/console/ （电脑浏览器打开）② Codex 对话直接说「批准/否决 + 标的」。"}},
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue",
                   "title": {"tag": "plain_text", "content": f"🤖 交易团队日报 | {day}"}},
        "elements": elements,
    }


def _verify_source(day: str, team: Path) -> str:
    from trading_team.kimi_daily import inspect_generation
    from trading_team.run_ledger import RunLedger
    from trading_team.approvals import status
    from trading_team.approval_audit import validate_decision_facts
    from trading_team.decision_pipeline import canonical_plan_hash
    from trading_team.daily_finalize import _paper_evidence
    from trading_team.multica_reviews import require_reviews
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("invalid notification date")
    evidence = inspect_generation(team, day, RunLedger(team / ".runs/ledger.db", readonly=True))
    publisher = evidence["publisher"]
    if publisher["status"] != "succeeded" or publisher["output_hash"] != evidence["receipt_hash"]:
        raise ValueError("generation acknowledgement missing")
    plan = json.loads((team / "outputs" / day / "plan.json").read_text(encoding="utf-8"))
    plan_hash = canonical_plan_hash(plan)
    for name in ("decision.json", "paper_decision.json"):
        decision = json.loads((team / "decisions" / day / name).read_text(encoding="utf-8"))
        if decision.get("date") != day or decision.get("plan_hash") != plan_hash:
            raise ValueError("notification decision version mismatch")
        if name == "decision.json":
            validate_decision_facts(decision, plan)
    if status(day, team=team)["status"] not in ("complete", "legacy_untracked"):
        raise ValueError("notification approval evidence requires recovery")
    require_reviews(day, plan_hash, team=team)
    _paper_evidence(day, plan_hash, team)
    return plan_hash


def _send(day: str, plan_hash: str, card: dict, cfg: dict) -> dict:
    token = get_feishu_token(cfg["app_id"], cfg["app_secret"])
    if not token:
        raise NotSent("Feishu token unavailable before message request")
    response = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        params={"receive_id_type": cfg["receive_id_type"]},
        json={"receive_id": cfg["receive_id"], "msg_type": "interactive",
              "content": json.dumps(card, ensure_ascii=False), "uuid": notification_id(day, plan_hash)},
        timeout=10,
    )
    body = response.json()
    message_id = body.get("data", {}).get("message_id") if isinstance(body, dict) else None
    if (response.status_code != 200 or body.get("code") != 0
            or not isinstance(message_id, str) or not message_id.strip()):
        raise RuntimeError("Feishu message acknowledgement unavailable")
    return {"pushed": True, "date": day, "message_id": message_id}


def push(day: str, *, team: Path | None = None) -> dict:
    team = team or TEAM
    with state_lock(team):
        plan_hash = _verify_source(day, team)
        card = build_card(day, team)
        if not card:
            raise NotSent("daily conclusion missing before message request")
        cfg = _config()
        identity = {"card": card, "app_id": cfg["app_id"], "receive_id": cfg["receive_id"],
                    "receive_id_type": cfg["receive_id_type"]}
        fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return push_once(day, plan_hash, team=team, fingerprint=fingerprint,
                         sender=lambda _: _send(day, plan_hash, card, cfg))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()
    print(push(args.date))
