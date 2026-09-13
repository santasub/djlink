#!/bin/bash
# ProDJ Link MIDI Clock - Raspberry Pi Installer
# Tested on: Raspberry Pi OS Bookworm (64-bit), Pi 4 / Pi 5
# Run from the repository folder: bash install_rpi.sh
#
# What this does differently from install.sh:
#   - Installs real-time scheduling tools (chrt/rtkit) for better MIDI timing
#   - Configures ALSA for low-latency output
#   - Optionally sets up a systemd service for autostart on boot
#   - Configures the Qt platform plugin for a DSI/HDMI touchscreen

set -e
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "----------------------------------------------------"
echo "  ProDJ Link MIDI Clock - Raspberry Pi Installer"
echo "----------------------------------------------------"

# 1. System packages
echo "[1/5] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y \
    python3-venv python3-pip python3-dev \
    git libasound2-dev libjack-dev \
    libxcb-xinerama0 libxcb-cursor0 libxkbcommon-x11-0 libdbus-1-3 \
    libgl1-mesa-glx libgles2 \
    python3-pyqt5 python3-pyside6 \
    rtkit schedtool \
    alsa-utils || echo "Warning: some packages failed, continuing..."

# 2. ALSA low-latency config
echo "[2/5] Configuring ALSA for low latency..."
ASOUND_CONF="/etc/asound.conf"
if [ ! -f "$ASOUND_CONF" ]; then
    sudo tee "$ASOUND_CONF" > /dev/null <<'ALSAEOF'
# Low-latency ALSA configuration for MIDI clock
pcm.!default {
    type hw
    card 0
}
ctl.!default {
    type hw
    card 0
}
ALSAEOF
    echo "  Written $ASOUND_CONF"
else
    echo "  $ASOUND_CONF already exists, skipping."
fi

# 3. Python environment
echo "[3/5] Setting up Python environment..."
python3 -m venv --system-site-packages "$REPO_ROOT/.venv"
source "$REPO_ROOT/.venv/bin/activate"
pip install --upgrade pip setuptools wheel -q
pip uninstall -y rtmidi python-rtmidi 2>/dev/null || true
pip install -r "$REPO_ROOT/requirements.txt" -q
pip install --force-reinstall --no-cache-dir python-rtmidi==1.5.8 -q
pip install alsaseq -q || echo "  Note: alsaseq pip install failed (may use system package instead)."

# 4. Launcher scripts
echo "[4/5] Creating launcher scripts..."

# App launcher (called by the device launcher or directly)
cat > "$REPO_ROOT/start_midiclock.sh" <<'LAUNCHEOF'
#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
source "$DIR/.venv/bin/activate"
export QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-xcb}
export QT_QPA_EVDEV_TOUCHSCREEN_PARAMETERS=rotate=0
export QT_SCALE_FACTOR=${QT_SCALE_FACTOR:-1}
exec python3 "$DIR/midiclock-qt.py" --fullscreen "$@"
LAUNCHEOF
chmod +x "$REPO_ROOT/start_midiclock.sh"

# Device launcher (touchscreen menu: Launch / Update / Reboot / Shutdown)
cat > "$REPO_ROOT/start_launcher.sh" <<'DEVEOF'
#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
source "$DIR/.venv/bin/activate"
export QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-xcb}
export QT_QPA_EVDEV_TOUCHSCREEN_PARAMETERS=rotate=0
export QT_SCALE_FACTOR=${QT_SCALE_FACTOR:-1}
exec python3 "$DIR/launcher.py" "$@"
DEVEOF
chmod +x "$REPO_ROOT/start_launcher.sh"

# 5. Autostart the launcher when the Pi OS desktop (X11) comes up
echo "[5/5] Setting up autostart..."

CURRENT_USER="${SUDO_USER:-$USER}"
AUTOSTART_DIR="/home/$CURRENT_USER/.config/autostart"
mkdir -p "$AUTOSTART_DIR"

# XDG .desktop autostart entry — works on Pi OS (LXDE/Wayfire/Labwc)
cat > "$AUTOSTART_DIR/prodj-launcher.desktop" <<DESKTOPEOF
[Desktop Entry]
Type=Application
Name=ProDJ Link Launcher
Comment=ProDJ Link MIDI Clock device launcher
Exec=$REPO_ROOT/start_launcher.sh
X-GNOME-Autostart-enabled=true
NoDisplay=false
Hidden=false
X-GNOME-Autostart-Delay=3
DESKTOPEOF

echo "  Autostart entry written to $AUTOSTART_DIR/prodj-launcher.desktop"
echo "  The launcher will start automatically next time the desktop loads."

# Also write a systemd user service as fallback for headless / Wayfire setups
mkdir -p "/home/$CURRENT_USER/.config/systemd/user"
cat > "/home/$CURRENT_USER/.config/systemd/user/prodj-launcher.service" <<SVCEOF
[Unit]
Description=ProDJ Link MIDI Clock Launcher
After=graphical-session.target
Wants=graphical-session.target

[Service]
Type=simple
WorkingDirectory=$REPO_ROOT
ExecStart=$REPO_ROOT/start_launcher.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
SVCEOF

# Enable the systemd user service (harmless if systemd --user isn't running yet)
systemctl --user daemon-reload 2>/dev/null || true
systemctl --user enable prodj-launcher.service 2>/dev/null || true
echo "  systemd user service also enabled as fallback."

echo "----------------------------------------------------"
echo "  Installation finished!"
echo ""
echo "  The launcher auto-starts when the Pi OS desktop loads."
echo "  From the launcher you can:"
echo "    - Launch the MIDI clock app"
echo "    - Run updates (git pull + pip sync)"
echo "    - Reboot or shut down the device"
echo ""
echo "  Run launcher manually : ./start_launcher.sh"
echo "  Run app directly      : ./start_midiclock.sh [--iface eth0]"
echo "  Interface hint        : ip link show"
echo "----------------------------------------------------"
