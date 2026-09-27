"""Offline tests: run with `python3 -m unittest discover -s tests` from tools/claude-telegram."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import bot  # noqa: E402

OWNER = 42


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.next_id = 100

    async def call(self, method, files=None, http_timeout=30, **params):
        params = {k: v for k, v in params.items() if v is not None}
        self.calls.append((method, params))
        self.next_id += 1
        if method == "createForumTopic":
            return {"message_thread_id": 77, "name": params["name"]}
        return {"message_id": self.next_id}

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]


def message(text, thread_id=None, user_id=OWNER, chat_id=-1001, chat_type="supergroup"):
    msg = {
        "message_id": 1,
        "chat": {"id": chat_id, "type": chat_type, "is_forum": True},
        "from": {"id": user_id, "is_bot": False, "username": "me"},
        "text": text,
    }
    if thread_id:
        msg.update(message_thread_id=thread_id, is_topic_message=True)
    return msg


class BridgeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.log = root / "claude.log"
        os.environ["FAKE_CLAUDE_LOG"] = str(self.log)
        (root / "project").mkdir()
        self.cfg = bot.Config(
            token="x",
            allowed_users={OWNER},
            allowed_chats=set(),
            default_cwd=root,
            data_dir=root / "data",
            claude_bin=str(HERE / "fake_claude.py"),
            permission_mode="acceptEdits",
            model="",
            allowed_tools=["Bash(git status)", "Read"],
            extra_args=[],
            system_prompt="sys",
            show_progress=True,
        )
        self.tg = FakeTelegram()
        self.store = bot.Store(self.cfg.data_dir / "state.json")
        self.bridge = bot.Bridge(self.cfg, self.tg, self.store)
        self.bridge.username = "claudebot"
        self.root = root

    async def asyncTearDown(self):
        for topic in self.bridge.topics.values():
            topic.worker.cancel()
        self.tmp.cleanup()

    def runs(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    async def settle(self):
        for _ in range(200):
            await asyncio.sleep(0.02)
            if all(not t.busy and t.queue.empty() for t in self.bridge.topics.values()):
                return

    async def test_topics_get_separate_resumed_sessions(self):
        await self.bridge.on_message(message("hello", thread_id=5))
        await self.settle()
        await self.bridge.on_message(message("again", thread_id=5))
        await self.settle()

        runs = self.runs()
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["prompt"], "hello")
        self.assertNotIn("--resume", runs[0]["args"])
        self.assertEqual(runs[1]["args"][runs[1]["args"].index("--resume") + 1], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(runs[1]["args"][-3:], ["--allowedTools", "Bash(git status)", "Read"])

        replies = [p for p in self.tg.sent() if p.get("parse_mode") == "HTML"]
        self.assertEqual(replies[0]["message_thread_id"], 5)
        self.assertIn('<pre><code class="language-sh">ls &lt;dir&gt;</code></pre>', replies[0]["text"])
        self.assertIn("<b>this</b>", replies[0]["text"])

        status = [p["text"] for p in self.tg.sent() if p.get("disable_notification")]
        self.assertTrue(status and "Bash: ls -la" in status[0])
        self.assertIn("💬 Let me look.", status[0])

        # A different topic starts its own session.
        await self.bridge.on_message(message("other", thread_id=9))
        await self.settle()
        self.assertNotIn("--resume", self.runs()[2]["args"])
        self.assertIn("-1001:9", self.store.data["topics"])

    async def test_missing_session_restarts_fresh(self):
        self.store.topic("-1001:5")["session_id"] = "missing"
        await self.bridge.on_message(message("hi", thread_id=5))
        await self.settle()
        runs = self.runs()
        self.assertEqual(len(runs), 2)
        self.assertNotIn("--resume", runs[1]["args"])
        self.assertTrue(any("not found" in p["text"] for p in self.tg.sent()))

    async def test_unauthorized_users_are_ignored(self):
        await self.bridge.on_message(message("rm -rf /", user_id=7, chat_id=7, chat_type="private"))
        await self.settle()
        self.assertEqual(self.runs(), [])
        self.assertIn("Your Telegram user id is 7", self.tg.sent()[0]["text"])

    async def test_commands(self):
        await self.bridge.on_message(message("/cwd project", thread_id=5))
        self.assertEqual(self.store.topic("-1001:5")["cwd"], str((self.root / "project").resolve()))

        await self.bridge.on_message(message(f"/topic Blog | {self.root / 'project'}", thread_id=5))
        self.assertEqual(self.tg.sent("createForumTopic")[0]["name"], "Blog")
        self.assertEqual(self.store.topic("-1001:77")["name"], "Blog")

        await self.bridge.on_message(message("/every 2h check CI", thread_id=77))
        await self.bridge.on_message(message("/daily 08:30 brief", thread_id=77))
        jobs = self.store.data["jobs"]
        self.assertEqual([j["id"] for j in jobs], [1, 2])
        self.assertEqual(jobs[0]["every"], 7200)
        await self.bridge.on_message(message("/unjob 1", thread_id=77))
        self.assertEqual([j["id"] for j in self.store.data["jobs"]], [2])

        # Commands for other bots are ignored; unknown commands go to Claude.
        await self.bridge.on_message(message("/new@otherbot", thread_id=5))
        await self.bridge.on_message(message("/review the diff", thread_id=5))
        await self.settle()
        self.assertEqual(self.runs()[-1]["prompt"], "/review the diff")
        self.assertEqual(self.runs()[-1]["cwd"], str((self.root / "project").resolve()))


class FormattingTest(unittest.TestCase):
    def test_split_keeps_fences_balanced(self):
        text = "intro\n```py\n" + "\n".join(f"line {i}" for i in range(2000)) + "\n```\nbye"
        chunks = bot.split_markdown(text, limit=1000)
        self.assertGreater(len(chunks), 5)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 1000)
            self.assertEqual(chunk.count("```") % 2, 0, chunk[:80])

    def test_md_to_html_escapes(self):
        out = bot.md_to_html("# Title\nuse `a<b>` and [docs](https://x.y/?a=1&b=2)")
        self.assertIn("<b>Title</b>", out)
        self.assertIn("<code>a&lt;b&gt;</code>", out)
        self.assertIn('<a href="https://x.y/?a=1&amp;b=2">docs</a>', out)

    def test_split_tools(self):
        self.assertEqual(
            bot.split_tools("Bash(git status:*) Read, Bash(npm run test:*),WebFetch"),
            ["Bash(git status:*)", "Read", "Bash(npm run test:*)", "WebFetch"],
        )

    def test_schedule_helpers(self):
        self.assertEqual(bot.parse_interval("30m"), 1800)
        self.assertIsNone(bot.parse_interval("soon"))
        now = datetime(2026, 1, 1, 9, 0)
        self.assertEqual(datetime.fromtimestamp(bot.next_daily("08:30", now)), datetime(2026, 1, 2, 8, 30))
        self.assertEqual(datetime.fromtimestamp(bot.next_daily("10:00", now)), datetime(2026, 1, 1, 10, 0))


if __name__ == "__main__":
    unittest.main()
