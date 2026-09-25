
#!/bin/bash
set -e

# ProDJ Link MIDI Clock - Update Script
# Works on Linux (Raspberry Pi / reTerminal) and macOS.
# Safe to run repeatedly — installs from scratch if .venv is missing.
#
# Usage:
#   bash update.sh            # update from git + refresh venv
#   bash update.sh --iface eth0  # same, then launch with iface flag
#
# ============================================================
# CONFIG — edit these if you forked the repo or use a branch
# ============================================================
GIT_BRANCH="${GIT_BRANCH:-main}"   # branch to pull — overridden by launcher via env
RTMIDI_VERSION="1.5.8"            # pinned python-rtmidi version
# ============================================================

REPO_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$REPO_DIR"

echo "===================================================="
echo "  ProDJ Link MIDI Clock — Update / Install"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "===================================================="

# ── 1. Pull latest code ───────────────────────────────────────────
echo
echo "[1/4] Pulling latest code (branch: $GIT_BRANCH)..."
# Unshallow if needed (shallow clones block pull on some Pi installs)
git fetch --unshallow 2>/dev/null || true
# Fetch ALL branches so the target branch is available even if never checked out
git fetch --all --prune
if git reset --hard origin/"$GIT_BRANCH"; then
    echo "      OK — now at $(git rev-parse --short HEAD) on $GIT_BRANCH."
else
    echo "      ERROR: branch 'origin/$GIT_BRANCH' not found after fetch. Aborting."
    exit 1
fi

# ── 2. System dependencies (Linux only) ──────────────────────────
if [[ "$(uname)" == "Linux" ]]; then
    echo
    echo "[2/4] Installing system packages (may prompt for sudo)..."
    sudo apt-get update -qq || true
    sudo apt-get install -y -qq \
        python3-venv python3-pip python3-dev \
        git libasound2-dev libjack-dev \
        libxcb-xinerama0 libxcb-cursor0 libxkbcommon-x11-0 \
        libdbus-1-3 || true
else
    echo
    echo "[2/4] macOS detected — skipping system packages."
fi

# ── 3. Python virtual environment ────────────────────────────────
echo
echo "[3/4] Setting up Python environment..."

if [ ! -d ".venv" ]; then
    echo "      Creating new virtual environment..."
    python3 -m venv --system-site-packages .venv
fi

# Activate
# shellcheck disable=SC1091
source .venv/bin/activate

pip install --upgrade pip setuptools wheel -q

# Remove conflicting rtmidi packages
pip uninstall -y rtmidi python-rtmidi 2>/dev/null || true

# Install requirements
pip install -r requirements.txt -q || true

# Force-install the correct rtmidi version
pip install --force-reinstall --no-cache-dir python-rtmidi=="$RTMIDI_VERSION" -q || true

# ALSA sequencer (Linux only, optional)
if [[ "$(uname)" == "Linux" ]]; then
    pip install alsaseq -q || echo "      Note: alsaseq optional, skipping."
fi

echo "      Python environment ready."

# ── 4. Refresh launcher scripts ───────────────────────────────────
echo
echo "[4/4] Writing launcher scripts..."

# Main app launcher
cat > start_midiclock.sh << 'LAUNCHER'
#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
source "$DIR/.venv/bin/activate"
exec python3 "$DIR/midiclock-qt.py" "$@"
LAUNCHER
chmod +x start_midiclock.sh

# Device launcher (reTerminal touchscreen menu)
cat > start_launcher.sh << 'DEVLAUNCHER'
#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Ensure Qt reaches the local X display from both desktop terminal and SSH
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/home/pi/.Xauthority}"

source "$DIR/.venv/bin/activate"
exec python3 "$DIR/launcher.py" "$@"
DEVLAUNCHER
chmod +x start_launcher.sh

# systemd service file (written but not enabled automatically)
cat > prodj-launcher.service << SYSTEMD
[Unit]
Description=ProDJ Link MIDI Clock Launcher
After=graphical.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/start_launcher.sh
Restart=on-failure
RestartSec=5
Environment=DISPLAY=:0
Environment=XAUTHORITY=/home/$USER/.Xauthority

[Install]
WantedBy=graphical.target
SYSTEMD

echo
echo "===================================================="
echo "  Update complete!"
echo
echo "  Start app     : ./start_midiclock.sh [--iface eth0]"
echo "  Device launcher: ./start_launcher.sh"
echo
echo "  To auto-start launcher on boot:"
echo "    sudo cp prodj-launcher.service /etc/systemd/system/"
echo "    sudo systemctl enable --now prodj-launcher.service"
echo "===================================================="

# If an iface argument was passed, launch the app immediately
if [[ -n "$1" ]]; then
    echo
    echo "Launching with $1..."
    exec ./start_midiclock.sh "$@"
fi
