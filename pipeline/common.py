# -*- coding: utf-8 -*-
"""公共层：status.json 读写、阶段执行器"""
import json
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUS_FILE = ROOT / "status.json"
TZ = timezone(timedelta(hours=8))

STAGES = ["data", "strategy", "backtest", "paper", "review", "audit"]


def now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def load_status():
    if STATUS_FILE.exists():
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    return {"project": "quant-closed-loop", "version": 1, "stages": {}}


def save_status(st):
    STATUS_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def run_stage(name, fn, *args, **kwargs):
    """执行阶段函数，结果写入 status.json。返回 (ok, result)"""
    st = load_status()
    entry = {"status": "running", "started_at": now()}
    st["stages"][name] = entry
    save_status(st)
    t0 = time.time()
    try:
        result = fn(*args, **kwargs) or {}
        entry.update({"status": "ok", "finished_at": now(),
                      "elapsed_sec": round(time.time() - t0, 1),
                      "result": result})
        ok = True
    except Exception as e:
        entry.update({"status": "failed", "finished_at": now(),
                      "elapsed_sec": round(time.time() - t0, 1),
                      "error": f"{type(e).__name__}: {e}",
                      "traceback": traceback.format_exc()[-2000:]})
        ok = False
    st = load_status()
    st["stages"][name] = entry
    st["updated_at"] = now()
    save_status(st)
    return ok, entry
