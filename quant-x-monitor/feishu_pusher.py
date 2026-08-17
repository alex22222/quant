import requests
import json
import time
from typing import Dict, Any, Optional

def get_feishu_token(app_id: str, app_secret: str) -> Optional[str]:
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    headers = {"Content-Type": "application/json"}
    payload = {"app_id": app_id, "app_secret": app_secret}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        data = resp.json()
        if data.get("code") == 0:
            return data.get("tenant_access_token")
        else:
            print(f"[Feishu] 获取token失败: {data}")
            return None
    except Exception as e:
        print(f"[Feishu] 请求异常: {e}")
        return None

def send_feishu_text(token: str, receive_id: str, receive_id_type: str, text: str) -> bool:
    url = "https://open.feishu.cn/open-apis/im/v1/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    params = {"receive_id_type": receive_id_type}
    payload = {
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False)
    }
    try:
        resp = requests.post(url, headers=headers, params=params, json=payload, timeout=10)
        data = resp.json()
        if data.get("code") == 0:
            print(f"[Feishu] 消息发送成功")
            return True
        else:
            print(f"[Feishu] 发送失败: {data}")
            return False
    except Exception as e:
        print(f"[Feishu] 发送异常: {e}")
        return False

def send_feishu_interactive(token: str, receive_id: str, receive_id_type: str, card: Dict[str, Any]) -> bool:
    url = "https://open.feishu.cn/open-apis/im/v1/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    params = {"receive_id_type": receive_id_type}
    payload = {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False)
    }
    try:
        resp = requests.post(url, headers=headers, params=params, json=payload, timeout=10)
        data = resp.json()
        if data.get("code") == 0:
            print(f"[Feishu] 卡片消息发送成功")
            return True
        else:
            print(f"[Feishu] 卡片发送失败: {data}")
            return False
    except Exception as e:
        print(f"[Feishu] 卡片发送异常: {e}")
        return False

def build_content_card(items: list, date_str: str) -> Dict[str, Any]:
    elements = []
    elements.append({
        "tag": "div",
        "text": {
            "tag": "plain_text",
            "content": f"📱 大V内容监控日报 | {date_str}",
            "style": {"bold": True, "font_size": 18}
        }
    })
    elements.append({"tag": "hr"})
    
    if not items:
        elements.append({
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": "今日暂无新内容",
                "style": {"font_size": 14}
            }
        })
    else:
        # 按平台分组
        from collections import defaultdict
        by_platform = defaultdict(list)
        for item in items:
            by_platform[item.get("platform", "其他")].append(item)
        
        platform_names = {
            "weibo": "🧣 微博",
            "xiaohongshu": "📕 小红书",
            "wechat_mp": "📰 微信公众号",
            "wechat_channels": "🎬 微信视频号",
            "x": "𝕏 X/Twitter"
        }
        
        for platform, platform_items in by_platform.items():
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": platform_names.get(platform, platform),
                    "style": {"bold": True, "font_size": 16}
                }
            })
            for item in platform_items:
                title = item.get("title", "无标题")
                author = item.get("author", "未知")
                url = item.get("url", "")
                summary = item.get("summary", "")
                time_str = item.get("publish_time", "")
                
                # 小红书特殊处理：直接展示内容，链接仅作参考
                if platform == "xiaohongshu":
                    # 构建小红书笔记卡片内容
                    xhs_content = f"**{author}**：{title}"
                    if time_str:
                        xhs_content += f"\n🕐 {time_str}"
                    if summary:
                        xhs_content += f"\n{summary}"
                    
                    # 互动数据
                    liked = item.get("liked_count", "")
                    collected = item.get("collected_count", "")
                    commented = item.get("comment_count", "")
                    if liked or collected or commented:
                        stats = []
                        if liked: stats.append(f"❤️ {liked}")
                        if collected: stats.append(f"⭐ {collected}")
                        if commented: stats.append(f"💬 {commented}")
                        xhs_content += f"\n{' | '.join(stats)}"
                    
                    # 封面图链接（飞书卡片不支持直接外链图片，提供链接）
                    cover_url = item.get("cover_url", "")
                    if cover_url:
                        xhs_content += f"\n[📷 封面图]({cover_url})"
                    
                    xhs_content += f"\n\n*（小红书网页需登录，请用App搜索查看）*"
                    if url:
                        xhs_content += f"\n[笔记链接]({url})"
                    
                    elements.append({
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": xhs_content
                        }
                    })
                else:
                    # 其他平台正常展示
                    content = f"**{author}**：{title}"
                    if time_str:
                        content += f"\n🕐 {time_str}"
                    if summary:
                        content += f"\n{summary}"
                    if url:
                        content += f"\n[查看原文]({url})"
                    
                    elements.append({
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": content
                        }
                    })
                elements.append({"tag": "hr"})
    
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": "大V内容监控"
            }
        },
        "elements": elements
    }
