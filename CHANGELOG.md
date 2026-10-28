# Changelog

## 0.1.0 — 2026-10-28

First release.

- Messages go to the active pane: the agent you last focused in Rex, or picked with `/ls`. Switching from Telegram moves Rex's focus too.
- Replies come back through a Claude Code Stop hook and Codex's `notify`. Only the turn that answers your Telegram message is forwarded.
- Presence-aware: after a few idle minutes, the active agent's replies and any agent blocked on you come through too.
- Blocked prompts arrive with their real choices as buttons.
- "typing…" while an agent you messaged is working.
- Photos and files are saved and their path is pasted in.
- `/ls`, `/cap`, `/esc`, `/key`, `/new`, `/close`, `/mute`.
- `rexgram setup` installs the hooks and a launchd or systemd service. `rexgram setup --remove` undoes it.
