"""Install and inspect only the model-free workflow backup LaunchAgent."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from trading_team.execution_bars import SHANGHAI, TEAM
from trading_team.backup_service import service_status

LABEL = "com.hqcapital.workflow-backup"


def launch_agent_config(root: Path) -> dict:
    root = root.resolve()
    logs = root / "trading_team/.runs"
    return {
        "Label": LABEL,
        "ProgramArguments": [str(root / ".venv/bin/python"), "-m", "trading_team.backup_service",
                             "tick", "--trigger-source", "launchd"],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "StartCalendarInterval": [{"Minute": minute} for minute in range(0, 60, 15)],
        "ProcessType": "Background",
        "Umask": 63,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "HTTP_PROXY": "", "HTTPS_PROXY": "",
                                 "ALL_PROXY": "", "http_proxy": "", "https_proxy": "", "all_proxy": "",
                                 "NO_PROXY": "*", "no_proxy": "*"},
        "StandardOutPath": str(logs / "backup-service.log"),
        "StandardErrorPath": str(logs / "backup-service.error.log"),
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=10)


def _field(output: str, name: str) -> str | None:
    match = re.search(rf"^\t{re.escape(name)} = (.+)$", output, re.MULTILINE)
    return match.group(1) if match else None


def launchd_status(*, root: Path = TEAM.parent, home: Path | None = None) -> dict:
    home = home or Path.home()
    path = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    result = {"label": LABEL, "status": "unknown", "installed": path.is_file(), "loaded": None,
              "disabled": None, "config_matches": False, "runs": None, "last_exit_code": None}
    if sys.platform != "darwin":
        return {**result, "status": "unsupported"}
    expected = launch_agent_config(root)
    try:
        file_matches = path.is_file() and not path.is_symlink() and plistlib.loads(path.read_bytes()) == expected
        result["config_matches"] = file_matches
        domain = f"gui/{os.getuid()}"
        disabled = _launchctl("print-disabled", domain)
        if disabled.returncode != 0 or "disabled services" not in disabled.stdout:
            raise ValueError("launchd disable-state probe unavailable")
        match = re.search(rf'"{re.escape(LABEL)}"\s*=>\s*(true|false)', disabled.stdout)
        result["disabled"] = bool(match and match.group(1) == "true")
        probe = _launchctl("print", f"{domain}/{LABEL}")
        if probe.returncode != 0:
            if probe.returncode == 113 and "Could not find service" in probe.stderr:
                return {**result, "loaded": False,
                        "status": "disabled" if result["disabled"] else "not_loaded" if path.exists() else "not_installed"}
            raise ValueError(f"launchd runtime probe failed ({probe.returncode})")
        result["loaded"] = True
        output = probe.stdout
        args = re.search(r"^\targuments = \{\n(.*?)^\t\}", output, re.MULTILINE | re.DOTALL)
        actual_args = [line.strip() for line in args.group(1).splitlines()] if args else []
        minutes = [int(value) for value in re.findall(r'"Minute"\s*=>\s*(\d+)', output)]
        result["config_matches"] = file_matches and (
            _field(output, "path") == str(path)
            and _field(output, "program") == expected["ProgramArguments"][0]
            and _field(output, "working directory") == expected["WorkingDirectory"]
            and actual_args == expected["ProgramArguments"]
            and sorted(minutes) == list(range(0, 60, 15)))
        for field, key in (("runs", "runs"), ("last exit code", "last_exit_code"), ("pid", "pid")):
            value = _field(output, field)
            if value is not None:
                result[key] = int(value)
        state = _field(output, "state")
        result["process_state"] = state
        result["status"] = (
            "disabled" if result["disabled"] else "configuration_mismatch" if not result["config_matches"]
            else "running" if state == "running"
            else "failed" if result["last_exit_code"] not in (None, 0)
            else "waiting" if state in ("not running", "waiting") else "unknown")
        if result["config_matches"] and not result["disabled"]:
            now = datetime.now(SHANGHAI)
            result["next_scheduled_at"] = (now.replace(second=0, microsecond=0)
                                            + timedelta(minutes=15 - now.minute % 15)).isoformat()
        return result
    except (OSError, ValueError, plistlib.InvalidFileException, subprocess.SubprocessError) as exc:
        return {**result, "status": "unknown", "error": str(exc)}


def install(*, root: Path = TEAM.parent, home: Path | None = None) -> dict:
    if sys.platform != "darwin":
        raise ValueError("launchd installation requires macOS")
    readiness = service_status(team=root / "trading_team")
    if readiness["status"] in ("uninitialized", "invalid"):
        raise ValueError("backup store must be initialized and readable before installation")
    home = home or Path.home()
    path = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    config = launch_agent_config(root)
    if not Path(config["ProgramArguments"][0]).is_file():
        raise ValueError("project Python runtime missing")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or plistlib.loads(path.read_bytes()) != config:
            raise ValueError("existing backup configuration differs; explicit migration required")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            plistlib.dump(config, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
    Path(config["StandardOutPath"]).parent.mkdir(parents=True, exist_ok=True)
    status = launchd_status(root=root, home=home)
    if status["disabled"] is True:
        raise ValueError("backup service was explicitly disabled; refusing to enable it")
    if status["loaded"] is True:
        if not status["config_matches"]:
            raise ValueError("loaded backup configuration differs; explicit migration required")
        return {"status": "already_loaded", "path": str(path)}
    if status["status"] != "not_loaded":
        raise ValueError("cannot confirm unloaded backup service; refusing duplicate bootstrap")
    result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
    if result.returncode:
        raise RuntimeError(f"backup bootstrap failed ({result.returncode}); configuration retained for inspection")
    return {"status": "bootstrapped", "path": str(path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "status"))
    args = parser.parse_args()
    try:
        result = install() if args.command == "install" else launchd_status()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
