"""The Telegram side: your messages go to the active pane, and agents that need you come back."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import threading
import time
from datetime import datetime
from pathlib import Path

from rexgram import agents, config, presence, rex, state
from rexgram import format as fmt
from rexgram.agents import Agent
from rexgram.telegram import Telegram, TelegramError, keyboard

log = logging.getLogger("rexgram")

STALE_SECONDS = 300  # ignore messages that queued up while the bot was down
SCREEN_LINES = 40
ALERT_LINES = 18

COMMANDS = [
    ("ls", "Agents: tap one to make it active"),
    ("cap", "Show the active pane's screen"),
    ("esc", "Interrupt the active agent"),
    ("key", "Press keys: /key 1, /key enter, /key ctrl+c"),
    ("new", "Start an agent: /new [dir] <task>"),
    ("mute", "Stop all messages until /unmute"),
    ("unmute", "Resume messages"),
    ("help", "How rexgram works"),
]

HELP = """Messages go to the <b>active pane</b>: whichever agent you last focused in Rex, or picked with /ls. Switching here moves Rex's focus too.

Reply to an agent's message to talk to that agent. <code>&gt;3 text</code> sends to agent 3 once.
Photos and files are saved and their path is pasted in.

At the Mac, you only hear back about what you sent from here. After {away} of no keyboard or mouse input, the active agent's replies come through too, along with any agent waiting on you.

/ls · /cap · /esc · /key 1 · /new [dir] task · /mute"""

KEY_ALIASES = {"esc": "escape", "return": "enter", "ret": "enter", "bs": "backspace", "del": "delete"}


class UserError(Exception):
    """A problem to report back to the chat, not a crash."""


def _uuid(block_id: str) -> str:
    return block_id.removeprefix("block:")


def _home(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


class Bot:
    def __init__(self, cfg: config.Config):
        self.cfg = cfg
        self.tg = Telegram(cfg.token)
        self.chat = cfg.chat_id
        self.apps = agents.AGENT_APPS | cfg.extra_apps
        self.sessions: dict[str, str] = {}  # block_id -> session_id
        self.states: dict[str, str] = {}  # block_id -> last reported status
        self.focus: dict[str, str] = {}  # session_id -> focused block_id
        self.stopping = threading.Event()

    # Sending

    def send(self, html: str, **kwargs) -> int:
        return self.tg.send(self.chat, html, **kwargs)

    def send_about(self, session_id: str, block_id: str, html: str, **kwargs) -> int:
        """Send a message that replies are routed back from."""
        mid = self.send(html, **kwargs)
        state.remember_messages([mid], session_id, block_id)
        return mid

    def ack(self, msg: dict, emoji: str = "👌") -> None:
        self.tg.react(self.chat, msg["message_id"], emoji)

    # Panes

    def agents(self) -> list[Agent]:
        found = agents.list_agents(self.apps)
        self.sessions.update({a.block_id: a.session_id for a in found})
        return found

    def session_of(self, block_id: str) -> str:
        if block_id not in self.sessions:
            session_id = rex.find_session(block_id)
            if not session_id:
                raise UserError("That pane is gone.")
            self.sessions[block_id] = session_id
        return self.sessions[block_id]

    def name(self, session_id: str, block_id: str) -> str:
        agent = agents.describe(session_id, block_id, self.apps)
        return agent.name if agent else f"pane {block_id[-6:]}"

    def resolve(self, query: str) -> tuple[str, str]:
        """Turn "3", "ai_ofm", or a block ID suffix into (session_id, block_id)."""
        query = query.strip()
        if query.isdigit():
            listed = state.load().get("listed") or [[a.session_id, a.block_id] for a in self.agents()]
            n = int(query)
            if not 1 <= n <= len(listed):
                raise UserError(f"No agent {n}. Send /ls to see the list.")
            return listed[n - 1][0], listed[n - 1][1]
        q = query.lower()
        found = self.agents()
        exact = [a for a in found if q in (a.short, a.block_label.lower(), a.session_label.lower(), a.project.lower())
                 or a.block_id.endswith(q)]
        loose = exact or [a for a in found if q in f"{a.project} {a.title} {a.session_label}".lower()]
        if len(loose) == 1:
            return loose[0].session_id, loose[0].block_id
        if len(loose) > 1:
            names = "\n".join(f"• {fmt.escape(a.name)} — {fmt.escape(a.title[:40])}" for a in loose[:10])
            raise UserError(f"“{fmt.escape(query)}” matches several agents:\n{names}")
        raise UserError(f"No agent matches “{fmt.escape(query)}”. Send /ls to see them.")

    def active(self) -> tuple[str, str] | None:
        return state.active(state.load())

    def require_active(self) -> tuple[str, str]:
        active = self.active()
        if not active:
            raise UserError("No active agent yet. Send /ls and tap one.")
        return active

    def set_active(self, session_id: str, block_id: str) -> None:
        with state.update() as data:
            data["active"] = [session_id, block_id]
        self.sessions[block_id] = session_id

    def switch(self, session_id: str, block_id: str) -> None:
        """Make a pane active here and focused in Rex."""
        self.set_active(session_id, block_id)
        self.focus[session_id] = block_id
        try:
            rex.focus(session_id, block_id)
        except rex.RexError as e:
            log.warning("couldn't focus %s in Rex: %s", block_id, e)

    def follow_focus(self, session_id: str) -> None:
        """When you focus a different agent in Rex, make it the active pane."""
        try:
            block_id = rex.focused(session_id)
        except rex.RexError:
            return
        if not block_id or self.focus.get(session_id) == block_id:
            return
        self.focus[session_id] = block_id
        active = self.active()
        if active and active[1] == block_id:
            return
        # Only agents: focusing a shell or a diff tab shouldn't redirect your messages into it.
        if agents.describe(session_id, block_id, self.apps):
            log.info("focus moved to %s; it is now the active pane", block_id)
            self.set_active(session_id, block_id)

    # Talking to panes

    def deliver(self, session_id: str, block_id: str, text: str) -> None:
        rex.paste(session_id, block_id, text)
        # Give the agent a moment to take the paste in before submitting it.
        time.sleep(0.2 + min(len(text) / 20000, 1.0))
        rex.send_keys(session_id, block_id, "enter")
        with state.update() as data:
            data.setdefault("awaiting", {})[block_id] = [time.time(), text]

    def screen(self, session_id: str, block_id: str, lines: int = SCREEN_LINES) -> str:
        return fmt.tail(fmt.clean_screen(rex.capture(session_id, block_id)), lines)

    def send_screen(self, session_id: str, block_id: str, lines: int = SCREEN_LINES) -> None:
        name = fmt.escape(self.name(session_id, block_id))
        body = fmt.pre(self.screen(session_id, block_id, lines), fmt.LIMIT - len(name) - 20)
        self.send_about(session_id, block_id, f"<b>{name}</b>\n{body}")

    # Telegram updates

    def run(self) -> None:
        try:
            self.tg.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
        except (TelegramError, OSError) as e:
            log.warning("could not set the command menu: %s", e)
        threading.Thread(target=self.watch_events, name="events", daemon=True).start()
        threading.Thread(target=self.show_typing, name="typing", daemon=True).start()
        log.info("rexgram is running; chat %s", self.chat)
        offset = None
        while not self.stopping.is_set():
            try:
                updates = self.tg.updates(offset)
            except TelegramError as e:
                if e.code == 409:
                    log.error("another process is reading this bot's updates; retrying in 30s")
                    time.sleep(30)
                else:
                    log.error("getUpdates failed: %s", e)
                    time.sleep(5)
                continue
            except OSError as e:
                log.warning("network error: %s", e)
                time.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self.handle(update)
                except Exception:
                    log.exception("handling update %s failed", update.get("update_id"))

    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            return self.on_button(update["callback_query"])
        msg = update.get("message")
        if not msg:
            return
        if not self.cfg.authorized(msg["chat"]["id"], msg.get("from", {}).get("id")):
            log.warning("ignoring message from chat %s user %s", msg["chat"]["id"], msg.get("from", {}).get("id"))
            return
        if time.time() - msg.get("date", 0) > STALE_SECONDS:
            log.info("skipping a message that is %ds old", time.time() - msg.get("date", 0))
            return
        try:
            text = msg.get("text") or ""
            if text.startswith("/"):
                self.on_command(msg, text)
            else:
                self.on_input(msg)
        except UserError as e:
            self.send(f"⚠️ {e}", reply_to=msg["message_id"])
        except rex.RexError as e:
            self.send(f"⚠️ rex: {fmt.escape(str(e))}", reply_to=msg["message_id"])

    def on_input(self, msg: dict) -> None:
        text = msg.get("text") or msg.get("caption") or ""
        to = None
        if text.startswith(">"):
            head, _, text = text[1:].partition(" ")
            to = self.resolve(head)
        elif reply := msg.get("reply_to_message"):
            to = state.message_pane(state.load(), reply["message_id"])
        to = to or self.active()
        if not to:
            self.send_list("Which agent? Tap one, then send that again.")
            return
        if path := self.download(msg):
            text = f"{text} {path}".strip()
        if not text.strip():
            return
        self.deliver(*to, text)
        self.ack(msg, "👀")
        with state.update() as data:
            changed = data.get("last_to") != to[1]
            data["last_to"] = to[1]
        if changed:  # say where it went only when that changes
            self.send(f"→ {fmt.escape(self.name(*to))}", silent=True)

    def download(self, msg: dict) -> Path | None:
        if msg.get("photo"):
            file, name = msg["photo"][-1], "photo.jpg"
        else:
            kind = next((k for k in ("document", "video", "audio", "voice", "animation") if msg.get(k)), None)
            if not kind:
                return None
            file = msg[kind]
            name = file.get("file_name") or f"{kind}{'.ogg' if kind == 'voice' else ''}"
        safe = re.sub(r"[^\w.-]+", "_", name)
        dest = self.cfg.files_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{safe}"
        try:
            return self.tg.download(file["file_id"], dest)
        except (TelegramError, OSError) as e:
            raise UserError(f"Couldn't download that file: {fmt.escape(str(e))}") from None

    # Commands

    def on_command(self, msg: dict, text: str) -> None:
        command, _, arg = text.partition(" ")
        command = command[1:].split("@")[0].lower()
        arg = arg.strip()
        handler = {
            "start": self.cmd_help, "help": self.cmd_help,
            "ls": self.cmd_ls, "status": self.cmd_ls,
            "t": self.cmd_switch, "to": self.cmd_switch,
            "cap": self.cmd_cap, "screen": self.cmd_cap,
            "esc": self.cmd_esc, "stop": self.cmd_esc,
            "key": self.cmd_key, "keys": self.cmd_key,
            "new": self.cmd_new,
            "close": self.cmd_close,
            "mute": lambda m, a: self.cmd_mute(m, True), "unmute": lambda m, a: self.cmd_mute(m, False),
        }.get(command)
        if not handler:
            raise UserError(f"Unknown command /{fmt.escape(command)}. Send /help.")
        handler(msg, arg)

    def cmd_help(self, msg: dict, arg: str) -> None:
        minutes = max(1, round(self.cfg.away_after / 60))
        self.send(HELP.format(away=f"{minutes} minute{'s' if minutes > 1 else ''}"))

    def render_list(self, found: list[Agent], active: tuple[str, str] | None) -> tuple[str, dict]:
        lines = []
        many_sessions = len({a.session_id for a in found}) > 1
        session = None
        for n, a in enumerate(found, 1):
            if many_sessions and a.session_label != session:
                session = a.session_label
                lines.append(f"\n<b>{fmt.escape(session or a.session_id[-6:])}</b>")
            title = f" — {fmt.escape(a.title[:42])}" if a.title else ""
            row = f"{n}. {a.icon} {fmt.escape(a.name)}{title}"
            lines.append(f"<u>{row}</u> ◀︎" if active and active[1] == a.block_id else row)
        buttons = [(f"{'• ' if active and active[1] == a.block_id else ''}{n}", f"t|{_uuid(a.block_id)}")
                   for n, a in enumerate(found, 1)]
        return "\n".join(lines).strip(), keyboard(*[buttons[i:i + 6] for i in range(0, len(buttons), 6)])

    def send_list(self, intro: str = "") -> None:
        found = self.agents()
        with state.update() as data:
            data["listed"] = [[a.session_id, a.block_id] for a in found]
            active = state.active(data)
        if not found:
            self.send("No agents are running in Rex.")
            return
        text, markup = self.render_list(found, active)
        if state.load().get("muted"):
            text += "\n\n🔇 Muted. /unmute to resume."
        self.send(f"{intro}\n\n{text}" if intro else text, markup=markup)

    def cmd_ls(self, msg: dict, arg: str) -> None:
        self.send_list()

    def cmd_switch(self, msg: dict, arg: str) -> None:
        if not arg:
            return self.send_list()
        self.switch(*self.resolve(arg))
        self.ack(msg)

    def cmd_cap(self, msg: dict, arg: str) -> None:
        words = arg.split()
        lines = SCREEN_LINES
        if len(words) == 2 and words[1].isdigit():
            lines = min(int(words.pop()), 200)
        pane = self.resolve(words[0]) if words else self.require_active()
        self.send_screen(*pane, lines)

    def cmd_key(self, msg: dict, arg: str) -> None:
        if not arg:
            raise UserError("Which keys? For example /key 1, /key enter, /key ctrl+c.")
        rex.send_keys(*self.require_active(), *[KEY_ALIASES.get(k.lower(), k) for k in arg.split()])
        self.ack(msg)

    def cmd_esc(self, msg: dict, arg: str) -> None:
        rex.send_keys(*(self.resolve(arg) if arg else self.require_active()), "escape")
        self.ack(msg)

    def cmd_new(self, msg: dict, arg: str) -> None:
        cwd, task = None, arg
        first, _, rest = arg.partition(" ")
        if first.startswith(("/", "~", ".")):
            candidate = Path(first).expanduser()
            if not candidate.is_dir():
                raise UserError(f"No such directory: {fmt.escape(first)}")
            cwd, task = str(candidate.resolve()), rest.strip()
        active = self.active()
        sessions = rex.sessions()
        if not sessions:
            raise UserError("Rex has no sessions to open a tab in.")
        in_active = active and any(s["session_id"] == active[0] for s in sessions)
        session_id = active[0] if in_active else sessions[0]["session_id"]
        cwd = cwd or self._cwd(active) or os.path.expanduser("~")
        argv = shlex.split(self.cfg.agent_cmd) + ([task] if task else [])
        block_id = rex.new_window(session_id, "tg: " + (" ".join(task.split()[:4]) or argv[0]), cwd, argv)
        self.switch(session_id, block_id)
        if task:
            with state.update() as data:
                data.setdefault("awaiting", {})[block_id] = [time.time(), task]
        self.send_about(session_id, block_id, f"🚀 {fmt.escape(argv[0])} in <code>{fmt.escape(_home(cwd))}</code>")
        # Agents can open with a prompt that reports no status, like Claude asking to trust a folder.
        threading.Timer(6, self._check_start, args=(session_id, block_id)).start()

    def _cwd(self, pane: tuple[str, str] | None) -> str | None:
        try:
            return (rex.call(*pane, "process").get("foreground") or {}).get("cwd") if pane else None
        except rex.RexError:
            return None

    def _check_start(self, session_id: str, block_id: str) -> None:
        try:
            if rex.call(session_id, block_id, "program_status").get("records"):
                return  # it started and is reporting status, so it isn't stuck on a prompt
            screen = self.screen(session_id, block_id, 16)
            uid = _uuid(block_id)
            markup = keyboard([("↑", f"k|{uid}|up"), ("↓", f"k|{uid}|down"), ("Enter", f"k|{uid}|enter"),
                               ("Esc", f"k|{uid}|escape")])
            self.send_about(session_id, block_id, f"It's asking something:\n{fmt.pre(screen)}", markup=markup)
        except (rex.RexError, TelegramError, OSError) as e:
            log.warning("couldn't check on the new agent: %s", e)

    def cmd_close(self, msg: dict, arg: str) -> None:
        session_id, block_id = self.resolve(arg) if arg else self.require_active()
        self.send(f"Close <b>{fmt.escape(self.name(session_id, block_id))}</b>? Its program will be ended.",
                  markup=keyboard([("Close it", f"X|{_uuid(block_id)}"), ("Cancel", "cancel")]))

    def cmd_mute(self, msg: dict, muted: bool) -> None:
        with state.update() as data:
            data["muted"] = muted
        self.ack(msg, "🙊" if muted else "👌")

    # Buttons

    def on_button(self, cb: dict) -> None:
        message = cb.get("message") or {}
        if not self.cfg.authorized(message.get("chat", {}).get("id"), cb.get("from", {}).get("id")):
            return self.tg.answer(cb["id"])
        action, _, rest = cb.get("data", "").partition("|")
        mid = message.get("message_id")
        if action == "cancel":
            self.tg.edit(self.chat, mid, "Cancelled.")
            return self.tg.answer(cb["id"])
        uid, _, arg = rest.partition("|")
        block_id = f"block:{uid}"
        try:
            session_id = self.session_of(block_id)
            if action == "t":
                self.switch(session_id, block_id)
                self.tg.answer(cb["id"], self.name(session_id, block_id))
                listed = [tuple(p) for p in state.load().get("listed", [])]
                found = [a for a in self.agents() if (a.session_id, a.block_id) in set(listed)]
                found.sort(key=lambda a: listed.index((a.session_id, a.block_id)))
                text, markup = self.render_list(found, (session_id, block_id))
                self.tg.edit(self.chat, mid, text, markup)
            elif action in ("k", "o"):
                rex.send_keys(session_id, block_id, arg)
                self.tg.answer(cb["id"])
                if action == "o":  # an answered prompt: show the choice and retire the buttons
                    label = next((b["text"] for row in message.get("reply_markup", {}).get("inline_keyboard", [])
                                  for b in row if b.get("callback_data") == cb["data"]), arg)
                    head = (message.get("text") or "").split("\n")[0]
                    self.tg.edit(self.chat, mid, f"{fmt.escape(head)}\n✓ {fmt.escape(label)}")
            elif action == "s":
                self.tg.answer(cb["id"])
                self.send_screen(session_id, block_id)
            elif action == "X":
                name = self.name(session_id, block_id)
                rex.close(session_id, block_id)
                self.tg.edit(self.chat, mid, f"Closed <b>{fmt.escape(name)}</b>.")
                self.tg.answer(cb["id"])
            else:
                self.tg.answer(cb["id"])
        except (UserError, rex.RexError) as e:
            self.tg.answer(cb["id"], f"⚠️ {e}")
        except TelegramError as e:
            if "not modified" not in str(e):
                raise

    # Rex events

    def watch_events(self) -> None:
        backoff = 1
        while not self.stopping.is_set():
            try:
                self.states = {a.block_id: a.state for a in self.agents()}
                self.focus = {s["session_id"]: rex.focused(s["session_id"]) for s in rex.sessions()}
            except rex.RexError as e:
                log.warning("can't reach Rex: %s", e)
            started = time.monotonic()
            proc = rex.events()
            for line in proc.stdout:
                try:
                    self.on_event(json.loads(line))
                except ValueError:
                    continue
                except Exception:
                    log.exception("handling a Rex event failed")
            proc.wait()
            if time.monotonic() - started > 60:
                backoff = 1
            log.warning("Rex event stream ended; reconnecting in %ds", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def on_event(self, event: dict) -> None:
        if event.get("type") != "session_event":
            return
        payload = event.get("payload") or {}
        session_id = payload.get("session_id")
        inner = payload.get("event") or {}
        if inner.get("type") == "session_view_changed" and session_id:
            threading.Thread(target=self._safely, args=(self.follow_focus, session_id), daemon=True).start()
            return
        if inner.get("type") != "block_event":
            return
        block_event = inner.get("payload") or {}
        if block_event.get("name") != "program_status_changed":
            return
        status = block_event.get("payload") or {}
        record = status.get("record", status)
        if status.get("id") or record.get("id"):
            return  # a sub-task's record, not the agent's own status
        if agents.normalize_app(record.get("effective_app") or record.get("app") or "") not in self.apps:
            return
        block_id = block_event.get("block_id")
        new = record.get("state", "idle")
        old = self.states.get(block_id)
        self.states[block_id] = new
        if not (session_id and block_id) or new == old:
            return
        self.sessions[block_id] = session_id
        if new == "blocked":
            threading.Thread(target=self._safely, args=(self.alert_blocked, session_id, block_id, record.get("kind", "")),
                             daemon=True).start()

    def _safely(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("%s failed", fn.__name__)

    def show_typing(self) -> None:
        """Show "typing…" while an agent you messaged from Telegram is working."""
        while not self.stopping.wait(4):
            data = state.load()
            if any(self.states.get(b) == "working" and state.awaiting(data, b) for b in list(data.get("awaiting", {}))):
                try:
                    self.tg.call("sendChatAction", chat_id=self.chat, action="typing")
                except (TelegramError, OSError):
                    pass

    def alert_blocked(self, session_id: str, block_id: str, kind: str) -> None:
        data = state.load()
        if data.get("muted"):
            return
        if not state.awaiting(data, block_id) and not presence.away(self.cfg):
            return
        time.sleep(1)  # let the prompt finish drawing
        if self.states.get(block_id) != "blocked":
            return
        try:
            screen = self.screen(session_id, block_id, ALERT_LINES)
        except rex.RexError:
            screen = ""
        uid = _uuid(block_id)
        options = fmt.prompt_options(screen)
        rows = [[(f"{n}. {label.split(' · ')[0][:48]}", f"o|{uid}|{n}")] for n, label in options[:6]]
        rows.append([("Esc", f"o|{uid}|escape"), ("📺 Screen", f"s|{uid}")] if options else
                    [("Enter", f"o|{uid}|enter"), ("Esc", f"o|{uid}|escape")])
        waiting = {"permission": "permission", "question": "an answer", "auth": "a password"}.get(kind, "you")
        name = fmt.escape(self.name(session_id, block_id))
        self.send_about(session_id, block_id, f"❗ <b>{name}</b> needs {waiting}\n{fmt.pre(screen, 3000)}",
                        markup=keyboard(*rows))


def serve() -> int:
    cfg = config.load()
    if not cfg.ready:
        log.error("no bot token or chat ID; run `rexgram setup` first")
        return 1
    try:
        Bot(cfg).run()
    except KeyboardInterrupt:
        pass
    return 0
