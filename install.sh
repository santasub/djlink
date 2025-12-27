#!/bin/bash

# ProDJ Link MIDI Clock Utility - Linux Auto-Install Script
# Supported Platforms: Raspberry Pi OS, Debian, Ubuntu

# Removed set -e to handle optional packages gracefully

REPO_URL="https://github.com/santasub/djlink.git"
INSTALL_DIR="python-prodj-link"

echo "----------------------------------------------------"
echo "  ProDJ Link MIDI Clock Utility Installer"
echo "----------------------------------------------------"

# 1. Update and install system dependencies
echo "[1/5] Installing system dependencies (requires sudo)..."
sudo apt-get update || true
# We try to install multiple backends, as availability varies by OS version
# python3-pyqt5 is very common on older Pi OS, python3-pyside6 is on newer ones.
sudo apt-get install -y \
    python3-venv \
    python3-pip \
    python3-dev \
    git \
    libasound2-dev \
    libxcb-xinerama0 \
    libxcb-cursor0 \
    libxkbcommon-x11-0 \
    libdbus-1-3 \
    libqt5gui5 \
    python3-pyqt5 || echo "Warning: python3-pyqt5 not found"

# Try to install PySide6 as well if available
sudo apt-get install -y python3-pyside6 || echo "Warning: python3-pyside6 not found"

# 2. Clone repository
if [ -d "$INSTALL_DIR" ]; then
    echo "[2/5] Folder $INSTALL_DIR already exists, pulling updates..."
    # cd "$INSTALL_DIR" # We are likely already in it if they pulled it
    git pull || true
else
    echo "[2/5] Cloning repository..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# 3. Setup Virtual Environment
echo "[3/5] Setting up virtual environment..."
python3 -m venv --system-site-packages .venv || true
source .venv/bin/activate

# 4. Install Python dependencies
echo "[4/5] Installing Python dependencies..."
pip install --upgrade pip || true
# CRITICAL: Purge any conflicting rtmidi packages
echo "Cleaning up MIDI library conflicts..."
pip uninstall -y rtmidi python-rtmidi 2>/dev/null || true
# We use requirements.txt which now excludes PyQt5 to avoid build failures on Pi
pip install -r requirements.txt || true
# Force install the correct MIDI library
echo "Installing correct MIDI library..."
pip install --force-reinstall --no-cache-dir python-rtmidi==1.5.8

# 5. Create launch helper
echo "[5/5] Creating locally executable launch script..."
cat <<'EOF' > start_midiclock.sh
#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
source "$DIR/.venv/bin/activate"
python3 "$DIR/midiclock-qt.py" "$@"
EOF
chmod +x start_midiclock.sh

echo "----------------------------------------------------"
echo "Note: Replace 'eth0' with your actual interface (use 'ip a' to check)."
echo "----------------------------------------------------"
