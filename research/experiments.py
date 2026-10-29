"""Real attempts, including failures, registered BEFORE execution. No synthetic Sharpe history."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

DEFAULT_LEDGER = Path(__file__).resolve().parents[1] / "research/experiments.jsonl"


class ExperimentLedger:
    def __init__(self, path: Path = DEFAULT_LEDGER):
        self.path = Path(path)

    def append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def begin(self, strategy: str, specification: dict) -> dict:
        payload = json.dumps({"strategy": strategy, **specification}, sort_keys=True, allow_nan=False)
        trial = {"attempt_id": uuid4().hex, "trial_id": hashlib.sha256(payload.encode()).hexdigest(),
                 "strategy": strategy, "specification": specification,
                 "event": "started", "ts": datetime.now(timezone.utc).isoformat()}
        self.append(trial)
        return trial

    def finish(self, trial: dict, metrics: dict | None = None, error: str | None = None):
        # A missing/non-finite score is recorded as missing, never as zero.
        clean = {key: value for key, value in (metrics or {}).items()
                 if isinstance(value, (int, float)) and math.isfinite(value)}
        self.append({**trial, "event": "failed" if error else "completed",
                     "metrics": clean, "error": error,
                     "ts": datetime.now(timezone.utc).isoformat()})

    def evidence(self, strategy: str, window: list[str] | None = None) -> dict:
        rows = [json.loads(line) for line in self.path.read_text().splitlines()] if self.path.exists() else []
        attempts, scores = set(), {}
        for row in rows:
            if row["strategy"] != strategy:
                continue
            if window and row["specification"].get("window") != window:
                continue
            attempts.add(row["trial_id"])
            score = row.get("metrics", {}).get("sr_monthly")
            if row["event"] == "completed" and score is not None:
                scores[row["trial_id"]] = score
        return {"n_trials": len(attempts), "monthly_sharpes": list(scores.values()),
                "missing_results": len(attempts - scores.keys())}
