"""MQTT client for connecting to HiveMQ Cloud."""

import json
import logging
import ssl
import time
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from . import __version__
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
        # Where every command ack also goes (main.py: command_acks.CommandAcks, the HTTPS leg).
        self._ack_sink: Optional[Callable[..., None]] = None
        # Where every node event also goes (main.py: node_events.NodeEvents, the HTTPS leg).
        self._event_sink: Optional[Callable[[dict], None]] = None
        # What checks every message on the command topic before on_command (main.py:
        # command_signing.CommandGate — contracts v1.14.2, the node refuses unsigned or forged ones).
        self._gate = None
        self._unchecked_logged = False

    def set_ack_sink(self, sink: Optional[Callable[..., None]]) -> None:
        """Every command ack is also handed to ``sink(command_id, status, applied_value, error)``
        (and ``reason=`` for a refusal), connected or not. The backend reads acks only from
        telemetryIngress over HTTPS: nothing consumes the status topic (command_acks.py). The sink
        must not block."""
        self._ack_sink = sink

    def set_event_sink(self, sink: Optional[Callable[[dict], None]]) -> None:
        """Every event from ``publish_event`` is also handed to ``sink(event)``, connected or not
        (node_events.py: posted as ``sensor_event`` over HTTPS — nothing consumes the status
        topic, so an event on MQTT only never reached the cloud). The sink must not block."""
        self._event_sink = sink

    def set_command_gate(self, gate) -> None:
        """``gate.admit(raw)`` checks every message on the command topic and gives the command to
        carry out, or None (command_signing.CommandGate). main.py always sets one."""
        self._gate = gate

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
        # It carries this agent's version like every status it sends (contracts v1.14.2, nodes/README.md
        # §10.3); the backend stores no version from an offline status anyway.
        will_payload = json.dumps({
            "type": "status",
            "payload": {
                "nodeUid": self.config.username,
                "online": False,
                "uptime": 0,
                "firmwareVersion": __version__,
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

    def publish_status(self, online: bool, uptime: int, firmware_version: Optional[str] = None,
                       extra: Optional[dict] = None) -> None:
        """Publish a status. ``firmware_version`` as ``HttpReporter.report_status``: the running
        code's version, or no ``firmwareVersion`` at all — never the old "0.1.0" default."""
        if not self._client or not self._connected:
            logger.warning("Cannot publish status: not connected")
            return

        # Same {type, payload} envelope contract as publish_telemetry above.
        payload = {
            "nodeUid": self.config.username,
            "online": online,
            "uptime": uptime,
        }
        if firmware_version:
            payload["firmwareVersion"] = firmware_version
        # Report the primary MAC so the cloud can register/​index it (best-effort,
        # non-authoritative — hardwareSerial is the binding anchor).
        from .net_info import get_primary_mac
        mac = get_primary_mac()
        if mac:
            payload["mac"] = mac
        if extra:
            payload.update(extra)  # programVersion / programStatus / capabilities (v0.2.0)
        wire = json.dumps({"type": "status", "payload": payload})

        self._client.publish(
            self.config.status_topic,
            wire,
            qos=1,
        )

    def publish_event(self, event: dict) -> None:
        """Report a structured event — the one call for every node event.

        Used by the edge protection module (dry-run cutoff, low-water warning,
        baseline-learned), the block runtime (blocks/cloud.py ``CloudSink.event``) and the
        telemetry buffer (buffer_overflow): anything the cloud should record in /events.

        Event shape (functions.yaml NodeEvent; contracts/schemas/event.schema.json):
            {
                "type": "dry_run_protection_triggered" | "low_water_level_warning" | ...,
                "source": "protection" | "node" | "program",
                "sourceKey": "<asset.subkey>",
                "payload": { ... }
            }

        Caller does NOT need to include nodeUid or timestamp. It goes to the event sink — posted
        over HTTPS as ``{type: sensor_event, payload: {nodeUid, event, timestamp, eventId}}`` with
        the node's token, retried, whether MQTT is connected or not (node_events.py; contracts
        v1.14.2) — and, when connected, to the status topic as before (no consumer; harmless).
        """
        if self._event_sink is not None:
            try:
                self._event_sink(event)
            except Exception as e:  # noqa: BLE001 — reporting never fails the caller (a protection trip)
                logger.error("event %s not queued for the ingress: %s", event.get("type", "?"), e)
        if not self._client or not self._connected:
            if self._event_sink is None:
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

    # ---- Automation blocks (ADR-017, contracts v1.5.0) -----------------------
    # Fast path on the status topic; the HTTP reporter is the durable twin. All
    # three use the {type, payload} envelope the ingress dispatches on.

    def _publish_status_envelope(self, type_: str, payload: dict) -> bool:
        if not self._client or not self._connected:
            return False
        payload = dict(payload)
        payload.setdefault("nodeUid", self.config.username)
        self._client.publish(self.config.status_topic,
                             json.dumps({"type": type_, "payload": payload}), qos=1)
        return True

    def publish_program_ack(self, version: int, status: str, reason: Optional[str] = None,
                            firmware_version: Optional[str] = None, timestamp: Optional[int] = None) -> bool:
        payload: dict = {"version": int(version), "status": status,
                         "timestamp": int(timestamp) if timestamp is not None else int(time.time() * 1000)}
        if reason:
            payload["reason"] = reason[:500]
        if firmware_version:
            payload["firmwareVersion"] = firmware_version
        return self._publish_status_envelope("programAck", payload)

    def publish_variables(self, asset_id: str, values: dict) -> bool:
        return self._publish_status_envelope("variables", {
            "assetId": asset_id, "values": values, "ts": int(time.time() * 1000)})

    def publish_alert(self, text: str, severity: str, rule_id: Optional[str] = None,
                      asset_id: Optional[str] = None, timestamp: Optional[int] = None) -> bool:
        payload: dict = {"text": text[:200], "severity": severity,
                         "timestamp": int(timestamp) if timestamp is not None else int(time.time() * 1000)}
        if rule_id:
            payload["ruleId"] = rule_id
        if asset_id:
            payload["assetId"] = asset_id
        return self._publish_status_envelope("alert", payload)

    def publish_command_ack(
        self,
        command_id: str,
        status: str,
        applied_value: Optional[object] = None,
        error: Optional[str] = None,
        *,
        ingress: bool = True,
        reason: Optional[str] = None,
    ) -> None:
        """The one call for a command ack, from every site. It goes to the ack sink (the HTTPS
        leg the backend reads — whether MQTT is connected or not) and, when connected, to the
        status topic (no consumer today; harmless, a future broker consumer may use it).
        ``ingress=False`` keeps it on MQTT only: an ack the backend has no use for (the OTA
        receipt, whose outcome comes back as firmware_update events). ``reason`` (only with
        ``failed``) is why the node refused a message carrying this id before carrying anything
        out — enums.yaml#CommandRefusalReason (command_signing.CommandGate)."""
        if ingress and self._ack_sink is not None:
            try:
                if reason is None:
                    self._ack_sink(command_id, status, applied_value, error)
                else:
                    self._ack_sink(command_id, status, applied_value, error, reason=reason)
            except Exception as e:  # noqa: BLE001 — an ack sink failure never fails the command
                logger.error("command ack %s %s not queued for the ingress: %s", command_id, status, e)
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
        if reason:
            payload["reason"] = reason

        # Same {type, payload} envelope the ingress takes (the HTTPS leg posts this very payload).
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
        """Every message on the command topic is checked by the gate before anything reads it
        (command_signing.py): the handler is given the very object that was verified, or nothing.
        Without a gate (only a client nobody wired — main.py always sets one) the message is
        parsed and carried out unchecked, logged once."""
        try:
            if self._gate is not None:
                command = self._gate.admit(msg.payload)
                if command is None:
                    return  # refused: logged and answered by the gate
            else:
                if not self._unchecked_logged:
                    self._unchecked_logged = True
                    logger.error("MQTT commands are carried out UNCHECKED: no command gate is set")
                command = json.loads(msg.payload.decode("utf-8"))
                if not isinstance(command, dict):
                    logger.error("MQTT message is not a JSON object: dropped")
                    return
            logger.debug("Received command: %s", command)
            if self._on_command:
                self._on_command(command)
        except json.JSONDecodeError:
            logger.error("Invalid JSON in MQTT message: %s", msg.payload)
        except Exception as e:
            logger.error("Error handling MQTT message: %s", e)
