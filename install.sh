#!/bin/bash

# ProDJ Link MIDI Clock - Simple Installer
# Run this from the repository folder

echo "----------------------------------------------------"
echo "  ProDJ Link MIDI Clock - Installer"
echo "----------------------------------------------------"

# 1. System Dependencies
echo "[1/4] Installing system dependencies (sudo)..."
sudo apt-get update || true
sudo apt-get install -y \
    python3-venv python3-pip python3-dev \
    git libasound2-dev libjack-dev \
    libxcb-xinerama0 libxcb-cursor0 libxkbcommon-x11-0 libdbus-1-3 \
    libqt5gui5 python3-pyqt5 python3-pyside6 || echo "Warning: Some system packages failed to install."

# 2. Virtual Environment
echo "[2/4] Setting up Python environment..."
python3 -m venv --system-site-packages .venv || true
source .venv/bin/activate

# 3. Python Dependencies
echo "[3/4] Installing Python libraries..."
pip install --upgrade pip setuptools wheel || true
# Clean up MIDI conflicts
pip uninstall -y rtmidi python-rtmidi 2>/dev/null || true
# Install from requirements
pip install -r requirements.txt || true
# Force install correct MIDI
pip install --force-reinstall --no-cache-dir python-rtmidi==1.5.8 alsaseq || echo "Note: alsaseq optional install failed."

# 4. Create Launcher
echo "[4/4] Creating launcher..."
REPO_ROOT=$(pwd)
cat <<EOF > start_midiclock.sh
#!/bin/bash
DIR="\$( cd "\$( dirname "\${BASH_SOURCE[0]}" )" && pwd )"
source "\$DIR/.venv/bin/activate"
python3 "\$DIR/midiclock-qt.py" "\$@"
EOF
chmod +x start_midiclock.sh

echo "----------------------------------------------------"
echo "  Installation finished!"
echo "  Run with: ./start_midiclock.sh --iface wlan0"
echo "----------------------------------------------------"
