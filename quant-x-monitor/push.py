#!/usr/bin/env python3
"""
大V内容监控 - 飞书推送脚本
用法: python feishu_pusher.py --config config.json --items-file items.json
或:   python feishu_pusher.py --config config.json --text "纯文本消息"
"""
import argparse
import json
import sys
import os

# 将脚本目录加入路径，以便导入 utils
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from feishu_pusher import get_feishu_token, send_feishu_text, send_feishu_interactive, build_content_card

def main():
    parser = argparse.ArgumentParser(description="推送大V监控内容到飞书")
    parser.add_argument("--config", required=True, help="配置文件路径")
    parser.add_argument("--items-file", help="内容项 JSON 文件路径")
    parser.add_argument("--text", help="纯文本消息内容")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = json.load(f)

    feishu_cfg = config.get("feishu", {})
    app_id = feishu_cfg.get("app_id", "")
    app_secret = feishu_cfg.get("app_secret", "")
    receive_id = feishu_cfg.get("receive_id", "")
    receive_id_type = feishu_cfg.get("receive_id_type", "open_id")

    if not app_id or not app_secret:
        print("[Error] 飞书 app_id 或 app_secret 未配置")
        sys.exit(1)
    if not receive_id:
        print("[Error] 飞书 receive_id 未配置")
        sys.exit(1)

    token = get_feishu_token(app_id, app_secret)
    if not token:
        print("[Error] 获取飞书 token 失败")
        sys.exit(1)

    if args.text:
        send_feishu_text(token, receive_id, receive_id_type, args.text)
    elif args.items_file:
        with open(args.items_file, "r", encoding="utf-8") as f:
            items = json.load(f)
        from datetime import datetime
        date_str = datetime.now().strftime("%Y-%m-%d")
        card = build_content_card(items, date_str)
        send_feishu_interactive(token, receive_id, receive_id_type, card)
    else:
        print("[Error] 请提供 --items-file 或 --text")
        sys.exit(1)

if __name__ == "__main__":
    main()
