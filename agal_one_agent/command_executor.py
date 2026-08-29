"""Routes incoming commands to the appropriate hardware handler."""

import logging
from typing import Optional

from .config import AgentConfig, PinConfig
from .mqtt_client import AgalOneMqttClient

logger = logging.getLogger(__name__)

# Protocol → handler mapping (lazy-loaded)
_handlers: dict = {}

# Sensor pin (physical_pin) → sensor instance. Populated by main.py at startup
# so the telemetry publisher + protection monitor share the same instance.
_sensors: dict = {}


def register_sensor(pin, instance) -> None:
    """Wire a sensor instance to its pin so telemetry + protection see the same object."""
    _sensors[pin.physical_pin] = instance


def get_sensor_instance(pin):
    return _sensors.get(pin.physical_pin)


def _get_handler(protocol: str):
    """Lazy-load the appropriate handler for a pin protocol."""
    if protocol in _handlers:
        return _handlers[protocol]

    handler = None
    try:
        if protocol in ("gpio_input", "gpio_output"):
            from .handlers.gpio_handler import GpioHandler
            handler = GpioHandler()
        elif protocol in ("i2c_sda", "i2c_scl"):
            from .handlers.i2c_handler import I2CHandler
            handler = I2CHandler()
        elif protocol in ("spi_mosi", "spi_miso", "spi_sck", "spi_cs"):
            from .handlers.spi_handler import SpiHandler
            handler = SpiHandler()
        elif protocol in ("uart_tx", "uart_rx"):
            from .handlers.uart_handler import UartHandler
            handler = UartHandler()
        elif protocol == "oneWire":
            from .handlers.onewire_handler import OneWireHandler
            handler = OneWireHandler()
        elif protocol == "pwm":
            from .handlers.pwm_handler import PwmHandler
            handler = PwmHandler()
    except ImportError as e:
        logger.warning("Handler for %s not available: %s", protocol, e)

    _handlers[protocol] = handler
    return handler


def _find_pin(config: AgentConfig, command: dict) -> Optional[PinConfig]:
    """Find the pin config matching a command's gpio/pin number."""
    gpio = command.get("gpioNumber")
    pin_num = command.get("pinNumber")

    for pin in config.pins:
        if gpio is not None and pin.gpio_number == gpio:
            return pin
        if pin_num is not None and pin.physical_pin == pin_num:
            return pin

    return None


def execute(config: AgentConfig, mqtt_client: AgalOneMqttClient, command: dict, protection_monitor=None) -> None:
    """Execute a command received from MQTT.

    If `protection_monitor` is provided, notify it of relay state changes so the
    dry-run protection logic can suppress inrush samples on pump-start and reset
    cut-off state on pump-stop.
    """
    command_id = command.get("commandId", "unknown")
    cmd_type = command.get("type", "setPower")
    value = command.get("value")
    protocol = command.get("pinProtocol")

    logger.info(
        "Executing command %s: type=%s, value=%s, protocol=%s",
        command_id, cmd_type, value, protocol
    )

    # Acknowledge receipt
    mqtt_client.publish_command_ack(command_id, "acknowledged")

    pin = _find_pin(config, command)
    if not pin:
        error = f"No matching pin for command (gpio={command.get('gpioNumber')}, pin={command.get('pinNumber')})"
        logger.error(error)
        mqtt_client.publish_command_ack(command_id, "failed", error=error)
        return

    # Use protocol from command or fall back to pin config
    effective_protocol = protocol or pin.protocol
    handler = _get_handler(effective_protocol)

    if not handler:
        error = f"No handler available for protocol {effective_protocol}"
        logger.error(error)
        mqtt_client.publish_command_ack(command_id, "failed", error=error)
        return

    try:
        mqtt_client.publish_command_ack(command_id, "executing")

        if cmd_type == "setPower":
            if effective_protocol == "pwm":
                # On a PWM pin "on" means the configured duty cycle (the
                # contracted PortTransport.pwm dutyCyclePct — asset.schema
                # v3.1), not a 1% duty. Runtime duty changes go through
                # setPortValue below (value = percentage per the contract).
                on_duty = pin.pwm_duty_cycle_pct if pin.pwm_duty_cycle_pct is not None else 100.0
                applied = handler.write(pin, on_duty if value else 0.0)
            else:
                applied = handler.write(pin, 1 if value else 0)
            # Notify protection so it knows to suppress inrush + re-arm
            if protection_monitor is not None and pin.label:
                if value:
                    protection_monitor.notify_pump_started(pin.label)
                else:
                    protection_monitor.notify_pump_stopped(pin.label)
        elif cmd_type == "setPortValue":
            applied = handler.write(pin, value)
        elif cmd_type == "readSensor":
            applied = handler.read(pin)
        else:
            error = f"Unknown command type: {cmd_type}"
            logger.error(error)
            mqtt_client.publish_command_ack(command_id, "failed", error=error)
            return

        mqtt_client.publish_command_ack(command_id, "completed", applied_value=applied)
        logger.info("Command %s completed, applied=%s", command_id, applied)

    except Exception as e:
        error = f"Execution error: {e}"
        logger.error(error)
        mqtt_client.publish_command_ack(command_id, "failed", error=error)
