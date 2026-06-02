#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Energia Doméstica — installer
# Run from the project root: ./deploy/install.sh
# ─────────────────────────────────────────────────────────────
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_NAME="energia-domestica"
SERVICE_USER="${SUDO_USER:-$(whoami)}"

echo "╔════════════════════════════════════════════════════════╗"
echo "║  Energia Doméstica — Tuya Local Dashboard              ║"
echo "║  Instalador                                           ║"
echo "╚════════════════════════════════════════════════════════╝"
echo
echo "Project dir: $PROJECT_DIR"
echo "Service user: $SERVICE_USER"
echo

# ── 1. Check Python ──
if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ python3 not found. Install it first."
    exit 1
fi
PYTHON_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "✅ Python $PYTHON_VERSION found"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "⚠️  Python 3.10+ recommended. You have $PYTHON_VERSION"
fi

# ── 2. Create venv ──
VENV_DIR="$PROJECT_DIR/.venv_energia"
if [ ! -d "$VENV_DIR" ]; then
    echo "📦 Creating virtualenv at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi
echo "✅ Virtualenv ready"

# ── 3. Install requirements ──
echo "📦 Installing dependencies..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"
echo "✅ Dependencies installed"

# ── 4. Create data/logs dirs ──
mkdir -p "$PROJECT_DIR/data" "$PROJECT_DIR/logs"
touch "$PROJECT_DIR/data/.gitkeep" "$PROJECT_DIR/logs/.gitkeep"

# ── 5. Check devices.json ──
if [ ! -f "$PROJECT_DIR/data/devices.json" ]; then
    echo
    echo "⚠️  data/devices.json not found!"
    echo "   Copying template..."
    cp "$PROJECT_DIR/src/devices.example.json" "$PROJECT_DIR/data/devices.json"
    echo "   📝 Edit $PROJECT_DIR/data/devices.json with your Tuya credentials before starting the service."
    echo "      See docs/MITM_GUIDE.md to discover your local_key."
    HAS_DEVICES=0
else
    echo "✅ data/devices.json exists"
    HAS_DEVICES=1
fi

# ── 6. Install systemd service ──
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
if [ -w "/etc/systemd/system" ] || [ -n "$SUDO_USER" ]; then
    echo "⚙️  Installing systemd unit to $SERVICE_FILE..."
    sudo tee "$SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=Energia Domestica - Tuya Local Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$PROJECT_DIR/src
ExecStart=$VENV_DIR/bin/python dashboard.py
Restart=on-failure
RestartSec=10
StandardOutput=append:$PROJECT_DIR/logs/service.log
StandardError=append:$PROJECT_DIR/logs/service.err

# Environment
Environment=PYTHONUNBUFFERED=1
# Default: bind to LAN so the dashboard is reachable from phone/tablet.
# Set to 127.0.0.1 if you only want loopback access (more secure).
Environment=ENERGIA_HOST=0.0.0.0
Environment=ENERGIA_PORT=8050

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    echo "✅ Service installed: $SERVICE_FILE"
    echo

    if [ "$HAS_DEVICES" = "1" ]; then
        echo "🚀 Starting service..."
        sudo systemctl enable --now "$SERVICE_NAME"
        sleep 2
        sudo systemctl status "$SERVICE_NAME" --no-pager -l || true
        echo
        echo "✅ Service running on http://localhost:8050"
    else
        echo "⏸️  Service NOT started because devices.json is missing."
        echo "   After editing it, run:  sudo systemctl start $SERVICE_NAME"
    fi
else
    echo "⚠️  Cannot write to /etc/systemd/system. Run with sudo to install the service."
fi

echo
echo "✨ Done!"
echo
echo "Useful commands:"
echo "  sudo systemctl status $SERVICE_NAME"
echo "  sudo journalctl -u $SERVICE_NAME -f"
echo "  tail -f $PROJECT_DIR/logs/service.log"
