#!/usr/bin/env python3
"""从飞书群拉取大V监控日报卡片，回填为本地按日存档 reports/YYYY-MM-DD.json

卡片实际结构：{"title": ..., "elements": [[{tag:text|a|hr, ...}], ...]}
条目行特征：行内含 tag=a 的「查看原文」链接。
同日多次推送按 URL 去重合并。
"""
import json, os, sys, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from feishu_pusher import get_feishu_token
import requests

BASE = os.path.dirname(os.path.abspath(__file__))

def parse_card(body):
    """返回 (date_str, [items])，非日报卡片返回 (None, [])"""
    rows = body.get("elements", [])
    if not rows or not isinstance(rows[0], list):
        return None, []
    first = rows[0][0].get("text", "") if rows[0] else ""
    m = re.search(r"日报\s*\|\s*(\d{4}-\d{2}-\d{2})", first)
    if not m:
        return None, []
    date = m.group(1)
    items = []
    for row in rows:
        if not isinstance(row, list):
            continue
        link = next((e for e in row if e.get("tag") == "a" and e.get("href")), None)
        if not link:
            continue
        texts = [e.get("text", "") for e in row if e.get("tag") == "text"]
        if not texts:
            continue
        author = texts[0].strip().strip("*").strip()
        joined = "".join(texts[1:])
        t_m = re.search(r"🕐\s*([^\n]+)", joined)
        # 去掉时间行后，第一行非空为标题，其余为摘要
        body_txt = re.sub(r"🕐[^\n]*", "", joined)
        lines = [l.strip() for l in body_txt.split("\n") if l.strip()]
        items.append({
            "platform": "x",
            "author": author,
            "title": lines[0] if lines else "",
            "url": link["href"],
            "publish_time": t_m.group(1).strip() if t_m else "",
            "summary": "\n".join(lines[1:]) if len(lines) > 1 else "",
        })
    return date, items

def main():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    fs = cfg["feishu"]
    token = get_feishu_token(fs["app_id"], fs["app_secret"])
    if not token:
        print("[Error] token 获取失败"); sys.exit(1)

    r = requests.get(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        params={"container_id_type": "chat", "container_id": fs["receive_id"],
                "sort_type": "ByCreateTimeDesc", "page_size": 50},
        timeout=15)
    data = r.json()
    if data.get("code") != 0:
        print(f"[Error] 拉取消息失败: {data}"); sys.exit(1)

    msgs = data["data"].get("items", [])
    reports = {}
    for m in msgs:
        if m.get("msg_type") != "interactive":
            continue
        try:
            body = json.loads(m["body"]["content"])
        except Exception:
            continue
        date, items = parse_card(body)
        if date:
            reports.setdefault(date, []).extend(items)

    out_dir = os.path.join(BASE, "reports")
    os.makedirs(out_dir, exist_ok=True)
    stats = {}
    for date, items in sorted(reports.items()):
        seen, uniq = set(), []
        for it in items:
            key = it["url"] or (it["author"] + it["title"])
            if key in seen:
                continue
            seen.add(key)
            uniq.append(it)
        path = os.path.join(out_dir, f"{date}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(uniq, f, ensure_ascii=False, indent=2)
        stats[date] = len(uniq)
        print(f"[Archive] {date}: {len(uniq)} 条", file=sys.stderr)
    print(json.dumps(stats, ensure_ascii=False))

if __name__ == "__main__":
    main()
