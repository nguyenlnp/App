# claude-telegram

Chat with **Claude Code** from Telegram. Each **forum topic** in your group is its own Claude Code session with its own project folder. The bridge runs 24/7 as a background service, like Hermes Agent's messaging gateway.

```
Telegram group "Claude HQ" (Topics on)
├── # General        → session A, ~/code
├── # Website        → session B, ~/code/website
├── # Translations   → session C, ~/work/lqa-2026
└── # Daily brief    → session D, runs "/daily 08:00 …" by itself
```

- **One topic = one session.** Topics run in parallel. Messages inside a topic are queued in order.
- **Survives restarts.** Session ids are saved in `~/.claude-telegram/state.json` and resumed with `claude --resume`, so after a reboot you continue where you stopped.
- **Live progress.** A single quiet status message shows the tools Claude is running (`🔧 Bash: npm test`), then the final answer is formatted for Telegram. Very long answers are attached as `reply.md`.
- **Files and photos.** Anything you send is downloaded and its path is passed to Claude.
- **Scheduled prompts.** `/every 2h …` and `/daily 08:30 …` run on their own and post into the topic.
- **Locked to you.** Only the Telegram user ids in `ALLOWED_USER_IDS` can use it.
- **Nothing to install** apart from Python 3.10+ and the Claude Code CLI. Standard library only.

## Setup (about 10 minutes)

### 1. Machine

Use a machine that stays on: a small VPS, a home server, or your Mac with sleep disabled. Install and log in to Claude Code there:

```bash
npm install -g @anthropic-ai/claude-code   # or the native installer
claude          # log in once (subscription or API key), then exit
```

Don't run the bridge as root: Claude Code refuses `bypassPermissions` as root, and a normal user limits damage anyway.

### 2. Bot

1. In Telegram, open **@BotFather**, send `/newbot`, and copy the token.
2. Still in BotFather: `/setprivacy` → choose your bot → **Disable**. Without this the bot only sees /commands in groups.

### 3. Group with topics

1. Create a group, then open **Group settings → Topics → On**. This makes it a forum supergroup.
2. Add your bot and make it an **admin** with **Manage topics** turned on. It needs this for `/topic`.

A private chat with the bot works too. It's a single session unless you enable threads for the bot in BotFather.

### 4. Install and configure

```bash
git clone … && cp -r tools/claude-telegram ~/claude-telegram   # or copy the folder any way you like
cd ~/claude-telegram
cp .env.example .env
nano .env        # set TELEGRAM_BOT_TOKEN and DEFAULT_CWD
python3 bot.py   # first run in the foreground
```

Message the bot in private. It answers `Your Telegram user id is 123456`. Put that number in `ALLOWED_USER_IDS` in `.env`, then stop the bot (Ctrl+C) and start it again.

### 5. Keep it running (the "Hermes" part)

**Linux (systemd):**

```bash
mkdir -p ~/.config/systemd/user
cp deploy/claude-telegram.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-telegram
sudo loginctl enable-linger "$USER"        # keep running after logout and start at boot
journalctl --user -u claude-telegram -f    # logs
```

**macOS (launchd):** edit `YOUR_USER` in `deploy/com.claude.telegram.plist`, then:

```bash
cp deploy/com.claude.telegram.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.claude.telegram.plist
tail -f /tmp/claude-telegram.log
```

**Quick and dirty:** `tmux new -s claude 'python3 ~/claude-telegram/bot.py'`, then detach with Ctrl+B D.

## Using it

Type normally in a topic. These commands work in any topic:

| Command | What it does |
|---|---|
| `/topic Website \| ~/code/website` | Create a new topic bound to a folder (new session) |
| `/cwd ~/code/app` | Change this topic's folder (starts a new session) |
| `/new` | Fresh session, same folder. `/new <prompt>` starts it with a prompt |
| `/stop` | Interrupt the running task and clear the queue |
| `/status` | Folder, session id, model, mode, queue, scheduled jobs |
| `/model opus` · `/model default` | Model for this topic |
| `/mode auto` | Permission mode for this topic (see below) |
| `/resume <session-id>` | Continue a session you started in the terminal (same folder) |
| `/every 2h check CI and tell me if anything broke` | Recurring prompt |
| `/daily 08:30 summarise my unread GitHub notifications` | Daily prompt, server's local time |
| `/jobs` · `/unjob 3` | List and delete scheduled prompts |

Any other `/command`, for example a Claude Code skill or custom slash command, is sent to Claude as is. Replying to an earlier message includes that message as context.

## Permissions: choose carefully

Nobody can press "Allow" from Telegram, so each run uses a fixed permission mode (`PERMISSION_MODE`, or `/mode` per topic):

- `acceptEdits` (default): Claude can read and edit files. Commands and other tools run only if they're listed in `ALLOWED_TOOLS`, e.g. `"Bash(git diff:*) Bash(npm run test:*) WebFetch"`. Anything else is denied, and Claude tells you what it needed.
- `auto`: a safety classifier approves routine actions and blocks risky ones. This is a good balance for a personal agent.
- `plan`: read-only. Claude proposes a plan and changes nothing.
- `bypassPermissions`: no checks at all. Use it only on a disposable VM or container that holds nothing you care about.

Anyone who gets into your Telegram account can drive this machine. Turn on Telegram 2-step verification, keep `ALLOWED_USER_IDS` to yourself, and keep `.env` private.

## Notes

- Sessions are stored per folder by Claude Code. That's why `/cwd` starts a new session, and why `/resume` only works for sessions created in the topic's folder.
- Telegram bots can only download files up to 20 MB.
- `/status` shows an API-equivalent cost. On a Pro/Max subscription that number is informational only.
- Tests (offline, using a fake `claude`): `python3 -m unittest discover -s tests`.
