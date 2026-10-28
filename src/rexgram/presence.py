"""Are you at the computer? Used to decide whether the phone should buzz."""

from __future__ import annotations

import platform
import re
import subprocess

from rexgram.config import Config


def idle_seconds() -> float | None:
    """Seconds since the last keyboard or mouse input, or None where that isn't known."""
    if platform.system() != "Darwin":
        return None
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r'"HIDIdleTime" = (\d+)', out)
    return int(match.group(1)) / 1e9 if match else None


def away(cfg: Config) -> bool:
    if cfg.notify == "always":
        return True
    if cfg.notify == "asked":
        return False
    idle = idle_seconds()
    return idle is None or idle >= cfg.away_after
