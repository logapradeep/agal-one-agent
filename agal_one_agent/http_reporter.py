"""HTTP reporter for sending telemetry and status to the Agal One backend."""

import json
import logging
import time
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

TELEMETRY_INGRESS_URL = "https://telemetryingress-amxy2i3cma-uc.a.run.app"
BOOT_STATE_URL = "https://getbootstate-amxy2i3cma-uc.a.run.app"
# Same project hash as provision-amxy2i3cma-uc.a.run.app


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
                 base_url: str = TELEMETRY_INGRESS_URL):
        self.node_uid = node_uid
        self.auth_token = auth_token
        self.base_url = base_url

    def report_status(self, online: bool, uptime: int, firmware_version: str = "0.1.0") -> None:
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
        self._post({"type": "status", "payload": payload})

    def report_telemetry(self, readings: list[dict]) -> None:
        self._post({
            "type": "telemetry",
            "payload": {
                "nodeUid": self.node_uid,
                "readings": readings,
            },
        })

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

    def _post(self, data: dict) -> None:
        try:
            body = json.dumps(data).encode("utf-8")
            req = urllib.request.Request(
                self.base_url,
                data=body,
                headers=self._headers(),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    logger.warning("HTTP report failed: %d", resp.status)
        except urllib.error.URLError as e:
            logger.warning("HTTP report error: %s", e)
        except Exception as e:
            logger.warning("HTTP report unexpected error: %s", e)
