"""Port adapters for the block runtime.

The runtime addresses I/O by the resolved transport in the compiled bundle
(``ports[sourceKey]`` → asset.schema.json PortTransport). Two adapters:

* :class:`SimulatedIO` — in-memory inputs/outputs for the simulator and tests
  (the "simulated bench" of _audit/99 P1).
* :class:`HardwareIO` — GPIO outputs/inputs through the existing handlers, analog
  inputs through the registered sensor drivers (current, ultrasonic). The node's
  configured pins gate which GPIO numbers exist (R-4/R-5: a port the node has
  not reported is rejected at compile time).

Debounce is applied by the runtime's sampler using the timestamps it passes in,
so adapters only return the *raw* value.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PortRef:
    asset_id: str
    source_key: str
    transport: dict  # PortTransport (kind, gpioNumber, direction, ...)

    @property
    def kind(self) -> str:
        return str(self.transport.get("kind", "gpio"))

    @property
    def gpio(self) -> Optional[int]:
        g = self.transport.get("gpioNumber")
        return int(g) if g is not None else None


class IOAdapter:
    def has_port(self, port: PortRef) -> bool:
        return True

    def read_input(self, port: PortRef) -> Any:
        raise NotImplementedError

    def write_output(self, port: PortRef, value: Any) -> None:
        raise NotImplementedError

    def set_change_listener(self, fn: Callable[[], None]) -> None:
        """Optional: called when an input may have changed (fast path)."""

    def close(self) -> None:
        pass


class SimulatedIO(IOAdapter):
    """Scriptable ports. Inputs default to 0/False; outputs are recorded."""

    def __init__(self, allowed_gpios: Optional[set[int]] = None):
        self._inputs: dict[tuple[str, str], Any] = {}
        self.outputs: dict[tuple[str, str], Any] = {}
        self.writes: list[tuple[str, str, Any]] = []
        self._allowed = allowed_gpios
        self._listener: Optional[Callable[[], None]] = None

    def has_port(self, port: PortRef) -> bool:
        if self._allowed is None:
            return True
        return port.gpio in self._allowed

    def set_input(self, asset_id: str, source_key: str, value: Any) -> None:
        self._inputs[(asset_id, source_key)] = value
        if self._listener:
            self._listener()

    def read_input(self, port: PortRef) -> Any:
        return self._inputs.get((port.asset_id, port.source_key), 0)

    def write_output(self, port: PortRef, value: Any) -> None:
        self.outputs[(port.asset_id, port.source_key)] = value
        self.writes.append((port.asset_id, port.source_key, value))

    def output(self, asset_id: str, source_key: str, default=None) -> Any:
        return self.outputs.get((asset_id, source_key), default)

    def set_change_listener(self, fn: Callable[[], None]) -> None:
        self._listener = fn


class _Pin:
    """Minimal PinConfig look-alike the GPIO/PWM handlers accept."""

    def __init__(self, gpio: int, protocol: str, label: str):
        self.gpio_number = gpio
        self.physical_pin = gpio
        self.protocol = protocol
        self.label = label
        self.pwm_frequency_hz = None
        self.pwm_duty_cycle_pct = None
        self.pwm_polarity = None


class HardwareIO(IOAdapter):
    """GPIO + sensor-backed ports on the node.

    ``configured_gpios``: BCM numbers present in config.pins (None = accept any).
    ``sensor_by_key``: sourceKey → sensor instance with ``read_rms()`` (current) or
    ``read()`` (ultrasonic) for analog inputs; falls back to a raw GPIO read.
    """

    def __init__(self, configured_gpios: Optional[set[int]] = None,
                 sensor_by_key: Optional[dict[str, Any]] = None):
        from ..command_executor import _get_handler
        self._get_handler = _get_handler
        self._configured = configured_gpios
        self._sensors = sensor_by_key or {}
        self._lock = threading.Lock()

    def has_port(self, port: PortRef) -> bool:
        if port.kind in ("gpio", "pwm"):
            if port.gpio is None:
                return False
            return self._configured is None or port.gpio in self._configured
        if port.kind in ("i2c", "spi", "uart", "onewire", "virtual"):
            return port.source_key in self._sensors or port.kind != "virtual"
        return False

    def read_input(self, port: PortRef) -> Any:
        sensor = self._sensors.get(port.source_key)
        if sensor is not None:
            try:
                if hasattr(sensor, "read_rms"):
                    r = sensor.read_rms()
                    return None if getattr(r, "quality", "good") == "bad" else float(r.rms_a)
                if hasattr(sensor, "read"):
                    r = sensor.read()
                    if hasattr(r, "percent_full"):
                        return None if getattr(r, "quality", "good") == "bad" else float(r.percent_full)
                    return r
            except Exception as e:  # noqa: BLE001
                logger.debug("sensor read %s failed: %s", port.source_key, e)
                return None
        if port.kind == "gpio" and port.gpio is not None:
            with self._lock:
                handler = self._get_handler("gpio_input")
                pin = _Pin(port.gpio, "gpio_input", port.source_key)
                v = handler.read(pin) if handler else None
            if v is None:
                return None
            v = bool(v)
            return (not v) if port.transport.get("activeLow") else v
        return None

    def write_output(self, port: PortRef, value: Any) -> None:
        if port.kind == "pwm" and port.gpio is not None:
            handler = self._get_handler("pwm")
            pin = _Pin(port.gpio, "pwm", port.source_key)
            pin.pwm_frequency_hz = port.transport.get("frequencyHz")
            pin.pwm_polarity = port.transport.get("polarity")
            if handler:
                handler.write(pin, float(value))
            return
        if port.gpio is None:
            raise ValueError(f"port {port.source_key} has no GPIO number")
        out = bool(value)
        if port.transport.get("activeLow"):
            out = not out
        with self._lock:
            handler = self._get_handler("gpio_output")
            pin = _Pin(port.gpio, "gpio_output", port.source_key)
            if handler:
                handler.write(pin, 1 if out else 0)
