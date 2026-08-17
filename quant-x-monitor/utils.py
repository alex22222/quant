import json
import os
import hashlib
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")

def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {"sent_hashes": [], "last_run": None}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state: Dict[str, Any]):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def content_hash(title: str, url: str, author: str) -> str:
    return hashlib.md5(f"{author}:{title}:{url}".encode()).hexdigest()

def is_new_content(item: Dict[str, Any], state: Dict[str, Any]) -> bool:
    if not state.get("sent_hashes"):
        return True
    h = content_hash(item.get("title", ""), item.get("url", ""), item.get("author", ""))
    return h not in state["sent_hashes"]

def mark_sent(item: Dict[str, Any], state: Dict[str, Any]):
    h = content_hash(item.get("title", ""), item.get("url", ""), item.get("author", ""))
    if h not in state.get("sent_hashes", []):
        state.setdefault("sent_hashes", []).append(h)
    # 保留最近1000条记录
    state["sent_hashes"] = state["sent_hashes"][-1000:]
    state["last_run"] = datetime.now().isoformat()
