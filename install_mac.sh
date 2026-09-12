#!/bin/bash
# ProDJ Link MIDI Clock - macOS Installer
# Requires: Homebrew (https://brew.sh)
# Run from the repository folder: bash install_mac.sh

set -e
echo "----------------------------------------------------"
echo "  ProDJ Link MIDI Clock - macOS Installer"
echo "----------------------------------------------------"

# 1. Homebrew check
if ! command -v brew &>/dev/null; then
    echo "ERROR: Homebrew not found. Install it first:"
    echo "  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    exit 1
fi

# 2. System dependencies via Homebrew
echo "[1/4] Installing system dependencies via Homebrew..."
brew install python3 || true
# python-rtmidi needs portmidi or the CoreMIDI headers (Xcode CLT)
if ! xcode-select -p &>/dev/null; then
    echo "  Installing Xcode Command Line Tools (needed to build python-rtmidi)..."
    xcode-select --install || true
    echo "  Re-run this script after the Xcode CLT installation completes."
    exit 1
fi

# 3. Virtual Environment
echo "[2/4] Setting up Python virtual environment..."
python3 -m venv .venv
source .venv/bin/activate

# 4. Python Dependencies
echo "[3/4] Installing Python libraries..."
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
# python-rtmidi on macOS uses CoreMIDI — no extra system libs needed
pip install --force-reinstall --no-cache-dir python-rtmidi==1.5.8
# alsaseq is Linux-only, skip silently
pip install alsaseq 2>/dev/null || true

# 5. Launcher
echo "[4/4] Creating launcher..."
cat > start_midiclock.sh <<'EOF'
#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
source "$DIR/.venv/bin/activate"
python3 "$DIR/midiclock-qt.py" "$@"
EOF
chmod +x start_midiclock.sh

# Optional: Qt platform plugin hint for macOS
# PySide6/PyQt6 on macOS sometimes needs this:
echo 'export QT_MAC_WANTS_LAYER=1' >> .venv/bin/activate

echo "----------------------------------------------------"
echo "  Installation finished!"
echo "  Run with: ./start_midiclock.sh --iface en0"
echo ""
echo "  Tip: to find your interface name, run:  ifconfig | grep 'inet '"
echo "----------------------------------------------------"
