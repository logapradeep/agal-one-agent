"""HTTP reporter for sending telemetry and status to the Agal One backend."""

import json
import logging
import time
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger(__name__)

# Fallback only — the backend supplies the live endpoint via config.yaml
# (telemetry.ingress_url). These defaults must track the current backend region:
# the whole agal-one backend is in asia-south1 (the us-central1 deployment was
# retired). A stale default here is what silently kept nodes offline before
# ingress_url became config-driven.
TELEMETRY_INGRESS_URL = "https://asia-south1-agal-one-prod.cloudfunctions.net/telemetryIngress"
BOOT_STATE_URL = "https://asia-south1-agal-one-prod.cloudfunctions.net/getBootState"


class HttpReporter:
    """Posts telemetry, status, and command acks to the backend HTTP endpoint.

    v0.1.7 (ADR-013 P0.5): every POST carries ``Authorization: Bearer
    <auth_token>`` — the node's 32-byte authToken from config.yaml (the same
    credential used as the MQTT password and by reportBoard/getBootState).
    telemetryIngress currently runs in accept-and-warn mode for tokenless
    requests (TELEMETRY_AUTH_ENFORCE=false) so daemons <= v0.1.6 keep working;
    the backend flips to enforcement once the fleet is on >= v0.1.7.
    """

    def __init__(self, node_uid: str, auth_token: str = "",
                 base_url: str = ""):
        self.node_uid = node_uid
        self.auth_token = auth_token
        # Prefer the backend-supplied URL (config.telemetry.ingress_url); fall
        # back to the module default when the config omits it (older configs or
        # a bare manual install).
        self.base_url = base_url or TELEMETRY_INGRESS_URL
        self.last_status: Optional[int] = None  # HTTP status of the last POST (429 → callers back off)

    def report_status(self, online: bool, uptime: int, firmware_version: str = "0.1.0",
                      extra: Optional[dict] = None) -> None:
        payload = {
            "nodeUid": self.node_uid,
            "online": online,
            "uptime": uptime,
            "firmwareVersion": firmware_version,
        }
        from .net_info import get_primary_mac
        mac = get_primary_mac()
        if mac:
            payload["mac"] = mac
        if extra:
            payload.update(extra)  # programVersion / programStatus / capabilities (v0.2.0)
        self._post({"type": "status", "payload": payload})

    # ---- Automation blocks (ADR-017, contracts v1.5.0) -----------------------

    def _sibling_url(self, function_name: str) -> str:
        """The ingress URL's sibling function on the same host/region."""
        return self.base_url.rsplit("/", 1)[0] + "/" + function_name

    def fetch_program(self, current_version: Optional[int] = None) -> Optional[dict]:
        """POST getProgram {nodeUid, currentVersion?} → {version, bundle} or {version, unchanged}.
        Returns None on transport failure (the caller keeps its current program)."""
        body: dict = {"nodeUid": self.node_uid}
        if current_version:
            body["currentVersion"] = int(current_version)
        try:
            req = urllib.request.Request(
                self._sibling_url("getProgram"),
                data=json.dumps(body).encode("utf-8"),
                headers=self._headers(),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status != 200:
                    logger.warning("getProgram failed: %d", resp.status)
                    return None
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("getProgram error: %s", e)
            return None

    def report_program_ack(self, version: int, status: str, reason: Optional[str] = None,
                           firmware_version: Optional[str] = None) -> bool:
        payload: dict = {"nodeUid": self.node_uid, "version": int(version), "status": status,
                         "timestamp": int(time.time() * 1000)}
        if reason:
            payload["reason"] = reason[:500]
        if firmware_version:
            payload["firmwareVersion"] = firmware_version
        return self._post({"type": "programAck", "payload": payload})

    def report_variables(self, asset_id: str, values: dict) -> bool:
        return self._post({"type": "variables", "payload": {
            "nodeUid": self.node_uid, "assetId": asset_id, "values": values,
            "ts": int(time.time() * 1000)}})

    def report_alert(self, text: str, severity: str, rule_id: Optional[str] = None,
                     asset_id: Optional[str] = None) -> bool:
        payload: dict = {"nodeUid": self.node_uid, "text": text[:200], "severity": severity,
                         "timestamp": int(time.time() * 1000)}
        if rule_id:
            payload["ruleId"] = rule_id
        if asset_id:
            payload["assetId"] = asset_id
        return self._post({"type": "alert", "payload": payload})

    def report_telemetry(self, readings: list[dict]) -> None:
        self._post({
            "type": "telemetry",
            "payload": {
                "nodeUid": self.node_uid,
                "readings": readings,
            },
        })

    def report_telemetry_batch(self, batch_id: str, readings: list[dict],
                               boot_session_id: str = "") -> bool:
        """POST a durable history batch (ADR-013 §6.2 `telemetry_batch`).

        Distinct from `report_telemetry` (the live channel): this is the
        durable-history channel drained from the on-node SQLite buffer. Each
        reading carries its own `tsMs`/`seq` and optional `tsUncertain`. Returns
        True on HTTP 200 so the uploader only prunes the buffer on confirmed
        delivery.
        """
        payload = {
            "nodeUid": self.node_uid,
            "batchId": batch_id,
            "readings": readings,
        }
        if boot_session_id:
            payload["bootSessionId"] = boot_session_id
        return self._post({"type": "telemetry_batch", "payload": payload})

    def report_command_ack(self, command_id: str, status: str,
                           applied_value=None, error: str = None) -> None:
        payload = {
            "nodeUid": self.node_uid,
            "commandId": command_id,
            "status": status,
        }
        if applied_value is not None:
            payload["appliedValue"] = applied_value
        if error:
            payload["error"] = error

        self._post({"type": "commandAck", "payload": payload})

    def report_firmware_event(self, phase: str, from_version: str = None,
                              to_version: str = None, command_id: str = None,
                              rollout_id: str = None, error: str = None) -> None:
        """Report an OTA lifecycle event (started|succeeded|rolled_back|failed).

        Sent over HTTP (not MQTT) so it lands even while the daemon is down or
        MQTT is mid-reconnect during the update/restart. The cloud
        telemetryIngress maps `phase` to the firmware_update_<phase> EventType,
        updates /iotNodes/{nodeUid}.otaStatus + firmwareVersion, and advances
        the firmwareRollouts progress.
        """
        payload = {
            "nodeUid": self.node_uid,
            "phase": phase,
            "timestamp": int(time.time() * 1000),
        }
        if from_version:
            payload["fromVersion"] = from_version
        if to_version:
            payload["toVersion"] = to_version
        if command_id:
            payload["commandId"] = command_id
        if rollout_id:
            payload["rolloutId"] = rollout_id
        if error:
            payload["error"] = error
        self._post({"type": "firmware_update", "payload": payload})

    def fetch_boot_state(self, auth_token: str) -> list[dict]:
        """Fetch desired pin states from the backend for boot reconciliation."""
        try:
            body = json.dumps({
                "nodeUid": self.node_uid,
                "authToken": auth_token,
            }).encode("utf-8")
            # Body authToken is what the deployed getBootState verifies; the
            # Bearer header rides along for the header-based scheme (ADR-013).
            headers = {"Content-Type": "application/json"}
            token = auth_token or self.auth_token
            if token:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(
                BOOT_STATE_URL,
                data=body,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data.get("commands", [])
                else:
                    logger.warning("fetch_boot_state failed: %d", resp.status)
                    return []
        except Exception as e:
            logger.warning("fetch_boot_state error: %s", e)
            return []

    def _headers(self) -> dict:
        """Request headers, including the node auth header when a token is configured."""
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    def _post(self, data: dict) -> bool:
        """POST an envelope to the ingress. Returns True iff the server replied
        200 — the durable batch path relies on this to prune only on confirmed
        delivery; the fire-and-forget callers ignore the return value."""
        try:
            body = json.dumps(data).encode("utf-8")
            req = urllib.request.Request(
                self.base_url,
                data=body,
                headers=self._headers(),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.last_status = resp.status
                if resp.status != 200:
                    logger.warning("HTTP report failed: %d", resp.status)
                    return False
                return True
        except urllib.error.HTTPError as e:
            self.last_status = e.code
            logger.warning("HTTP report error: %s", e)
        except urllib.error.URLError as e:
            self.last_status = None
            logger.warning("HTTP report error: %s", e)
        except Exception as e:
            logger.warning("HTTP report unexpected error: %s", e)
        return False
