# -*- coding: utf-8 -*-
"""飞书通知：复用 quant-x-monitor 的应用凭证与发送函数

凭证只从 quant-x-monitor/config.json 读取（该文件已 gitignore），
永不写日志、永不入库。push 失败不阻断交易流程。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "quant-x-monitor"))

from feishu_pusher import get_feishu_token, send_feishu_interactive  # noqa: E402


def _feishu_cfg(config_path: Path = None) -> dict:
    p = config_path or (ROOT / "quant-x-monitor" / "config.json")
    cfg = json.loads(Path(p).read_text(encoding="utf-8"))
    return cfg["feishu"]


def push_card(title: str, lines: list, color: str = "blue",
              config_path: Path = None) -> bool:
    """lines: [(label, text), ...]；返回是否推送成功"""
    try:
        fc = _feishu_cfg(config_path)
        token = get_feishu_token(fc["app_id"], fc["app_secret"])
        if not token:
            print("[execution] 飞书 token 获取失败，跳过推送")
            return False
        elements = [
            {"tag": "div", "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**{k}**"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": str(v)}},
            ]}
            for k, v in lines
        ]
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title},
                       "template": color},
            "elements": elements,
        }
        return send_feishu_interactive(token, fc["receive_id"],
                                       fc.get("receive_id_type", "chat_id"), card)
    except Exception as e:
        print(f"[execution] 飞书推送异常（不阻断）: {type(e).__name__}: {e}")
        return False
