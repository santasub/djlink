#!/bin/bash

# ProDJ Link MIDI Clock - Update Script
# Run this from the repository folder

echo "----------------------------------------------------"
echo "  ProDJ Link MIDI Clock - Updating"
echo "----------------------------------------------------"

# 1. Pull latest code
echo "[1/3] Pulling latest code from Git..."
git pull || { echo "Error: git pull failed. Make sure you are in the repository folder."; exit 1; }

# 2. Refresh Environment
echo "[2/3] Refreshing Python environment..."
# Check if venv exists, if not run install
if [ ! -d ".venv" ]; then
    echo "Virtual environment not found. Running full installation..."
    bash install.sh
    exit 0
fi

source .venv/bin/activate
pip install --upgrade pip setuptools wheel || true
# Clean up potential MIDI conflicts that might have appeared with new code
pip uninstall -y rtmidi python-rtmidi 2>/dev/null || true
pip install -r requirements.txt || true
pip install --force-reinstall --no-cache-dir python-rtmidi==1.5.8 pyalsaseq || true

# 3. Refresh Launcher
echo "[3/3] Refreshing launcher..."
REPO_ROOT=$(pwd)
cat <<EOF > start_midiclock.sh
#!/bin/bash
DIR="\$( cd "\$( dirname "\${BASH_SOURCE[0]}" )" && pwd )"
source "\$DIR/.venv/bin/activate"
python3 "\$DIR/midiclock-qt.py" "\$@"
EOF
chmod +x start_midiclock.sh

echo "----------------------------------------------------"
echo "  Update finished!"
echo "  Run with: ./start_midiclock.sh --iface wlan0"
echo "----------------------------------------------------"
