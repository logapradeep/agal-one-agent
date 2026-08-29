"""MQTT client for connecting to HiveMQ Cloud."""

import json
import logging
import ssl
import time
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from .config import MqttConfig

logger = logging.getLogger(__name__)


class AgalOneMqttClient:
    """Persistent MQTT connection to HiveMQ Cloud."""

    def __init__(self, config: MqttConfig):
        self.config = config
        self._client: Optional[mqtt.Client] = None
        self._on_command: Optional[Callable[[dict], None]] = None
        self._on_reconnect: Optional[Callable[[], None]] = None
        self._connected = False
        self._was_connected = False  # Tracks if we ever connected before

    def set_command_handler(self, handler: Callable[[dict], None]) -> None:
        self._on_command = handler

    def set_reconnect_handler(self, handler: Callable[[], None]) -> None:
        """Called when MQTT reconnects after a disconnection (not on first connect)."""
        self._on_reconnect = handler

    def connect(self) -> None:
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            protocol=mqtt.MQTTv5,
        )

        if self.config.tls:
            self._client.tls_set(tls_version=ssl.PROTOCOL_TLSv1_2)

        self._client.username_pw_set(self.config.username, self.config.password)

        # Set Last Will and Testament — broker publishes this when client
        # disconnects ungracefully (power loss, crash, network drop).
        will_payload = json.dumps({
            "type": "status",
            "payload": {
                "nodeUid": self.config.username,
                "online": False,
                "uptime": 0,
            },
        })
        self._client.will_set(
            self.config.status_topic,
            payload=will_payload,
            qos=1,
            retain=True,
        )

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        logger.info("Connecting to MQTT broker %s:%d", self.config.broker, self.config.port)
        self._client.connect(self.config.broker, self.config.port, keepalive=30)
        self._client.loop_start()

    def disconnect(self) -> None:
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def publish_telemetry(self, readings: list[dict]) -> None:
        if not self._client or not self._connected:
            logger.warning("Cannot publish telemetry: not connected")
            return

        # Canonical envelope expected by the cloud telemetryIngress webhook:
        #   const {type, payload} = req.body
        # then `payload` is destructured for nodeUid / readings / timestamp.
        # See agal-one/backend/functions/src/modules/mqtt/telemetryIngress.ts
        # (req-body destructure at line 120; handleTelemetry at line 188).
        # The HTTPS fallback in http_reporter.py:35 uses the same shape; this
        # MQTT path was previously emitting a bare payload which the cloud
        # function would 400 unless a HiveMQ Data Hub policy was injecting
        # `type` from the topic name (not version-controlled in this repo).
        # Wrapping here makes the MQTT path independent of any broker-side
        # policy.
        wire = json.dumps({
            "type": "telemetry",
            "payload": {
                "nodeUid": self.config.username,
                "readings": readings,
                "timestamp": int(time.time() * 1000),
            },
        })

        self._client.publish(
            self.config.telemetry_topic,
            wire,
            qos=1,
        )

    def publish_status(self, online: bool, uptime: int, firmware_version: str = "0.1.0") -> None:
        if not self._client or not self._connected:
            logger.warning("Cannot publish status: not connected")
            return

        # Same {type, payload} envelope contract as publish_telemetry above.
        payload = {
            "nodeUid": self.config.username,
            "online": online,
            "uptime": uptime,
            "firmwareVersion": firmware_version,
        }
        # Report the primary MAC so the cloud can register/​index it (best-effort,
        # non-authoritative — hardwareSerial is the binding anchor).
        from .net_info import get_primary_mac
        mac = get_primary_mac()
        if mac:
            payload["mac"] = mac
        wire = json.dumps({"type": "status", "payload": payload})

        self._client.publish(
            self.config.status_topic,
            wire,
            qos=1,
        )

    def publish_event(self, event: dict) -> None:
        """Publish a structured event to the status topic.

        Used by the edge protection module (dry-run cutoff, low-water warning,
        baseline-learned) and any other code that needs to emit an event the
        cloud should record in the /events collection.

        Event shape (matches contracts/schemas/event.schema.json):
            {
                "type": "dry_run_protection_triggered" | "low_water_level_warning" | ...,
                "source": "protection" | "node" | ...,
                "sourceKey": "<asset.subkey>",
                "payload": { ... }
            }

        Caller does NOT need to include nodeUid or timestamp — backend stamps them.
        """
        if not self._client or not self._connected:
            logger.warning("Cannot publish event (%s): not connected", event.get("type", "?"))
            return

        wrapped = {
            "nodeUid": self.config.username,
            "type": "sensor_event",
            "event": event,
            "timestamp": int(time.time() * 1000),
        }
        self._client.publish(
            self.config.status_topic,
            json.dumps(wrapped),
            qos=1,
        )
        logger.info(
            "Published event %s (sourceKey=%s)",
            event.get("type", "?"), event.get("sourceKey", "?"),
        )

    def publish_lora_uplink(self, uplink_data: dict) -> None:
        """Publish a LoRa uplink payload to the cloud for processing."""
        if not self._client or not self._connected:
            logger.warning("Cannot publish LoRa uplink: not connected")
            return

        payload = json.dumps({
            "nodeUid": self.config.username,
            "type": "lora_uplink",
            "data": uplink_data,
            "timestamp": int(time.time() * 1000),
        })

        self._client.publish(
            self.config.telemetry_topic,
            payload,
            qos=1,
        )
        logger.debug("Published LoRa uplink from devAddr=%s", uplink_data.get("devAddr", "?"))

    def publish_lora_survey_sample(self, sample: dict) -> None:
        """Publish a placement-survey sample (rssi/snr) for the live signal
        meter — ADR-011 §10.2 / §10.7 item 4 `lora_survey_sample` ingress type.

        Flat shape (like sensor_event): the cloud reads the whole body. Caller
        supplies childShortId/rssi/snr/sf/quality/at; nodeUid is stamped here."""
        if not self._client or not self._connected:
            logger.warning("Cannot publish LoRa survey sample: not connected")
            return
        wrapped = {
            "nodeUid": self.config.username,
            "type": "lora_survey_sample",
            "sample": sample,
            "timestamp": int(time.time() * 1000),
        }
        self._client.publish(self.config.status_topic, json.dumps(wrapped), qos=1)
        logger.debug("Published LoRa survey sample child=%s",
                     sample.get("childShortId", "?"))

    def publish_lora_child_status(self, status: dict) -> None:
        """Publish per-leaf health (battery, rssi/snr, lastSeen, quality) —
        ADR-011 §6 / §10.4 `lora_child_status` ingress type. Flat shape."""
        if not self._client or not self._connected:
            logger.warning("Cannot publish LoRa child status: not connected")
            return
        wrapped = {
            "nodeUid": self.config.username,
            "type": "lora_child_status",
            "child": status,
            "timestamp": int(time.time() * 1000),
        }
        self._client.publish(self.config.status_topic, json.dumps(wrapped), qos=1)
        logger.debug("Published LoRa child status child=%s",
                     status.get("childShortId", "?"))

    def publish_lora_event(self, event: dict) -> None:
        """Publish a LoRa network event (join, leave, error) to the cloud."""
        if not self._client or not self._connected:
            logger.warning("Cannot publish LoRa event: not connected")
            return

        payload = json.dumps({
            "nodeUid": self.config.username,
            "type": "lora_event",
            "event": event,
            "timestamp": int(time.time() * 1000),
        })

        self._client.publish(
            self.config.status_topic,
            payload,
            qos=1,
        )
        logger.debug("Published LoRa event: %s", event.get("type", "unknown"))

    def publish_command_ack(
        self,
        command_id: str,
        status: str,
        applied_value: Optional[object] = None,
        error: Optional[str] = None,
    ) -> None:
        if not self._client or not self._connected:
            return

        payload: dict = {
            "nodeUid": self.config.username,
            "commandId": command_id,
            "status": status,
        }
        if applied_value is not None:
            payload["appliedValue"] = applied_value
        if error:
            payload["error"] = error

        # Publish ack on status topic (backend listens for commandAck type)
        self._client.publish(
            self.config.status_topic,
            json.dumps({"type": "commandAck", "payload": payload}),
            qos=1,
        )

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self._connected = True
            logger.info("Connected to MQTT broker")
            client.subscribe(self.config.commands_topic, qos=1)
            logger.info("Subscribed to %s", self.config.commands_topic)
            # Trigger reconciliation on reconnect (not first connect — main.py handles that)
            if self._was_connected and self._on_reconnect:
                logger.info("MQTT reconnected — triggering state reconciliation")
                self._on_reconnect()
            self._was_connected = True
        else:
            logger.error("MQTT connection failed with code %d", rc)

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        self._connected = False
        if rc != 0:
            logger.warning("Unexpected MQTT disconnect (rc=%d), will auto-reconnect", rc)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            logger.debug("Received command: %s", payload)
            if self._on_command:
                self._on_command(payload)
        except json.JSONDecodeError:
            logger.error("Invalid JSON in MQTT message: %s", msg.payload)
        except Exception as e:
            logger.error("Error handling MQTT message: %s", e)
