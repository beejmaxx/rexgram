"""Find the coding agents running in Rex terminal blocks."""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from rexgram import rex

AGENT_APPS = {
    "aider", "amp", "claude", "codex", "copilot", "crush", "cursor-agent",
    "droid", "gemini", "goose", "opencode", "pi", "qwen",
}
APP_ALIASES = {"claude-code": "claude", "codex-cli": "codex", "gemini-cli": "gemini", "cursor": "cursor-agent"}
TERMINAL_CREATOR = "com.superlogical.terminal"
STATE_ICONS = {"working": "⚙️", "blocked": "❗", "done": "✅", "idle": "💤", "error": "❌"}
GENERIC_TITLES = {"claude code", "codex", "claude"}


@dataclass
class Agent:
    session_id: str
    session_label: str
    block_id: str
    block_label: str
    app: str
    state: str
    kind: str
    title: str
    cwd: str

    @property
    def short(self) -> str:
        return self.block_id[-6:]

    @property
    def project(self) -> str:
        return os.path.basename(self.cwd.rstrip("/")) or self.block_label or self.short

    @property
    def name(self) -> str:
        return f"{self.app} · {self.project}"

    @property
    def icon(self) -> str:
        return STATE_ICONS.get(self.state, "·")


def normalize_app(name: str) -> str:
    name = name.strip().lower()
    return APP_ALIASES.get(name, name)


def app_from_process(foreground: dict, apps: set[str]) -> str | None:
    for key in ("argv0", "invoked_path", "path", "name"):
        base = os.path.basename(foreground.get(key) or "").lstrip("-").lower()
        if normalize_app(base) in apps:
            return normalize_app(base)
    # Claude Code runs as a versioned binary, e.g. ~/.local/share/claude/versions/2.1.312.
    if "/claude/versions/" in (foreground.get("path") or ""):
        return "claude"
    return None


def status_record(records: list[dict], pid: int | None) -> dict:
    """The newest root status record owned by the foreground process."""
    mine = [
        r for r in records
        if not r.get("id") and (pid is None or not r.get("owner") or r["owner"].get("pid") == pid)
    ]
    return max(mine, key=lambda r: r.get("updated_at", ""), default={})


def clean_title(title: str, project: str = "") -> str:
    # Agents prefix titles with spinners and status glyphs such as ⠹ or ✳.
    title = re.sub(r"^[^\w(\[]+", "", title).strip()
    if project and title.endswith(f" | {project}"):
        title = title[: -len(project) - 3]
    return "" if title.lower() in GENERIC_TITLES else title


def inspect_block(session_id: str, block_id: str, session_label: str = "", block_label: str = "",
                  apps: set[str] = AGENT_APPS) -> Agent | None:
    """Describe the agent in a block, or None if its foreground program isn't one."""
    foreground = rex.call(session_id, block_id, "process").get("foreground") or {}
    if not foreground:
        return None
    record = status_record(rex.call(session_id, block_id, "program_status").get("records", []), foreground.get("pid"))
    app = app_from_process(foreground, apps)
    if app is None and record:
        reported = normalize_app(record.get("effective_app") or record.get("app") or "")
        app = reported if reported in apps else None
    if app is None:
        return None
    cwd = foreground.get("cwd") or ""
    try:
        title = rex.call(session_id, block_id, "title").get("title", "")
    except rex.RexError:
        title = ""
    return Agent(
        session_id=session_id,
        session_label=session_label,
        block_id=block_id,
        block_label=block_label,
        app=app,
        state=record.get("state", "idle") if record else "idle",
        kind=record.get("kind", "") if record else "",
        title=clean_title(title, os.path.basename(cwd.rstrip("/"))),
        cwd=cwd,
    )


def list_agents(apps: set[str] = AGENT_APPS) -> list[Agent]:
    """Every agent on the server, in session and block order."""
    jobs = []
    for session in rex.sessions():
        sid = session["session_id"]
        for block in rex.blocks(sid):
            if block.get("creator_name") == TERMINAL_CREATOR:
                jobs.append((sid, block["block_id"], session.get("label", ""), block.get("label", "")))

    def inspect(job):
        try:
            return inspect_block(*job, apps=apps)
        except rex.RexError:
            return None  # the block closed while we were looking

    with ThreadPoolExecutor(max_workers=16) as pool:
        return [a for a in pool.map(inspect, jobs) if a]


def describe(session_id: str, block_id: str, apps: set[str] = AGENT_APPS) -> Agent | None:
    """inspect_block for one block, filling in its session and block labels."""
    try:
        session_label = next((s.get("label", "") for s in rex.sessions() if s["session_id"] == session_id), "")
        block_label = next((b.get("label", "") for b in rex.blocks(session_id) if b["block_id"] == block_id), "")
        return inspect_block(session_id, block_id, session_label, block_label, apps=apps)
    except rex.RexError:
        return None
