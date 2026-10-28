# rexgram

Talk to the coding agents in your [Rex](https://www.superlogical.com/rex) panes from Telegram.

You step away from your desk with six agents running. One finishes, one is waiting on a question, and you have a follow-up for another. With rexgram, you handle all of that from your phone, and your phone stays quiet while you're at your desk.

- **Messages go to the active pane.** That's whichever agent you last focused in Rex. Tap another agent in `/ls` and Rex's focus moves with you, so you're on the right pane when you get back.
- **Replies come back.** When an agent you messaged finishes its turn, its answer arrives in Telegram, with Markdown and code blocks intact. Reply to any agent message to talk to that agent.
- **It knows if you're at your desk.** While you're using the Mac, you only hear back about what you sent from Telegram. After a few idle minutes, the active agent's replies come through too, along with any agent that is blocked on you.
- **Prompts become buttons.** When an agent stops for a permission prompt or a question, you get its screen and its actual choices as buttons (`1. Yes`, `2. Yes, and don't ask again`, `3. No`). Tap one to answer.
- **"typing…" while it works.** Telegram shows the typing indicator while an agent you messaged is busy.
- **Photos and files work.** They're saved locally and their path is pasted into the agent.

It works with Claude Code and Codex. It also detects pi, opencode, gemini, aider, amp, goose, crush and cursor-agent, so you can list them, switch to them, message them and get blocked alerts from them. Only Claude Code and Codex have hooks that forward replies.

## How it works

rexgram uses only what Rex already provides:

| Need | Rex feature |
| --- | --- |
| Which panes run agents | `rex block call process` (foreground program) |
| Working, done, or blocked | `program_status`, which agents report with OSC 7501 |
| Live changes | `rex events --all --json` (`program_status_changed`, `session_view_changed`) |
| Typing into an agent | a `paste` event, then `rex send-key enter` |
| Reading a screen | `rex capture` |
| Switching panes | `rex focus`, plus the app's `session.select` action |

When a turn ends, a Claude Code Stop hook or Codex's `notify` program runs `rexgram hook`. It reads `REX_BLOCK` to tell which pane the reply came from. Everything is Python standard library: no dependencies, and no server to host. It long-polls Telegram, so nothing needs to be exposed to the internet.

## Install

```sh
uv tool install git+https://github.com/beejmaxx/rexgram   # or: pipx install git+https://github.com/beejmaxx/rexgram
rexgram setup
```

`rexgram setup`:

1. asks for a bot token (create a bot with [@BotFather](https://t.me/BotFather)),
2. waits for you to message the bot so it learns your chat ID,
3. adds a Stop hook to Claude Code (`$CLAUDE_CONFIG_DIR` and `~/.claude`),
4. sets Codex's `notify` if it isn't already set (if it is, it prints the line to add to your chain),
5. starts the bot as a launchd agent (macOS) or a systemd user service (Linux).

rexgram is developed and tested on macOS. On Linux, everything except the idle-time check should work, and rexgram treats you as always away there. The systemd service is written but hasn't been tested yet; reports are welcome.

Undo all of it with `rexgram setup --remove`.

## Using it

| You send | It does |
| --- | --- |
| any text | sends it to the active pane |
| a reply to an agent's message | sends it to that agent |
| `>3 text` | sends it to agent 3 once |
| a photo or file | saves it and pastes its path, with your caption |
| `/ls` | lists agents; tap a number to make it active |
| `/cap [agent] [lines]` | shows the end of a screen |
| `/esc` | interrupts the active agent |
| `/key 1`, `/key enter`, `/key ctrl+c` | presses keys in the active pane |
| `/new [dir] task` | starts an agent in a new tab and makes it active |
| `/mute`, `/unmute` | pauses everything rexgram sends unprompted |

Status icons: ⚙️ working · ❗ blocked on you · ✅ done · 💤 idle · ❌ error.

## Configuration

Settings live in `~/.config/rexgram/config.env`. Environment variables with the same names override them.

| Variable | Default | Meaning |
| --- | --- | --- |
| `REXGRAM_BOT_TOKEN` | | bot token (`TELEGRAM_BOT_TOKEN` also works, so you can reuse an existing env file) |
| `REXGRAM_CHAT_ID` | | the only chat rexgram listens to |
| `REXGRAM_ALLOWED_USERS` | | comma-separated user IDs, for group chats |
| `REXGRAM_NOTIFY` | `auto` | `auto` (depends on whether you're at the Mac), `always`, or `asked` (only answers to what you sent) |
| `REXGRAM_AWAY_AFTER` | `180` | seconds without keyboard or mouse input before you count as away |
| `REXGRAM_AGENT_CMD` | `claude` | what `/new` runs, e.g. `claude --dangerously-skip-permissions` |
| `REXGRAM_AGENTS` | | extra program names to treat as agents |
| `REXGRAM_FILES_DIR` | `~/.local/state/rexgram/files` | where photos and files are saved |
| `REXGRAM_ENV_FILE` | | read config from this file instead |

Telegram is reached through `HTTPS_PROXY` or, on macOS, the system proxy, if you have one.

Logs are in `~/Library/Logs/rexgram.log` (macOS) or `~/.local/state/rexgram/rexgram.log`. Hook errors go to `~/.local/state/rexgram/hook.log`. `rexgram service status|restart|uninstall` manages the background service. `rexgram ls` shows what rexgram sees.

## Security

Whoever controls the bot's chat can type into your agents, and therefore run commands on your machine. rexgram only accepts messages from `REXGRAM_CHAT_ID` (and `REXGRAM_ALLOWED_USERS`, if set). It ignores messages that were queued more than five minutes before it saw them. Keep the bot token secret, and don't add the bot to groups you don't control.

rexgram only types into agent panes, never into plain shells, and it never starts a Rex server.

## Development

```sh
git clone https://github.com/beejmaxx/rexgram && cd rexgram
uv tool install -e .
python -m unittest discover -s tests
```

## License

MIT
