"""GPIO read/write handler using RPi.GPIO."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    _GPIO_AVAILABLE = True
except (ImportError, RuntimeError):
    _GPIO_AVAILABLE = False
    logger.warning("RPi.GPIO not available - GPIO operations will be simulated")


_setup_pins: dict[int, int] = {}  # gpio -> mode (GPIO.IN or GPIO.OUT)
# gpio -> (ON or OFF as last written, the pin's name) — what an OTA update waits on (outputs_on).
# ON/OFF, not the pin's level: the block runtime writes an active-low port through this handler with
# the level already inverted (blocks/io.HardwareIO.write_output passes ``logical``), and an active-low
# relay that is OFF is held HIGH. The legacy executor has no active-low: its level is its ON/OFF.
_levels: dict[int, tuple[int, str]] = {}


def outputs_on() -> list[str]:
    """The GPIO outputs last switched ON — a restart switches them off (ota_updater waits)."""
    return [f"{name} (GPIO{gpio})" for gpio, (on, name) in sorted(_levels.items()) if on]


class GpioHandler:
    def _setup(self, pin, mode, initial=None):
        gpio = pin.gpio_number
        if gpio is None:
            raise ValueError(f"No GPIO number for physical pin {pin.physical_pin}")
        if _setup_pins.get(gpio) != mode:
            if _GPIO_AVAILABLE:
                if initial is None:
                    GPIO.setup(gpio, mode)
                else:
                    # An output starts at the level it is about to be given, not at whatever the
                    # pin latch holds — LOW is ON to an active-low relay board.
                    GPIO.setup(gpio, mode, initial=initial)
            _setup_pins[gpio] = mode

    def read(self, pin) -> Optional[int]:
        gpio = pin.gpio_number
        if gpio is None:
            return None
        self._setup(pin, GPIO.IN if _GPIO_AVAILABLE else None)
        _levels.pop(gpio, None)  # an input now
        if _GPIO_AVAILABLE:
            return GPIO.input(gpio)
        logger.debug("Simulated read pin GPIO%d = 0", gpio)
        return 0

    def write(self, pin, value, logical=None) -> int:
        """Drive the pin to ``value`` (its level). ``logical``: whether that means ON, when it is not
        the level itself (an active-low port); None — the level is ON/OFF (the legacy executor)."""
        gpio = pin.gpio_number
        if gpio is None:
            raise ValueError(f"No GPIO number for physical pin {pin.physical_pin}")
        out = 1 if value else 0
        self._setup(pin, GPIO.OUT if _GPIO_AVAILABLE else None, initial=out)
        if _GPIO_AVAILABLE:
            GPIO.output(gpio, out)
        on = out if logical is None else (1 if logical else 0)
        _levels[gpio] = (on, getattr(pin, "label", None) or getattr(pin, "assigned_to", None) or f"pin {pin.physical_pin}")
        logger.info("GPIO%d set to %d", gpio, out)
        return out
