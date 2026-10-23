"""PWM handler using RPi.GPIO.

Honours the contracted PortTransport `pwm` variant
(Agal/contracts/schemas/asset.schema.json v3.1: frequencyHz + dutyCyclePct +
polarity), which reaches the daemon snake_cased on the pin config
(pwm_frequency_hz / pwm_duty_cycle_pct / pwm_polarity — see config.PinConfig).

Semantics:
- write(pin, value): `value` is the LOGICAL duty cycle in percent (0-100),
  per the contract ("the written value is interpreted as a percentage").
- polarity "inverted" flips the hardware duty (100 - logical) for active-low
  drivers; the returned/acked value stays the logical percentage.
- frequency comes from pin.pwm_frequency_hz (default 1000 Hz); if a pin's
  configured frequency changes between writes, ChangeFrequency is applied.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import RPi.GPIO as GPIO
    _GPIO_AVAILABLE = True
except (ImportError, RuntimeError):
    _GPIO_AVAILABLE = False

DEFAULT_FREQUENCY_HZ = 1000.0

# gpio -> (pwm instance, current frequency in Hz)
_pwm_instances: dict[int, tuple[object, float]] = {}
# gpio -> (logical duty last written, the pin's name) — what an OTA update waits on (outputs_on)
_duties: dict[int, tuple[float, str]] = {}


def _effective_frequency(pin) -> float:
    freq = getattr(pin, "pwm_frequency_hz", None)
    if freq is None or float(freq) <= 0:
        return DEFAULT_FREQUENCY_HZ
    return float(freq)


def _hardware_duty(pin, logical_duty: float) -> float:
    """Map logical duty -> hardware duty, honouring polarity."""
    if getattr(pin, "pwm_polarity", None) == "inverted":
        return 100.0 - logical_duty
    return logical_duty


def release(gpio: int) -> None:
    """Stop and forget the PWM channel on a GPIO.

    Called when a syncPinConfig remaps a pin away from the pwm protocol so a
    plain GpioHandler (or nothing) can take the pin over cleanly.
    """
    _duties.pop(gpio, None)
    entry = _pwm_instances.pop(gpio, None)
    if entry is None:
        return
    pwm, _ = entry
    try:
        pwm.stop()
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to stop PWM on GPIO%d: %s", gpio, e)
    logger.info("PWM GPIO%d released", gpio)


def active_gpios() -> set[int]:
    """GPIOs with a live PWM channel (used by syncPinConfig reconciliation)."""
    return set(_pwm_instances.keys())


def outputs_on() -> list[str]:
    """The legacy PWM outputs last written above 0 % — a restart stops them (ota_updater waits)."""
    return [f"{name} (PWM GPIO{gpio})" for gpio, (duty, name) in sorted(_duties.items()) if duty > 0]


def outputs_on_items() -> list[dict]:
    """The same, for an update's report (agent 0.4.4): each by its config name — a legacy output has no card."""
    return [{"name": str(name or f"PWM GPIO{gpio}")} for gpio, (duty, name) in sorted(_duties.items()) if duty > 0]


class PwmHandler:
    def read(self, pin) -> Optional[float]:
        return None  # PWM is output-only

    def write(self, pin, value) -> float:
        gpio = pin.gpio_number
        if gpio is None:
            raise ValueError(f"No GPIO number for physical pin {pin.physical_pin}")

        logical_duty = max(0.0, min(100.0, float(value)))
        frequency = _effective_frequency(pin)
        hw_duty = _hardware_duty(pin, logical_duty)

        if _GPIO_AVAILABLE:
            entry = _pwm_instances.get(gpio)
            if entry is None:
                GPIO.setup(gpio, GPIO.OUT)
                pwm = GPIO.PWM(gpio, frequency)
                pwm.start(hw_duty)
                _pwm_instances[gpio] = (pwm, frequency)
            else:
                pwm, current_freq = entry
                if current_freq != frequency:
                    pwm.ChangeFrequency(frequency)
                    _pwm_instances[gpio] = (pwm, frequency)
                pwm.ChangeDutyCycle(hw_duty)

        _duties[gpio] = (logical_duty, getattr(pin, "label", None) or getattr(pin, "assigned_to", None) or f"pin {pin.physical_pin}")
        logger.info(
            "PWM GPIO%d duty=%.1f%% (hw=%.1f%%, freq=%.1fHz, polarity=%s)",
            gpio, logical_duty, hw_duty, frequency,
            getattr(pin, "pwm_polarity", None) or "normal",
        )
        return logical_duty
