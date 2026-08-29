"""JSN-SR04T v2 waterproof ultrasonic distance sensor.

Wiring:
    JSN-SR04T VCC   → RPi 5 V
    JSN-SR04T GND   → RPi GND
    JSN-SR04T TRIG  → RPi GPIO (any free pin; e.g., GPIO 23)
    JSN-SR04T ECHO  → RPi GPIO via VOLTAGE DIVIDER (1 kΩ + 2 kΩ) to drop the
                      5 V echo to ~3.3 V before feeding the RPi input. Do NOT
                      connect ECHO directly — sustained 5 V on a 3.3 V GPIO will
                      damage the SoC.

Operation:
    1. Pull TRIG HIGH for 10 µs, then LOW.
    2. ECHO goes HIGH after the burst is sent; remains HIGH until the echo
       returns. Pulse width ≈ round-trip time of sound.
    3. distance_m = (pulse_width_s * 343) / 2
        (343 m/s = speed of sound at 20 °C; varies ~0.6 m/s per °C)

For an open well, mount at the well lip with a PVC stilling tube to suppress
wave reflections from the water surface. Range is ~25 cm to ~4.5 m.

The driver returns calibrated depth-of-water given the well's `total_depth_m`
config — it converts "distance from sensor to water surface" into "depth of
water column."
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import RPi.GPIO as GPIO  # type: ignore

    _GPIO_AVAILABLE = True
except (ImportError, RuntimeError):
    _GPIO_AVAILABLE = False
    logger.warning("RPi.GPIO not available — JSN-SR04T reads will be simulated")


SPEED_OF_SOUND_M_PER_S = 343.0


@dataclass
class UltrasonicReading:
    timestamp: float
    distance_m: float          # raw distance from sensor face to water surface
    depth_m: float             # derived water column depth (total_depth - distance)
    percent_full: float        # depth_m / total_depth_m * 100
    quality: str = "good"


@dataclass
class UltrasonicState:
    last_reading: Optional[UltrasonicReading] = None
    # time.monotonic() of last emitted low-water event, or None if never fired.
    # We use None as the "never" sentinel because time.monotonic() can be a
    # small positive number early in process life — a `0.0` default would
    # falsely indicate "fired just now" on the first check.
    last_low_water_event_at: Optional[float] = None
    history: list[UltrasonicReading] = field(default_factory=list)


class UltrasonicSensorJsnSr04t:
    """JSN-SR04T driver.

    `pin.sensor_params` accepts:
      trig_gpio:                int    REQUIRED — BCM pin for TRIG
      echo_gpio:                int    REQUIRED — BCM pin for ECHO (must be after voltage divider)
      total_depth_m:            float  REQUIRED — total well/tank depth from sensor face to bottom
      timeout_s:                float  default 0.060  (echo timeout; >5 m round-trip)
      median_of:                int    default 3      (take median of N readings to reject outliers)
      low_level_threshold_pct:  float  default 15
      low_level_cooldown_s:     int    default 3600   (min seconds between repeat low-water events)
      sensor_label:             str    defaults to pin.label
    """

    def __init__(self, pin, state: Optional[UltrasonicState] = None) -> None:
        self.pin = pin
        params = pin.sensor_params or {}

        if "trig_gpio" not in params or "echo_gpio" not in params:
            raise ValueError(
                f"JSN-SR04T pin {pin.physical_pin} missing sensor_params.trig_gpio or .echo_gpio"
            )
        if "total_depth_m" not in params:
            raise ValueError(
                f"JSN-SR04T pin {pin.physical_pin} missing sensor_params.total_depth_m"
            )

        self.trig_gpio = int(params["trig_gpio"])
        self.echo_gpio = int(params["echo_gpio"])
        self.total_depth_m = float(params["total_depth_m"])
        self.timeout_s = float(params.get("timeout_s", 0.060))
        self.median_of = max(1, int(params.get("median_of", 3)))
        self.low_level_threshold_pct = float(params.get("low_level_threshold_pct", 15))
        self.low_level_cooldown_s = float(params.get("low_level_cooldown_s", 3600))

        self.source_key = pin.label or f"water_level_pin_{pin.physical_pin}"
        self.state = state or UltrasonicState()

        if _GPIO_AVAILABLE:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            GPIO.setup(self.trig_gpio, GPIO.OUT)
            GPIO.setup(self.echo_gpio, GPIO.IN)
            GPIO.output(self.trig_gpio, GPIO.LOW)
            time.sleep(0.05)  # let it settle on first init

    # ---- Reading -------------------------------------------------------

    def _single_pulse_distance_m(self) -> float:
        """One trig/echo cycle. Returns distance in metres, or NaN on timeout."""
        if not _GPIO_AVAILABLE:
            # Simulated: half-full well by default
            return self.total_depth_m / 2.0

        # 10 µs trigger
        GPIO.output(self.trig_gpio, GPIO.HIGH)
        time.sleep(0.00001)
        GPIO.output(self.trig_gpio, GPIO.LOW)

        # Wait for echo to go HIGH
        echo_start = time.monotonic()
        deadline = echo_start + self.timeout_s
        while GPIO.input(self.echo_gpio) == 0:
            if time.monotonic() > deadline:
                return float("nan")
        t_rise = time.monotonic()

        # Wait for echo to go LOW
        while GPIO.input(self.echo_gpio) == 1:
            if time.monotonic() > deadline:
                return float("nan")
        t_fall = time.monotonic()

        pulse_width_s = t_fall - t_rise
        return (pulse_width_s * SPEED_OF_SOUND_M_PER_S) / 2.0

    def read(self) -> UltrasonicReading:
        """Take `median_of` measurements, return the median to reject outliers."""
        samples = []
        for _ in range(self.median_of):
            d = self._single_pulse_distance_m()
            if d == d and d > 0:  # NaN/zero check
                samples.append(d)
            time.sleep(0.05)  # 50 ms between pulses to let echoes die out

        if not samples:
            reading = UltrasonicReading(
                timestamp=time.time(),
                distance_m=float("nan"),
                depth_m=float("nan"),
                percent_full=float("nan"),
                quality="bad",
            )
            self.state.last_reading = reading
            return reading

        samples.sort()
        distance_m = samples[len(samples) // 2]

        # Convert distance-to-surface to depth-of-water
        depth_m = max(0.0, self.total_depth_m - distance_m)
        percent_full = max(0.0, min(100.0, (depth_m / self.total_depth_m) * 100.0))

        quality = "good"
        # Hard plausibility limits: JSN-SR04T spec is 0.25 - 4.5 m
        if distance_m < 0.20:
            quality = "uncertain"  # likely sensor too close to water
        elif distance_m > 4.6:
            quality = "uncertain"  # likely above max range; depth reading clamped

        reading = UltrasonicReading(
            timestamp=time.time(),
            distance_m=distance_m,
            depth_m=depth_m,
            percent_full=percent_full,
            quality=quality,
        )
        self.state.last_reading = reading
        self.state.history.append(reading)
        if len(self.state.history) > 100:
            self.state.history.pop(0)
        return reading

    # ---- Telemetry shape ------------------------------------------------

    def to_telemetry(self, reading: UltrasonicReading) -> list[dict]:
        """Emit two telemetry rows per reading — both depth and percent — so cloud
        consumers can pick either. They share a timestamp."""
        if reading.depth_m != reading.depth_m:  # NaN
            return []
        return [
            {
                "sourceKey": self.source_key + ".depth",
                "kind": "water_level_depth",
                "value": reading.depth_m,
                "unit": "m",
                "quality": reading.quality,
            },
            {
                "sourceKey": self.source_key + ".percent",
                "kind": "water_level_percent",
                "value": reading.percent_full,
                "unit": "%",
                "quality": reading.quality,
            },
        ]

    # ---- Low-water cooldown gate ---------------------------------------

    def should_emit_low_water_event(self, reading: UltrasonicReading) -> bool:
        """Returns True iff this reading is below threshold AND the cooldown has
        elapsed since the last emitted low-water event. Updates state."""
        if reading.percent_full != reading.percent_full:
            return False
        if reading.percent_full >= self.low_level_threshold_pct:
            return False
        last_at = self.state.last_low_water_event_at
        if last_at is not None and time.monotonic() - last_at < self.low_level_cooldown_s:
            return False
        self.state.last_low_water_event_at = time.monotonic()
        return True

    def close(self) -> None:
        if _GPIO_AVAILABLE:
            try:
                GPIO.cleanup([self.trig_gpio, self.echo_gpio])
            except Exception:  # noqa: BLE001
                pass
