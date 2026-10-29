import json
from types import SimpleNamespace

import pytest

from trading_team.multica_adapter import (
    MULTICA_DIR,
    MulticaCLI,
    MulticaStatus,
    bootstrap_workspace,
    discover_multica_binary,
    dispatch_daily_issue,
    load_bootstrap_state,
    prepare_daily_issue,
)


def test_status_fails_closed_when_cli_is_missing():
    status = MulticaCLI(binary="").status()
    assert status.installed is False
    assert status.daemon_running is False


def test_binary_discovery_uses_user_local_fallback(tmp_path, monkeypatch):
    binary = tmp_path / ".local" / "bin" / "multica"
    binary.parent.mkdir(parents=True)
    binary.write_text("binary", encoding="utf-8")
    monkeypatch.setattr("trading_team.multica_adapter.shutil.which", lambda _name: None)

    assert discover_multica_binary(home=tmp_path) == str(binary)


def test_status_does_not_treat_stopped_daemon_as_authenticated():
    def runner(command, **_kwargs):
        if command[1:] == ["auth", "status"]:
            return SimpleNamespace(
                returncode=0,
                stdout="",
                stderr="No server configured. Run 'multica setup' first.\n",
            )
        if command[1:] == ["daemon", "status", "--output", "json"]:
            return SimpleNamespace(
                returncode=0,
                stdout='{"status":"stopped"}',
                stderr="",
            )
        raise AssertionError(f"unexpected command: {command}")

    status = MulticaCLI(binary="multica", runner=runner).status()

    assert status.installed is True
    assert status.server_configured is False
    assert status.authenticated is False
    assert status.connected is False
    assert status.daemon_running is False


def test_status_requires_verified_auth_and_running_daemon():
    def runner(command, **_kwargs):
        if command[1:] == ["auth", "status"]:
            return SimpleNamespace(
                returncode=0,
                stdout="",
                stderr="Server:  https://app.multica.ai\nUser:    Henry (h@example.com)\n",
            )
        if command[1:] == ["daemon", "status", "--output", "json"]:
            return SimpleNamespace(
                returncode=0,
                stdout='{"status":"running"}',
                stderr="",
            )
        raise AssertionError(f"unexpected command: {command}")

    status = MulticaCLI(binary="multica", runner=runner).status()

    assert status.server_configured is True
    assert status.authenticated is True
    assert status.connected is True
    assert status.daemon_running is True


def test_status_uses_caller_timeout_for_cloud_auth():
    timeouts = []

    def runner(command, **kwargs):
        timeouts.append((command[1:], kwargs["timeout"]))
        if command[1:] == ["auth", "status"]:
            return SimpleNamespace(
                returncode=0,
                stdout="",
                stderr="Server:  https://app.multica.ai\nUser:    Henry (h@example.com)\n",
            )
        return SimpleNamespace(
            returncode=0,
            stdout='{"status":"running"}',
            stderr="",
        )

    MulticaCLI(binary="multica", runner=runner).status(timeout=60)

    assert timeouts == [
        (["auth", "status"], 60),
        (["daemon", "status", "--output", "json"], 60),
    ]


def test_prepare_daily_issue_uses_repository_contract(tmp_path):
    payload = prepare_daily_issue("2026-09-21", tmp_path)
    description = (tmp_path / "2026-09-21-daily-research.md").read_text(encoding="utf-8")
    persisted = json.loads(
        (tmp_path / "2026-09-21-daily-research.json").read_text(encoding="utf-8")
    )

    assert payload == persisted
    assert payload["assignee"] == "A-share Investment Committee"
    assert "No Live order is submitted" in description
    assert "--trigger-source multica" in description
    assert "--publish-only --no-push" in description
    assert "Only Research Lead invokes the complete publishing command" in description
    assert "not seven independently executed Multica tasks" in description
    assert "docs/SYSTEM_ACCEPTANCE.md" in description


def test_future_manual_issue_has_chinese_title_and_description(tmp_path):
    payload = prepare_daily_issue("2026-09-24", tmp_path)
    description = (tmp_path / "2026-09-24-daily-research.md").read_text(encoding="utf-8")
    assert payload["title"] == "A股每日研究 2026-09-24"
    assert "仅发布" in description
    assert "不得提交实盘订单" in description
    assert "--publish-only --no-push" in description
    assert "--multica-issue-id <current-issue-id>" in description


def test_scheduled_runbook_binds_issue_date_to_explicit_command_date():
    workspace = json.loads(
        (MULTICA_DIR / "workspace.json").read_text(encoding="utf-8")
    )
    runbook = (
        MULTICA_DIR / "runbooks" / "daily-research.md"
    ).read_text(encoding="utf-8")

    assert workspace["autopilot"]["issue_title_template"] == (
        "A股每日研究 {{date}}"
    )
    assert "--date YYYY-MM-DD" not in runbook
    assert "--date <business-date>" in runbook
    assert "--publish-only --no-push" in runbook
    assert "Asia/Shanghai" in runbook


def test_production_runbook_preserves_exact_issue_title():
    runbook = (
        MULTICA_DIR.parent / "docs" / "MULTICA_PRODUCTION_RUNBOOK.md"
    ).read_text(encoding="utf-8")

    assert "不要重命名当前 Issue" in runbook
    assert "rename this issue" in runbook


def test_create_issue_refuses_disconnected_runtime(tmp_path):
    description = tmp_path / "issue.md"
    description.write_text("test", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Multica unavailable"):
        MulticaCLI(binary="").create_issue(
            title="test",
            description_file=description,
            assignee="Research Lead",
        )


def test_create_issue_binds_exact_assignee_id(tmp_path, monkeypatch):
    description = tmp_path / "issue.md"
    description.write_text("test", encoding="utf-8")
    cli = MulticaCLI(binary="multica")
    captured = {}
    monkeypatch.setattr(
        cli,
        "status",
        lambda **_kwargs: MulticaStatus(True, True, True, "running"),
    )
    monkeypatch.setattr(
        cli,
        "_run_json",
        lambda args: captured.update(args=args) or {"id": "issue-1"},
    )

    cli.create_issue(
        title="test",
        description_file=description,
        assignee="squad-1",
        project="project-1",
    )

    assert captured["args"][captured["args"].index("--assignee-id") + 1] == "squad-1"
    assert "--assignee" not in captured["args"]


def test_dispatch_requires_bootstrapped_resources(tmp_path):
    with pytest.raises(RuntimeError, match="not bootstrapped"):
        dispatch_daily_issue(
            "2026-09-21",
            MulticaCLI(binary="multica"),
            out_dir=tmp_path / "out",
            state_path=tmp_path / "missing.json",
        )


def test_dispatch_uses_exact_bootstrap_ids(tmp_path, monkeypatch):
    cli = MulticaCLI(binary="multica")
    captured = {}
    monkeypatch.setattr(
        cli,
        "create_issue",
        lambda **kwargs: captured.update(kwargs) or {"id": "issue-1"},
    )
    state_path = tmp_path / "bootstrap.json"
    state_path.write_text(json.dumps({
        "project_id": "project-1",
        "squad_id": "squad-1",
    }), encoding="utf-8")

    result = dispatch_daily_issue(
        "2026-09-21",
        cli,
        out_dir=tmp_path / "out",
        state_path=state_path,
    )

    assert result["issue"] == {"id": "issue-1"}
    assert captured["assignee"] == "squad-1"
    assert captured["project"] == "project-1"


def test_bootstrap_workspace_is_idempotent(tmp_path, monkeypatch):
    cli = MulticaCLI(binary="multica")
    monkeypatch.setattr(
        cli,
        "status",
        lambda **_kwargs: MulticaStatus(True, True, True, "running"),
    )
    store = {
        "projects": [],
        "resources": [],
        "agents": [],
        "squads": [],
        "members": [],
        "autopilots": [],
        "triggers": [],
    }

    def created(kind, name=None, **extra):
        value = {"id": f"{kind}-{len(store[kind + 's']) + 1}", **extra}
        if name is not None:
            value["name"] = name
        store[kind + "s"].append(value)
        return {kind: value}

    def fake_run(args, timeout=60):
        if args[:2] == ["project", "list"]:
            return {"projects": store["projects"]}
        if args[:2] == ["project", "create"]:
            title = args[args.index("--title") + 1]
            return created("project", title=title)
        if args[:3] == ["project", "resource", "list"]:
            return {"resources": store["resources"]}
        if args[:3] == ["project", "resource", "add"]:
            resource = {
                "id": f"resource-{len(store['resources']) + 1}",
                "resource_type": "local_directory",
                "resource_ref": {
                    "local_path": args[args.index("--local-path") + 1],
                    "daemon_id": args[args.index("--daemon-id") + 1],
                },
            }
            store["resources"].append(resource)
            return {"resource": resource}
        if args[:2] == ["agent", "list"]:
            return {"agents": store["agents"]}
        if args[:2] == ["agent", "create"]:
            return created("agent", args[args.index("--name") + 1])
        if args[:2] == ["squad", "list"]:
            return {"squads": store["squads"]}
        if args[:2] == ["squad", "create"]:
            result = created(
                "squad",
                args[args.index("--name") + 1],
                instructions="",
            )
            leader = args[args.index("--leader") + 1]
            store["members"].append({"member_id": leader, "member_type": "agent"})
            return result
        if args[:2] == ["squad", "get"]:
            return {"squad": store["squads"][0]}
        if args[:2] == ["squad", "update"]:
            store["squads"][0]["instructions"] = args[args.index("--instructions") + 1]
            return {"squad": store["squads"][0]}
        if args[:3] == ["squad", "member", "list"]:
            return {"members": store["members"]}
        if args[:3] == ["squad", "member", "add"]:
            store["members"].append({
                "member_id": args[args.index("--member-id") + 1],
                "member_type": "agent",
            })
            return {"member": store["members"][-1]}
        if args[:2] == ["autopilot", "list"]:
            return {"autopilots": store["autopilots"]}
        if args[:2] == ["autopilot", "create"]:
            return created(
                "autopilot",
                title=args[args.index("--title") + 1],
                agent_id=args[args.index("--agent") + 1],
                description=args[args.index("--description") + 1],
                issue_title_template=args[
                    args.index("--issue-title-template") + 1
                ],
                mode=args[args.index("--mode") + 1],
                project_id=args[args.index("--project") + 1],
            )
        if args[:2] == ["autopilot", "get"]:
            return {
                "autopilot": store["autopilots"][0],
                "triggers": store["triggers"],
            }
        if args[:2] == ["autopilot", "update"]:
            autopilot = store["autopilots"][0]
            for flag, field in (
                ("--description", "description"),
                ("--issue-title-template", "issue_title_template"),
            ):
                if flag in args:
                    autopilot[field] = args[args.index(flag) + 1]
            return {"autopilot": autopilot}
        if args[:2] == ["autopilot", "trigger-add"]:
            trigger = {
                "id": f"trigger-{len(store['triggers']) + 1}",
                "kind": "schedule",
                "cron_expression": args[args.index("--cron") + 1],
                "timezone": args[args.index("--timezone") + 1],
            }
            store["triggers"].append(trigger)
            return {"trigger": trigger}
        raise AssertionError(f"unexpected Multica command: {args}")

    monkeypatch.setattr(cli, "_run_json", fake_run)
    state_path = tmp_path / "bootstrap.json"

    first = bootstrap_workspace(
        cli,
        runtime_id="runtime-1",
        daemon_id="daemon-1",
        state_path=state_path,
    )
    second = bootstrap_workspace(
        cli,
        runtime_id="runtime-1",
        daemon_id="daemon-1",
        state_path=state_path,
    )

    assert len(first["agent_ids"]) == 7
    assert first["actions"]
    assert second["actions"] == []
    assert len(store["projects"]) == 1
    assert len(store["resources"]) == 1
    assert len(store["agents"]) == 7
    assert len(store["squads"]) == 1
    assert len(store["members"]) == 7
    assert len(store["autopilots"]) == 1
    assert first["leader_agent_id"] == first["agent_ids"]["Research Lead"]
    assert store["autopilots"][0]["agent_id"] == first["leader_agent_id"]
    assert store["autopilots"][0]["agent_id"] != first["squad_id"]
    assert store["autopilots"][0]["description"] == (
        (MULTICA_DIR.parent / "docs" / "MULTICA_PRODUCTION_RUNBOOK.md").read_text(encoding="utf-8")
    )
    assert store["autopilots"][0]["issue_title_template"] == (
        "A股每日研究 {{date}}"
    )
    assert len(store["triggers"]) == 1
    assert store["triggers"][0]["cron_expression"] == "30 7 * * 1-5"
    assert json.loads(state_path.read_text(encoding="utf-8")) == second
    assert load_bootstrap_state(state_path) == {"configured": True, **second}
