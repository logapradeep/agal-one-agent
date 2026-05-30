"""1-Wire handler using w1thermsensor."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from w1thermsensor import W1ThermSensor
    _W1_AVAILABLE = True
except (ImportError, Exception):
    _W1_AVAILABLE = False
    logger.warning("w1thermsensor not available - 1-Wire operations will be simulated")


class OneWireHandler:
    def __init__(self):
        self._sensors: list = []
        if _W1_AVAILABLE:
            try:
                self._sensors = W1ThermSensor.get_available_sensors()
                logger.info("Found %d 1-Wire sensors", len(self._sensors))
            except Exception as e:
                logger.warning("Could not enumerate 1-Wire sensors: %s", e)

    def _find_sensor(self, pin):
        """Find a 1-Wire sensor matching the pin's one_wire_device_id."""
        if not self._sensors:
            return None
        # Match by device ID if specified
        if pin.one_wire_device_id:
            for sensor in self._sensors:
                if sensor.id == pin.one_wire_device_id:
                    return sensor
            logger.warning(
                "1-Wire sensor %s not found, falling back to first sensor",
                pin.one_wire_device_id,
            )
        return self._sensors[0] if self._sensors else None

    def read(self, pin) -> Optional[float]:
        if not _W1_AVAILABLE:
            logger.debug("Simulated 1-Wire read = 25.0")
            return 25.0
        sensor = self._find_sensor(pin)
        if not sensor:
            logger.warning("No 1-Wire sensor available for pin %d", pin.physical_pin)
            return None
        try:
            return sensor.get_temperature()
        except Exception as e:
            logger.error("1-Wire read error: %s", e)
            return None

    def write(self, pin, value):
        raise NotImplementedError("1-Wire is read-only")
