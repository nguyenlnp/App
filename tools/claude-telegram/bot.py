#!/usr/bin/env python3
"""
claude-telegram: chat with Claude Code from Telegram.

* Every forum topic (or private chat) gets its own persistent Claude Code
  session and working folder, so one Telegram group can hold many parallel
  "sub topics" (one per project / task).
* Runs as a long-lived daemon (systemd / launchd), survives restarts because
  sessions are resumed with `claude --resume`, and can run scheduled prompts,
  in the spirit of Hermes Agent's messaging gateway.

Standard library only. Needs Python 3.10+ and the `claude` CLI on PATH.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shlex
import shutil
import signal
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("claude-telegram")

HERE = Path(__file__).resolve().parent

# Telegram's hard limit is 4096 characters; keep headroom for HTML tags.
TG_LIMIT = 3800

# Replies longer than this many chunks are attached as a file instead.
MAX_CHUNKS = 6

DEFAULT_SYSTEM_PROMPT = (
    "You are being used through a Telegram chat and the user usually reads on a phone. "
    "Keep answers concise: short paragraphs and bullet lists, no wide tables, and put "
    "commands or code in fenced code blocks. Nobody can answer interactive permission "
    "prompts; if a tool is denied, say what you needed and why."
)

COMMANDS = [
    {"command": "new", "description": "Start a fresh Claude session in this topic"},
    {"command": "stop", "description": "Stop the running task and clear the queue"},
    {"command": "status", "description": "Session, folder and queue of this topic"},
    {"command": "cwd", "description": "Show or set the working folder: /cwd ~/project"},
    {"command": "topic", "description": "Create a topic: /topic Name | ~/folder"},
    {"command": "model", "description": "Show or set the model: /model opus"},
    {"command": "mode", "description": "Permission mode: acceptEdits, auto, plan..."},
    {"command": "resume", "description": "Attach an existing Claude session id"},
    {"command": "every", "description": "Recurring prompt: /every 2h check CI"},
    {"command": "daily", "description": "Daily prompt: /daily 08:30 morning brief"},
    {"command": "jobs", "description": "List scheduled prompts"},
    {"command": "unjob", "description": "Delete a scheduled prompt: /unjob 3"},
    {"command": "help", "description": "Show help"},
]

HELP = """🤖 Claude Code over Telegram

Just type a message: it goes to the Claude Code session of this topic. Each forum topic is a separate session with its own folder, so use one topic per project or task. Photos and files are saved and passed to Claude.

/new: fresh session (same folder)
/stop: stop the running task, clear the queue
/status: what this topic is doing
/cwd ~/code/site: set this topic's folder (starts a new session)
/topic Blog | ~/code/blog: create a new topic bound to a folder
/model opus: model for this topic (/model default to reset)
/mode acceptEdits|auto|plan|bypassPermissions: permission mode
/resume <session-id>: continue a session started in the terminal
/every 2h <prompt>: recurring prompt in this topic
/daily 08:30 <prompt>: prompt every day at a local time
/jobs, /unjob <id>: list or delete scheduled prompts

Any other /command (for example a Claude Code skill) is passed to Claude as is."""

PERMISSION_MODES = ("acceptEdits", "auto", "plan", "bypassPermissions", "dontAsk", "default", "manual")


# --------------------------------------------------------------------------- config


def load_env_file(path: Path) -> None:
    """Minimal .env loader. Values already in the environment win."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _int_set(value: str) -> set[int]:
    return {int(v) for v in re.split(r"[,\s]+", value.strip()) if v}


def split_tools(value: str) -> list[str]:
    """Split "Bash(git status:*) Read, WebFetch" without breaking inside parentheses."""
    return re.findall(r"[^\s,()]+(?:\([^)]*\))?", value)


def _truthy(value: str) -> bool:
    return value.strip().lower() not in ("", "0", "false", "no", "off")


@dataclass
class Config:
    token: str
    allowed_users: set[int]
    allowed_chats: set[int]
    default_cwd: Path
    data_dir: Path
    claude_bin: str
    permission_mode: str
    model: str
    allowed_tools: list[str]
    extra_args: list[str]
    system_prompt: str
    show_progress: bool

    @classmethod
    def from_env(cls) -> Config:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            sys.exit("TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
        return cls(
            token=token,
            allowed_users=_int_set(os.environ.get("ALLOWED_USER_IDS", "")),
            allowed_chats=_int_set(os.environ.get("ALLOWED_CHAT_IDS", "")),
            default_cwd=Path(os.environ.get("DEFAULT_CWD", "~")).expanduser(),
            data_dir=Path(os.environ.get("DATA_DIR", "~/.claude-telegram")).expanduser(),
            claude_bin=os.environ.get("CLAUDE_BIN", "claude"),
            permission_mode=os.environ.get("PERMISSION_MODE", "acceptEdits"),
            model=os.environ.get("CLAUDE_MODEL", ""),
            allowed_tools=split_tools(os.environ.get("ALLOWED_TOOLS", "")),
            extra_args=shlex.split(os.environ.get("CLAUDE_EXTRA_ARGS", "")),
            system_prompt=os.environ.get("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
            show_progress=_truthy(os.environ.get("SHOW_PROGRESS", "1")),
        )


class Store:
    """Tiny JSON state file: per-topic settings, scheduled jobs, update offset."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {"topics": {}, "jobs": [], "offset": 0}
        if path.is_file():
            try:
                self.data.update(json.loads(path.read_text()))
            except (OSError, ValueError):
                log.exception("Could not read %s, starting with empty state", path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))
        os.replace(tmp, self.path)

    def topic(self, key: str) -> dict[str, Any]:
        return self.data["topics"].setdefault(key, {})


# --------------------------------------------------------------------------- telegram


class TelegramError(Exception):
    def __init__(self, description: str, code: int = 0, retry_after: int = 0):
        super().__init__(description)
        self.description = description
        self.code = code
        self.retry_after = retry_after


def _multipart(params: dict[str, Any], files: dict[str, tuple[str, bytes]]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for key, value in params.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    for key, (filename, data) in files.items():
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        )
        parts.append(head.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class Telegram:
    """Just enough of the Bot API, over urllib in worker threads."""

    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.file_base = f"https://api.telegram.org/file/bot{token}/"

    def _request(self, method: str, params: dict[str, Any], files: dict | None, http_timeout: float) -> Any:
        if files:
            body, content_type = _multipart(params, files)
        else:
            body, content_type = json.dumps(params).encode(), "application/json"
        req = urllib.request.Request(self.base + method, data=body, headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read())
            except ValueError:
                raise TelegramError(f"HTTP {e.code}", e.code) from None
        if not payload.get("ok"):
            extra = payload.get("parameters") or {}
            raise TelegramError(
                payload.get("description", "unknown error"),
                payload.get("error_code", 0),
                extra.get("retry_after", 0),
            )
        return payload["result"]

    async def call(self, method: str, files: dict | None = None, http_timeout: float = 30, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(5):
            try:
                return await asyncio.to_thread(self._request, method, params, files, http_timeout)
            except TelegramError as e:
                if e.retry_after and attempt < 4:
                    await asyncio.sleep(e.retry_after + 1)
                    continue
                raise
            except OSError:
                if attempt == 4:
                    raise
                await asyncio.sleep(2**attempt)
        raise TelegramError(f"{method} failed after retries")

    async def download(self, file_id: str, dest: Path) -> Path:
        info = await self.call("getFile", file_id=file_id)
        url = self.file_base + info["file_path"]

        def fetch() -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as out:
                shutil.copyfileobj(resp, out)

        await asyncio.to_thread(fetch)
        return dest


# --------------------------------------------------------------------------- formatting


def one_line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def split_markdown(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Split on line boundaries so each chunk fits a message, keeping ``` fences balanced."""
    lines: list[str] = []
    for line in text.split("\n"):
        while len(line) > limit - 40:
            lines.append(line[: limit - 40])
            line = line[limit - 40 :]
        lines.append(line)

    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    fence: str | None = None
    for line in lines:
        if cur and size + len(line) + 1 > limit - 8:
            if fence is not None:
                cur.append("```")
            chunks.append("\n".join(cur))
            cur = [fence] if fence is not None else []
            size = sum(len(c) + 1 for c in cur)
        cur.append(line)
        size += len(line) + 1
        if line.lstrip().startswith("```"):
            fence = None if fence is not None else line.strip()
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()] or [""]


def _pre(lines: list[str], lang: str) -> str:
    body = html.escape("\n".join(lines))
    if lang and re.fullmatch(r"[\w+#.-]+", lang):
        return f'<pre><code class="language-{lang}">{body}</code></pre>'
    return f"<pre>{body}</pre>"


def _inline(line: str) -> str:
    out = []
    for piece in re.split(r"(`[^`\n]+`)", line):
        if len(piece) >= 2 and piece.startswith("`") and piece.endswith("`"):
            out.append(f"<code>{html.escape(piece[1:-1])}</code>")
            continue
        s = html.escape(piece, quote=False)
        s = re.sub(r"^(\s*)#{1,6}\s+(.+)$", r"\1<b>\2</b>", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s\"]+)\)", r'<a href="\2">\1</a>', s)
        out.append(s)
    return "".join(out)


def md_to_html(md: str) -> str:
    """Convert the Markdown Claude usually writes into Telegram's HTML subset."""
    out: list[str] = []
    code: list[str] = []
    lang = ""
    in_code = False
    for line in md.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                out.append(_pre(code, lang))
                code, in_code = [], False
            else:
                in_code, lang = True, stripped[3:].strip()
            continue
        if in_code:
            code.append(line)
        else:
            out.append(_inline(line))
    if in_code:
        out.append(_pre(code, lang))
    return "\n".join(out)


def describe_tool(block: dict[str, Any]) -> str:
    name = str(block.get("name", "tool"))
    if name.startswith("mcp__"):
        name = name.split("__", 2)[-1]
    inp = block.get("input") or {}
    for key in ("command", "file_path", "notebook_path", "path", "pattern", "url", "query", "description", "skill", "prompt"):
        value = inp.get(key)
        if value:
            return f"{name}: {one_line(str(value), 90)}"
    return name


# --------------------------------------------------------------------------- scheduling


def parse_interval(value: str) -> int | None:
    m = re.fullmatch(r"(\d+)\s*([smhd])", value.strip().lower())
    if not m:
        return None
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def next_daily(hhmm: str, now: datetime) -> float:
    hour, minute = map(int, hhmm.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


# --------------------------------------------------------------------------- bridge


class Topic:
    """Runtime state of one conversation (a forum topic or a whole private chat)."""

    def __init__(self, key: str, chat_id: int, thread_id: int | None):
        self.key = key
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.proc: asyncio.subprocess.Process | None = None
        self.worker: asyncio.Task | None = None
        self.busy = False
        self.cancelled = False


class Progress:
    """One quietly edited status message listing what Claude is doing."""

    EDIT_EVERY = 3.0

    def __init__(self, bridge: Bridge, topic: Topic):
        self.bridge = bridge
        self.topic = topic
        self.lines: list[str] = []
        self.msg_id: int | None = None
        self.last_edit = 0.0
        self.task: asyncio.Task | None = None
        self.sleeping = False

    def add(self, line: str) -> None:
        self.lines.append(line)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        self.sleeping = True
        try:
            await asyncio.sleep(max(0.0, self.last_edit + self.EDIT_EVERY - time.monotonic()))
        finally:
            self.sleeping = False
        await self._render("⚙️ Working…")

    def _text(self, header: str) -> str:
        shown = self.lines[-12:]
        hidden = len(self.lines) - len(shown)
        parts = [header]
        if hidden:
            parts.append(f"… {hidden} earlier steps")
        parts.extend(shown)
        return "\n".join(parts)[:4000]

    async def _render(self, header: str) -> None:
        self.last_edit = time.monotonic()
        text = self._text(header)
        tg = self.bridge.tg
        try:
            if self.msg_id is None:
                msg = await tg.call(
                    "sendMessage",
                    chat_id=self.topic.chat_id,
                    message_thread_id=self.topic.thread_id,
                    text=text,
                    disable_notification=True,
                )
                self.msg_id = msg["message_id"]
            else:
                await tg.call("editMessageText", chat_id=self.topic.chat_id, message_id=self.msg_id, text=text)
        except TelegramError as e:
            if "not modified" not in e.description:
                log.warning("Progress update failed: %s", e.description)

    async def finish(self, header: str) -> None:
        if self.task and not self.task.done():
            if self.sleeping:
                self.task.cancel()
            else:
                await asyncio.gather(self.task, return_exceptions=True)
        if not self.lines and self.msg_id is None:
            return  # quick answer without tool use: no status message needed
        await self._render(header)


class Bridge:
    def __init__(self, cfg: Config, tg: Telegram, store: Store):
        self.cfg = cfg
        self.tg = tg
        self.store = store
        self.topics: dict[str, Topic] = {}
        self.inbox = cfg.data_dir / "inbox"
        self.username = ""

    # ---- lifecycle

    async def main(self) -> None:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        poller = asyncio.create_task(self.poll())
        scheduler = asyncio.create_task(self.scheduler())
        stopper = asyncio.create_task(stop.wait())
        await asyncio.wait({poller, scheduler, stopper}, return_when=asyncio.FIRST_COMPLETED)
        for task in (poller, scheduler):
            if task.done() and task.exception():
                log.error("Fatal error", exc_info=task.exception())
        await self.shutdown()
        for task in (poller, scheduler, stopper):
            task.cancel()

    async def shutdown(self) -> None:
        log.info("Shutting down")
        busy = [t for t in self.topics.values() if t.proc and t.proc.returncode is None]
        for topic in busy:
            topic.cancelled = True
            self._signal(topic.proc, signal.SIGTERM)
        notes = [
            self.say(topic, "♻️ The bridge is restarting and this task was interrupted. Send “continue” to pick it up.")
            for topic in busy
        ]
        if notes:
            await asyncio.wait_for(asyncio.gather(*notes, return_exceptions=True), timeout=10)
        self.store.save()

    async def poll(self) -> None:
        me = await self.tg.call("getMe")
        self.username = me.get("username", "")
        log.info("Connected as @%s", self.username)
        try:
            await self.tg.call("setMyCommands", commands=COMMANDS)
        except TelegramError as e:
            log.warning("setMyCommands failed: %s", e.description)
        offset = int(self.store.data.get("offset", 0))
        while True:
            try:
                updates = await self.tg.call(
                    "getUpdates", offset=offset, timeout=50, allowed_updates=["message"], http_timeout=65
                )
            except Exception:
                log.exception("getUpdates failed, retrying in 5s")
                await asyncio.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                if "message" in update:
                    try:
                        await self.on_message(update["message"])
                    except Exception:
                        log.exception("Failed to handle update %s", update["update_id"])
            if updates:
                self.store.data["offset"] = offset
                self.store.save()

    # ---- helpers

    def topic_for(self, chat_id: int, thread_id: int | None) -> Topic:
        key = f"{chat_id}:{thread_id or 0}"
        topic = self.topics.get(key)
        if topic is None:
            topic = self.topics[key] = Topic(key, chat_id, thread_id)
        if topic.worker is None or topic.worker.done():
            topic.worker = asyncio.create_task(self._worker(topic))
        return topic

    def settings(self, topic: Topic) -> dict[str, Any]:
        s = self.store.topic(topic.key)
        s.setdefault("cwd", str(self.cfg.default_cwd))
        return s

    async def say(self, topic: Topic, text: str) -> None:
        try:
            await self.tg.call(
                "sendMessage", chat_id=topic.chat_id, message_thread_id=topic.thread_id, text=text[:4096]
            )
        except Exception:
            log.exception("sendMessage failed")

    async def send_reply(self, topic: Topic, text: str) -> None:
        text = text.strip() or "(empty reply)"
        chunks = split_markdown(text)
        if len(chunks) > MAX_CHUNKS:
            chunks = chunks[:1]
            await self.tg.call(
                "sendDocument",
                files={"document": ("reply.md", text.encode())},
                chat_id=topic.chat_id,
                message_thread_id=topic.thread_id,
                caption="Full reply (too long for chat messages)",
            )
        for chunk in chunks:
            try:
                await self.tg.call(
                    "sendMessage",
                    chat_id=topic.chat_id,
                    message_thread_id=topic.thread_id,
                    text=md_to_html(chunk),
                    parse_mode="HTML",
                    link_preview_options={"is_disabled": True},
                )
            except TelegramError as e:
                log.info("HTML send rejected (%s), sending plain text", e.description)
                await self.say(topic, chunk)

    async def _typing(self, topic: Topic) -> None:
        while True:
            try:
                await self.tg.call(
                    "sendChatAction", chat_id=topic.chat_id, message_thread_id=topic.thread_id, action="typing"
                )
            except Exception:
                pass
            await asyncio.sleep(4.5)

    @staticmethod
    def _signal(proc: asyncio.subprocess.Process | None, sig: int) -> None:
        if proc is None or proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    async def _escalate(self, proc: asyncio.subprocess.Process) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            await asyncio.sleep(5)
            if proc.returncode is not None:
                return
            self._signal(proc, sig)

    # ---- incoming messages

    async def on_message(self, msg: dict[str, Any]) -> None:
        chat = msg["chat"]
        user = msg.get("from") or {}
        chat_id = chat["id"]
        if user.get("is_bot"):
            return
        if user.get("id") not in self.cfg.allowed_users:
            log.warning("Ignoring message from unauthorized user %s (@%s)", user.get("id"), user.get("username"))
            if chat.get("type") == "private":
                await self.tg.call(
                    "sendMessage",
                    chat_id=chat_id,
                    text=f"⛔ Not authorized. Your Telegram user id is {user.get('id')}. "
                    "Add it to ALLOWED_USER_IDS in the bridge's .env to use this bot.",
                )
            return
        if chat.get("type") != "private" and self.cfg.allowed_chats and chat_id not in self.cfg.allowed_chats:
            log.warning("Ignoring message from chat %s (not in ALLOWED_CHAT_IDS)", chat_id)
            return

        if "forum_topic_created" in msg or "forum_topic_edited" in msg:
            info = msg.get("forum_topic_created") or msg.get("forum_topic_edited") or {}
            if info.get("name"):
                self.store.topic(f"{chat_id}:{msg['message_thread_id']}")["name"] = info["name"]
                self.store.save()
            return

        thread_id = msg.get("message_thread_id") if msg.get("is_topic_message") else None
        topic = self.topic_for(chat_id, thread_id)
        text = msg.get("text") or msg.get("caption") or ""

        m = re.match(r"/(\w+)(?:@(\w+))?(?:\s+(.*))?$", text, re.S)
        if m:
            command, target, arg = m.group(1).lower(), m.group(2), (m.group(3) or "").strip()
            if target and target.lower() != self.username.lower():
                return  # addressed to another bot in the group
            handler = getattr(self, f"cmd_{command}", None)
            if handler:
                await handler(topic, arg, msg)
                return
            # Unknown commands (e.g. Claude Code skills) go to Claude untouched.

        attachments = await self._download_attachments(msg, topic)
        if not text and not attachments:
            return
        prompt = text
        quoted = msg.get("reply_to_message") or {}
        if quoted and "forum_topic_created" not in quoted:
            quoted_text = quoted.get("text") or quoted.get("caption")
            if quoted_text:
                prompt = f"(Replying to this earlier message:\n{quoted_text[:1500]}\n)\n\n{prompt}"
        if attachments:
            prompt += "\n\n" + "\n".join(f"[Attached file: {p}]" for p in attachments)
        await self.enqueue(topic, prompt.strip())

    async def _download_attachments(self, msg: dict[str, Any], topic: Topic) -> list[str]:
        items: list[tuple[str, str]] = []
        if msg.get("photo"):
            items.append((msg["photo"][-1]["file_id"], f"photo_{msg['message_id']}.jpg"))
        for kind, ext in (("document", ""), ("audio", ".mp3"), ("video", ".mp4"), ("voice", ".ogg")):
            obj = msg.get(kind)
            if obj:
                items.append((obj["file_id"], obj.get("file_name") or f"{kind}_{msg['message_id']}{ext}"))
        paths = []
        for file_id, name in items:
            safe = re.sub(r"[^\w.\-]+", "_", name)[-100:]
            dest = self.inbox / topic.key.replace(":", "_") / f"{int(time.time())}_{safe}"
            try:
                paths.append(str(await self.tg.download(file_id, dest)))
            except Exception as e:
                await self.say(topic, f"⚠️ Could not download {name}: {e} (Telegram bots can only fetch files up to 20 MB)")
        return paths

    async def enqueue(self, topic: Topic, prompt: str) -> None:
        ahead = topic.queue.qsize() + (1 if topic.busy else 0)
        topic.queue.put_nowait(prompt)
        if ahead:
            await self.say(topic, f"⏳ Queued ({ahead} ahead). /stop cancels everything.")

    async def _worker(self, topic: Topic) -> None:
        while True:
            prompt = await topic.queue.get()
            topic.busy = True
            try:
                await self.run_claude(topic, prompt)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("Run failed in %s", topic.key)
                await self.say(topic, f"💥 Bridge error: {e}")
            finally:
                topic.busy = False

    # ---- running Claude Code

    def build_command(self, s: dict[str, Any]) -> list[str]:
        cmd = [
            self.cfg.claude_bin,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            s.get("mode") or self.cfg.permission_mode,
            "--append-system-prompt",
            self.cfg.system_prompt,
            "--add-dir",
            str(self.inbox),
        ]
        if s.get("session_id"):
            cmd += ["--resume", s["session_id"]]
        model = s.get("model") or self.cfg.model
        if model:
            cmd += ["--model", model]
        cmd += self.cfg.extra_args
        if self.cfg.allowed_tools:
            # Variadic flag, so it goes last; the prompt itself is sent on stdin.
            cmd += ["--allowedTools", *self.cfg.allowed_tools]
        return cmd

    async def run_claude(self, topic: Topic, prompt: str, retry: bool = True) -> None:
        s = self.settings(topic)
        cwd = Path(s["cwd"])
        if not cwd.is_dir():
            await self.say(topic, f"⚠️ Folder {cwd} does not exist. Set one with /cwd <path>.")
            return
        self.inbox.mkdir(parents=True, exist_ok=True)
        topic.cancelled = False
        progress = Progress(self, topic) if self.cfg.show_progress else None
        started = time.monotonic()

        proc = await asyncio.create_subprocess_exec(
            *self.build_command(s),
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=64 * 1024 * 1024,
        )
        topic.proc = proc
        assert proc.stdin and proc.stdout and proc.stderr
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()
        stderr_task = asyncio.create_task(proc.stderr.read())
        typing = asyncio.create_task(self._typing(topic))

        result: dict[str, Any] | None = None
        pending_text = ""
        try:
            async for raw in proc.stdout:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                etype = event.get("type")
                if etype == "system" and event.get("subtype") == "init" and event.get("session_id"):
                    s["session_id"] = event["session_id"]
                    self.store.save()
                elif etype == "assistant" and progress:
                    nested = event.get("parent_tool_use_id") is not None
                    for block in (event.get("message") or {}).get("content") or []:
                        if block.get("type") == "tool_use":
                            if pending_text:
                                progress.add("💬 " + one_line(pending_text, 140))
                                pending_text = ""
                            progress.add(("   ↳ " if nested else "") + "🔧 " + describe_tool(block))
                        elif block.get("type") == "text" and not nested and block.get("text", "").strip():
                            pending_text = block["text"]
                elif etype == "result":
                    result = event
        finally:
            typing.cancel()
            await proc.wait()
            topic.proc = None
        stderr = (await stderr_task).decode(errors="replace")
        elapsed = time.monotonic() - started

        if topic.cancelled:
            if progress:
                await progress.finish(f"🛑 Stopped after {elapsed:.0f}s")
            return

        details = stderr + json.dumps(result or {})
        if retry and s.get("session_id") and "No conversation found" in details:
            s.pop("session_id", None)
            self.store.save()
            await self.say(topic, "ℹ️ The previous session was not found (folder changed?). Starting a fresh one.")
            await self.run_claude(topic, prompt, retry=False)
            return

        if result is None:
            if progress:
                await progress.finish("❌ Failed")
            tail = stderr.strip()[-1500:] or "(no error output)"
            await self.say(topic, f"❌ Claude Code exited with code {proc.returncode}.\n\n{tail}")
            return

        if result.get("session_id"):
            s["session_id"] = result["session_id"]
        s["cost_usd"] = round(s.get("cost_usd", 0) + (result.get("total_cost_usd") or 0), 4)
        s["last_run"] = datetime.now().isoformat(timespec="seconds")
        self.store.save()

        is_error = bool(result.get("is_error"))
        if progress:
            icon = "⚠️" if is_error else "✅"
            await progress.finish(f"{icon} Done · {result.get('num_turns', '?')} turns · {elapsed:.0f}s")
        text = result.get("result") or ""
        if is_error and not text:
            errors = "; ".join(map(str, result.get("errors") or [])) or result.get("subtype", "error")
            text = f"⚠️ Claude Code stopped: {errors}"
        await self.send_reply(topic, text)

    # ---- commands

    async def cmd_start(self, topic: Topic, arg: str, msg: dict) -> None:
        await self.cmd_help(topic, arg, msg)

    async def cmd_help(self, topic: Topic, arg: str, msg: dict) -> None:
        await self.say(topic, HELP)

    async def cmd_new(self, topic: Topic, arg: str, msg: dict) -> None:
        s = self.settings(topic)
        s.pop("session_id", None)
        self.store.save()
        await self.say(topic, f"🆕 Fresh session. Folder: {s['cwd']}")
        if arg:
            await self.enqueue(topic, arg)

    async def cmd_stop(self, topic: Topic, arg: str, msg: dict) -> None:
        dropped = 0
        while not topic.queue.empty():
            topic.queue.get_nowait()
            dropped += 1
        running = topic.proc is not None and topic.proc.returncode is None
        if running and topic.proc:
            topic.cancelled = True
            self._signal(topic.proc, signal.SIGINT)
            asyncio.create_task(self._escalate(topic.proc))
        if not running and not dropped:
            await self.say(topic, "Nothing is running.")
            return
        await self.say(topic, f"🛑 Stopping{' the running task' if running else ''}" + (f", dropped {dropped} queued" if dropped else "") + ".")

    async def cmd_status(self, topic: Topic, arg: str, msg: dict) -> None:
        s = self.settings(topic)
        jobs = [j for j in self.store.data["jobs"] if j["key"] == topic.key]
        lines = [
            f"📌 {s.get('name') or ('Private chat' if topic.chat_id > 0 else 'General')}",
            f"📁 {s['cwd']}",
            f"🧵 Session: {s.get('session_id') or 'none yet'}",
            f"🧠 Model: {s.get('model') or self.cfg.model or 'CLI default'}",
            f"🔐 Mode: {s.get('mode') or self.cfg.permission_mode}",
            f"⚙️ {'Running' if topic.busy else 'Idle'}, {topic.queue.qsize()} queued",
            f"⏰ {len(jobs)} scheduled prompt(s)",
            f"💵 API-equivalent cost so far: ${s.get('cost_usd', 0):.2f}",
        ]
        await self.say(topic, "\n".join(lines))

    def _resolve_dir(self, topic: Topic, raw: str) -> Path | None:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(self.settings(topic)["cwd"]) / path
        path = path.resolve()
        return path if path.is_dir() else None

    async def cmd_cwd(self, topic: Topic, arg: str, msg: dict) -> None:
        s = self.settings(topic)
        if not arg:
            await self.say(topic, f"📁 {s['cwd']}\nChange it with /cwd <path>.")
            return
        path = self._resolve_dir(topic, arg)
        if path is None:
            await self.say(topic, f"⚠️ Not a folder: {arg}")
            return
        s["cwd"] = str(path)
        s.pop("session_id", None)  # sessions are stored per folder, so start over
        self.store.save()
        await self.say(topic, f"📁 Folder set to {path}. A new session starts with your next message.")

    async def cmd_topic(self, topic: Topic, arg: str, msg: dict) -> None:
        name, _, folder = (part.strip() for part in arg.partition("|"))
        if not name:
            await self.say(topic, "Usage: /topic Name | ~/optional/folder")
            return
        path = self._resolve_dir(topic, folder) if folder else Path(self.settings(topic)["cwd"])
        if path is None:
            await self.say(topic, f"⚠️ Not a folder: {folder}")
            return
        try:
            created = await self.tg.call("createForumTopic", chat_id=topic.chat_id, name=name[:128])
        except TelegramError as e:
            await self.say(
                topic,
                f"⚠️ Could not create the topic: {e.description}\n"
                "Use a group with Topics enabled and make the bot an admin with “Manage topics”.",
            )
            return
        new = self.topic_for(topic.chat_id, created["message_thread_id"])
        s = self.settings(new)
        s.update(name=name, cwd=str(path))
        self.store.save()
        await self.say(new, f"👋 New Claude Code session for “{name}”.\n📁 {path}\nSend a message to start.")

    async def cmd_model(self, topic: Topic, arg: str, msg: dict) -> None:
        s = self.settings(topic)
        if not arg:
            await self.say(topic, f"🧠 Model: {s.get('model') or self.cfg.model or 'CLI default'}\nChange with /model <name> (e.g. opus, sonnet, haiku), or /model default.")
            return
        if arg.lower() == "default":
            s.pop("model", None)
        else:
            s["model"] = arg.split()[0]
        self.store.save()
        await self.say(topic, f"🧠 Model for this topic: {s.get('model') or 'default'}")

    async def cmd_mode(self, topic: Topic, arg: str, msg: dict) -> None:
        s = self.settings(topic)
        if arg not in PERMISSION_MODES:
            await self.say(
                topic,
                f"🔐 Mode: {s.get('mode') or self.cfg.permission_mode}\n"
                f"Options: {', '.join(PERMISSION_MODES)}\n"
                "bypassPermissions lets Claude run any command without asking. Only use it on a machine you can afford to break.",
            )
            return
        s["mode"] = arg
        self.store.save()
        await self.say(topic, f"🔐 Permission mode for this topic: {arg}")

    async def cmd_resume(self, topic: Topic, arg: str, msg: dict) -> None:
        if not re.fullmatch(r"[0-9a-fA-F-]{8,}", arg):
            await self.say(topic, "Usage: /resume <session-id>\nThe session must belong to this topic's folder (see /cwd).")
            return
        self.settings(topic)["session_id"] = arg
        self.store.save()
        await self.say(topic, f"🧵 This topic now continues session {arg}.")

    def _add_job(self, topic: Topic, prompt: str, **when: Any) -> dict[str, Any]:
        jobs = self.store.data["jobs"]
        job = {
            "id": max((j["id"] for j in jobs), default=0) + 1,
            "key": topic.key,
            "chat_id": topic.chat_id,
            "thread_id": topic.thread_id,
            "prompt": prompt,
            **when,
        }
        jobs.append(job)
        self.store.save()
        return job

    async def cmd_every(self, topic: Topic, arg: str, msg: dict) -> None:
        interval, _, prompt = arg.partition(" ")
        seconds = parse_interval(interval)
        if not seconds or seconds < 60 or not prompt.strip():
            await self.say(topic, "Usage: /every 30m <prompt>  (units: m, h, d; minimum 1m)")
            return
        job = self._add_job(topic, prompt.strip(), every=seconds, next=time.time() + seconds)
        await self.say(topic, f"⏰ Job #{job['id']}: every {interval}: {prompt.strip()}")

    async def cmd_daily(self, topic: Topic, arg: str, msg: dict) -> None:
        at, _, prompt = arg.partition(" ")
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", at) or not prompt.strip():
            await self.say(topic, "Usage: /daily 08:30 <prompt>  (server's local time)")
            return
        job = self._add_job(topic, prompt.strip(), daily=at, next=next_daily(at, datetime.now()))
        await self.say(topic, f"⏰ Job #{job['id']}: daily at {at}: {prompt.strip()}")

    async def cmd_jobs(self, topic: Topic, arg: str, msg: dict) -> None:
        jobs = [j for j in self.store.data["jobs"] if j["key"] == topic.key]
        if not jobs:
            await self.say(topic, "No scheduled prompts in this topic. Add one with /every or /daily.")
            return
        lines = []
        for j in jobs:
            when = f"daily {j['daily']}" if j.get("daily") else f"every {j['every'] // 60}m"
            nxt = datetime.fromtimestamp(j["next"]).strftime("%m-%d %H:%M")
            lines.append(f"#{j['id']} · {when} · next {nxt}\n   {one_line(j['prompt'], 120)}")
        await self.say(topic, "\n".join(lines))

    async def cmd_unjob(self, topic: Topic, arg: str, msg: dict) -> None:
        jobs = self.store.data["jobs"]
        keep = [j for j in jobs if not (str(j["id"]) == arg.lstrip("#") and j["key"] == topic.key)]
        if len(keep) == len(jobs):
            await self.say(topic, "Usage: /unjob <id> (see /jobs)")
            return
        self.store.data["jobs"] = keep
        self.store.save()
        await self.say(topic, f"🗑 Deleted job #{arg.lstrip('#')}.")

    async def scheduler(self) -> None:
        while True:
            await asyncio.sleep(20)
            now = time.time()
            changed = False
            for job in list(self.store.data["jobs"]):
                if job["next"] > now:
                    continue
                topic = self.topic_for(job["chat_id"], job["thread_id"])
                await self.say(topic, f"⏰ Scheduled job #{job['id']}: {one_line(job['prompt'], 200)}")
                await self.enqueue(topic, f"[Scheduled job #{job['id']}] {job['prompt']}")
                job["next"] = next_daily(job["daily"], datetime.now()) if job.get("daily") else now + job["every"]
                changed = True
            if changed:
                self.store.save()


def main() -> None:
    load_env_file(Path(os.environ.get("ENV_FILE", HERE / ".env")))
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = Config.from_env()
    if shutil.which(cfg.claude_bin) is None:
        sys.exit(f"Claude Code CLI not found ({cfg.claude_bin}). Install it or set CLAUDE_BIN.")
    if not cfg.allowed_users:
        log.warning("ALLOWED_USER_IDS is empty: the bot only tells people their user id. Message it to find yours.")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    bridge = Bridge(cfg, Telegram(cfg.token), Store(cfg.data_dir / "state.json"))
    asyncio.run(bridge.main())


if __name__ == "__main__":
    main()
