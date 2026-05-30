"""Menvayal Agent - main entry point."""

import argparse
import logging
import signal
import sys
import time

from .config import AgentConfig, PinConfig
from .mqtt_client import MenvayalMqttClient
from .command_executor import execute, register_sensor, _get_handler
from .telemetry_publisher import TelemetryPublisher
from .heartbeat import HeartbeatPublisher
from .http_reporter import HttpReporter
from . import boot_reconciler
from .sentry_setup import init_sentry
from .sensors import (
    CurrentSensorACS758,
    UltrasonicSensorJsnSr04t,
    ProtectionMonitor,
)
from .sensors.protection import ProtectionConfig

logger = logging.getLogger("agal_agent")


def main():
    parser = argparse.ArgumentParser(description="Menvayal IoT Agent")
    parser.add_argument(
        "--config", "-c",
        default="/etc/agal-agent/config.yaml",
        help="Path to YAML config file (default: /etc/agal-agent/config.yaml)",
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

    # Initialize Sentry as early as possible AFTER config load, so the node_uid
    # tag is attached to every event. No-op if SENTRY_DSN is unset (dev / bench).
    init_sentry(node_uid=config.node.uid, agent_version="0.1.5")

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

    # ---- Sensor + edge-protection setup --------------------------------------
    current_sensors: list[CurrentSensorACS758] = []
    ultrasonic_sensors: list[UltrasonicSensorJsnSr04t] = []
    protection_configs: dict[str, ProtectionConfig] = {}

    for pin in config.pins:
        if not pin.sensor_type:
            continue
        try:
            if pin.sensor_type == "current_acs758":
                sensor = CurrentSensorACS758(pin)
                current_sensors.append(sensor)
                register_sensor(pin, sensor)
                if pin.protection:
                    protection_configs[sensor.source_key] = ProtectionConfig(
                        cut_pin_label=pin.protection["cut_pin_label"],
                        threshold_pct=float(pin.protection.get("threshold_pct", 20)),
                        confirm_window_s=float(pin.protection.get("confirm_window_s", 5)),
                        inrush_ignore_s=float(pin.protection.get("inrush_ignore_s", 2)),
                        baseline_learn_after_s=float(pin.protection.get("baseline_learn_after_s", 30)),
                        baseline_window_s=float(pin.protection.get("baseline_window_s", 10)),
                        auto_restart_after_s=float(pin.protection.get("auto_restart_after_s", 0)),
                    )
                logger.info("Sensor wired: %s (current_acs758)", sensor.source_key)
            elif pin.sensor_type == "ultrasonic_jsn_sr04t":
                sensor = UltrasonicSensorJsnSr04t(pin)
                ultrasonic_sensors.append(sensor)
                register_sensor(pin, sensor)
                logger.info("Sensor wired: %s (ultrasonic_jsn_sr04t)", sensor.source_key)
            else:
                logger.warning("Unknown sensor_type on pin %d: %s", pin.physical_pin, pin.sensor_type)
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to wire sensor on pin %d (%s): %s", pin.physical_pin, pin.sensor_type, e)

    def _find_pin_by_label(label: str) -> "PinConfig | None":
        for p in config.pins:
            if p.label == label:
                return p
        return None

    def relay_writer(pin_label: str, value: int) -> None:
        """Used by ProtectionMonitor to cut a relay on dry-run trigger."""
        pin = _find_pin_by_label(pin_label)
        if not pin:
            raise ValueError(f"No pin with label {pin_label}")
        handler = _get_handler(pin.protocol)
        if not handler:
            raise RuntimeError(f"No handler for protocol {pin.protocol} (pin {pin_label})")
        handler.write(pin, value)

    def event_publisher(event: dict) -> None:
        mqtt_client.publish_event(event)

    protection_monitor: ProtectionMonitor | None = None
    if current_sensors or ultrasonic_sensors:
        protection_monitor = ProtectionMonitor(
            current_sensors=current_sensors,
            protection_configs=protection_configs,
            ultrasonic_sensors=ultrasonic_sensors,
            relay_writer=relay_writer,
            event_publisher=event_publisher,
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
        execute(config, mqtt_client, command, protection_monitor=protection_monitor)

    mqtt_client.set_command_handler(on_command)

    # HTTP reporter for backend status updates
    http_reporter = HttpReporter(config.node.uid)

    # Reconnect handler — reconcile state when MQTT reconnects after a drop
    def on_reconnect():
        boot_reconciler.reconcile(config, mqtt_client, http_reporter)

    mqtt_client.set_reconnect_handler(on_reconnect)

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
        if protection_monitor is not None:
            protection_monitor.start()

        # Reconcile pin states with cloud on boot
        boot_reconciler.reconcile(config, mqtt_client, http_reporter)

        logger.info("Agent running. Press Ctrl+C to stop.")

        while running:
            time.sleep(1)

    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Stopping services...")
        telemetry.stop()
        heartbeat.stop()
        if protection_monitor is not None:
            protection_monitor.stop()
        if lora_bridge:
            lora_bridge.stop()
        mqtt_client.disconnect()
        logger.info("Agent stopped.")


if __name__ == "__main__":
    main()
