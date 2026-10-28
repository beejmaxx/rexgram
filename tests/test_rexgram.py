import json
import os
import time
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from rexgram import agents, config, format as fmt, hooks, install, presence, state
from rexgram.bot import Bot


class AgentDetection(unittest.TestCase):
    def test_app_from_process(self):
        apps = agents.AGENT_APPS
        self.assertEqual(agents.app_from_process({"argv0": "claude"}, apps), "claude")
        self.assertEqual(agents.app_from_process({"argv0": "-codex", "path": "/x/codex"}, apps), "codex")
        self.assertEqual(
            agents.app_from_process({"argv0": "2.1.3", "path": "/Users/a/.local/share/claude/versions/2.1.3"}, apps),
            "claude",
        )
        self.assertIsNone(agents.app_from_process({"argv0": "-fish", "path": "/opt/homebrew/bin/fish"}, apps))

    def test_status_record_prefers_foreground_owner_and_root(self):
        records = [
            {"state": "done", "owner": {"pid": 1}, "updated_at": "2026-01-02"},
            {"state": "working", "owner": {"pid": 2}, "updated_at": "2026-01-03"},
            {"id": "sub", "state": "blocked", "owner": {"pid": 2}, "updated_at": "2026-01-04"},
        ]
        self.assertEqual(agents.status_record(records, 2)["state"], "working")
        self.assertEqual(agents.status_record(records, 3), {})

    def test_clean_title(self):
        self.assertEqual(agents.clean_title("⠹ Prevent leaks | img-gen", "img-gen"), "Prevent leaks")
        self.assertEqual(agents.clean_title("✳ Claude Code"), "")
        self.assertEqual(agents.clean_title("Fix login"), "Fix login")


class Formatting(unittest.TestCase):
    def test_markdown_to_html(self):
        html = fmt.markdown_to_html("# Done\nUse `a<b>` and **bold** [x](https://e.com?a=1&b=2)\n```py\nif a < b:\n```")
        self.assertIn("<b>Done</b>", html)
        self.assertIn("<code>a&lt;b&gt;</code>", html)
        self.assertIn("<b>bold</b>", html)
        self.assertIn('<a href="https://e.com?a=1&amp;b=2">x</a>', html)
        self.assertIn("<pre>if a &lt; b:</pre>", html)

    def test_split_keeps_fences_balanced(self):
        text = "intro\n```\n" + "\n".join(f"line {i}" for i in range(400)) + "\n```\nafter"
        chunks = fmt.split_markdown(text, 500)
        self.assertGreater(len(chunks), 3)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 520)
            self.assertEqual(chunk.count("```") % 2, 0, chunk)
        self.assertTrue(chunks[-1].endswith("after"))

    def test_split_long_line(self):
        chunks = fmt.split_markdown("x" * 1200, 500)
        self.assertEqual("".join(chunks), "x" * 1200)

    def test_clean_screen(self):
        raw = "⏺ Done\n\n\n────────\n❯ \n────────\n  [Opus] agent\n"
        self.assertEqual(fmt.clean_screen(raw), "⏺ Done\n\n❯\n  [Opus] agent")

    def test_prompt_options(self):
        screen = (
            "Here are steps:\n1. old\n2. list\n Bash command\n   date\n Do you want to proceed?\n"
            " ❯ 1. Yes\n   2. Yes, and don't ask again for date commands\n   3. No, and tell Claude what to do (esc)\n"
        )
        self.assertEqual([n for n, _ in fmt.prompt_options(screen)], ["1", "2", "3"])
        self.assertEqual(fmt.prompt_options(screen)[0][1], "Yes")
        self.assertEqual(fmt.prompt_options("nothing to choose\n"), [])

    def test_tail(self):
        self.assertEqual(fmt.tail("a\nb\nc\n\n", 2), "b\nc")


class Isolated(unittest.TestCase):
    """Point config and state at a temporary directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = {"XDG_STATE_HOME": self.tmp.name, "XDG_CONFIG_HOME": self.tmp.name, "HOME": self.tmp.name}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        for key in [k for k in os.environ if k.startswith("REXGRAM_")]:
            del os.environ[key]
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)


class Config(Isolated):
    def test_env_file_and_overrides(self):
        path = config.config_path()
        path.parent.mkdir(parents=True)
        path.write_text('# comment\nexport TELEGRAM_BOT_TOKEN="abc"\nREXGRAM_CHAT_ID=42\nREXGRAM_NOTIFY=bogus\n')
        cfg = config.load()
        self.assertEqual((cfg.token, cfg.chat_id, cfg.notify), ("abc", 42, "auto"))
        with mock.patch.dict(os.environ, {"REXGRAM_CHAT_ID": "7", "REXGRAM_ALLOWED_USERS": "1, 2"}):
            cfg = config.load()
        self.assertEqual((cfg.chat_id, cfg.allowed_users), (7, {1, 2}))
        self.assertTrue(cfg.authorized(7, 1))
        self.assertFalse(cfg.authorized(7, 3))
        self.assertFalse(cfg.authorized(8, 1))

    def test_write_merges(self):
        config.write({"REXGRAM_BOT_TOKEN": "a", "REXGRAM_CHAT_ID": "1"})
        config.write({"REXGRAM_CHAT_ID": "2"})
        text = config.config_path().read_text()
        self.assertIn("REXGRAM_BOT_TOKEN=a", text)
        self.assertIn("REXGRAM_CHAT_ID=2", text)
        self.assertNotIn("REXGRAM_CHAT_ID=1", text)
        self.assertEqual(config.config_path().stat().st_mode & 0o777, 0o600)


class State(Isolated):
    def test_messages_are_capped(self):
        with mock.patch.object(state, "MAX_MESSAGES", 3):
            state.remember_messages([1, 2, 3, 4, 5], "s", "b")
        data = state.load()
        self.assertEqual(list(data["messages"]), ["3", "4", "5"])
        self.assertEqual(state.message_pane(data, 5), ("s", "b"))
        self.assertIsNone(state.message_pane(data, 1))


class Hooks(Isolated):
    def test_claude_reply_from_transcript(self):
        path = Path(self.tmp.name) / "t.jsonl"
        entries = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "final answer"}]}},
        ]
        path.write_text("\n".join(json.dumps(e) for e in entries))
        self.assertEqual(hooks.claude_reply({"transcript_path": str(path)}), "final answer")
        self.assertEqual(hooks.claude_reply({"last_assistant_message": "direct"}), "direct")

    def test_claude_turn_must_include_the_telegram_message(self):
        path = Path(self.tmp.name) / "t.jsonl"
        sent = 1_800_000_000.0
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")
        before = [
            {"type": "user", "timestamp": iso(sent - 60), "message": {"content": "earlier question"}},
            {"type": "user", "timestamp": iso(sent - 30), "message": {"content": [{"type": "tool_result"}]}},
            {"type": "queue-operation", "timestamp": iso(sent + 1), "operation": "enqueue", "content": "123"},
            {"type": "api-request-blob"},
            {"type": "assistant", "timestamp": iso(sent + 3), "message": {"content": [{"type": "text", "text": "old"}]}},
        ]
        path.write_text("\n".join(json.dumps(e) for e in before))
        self.assertFalse(hooks.claude_turn_has_input_since({"transcript_path": str(path)}, sent))
        after = before + [{"type": "user", "timestamp": iso(sent + 5), "message": {"content": "123"}}]
        path.write_text("\n".join(json.dumps(e) for e in after))
        self.assertTrue(hooks.claude_turn_has_input_since({"transcript_path": str(path)}, sent))
        mid_turn = before + [{"type": "attachment", "timestamp": iso(sent + 2),
                              "attachment": {"type": "queued_command", "prompt": "123"}}]
        path.write_text("\n".join(json.dumps(e) for e in mid_turn))
        self.assertTrue(hooks.claude_turn_has_input_since({"transcript_path": str(path)}, sent))

    def test_codex_turn_has(self):
        self.assertTrue(hooks.codex_turn_has({"input-messages": ["please  fix\nthe bug"]}, "please fix the bug"))
        self.assertFalse(hooks.codex_turn_has({"input-messages": ["something else"]}, "please fix the bug"))

    def test_codex_reply(self):
        self.assertEqual(hooks.codex_reply({"type": "agent-turn-complete", "last-assistant-message": "hi"}), "hi")
        self.assertEqual(hooks.codex_reply({"type": "other"}), "")

    def test_wants(self):
        cfg = config.Config(token="t", chat_id=1)
        with mock.patch.object(presence, "idle_seconds", return_value=5):  # at the Mac
            self.assertFalse(hooks.wants(cfg, "block:a"))
            with state.update() as data:
                data["active"] = ["s", "block:a"]
            self.assertFalse(hooks.wants(cfg, "block:a"))  # active, but you're right there
            with state.update() as data:
                data["awaiting"] = {"block:b": [time.time(), "hi"]}
            self.assertTrue(hooks.wants(cfg, "block:b"))  # you asked from Telegram
        with mock.patch.object(presence, "idle_seconds", return_value=600):  # away
            self.assertTrue(hooks.wants(cfg, "block:a"))
            self.assertFalse(hooks.wants(cfg, "block:c"))
            cfg.notify = "asked"
            self.assertFalse(hooks.wants(cfg, "block:a"))
            self.assertTrue(hooks.wants(cfg, "block:b"))
            with state.update() as data:
                data["muted"] = True
            self.assertFalse(hooks.wants(cfg, "block:b"))


class Install(Isolated):
    def test_claude_hook_round_trip(self):
        d = Path(self.tmp.name) / ".claude"
        d.mkdir()
        other = {"type": "command", "command": "notify-me"}
        (d / "settings.json").write_text(json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [other]}]}}))
        with mock.patch.object(install, "executable", return_value="/bin/rexgram"):
            install.claude_hook(d)
            install.claude_hook(d)  # idempotent
            data = json.loads((d / "settings.json").read_text())
            commands = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
            self.assertEqual(commands, ["notify-me", "/bin/rexgram hook claude"])
            install.claude_hook(d, remove=True)
        data = json.loads((d / "settings.json").read_text())
        self.assertEqual(data, {"model": "x", "hooks": {"Stop": [{"hooks": [other]}]}})
        self.assertTrue((d / "settings.json.rexgram-backup").exists())

    def test_codex_hook_respects_existing_notify(self):
        codex = Path(self.tmp.name) / ".codex"
        codex.mkdir()
        cfg = codex / "config.toml"
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex)}), \
                mock.patch.object(install, "executable", return_value="/bin/rexgram"):
            cfg.write_text('model = "x"\n\n[tui]\nnotify = true\n')
            install.codex_hook()
            self.assertTrue(cfg.read_text().startswith('notify = ["/bin/rexgram", "hook", "codex"]\nmodel'))
            install.codex_hook(remove=True)
            self.assertEqual(cfg.read_text(), 'model = "x"\n\n[tui]\nnotify = true\n')
            cfg.write_text('notify = ["other"]\n')
            self.assertIn("left alone", install.codex_hook())
            self.assertEqual(cfg.read_text(), 'notify = ["other"]\n')


class Events(Isolated):
    def event(self, state_, app="claude-code", record_id=None):
        # The shape Rex sends: {"id": "", "record": {...}}
        status = {"id": record_id or "", "record": {"state": state_, "app": app}}
        return {"type": "session_event", "payload": {"session_id": "session:s", "event": {
            "type": "block_event", "payload": {"block_id": "block:b", "name": "program_status_changed",
                                               "payload": status}}}}

    def test_transitions(self):
        bot = Bot(config.Config(token="t", chat_id=1))
        with mock.patch.object(bot, "alert_blocked") as alert, \
                mock.patch("threading.Thread", side_effect=lambda target, args, daemon: mock.Mock(
                    start=lambda: target(*args))):
            bot.on_event(self.event("working"))
            bot.on_event(self.event("blocked"))
            bot.on_event(self.event("blocked"))  # no repeat alert
            bot.on_event(self.event("working"))
            bot.on_event(self.event("done"))
            bot.on_event(self.event("blocked", record_id="sub"))  # sub-task record
            bot.on_event(self.event("blocked", app="cargo"))  # not an agent
        alert.assert_called_once_with("session:s", "block:b", "")

    def test_focus_follows_agents_only(self):
        bot = Bot(config.Config(token="t", chat_id=1))
        bot.focus = {"session:s": "block:a"}
        with mock.patch("rexgram.rex.focused", return_value="block:shell"), \
                mock.patch("rexgram.agents.describe", return_value=None):
            bot.follow_focus("session:s")
        self.assertIsNone(state.active(state.load()))
        with mock.patch("rexgram.rex.focused", return_value="block:agent"), \
                mock.patch("rexgram.agents.describe", return_value=object()):
            bot.follow_focus("session:s")
        self.assertEqual(state.active(state.load()), ("session:s", "block:agent"))


if __name__ == "__main__":
    unittest.main()
