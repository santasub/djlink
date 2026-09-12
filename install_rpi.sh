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

# 4. Launcher with Qt touchscreen settings
echo "[4/5] Creating launcher..."
cat > "$REPO_ROOT/start_midiclock.sh" <<LAUNCHEOF
#!/bin/bash
DIR="\$( cd "\$( dirname "\${BASH_SOURCE[0]}" )" && pwd )"
source "\$DIR/.venv/bin/activate"

# Qt platform settings for Raspberry Pi touchscreen
export QT_QPA_PLATFORM=\${QT_QPA_PLATFORM:-xcb}    # use xcb under X11; set to 'eglfs' for framebuffer
export QT_QPA_EVDEV_TOUCHSCREEN_PARAMETERS=rotate=0 # adjust if screen is rotated (90/180/270)
export QT_SCALE_FACTOR=\${QT_SCALE_FACTOR:-1}       # set to 2 for HiDPI displays

# Launch (fullscreen by default on Pi)
python3 "\$DIR/midiclock-qt.py" --fullscreen "\$@"
LAUNCHEOF
chmod +x "$REPO_ROOT/start_midiclock.sh"

# 5. Optional systemd service
echo "[5/5] systemd autostart..."
read -r -p "  Install systemd service to autostart on boot? [y/N] " REPLY
if [[ "$REPLY" =~ ^[Yy]$ ]]; then
    CURRENT_USER="${SUDO_USER:-$USER}"
    SERVICE_FILE="/etc/systemd/system/midiclock.service"
    sudo tee "$SERVICE_FILE" > /dev/null <<SVCEOF
[Unit]
Description=ProDJ Link MIDI Clock
After=network-online.target sound.target graphical.target
Wants=network-online.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$REPO_ROOT
ExecStart=$REPO_ROOT/start_midiclock.sh
Restart=on-failure
RestartSec=5
# Give the process slightly elevated scheduling priority
Nice=-5
# Allow the process to request real-time priority for its MIDI thread
AmbientCapabilities=CAP_SYS_NICE

[Install]
WantedBy=graphical.target
SVCEOF
    sudo systemctl daemon-reload
    sudo systemctl enable midiclock.service
    echo "  Service installed and enabled."
    echo "  Start now with:  sudo systemctl start midiclock"
    echo "  View logs with:  journalctl -u midiclock -f"
fi

echo "----------------------------------------------------"
echo "  Installation finished!"
echo "  Run manually:   ./start_midiclock.sh"
echo "  Autostart:      sudo systemctl start midiclock"
echo ""
echo "  Interface tip:  ip link show"
echo "  Pass interface: ./start_midiclock.sh --iface eth0"
echo "----------------------------------------------------"
