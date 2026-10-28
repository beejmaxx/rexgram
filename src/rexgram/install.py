"""Install and remove the agent hooks and the background service."""

from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from rexgram import rex

LABEL = "dev.rexgram.bot"


def executable() -> str:
    found = shutil.which("rexgram")
    return found or str(Path(sys.argv[0]).resolve())


# Claude Code


def claude_dirs() -> list[Path]:
    candidates = [os.environ.get("CLAUDE_CONFIG_DIR"), "~/.claude"]
    dirs: list[Path] = []
    for c in candidates:
        if c and (p := Path(c).expanduser()).is_dir() and p.resolve() not in [d.resolve() for d in dirs]:
            dirs.append(p)
    return dirs


def _is_ours(hook: dict) -> bool:
    return "rexgram" in hook.get("command", "") and hook.get("command", "").endswith("hook claude")


def claude_hook(settings_dir: Path, remove: bool = False) -> str:
    path = settings_dir / "settings.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    stop = hooks.setdefault("Stop", [])
    had = any(_is_ours(h) for group in stop for h in group.get("hooks", []))
    for group in stop:
        group["hooks"] = [h for h in group.get("hooks", []) if not _is_ours(h)]
    stop[:] = [g for g in stop if g.get("hooks")]
    if not remove:
        command = f"{shlex.quote(executable())} hook claude"
        stop.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    if not stop:
        del hooks["Stop"]
    if not hooks:
        del data["hooks"]
    if path.exists() and not path.with_name("settings.json.rexgram-backup").exists():
        shutil.copy2(path, path.with_name("settings.json.rexgram-backup"))
    path.write_text(json.dumps(data, indent=2) + "\n")
    if remove:
        return f"removed the Stop hook from {path}" if had else f"no hook in {path}"
    return f"{'updated' if had else 'added'} the Stop hook in {path}"


# Codex


def codex_config() -> Path:
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "config.toml"


def codex_hook(remove: bool = False) -> str:
    """Codex runs one notify program, so only set it when nothing else has claimed it."""
    path = codex_config()
    if not path.parent.is_dir():
        return "Codex isn't installed; skipped"
    lines = path.read_text().splitlines() if path.exists() else []
    first_table = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
    notify = next((i for i in range(first_table) if re.match(r"\s*notify\s*=", lines[i])), None)
    ours = json.dumps([executable(), "hook", "codex"])
    if remove:
        if notify is not None and "rexgram" in lines[notify] and lines[notify].rstrip().endswith("]"):
            del lines[notify]
            path.write_text("\n".join(lines) + "\n")
            return f"removed notify from {path}"
        return f"left {path} alone (no rexgram notify line)"
    if notify is None:
        lines.insert(0, f"notify = {ours}")
        path.write_text("\n".join(lines) + "\n")
        return f"set notify in {path}"
    if "rexgram" in lines[notify]:
        return f"notify in {path} already calls rexgram"
    return (f"{path} already has a notify program, so it was left alone. Have that program also run:\n"
            f"    {shlex.join([executable(), 'hook', 'codex'])} '<payload json>'")


# Background service


def _log_path() -> Path:
    if platform.system() == "Darwin":
        return Path("~/Library/Logs/rexgram.log").expanduser()
    return Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser() / "rexgram" / "rexgram.log"


def _service_path() -> str:
    dirs = [os.path.dirname(executable()), os.path.dirname(rex.rex_bin()),
            "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    return ":".join(dict.fromkeys(d for d in dirs if d))


def service(action: str) -> str:
    if platform.system() == "Darwin":
        return _launchd(action)
    if shutil.which("systemctl"):
        return _systemd(action)
    return "No launchd or systemd here; run `rexgram serve` under your own supervisor."


def _launchd(action: str) -> str:
    plist = Path(f"~/Library/LaunchAgents/{LABEL}.plist").expanduser()
    domain = f"gui/{os.getuid()}"
    run = lambda *a: subprocess.run(["launchctl", *a], capture_output=True, text=True)  # noqa: E731
    if action == "status":
        out = run("print", f"{domain}/{LABEL}")
        if out.returncode != 0:
            return "not installed" if not plist.exists() else "installed but not loaded"
        state = re.search(r"^\s*state = (.+)$", out.stdout, re.M)
        pid = re.search(r"^\s*pid = (\d+)$", out.stdout, re.M)
        return f"{state.group(1) if state else 'loaded'}{f', pid {pid.group(1)}' if pid else ''}; log: {_log_path()}"
    run("bootout", f"{domain}/{LABEL}")
    if action == "uninstall":
        plist.unlink(missing_ok=True)
        return "stopped and removed the launchd agent"
    if action == "restart" and plist.exists():
        out = run("bootstrap", domain, str(plist))
        return "restarted" if out.returncode == 0 else f"launchctl failed: {out.stderr.strip()}"
    plist.parent.mkdir(parents=True, exist_ok=True)
    _log_path().parent.mkdir(parents=True, exist_ok=True)
    with open(plist, "wb") as f:
        plistlib.dump({
            "Label": LABEL,
            "ProgramArguments": [executable(), "serve"],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "StandardOutPath": str(_log_path()),
            "StandardErrorPath": str(_log_path()),
            "EnvironmentVariables": {"PATH": _service_path(), "PYTHONUNBUFFERED": "1"},
        }, f)
    out = run("bootstrap", domain, str(plist))
    if out.returncode != 0:
        return f"wrote {plist}, but launchctl failed: {out.stderr.strip()}"
    return f"running as launchd agent {LABEL}; log: {_log_path()}"


def _systemd(action: str) -> str:
    unit = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "systemd/user/rexgram.service"
    ctl = lambda *a: subprocess.run(["systemctl", "--user", *a], capture_output=True, text=True)  # noqa: E731
    if action == "status":
        return ctl("is-active", "rexgram").stdout.strip() or "unknown"
    if action == "uninstall":
        ctl("disable", "--now", "rexgram")
        unit.unlink(missing_ok=True)
        ctl("daemon-reload")
        return "stopped and removed the systemd unit"
    if action == "restart":
        ctl("restart", "rexgram")
        return "restarted"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        "[Unit]\nDescription=rexgram: Telegram bridge for Rex agents\n\n"
        f"[Service]\nExecStart={shlex.quote(executable())} serve\n"
        f"Environment=PATH={_service_path()}\nEnvironment=PYTHONUNBUFFERED=1\n"
        "Restart=always\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n"
    )
    ctl("daemon-reload")
    out = ctl("enable", "--now", "rexgram")
    return "running as systemd user service rexgram" if out.returncode == 0 else f"systemctl failed: {out.stderr.strip()}"
