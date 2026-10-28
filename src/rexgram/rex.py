"""Thin wrapper around the rex CLI.

rexgram never starts a Rex server: every call passes --autostart=false, so a missing
server is an error rather than a surprise background process.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess


class RexError(RuntimeError):
    pass


def rex_bin() -> str:
    return os.environ.get("REXGRAM_REX") or shutil.which("rex") or os.path.expanduser("~/.local/bin/rex")


def run(*args: str, timeout: float = 15, stdin: str | None = None) -> str:
    cmd = [rex_bin(), "--autostart=false", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin)
    except FileNotFoundError:
        raise RexError(f"rex not found at {cmd[0]}; set REXGRAM_REX") from None
    except subprocess.TimeoutExpired:
        raise RexError(f"rex {args[0]} timed out") from None
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip().splitlines()
        raise RexError(msg[0].removeprefix("Error: ") if msg else f"rex {args[0]} failed")
    return proc.stdout


def run_json(*args: str, **kwargs) -> dict:
    out = run(*args, **kwargs)
    return json.loads(out) if out.strip() else {}


def sessions() -> list[dict]:
    return run_json("ls", "--json").get("sessions", [])


def blocks(session_id: str) -> list[dict]:
    return run_json("block", "ls", "-s", session_id, "--json").get("blocks", [])


def call(session_id: str, block_id: str, method: str, args: dict | None = None) -> dict:
    argv = ["block", "call", "-s", session_id, "-b", block_id, method]
    if args is not None:
        argv.append(json.dumps(args))
    return run_json(*argv)


def capture(session_id: str, block_id: str) -> str:
    return run("capture", "-s", session_id, "-b", block_id, "--trim", "--unwrap")


def paste(session_id: str, block_id: str, text: str) -> None:
    """Paste text as one bracketed paste, so newlines don't submit early."""
    request = {
        "session_id": session_id,
        "block_id": block_id,
        "event": {"type": "paste", "payload": {"text": text}},
    }
    run("api", "call", "block.event", "-", stdin=json.dumps(request))


def send_keys(session_id: str, block_id: str, *keys: str) -> None:
    run("send-key", "-s", session_id, "-b", block_id, *keys)


def close(session_id: str, block_id: str) -> None:
    run("block", "close", "-s", session_id, block_id)


def new_window(session_id: str, label: str, cwd: str, argv: list[str]) -> str:
    """Open a background tab running argv and return its block ID."""
    out = run_json("window", "new", "-s", session_id, label, "-c", cwd, "--focus=false", "--json", "--", *argv)
    return out["block_ids"][0]


def focused(session_id: str) -> str | None:
    """The focused block of the session's active window."""
    view = run_json("session", "inspect", session_id, "--json")
    window = next((w for w in view.get("windows", []) if w["window_id"] == view.get("active_window_id")), None)
    return window.get("focused_block_id") if window else None


def focus(session_id: str, block_id: str) -> None:
    """Focus a block, and have every Rex app that is connected show its session."""
    run("focus", "-s", session_id, block_id)
    for client in run_json("client", "ls", "--json").get("clients", []):
        if (client.get("info") or {}).get("kind") == "app":
            try:
                run("-C", client["client_id"], "do", "session.select", f"session_id={session_id}")
            except RexError:
                pass  # an app that can't switch sessions still has the focus moved


def find_session(block_id: str) -> str | None:
    for session in sessions():
        if any(b["block_id"] == block_id for b in blocks(session["session_id"])):
            return session["session_id"]
    return None


def events() -> subprocess.Popen:
    """Stream every server event as one JSON object per line."""
    return subprocess.Popen(
        [rex_bin(), "--autostart=false", "events", "--all", "--json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
