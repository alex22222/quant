# -*- coding: utf-8 -*-
"""Durable workflow and step ledger for research and Paper operations."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


TEAM = Path(__file__).resolve().parent
DEFAULT_DB_PATH = TEAM / ".runs" / "ledger.db"
RUN_STATES = {"queued", "running", "succeeded", "failed", "cancelled"}
STEP_STATES = {"running", "succeeded", "failed", "skipped"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


class RunLedger:
    """SQLite-backed source of truth for workflow execution history."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH, *, readonly: bool = False):
        self.path = Path(path)
        self.readonly = readonly
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = (sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
                if self.readonly else sqlite3.connect(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if not self.readonly:
            conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    run_id TEXT PRIMARY KEY,
                    workflow TEXT NOT NULL,
                    business_date TEXT NOT NULL,
                    trigger_source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step TEXT,
                    model TEXT,
                    data_asof TEXT,
                    input_hash TEXT,
                    plan_hash TEXT,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_workflow_runs_lookup
                ON workflow_runs(workflow, business_date, started_at DESC);

                CREATE TABLE IF NOT EXISTS step_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    step_name TEXT NOT NULL,
                    role TEXT,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    input_hash TEXT,
                    prompt_hash TEXT,
                    output_hash TEXT,
                    model TEXT,
                    artifact_path TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(run_id, step_name, attempt),
                    FOREIGN KEY(run_id) REFERENCES workflow_runs(run_id)
                );

                CREATE INDEX IF NOT EXISTS idx_step_runs_run
                ON step_runs(run_id, id);

                CREATE TABLE IF NOT EXISTS workflow_attempts (
                    run_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    trigger_source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT,
                    external_context_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(run_id, attempt),
                    FOREIGN KEY(run_id) REFERENCES workflow_runs(run_id)
                );
                """
            )
            conn.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(step_runs)")}
            if "workflow_attempt" not in columns:
                conn.execute("ALTER TABLE step_runs ADD COLUMN workflow_attempt INTEGER")

    def start_run(
        self,
        workflow: str,
        business_date: str,
        *,
        trigger_source: str,
        model: str | None = None,
        input_hash: str | None = None,
        metadata: dict | None = None,
        external_context: dict | None = None,
        resume: bool = True,
    ) -> str:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = None
            if resume:
                previous = conn.execute(
                    """
                    SELECT run_id, attempt FROM workflow_runs
                    WHERE workflow=? AND business_date=? AND status IN ('running', 'failed')
                      AND (input_hash=? OR (input_hash IS NULL AND ? IS NULL))
                    ORDER BY started_at DESC LIMIT 1
                    """,
                    (workflow, business_date, input_hash, input_hash),
                ).fetchone()
            if previous:
                run_id = str(previous["run_id"])
                conn.execute(
                    """
                    UPDATE workflow_runs
                    SET status='running', attempt=?, updated_at=?, finished_at=NULL,
                        error=NULL, trigger_source=?, model=COALESCE(?, model),
                        input_hash=COALESCE(?, input_hash), metadata_json=?
                    WHERE run_id=?
                    """,
                    (
                        int(previous["attempt"]) + 1,
                        now,
                        trigger_source,
                        model,
                        input_hash,
                        _json(metadata),
                        run_id,
                    ),
                )
                attempt = int(previous["attempt"]) + 1
                conn.execute(
                    "UPDATE workflow_attempts SET status='superseded', finished_at=? "
                    "WHERE run_id=? AND status='running'", (now, run_id),
                )
            else:
                run_id = uuid.uuid4().hex
                attempt = 1
                conn.execute(
                    """
                    INSERT INTO workflow_runs (
                        run_id, workflow, business_date, trigger_source, status,
                        model, input_hash, started_at, updated_at, metadata_json
                    ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        workflow,
                        business_date,
                        trigger_source,
                        model,
                        input_hash,
                        now,
                        now,
                        _json(metadata),
                    ),
                )
            conn.execute(
                "INSERT INTO workflow_attempts "
                "(run_id,attempt,trigger_source,status,started_at,external_context_json) "
                "VALUES (?,?,?,'running',?,?)",
                (run_id, attempt, trigger_source, now, _json(external_context)),
            )
        return run_id

    def find_successful_run(
        self, workflow: str, business_date: str, input_hash: str | None
    ) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM workflow_runs
                WHERE workflow=? AND business_date=? AND status='succeeded'
                  AND (input_hash=? OR (input_hash IS NULL AND ? IS NULL))
                ORDER BY finished_at DESC LIMIT 1
                """,
                (workflow, business_date, input_hash, input_hash),
            ).fetchone()
        return self._decode(row) if row else None

    def latest_step(self, run_id: str, step_name: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM step_runs
                WHERE run_id=? AND step_name=?
                ORDER BY attempt DESC LIMIT 1
                """,
                (run_id, step_name),
            ).fetchone()
        return self._decode(row) if row else None

    def latest_run(self, workflow: str, business_date: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_runs WHERE workflow=? AND business_date=? "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (workflow, business_date),
            ).fetchone()
        return self._decode(row) if row else None

    def start_step(
        self,
        run_id: str,
        step_name: str,
        *,
        role: str | None = None,
        input_hash: str | None = None,
        prompt_hash: str | None = None,
        model: str | None = None,
        metadata: dict | None = None,
    ) -> int:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 FROM step_runs WHERE run_id=? AND step_name=?",
                (run_id, step_name),
            ).fetchone()
            attempt = int(row[0])
            cursor = conn.execute(
                """
                INSERT INTO step_runs (
                    run_id, step_name, role, status, attempt, input_hash,
                    prompt_hash, model, started_at, metadata_json
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    step_name,
                    role,
                    attempt,
                    input_hash,
                    prompt_hash,
                    model,
                    now,
                    _json(metadata),
                ),
            )
            conn.execute(
                "UPDATE step_runs SET workflow_attempt=(SELECT attempt FROM workflow_runs WHERE run_id=?) "
                "WHERE id=?", (run_id, cursor.lastrowid),
            )
            conn.execute(
                "UPDATE workflow_runs SET current_step=?, status='running', updated_at=? WHERE run_id=?",
                (step_name, now, run_id),
            )
            return int(cursor.lastrowid)

    def finish_step(
        self,
        step_id: int,
        *,
        status: str = "succeeded",
        output_hash: str | None = None,
        artifact_path: str | None = None,
        error: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        if status not in STEP_STATES:
            raise ValueError(f"invalid step status: {status}")
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE step_runs
                SET status=?, output_hash=?, artifact_path=?, finished_at=?,
                    error=?, metadata_json=?
                WHERE id=?
                """,
                (status, output_hash, artifact_path, now, error, _json(metadata), step_id),
            )
            row = conn.execute("SELECT run_id FROM step_runs WHERE id=?", (step_id,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE workflow_runs SET updated_at=? WHERE run_id=?",
                    (now, row["run_id"]),
                )

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        data_asof: str | None = None,
        plan_hash: str | None = None,
        error: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        if status not in RUN_STATES:
            raise ValueError(f"invalid run status: {status}")
        now = _now()
        finished_at = now if status in {"succeeded", "failed", "cancelled"} else None
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE workflow_runs
                SET status=?, data_asof=COALESCE(?, data_asof),
                    plan_hash=COALESCE(?, plan_hash), error=?, metadata_json=?,
                    updated_at=?, finished_at=?
                WHERE run_id=?
                """,
                (status, data_asof, plan_hash, error, _json(metadata), now, finished_at, run_id),
            )
            conn.execute(
                "UPDATE workflow_attempts SET status=?,finished_at=?,error=? "
                "WHERE run_id=? AND attempt=(SELECT attempt FROM workflow_runs WHERE run_id=?)",
                (status, finished_at, error, run_id, run_id),
            )

    def list_runs(self, limit: int = 30) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *, (
                    SELECT COUNT(*) FROM step_runs s WHERE s.run_id=workflow_runs.run_id
                ) AS step_count
                FROM workflow_runs
                ORDER BY started_at DESC, rowid DESC LIMIT ?
                """,
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        result = []
        for row in rows:
            item = self._decode(row)
            item.pop("metadata", None)
            result.append(item)
        return result

    def get_run(self, run_id: str) -> dict | None:
        with self._connect() as conn:
            run = conn.execute(
                "SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                return None
            steps = conn.execute(
                "SELECT * FROM step_runs WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
            attempts = conn.execute(
                "SELECT * FROM workflow_attempts WHERE run_id=? ORDER BY attempt", (run_id,),
            ).fetchall()
        result = self._decode(run)
        result["steps"] = [self._decode(row) for row in steps]
        result["attempts"] = []
        for row in attempts:
            item = dict(row)
            item["external_context"] = json.loads(item.pop("external_context_json"))
            result["attempts"].append(item)
        return result

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        result = dict(row)
        raw = result.pop("metadata_json", "{}")
        try:
            result["metadata"] = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            result["metadata"] = {}
        return result
