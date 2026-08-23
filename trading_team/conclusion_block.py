# -*- coding: utf-8 -*-
"""角色日报结构化结论块：提取、校验、accuracy.json 自动回填（quant-wiki 建议 1）

各角色日报末尾的 ```json 结论块是机器可读的唯一事实源（prompts 01-07 已写入契约）。
本模块：
- extract_block(text, role)：提取最后一个 ```json 围栏块，解析并按角色校验；失败返回 None（fail closed）
- backfill_accuracy(date)：读取 outputs/<date>/05_researcher.md 的结论块，
  upsert loops/accuracy.json 当日 entry 的 ratings（ref_price 取自 context/<date>/technical_<code>.json）。
  幂等：已 verified 的 entry 不覆盖。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

TEAM_DIR = Path(__file__).resolve().parent

RATING_ENUM = {"强烈看多", "看多", "中性偏多", "中性", "中性偏空", "看空", "强烈看空"}
ROLES_WITH_RATINGS = {"fundamental", "sentiment", "news", "technical", "researcher"}

_JSON_FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


class BlockError(ValueError):
    pass


def _validate_rating(r: dict) -> dict:
    if not isinstance(r.get("code"), str) or not r["code"]:
        raise BlockError(f"rating 缺 code: {r}")
    if r.get("rating") not in RATING_ENUM:
        raise BlockError(f"非法 rating {r.get('rating')!r}（{r.get('code')}）")
    conf = r.get("confidence")
    if not isinstance(conf, int) or not 1 <= conf <= 5:
        raise BlockError(f"非法 confidence {conf!r}（{r.get('code')}）")
    return r


def extract_block(text: str, role: str) -> dict | None:
    """提取并校验结论块。任何不合法返回 None（调用方按"报告未完成"处理）。"""
    blocks = _JSON_FENCE.findall(text or "")
    if not blocks:
        return None
    try:
        data = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("role") != role:
        return None
    if not data.get("date"):
        return None
    try:
        if role in ROLES_WITH_RATINGS:
            ratings = data.get("ratings")
            if not isinstance(ratings, list):
                return None
            data["ratings"] = [_validate_rating(r) for r in ratings]
        if role == "news":
            items = data.get("items")
            if not isinstance(items, list):
                return None
            for it in items:
                if it.get("sentiment") not in ("Positive", "Negative", "Neutral"):
                    raise BlockError(f"非法 sentiment {it.get('sentiment')!r}")
                if not isinstance(it.get("score"), (int, float)) or not -1 <= it["score"] <= 1:
                    raise BlockError(f"非法 score {it.get('score')!r}")
            t = data.get("temperature")
            if not isinstance(t, (int, float)) or not -1 <= t <= 1:
                raise BlockError(f"非法 temperature {t!r}")
        if role == "risk_manager":
            reviews = data.get("reviews")
            if not isinstance(reviews, list):
                return None
            for rv in reviews:
                if rv.get("decision") not in ("通过", "降仓通过", "否决"):
                    raise BlockError(f"非法 decision {rv.get('decision')!r}")
    except (BlockError, TypeError, KeyError):
        return None
    return data


def _ref_price(date: str, code: str, team_dir: Path = TEAM_DIR) -> float | None:
    p = team_dir / "context" / date / f"technical_{code}.json"
    if not p.exists():
        return None
    try:
        v = json.loads(p.read_text(encoding="utf-8")).get("close")
        return float(v) if v is not None else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def backfill_accuracy(date: str, team_dir: Path = TEAM_DIR) -> dict:
    """从研究员结论块回填 accuracy.json 当日 ratings。幂等：已 verified 不覆盖。

    返回 {"date":..., "updated": bool, "ratings": n, "reason": ...}"""
    acc_path = team_dir / "loops" / "accuracy.json"
    report = team_dir / "outputs" / date / "05_researcher.md"
    if not report.exists():
        return {"date": date, "updated": False, "ratings": 0,
                "reason": f"缺研究员报告 {report.name}"}
    block = extract_block(report.read_text(encoding="utf-8"), "researcher")
    if block is None:
        return {"date": date, "updated": False, "ratings": 0,
                "reason": "结论块缺失或不合法（报告未完成）"}

    acc = json.loads(acc_path.read_text(encoding="utf-8"))
    entries = acc.setdefault("entries", [])
    entry = next((e for e in entries if e.get("date") == date), None)
    if entry and entry.get("verified"):
        return {"date": date, "updated": False, "ratings": 0,
                "reason": "当日已 verified，不覆盖"}

    ratings = {r["code"]: {"rating": r["rating"], "confidence": r["confidence"],
                           "ref_price": _ref_price(date, r["code"], team_dir)}
               for r in block["ratings"]}
    if entry is None:
        entry = {"date": date, "verified": False, "ratings": {},
                 "result_5d": None, "score": None}
        entries.append(entry)
    entry["ratings"] = ratings
    entry["block_source"] = "05_researcher.md 结构化结论块"
    if block.get("market"):
        entry["market_view"] = block["market"]
    acc_path.write_text(json.dumps(acc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"date": date, "updated": True, "ratings": len(ratings), "reason": "ok"}


def main():
    import argparse
    ap = argparse.ArgumentParser(prog="trading_team.conclusion_block")
    ap.add_argument("date", help="回填日期 YYYY-MM-DD")
    args = ap.parse_args()
    print(json.dumps(backfill_accuracy(args.date), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
