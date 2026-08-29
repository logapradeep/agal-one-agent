#!/bin/bash
set -e

echo "=== Agal One Agent Installer ==="

# Check for root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root: sudo bash install.sh"
    exit 1
fi

# Install system dependencies
echo "[1/5] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv

# Create install directory
echo "[2/5] Creating install directory..."
mkdir -p /opt/agal-one-agent
mkdir -p /etc/agal-one-agent

# Create virtual environment
echo "[3/5] Setting up Python virtual environment..."
python3 -m venv /opt/agal-one-agent/venv

# Install the agent
echo "[4/5] Installing agal-one-agent..."
/opt/agal-one-agent/venv/bin/pip install --quiet \
    paho-mqtt PyYAML RPi.GPIO gpiozero smbus2 \
    spidev pyserial w1thermsensor 2>/dev/null || true

# Copy agent source
cp -r agal_one_agent /opt/agal-one-agent/
cp setup.py /opt/agal-one-agent/
cd /opt/agal-one-agent
/opt/agal-one-agent/venv/bin/pip install --quiet -e .

# Install systemd service + the managed-OTA rollback verify-timer
echo "[5/5] Installing systemd units (service + OTA verify-timer)..."
SYSTEMD_SRC=/opt/agal-one-agent/systemd
[ -d "$SYSTEMD_SRC" ] || SYSTEMD_SRC=systemd
cp "$SYSTEMD_SRC/agal-one-agent.service" /etc/systemd/system/
cp "$SYSTEMD_SRC/agal-one-agent-ota-verify.service" /etc/systemd/system/
cp "$SYSTEMD_SRC/agal-one-agent-ota-verify.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable agal-one-agent
# The verify-timer is the managed-OTA rollback safety net — runs independently
# so it can recover even when the main service is crash-looping on a bad update.
systemctl enable --now agal-one-agent-ota-verify.timer

echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Copy your config.yaml to /etc/agal-one-agent/config.yaml"
echo "  2. Start the agent: sudo systemctl start agal-one-agent"
echo "  3. Check status: sudo systemctl status agal-one-agent"
echo "  4. View logs: sudo journalctl -u agal-one-agent -f"
