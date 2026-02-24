#!/bin/bash
# Aircon Local Control — Setup & Start

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# Create venv if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# Activate and install
source "$VENV_DIR/bin/activate"
pip install -q -r "$SCRIPT_DIR/requirements.txt"

# Get local IP for display
LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║       Aircon Local Control           ║"
echo "  ║                                      ║"
echo "  ║  Open on your iPhone:                ║"
echo "  ║  http://$LOCAL_IP:8082               ║"
echo "  ║                                      ║"
echo "  ║  Add to Home Screen for app feel     ║"
echo "  ║  Tap Share > Add to Home Screen      ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

# Run
cd "$SCRIPT_DIR"
python3 app.py
