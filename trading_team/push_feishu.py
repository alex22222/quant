# -*- coding: utf-8 -*-
"""交易团队日报 → 飞书卡片推送（复用 quant-x-monitor 的飞书应用凭证）。

用法:  .venv/bin/python -m trading_team.push_feishu [--date YYYY-MM-DD]

推送内容：一句话总览 + 各成员结论速览 + 最终放行计划 + 决策入口指引。
凭证只从 quant-x-monitor/config.json 读取，永不打印。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "quant-x-monitor"))
from feishu_pusher import get_feishu_token, send_feishu_interactive  # noqa: E402

from trading_team import approvals  # noqa: E402

CFG = json.loads((ROOT / "quant-x-monitor" / "config.json").read_text(encoding="utf-8"))["feishu"]
TEAM = Path(__file__).resolve().parent


def extract_section(md: str, title_pat: str) -> str:
    """抽取 '## <title>' 到下一个 '## ' 之间的正文。"""
    m = re.search(title_pat + r"\s*\n([\s\S]*?)(?=\n## |\Z)", md)
    return m.group(1).strip() if m else ""


def build_card(day: str) -> dict | None:
    day_dir = TEAM / "outputs" / day
    concl = day_dir / "daily_conclusion.md"
    if not concl.exists():
        return None
    md = concl.read_text(encoding="utf-8")

    headline = extract_section(md, r"## 一句话总览").strip("*").strip() or "（无总览）"
    plan = extract_section(md, r"## 最终放行计划[^\n]*")
    verify = extract_section(md, r"## 下[^\n]*验证点[^\n]*")

    idx = json.loads((TEAM / "outputs" / "index.json").read_text(encoding="utf-8"))
    meta = next((d for d in idx if d["date"] == day), {})
    ratings = meta.get("ratings_line", "")
    data_asof = meta.get("data_asof", "—")
    collect_ok = meta.get("collect_ok", True)

    elements = [
        {"tag": "div", "text": {"tag": "lark_md",
         "content": f"**{headline}**"}},
        {"tag": "div", "text": {"tag": "plain_text",
         "content": f"数据截至 {data_asof} · 采集{'正常' if collect_ok else '部分源异常'}",
         "style": {"font_size": 12}}},
        {"tag": "hr"},
    ]
    if ratings:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": f"**研究员评级**  {ratings}"}})
    if plan:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": "**📋 最终放行计划（风控已审）**\n" + plan}})
    if verify:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": "**🔭 下一交易日验证点**\n" + verify}})

    # 未批复提案清单
    pend = approvals.pending(day)
    if pend:
        items = "、".join(f"{p['name']}({p['direction']} {p.get('position_pct', '')})" for p in pend)
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": f"**⏳ 待你批复（{len(pend)} 项）**：{items}"}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": "✅ 当日提案已全部批复"}})

    elements += [
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md",
         "content": "**🖐 决策入口**：本卡为团队提案。批复方式——① 控制台「交易团队日报」审批面板点按钮："
                    "http://localhost:7100/console/ （电脑浏览器打开）② Kimi 对话直接说「批准/否决 + 标的」。"}},
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue",
                   "title": {"tag": "plain_text", "content": f"🤖 交易团队日报 | {day}"}},
        "elements": elements,
    }


def push(day: str) -> dict:
    card = build_card(day)
    if not card:
        return {"pushed": False, "reason": f"{day} 无 daily_conclusion.md"}
    token = get_feishu_token(CFG["app_id"], CFG["app_secret"])
    if not token:
        raise RuntimeError("飞书 token 获取失败")
    ok = send_feishu_interactive(token, CFG["receive_id"], CFG["receive_id_type"], card)
    return {"pushed": ok, "date": day}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()
    print(push(args.date))
