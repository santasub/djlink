#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Ensure Qt reaches the local X display from both desktop terminal and SSH
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/home/pi/.Xauthority}"

source "$DIR/.venv/bin/activate"
exec python3 "$DIR/launcher.py" "$@"
