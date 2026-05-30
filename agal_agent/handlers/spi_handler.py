"""SPI handler using spidev."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import spidev
    _SPI_AVAILABLE = True
except ImportError:
    _SPI_AVAILABLE = False
    logger.warning("spidev not available - SPI operations will be simulated")


class SpiHandler:
    def __init__(self, bus: int = 0, device: int = 0):
        self._spi = None
        if _SPI_AVAILABLE:
            self._spi = spidev.SpiDev()
            self._spi.open(bus, device)
            self._spi.max_speed_hz = 1000000

    def read(self, pin) -> Optional[int]:
        if not self._spi:
            logger.debug("Simulated SPI read")
            return 0
        try:
            result = self._spi.xfer2([0x00])
            return result[0] if result else None
        except Exception as e:
            logger.error("SPI read error: %s", e)
            return None

    def write(self, pin, value) -> int:
        if not self._spi:
            logger.debug("Simulated SPI write: %s", value)
            return int(value)
        try:
            self._spi.xfer2([int(value) & 0xFF])
            return int(value)
        except Exception as e:
            logger.error("SPI write error: %s", e)
            raise
