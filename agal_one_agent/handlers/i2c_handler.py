"""I2C handler using smbus2."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import smbus2
    _I2C_AVAILABLE = True
except ImportError:
    _I2C_AVAILABLE = False
    logger.warning("smbus2 not available - I2C operations will be simulated")


class I2CHandler:
    def __init__(self, bus_number: int = 1):
        self._buses: dict[int, "smbus2.SMBus"] = {}
        self._default_bus = bus_number
        if _I2C_AVAILABLE:
            self._buses[bus_number] = smbus2.SMBus(bus_number)

    def _get_bus(self, pin) -> Optional["smbus2.SMBus"]:
        """Get the SMBus instance for a pin's bus_id (e.g., 'I2C1' -> bus 1)."""
        if not _I2C_AVAILABLE:
            return None
        bus_num = self._default_bus
        if pin.bus_id:
            # Extract bus number from bus_id like "I2C1", "I2C0"
            try:
                bus_num = int(pin.bus_id.replace("I2C", ""))
            except (ValueError, AttributeError):
                bus_num = self._default_bus
        if bus_num not in self._buses:
            self._buses[bus_num] = smbus2.SMBus(bus_num)
        return self._buses[bus_num]

    def _get_address(self, pin) -> int:
        """Get I2C address from pin config, falling back to gpio_number."""
        if pin.i2c_address is not None:
            return pin.i2c_address
        return pin.gpio_number or 0x48

    def read(self, pin) -> Optional[int]:
        bus = self._get_bus(pin)
        if not bus:
            logger.debug("Simulated I2C read")
            return 0
        address = self._get_address(pin)
        try:
            if pin.i2c_register is not None:
                return bus.read_byte_data(address, pin.i2c_register)
            return bus.read_byte(address)
        except Exception as e:
            logger.error("I2C read error at 0x%02x: %s", address, e)
            return None

    def write(self, pin, value) -> int:
        bus = self._get_bus(pin)
        if not bus:
            logger.debug("Simulated I2C write: %s", value)
            return int(value)
        address = self._get_address(pin)
        try:
            if pin.i2c_register is not None:
                bus.write_byte_data(address, pin.i2c_register, int(value))
            else:
                bus.write_byte(address, int(value))
            return int(value)
        except Exception as e:
            logger.error("I2C write error at 0x%02x: %s", address, e)
            raise

    def close(self):
        for bus in self._buses.values():
            try:
                bus.close()
            except Exception:
                pass
        self._buses.clear()
