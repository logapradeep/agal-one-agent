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

    @property
    def line(self) -> Optional[tuple[str, int]]:
        """(chip LABEL, line) — how a port names a GPIO since contracts v1.9.0 (ADR-024).
        Broadcom numbers (``gpio``) exist on one board family; this works on every Linux board."""
        chip, line = self.transport.get("chip"), self.transport.get("line")
        return (str(chip), int(line)) if chip and line is not None else None


# Facts about how a line is wired or read, not about which line it is.
_WIRING_FACTS = frozenset({"direction", "activeLow", "pull", "debounceMs", "sampleMs", "measure", "transform",
                           "frequencyHz", "dutyCyclePct", "polarity", "unit"})


def port_identity(transport: dict) -> tuple:
    """Which physical port a transport names — the same for a port's stamp in a compiled bundle
    and in a testPort command, whatever its direction or wiring facts (active low, pull, scale)."""
    return tuple(sorted((k, str(v)) for k, v in (transport or {}).items() if k not in _WIRING_FACTS))


def physical_port(transport: dict) -> tuple:
    """The physical port a transport uses, whatever it is used as: a GPIO line is the same line
    whether a card names it as a ``gpio`` or a ``pwm`` port. Other kinds: :func:`port_identity`.
    (A chip label + line and a Broadcom number are not related here: nothing names a board.)"""
    t = transport or {}
    if str(t.get("kind", "gpio")) in ("gpio", "pwm"):
        if t.get("chip") and t.get("line") is not None:
            return ("line", str(t["chip"]), int(t["line"]))
        if t.get("gpioNumber") is not None:
            return ("gpio", int(t["gpioNumber"]))
    return port_identity(t)


def describe_port(transport: dict) -> str:
    """A port in words, for a refusal: ``line 17 of pinctrl-bcm2835``, ``GPIO 17``, ``i2c bus 1 …``."""
    p = physical_port(transport)
    if p and p[0] == "line":
        return f"line {p[2]} of {p[1]}"
    if p and p[0] == "gpio":
        return f"GPIO {p[1]}"
    t = transport or {}
    return " ".join(f"{k} {v}" for k, v in sorted(t.items()) if k not in _WIRING_FACTS)[:80]


class IOAdapter:
    def has_port(self, port: PortRef) -> bool:
        return True

    def read_input(self, port: PortRef) -> Any:
        raise NotImplementedError

    def write_output(self, port: PortRef, value: Any) -> None:
        raise NotImplementedError

    def set_change_listener(self, fn: Callable[[], None]) -> None:
        """Optional: called when an input may have changed (fast path)."""

    def release_output(self, port: PortRef) -> None:
        """Let go of an output the program no longer drives (after it was written to its safe state)."""

    def close(self) -> None:
        pass


class SimulatedIO(IOAdapter):
    """Scriptable ports. Inputs default to 0/False; outputs are recorded."""

    def __init__(self, allowed_gpios: Optional[set[int]] = None):
        self._inputs: dict[tuple[str, str], Any] = {}
        self.outputs: dict[tuple[str, str], Any] = {}
        self.writes: list[tuple[str, str, Any]] = []
        self.released: list[tuple[str, str, dict]] = []
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

    def release_output(self, port: PortRef) -> None:
        self.released.append((port.asset_id, port.source_key, dict(port.transport)))

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
                 sensor_by_key: Optional[dict[str, Any]] = None, lines=None, analog=None):
        from ..command_executor import _get_handler
        self._get_handler = _get_handler
        self._configured = configured_gpios
        self._sensors = sensor_by_key or {}
        self._lock = threading.Lock()
        self._lines = lines  # ports.gpiochip.LineIO — the character-device path (ADR-024)
        self._analog = analog  # ports.analog.AnalogReader — an AI port read from its own wiring facts (v1.9.1)

    def has_port(self, port: PortRef) -> bool:
        if port.kind == "gpio" and port.line is not None:
            # Only that the chip label exists on this board is checked here. That one line is
            # claimed by one card only is checked when the program is built (BlockRuntime._build);
            # that the line is a port of this node's table and linked to that card is the cloud's
            # half of RBAC-01 — a card's own portBindings[].transport is stamped as it is today.
            return self._lines is not None and self._lines.has(port.line[0])
        if port.kind in ("gpio", "pwm"):
            if port.gpio is None:
                return False
            return self._configured is None or port.gpio in self._configured
        if port.kind in ("i2c", "spi", "uart", "onewire", "virtual"):
            if self._analog is not None and self._analog.supports(port.transport):
                return True
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
        if self._analog is not None and self._analog.supports(port.transport):
            # An AI port of the node's table: the channel, how it is read and its scale all
            # come with the transport — the value is already in the port's unit (amps, bar …).
            try:
                return self._analog.read(port.transport)
            except Exception as e:  # noqa: BLE001
                logger.debug("analog read %s failed: %s", port.source_key, e)
                return None
        if port.kind == "gpio" and port.line is not None and self._lines is not None:
            try:
                return self._lines.read(port.line[0], port.line[1], bool(port.transport.get("activeLow")))
            except Exception as e:  # noqa: BLE001
                logger.debug("line read %s failed: %s", port.source_key, e)
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

    def release_output(self, port: PortRef) -> None:
        if port.kind == "gpio" and port.line is not None and self._lines is not None:
            self._lines.release(port.line[0], port.line[1])
        elif port.kind == "pwm" and port.gpio is not None:
            from ..handlers import pwm_handler  # noqa: PLC0415
            pwm_handler.release(port.gpio)
        # A Broadcom-numbered line stays set up as an output, at the level just written.

    def write_output(self, port: PortRef, value: Any) -> None:
        if port.kind == "pwm" and port.gpio is not None:
            handler = self._get_handler("pwm")
            pin = _Pin(port.gpio, "pwm", port.source_key)
            pin.pwm_frequency_hz = port.transport.get("frequencyHz")
            pin.pwm_polarity = port.transport.get("polarity")
            if handler:
                handler.write(pin, float(value))
            return
        if port.kind == "gpio" and port.line is not None and self._lines is not None:
            self._lines.write(port.line[0], port.line[1], bool(value), bool(port.transport.get("activeLow")))
            return
        if port.gpio is None:
            raise ValueError(f"port {port.source_key} names neither a chip label + line nor a GPIO number")
        out = bool(value)
        if port.transport.get("activeLow"):
            out = not out
        with self._lock:
            handler = self._get_handler("gpio_output")
            pin = _Pin(port.gpio, "gpio_output", port.source_key)
            if handler:
                # the level (inverted for active low), and whether it is ON — what an OTA update waits on
                handler.write(pin, 1 if out else 0, logical=bool(value))
