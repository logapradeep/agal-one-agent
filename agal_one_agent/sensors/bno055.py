"""Adafruit BNO055 9-DoF absolute-orientation IMU (Bosch Sensortec), via I2C.

Wiring (Adafruit breakout, 3.3 V logic — safe to connect directly):
    BNO055 VIN → RPi 3V3
    BNO055 GND → RPi GND
    BNO055 SDA → RPi SDA (GPIO 2 on the default hardware bus)
    BNO055 SCL → RPi SCL (GPIO 3 on the default hardware bus)
    BNO055 ADR → float/GND for address 0x28 (default); tie to 3V3 for 0x29.

The sensor runs in NDOF fusion mode (gyro + accel + mag fused on-chip) and
reports absolute orientation as Euler angles — heading/roll/pitch in degrees —
plus a per-subsystem calibration status (sys/gyro/accel/mag, each 0–3 where 3
is fully calibrated) and the die temperature in °C.

IMPORTANT — I2C clock stretching on Raspberry Pi:
    The BNO055 stretches the I2C clock, and the BCM283x hardware I2C controller
    has a well-known clock-stretching bug that corrupts reads intermittently
    (shows up as OSError / Remote I/O errors). Nodes carrying a BNO055 should
    prefer ONE of these boot-config (`/boot/firmware/config.txt`) mitigations:

      1. (preferred) Move the sensor to a software I2C bus:
             dtoverlay=i2c-gpio,bus=3,i2c_gpio_sda=23,i2c_gpio_scl=24
         Bit-banged I2C honours clock stretching correctly.
      2. Slow the hardware bus to 10 kHz so stretching windows are tolerated:
             dtparam=i2c_arm_baudrate=10000

    This is provisioning/boot-config documentation, not something this driver
    enforces. As defence-in-depth the driver additionally wraps every bus read
    with a small retry (2 retries on OSError/timeout) so a single stretched
    transaction does not poison a telemetry cycle.

Hardware imports (adafruit-blinka + adafruit-circuitpython-bno055) are guarded:
on a non-Pi dev machine the driver falls back to a deterministic simulated
reading — same approach as the ACS758/JSN-SR04T drivers — so unit tests and
the bench harness run unmodified.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

try:  # pragma: no cover — exercised only on a real Pi with the libs installed
    import adafruit_bno055  # type: ignore
    import board  # type: ignore
    import busio  # type: ignore

    _BNO055_AVAILABLE = True
except (ImportError, NotImplementedError, RuntimeError):
    # ImportError: libs not installed (dev box). NotImplementedError/RuntimeError:
    # adafruit-blinka's `board` raises these on unsupported platforms.
    _BNO055_AVAILABLE = False
    logger.warning(
        "adafruit-blinka / adafruit-circuitpython-bno055 not available — "
        "BNO055 reads will be simulated"
    )

_T = TypeVar("_T")

#: Deterministic values returned in simulation mode (non-Pi dev boxes).
_SIM_HEADING_DEG = 90.0
_SIM_ROLL_DEG = 0.0
_SIM_PITCH_DEG = 0.0
_SIM_CALIBRATION = (3, 3, 3, 3)  # sys, gyro, accel, mag
_SIM_TEMPERATURE_C = 25.0


@dataclass
class Bno055Reading:
    """One fused orientation sample. Angle fields are NaN when unavailable."""

    timestamp: float          # epoch seconds
    heading_deg: float        # 0–360, magnetic heading (NDOF fusion)
    roll_deg: float           # -90–+90
    pitch_deg: float          # -180–+180
    calib_sys: int            # 0–3 (3 = fully calibrated)
    calib_gyro: int           # 0–3
    calib_accel: int          # 0–3
    calib_mag: int            # 0–3
    temperature_c: float      # die temperature, °C (NaN when unavailable)
    quality: str = "good"     # "good" | "uncertain" | "bad"


@dataclass
class Bno055State:
    """In-memory state for one BNO055 instance."""

    last_reading: Optional[Bno055Reading] = None
    history: list[Bno055Reading] = field(default_factory=list)


class ImuSensorBno055:
    """BNO055 9-DoF IMU driver (sensor_type ``bno055_9dof``).

    I2C address resolution order:
      1. ``pin.sensor_params["i2c_address"]`` (e.g. ``0x29`` with ADR tied high)
      2. ``pin.i2c_address`` (the generic PinConfig bus field)
      3. ``0x28`` (breakout default)

    `pin.sensor_params` accepts:
      i2c_address:    int    (default 0x28; 0x29 with ADR pin high)
      retry_delay_s:  float  (default 0.05 — pause between clock-stretch retries)
      sensor_label:   str    (defaults to pin.label)
    """

    #: 1 initial attempt + 2 retries — BNO055 clock-stretching flakiness on the
    #: Pi hardware I2C bus surfaces as sporadic OSError; see module docstring.
    READ_ATTEMPTS = 3

    def __init__(self, pin, state: Optional[Bno055State] = None) -> None:
        self.pin = pin
        params = pin.sensor_params or {}

        param_address = params.get("i2c_address")
        if param_address is not None:
            self.i2c_address = int(param_address)
        elif getattr(pin, "i2c_address", None) is not None:
            self.i2c_address = int(pin.i2c_address)
        else:
            self.i2c_address = 0x28

        self.retry_delay_s = float(params.get("retry_delay_s", 0.05))
        self.source_key = pin.label or f"bno055_pin_{pin.physical_pin}"
        self.state = state or Bno055State()

        self._dev = None
        if _BNO055_AVAILABLE:
            i2c = busio.I2C(board.SCL, board.SDA)
            self._dev = adafruit_bno055.BNO055_I2C(i2c, address=self.i2c_address)
            # NDOF: full 9-DoF fusion (accel + gyro + mag) with absolute
            # (magnetic-north-referenced) heading. The Adafruit driver defaults
            # to NDOF too — set explicitly so a prior soft-reset state or a
            # future library default change cannot leave us in CONFIG mode.
            self._dev.mode = adafruit_bno055.NDOF_MODE

    # ---- Reading -------------------------------------------------------

    def _with_retries(self, fn: Callable[[], _T]) -> _T:
        """Run a bus read, retrying on OSError/TimeoutError (clock stretching)."""
        last_exc: Exception | None = None
        for attempt in range(self.READ_ATTEMPTS):
            try:
                return fn()
            except (OSError, TimeoutError) as e:
                last_exc = e
                logger.debug(
                    "[%s] BNO055 read attempt %d/%d failed: %s",
                    self.source_key, attempt + 1, self.READ_ATTEMPTS, e,
                )
                time.sleep(self.retry_delay_s)
        assert last_exc is not None
        raise last_exc

    def read(self) -> Bno055Reading:
        """Read fused euler angles + calibration status + temperature.

        On non-Pi dev boxes (adafruit libs unavailable) returns a deterministic
        simulated reading marked ``quality="uncertain"``, matching how the
        ACS758 driver handles missing hardware.
        """
        if self._dev is None:
            reading = Bno055Reading(
                timestamp=time.time(),
                heading_deg=_SIM_HEADING_DEG,
                roll_deg=_SIM_ROLL_DEG,
                pitch_deg=_SIM_PITCH_DEG,
                calib_sys=_SIM_CALIBRATION[0],
                calib_gyro=_SIM_CALIBRATION[1],
                calib_accel=_SIM_CALIBRATION[2],
                calib_mag=_SIM_CALIBRATION[3],
                temperature_c=_SIM_TEMPERATURE_C,
                quality="uncertain",  # simulated readings are marked uncertain
            )
            self._record(reading)
            return reading

        try:
            euler = self._with_retries(lambda: self._dev.euler)
            calib = self._with_retries(lambda: self._dev.calibration_status)
            temperature = self._with_retries(lambda: self._dev.temperature)
        except (OSError, TimeoutError) as e:
            logger.warning("[%s] BNO055 read failed after %d attempts: %s",
                           self.source_key, self.READ_ATTEMPTS, e)
            reading = Bno055Reading(
                timestamp=time.time(),
                heading_deg=float("nan"),
                roll_deg=float("nan"),
                pitch_deg=float("nan"),
                calib_sys=0, calib_gyro=0, calib_accel=0, calib_mag=0,
                temperature_c=float("nan"),
                quality="bad",
            )
            self._record(reading)
            return reading

        # The Adafruit driver returns None components while the chip is still
        # settling / not yet calibrated — map those to NaN and mark uncertain.
        heading, roll, pitch = (euler if euler is not None else (None, None, None))
        calib_sys, calib_gyro, calib_accel, calib_mag = (
            calib if calib is not None else (0, 0, 0, 0)
        )

        quality = "good"
        if heading is None or roll is None or pitch is None:
            quality = "uncertain"
        elif calib_sys == 0:
            # Fusion output is unreliable until the on-chip algorithm reports
            # at least minimal system calibration.
            quality = "uncertain"

        reading = Bno055Reading(
            timestamp=time.time(),
            heading_deg=float(heading) if heading is not None else float("nan"),
            roll_deg=float(roll) if roll is not None else float("nan"),
            pitch_deg=float(pitch) if pitch is not None else float("nan"),
            calib_sys=int(calib_sys),
            calib_gyro=int(calib_gyro),
            calib_accel=int(calib_accel),
            calib_mag=int(calib_mag),
            temperature_c=float(temperature) if temperature is not None else float("nan"),
            quality=quality,
        )
        self._record(reading)
        return reading

    def _record(self, reading: Bno055Reading) -> None:
        self.state.last_reading = reading
        self.state.history.append(reading)
        if len(self.state.history) > 60:
            self.state.history.pop(0)

    # ---- Telemetry shape ------------------------------------------------

    def to_telemetry(self, reading: Bno055Reading) -> list[dict]:
        """Convert a reading to canonical telemetry rows (same wire shape as the
        other sensor drivers: sourceKey/kind/value/unit/quality). NaN angle or
        temperature rows are skipped; the calibration row is always emitted so
        the cloud can surface "needs calibration" even while angles are NaN."""
        rows: list[dict] = []

        angles = (
            (".orientation.heading", "orientation_heading", reading.heading_deg),
            (".orientation.roll", "orientation_roll", reading.roll_deg),
            (".orientation.pitch", "orientation_pitch", reading.pitch_deg),
        )
        for suffix, kind, value in angles:
            if value != value:  # NaN check
                continue
            rows.append({
                "sourceKey": self.source_key + suffix,
                "kind": kind,
                "value": value,
                "unit": "deg",
                "quality": reading.quality,
            })

        # Overall calibration = the weakest subsystem (all four must reach 3
        # for NDOF output to be fully trustworthy). Per-subsystem detail rides
        # along as extra keys, like `samples`/`windowMs` on the current sensor.
        rows.append({
            "sourceKey": self.source_key + ".calibration",
            "kind": "calibration_status",
            "value": float(min(reading.calib_sys, reading.calib_gyro,
                               reading.calib_accel, reading.calib_mag)),
            "unit": "level",
            "quality": reading.quality,
            "sys": reading.calib_sys,
            "gyro": reading.calib_gyro,
            "accel": reading.calib_accel,
            "mag": reading.calib_mag,
        })

        if reading.temperature_c == reading.temperature_c:  # not NaN
            rows.append({
                "sourceKey": self.source_key + ".temperature",
                "kind": "temperature",
                "value": reading.temperature_c,
                "unit": "degC",
                "quality": reading.quality,
            })

        return rows

    def close(self) -> None:
        # The Adafruit driver holds no exclusive resources beyond the shared
        # I2C bus object; nothing to release explicitly.
        self._dev = None
