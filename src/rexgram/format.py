"""Turn agent Markdown into Telegram HTML, split to fit Telegram's message limit."""

from __future__ import annotations

import html
import re

LIMIT = 3800  # Telegram allows 4096 characters of text per message

FENCE = re.compile(r"^\s*(```|~~~)")


def escape(text: str) -> str:
    return html.escape(text, quote=False)


def _inline(text: str) -> str:
    """Escape one non-code paragraph and render the inline Markdown Telegram supports."""
    parts = re.split(r"(`[^`\n]+`)", text)
    out = []
    for part in parts:
        if len(part) > 1 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{escape(part[1:-1])}</code>")
            continue
        part = escape(part)
        part = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', part)
        part = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", part)
        part = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", part)
        out.append(part)
    return "".join(out)


def markdown_to_html(text: str) -> str:
    out: list[str] = []
    prose: list[str] = []
    code: list[str] | None = None
    for line in text.split("\n"):
        if FENCE.match(line):
            if code is None:
                out.append(_inline("\n".join(prose)))
                prose, code = [], []
            else:
                out.append("<pre>" + escape("\n".join(code)) + "</pre>")
                code = None
        elif code is not None:
            code.append(line)
        else:
            prose.append(line)
    if code is not None:
        out.append("<pre>" + escape("\n".join(code)) + "</pre>")
    out.append(_inline("\n".join(prose)))
    return "\n".join(p for p in out if p).strip()


def split_markdown(text: str, limit: int = LIMIT) -> list[str]:
    """Split on line boundaries, closing and reopening code fences across chunks."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    fence = ""

    def flush():
        nonlocal current, size
        if current:
            chunks.append("\n".join(current + (["```"] if fence else [])))
        current = [fence] if fence else []
        size = len(fence)

    for line in text.split("\n"):
        while len(line) > limit:
            flush()
            current.append(line[:limit])
            size += limit
            line = line[limit:]
        if size + len(line) + 1 > limit:
            flush()
        current.append(line)
        size += len(line) + 1
        if FENCE.match(line):
            fence = "" if fence else line.strip()
    if any(line.strip() for line in current if line != fence):
        chunks.append("\n".join(current + (["```"] if fence else [])))
    return chunks or [""]


def pre(text: str, limit: int = LIMIT) -> str:
    """A <pre> block of the end of text, trimmed to fit one message."""
    text = text.rstrip()
    if len(text) > limit:
        text = "…" + text[-limit:]
    return f"<pre>{escape(text)}</pre>"


def tail(text: str, lines: int, width: int = 160) -> str:
    rows = text.rstrip().split("\n")[-lines:]
    return "\n".join(r if len(r) <= width else r[: width - 1] + "…" for r in rows)


RULE = re.compile(r"^[\s─━═▔▁╌┄-]*$")
OPTION = re.compile(r"^\s*(?:[❯›>▶]\s*)?(\d)[.)]\s+(\S.*?)\s*$")


def clean_screen(text: str) -> str:
    """Drop the rules and padding that full-screen agents draw, keeping the words."""
    out: list[str] = []
    for line in text.rstrip().split("\n"):
        line = line.rstrip()
        if line and RULE.match(line):
            continue
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip("\n")


def prompt_options(screen: str, look_back: int = 25) -> list[tuple[str, str]]:
    """The numbered choices of the prompt at the bottom of a screen, e.g. [("1", "Yes"), ...]."""
    found: list[tuple[str, str]] = []
    for line in screen.rstrip().split("\n")[-look_back:]:
        match = OPTION.match(line)
        if match and match.group(1) == str(len(found) + 1):
            found.append((match.group(1), match.group(2)))
        elif match and match.group(1) == "1":
            found = [(match.group(1), match.group(2))]  # a newer list starts below an older one
    return found if len(found) >= 2 else []
