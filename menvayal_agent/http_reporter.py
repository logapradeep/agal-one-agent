"""HTTP reporter for sending telemetry and status to the Menvayal backend."""

import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

# Fallback only — the backend supplies the live endpoint via config.yaml
# (telemetry.ingress_url). This default must track the current backend region:
# the whole agal-one backend is in asia-south1 (the us-central1 deployment was
# retired). A stale default here is what silently kept nodes offline before
# ingress_url became config-driven.
TELEMETRY_INGRESS_URL = "https://asia-south1-agal-one-prod.cloudfunctions.net/telemetryIngress"


class HttpReporter:
    """Posts telemetry, status, and command acks to the backend HTTP endpoint."""

    def __init__(self, node_uid: str, base_url: str = ""):
        self.node_uid = node_uid
        # Prefer the backend-supplied URL (config.telemetry.ingress_url); fall
        # back to the module default when the config omits it (older configs or
        # a bare manual install).
        self.base_url = base_url or TELEMETRY_INGRESS_URL

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
