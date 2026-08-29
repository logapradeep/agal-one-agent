"""Periodic sensor reading and telemetry publishing.

Two channels (ADR-013 §2):
  * **Live channel** — the current MQTT/HTTP payload shape, published at the
    adaptive live cadence (fast while an app is watching, else idle). Feeds
    ``state.byPort``/denorms; never persisted per reading.
  * **Durable history channel** — every reading is appended to the on-node
    SQLite ring buffer *first* (via ``telemetry_buffer``); a separate uploader
    thread drains it in ``telemetry_batch`` payloads with backoff. This is what
    survives a network outage.

The publisher owns the sampling loop + the live publish + the buffer append.
The uploader (``telemetry_uploader``) owns the drain. Both are optional: when no
buffer is wired the publisher behaves exactly as before (live-only).
"""

import logging
import threading
import time
from typing import Optional

from .config import AgentConfig
from .mqtt_client import AgalOneMqttClient
from .live_cadence import LiveCadenceController
from .telemetry_buffer import TIER_PRIORITY, DEFAULT_PRIORITY

logger = logging.getLogger(__name__)


class TelemetryPublisher:
    """Reads all input pins periodically, buffers durably, publishes live."""

    def __init__(self, config: AgentConfig, mqtt_client: AgalOneMqttClient,
                 buffer=None, cadence: Optional[LiveCadenceController] = None,
                 clock_ok: Optional[callable] = None):
        self.config = config
        self.mqtt_client = mqtt_client
        # Durable ring buffer (ADR-013 P0). None ⇒ live-only (legacy behaviour).
        self.buffer = buffer
        # Adaptive live cadence (ADR-013 §9-D3). None ⇒ fixed interval_seconds.
        self.cadence = cadence
        # Returns True once the wall clock is trustworthy (NTP synced). Readings
        # sampled before that are buffered with ts_uncertain=True (§5.4). Default
        # assumes the clock is fine (dev/bench) unless a probe is provided.
        self._clock_ok = clock_ok or (lambda: True)
        self._timer: Optional[threading.Timer] = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._schedule_next()
        logger.info(
            "Telemetry publisher started (live cadence=%ds%s, buffer=%s)",
            self._live_interval(),
            " adaptive" if self.cadence else "",
            "on" if self.buffer else "off",
        )

    def stop(self) -> None:
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _live_interval(self) -> int:
        if self.cadence is not None:
            return self.cadence.current_interval()
        return self.config.telemetry.interval_seconds

    def _schedule_next(self) -> None:
        if not self._running:
            return
        # Re-evaluate the cadence every cycle so a watch signal takes effect on
        # the next tick (no thread churn).
        self._timer = threading.Timer(self._live_interval(), self._publish_cycle)
        self._timer.daemon = True
        self._timer.start()

    def _publish_cycle(self) -> None:
        try:
            readings = self._read_all_inputs()
            if readings:
                # 1) Durable history channel FIRST — append before publishing so
                #    a drop between sample and publish still leaves the reading on
                #    disk (ADR-013 §5.2 "appends to the buffer first").
                if self.buffer is not None:
                    try:
                        self.buffer.append(
                            readings,
                            ts_ms=int(time.time() * 1000),
                            ts_uncertain=not self._clock_ok(),
                            priority=self._buffer_priority(),
                        )
                    except Exception as e:  # noqa: BLE001 — buffer must never
                        logger.error("Buffer append failed: %s", e)  # break live
                # 2) Live channel (unchanged shape; legacy servers keep working).
                self.mqtt_client.publish_telemetry(readings)
                logger.debug("Published %d telemetry readings", len(readings))
        except Exception as e:
            logger.error("Telemetry publish error: %s", e)
        finally:
            self._schedule_next()

    def _buffer_priority(self) -> int:
        """Drain priority for this node's readings — the finest configured tier
        wins (raw drains first, §5.3). Absent per-port tier config we default to
        "normal"; the server remains the storage-tier enforcement point."""
        best = DEFAULT_PRIORITY
        for pin in self.config.pins:
            tier = None
            if isinstance(pin.protection, dict):
                tier = pin.protection.get("telemetry_tier")
            tier = tier or getattr(pin, "telemetry_tier", None)
            if tier and tier in TIER_PRIORITY:
                best = max(best, TIER_PRIORITY[tier])
        return best

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
                    elif pin.sensor_type == "bno055_9dof":
                        # No protection thread reads the IMU — always sample fresh
                        # so orientation rows aren't stale between cycles.
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
