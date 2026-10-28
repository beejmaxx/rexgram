"""Configuration: a KEY=VALUE env file, overridden by REXGRAM_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# auto:   presence-aware. At the Mac you only hear back about what you sent from Telegram;
#         once you've been idle a while, the active agent's replies and any agent that is
#         blocked on you come through too.
# always: notify as if you were always away.
# asked:  only ever reply to what you sent from Telegram.
NOTIFY_MODES = ("auto", "always", "asked")


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "rexgram"


def config_path() -> Path:
    override = os.environ.get("REXGRAM_ENV_FILE")
    return Path(override).expanduser() if override else config_dir() / "config.env"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(base).expanduser() / "rexgram"


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _choice(value: str | None, choices: tuple[str, ...]) -> str:
    value = (value or "").strip().lower()
    return value if value in choices else choices[0]


@dataclass
class Config:
    token: str = ""
    chat_id: int | None = None
    allowed_users: set[int] = field(default_factory=set)
    notify: str = "auto"
    away_after: int = 180  # seconds without keyboard or mouse input before you count as away
    agent_cmd: str = "claude"
    files_dir: Path = field(default_factory=lambda: state_dir() / "files")
    extra_apps: set[str] = field(default_factory=set)

    @property
    def ready(self) -> bool:
        return bool(self.token and self.chat_id)

    def authorized(self, chat_id: int | None, user_id: int | None) -> bool:
        if not self.ready or chat_id != self.chat_id:
            return False
        return not self.allowed_users or user_id in self.allowed_users


def load() -> Config:
    raw = parse_env_file(config_path())
    raw.update({k: v for k, v in os.environ.items() if k.startswith("REXGRAM_")})

    def get(name: str) -> str | None:
        # TELEGRAM_* names are accepted in the file so an existing bot.env can be reused.
        return raw.get(f"REXGRAM_{name}") or raw.get(f"TELEGRAM_{name}")

    users = {_int(u.strip()) for u in (get("ALLOWED_USERS") or "").split(",")}
    cfg = Config(
        token=get("BOT_TOKEN") or "",
        chat_id=_int(get("CHAT_ID")),
        allowed_users={u for u in users if u is not None},
        notify=_choice(raw.get("REXGRAM_NOTIFY"), NOTIFY_MODES),
        away_after=_int(raw.get("REXGRAM_AWAY_AFTER")) or 180,
        agent_cmd=raw.get("REXGRAM_AGENT_CMD") or "claude",
        extra_apps={a.strip().lower() for a in (raw.get("REXGRAM_AGENTS") or "").split(",") if a.strip()},
    )
    if raw.get("REXGRAM_FILES_DIR"):
        cfg.files_dir = Path(raw["REXGRAM_FILES_DIR"]).expanduser()
    return cfg


def write(values: dict[str, str]) -> Path:
    """Merge values into the config file, keeping keys and comments already there."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else [
        "# rexgram configuration. See README.md for every option.",
    ]
    pending = dict(values)
    for i, line in enumerate(lines):
        key = line.strip().removeprefix("export ").partition("=")[0].strip()
        if key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    lines += [f"{k}={v}" for k, v in pending.items()]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    return path
