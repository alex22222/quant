#!/usr/bin/env python3
"""
大V内容监控 - 协调脚本
提供任务列表生成、结果收集、状态管理功能
"""
import argparse
import json
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import load_config, load_state, save_state, is_new_content, mark_sent

def list_tasks(config_path: str):
    """输出需要执行的搜索任务列表"""
    config = load_config()
    tasks = []
    for platform, cfg in config.get("platforms", {}).items():
        if not cfg.get("enabled", False):
            continue
        for account in cfg.get("accounts", []):
            keyword = account.get("keyword", account.get("name", ""))
            if not keyword:
                continue
            tasks.append({
                "platform": platform,
                "account_name": account.get("name", ""),
                "keyword": keyword,
                "search_query": build_search_query(platform, keyword)
            })
    print(json.dumps(tasks, ensure_ascii=False, indent=2))

def build_search_query(platform: str, keyword: str) -> str:
    """为不同平台构建搜索查询"""
    queries = {
        "weibo": f"{keyword} site:weibo.com 最新",
        "xiaohongshu": f"{keyword} 小红书 最新笔记",
        "wechat_mp": f"{keyword} 微信公众号 最新文章",
        "wechat_channels": f"{keyword} 微信视频号 最新",
        "x": f"{keyword} X Twitter 最新"
    }
    return queries.get(platform, f"{keyword} 最新")

def filter_new_items(items_file: str, state_file: str = None):
    """过滤出未发送过的新内容"""
    with open(items_file, "r", encoding="utf-8") as f:
        items = json.load(f)
    
    state = load_state()
    new_items = [item for item in items if is_new_content(item, state)]
    
    # 输出新内容
    print(json.dumps(new_items, ensure_ascii=False, indent=2))
    
    # 同时输出数量信息到 stderr
    print(f"[Monitor] 共 {len(items)} 条内容，其中 {len(new_items)} 条为新内容", file=sys.stderr)

def mark_items_sent(items_file: str):
    """将内容标记为已发送"""
    with open(items_file, "r", encoding="utf-8") as f:
        items = json.load(f)
    
    state = load_state()
    for item in items:
        mark_sent(item, state)
    save_state(state)
    print(f"[Monitor] 已标记 {len(items)} 条内容为已发送", file=sys.stderr)

def main():
    parser = argparse.ArgumentParser(description="大V内容监控协调脚本")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    subparsers = parser.add_subparsers(dest="command", help="子命令")
    
    # list-tasks: 列出搜索任务
    p_list = subparsers.add_parser("list-tasks", help="列出搜索任务")
    
    # filter-new: 过滤新内容
    p_filter = subparsers.add_parser("filter-new", help="过滤未发送的新内容")
    p_filter.add_argument("--items-file", required=True, help="内容项 JSON 文件")
    
    # mark-sent: 标记已发送
    p_mark = subparsers.add_parser("mark-sent", help="标记内容为已发送")
    p_mark.add_argument("--items-file", required=True, help="内容项 JSON 文件")
    
    args = parser.parse_args()
    
    if args.command == "list-tasks":
        list_tasks(args.config)
    elif args.command == "filter-new":
        filter_new_items(args.items_file)
    elif args.command == "mark-sent":
        mark_items_sent(args.items_file)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
