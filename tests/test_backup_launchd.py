import plistlib

import pytest

from test_archive_launchd import installation, response


def runtime(monkeypatch, root, home, *, loaded=True, disabled=False, drift=False, exit_code=0):
    from trading_team import backup_launchd as module
    config = module.launch_agent_config(root)
    path = home / "Library/LaunchAgents" / f"{module.LABEL}.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(config))
    calls = []
    def probe(*args):
        calls.append(args)
        if args[0] == "print-disabled":
            return response(stdout=f'disabled services = {{ "{module.LABEL}" => {str(disabled).lower()} }}')
        if args[0] == "print" and not loaded:
            return response(113, stderr="Could not find service")
        command = [*config["ProgramArguments"]]
        if drift:
            command[2] = "trading_team.kimi_daily"
        return response(stdout=f"\tpath = {path}\n\tstate = not running\n\tprogram = {command[0]}\n"
                        + f"\tworking directory = {root}\n\targuments = {{\n"
                        + "".join(f"\t\t{arg}\n" for arg in command) + "\t}\n\truns = 2\n"
                        + f"\tlast exit code = {exit_code}\n"
                        + "".join(f'\t\t"Minute" => {m}\n' for m in (0, 15, 30, 45)))
    monkeypatch.setattr(module, "_launchctl", probe)
    monkeypatch.setattr(module, "service_status", lambda **_: {"status": "healthy"})
    return module, path, calls


def test_timer_runs_only_backup_checker(installation):
    from trading_team.backup_launchd import launch_agent_config
    root, _ = installation
    config = launch_agent_config(root)
    assert config["ProgramArguments"] == [str(root / ".venv/bin/python"), "-m",
                                          "trading_team.backup_service", "tick", "--trigger-source", "launchd"]
    assert config["StartCalendarInterval"] == [{"Minute": m} for m in (0, 15, 30, 45)]
    assert config["RunAtLoad"] and not config.get("KeepAlive")
    assert config["Umask"] == 63


@pytest.mark.parametrize("flags,expected", [({}, "waiting"), ({"disabled": True}, "disabled"),
    ({"loaded": False}, "not_loaded"), ({"drift": True}, "configuration_mismatch"), ({"exit_code": 1}, "failed")])
def test_scheduler_facts_not_confused_with_backup_health(installation, monkeypatch, flags, expected):
    root, home = installation
    module, _, _ = runtime(monkeypatch, root, home, **flags)
    assert module.launchd_status(root=root, home=home)["status"] == expected


def test_install_idempotent_and_never_overrides_disable(installation, monkeypatch):
    root, home = installation
    module, path, calls = runtime(monkeypatch, root, home)
    assert module.install(root=root, home=home)["status"] == "already_loaded"
    assert all(c[0] in ("print", "print-disabled") for c in calls)
    module, path, calls = runtime(monkeypatch, root, home, loaded=False, disabled=True)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="disabled"):
        module.install(root=root, home=home)
    assert path.read_bytes() == before and not any(c[0] == "bootstrap" for c in calls)


def test_install_only_bootstraps_confirmed_unloaded_service(installation, monkeypatch):
    root, home = installation
    module, _, calls = runtime(monkeypatch, root, home, loaded=False)
    assert module.install(root=root, home=home)["status"] == "bootstrapped"
    assert sum(c[0] == "bootstrap" for c in calls) == 1
    monkeypatch.setattr(module, "_launchctl", lambda *_: response(1, stderr="not permitted"))
    with pytest.raises(ValueError, match="confirm"):
        module.install(root=root, home=home)


def test_install_refuses_invalid_store_or_changed_config(installation, monkeypatch):
    root, home = installation
    module, path, _ = runtime(monkeypatch, root, home)
    monkeypatch.setattr(module, "service_status", lambda **_: {"status": "invalid"})
    with pytest.raises(ValueError, match="store"):
        module.install(root=root, home=home)
    monkeypatch.setattr(module, "service_status", lambda **_: {"status": "healthy"})
    path.write_bytes(plistlib.dumps({"Label": module.LABEL}))
    with pytest.raises(ValueError, match="configuration"):
        module.install(root=root, home=home)
