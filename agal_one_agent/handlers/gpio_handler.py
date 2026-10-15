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


_setup_pins: dict[int, object] = {}  # gpio -> mode (GPIO.IN or GPIO.OUT), or (GPIO.IN, pull) for a pulled input

_PUD = {"up": "PUD_UP", "down": "PUD_DOWN", "none": "PUD_OFF"}


class GpioHandler:
    def _setup(self, pin, mode, initial=None, pull=None):
        gpio = pin.gpio_number
        if gpio is None:
            raise ValueError(f"No GPIO number for physical pin {pin.physical_pin}")
        state = mode if pull is None else (mode, pull)
        if _setup_pins.get(gpio) != state:
            if _GPIO_AVAILABLE:
                if pull is not None:
                    # A port's `pull` is the input's bias (programs/README.md §7.2), set with the pin.
                    if pull not in _PUD:
                        raise ValueError(f"GPIO{gpio}: unknown pull {pull!r} (up, down or none)")
                    GPIO.setup(gpio, mode, pull_up_down=getattr(GPIO, _PUD[pull]))
                elif initial is None:
                    GPIO.setup(gpio, mode)
                else:
                    # An output starts at the level it is about to be given, not at whatever the
                    # pin latch holds — LOW is ON to an active-low relay board.
                    GPIO.setup(gpio, mode, initial=initial)
            _setup_pins[gpio] = state

    def read(self, pin) -> Optional[int]:
        gpio = pin.gpio_number
        if gpio is None:
            return None
        self._setup(pin, GPIO.IN if _GPIO_AVAILABLE else None, pull=getattr(pin, "pull", None))
        if _GPIO_AVAILABLE:
            return GPIO.input(gpio)
        logger.debug("Simulated read pin GPIO%d = 0", gpio)
        return 0

    def write(self, pin, value) -> int:
        gpio = pin.gpio_number
        if gpio is None:
            raise ValueError(f"No GPIO number for physical pin {pin.physical_pin}")
        out = 1 if value else 0
        self._setup(pin, GPIO.OUT if _GPIO_AVAILABLE else None, initial=out)
        if _GPIO_AVAILABLE:
            GPIO.output(gpio, out)
        logger.info("GPIO%d set to %d", gpio, out)
        return out
