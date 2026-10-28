"""A small Telegram Bot API client on urllib.

urllib honors HTTP(S)_PROXY and, on macOS, the system proxy settings, which matters
on networks where api.telegram.org is only reachable through a proxy.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from rexgram import format as fmt

API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    def __init__(self, description: str, code: int = 0):
        super().__init__(description)
        self.code = code


class Telegram:
    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, _timeout: float = 20, **params) -> object:
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(
            f"{API}/bot{self.token}/{method}", data=body, headers={"Content-Type": "application/json"}
        )
        # Proxies sometimes drop the connection; retry, except for sends, which may have gone through.
        attempts = 1 if method.startswith("send") else 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=_timeout) as resp:
                    data = json.load(resp)
                break
            except urllib.error.HTTPError as e:
                try:
                    data = json.load(e)
                except ValueError:
                    raise TelegramError(f"HTTP {e.code}", e.code) from None
                break
            except (ConnectionError, http.client.HTTPException):
                if attempt == attempts - 1:
                    raise
                time.sleep(1 + attempt)
        if not data.get("ok"):
            raise TelegramError(data.get("description", "request failed"), data.get("error_code", 0))
        return data.get("result")

    def send(self, chat_id: int, html: str, *, markup: dict | None = None, reply_to: int | None = None,
             silent: bool = False) -> int:
        """Send one HTML message, falling back to plain text if Telegram rejects the markup."""
        params = dict(
            chat_id=chat_id,
            reply_markup=markup,
            disable_notification=silent or None,
            link_preview_options={"is_disabled": True},
            reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None,
        )
        try:
            return self.call("sendMessage", text=html, parse_mode="HTML", **params)["message_id"]
        except TelegramError as e:
            if "parse" not in str(e).lower():
                raise
        plain = re.sub(r"<[^>]+>", "", html).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        return self.call("sendMessage", text=plain, **params)["message_id"]

    def send_markdown(self, chat_id: int, header: str, markdown: str, *, markup: dict | None = None,
                      silent: bool = False) -> list[int]:
        """Send an agent's Markdown reply, split across messages; buttons go on the last one."""
        chunks = fmt.split_markdown(markdown, fmt.LIMIT - len(header))
        ids = []
        for i, chunk in enumerate(chunks):
            body = fmt.markdown_to_html(chunk)
            text = f"{header}\n\n{body}" if i == 0 and header else body
            last = i == len(chunks) - 1
            ids.append(self.send(chat_id, text or header, markup=markup if last else None, silent=silent))
        return ids

    def edit(self, chat_id: int, message_id: int, html: str, markup: dict | None = None) -> None:
        self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=html,
                  parse_mode="HTML", reply_markup=markup or {"inline_keyboard": []})

    def answer(self, callback_id: str, text: str | None = None) -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except (TelegramError, OSError):
            pass  # the button spinner times out on its own

    def react(self, chat_id: int, message_id: int, emoji: str) -> bool:
        try:
            self.call("setMessageReaction", chat_id=chat_id, message_id=message_id,
                      reaction=[{"type": "emoji", "emoji": emoji}])
            return True
        except (TelegramError, OSError):
            return False

    def updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        return self.call("getUpdates", _timeout=timeout + 10, offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query"])

    def download(self, file_id: str, dest: Path) -> Path:
        info = self.call("getFile", file_id=file_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(f"{API}/file/bot{self.token}/{info['file_path']}", timeout=120) as resp:
            dest.write_bytes(resp.read())
        return dest


def keyboard(*rows: list[tuple[str, str]]) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows if row]}
