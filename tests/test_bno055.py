"""BNO055 9-DoF IMU driver tests — mock mode (no hardware required, same
approach as test_protection_no_hw.py / test_pwm.py).

Covers:
- Simulated read() on a dev box (adafruit libs absent) is deterministic and
  marked quality="uncertain".
- to_telemetry() wire shape: 5 rows (heading/roll/pitch/calibration/temperature)
  with the canonical sourceKey/kind/value/unit/quality keys.
- NaN rows are skipped; the calibration row survives a failed read.
- I2C address resolution: sensor_params override > pin.i2c_address > 0x28.
- Read retry wrapper: 2 retries on OSError, then the error propagates as a
  quality="bad" reading.
"""

from __future__ import annotations

import math

from agal_one_agent.config import PinConfig
from agal_one_agent.sensors.bno055 import Bno055Reading, ImuSensorBno055


def _imu_pin(**overrides) -> PinConfig:
    defaults = dict(
        physical_pin=3,
        protocol="i2c_sda",
        label="imu_main",
        sensor_type="bno055_9dof",
    )
    defaults.update(overrides)
    return PinConfig(**defaults)


# ----- simulated read (dev box: adafruit-blinka not installed) ---------------


def test_simulated_read_is_deterministic_and_uncertain():
    sensor = ImuSensorBno055(_imu_pin())
    assert sensor._dev is None  # guarded import fell back to mock mode

    r1 = sensor.read()
    r2 = sensor.read()

    assert (r1.heading_deg, r1.roll_deg, r1.pitch_deg) == (90.0, 0.0, 0.0)
    assert (r1.calib_sys, r1.calib_gyro, r1.calib_accel, r1.calib_mag) == (3, 3, 3, 3)
    assert r1.temperature_c == 25.0
    assert r1.quality == "uncertain"
    # Deterministic across reads (timestamps aside)
    assert (r2.heading_deg, r2.roll_deg, r2.pitch_deg) == (90.0, 0.0, 0.0)
    assert sensor.state.last_reading is r2
    assert len(sensor.state.history) == 2


# ----- telemetry wire shape ---------------------------------------------------


def test_to_telemetry_shape_and_source_keys():
    sensor = ImuSensorBno055(_imu_pin())
    rows = sensor.to_telemetry(sensor.read())

    assert [r["sourceKey"] for r in rows] == [
        "imu_main.orientation.heading",
        "imu_main.orientation.roll",
        "imu_main.orientation.pitch",
        "imu_main.calibration",
        "imu_main.temperature",
    ]
    # Canonical wire shape on every row
    for row in rows:
        for key in ("sourceKey", "kind", "value", "unit", "quality"):
            assert key in row, f"missing {key} in {row}"

    by_key = {r["sourceKey"]: r for r in rows}
    assert by_key["imu_main.orientation.heading"]["kind"] == "orientation_heading"
    assert by_key["imu_main.orientation.heading"]["unit"] == "deg"
    assert by_key["imu_main.orientation.heading"]["value"] == 90.0
    assert by_key["imu_main.orientation.roll"]["kind"] == "orientation_roll"
    assert by_key["imu_main.orientation.pitch"]["kind"] == "orientation_pitch"

    calib = by_key["imu_main.calibration"]
    assert calib["kind"] == "calibration_status"
    assert calib["unit"] == "level"
    assert calib["value"] == 3.0  # min of (3, 3, 3, 3)
    assert (calib["sys"], calib["gyro"], calib["accel"], calib["mag"]) == (3, 3, 3, 3)

    temp = by_key["imu_main.temperature"]
    assert temp["kind"] == "temperature"
    assert temp["unit"] == "degC"
    assert temp["value"] == 25.0


def test_to_telemetry_skips_nan_rows_but_keeps_calibration():
    sensor = ImuSensorBno055(_imu_pin())
    bad = Bno055Reading(
        timestamp=0.0,
        heading_deg=float("nan"),
        roll_deg=float("nan"),
        pitch_deg=float("nan"),
        calib_sys=0, calib_gyro=1, calib_accel=2, calib_mag=0,
        temperature_c=float("nan"),
        quality="bad",
    )
    rows = sensor.to_telemetry(bad)
    assert [r["sourceKey"] for r in rows] == ["imu_main.calibration"]
    assert rows[0]["value"] == 0.0  # min of (0, 1, 2, 0)
    assert rows[0]["quality"] == "bad"


def test_source_key_falls_back_to_pin_number():
    sensor = ImuSensorBno055(_imu_pin(label=""))
    assert sensor.source_key == "bno055_pin_3"


# ----- I2C address resolution -------------------------------------------------


def test_default_address_is_0x28():
    assert ImuSensorBno055(_imu_pin()).i2c_address == 0x28


def test_pin_i2c_address_is_honoured():
    assert ImuSensorBno055(_imu_pin(i2c_address=0x29)).i2c_address == 0x29


def test_sensor_params_address_overrides_pin_field():
    pin = _imu_pin(i2c_address=0x28, sensor_params={"i2c_address": 0x29})
    assert ImuSensorBno055(pin).i2c_address == 0x29


# ----- clock-stretch retry wrapper ---------------------------------------------


def test_retry_wrapper_retries_twice_then_succeeds():
    sensor = ImuSensorBno055(_imu_pin(sensor_params={"retry_delay_s": 0.0}))
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError(121, "Remote I/O error")  # clock-stretch symptom
        return (90.0, 0.0, 0.0)

    assert sensor._with_retries(flaky) == (90.0, 0.0, 0.0)
    assert calls["n"] == 3  # 1 attempt + 2 retries


def test_hw_read_failure_yields_bad_quality_reading():
    sensor = ImuSensorBno055(_imu_pin(sensor_params={"retry_delay_s": 0.0}))

    class AlwaysFailingDev:
        @property
        def euler(self):
            raise OSError(121, "Remote I/O error")

        @property
        def calibration_status(self):
            raise OSError(121, "Remote I/O error")

        @property
        def temperature(self):
            raise OSError(121, "Remote I/O error")

    sensor._dev = AlwaysFailingDev()  # simulate hardware present but flaky
    reading = sensor.read()
    assert reading.quality == "bad"
    assert math.isnan(reading.heading_deg)
    # Telemetry still emits the calibration row so the cloud sees the sensor
    rows = sensor.to_telemetry(reading)
    assert [r["sourceKey"] for r in rows] == ["imu_main.calibration"]
