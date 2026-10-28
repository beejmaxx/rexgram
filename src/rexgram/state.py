"""Shared state between the bot and the hooks, kept in one locked JSON file.

    active    [session_id, block_id] of the active pane, which plain messages go to
    awaiting  block_id -> [time, text] you last sent it from Telegram; the reply to it comes back
    listed    the panes shown by the last /ls, so "/t 3" means what you saw
    messages  Telegram message ID -> [session_id, block_id], for reply routing
    last_to   block_id your last message went to, to say so when that changes
    muted     when true, nothing is sent unprompted
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from typing import Iterator

from rexgram.config import state_dir

MAX_MESSAGES = 2000
AWAIT_SECONDS = 12 * 3600


def _path():
    return state_dir() / "state.json"


def load() -> dict:
    try:
        return json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}


@contextmanager
def update() -> Iterator[dict]:
    """Read, modify, and atomically write the state while holding an exclusive lock."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = load()
        yield data
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)


def _pane(value) -> tuple[str, str] | None:
    return (value[0], value[1]) if isinstance(value, list) and len(value) == 2 else None


def active(data: dict) -> tuple[str, str] | None:
    return _pane(data.get("active"))


def awaiting_since(data: dict, block_id: str) -> tuple[float, str] | None:
    """When and what you last sent this pane from Telegram, if it hasn't answered yet."""
    value = data.get("awaiting", {}).get(block_id)
    if not isinstance(value, list) or len(value) != 2 or time.time() - value[0] > AWAIT_SECONDS:
        return None
    return value[0], value[1]


def awaiting(data: dict, block_id: str) -> bool:
    return awaiting_since(data, block_id) is not None


def remember_messages(message_ids: list[int], session_id: str, block_id: str) -> None:
    with update() as data:
        messages = data.setdefault("messages", {})
        for mid in message_ids:
            messages[str(mid)] = [session_id, block_id]
        for old in list(messages)[: max(0, len(messages) - MAX_MESSAGES)]:
            del messages[old]


def message_pane(data: dict, message_id: int) -> tuple[str, str] | None:
    return _pane(data.get("messages", {}).get(str(message_id)))
