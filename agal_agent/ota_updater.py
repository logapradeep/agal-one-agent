"""OTA (Over-The-Air) self-update for the Menvayal agent."""

import logging
import subprocess
import sys
import os

logger = logging.getLogger(__name__)

GITHUB_REPO = "https://github.com/logapradeep/agal-agent.git"
VENV_PIP = os.path.join(sys.prefix, "bin", "pip")


def perform_update(version: str) -> str:
    """Download and install a new agent version, then restart the service.

    Returns a status message on success, raises on failure.
    """
    if not version or not version.startswith("v"):
        version = f"v{version}"

    url = f"git+{GITHUB_REPO}@{version}"
    logger.info("OTA update: installing %s from %s", version, url)

    # Install the new version
    result = subprocess.run(
        [VENV_PIP, "install", "--upgrade", url],
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"pip install failed: {error}")

    logger.info("OTA update: install complete, scheduling restart...")

    # Restart the systemd service (runs in background so we can ack first)
    subprocess.Popen(
        ["sudo", "systemctl", "restart", "agal-agent"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return f"Updated to {version}, restarting..."
