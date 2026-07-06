"""Sensor-specific drivers built on top of the generic bus handlers.

Distinct from `handlers/`:
  - `handlers/` = bus protocol drivers (GPIO, I2C, SPI, UART, 1-Wire, PWM).
  - `sensors/`  = real-world device drivers (ACS758 current, JSN-SR04T
    ultrasonic) that USE one or more bus protocols and produce calibrated,
    domain-meaningful readings (amps, metres).

Each sensor module exposes a class with a `.read()` method returning a list of
telemetry readings in the canonical wire shape
    {"sourceKey": str, "kind": str, "value": float, "unit": str}
plus a `.start_background()` / `.stop_background()` pair for sensors that need
their own poll thread (e.g., the protection monitor for dry-run cutoff).

All hardware imports are guarded — on a non-Pi dev machine (macOS), drivers
fall back to simulation so unit tests + the bench harness can run unmodified.
"""

from .current_sensor import CurrentSensorACS758
from .ultrasonic_sensor import UltrasonicSensorJsnSr04t
from .bno055 import ImuSensorBno055
from .protection import ProtectionMonitor

__all__ = [
    "CurrentSensorACS758",
    "UltrasonicSensorJsnSr04t",
    "ImuSensorBno055",
    "ProtectionMonitor",
]
