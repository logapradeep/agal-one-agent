"""Managed OTA (Over-The-Air) self-update for the Agal One agent, with
health-gated rollback.

The hard problem: the process that triggers an update *dies* when it restarts
the service, so rollback cannot live in that process. This module solves it with
an on-disk state marker plus an INDEPENDENT systemd verify-timer.

Flow (see Agal/contracts/enums.yaml#OtaStatus + firmware-rollout.schema.json):

  1. perform_update(target, ...) — record {previous, target, deadline} to
     ota_state.json, emit firmware_update_started, `pip install` the target, and
     `systemctl restart`. The OLD process dies here.
  2. The NEW process boots in probation. main.py, on seeing phase=pending_verify,
     waits PROBATION_WINDOW_S of healthy uptime (config loaded + MQTT connected)
     then calls confirm_update() → phase=idle, emit firmware_update_succeeded.
  3. An INDEPENDENT systemd verify-timer runs run_verify() every ~60s. If a
     pending_verify update is past its deadline (the new version crash-looped,
     hung, or never connected), it reinstalls `previous`, emits
     firmware_update_rolled_back, and restarts. This is the safety net that
     survives a daemon that never comes back up.

Firmware lifecycle events are reported over HTTP (http_reporter), NOT MQTT, so
they land even while the daemon is down / MQTT is mid-reconnect.
"""

import json
import logging
import os
import subprocess
import sys
import time
from importlib.metadata import version as pkg_version, PackageNotFoundError

logger = logging.getLogger(__name__)

GITHUB_REPO = "https://github.com/logapradeep/agal-one-agent.git"
VENV_PIP = os.path.join(sys.prefix, "bin", "pip")
SERVICE_NAME = "agal-one-agent"

STATE_DIR = os.environ.get("AGAL_ONE_AGENT_STATE_DIR", "/var/lib/agal-one-agent")
STATE_FILE = os.path.join(STATE_DIR, "ota_state.json")

# The new version must stay healthy this long (seconds) before self-confirming.
PROBATION_WINDOW_S = int(os.environ.get("AGAL_ONE_AGENT_OTA_PROBATION_S", "60"))
# The verify-timer rolls back if still unconfirmed this long after perform_update.
# MUST be > PROBATION_WINDOW_S so a healthy version confirms first.
VERIFY_DEADLINE_S = int(os.environ.get("AGAL_ONE_AGENT_OTA_DEADLINE_S", "150"))

PIP_TIMEOUT_S = 300


# ---- version helpers -------------------------------------------------------

def _norm_tag(version: str) -> str:
    """Normalize to a GitHub release tag form (vX.Y.Z)."""
    v = (version or "").strip()
    return v if v.startswith("v") else f"v{v}"


def installed_version() -> str:
    """Currently-installed package version as a tag (vX.Y.Z). Falls back to v0.0.0."""
    try:
        return _norm_tag(pkg_version("agal-one-agent"))
    except (PackageNotFoundError, Exception):  # noqa: BLE001
        return "v0.0.0"


# ---- state file (atomic) ---------------------------------------------------

def read_state() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f) or {"phase": "idle"}
    except FileNotFoundError:
        return {"phase": "idle"}
    except Exception as e:  # noqa: BLE001
        logger.warning("OTA: could not read state (%s); treating as idle", e)
        return {"phase": "idle"}


def write_state(state: dict) -> None:
    state["updatedEpoch"] = int(time.time())
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)  # atomic on POSIX
    except Exception as e:  # noqa: BLE001
        logger.error("OTA: failed to persist state: %s", e)


# ---- low-level ops (patched out in tests) ----------------------------------

def _pip_install(tag: str) -> "subprocess.CompletedProcess":
    """Install a specific tag, force-reinstalling so the exact ref lands even if
    pip thinks the version is already current. Deps come along (not --no-deps) so
    a release that adds a dependency still works."""
    url = f"git+{GITHUB_REPO}@{tag}"
    logger.info("OTA: pip install %s", url)
    return subprocess.run(
        [VENV_PIP, "install", "--upgrade", "--force-reinstall", url],
        capture_output=True, text=True, timeout=PIP_TIMEOUT_S,
    )


def _restart_service() -> None:
    logger.info("OTA: restarting %s", SERVICE_NAME)
    subprocess.Popen(
        ["sudo", "systemctl", "restart", SERVICE_NAME],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _emit(phase: str, state: dict, *, error: str = None) -> None:
    """Report a firmware lifecycle event over HTTP. Best-effort — a reporting
    failure must never crash an update/rollback."""
    node_uid = state.get("nodeUid")
    if not node_uid:
        logger.debug("OTA: no nodeUid in state; skipping %s report", phase)
        return
    try:
        from .http_reporter import HttpReporter
        # Best-effort auth token from the on-disk config (ADR-013 P0.5 —
        # telemetryIngress verifies it once enforcement is on). OTA events must
        # still go out even if the config is unreadable mid-update, so failures
        # fall back to an unauthenticated report.
        auth_token = ""
        try:
            import os
            from .config import AgentConfig
            cfg_path = os.environ.get(
                "AGAL_ONE_AGENT_CONFIG", "/etc/agal-one-agent/config.yaml")
            auth_token = AgentConfig.from_yaml(cfg_path).node.auth_token
        except Exception:  # noqa: BLE001
            logger.debug("OTA: could not load auth token for %s report", phase)
        HttpReporter(node_uid, auth_token=auth_token).report_firmware_event(
            phase=phase,
            from_version=state.get("previous"),
            to_version=state.get("target"),
            command_id=state.get("commandId"),
            rollout_id=state.get("rolloutId"),
            error=error,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("OTA: firmware event report failed (%s): %s", phase, e)


# ---- public state machine --------------------------------------------------

def perform_update(target_version: str, command_id: str = "", rollout_id: str = "",
                   node_uid: str = "") -> str:
    """Install target_version and restart into probation. Returns a status string.

    On pip failure, reinstalls the known-good `previous` so the next restart
    still loads working code, and marks the update failed (no restart).
    """
    target = _norm_tag(target_version)
    previous = installed_version()
    state = {
        "phase": "pending_verify",
        "previous": previous,
        "target": target,
        "deadlineEpoch": int(time.time()) + VERIFY_DEADLINE_S,
        "commandId": command_id,
        "rolloutId": rollout_id,
        "nodeUid": node_uid,
    }

    if previous == target:
        logger.info("OTA: already on %s; no-op", target)
        write_state({"phase": "idle", "previous": target, "target": target,
                     "commandId": command_id, "rolloutId": rollout_id, "nodeUid": node_uid})
        _emit("succeeded", state)
        return f"Already on {target}"

    write_state(state)
    _emit("started", state)

    result = _pip_install(target)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:500]
        logger.error("OTA: install of %s failed: %s", target, err)
        _pip_install(previous)  # restore known-good (best effort)
        failed = {**state, "phase": "failed", "error": err}
        write_state(failed)
        _emit("failed", state, error=err)
        return f"Install of {target} failed; restored {previous}: {err}"

    logger.info("OTA: %s installed; restarting into probation (deadline %ds)",
                target, VERIFY_DEADLINE_S)
    _restart_service()
    return f"Installing {target}; restarting into probation"


def confirm_update(node_uid: str = "") -> bool:
    """Called by the daemon after PROBATION_WINDOW_S of healthy uptime. Settles a
    pending_verify update to idle (new known-good) and reports success.
    Returns True if a probation was confirmed, False if nothing to do."""
    state = read_state()
    if state.get("phase") != "pending_verify":
        return False
    target = state.get("target")
    logger.info("OTA: probation passed; confirming %s as known-good", target)
    write_state({
        "phase": "idle",
        "previous": target,            # the target is now the known-good baseline
        "target": target,
        "commandId": state.get("commandId"),
        "rolloutId": state.get("rolloutId"),
        "nodeUid": state.get("nodeUid") or node_uid,
    })
    _emit("succeeded", state)
    return True


def run_verify() -> str:
    """Independent systemd-timer entry. Rolls back an unconfirmed update once it
    is past its deadline. No-op for any non-probation state."""
    state = read_state()
    if state.get("phase") != "pending_verify":
        return f"noop (phase={state.get('phase')})"

    now = int(time.time())
    deadline = int(state.get("deadlineEpoch", 0))
    if now < deadline:
        return f"in probation ({deadline - now}s left)"

    previous = state.get("previous")
    target = state.get("target")
    logger.warning("OTA verify: %s did not confirm by deadline; rolling back to %s",
                   target, previous)

    result = _pip_install(previous)
    rolled = {
        "phase": "rolled_back",
        "previous": previous,
        "target": target,
        "commandId": state.get("commandId"),
        "rolloutId": state.get("rolloutId"),
        "nodeUid": state.get("nodeUid"),
    }
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:500]
        logger.error("OTA verify: rollback reinstall of %s failed: %s", previous, err)
        rolled["phase"] = "failed"
        rolled["error"] = err
        write_state(rolled)
        _emit("failed", state, error=err)
        _restart_service()
        return f"rollback FAILED: {err}"

    write_state(rolled)
    _emit("rolled_back", state)
    _restart_service()
    return f"rolled back {target} -> {previous}"


def main_verify() -> None:
    """Console entry for the systemd verify-timer (agal-one-agent-ota-verify)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [ota-verify] %(levelname)s: %(message)s",
    )
    try:
        outcome = run_verify()
        logger.info("OTA verify: %s", outcome)
    except Exception as e:  # noqa: BLE001 — the timer must never hard-fail
        logger.error("OTA verify crashed (non-fatal): %s", e)


if __name__ == "__main__":
    main_verify()
