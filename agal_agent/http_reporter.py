"""HTTP reporter for sending telemetry and status to the Menvayal backend."""

import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

TELEMETRY_INGRESS_URL = "https://telemetryingress-amxy2i3cma-uc.a.run.app"
BOOT_STATE_URL = "https://getbootstate-amxy2i3cma-uc.a.run.app"
# Same project hash as provision-amxy2i3cma-uc.a.run.app


class HttpReporter:
    """Posts telemetry, status, and command acks to the backend HTTP endpoint."""

    def __init__(self, node_uid: str, base_url: str = TELEMETRY_INGRESS_URL):
        self.node_uid = node_uid
        self.base_url = base_url

    def report_status(self, online: bool, uptime: int, firmware_version: str = "0.1.0") -> None:
        self._post({
            "type": "status",
            "payload": {
                "nodeUid": self.node_uid,
                "online": online,
                "uptime": uptime,
                "firmwareVersion": firmware_version,
            },
        })

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

    def fetch_boot_state(self, auth_token: str) -> list[dict]:
        """Fetch desired pin states from the backend for boot reconciliation."""
        try:
            body = json.dumps({
                "nodeUid": self.node_uid,
                "authToken": auth_token,
            }).encode("utf-8")
            req = urllib.request.Request(
                BOOT_STATE_URL,
                data=body,
                headers={"Content-Type": "application/json"},
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

    def _post(self, data: dict) -> None:
        try:
            body = json.dumps(data).encode("utf-8")
            req = urllib.request.Request(
                self.base_url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    logger.warning("HTTP report failed: %d", resp.status)
        except urllib.error.URLError as e:
            logger.warning("HTTP report error: %s", e)
        except Exception as e:
            logger.warning("HTTP report unexpected error: %s", e)
