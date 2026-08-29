"""LoRa Gateway Bridge — connects ChirpStack packet forwarder to Agal One MQTT.

Architecture:
  LoRa Radio (SX1301/SX1302 concentrator)
    → Semtech UDP Packet Forwarder (lora_pkt_fwd)
    → ChirpStack Gateway Bridge (converts UDP to MQTT)
    → Local Mosquitto MQTT broker
    → This bridge subscribes to local MQTT
    → Forwards uplink data to HiveMQ (Agal One cloud) as telemetry
    → Receives downlink commands from HiveMQ
    → Publishes downlink to local MQTT for the gateway bridge

This module manages:
1. Local MQTT connection (to ChirpStack Gateway Bridge)
2. Uplink forwarding: local LoRa uplinks → Agal One cloud telemetry
3. Downlink routing: Agal One cloud commands → local LoRa downlinks
4. End-device registration tracking
"""

import json
import logging
import threading
from typing import Optional, Callable

import paho.mqtt.client as mqtt

from .config import LoRaGatewayConfig

logger = logging.getLogger(__name__)


class LoRaBridge:
    """Bridges between local ChirpStack Gateway Bridge MQTT and Agal One cloud MQTT."""

    def __init__(
        self,
        gateway_config: LoRaGatewayConfig,
        on_uplink: Callable[[dict], None],
        on_device_join: Callable[[str, str], None],
    ):
        """
        Args:
            gateway_config: LoRa gateway configuration
            on_uplink: Callback when an uplink is received. Passes parsed payload.
            on_device_join: Callback when a device joins. Passes (dev_eui, dev_addr).
        """
        self.config = gateway_config
        self._on_uplink = on_uplink
        self._on_device_join = on_device_join
        self._client: Optional[mqtt.Client] = None
        self._connected = False
        self._known_devices: set[str] = set()

    def start(self) -> None:
        """Connect to local MQTT broker and subscribe to gateway bridge topics."""
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"agal-lora-bridge-{self.config.gateway_eui[:8]}",
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        logger.info(
            "LoRa bridge connecting to local MQTT %s:%d",
            self.config.bridge_mqtt_broker,
            self.config.bridge_mqtt_port,
        )

        self._client.connect(
            self.config.bridge_mqtt_broker,
            self.config.bridge_mqtt_port,
            keepalive=60,
        )
        self._client.loop_start()

    def stop(self) -> None:
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def known_devices(self) -> set[str]:
        return self._known_devices

    def send_downlink(self, dev_eui: str, payload: bytes, port: int = 1, confirmed: bool = False) -> None:
        """Send a downlink command to a LoRa end-device via the gateway bridge."""
        if not self._client or not self._connected:
            logger.warning("Cannot send downlink: local MQTT not connected")
            return

        # ChirpStack Gateway Bridge v3.x downlink topic format
        topic = f"{self.config.bridge_topic_prefix}/{self.config.gateway_eui}/command/down"

        # ChirpStack expects a specific protobuf-based JSON format
        # Simplified: the actual implementation would use chirpstack-api protobuf
        downlink = {
            "phyPayload": payload.hex(),
            "txInfo": {
                "frequency": 865062500,  # Varies by region, simplified
                "power": 14,
                "modulation": "LORA",
                "loRaModulationInfo": {
                    "bandwidth": 125,
                    "spreadingFactor": 7,
                    "codeRate": "4/5",
                    "polarizationInversion": True,
                },
                "timing": "DELAY",
                "timingInfo": {"delay": "1s"},
            },
        }

        self._client.publish(topic, json.dumps(downlink), qos=1)
        logger.info("Downlink sent to %s on port %d", dev_eui, port)

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self._connected = True
            logger.info("LoRa bridge connected to local MQTT")

            # Subscribe to gateway event topics (ChirpStack Gateway Bridge format)
            prefix = self.config.bridge_topic_prefix
            gw_eui = self.config.gateway_eui

            # Uplink events
            client.subscribe(f"{prefix}/{gw_eui}/event/up", qos=1)
            # Join requests
            client.subscribe(f"{prefix}/{gw_eui}/event/join", qos=1)
            # Stats
            client.subscribe(f"{prefix}/{gw_eui}/event/stats", qos=1)
            # Ack
            client.subscribe(f"{prefix}/{gw_eui}/event/ack", qos=1)

            logger.info("Subscribed to gateway events for %s", gw_eui)
        else:
            logger.error("LoRa bridge MQTT connection failed (rc=%d)", rc)

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        self._connected = False
        if rc != 0:
            logger.warning("LoRa bridge disconnected (rc=%d), will reconnect", rc)

    def _on_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            payload = json.loads(msg.payload.decode("utf-8"))

            if "/event/up" in topic:
                self._handle_uplink(payload)
            elif "/event/join" in topic:
                self._handle_join(payload)
            elif "/event/stats" in topic:
                self._handle_stats(payload)
            elif "/event/ack" in topic:
                self._handle_ack(payload)
            else:
                logger.debug("Unknown LoRa event topic: %s", topic)

        except json.JSONDecodeError:
            logger.error("Invalid JSON from LoRa bridge: %s", msg.payload[:100])
        except Exception as e:
            logger.error("LoRa bridge message error: %s", e)

    def _handle_uplink(self, payload: dict) -> None:
        """Process an uplink frame from a LoRa end-device."""
        # ChirpStack Gateway Bridge uplink format (simplified)
        phy_payload = payload.get("phyPayload", "")
        rx_info = payload.get("rxInfo", [{}])
        tx_info = payload.get("txInfo", {})

        # Extract device info from MAC header
        dev_addr = self._extract_dev_addr(phy_payload)
        if dev_addr:
            self._known_devices.add(dev_addr)

        rssi = rx_info[0].get("rssi", 0) if rx_info else 0
        snr = rx_info[0].get("loRaSNR", 0) if rx_info else 0
        frequency = tx_info.get("frequency", 0)

        # Build telemetry-compatible payload for Agal One cloud
        uplink_data = {
            "type": "lora_uplink",
            "devAddr": dev_addr,
            "phyPayload": phy_payload,
            "rssi": rssi,
            "snr": snr,
            "frequency": frequency,
            "spreadingFactor": tx_info.get("loRaModulationInfo", {}).get("spreadingFactor", 0),
        }

        logger.debug("LoRa uplink from %s (RSSI=%d, SNR=%.1f)", dev_addr, rssi, snr)
        self._on_uplink(uplink_data)

    def _handle_join(self, payload: dict) -> None:
        """Process a join-accept for a LoRa end-device."""
        phy_payload = payload.get("phyPayload", "")
        dev_eui = payload.get("devEUI", "")
        dev_addr = self._extract_dev_addr(phy_payload)

        if dev_eui and dev_addr:
            self._known_devices.add(dev_addr)
            logger.info("LoRa device joined: DevEUI=%s, DevAddr=%s", dev_eui, dev_addr)
            self._on_device_join(dev_eui, dev_addr)

    def _handle_stats(self, payload: dict) -> None:
        """Process gateway statistics."""
        rx_ok = payload.get("rxPacketsReceived", 0)
        rx_fw = payload.get("rxPacketsReceivedOK", 0)
        tx_ok = payload.get("txPacketsEmitted", 0)
        logger.debug("Gateway stats: RX=%d, RX_OK=%d, TX=%d", rx_ok, rx_fw, tx_ok)

    def _handle_ack(self, payload: dict) -> None:
        """Process downlink acknowledgement."""
        logger.debug("Downlink ACK received")

    @staticmethod
    def _extract_dev_addr(phy_payload_hex: str) -> Optional[str]:
        """Extract DevAddr from a LoRaWAN PHY payload (data frame)."""
        try:
            if len(phy_payload_hex) < 18:  # Minimum data frame length
                return None
            raw = bytes.fromhex(phy_payload_hex)
            mhdr = raw[0]
            mtype = (mhdr >> 5) & 0x07
            # MType 2=Unconfirmed Data Up, 4=Confirmed Data Up
            if mtype in (2, 4):
                dev_addr = raw[1:5][::-1].hex()  # Little-endian
                return dev_addr
        except (ValueError, IndexError):
            pass
        return None
