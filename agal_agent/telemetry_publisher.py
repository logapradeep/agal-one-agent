"""Periodic sensor reading and telemetry publishing."""

import logging
import threading
from typing import Optional

from .config import AgentConfig
from .mqtt_client import MenvayalMqttClient

logger = logging.getLogger(__name__)


class TelemetryPublisher:
    """Reads all input pins periodically and publishes telemetry."""

    def __init__(self, config: AgentConfig, mqtt_client: MenvayalMqttClient):
        self.config = config
        self.mqtt_client = mqtt_client
        self._timer: Optional[threading.Timer] = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._schedule_next()
        logger.info(
            "Telemetry publisher started (interval=%ds)",
            self.config.telemetry.interval_seconds,
        )

    def stop(self) -> None:
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _schedule_next(self) -> None:
        if not self._running:
            return
        self._timer = threading.Timer(
            self.config.telemetry.interval_seconds,
            self._publish_cycle,
        )
        self._timer.daemon = True
        self._timer.start()

    def _publish_cycle(self) -> None:
        try:
            readings = self._read_all_inputs()
            if readings:
                self.mqtt_client.publish_telemetry(readings)
                logger.debug("Published %d telemetry readings", len(readings))
        except Exception as e:
            logger.error("Telemetry publish error: %s", e)
        finally:
            self._schedule_next()

    def _read_all_inputs(self) -> list[dict]:
        """Build telemetry readings from all readable pins.

        Three sources:
          1. Generic input pins (gpio_input / analog_input without sensor_type / oneWire) — read directly via handlers.
          2. Sensor pins (sensor_type set) — pull the latest reading from the sensor
             instance maintained by the ProtectionMonitor (which is the sole owner
             of high-frequency sensor I/O — telemetry just publishes the latest).
          3. Sensor pins WITHOUT a protection monitor (configured but protection thread
             not running) — read on-demand here.
        """
        from .command_executor import _get_handler, get_sensor_instance

        readings: list[dict] = []

        for pin in self.config.pins:
            # ---- Sensor-typed pins: publish whatever the sensor's state has -------
            if pin.sensor_type:
                sensor = get_sensor_instance(pin)
                if sensor is None:
                    continue

                try:
                    if pin.sensor_type == "current_acs758":
                        last = sensor.state.last_reading
                        if last is None:
                            # No protection thread reading it yet — do a one-shot read.
                            last = sensor.read_rms()
                        if last is not None:
                            readings.append(sensor.to_telemetry(last))
                    elif pin.sensor_type == "ultrasonic_jsn_sr04t":
                        last = sensor.state.last_reading
                        if last is None:
                            last = sensor.read()
                        if last is not None:
                            readings.extend(sensor.to_telemetry(last))
                    else:
                        logger.debug("Unknown sensor_type for telemetry: %s", pin.sensor_type)
                except Exception as e:  # noqa: BLE001
                    logger.debug("Sensor read failed for pin %d (%s): %s",
                                 pin.physical_pin, pin.sensor_type, e)
                continue

            # ---- Generic input pins ---------------------------------------------
            if pin.protocol not in ("gpio_input", "analog_input", "oneWire"):
                continue

            handler = _get_handler(pin.protocol)
            if not handler:
                continue

            try:
                value = handler.read(pin)
                if value is not None:
                    source_key = pin.label or f"pin_{pin.physical_pin}"
                    readings.append({
                        "sourceKey": source_key,
                        "value": value,
                    })
            except Exception as e:
                logger.debug("Failed to read pin %d: %s", pin.physical_pin, e)

        return readings
