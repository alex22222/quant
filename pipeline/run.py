# -*- coding: utf-8 -*-
"""编排器：一条命令跑完整条流水线

用法：
  python -m pipeline.run                      # 全部阶段
  python -m pipeline.run --stages data,paper  # 子集
  python -m pipeline.run --update-data        # data 阶段触发月度包更新
"""
import argparse
import importlib

from .common import STAGES, run_stage, load_status, save_status, now


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", default=",".join(STAGES))
    ap.add_argument("--update-data", action="store_true")
    args = ap.parse_args()

    todo = [s.strip() for s in args.stages.split(",") if s.strip() in STAGES]
    print(f"pipeline 启动，阶段: {todo}")
    for name in todo:
        mod = importlib.import_module(f"pipeline.stage_{name}")
        kwargs = {"update": True} if (name == "data" and args.update_data) else {}
        ok, entry = run_stage(name, mod.main, **kwargs)
        icon = "✅" if ok else "❌"
        print(f"{icon} {name}: {entry['status']} ({entry.get('elapsed_sec')}s)")
        if not ok:
            print(f"   错误: {entry.get('error')}")
            if name in ("data",):  # 数据失败则后续无意义
                print("关键阶段失败，终止")
                break
    st = load_status()
    st["last_loop_at"] = now()
    save_status(st)
    print("pipeline 结束，状态已写入 status.json")


if __name__ == "__main__":
    main()
