# -*- coding: utf-8 -*-
"""Thin, fail-closed integration boundary for the Multica CLI."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from uuid import UUID

from trading_team.run_ledger import RunLedger


ROOT = Path(__file__).resolve().parent.parent
MULTICA_DIR = ROOT / "multica"
DEFAULT_OUT = ROOT / "trading_team" / ".runs" / "multica"
DEFAULT_BOOTSTRAP_STATE = DEFAULT_OUT / "bootstrap.json"
CHINESE_TASK_TITLES_FROM = "2026-09-24"


def daily_issue_title(day: str) -> str:
    return f"{'A股每日研究' if day >= CHINESE_TASK_TITLES_FROM else 'A-share daily research'} {day}"


def discover_multica_binary(*, home: Path | None = None) -> str | None:
    binary = shutil.which("multica")
    if binary:
        return binary
    candidate = (home or Path.home()) / ".local" / "bin" / "multica"
    return str(candidate) if candidate.is_file() else None


@dataclass(frozen=True)
class MulticaStatus:
    installed: bool
    connected: bool
    daemon_running: bool
    detail: str
    raw: dict | None = None
    server_configured: bool = False
    authenticated: bool = False

    def as_dict(self) -> dict:
        return {
            "installed": self.installed,
            "connected": self.connected,
            "daemon_running": self.daemon_running,
            "server_configured": self.server_configured,
            "authenticated": self.authenticated,
            "detail": self.detail,
            "raw": self.raw,
        }


class MulticaCLI:
    def __init__(self, binary: str | None = None, runner=None, cwd: Path = ROOT):
        self.binary = discover_multica_binary() if binary is None else binary
        self.runner = runner or subprocess.run
        self.cwd = cwd

    def _run_json(self, args: list[str], *, timeout: int = 60) -> dict | list:
        if not self.binary:
            raise RuntimeError("Multica unavailable: multica CLI not installed")
        result = self.runner(
            [str(self.binary), *args],
            cwd=self.cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "command failed").strip()
            raise RuntimeError(f"Multica command failed: {detail[:500]}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Multica command returned non-JSON output") from exc

    def status(self, *, timeout: int = 20) -> MulticaStatus:
        if not self.binary:
            return MulticaStatus(False, False, False, "multica CLI not installed")
        try:
            auth_result = self.runner(
                [self.binary, "auth", "status"],
                cwd=self.cwd,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return MulticaStatus(True, False, False, f"auth status failed: {exc}"[:500])

        auth_text = (auth_result.stderr or auth_result.stdout or "").strip()
        auth_lower = auth_text.lower()
        server_configured = "no server configured" not in auth_lower
        authenticated = (
            auth_result.returncode == 0
            and "server:" in auth_lower
            and "user:" in auth_lower
        )

        try:
            result = self.runner(
                [self.binary, "daemon", "status", "--output", "json"],
                cwd=self.cwd,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            detail = auth_text.splitlines()[0] if auth_text else "auth status unknown"
            return MulticaStatus(
                True,
                authenticated,
                False,
                f"{detail}; daemon status failed: {exc}"[:500],
                server_configured=server_configured,
                authenticated=authenticated,
            )
        if result.returncode != 0:
            daemon_detail = (result.stderr or result.stdout or "daemon status failed").strip()
            auth_detail = auth_text.splitlines()[0] if auth_text else "auth status unknown"
            return MulticaStatus(
                True,
                authenticated,
                False,
                f"{auth_detail}; {daemon_detail}"[:500],
                server_configured=server_configured,
                authenticated=authenticated,
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return MulticaStatus(
                True,
                authenticated,
                False,
                "daemon returned non-JSON status",
                server_configured=server_configured,
                authenticated=authenticated,
            )
        state = str(payload.get("status") or payload.get("state") or "").lower()
        running = state == "running" or payload.get("running") is True
        auth_detail = auth_text.splitlines()[0] if auth_text else "auth status unknown"
        return MulticaStatus(
            True,
            authenticated,
            running,
            f"{auth_detail}; daemon {state or 'unknown'}"[:500],
            payload,
            server_configured=server_configured,
            authenticated=authenticated,
        )

    def create_issue(
        self,
        *,
        title: str,
        description_file: Path,
        assignee: str,
        project: str | None = None,
    ) -> dict:
        from pipeline.policy import require
        require("multica_dispatch")
        status = self.status(timeout=60)
        if not status.installed or not status.connected or not status.daemon_running:
            raise RuntimeError(f"Multica unavailable: {status.detail}")
        command = [
            str(self.binary),
            "issue",
            "create",
            "--title",
            title,
            "--description-file",
            str(description_file),
            "--status",
            "todo",
            "--assignee-id",
            assignee,
            "--output",
            "json",
        ]
        if project:
            command.extend(["--project", project])
        return self._run_json(command[1:])


def _items(payload: dict | list, key: str) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    value = payload.get(key, []) if isinstance(payload, dict) else []
    if isinstance(value, dict):
        value = value.get("items", [])
    return [item for item in value if isinstance(item, dict)]


def _entity(payload: dict | list, key: str) -> dict:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Multica {key} create returned invalid payload")
    value = payload.get(key, payload)
    if not isinstance(value, dict) or not value.get("id"):
        raise RuntimeError(f"Multica {key} payload has no id")
    return value


def _find(items: list[dict], field: str, value: str) -> dict | None:
    return next((item for item in items if item.get(field) == value), None)


def _write_bootstrap_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def load_bootstrap_state(path: Path = DEFAULT_BOOTSTRAP_STATE) -> dict:
    if not path.exists():
        return {"configured": False}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"configured": False, "error": str(exc)}
    return {"configured": True, **state}


def verify_execution_context(
    day: str, issue_id: str, *, cli: MulticaCLI | None = None,
    environ: dict | None = None, state_path: Path = DEFAULT_BOOTSTRAP_STATE,
    title_prefix: str | None = None,
) -> dict:
    """Bind the daemon task identity to a running, project-owned remote execution."""
    date.fromisoformat(day)
    env = os.environ if environ is None else environ
    identity = {}
    for key in ("task", "agent", "workspace"):
        value = env.get(f"MULTICA_{key.upper()}_ID", "")
        try:
            identity[f"{key}_id"] = str(UUID(value))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"Multica {key} identity missing or invalid") from exc
    if not issue_id:
        raise ValueError("Multica issue identity is required")
    state = load_bootstrap_state(state_path)
    if not state.get("configured") or not state.get("project_id"):
        raise ValueError("Multica project is not bootstrapped")
    role = next((name for name, agent_id in (state.get("agent_ids") or {}).items()
                 if agent_id == identity["agent_id"]), None)
    if role is None:
        raise ValueError("Multica agent is outside the configured research team")
    cli = cli or MulticaCLI()
    issue = _entity(cli._run_json(["issue", "get", issue_id, "--output", "json"]), "issue")
    if (issue.get("project_id") != state["project_id"]
            or issue.get("workspace_id") != identity["workspace_id"]):
        raise ValueError("Multica issue project/workspace mismatch")
    allowed_titles = ({daily_issue_title(day), f"A-share daily research {day}"}
                      if title_prefix is None else {f"{title_prefix} {day}"})
    if issue.get("title") not in allowed_titles:
        raise ValueError("Multica issue business date/title mismatch")
    runs = _items(cli._run_json(["issue", "runs", issue["id"], "--output", "json"]), "runs")
    task = next((run for run in runs if run.get("id") == identity["task_id"]), None)
    if not task or task.get("agent_id") != identity["agent_id"] or task.get("status") != "running":
        raise ValueError("Multica task is not a matching running execution")
    context = {
        "provider": "multica", "verification": "remote_verified", **identity,
        "issue_id": issue["id"], "issue_identifier": issue.get("identifier"),
        "issue_title": issue["title"], "project_id": issue["project_id"],
        "squad_id": state.get("squad_id"), "role": role,
        "observed_status": task["status"],
        "observed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    context["evidence_hash"] = hashlib.sha256(
        json.dumps(context, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return context


def _audit_code_hashes() -> dict:
    paths = sorted(path for folder in ("trading_team", "paper", "pipeline", "strategies", "tests", "execution")
                   for path in (ROOT / folder).rglob("*.py") if ".runs" not in path.parts)
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def run_integration_audit(
    day: str, issue_id: str, *, cli: MulticaCLI | None = None, environ: dict | None = None,
    state_path: Path = DEFAULT_BOOTSTRAP_STATE, ledger: RunLedger | None = None,
    output_dir: Path = DEFAULT_OUT / "audit", runner=None,
) -> dict:
    """Exercise a real Multica execution without generating research or touching accounts."""
    context = verify_execution_context(
        day, issue_id, cli=cli, environ=environ, state_path=state_path,
        title_prefix="A-share integration audit",
    )
    if context["role"] != "Audit Verifier":
        raise ValueError("Multica integration audit requires the Audit Verifier agent")
    ledger = ledger or RunLedger()
    code_hashes = _audit_code_hashes()
    run_id = ledger.start_run(
        "multica_audit", day, trigger_source=f"multica:{context['issue_id']}:{context['task_id']}",
        external_context=context, resume=False,
    )
    step_id = ledger.start_step(run_id, "project_tests", role="Audit Verifier")
    try:
        command = [sys.executable, "-m", "pytest", "tests", "execution/tests", "-q"]
        execution = (runner or subprocess.run)(
            command, cwd=ROOT, text=True, capture_output=True, timeout=300, check=False,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        log = output_dir / f"{run_id}.log"
        log.write_text(execution.stdout + execution.stderr, encoding="utf-8")
        unchanged = code_hashes == _audit_code_hashes()
        status = "succeeded" if execution.returncode == 0 and unchanged else "failed"
        error = None if status == "succeeded" else "project tests failed or code changed during audit"
        log_hash = hashlib.sha256(log.read_bytes()).hexdigest()
        ledger.finish_step(
            step_id, status=status, artifact_path=str(log), output_hash=log_hash, error=error,
            metadata={"command": command, "returncode": execution.returncode,
                      "code_hashes": code_hashes, "code_unchanged": unchanged},
        )
        result = {"status": status, "run_id": run_id, "business_date": day,
                  "external_context": context, "test_log": str(log), "test_log_hash": log_hash}
        ledger.finish_run(run_id, status=status, error=error, metadata=result)
        return result
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        ledger.finish_step(step_id, status="failed", error=error)
        ledger.finish_run(run_id, status="failed", error=error)
        raise


def bootstrap_workspace(
    cli: MulticaCLI,
    *,
    runtime_id: str,
    daemon_id: str,
    state_path: Path = DEFAULT_BOOTSTRAP_STATE,
) -> dict:
    """Create or reuse the Multica resources declared by workspace.json."""
    from pipeline.policy import require
    require("multica_dispatch")
    status = cli.status(timeout=60)
    if not status.installed or not status.connected or not status.daemon_running:
        raise RuntimeError(f"Multica unavailable: {status.detail}")
    definition = json.loads((MULTICA_DIR / "workspace.json").read_text(encoding="utf-8"))
    runbook = (ROOT / definition["workflow"]["runbook"]).read_text(encoding="utf-8")
    autopilot_runbook = (ROOT / "docs/MULTICA_PRODUCTION_RUNBOOK.md").read_text(encoding="utf-8")
    project_def = definition["project"]
    actions = []

    projects = _items(cli._run_json(["project", "list", "--output", "json"]), "projects")
    project = _find(projects, "title", project_def["name"])
    if project is None:
        project = _entity(cli._run_json([
            "project", "create",
            "--title", project_def["name"],
            "--description", "Auditable A-share research and Paper trading workflow.",
            "--status", "in_progress",
            "--output", "json",
        ]), "project")
        actions.append("project_created")
    project_id = str(project["id"])

    resources = _items(cli._run_json([
        "project", "resource", "list", project_id, "--output", "json",
    ]), "resources")
    local_path = str(Path(project_def["local_path"]).resolve())
    resource = next((item for item in resources if (
        (item.get("resource_type") or item.get("type")) == "local_directory"
        and str((item.get("resource_ref") or {}).get("local_path") or item.get("local_path")) == local_path
        and str((item.get("resource_ref") or {}).get("daemon_id") or item.get("daemon_id")) == daemon_id
    )), None)
    if resource is None:
        resource = _entity(cli._run_json([
            "project", "resource", "add", project_id,
            "--type", "local_directory",
            "--local-path", local_path,
            "--daemon-id", daemon_id,
            "--execution-mode", project_def["execution_mode"],
            "--output", "json",
        ]), "resource")
        actions.append("resource_created")

    existing_agents = _items(
        cli._run_json(["agent", "list", "--output", "json"]), "agents"
    )
    agents = {}
    for agent_def in definition["agents"]:
        agent = _find(existing_agents, "name", agent_def["name"])
        if agent is None:
            instructions = (
                "Work only inside the bound project. Treat Python artifacts, ledger, approvals, "
                "and Paper databases as authoritative. Never submit Live orders.\n\n"
                + agent_def["role"]
            )
            agent = _entity(cli._run_json([
                "agent", "create",
                "--name", agent_def["name"],
                "--runtime-id", runtime_id,
                "--instructions", instructions,
                "--max-concurrent-tasks", "1",
                "--output", "json",
            ]), "agent")
            existing_agents.append(agent)
            actions.append(f"agent_created:{agent_def['name']}")
        agents[agent_def["name"]] = agent

    squad_def = definition["squad"]
    leader_agent_id = str(agents[squad_def["leader"]]["id"])
    squads = _items(cli._run_json(["squad", "list", "--output", "json"]), "squads")
    squad = _find(squads, "name", squad_def["name"])
    if squad is None:
        squad = _entity(cli._run_json([
            "squad", "create",
            "--name", squad_def["name"],
            "--leader", leader_agent_id,
            "--output", "json",
        ]), "squad")
        actions.append("squad_created")
    squad_id = str(squad["id"])
    squad_detail = _entity(
        cli._run_json(["squad", "get", squad_id, "--output", "json"]), "squad"
    )
    if squad_detail.get("instructions") != runbook:
        cli._run_json([
            "squad", "update", squad_id, "--instructions", runbook, "--output", "json",
        ])
        actions.append("squad_instructions_updated")

    members = _items(cli._run_json([
        "squad", "member", "list", squad_id, "--output", "json",
    ]), "members")
    member_ids = {str(item.get("member_id")) for item in members}
    for agent_def in definition["agents"]:
        agent = agents[agent_def["name"]]
        agent_id = str(agent["id"])
        if agent_id in member_ids:
            continue
        cli._run_json([
            "squad", "member", "add", squad_id,
            "--member-id", agent_id,
            "--type", "agent",
            "--role", agent_def["role"],
            "--output", "json",
        ])
        member_ids.add(agent_id)
        actions.append(f"squad_member_added:{agent_def['name']}")

    autopilot_def = definition["autopilot"]
    autopilots = _items(
        cli._run_json(["autopilot", "list", "--output", "json"]), "autopilots"
    )
    autopilot = _find(autopilots, "title", autopilot_def["title"])
    if autopilot is None:
        autopilot = _entity(cli._run_json([
            "autopilot", "create",
            "--title", autopilot_def["title"],
            "--description", autopilot_runbook,
            "--issue-title-template", autopilot_def["issue_title_template"],
            "--agent", leader_agent_id,
            "--mode", autopilot_def["mode"],
            "--project", project_id,
            "--output", "json",
        ]), "autopilot")
        actions.append("autopilot_created")
    autopilot_id = str(autopilot["id"])
    autopilot_detail = cli._run_json([
        "autopilot", "get", autopilot_id, "--output", "json",
    ])
    autopilot_state = _entity(autopilot_detail, "autopilot")
    if (
        autopilot_state.get("description") != autopilot_runbook
        or autopilot_state.get("issue_title_template")
        != autopilot_def["issue_title_template"]
    ):
        cli._run_json([
            "autopilot", "update", autopilot_id,
            "--description", autopilot_runbook,
            "--issue-title-template", autopilot_def["issue_title_template"],
            "--output", "json",
        ])
        actions.append("autopilot_updated")
    triggers = _items(autopilot_detail, "triggers")
    cron = autopilot_def["cron"]
    timezone = autopilot_def["timezone"]
    trigger = next((item for item in triggers if (
        str(item.get("kind", "schedule")) == "schedule"
        and str(
            item.get("cron_expression")
            or item.get("cron")
            or (item.get("schedule") or {}).get("cron")
        ) == cron
        and str(item.get("timezone") or (item.get("schedule") or {}).get("timezone")) == timezone
    )), None)
    if trigger is None:
        trigger = _entity(cli._run_json([
            "autopilot", "trigger-add", autopilot_id,
            "--cron", cron,
            "--timezone", timezone,
            "--output", "json",
        ]), "trigger")
        actions.append("trigger_created")

    state = {
        "project_id": project_id,
        "resource_id": str(resource["id"]),
        "agent_ids": {name: str(agent["id"]) for name, agent in agents.items()},
        "leader_agent_id": leader_agent_id,
        "squad_id": squad_id,
        "autopilot_id": autopilot_id,
        "trigger_id": str(trigger["id"]),
        "runtime_id": runtime_id,
        "daemon_id": daemon_id,
        "actions": actions,
    }
    _write_bootstrap_state(state_path, state)
    return state


def prepare_daily_issue(day: str, out_dir: Path = DEFAULT_OUT) -> dict:
    date.fromisoformat(day)
    definition = json.loads((MULTICA_DIR / "workspace.json").read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    description_path = out_dir / f"{day}-daily-research.md"
    content = f"""# A-share daily research run: {day}

Follow `multica/runbooks/daily-research.md` exactly.

## Inputs

- Repository: `{ROOT}`
- Business date: `{day}`
- Workflow: `daily_research`
- Local command: `.venv/bin/python -m trading_team.kimi_daily --date {day} --publish-only --no-push --trigger-source multica --multica-issue-id <current-issue-id>`
- Resolve the current issue ID from your task context; do not invent an ID or pass a literal placeholder.

## Required evidence

- The data manifest passes its deterministic gate.
- Every role step is present in `.runs/ledger.db` with input and output hashes.
- The published `plan.json` and generation receipt bind the same `plan_hash`.
- Publication success is a review handoff, not completed decisions, Paper settlement or delivery.
- After independent reviews, authorized deterministic finalization must bind both decisions to that plan.
- No Live order is submitted. Paper uses the repository T+1 execution model only.
- The verifier reports test commands and outcomes before the issue moves to review.

## Routing

Assign the parent issue to `{definition['squad']['name']}`. The squad leader routes
bounded review tasks to the team. Only Research Lead invokes the complete publishing command
above; reviewers must not rerun that command, write replacement reports, approve plans, or settle accounts.
The seven LLM role steps inside the Python runner are not seven independently executed Multica tasks.
Preserve actual task identities and review evidence; do not infer completed delegation from local role files.
Independent remote maker/checker execution is still pending acceptance in `docs/SYSTEM_ACCEPTANCE.md`.
Do not fan out all agents automatically or mark the full integration complete from one local test run.
"""
    if day >= CHINESE_TASK_TITLES_FROM:
        content = f"""# A股每日研究：{day}

按 `docs/MULTICA_PRODUCTION_RUNBOOK.md` 执行，仅发布研究包，不完成审批、Paper 结算或通知。

## 输入与身份

- 项目目录：`{ROOT}`；业务日期：`{day}`；工作流：`daily_research`。
- 从当前真实任务取得 Issue ID，不能猜测 ID，也不能把占位符传给命令。
- 仅 Research Lead 可执行一次：
  `.venv/bin/python -m trading_team.kimi_daily --date {day} --publish-only --no-push --trigger-source multica --multica-issue-id <current-issue-id>`
- 先核对 A 股交易日历、原始归档、当前版本和是否已有发布回执；不得覆盖、补签或重复调用模型。

## 必须保留的证据

- 数据清单通过确定性校验；七个本地角色步骤在 `.runs/ledger.db` 留有输入、输出哈希。
- `plan.json` 与生成回执绑定同一个 `plan_hash`；仅发布成功不等于业务闭环。
- 后续六个独立 Multica 审阅任务须逐级绑定当前版本；通过后才能做确定性决策与 Paper 收尾。
- Paper 仅使用项目现有 T+1 撮合模型，不得提交实盘订单或替用户批准 Live。
- 验证者须报告实际命令、Run、回执和失败；不得把本地七次 LLM 调用冒充七个远端任务。

## 路由

父任务分配给 `{definition['squad']['name']}`，由组长按阶段分派审阅。审阅者不得重跑发布命令、
替换研究报告、批准计划、直接改 Paper 账户或发通知。保留真实 task 身份及历史失败；
完整系统剩余验收范围见 `docs/SYSTEM_ACCEPTANCE.md`。
"""
    description_path.write_text(content, encoding="utf-8")
    payload = {
        "date": day,
        "title": daily_issue_title(day),
        "description_file": str(description_path),
        "assignee": definition["squad"]["name"],
        "project": definition["project"]["name"],
    }
    payload_path = out_dir / f"{day}-daily-research.json"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def dispatch_daily_issue(
    day: str,
    cli: MulticaCLI,
    *,
    out_dir: Path = DEFAULT_OUT,
    state_path: Path = DEFAULT_BOOTSTRAP_STATE,
    assignee: str | None = None,
    project: str | None = None,
) -> dict:
    from pipeline.policy import require
    require("multica_dispatch")
    state = load_bootstrap_state(state_path)
    if not state.get("configured"):
        raise RuntimeError("Multica resources are not bootstrapped")
    payload = prepare_daily_issue(day, out_dir)
    issue = cli.create_issue(
        title=payload["title"],
        description_file=Path(payload["description_file"]),
        assignee=assignee or str(state["squad_id"]),
        project=project or str(state["project_id"]),
    )
    return {"prepared": payload, "issue": issue}


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    audit = sub.add_parser("audit")
    audit.add_argument("--date", required=True)
    audit.add_argument("--issue-id", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--date", default=date.today().isoformat())
    dispatch = sub.add_parser("dispatch")
    dispatch.add_argument("--date", default=date.today().isoformat())
    dispatch.add_argument("--assignee")
    dispatch.add_argument("--project")
    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--runtime-id", required=True)
    bootstrap.add_argument("--daemon-id", required=True)
    args = parser.parse_args()

    cli = MulticaCLI()
    if args.command == "status":
        result = {**cli.status().as_dict(), "bootstrap": load_bootstrap_state()}
    elif args.command == "audit":
        result = run_integration_audit(args.date, args.issue_id, cli=cli)
    elif args.command == "bootstrap":
        result = bootstrap_workspace(
            cli,
            runtime_id=args.runtime_id,
            daemon_id=args.daemon_id,
        )
    else:
        if args.command == "prepare":
            result = prepare_daily_issue(args.date)
        else:
            result = dispatch_daily_issue(
                args.date,
                cli,
                assignee=args.assignee,
                project=args.project,
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
