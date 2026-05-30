"""UART handler using pyserial."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import serial
    _SERIAL_AVAILABLE = True
except ImportError:
    _SERIAL_AVAILABLE = False
    logger.warning("pyserial not available - UART operations will be simulated")


class UartHandler:
    def __init__(self, port: str = "/dev/serial0", baudrate: int = 9600):
        self._default_port = port
        self._default_baudrate = baudrate
        self._ports: dict[str, "serial.Serial"] = {}
        if _SERIAL_AVAILABLE:
            try:
                self._ports[port] = serial.Serial(port, baudrate, timeout=1)
            except Exception as e:
                logger.warning("Could not open UART port %s: %s", port, e)

    def _get_port(self, pin) -> Optional["serial.Serial"]:
        """Get or create a serial port for the pin's baud rate / bus_id."""
        if not _SERIAL_AVAILABLE:
            return None
        port_name = self._default_port
        baud = pin.uart_baud_rate or self._default_baudrate

        # Use bus_id as port identifier if specified (e.g., "UART0" -> /dev/serial0)
        if pin.bus_id:
            try:
                idx = int(pin.bus_id.replace("UART", ""))
                port_name = f"/dev/serial{idx}"
            except (ValueError, AttributeError):
                pass

        key = f"{port_name}:{baud}"
        if key not in self._ports:
            try:
                self._ports[key] = serial.Serial(port_name, baud, timeout=1)
            except Exception as e:
                logger.warning("Could not open UART %s@%d: %s", port_name, baud, e)
                return None
        return self._ports.get(key)

    def read(self, pin) -> Optional[str]:
        ser = self._get_port(pin)
        if not ser:
            logger.debug("Simulated UART read")
            return None
        try:
            if ser.in_waiting:
                return ser.readline().decode("utf-8").strip()
            return None
        except Exception as e:
            logger.error("UART read error: %s", e)
            return None

    def write(self, pin, value) -> str:
        data = str(value)
        ser = self._get_port(pin)
        if not ser:
            logger.debug("Simulated UART write: %s", data)
            return data
        try:
            ser.write((data + "\n").encode("utf-8"))
            return data
        except Exception as e:
            logger.error("UART write error: %s", e)
            raise

    def close(self):
        for ser in self._ports.values():
            try:
                ser.close()
            except Exception:
                pass
        self._ports.clear()
