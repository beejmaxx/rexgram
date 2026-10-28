"""Agent hooks: forward an agent's final reply to Telegram when its turn ends.

Claude Code runs `rexgram hook claude` as a Stop hook (JSON on stdin).
Codex runs `rexgram hook codex '<json>'` as its notify program.
Both run inside the agent's Rex terminal, so REX_SESSION and REX_BLOCK say which pane
the reply came from.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from rexgram import agents, config, presence, state, telegram
from rexgram import format as fmt
from rexgram.config import state_dir

log = logging.getLogger("rexgram.hook")


def claude_reply(data: dict) -> str:
    text = data.get("last_assistant_message")
    if isinstance(text, str) and text.strip():
        return text
    path = data.get("transcript_path")
    if not path:
        return ""
    # The transcript can lag the Stop hook by a moment; wait for the turn's last text.
    for _ in range(10):
        text, last_is_text = _transcript_reply(Path(path))
        if last_is_text:
            return text
        time.sleep(0.3)
    return text


def _transcript_reply(path: Path) -> tuple[str, bool]:
    """The last assistant text in a transcript, and whether it is the final entry."""
    reply, last_is_text = "", False
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return "", False
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") not in ("assistant", "user"):
            continue
        content = entry.get("message", {}).get("content", [])
        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        if entry["type"] == "assistant" and texts:
            reply, last_is_text = "\n".join(texts), True
        else:
            last_is_text = False
    return reply, last_is_text


def _timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


def claude_turn_has_input_since(data: dict, since: float) -> bool:
    """Whether this turn took in input that arrived after `since`.

    A message pasted while Claude is mid-turn is queued and starts the next turn, or is
    attached to the current one; either way it is logged with the time Claude took it in.
    """
    path = data.get("transcript_path")
    if not path:
        return True
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return True
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not entry.get("timestamp"):
            continue
        if _timestamp(entry["timestamp"]) < since - 2:
            return False
        if entry.get("type") == "attachment" and (entry.get("attachment") or {}).get("type") == "queued_command":
            return True
        if entry.get("type") == "user":
            content = (entry.get("message") or {}).get("content")
            if isinstance(content, str) or any(isinstance(c, dict) and c.get("type") == "text" for c in content or []):
                return True
    return False


def codex_turn_has(data: dict, text: str) -> bool:
    needle = " ".join(text.split())[:60]
    return any(needle in " ".join(str(m).split()) for m in data.get("input-messages") or [])


def codex_reply(data: dict) -> str:
    if data.get("type") != "agent-turn-complete":
        return ""
    return data.get("last-assistant-message") or ""


def wants(cfg: config.Config, block_id: str) -> bool:
    """Forward this pane's reply if you asked from Telegram, or if you're away and it's active."""
    if not cfg.ready:
        return False
    data = state.load()
    if data.get("muted"):
        return False
    if state.awaiting(data, block_id):
        return True
    active = state.active(data)
    return active is not None and active[1] == block_id and presence.away(cfg)


def answers_telegram(app: str, data: dict, block_id: str) -> bool | None:
    """Whether this turn answered what you sent from Telegram; None if you sent nothing."""
    sent = state.awaiting_since(state.load(), block_id)
    if sent is None:
        return None
    if app == "claude":
        return claude_turn_has_input_since(data, sent[0])
    return codex_turn_has(data, sent[1])


def _detach() -> None:
    """Return control to the agent at once and finish in the background."""
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)


def forward(app: str, text: str, session_id: str, block_id: str, clear: bool = True) -> None:
    cfg = config.load()
    agent = agents.describe(session_id, block_id, agents.AGENT_APPS | cfg.extra_apps)
    header = f"<b>{fmt.escape(agent.name if agent else app)}</b>"
    ids = telegram.Telegram(cfg.token).send_markdown(cfg.chat_id, header, text.strip())
    state.remember_messages(ids, session_id, block_id)
    if clear:
        with state.update() as data:
            data.get("awaiting", {}).pop(block_id, None)


def run(app: str, payload: str | None) -> int:
    session_id = os.environ.get("REX_SESSION", "")
    block_id = os.environ.get("REX_BLOCK", "")
    if not (session_id and block_id):
        return 0  # not running inside Rex
    try:
        if app == "claude":
            data = json.loads(sys.stdin.read() or "{}")
        else:
            data = json.loads(payload or "{}")
    except ValueError:
        return 0
    cfg = config.load()
    if not wants(cfg, block_id):
        return 0
    _detach()
    state_dir().mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=state_dir() / "hook.log", level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        answered = answers_telegram(app, data, block_id)
        if answered is False:
            # This turn was already underway when your message arrived; its own turn comes next.
            active = state.active(state.load())
            if not (active and active[1] == block_id and presence.away(cfg)):
                return 0
        text = claude_reply(data) if app == "claude" else codex_reply(data)
        if text.strip():
            forward(app, text, session_id, block_id, clear=answered is not False)
    except Exception:
        log.exception("forwarding a %s reply from %s failed", app, block_id)
    return 0
