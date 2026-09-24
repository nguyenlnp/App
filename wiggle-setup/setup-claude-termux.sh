#!/data/data/com.termux/files/usr/bin/bash
# Wiggle + Claude Code setup for Termux (Android).
#
# Claude Code ships no Android build, so this runs it inside an Ubuntu
# environment (proot-distro) with your Wiggle folder shared into it.
#
# What it does (safe to re-run; finished steps are skipped):
#   1. Finds your Wiggle folder (or uses the path you pass in)
#   2. Backs it up: a .tar.gz copy + a git commit and tag of the current state
#   3. Removes the broken npm install of Claude Code
#   4. Installs proot-distro + Ubuntu, then Claude Code inside Ubuntu
#   5. Creates the `wiggle-claude` command to start Claude in the Wiggle folder
#
# Usage:  bash setup-claude-termux.sh [path/to/wiggle]

set -euo pipefail

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

[ -n "${PREFIX:-}" ] && [ -d "$PREFIX" ] && case "$PREFIX" in *com.termux*) true ;; *) false ;; esac \
    || die "Run this inside Termux, not inside Ubuntu/proot."

DISTRO=ubuntu
PD_DIR="$PREFIX/var/lib/proot-distro"
LAUNCHER="$PREFIX/bin/wiggle-claude"
CONFIG="$HOME/.wiggle-claude.conf"

# ---------------------------------------------------------------- 1. find Wiggle
say "Locating the Wiggle project"
WIGGLE_DIR="${1:-}"
if [ -z "$WIGGLE_DIR" ] && [ -f "$CONFIG" ]; then
    WIGGLE_DIR="$(cat "$CONFIG")"
fi
if [ -z "$WIGGLE_DIR" ]; then
    mapfile -t CANDIDATES < <(find "$HOME" "$HOME/storage/shared" -maxdepth 4 -type d -iname '*wiggle*' \
        -not -path '*/node_modules/*' -not -path '*/.git/*' -not -path '*/installed-rootfs/*' 2>/dev/null | sort -u)
    if [ "${#CANDIDATES[@]}" -eq 1 ]; then
        WIGGLE_DIR="${CANDIDATES[0]}"
    elif [ "${#CANDIDATES[@]}" -gt 1 ]; then
        echo "Found several folders:"
        for i in "${!CANDIDATES[@]}"; do echo "  $((i + 1))) ${CANDIDATES[$i]}"; done
        read -rp "Pick the Wiggle project folder [1-${#CANDIDATES[@]}]: " pick
        [[ "$pick" =~ ^[0-9]+$ ]] && [ "$pick" -ge 1 ] && [ "$pick" -le "${#CANDIDATES[@]}" ] || die "Invalid choice."
        WIGGLE_DIR="${CANDIDATES[$((pick - 1))]}"
    else
        read -rp "Couldn't find it automatically. Full path to the Wiggle folder: " WIGGLE_DIR
    fi
fi
WIGGLE_DIR="${WIGGLE_DIR/#\~/$HOME}"
[ -d "$WIGGLE_DIR" ] || die "Folder not found: $WIGGLE_DIR"
WIGGLE_DIR="$(cd "$WIGGLE_DIR" && pwd)"
echo "$WIGGLE_DIR" > "$CONFIG"
echo "Using: $WIGGLE_DIR"

# ---------------------------------------------------------------- 2. packages
say "Installing Termux packages (git, proot-distro)"
yes | pkg update -y >/dev/null 2>&1 || warn "pkg update had warnings; continuing"
pkg install -y git proot-distro tar

# ---------------------------------------------------------------- 3. backup
say "Backing up Wiggle before any changes"
BACKUP_DIR="$HOME/wiggle-backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
tar -czf "$BACKUP_DIR/wiggle-$STAMP.tar.gz" -C "$(dirname "$WIGGLE_DIR")" \
    --exclude='node_modules' --exclude='.gradle' "$(basename "$WIGGLE_DIR")"
echo "Archive: $BACKUP_DIR/wiggle-$STAMP.tar.gz"

cd "$WIGGLE_DIR"
git config --global --add safe.directory "$WIGGLE_DIR" 2>/dev/null || true
if [ ! -d .git ]; then
    git init -q
    git symbolic-ref HEAD refs/heads/main
fi
if [ ! -f .gitignore ]; then
    printf '%s\n' 'node_modules/' '.gradle/' 'build/' 'dist/' '.expo/' '.env' '.env.*' > .gitignore
    echo "Created a basic .gitignore (node_modules, build output, .env secrets)"
fi
git config user.name  >/dev/null 2>&1 || git config user.name  "Wiggle Dev"
git config user.email >/dev/null 2>&1 || git config user.email "wiggle@localhost"
git add -A
if git diff --cached --quiet && git rev-parse -q --verify HEAD >/dev/null; then
    echo "No uncommitted changes; current commit already saved."
else
    git commit -q --allow-empty -m "Snapshot before Claude Code handoff ($STAMP)"
    echo "Committed current state."
fi
git tag -f "pre-claude-$STAMP" >/dev/null
echo "Tagged as pre-claude-$STAMP  (restore any time: git checkout pre-claude-$STAMP)"

# ---------------------------------------------------------------- 4. remove broken install
if command -v npm >/dev/null 2>&1 && npm ls -g @anthropic-ai/claude-code >/dev/null 2>&1; then
    say "Removing the Termux npm install of Claude Code (it can't run on Android)"
    npm uninstall -g @anthropic-ai/claude-code || warn "Couldn't uninstall; harmless, continuing"
fi

# ---------------------------------------------------------------- 5. Ubuntu + Claude
# Some networks block Docker Hub (proot-distro's default source), so try
# mirrors of the same official image, then Ubuntu's own base tarball.
UBUNTU_SOURCES=(
    "ubuntu:24.04"
    "mirror.gcr.io/library/ubuntu:24.04"
    "public.ecr.aws/docker/library/ubuntu:24.04"
    "https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/ubuntu-base-24.04.5-base-arm64.tar.gz"
)
if [ -d "$PD_DIR/containers/$DISTRO" ] || [ -d "$PD_DIR/installed-rootfs/$DISTRO" ]; then
    say "Ubuntu already installed"
else
    say "Installing Ubuntu (one-time download, about 30-100 MB)"
    installed=0
    for src in "${UBUNTU_SOURCES[@]}"; do
        echo "Trying: $src"
        if proot-distro install --name "$DISTRO" "$src"; then
            installed=1
            break
        fi
        warn "That source failed; trying the next one"
        proot-distro remove "$DISTRO" >/dev/null 2>&1 || true
    done
    [ "$installed" -eq 1 ] || die "Couldn't download Ubuntu from any source. Your network is blocking them;
    turn on a VPN (e.g. the free Cloudflare 1.1.1.1 app) and re-run: bash setup.sh"
fi

say "Installing Claude Code inside Ubuntu"
proot-distro login "$DISTRO" -- /bin/bash -c '
    set -e
    export DEBIAN_FRONTEND=noninteractive
    if ! command -v curl >/dev/null || ! command -v git >/dev/null; then
        apt-get update -y
        apt-get install -y curl git ca-certificates
    fi
    git config --global --add safe.directory "*"
    if [ ! -x "$HOME/.local/bin/claude" ]; then
        if ! { curl -fsSL --retry 3 -o /tmp/claude-install.sh https://claude.ai/install.sh \
                && bash /tmp/claude-install.sh; }; then
            # Fallback: the same native binary, published on the npm registry
            echo "Official installer failed; downloading Claude Code from npm instead"
            ver=$(curl -fsSL --retry 3 https://registry.npmjs.org/@anthropic-ai/claude-code-linux-arm64/latest \
                | grep -o "\"version\":\"[^\"]*\"" | head -1 | cut -d\" -f4)
            [ -n "$ver" ] || { echo "Could not reach the npm registry either"; exit 1; }
            tmp=$(mktemp -d)
            curl -fL --retry 3 -o "$tmp/c.tgz" \
                "https://registry.npmjs.org/@anthropic-ai/claude-code-linux-arm64/-/claude-code-linux-arm64-$ver.tgz"
            tar -xzf "$tmp/c.tgz" -C "$tmp"
            mkdir -p "$HOME/.local/bin"
            install -m 755 "$tmp/package/claude" "$HOME/.local/bin/claude"
            rm -rf "$tmp"
        fi
    fi
    grep -q ".local/bin" "$HOME/.bashrc" 2>/dev/null || echo "export PATH=\"\$HOME/.local/bin:\$PATH\"" >> "$HOME/.bashrc"
    "$HOME/.local/bin/claude" --version
'

# ---------------------------------------------------------------- 6. launcher
say "Creating the wiggle-claude command"
cat > "$LAUNCHER" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Starts Claude Code (inside Ubuntu) in the Wiggle project folder.
# Extra arguments go to claude, e.g.:  wiggle-claude remote-control
WIGGLE_DIR="$(cat "$HOME/.wiggle-claude.conf" 2>/dev/null)"
[ -d "$WIGGLE_DIR" ] || { echo "Wiggle folder not set. Re-run setup-claude-termux.sh"; exit 1; }
exec proot-distro login ubuntu --bind "$WIGGLE_DIR:/root/wiggle" -- \
    /bin/bash -lc 'cd /root/wiggle && exec "$HOME/.local/bin/claude" "$@"' wiggle-claude "$@"
EOF
chmod +x "$LAUNCHER"

say "Done!"
cat <<EOF
Backup archive : $BACKUP_DIR/wiggle-$STAMP.tar.gz
Git snapshot   : tag pre-claude-$STAMP in $WIGGLE_DIR

Start Claude in Wiggle:        wiggle-claude
Control it from the Claude app: wiggle-claude remote-control

First time, log in with your Claude account, then ask:
  "Check Wiggle's progress (latest v0.1.3), summarise where it stopped, and continue.
   Commit before any drastic change."
EOF
