#!/bin/bash
# Container entrypoint.
# 1. Render /etc/agal-agent/config.yaml from balena environment variables.
# 2. Hand off to the agent process under PID 1.
set -euo pipefail

CONFIG_PATH="${CONFIG_PATH:-/etc/agal-agent/config.yaml}"

echo "[agal-agent] Rendering config to ${CONFIG_PATH}"
/opt/agal-agent/scripts/render_config.py "${CONFIG_PATH}"

# Validate required env vars early so we fail loud, not silent.
: "${NODE_UID:?NODE_UID must be set as a balena device variable}"
: "${AUTH_TOKEN:?AUTH_TOKEN must be set as a balena device variable}"
: "${MQTT_BROKER:?MQTT_BROKER must be set as a balena device or fleet variable}"

echo "[agal-agent] Starting agent (node=${NODE_UID})"
exec /usr/local/bin/agal-agent --config "${CONFIG_PATH}"
