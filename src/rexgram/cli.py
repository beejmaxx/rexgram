"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
import time

from rexgram import __version__, agents, config, install
from rexgram.telegram import Telegram, TelegramError


def cmd_serve(args) -> int:
    from rexgram import bot

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return bot.serve()


def cmd_hook(args) -> int:
    from rexgram import hooks

    return hooks.run(args.agent, args.payload)


def cmd_ls(args) -> int:
    cfg = config.load()
    for n, a in enumerate(agents.list_agents(agents.AGENT_APPS | cfg.extra_apps), 1):
        print(f"{n:>3}. {a.icon} {a.name:<34} {a.session_label:<22} {a.short}  {a.title[:50]}")
    return 0


def discover_chat(tg: Telegram, wait: int = 120) -> int:
    print("Open Telegram and send any message to your bot…", flush=True)
    offset = None
    deadline = time.time() + wait
    while time.time() < deadline:
        for update in tg.updates(offset, timeout=20):
            offset = update["update_id"] + 1
            if msg := update.get("message"):
                tg.updates(offset, timeout=0)  # acknowledge it so the bot doesn't see it later
                return msg["chat"]["id"]
    raise SystemExit("No message arrived. Run `rexgram setup` again when you're ready.")


def cmd_setup(args) -> int:
    if args.remove:
        for d in install.claude_dirs():
            print("claude:", install.claude_hook(d, remove=True))
        print("codex: ", install.codex_hook(remove=True))
        print("service:", install.service("uninstall"))
        return 0

    cfg = config.load()
    token = args.token or cfg.token
    if not token:
        token = input("Bot token from @BotFather: ").strip()
    tg = Telegram(token)
    try:
        me = tg.call("getMe")
    except (TelegramError, OSError) as e:
        print(f"Telegram rejected that token or couldn't be reached: {e}", file=sys.stderr)
        return 1
    print(f"bot:    @{me['username']}")
    chat_id = args.chat_id or cfg.chat_id
    if not chat_id:
        chat_id = discover_chat(tg)
        print(f"chat:   {chat_id}")
    print("config:", config.write({"REXGRAM_BOT_TOKEN": token, "REXGRAM_CHAT_ID": str(chat_id)}))

    if not args.no_hooks:
        for d in install.claude_dirs():
            print("claude:", install.claude_hook(d))
        print("codex: ", install.codex_hook())
    if not args.no_service:
        print("service:", install.service("install"))
    try:
        Telegram(token).send(int(chat_id), "👋 <b>rexgram</b> is connected. Send /ls to see your agents.")
    except (TelegramError, OSError) as e:
        print(f"Couldn't send a test message: {e}", file=sys.stderr)
    return 0


def cmd_service(args) -> int:
    print(install.service(args.action))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rexgram", description="Talk to the coding agents in your Rex panes from Telegram.")
    parser.add_argument("--version", action="version", version=f"rexgram {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the bot in the foreground").set_defaults(func=cmd_serve)

    hook = sub.add_parser("hook", help="forward an agent's reply (run by the agent)")
    hook.add_argument("agent", choices=["claude", "codex"])
    hook.add_argument("payload", nargs="?", help="Codex's notify JSON")
    hook.set_defaults(func=cmd_hook)

    sub.add_parser("ls", help="list the agents rexgram can see").set_defaults(func=cmd_ls)

    setup = sub.add_parser("setup", help="configure the bot, install hooks, and start the service")
    setup.add_argument("--token", help="bot token from @BotFather")
    setup.add_argument("--chat-id", type=int, help="the chat to accept commands from")
    setup.add_argument("--no-hooks", action="store_true", help="don't touch Claude Code or Codex settings")
    setup.add_argument("--no-service", action="store_true", help="don't install the background service")
    setup.add_argument("--remove", action="store_true", help="remove the hooks and the service")
    setup.set_defaults(func=cmd_setup)

    svc = sub.add_parser("service", help="manage the background service")
    svc.add_argument("action", choices=["install", "uninstall", "restart", "status"])
    svc.set_defaults(func=cmd_service)

    args = parser.parse_args(argv)
    return args.func(args)
