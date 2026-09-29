#!/data/data/com.termux/files/usr/bin/bash
# One-shot installer for running claude-telegram 24/7 on an Android phone with Termux.
#
#   curl -fsSL https://raw.githubusercontent.com/nguyenlnp/App/claude/claude-code-telegram-r6k4pv/tools/claude-telegram/install-termux.sh | bash
#
# Private repo? Create a GitHub token with read access and run:
#   GITHUB_TOKEN=ghp_xxx bash -c "$(curl -fsSL -H "Authorization: token ghp_xxx" https://raw.githubusercontent.com/...install-termux.sh)"
#
# Safe to re-run: it updates the code and keeps your .env and sessions.

set -euo pipefail

REPO="${REPO:-nguyenlnp/App}"
BRANCH="${BRANCH:-claude/claude-code-telegram-r6k4pv}"
SUBDIR="tools/claude-telegram"
APP_DIR="${APP_DIR:-$HOME/claude-telegram}"
SERVICE=claude-telegram
SVDIR="${PREFIX:-}/var/service"
FILES=(bot.py .env.example README.md)

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mxx %s\033[0m\n' "$*" >&2; exit 1; }

# Read from the terminal even when this script is piped into bash.
ask() {
    local prompt="$1" var
    read -r -p "$prompt" var </dev/tty
    printf '%s' "$var"
}

[[ "${PREFIX:-}" == *com.termux* ]] || die "Run this inside the Termux app."

say "Installing packages (python, termux-services, jq, curl, nano)"
pkg update -y >/dev/null 2>&1 || true
pkg install -y python termux-services jq curl nano >/dev/null

command -v claude >/dev/null || die "Claude Code CLI not found on PATH. Install it, run 'claude' once to log in, then re-run this script."
say "Claude Code found: $(claude --version 2>/dev/null || echo unknown)"

# Stop an old copy so it does not fight over Telegram updates.
if [ -d "$SVDIR/$SERVICE" ]; then
    sv down "$SERVICE" 2>/dev/null || true
fi

say "Downloading the bridge into $APP_DIR"
mkdir -p "$APP_DIR"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /nonexistent)"
if [ -f "$HERE/bot.py" ] && [ "$HERE" != "$APP_DIR" ]; then
    for f in "${FILES[@]}"; do cp "$HERE/$f" "$APP_DIR/$f"; done
else
    AUTH=()
    [ -n "${GITHUB_TOKEN:-}" ] && AUTH=(-H "Authorization: token $GITHUB_TOKEN")
    for f in "${FILES[@]}"; do
        curl -fsSL "${AUTH[@]}" "https://raw.githubusercontent.com/$REPO/$BRANCH/$SUBDIR/$f" -o "$APP_DIR/$f.new" \
            || die "Could not download $f. If the repo is private, set GITHUB_TOKEN (see the top of this script)."
        mv "$APP_DIR/$f.new" "$APP_DIR/$f"
    done
fi
chmod +x "$APP_DIR/bot.py"
python3 -m py_compile "$APP_DIR/bot.py"

ENV="$APP_DIR/.env"
setenv() { # setenv KEY VALUE: replace or append a line in .env
    local key="$1" value="$2"
    if grep -q "^$key=" "$ENV"; then
        python3 - "$ENV" "$key" "$value" <<'PY'
import sys
path, key, value = sys.argv[1:]
lines = open(path).read().splitlines()
lines = [f"{key}={value}" if l.startswith(key + "=") else l for l in lines]
open(path, "w").write("\n".join(lines) + "\n")
PY
    else
        echo "$key=$value" >>"$ENV"
    fi
}
getenv() { grep "^$1=" "$ENV" 2>/dev/null | head -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }

if [ ! -f "$ENV" ]; then
    cp "$APP_DIR/.env.example" "$ENV"
    chmod 600 "$ENV"
    setenv DEFAULT_CWD "$HOME"
    setenv CLAUDE_BIN "$(command -v claude)"
fi

TOKEN="$(getenv TELEGRAM_BOT_TOKEN)"
if [ -z "$TOKEN" ] || [[ "$TOKEN" == *replace-me* ]]; then
    say "Telegram bot token"
    echo "In Telegram: open @BotFather, send /newbot, and copy the token."
    echo "Also send /setprivacy to @BotFather, choose your bot, and pick Disable."
    TOKEN="$(ask 'Paste the bot token: ')"
    [ -n "$TOKEN" ] || die "No token given."
    setenv TELEGRAM_BOT_TOKEN "$TOKEN"
fi

API="https://api.telegram.org/bot$TOKEN"
BOT_NAME="$(curl -fsS "$API/getMe" | jq -r '.result.username // empty')" \
    || die "Telegram rejected the token. Check it with @BotFather."
[ -n "$BOT_NAME" ] || die "Telegram rejected the token. Check it with @BotFather."
say "Bot is @$BOT_NAME"

if [ -z "$(getenv ALLOWED_USER_IDS)" ]; then
    say "Linking your Telegram account"
    echo "Open https://t.me/$BOT_NAME and send it any message (for example: hi)."
    ask 'Press Enter after you have sent the message... ' >/dev/null
    IDS=""
    for _ in 1 2 3 4 5 6; do
        IDS="$(curl -fsS "$API/getUpdates" | jq -r '[.result[].message.from | select(. != null and .is_bot == false) | .id] | unique | join(",")')"
        [ -n "$IDS" ] && break
        sleep 5
    done
    [ -n "$IDS" ] || die "No message received. Send the bot a message and re-run this script."
    WHO="$(curl -fsS "$API/getUpdates" | jq -r '[.result[].message.from | select(. != null and .is_bot == false) | "\(.id) (\(.first_name // "") @\(.username // "-"))"] | unique | join(", ")')"
    echo "Messages came from: $WHO"
    CONFIRM="$(ask "Allow these account(s) to control Claude on this phone? [y/N] ")"
    [[ "$CONFIRM" =~ ^[Yy] ]] || die "Cancelled. Set ALLOWED_USER_IDS in $ENV yourself and re-run."
    setenv ALLOWED_USER_IDS "$IDS"
    # Mark the linking message as read so the bridge does not send "hi" to Claude.
    LAST="$(curl -fsS "$API/getUpdates" | jq -r '[.result[].update_id] | max // empty')"
    [ -n "$LAST" ] && curl -fsS "$API/getUpdates?offset=$((LAST + 1))&timeout=0" >/dev/null || true
fi

say "Creating the background service"
mkdir -p "$SVDIR/$SERVICE/log" "$PREFIX/var/log/sv/$SERVICE"
cat >"$SVDIR/$SERVICE/run" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
# Started and restarted automatically by runit (termux-services).
export PATH="$PREFIX/bin:\$PATH"
export HOME="$HOME"
export ENV_FILE="$ENV"
export PYTHONUNBUFFERED=1
cd "$APP_DIR"
exec python3 "$APP_DIR/bot.py" 2>&1
EOF
cat >"$SVDIR/$SERVICE/log/run" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
exec svlogd -tt "$PREFIX/var/log/sv/$SERVICE"
EOF
chmod +x "$SVDIR/$SERVICE/run" "$SVDIR/$SERVICE/log/run"

say "Adding the claude-tg command"
cat >"$PREFIX/bin/claude-tg" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# Manage the Claude Code Telegram bridge.
case "\${1:-status}" in
    status)  sv status $SERVICE ;;
    start)   termux-wake-lock; sv up $SERVICE ;;
    stop)    sv down $SERVICE ;;
    restart) sv restart $SERVICE ;;
    logs)    tail -n 50 -f "$PREFIX/var/log/sv/$SERVICE/current" ;;
    config)  \${EDITOR:-nano} "$ENV" && sv restart $SERVICE ;;
    update)  curl -fsSL "https://raw.githubusercontent.com/$REPO/$BRANCH/$SUBDIR/install-termux.sh" | bash ;;
    *) echo "usage: claude-tg [status|start|stop|restart|logs|config|update]" ;;
esac
EOF
chmod +x "$PREFIX/bin/claude-tg"

say "Start on boot (needs the Termux:Boot app)"
mkdir -p "$HOME/.termux/boot"
cat >"$HOME/.termux/boot/start-claude-telegram" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
# Run by Termux:Boot after the phone restarts: keep the CPU awake and start runit services.
termux-wake-lock
. "$PREFIX/etc/profile"
EOF
chmod +x "$HOME/.termux/boot/start-claude-telegram"

say "Starting"
termux-wake-lock || true
# termux-services normally starts runsvdir from a new Termux session; start it now as well.
if ! pgrep -f "runsvdir.*$SVDIR" >/dev/null; then
    if [ -f "$PREFIX/etc/profile.d/start-services.sh" ]; then
        . "$PREFIX/etc/profile.d/start-services.sh"
    else
        (nohup runsvdir "$SVDIR" >/dev/null 2>&1 &)
    fi
fi
for _ in $(seq 1 20); do
    [ -e "$SVDIR/$SERVICE/supervise/ok" ] && break
    sleep 1
done
sv-enable "$SERVICE" >/dev/null 2>&1 || true
sv up "$SERVICE" || die "Could not start the service. Close Termux completely, open it again, and run: claude-tg start"
sleep 6
sv status "$SERVICE" || true
echo
tail -n 5 "$PREFIX/var/log/sv/$SERVICE/current" 2>/dev/null || true

cat <<EOF

✅ Done. Message @$BOT_NAME in Telegram and Claude Code on this phone will answer.

For sub topics: create a Telegram group, turn on Topics, add @$BOT_NAME as an
admin with "Manage topics", then in any topic send: /topic Website | ~/website

Keep it alive on Android (important, do these once):
  1. Settings > Apps > Termux > Battery > Unrestricted (turn off battery optimisation).
  2. Install "Termux:Boot" from the same store you got Termux from (F-Droid or GitHub),
     open it once, so the bridge starts after a reboot.
  3. Android 12+: turn off the "phantom process" killer, otherwise Android may kill
     Claude after a while. On Android 14+: Developer options > "Disable child process
     restrictions". On Android 12-13 from a computer with adb:
       adb shell device_config set_sync_disabled_for_tests persistent
       adb shell device_config put activity_manager max_phantom_processes 2147483647
  4. Keep the Termux notification (it shows "wake lock held").

Manage it with: claude-tg status | logs | restart | stop | config | update
Settings file:  $ENV
EOF
