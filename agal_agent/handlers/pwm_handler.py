"""PWM handler using RPi.GPIO."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import RPi.GPIO as GPIO
    _GPIO_AVAILABLE = True
except (ImportError, RuntimeError):
    _GPIO_AVAILABLE = False

_pwm_instances: dict[int, object] = {}


class PwmHandler:
    def read(self, pin) -> Optional[float]:
        return None  # PWM is output-only

    def write(self, pin, value) -> float:
        gpio = pin.gpio_number
        if gpio is None:
            raise ValueError(f"No GPIO number for physical pin {pin.physical_pin}")

        duty_cycle = max(0.0, min(100.0, float(value)))

        if _GPIO_AVAILABLE:
            if gpio not in _pwm_instances:
                GPIO.setup(gpio, GPIO.OUT)
                pwm = GPIO.PWM(gpio, 1000)  # 1kHz default
                pwm.start(duty_cycle)
                _pwm_instances[gpio] = pwm
            else:
                _pwm_instances[gpio].ChangeDutyCycle(duty_cycle)

        logger.info("PWM GPIO%d duty_cycle=%.1f%%", gpio, duty_cycle)
        return duty_cycle
