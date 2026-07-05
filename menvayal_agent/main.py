"""Menvayal Agent - main entry point."""

import argparse
import logging
import signal
import sys
import time

from .config import AgentConfig
from .mqtt_client import MenvayalMqttClient
from .command_executor import execute
from .telemetry_publisher import TelemetryPublisher
from .heartbeat import HeartbeatPublisher
from .http_reporter import HttpReporter

logger = logging.getLogger("menvayal_agent")


def main():
    parser = argparse.ArgumentParser(description="Menvayal IoT Agent")
    parser.add_argument(
        "--config", "-c",
        default="/etc/menvayal/config.yaml",
        help="Path to YAML config file (default: /etc/menvayal/config.yaml)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    logger.info("Menvayal Agent v0.1.5 starting")
    logger.info("Loading config from %s", args.config)

    try:
        config = AgentConfig.from_yaml(args.config)
    except Exception as e:
        logger.error("Failed to load config: %s", e)
        sys.exit(1)

    logger.info("Node: %s (%s)", config.node.name, config.node.uid)
    logger.info("Board: %s (%s)", config.board.model, config.board.category)
    logger.info("Connectivity: %s", config.connectivity)
    logger.info("Node type: %s", config.node.node_type)
    logger.info("Pins configured: %d", len(config.pins))

    if config.board.hat_id:
        logger.info("HAT: %s (%s)", config.board.hat_name, config.board.hat_id)

    # MQTT client (cloud — HiveMQ)
    mqtt_client = MenvayalMqttClient(config.mqtt)

    # LoRa bridge (local — ChirpStack Gateway Bridge)
    lora_bridge = None
    if config.is_lora_gateway and config.lora and config.lora.gateway:
        from .lora_bridge import LoRaBridge

        def on_lora_uplink(uplink_data: dict):
            """Forward LoRa uplink to cloud as telemetry."""
            dev_addr = uplink_data.get("devAddr", "unknown")
            readings = [
                {"sourceKey": f"lora.{dev_addr}.rssi", "value": uplink_data.get("rssi", 0)},
                {"sourceKey": f"lora.{dev_addr}.snr", "value": uplink_data.get("snr", 0)},
            ]
            mqtt_client.publish_lora_uplink(uplink_data)
            mqtt_client.publish_telemetry(readings)

        def on_lora_join(dev_eui: str, dev_addr: str):
            """Notify cloud that a device joined."""
            mqtt_client.publish_lora_event({
                "type": "device_join",
                "devEUI": dev_eui,
                "devAddr": dev_addr,
            })

        lora_bridge = LoRaBridge(
            config.lora.gateway,
            on_uplink=on_lora_uplink,
            on_device_join=on_lora_join,
        )
        logger.info(
            "LoRa gateway mode: EUI=%s, region=%s",
            config.lora.gateway.gateway_eui,
            config.lora.gateway.region,
        )

    # Command handler
    def on_command(command: dict):
        cmd_type = command.get("type", "")
        command_id = command.get("commandId", "")

        # OTA firmware update
        if cmd_type == "firmwareUpdate":
            from .ota_updater import perform_update
            version = command.get("version", "")
            mqtt_client.publish_command_ack(command_id, "executing")
            try:
                msg = perform_update(version)
                mqtt_client.publish_command_ack(command_id, "completed", applied_value=msg)
            except Exception as e:
                mqtt_client.publish_command_ack(command_id, "failed", error=str(e))
            return

        # Route LoRa downlink commands to the bridge
        if cmd_type == "loraDownlink" and lora_bridge:
            dev_eui = command.get("devEUI", "")
            payload_hex = command.get("payload", "")
            port = command.get("port", 1)
            try:
                lora_bridge.send_downlink(dev_eui, bytes.fromhex(payload_hex), port=port)
                mqtt_client.publish_command_ack(command_id, "completed")
            except Exception as e:
                mqtt_client.publish_command_ack(command_id, "failed", error=str(e))
            return

        # Pin configuration sync from backend
        if cmd_type == "syncPinConfig":
            pins_data = command.get("pins", [])
            logger.info("Received pin config sync: %d pins", len(pins_data))
            config.update_pins(pins_data, args.config)
            logger.info("Pin config updated: %d pins active", len(config.pins))
            mqtt_client.publish_command_ack(command_id, "completed")
            return

        # Regular GPIO/pin commands
        execute(config, mqtt_client, command)

    mqtt_client.set_command_handler(on_command)

    # HTTP reporter for backend status updates. The ingress URL comes from the
    # backend-generated config (telemetry.ingress_url); HttpReporter falls back
    # to its module default when that's empty.
    http_reporter = HttpReporter(config.node.uid, config.telemetry.ingress_url)

    # Telemetry & heartbeat
    telemetry = TelemetryPublisher(config, mqtt_client)
    heartbeat = HeartbeatPublisher(config, mqtt_client, http_reporter=http_reporter)

    # Graceful shutdown
    running = True

    def shutdown(signum, frame):
        nonlocal running
        logger.info("Shutting down (signal %d)...", signum)
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Start
    try:
        mqtt_client.connect()

        # Wait for connection
        for _ in range(30):
            if mqtt_client.is_connected:
                break
            time.sleep(1)

        if not mqtt_client.is_connected:
            logger.error("Failed to connect to MQTT broker")
            sys.exit(1)

        # Start LoRa bridge if this is a gateway node
        if lora_bridge:
            lora_bridge.start()
            logger.info("LoRa bridge started")

        telemetry.start()
        heartbeat.start()

        logger.info("Agent running. Press Ctrl+C to stop.")

        while running:
            time.sleep(1)

    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Stopping services...")
        telemetry.stop()
        heartbeat.stop()
        if lora_bridge:
            lora_bridge.stop()
        mqtt_client.disconnect()
        logger.info("Agent stopped.")


if __name__ == "__main__":
    main()
