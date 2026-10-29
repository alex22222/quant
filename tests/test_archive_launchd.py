import plistlib
import subprocess

import pytest


@pytest.fixture
def installation(tmp_path):
    root, home = tmp_path / "project", tmp_path / "home"
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/python").touch()
    (root / "trading_team").mkdir()
    return root, home


def response(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def test_plist_contains_only_model_free_command_and_calendar_trigger(installation):
    from trading_team.archive_launchd import LABEL, launch_agent_config
    root, _ = installation
    config = launch_agent_config(root)
    assert config["Label"] == LABEL
    assert config["ProgramArguments"] == [str(root / ".venv/bin/python"), "-m",
                                          "trading_team.archive_service", "tick", "--trigger-source", "launchd"]
    assert config["StartCalendarInterval"] == [{"Minute": value} for value in range(0, 60, 5)]
    assert config["RunAtLoad"] is True
    assert not config.get("KeepAlive")
    assert all("KEY" not in key for key in config["EnvironmentVariables"])
    assert config["EnvironmentVariables"]["HTTPS_PROXY"] == ""
    assert config["EnvironmentVariables"]["NO_PROXY"] == "*"
    assert plistlib.loads(plistlib.dumps(config)) == config


def test_installer_creates_only_own_plist_and_bootstraps(installation, monkeypatch):
    from trading_team import archive_launchd
    root, home = installation
    calls = []
    def launchctl(*args):
        calls.append(args)
        if args[0] == "print-disabled":
            return response(stdout="disabled services = {}")
        return response(113, stderr="Could not find service") if args[0] == "print" else response()
    monkeypatch.setattr(archive_launchd, "_launchctl", launchctl)
    result = archive_launchd.install(root=root, home=home)
    path = home / "Library/LaunchAgents" / f"{archive_launchd.LABEL}.plist"
    assert result["status"] == "bootstrapped"
    assert plistlib.loads(path.read_bytes()) == archive_launchd.launch_agent_config(root)
    assert sum(args[0] == "bootstrap" for args in calls) == 1
    assert all(args[0] not in ("bootout", "enable", "disable", "kickstart") for args in calls)


def test_installer_refuses_existing_different_configuration(installation, monkeypatch):
    from trading_team import archive_launchd
    root, home = installation
    path = home / "Library/LaunchAgents" / f"{archive_launchd.LABEL}.plist"
    path.parent.mkdir(parents=True)
    original = plistlib.dumps({"Label": archive_launchd.LABEL, "ProgramArguments": ["/other/python"]})
    path.write_bytes(original)
    monkeypatch.setattr(archive_launchd, "_launchctl", lambda *_: pytest.fail("launchctl called"))
    with pytest.raises(ValueError, match="configuration"):
        archive_launchd.install(root=root, home=home)
    assert path.read_bytes() == original


def prepared_runtime(installation, monkeypatch, *, state="not running", exit_code=0, disabled=False, loaded=True):
    from trading_team import archive_launchd
    root, home = installation
    path = home / "Library/LaunchAgents" / f"{archive_launchd.LABEL}.plist"
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps(archive_launchd.launch_agent_config(root)))
    config = archive_launchd.launch_agent_config(root)
    output = (f"gui/502/{archive_launchd.LABEL} = {{\n\tpath = {path}\n\tstate = {state}\n"
              f"\tprogram = {root / '.venv/bin/python'}\n\tworking directory = {root}\n"
              "\targuments = {\n" + "".join(f"\t\t{arg}\n" for arg in config["ProgramArguments"])
              + f"\t}}\n\truns = 2\n\tlast exit code = {exit_code}\n"
              + "".join(f'\t\t\t\t"Minute" => {minute}\n' for minute in range(0, 60, 5)) + "}\n")
    def launchctl(*args):
        if args[0] == "print-disabled":
            return response(stdout=f'disabled services = {{ "{archive_launchd.LABEL}" => {str(disabled).lower()} }}')
        return response(stdout=output) if loaded else response(113, stderr="Could not find service")
    monkeypatch.setattr(archive_launchd, "_launchctl", launchctl)
    return archive_launchd, path


@pytest.mark.parametrize("state", ["not running", "running"])
def test_loaded_timer_between_invocations_is_not_called_stopped(installation, monkeypatch, state):
    module, _ = prepared_runtime(installation, monkeypatch, state=state)
    result = module.launchd_status(root=installation[0], home=installation[1])
    assert result["loaded"] is True and result["config_matches"] is True
    assert result["status"] == ("running" if state == "running" else "waiting")
    assert result["runs"] == 2


@pytest.mark.parametrize("flags,expected", [
    ({"disabled": True}, "disabled"), ({"loaded": False}, "not_loaded"),
    ({"exit_code": 1}, "failed"),
])
def test_file_presence_is_not_scheduler_health(installation, monkeypatch, flags, expected):
    module, _ = prepared_runtime(installation, monkeypatch, **flags)
    assert module.launchd_status(root=installation[0], home=installation[1])["status"] == expected


def test_runtime_configuration_drift_is_not_healthy(installation, monkeypatch):
    module, _ = prepared_runtime(installation, monkeypatch)
    launchctl = module._launchctl
    def changed(*args):
        result = launchctl(*args)
        result.stdout = result.stdout.replace("trading_team.archive_service", "trading_team.kimi_daily")
        return result
    monkeypatch.setattr(module, "_launchctl", changed)
    result = module.launchd_status(root=installation[0], home=installation[1])
    assert result["status"] == "configuration_mismatch"
    assert result["config_matches"] is False


def test_runtime_probe_error_is_unknown_not_missing(installation, monkeypatch):
    module, _ = prepared_runtime(installation, monkeypatch)
    monkeypatch.setattr(module, "_launchctl", lambda *_: response(1, stderr="operation not permitted"))
    result = module.launchd_status(root=installation[0], home=installation[1])
    assert result["status"] == "unknown" and result["loaded"] is None


def test_identical_install_does_not_restart_a_loaded_service(installation, monkeypatch):
    module, _ = prepared_runtime(installation, monkeypatch)
    original = module._launchctl
    calls = []
    def launchctl(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(module, "_launchctl", launchctl)
    assert module.install(root=installation[0], home=installation[1])["status"] == "already_loaded"
    assert all(args[0] in ("print", "print-disabled") for args in calls)


def test_installer_does_not_override_explicit_disable(installation, monkeypatch):
    module, path = prepared_runtime(installation, monkeypatch, loaded=False, disabled=True)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="explicitly disabled"):
        module.install(root=installation[0], home=installation[1])
    assert path.read_bytes() == before
