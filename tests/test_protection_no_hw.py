"""End-to-end test of the edge protection logic without any hardware.

Run with:
    cd Agal/daemon/agal-one-agent
    python -m pytest tests/test_protection_no_hw.py -v

Validates:
  1. Baseline learning: protection captures the average current over the
     baseline window after inrush ends.
  2. Dry-run trigger: when current drops below threshold for confirm_window_s,
     the relay writer is called and a dry_run_protection_triggered event fires.
  3. Inrush suppression: during inrush_ignore_s after pump start, no readings
     count toward baseline or dry-run.
  4. Recovery: if current recovers before confirm window expires, no cutoff.
  5. Low-water event: ultrasonic sensor below threshold emits a
     low_water_level_warning event respecting cooldown.

Strategy: monkey-patch the sensor's read methods to return scripted values, then
drive the protection monitor's _step_* methods directly (no threads, no time
sleeps) so the test is deterministic and fast.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from agal_one_agent.sensors.current_sensor import (
    CurrentSensorACS758,
    CurrentSensorReading,
)
from agal_one_agent.sensors.ultrasonic_sensor import (
    UltrasonicSensorJsnSr04t,
    UltrasonicReading,
)
from agal_one_agent.sensors.protection import (
    ProtectionConfig,
    ProtectionMonitor,
    _SensorBundle,
)


# ----- Fixture pin (mimics agal_one_agent.config.PinConfig) ----------------

@dataclass
class FakePin:
    physical_pin: int = 12
    gpio_number: Optional[int] = None
    protocol: str = "analog_input"
    label: str = "pump1.current"
    sensor_type: str = "current_acs758"
    sensor_params: dict = field(default_factory=dict)
    protection: Optional[dict] = None
    # Bus fields (unused here)
    bus_id: Optional[str] = None
    i2c_address: Optional[int] = None
    i2c_register: Optional[int] = None
    spi_cs_pin: Optional[int] = None
    uart_baud_rate: Optional[int] = None
    one_wire_device_id: Optional[str] = None
    assigned_to: Optional[str] = None


def make_current_sensor(baseline_dir=None) -> CurrentSensorACS758:
    """Construct a CurrentSensorACS758 for the protection tests.

    Defaults to ``baseline_dir=None`` so the protection-loop tests don't
    touch the filesystem (``/var/lib/agal-one-agent`` isn't writable on macOS
    CI without root, and the warning log would clutter output). The
    dedicated persistence tests below pass a real ``tmp_path``.
    """
    pin = FakePin(
        physical_pin=12,
        label="pump1.current",
        sensor_params={"ads1115_address": 0x48, "window_ms": 100},
    )
    return CurrentSensorACS758(pin, baseline_dir=baseline_dir)


def make_protection_config(**overrides) -> ProtectionConfig:
    defaults = dict(
        cut_pin_label="pump1.relay",
        threshold_pct=20.0,
        confirm_window_s=5.0,
        inrush_ignore_s=2.0,
        baseline_learn_after_s=30.0,
        baseline_window_s=10.0,
    )
    defaults.update(overrides)
    return ProtectionConfig(**defaults)


def make_monitor(
    sensor: CurrentSensorACS758,
    protection_config: ProtectionConfig,
    ultrasonic_sensors: Optional[list] = None,
) -> tuple[ProtectionMonitor, MagicMock, MagicMock]:
    relay_writer = MagicMock()
    event_publisher = MagicMock()
    monitor = ProtectionMonitor(
        current_sensors=[sensor],
        protection_configs={sensor.source_key: protection_config},
        ultrasonic_sensors=ultrasonic_sensors or [],
        relay_writer=relay_writer,
        event_publisher=event_publisher,
        poll_interval_s=0.01,
    )
    return monitor, relay_writer, event_publisher


def make_reading(rms_a: float, quality: str = "good") -> CurrentSensorReading:
    return CurrentSensorReading(
        timestamp=time.time(),
        rms_a=rms_a,
        samples=30,
        window_s=0.1,
        quality=quality,
    )


# ----- Tests --------------------------------------------------------------


def test_baseline_learned_after_inrush(monkeypatch):
    sensor = make_current_sensor()
    pconfig = make_protection_config(
        inrush_ignore_s=0.0,             # skip inrush so test stays fast
        baseline_window_s=0.5,           # 0.5s baseline window
    )
    monitor, _relay_writer, event_publisher = make_monitor(sensor, pconfig)

    # Simulate pump start — sets the baseline-learning window starting now
    monitor.notify_pump_started("pump1.relay")
    bundle = monitor._current_bundles[0]
    # Force the learning window to start immediately (notify_pump_started adds 0.5s)
    bundle._learning_started_at = time.monotonic()

    # Feed a stream of stable readings around 5.0 A
    monkeypatch.setattr(sensor, "read_rms", lambda: make_reading(5.0))

    # Drive 5 steps spaced 0.15s apart — covers >0.5s window
    for _ in range(5):
        monitor._step_current(bundle)
        time.sleep(0.15)

    assert sensor.has_baseline(), "baseline should have been learned"
    assert sensor.state.baseline_a == pytest.approx(5.0, abs=0.01)
    # An event should have been emitted
    event_types = [call.args[0]["type"] for call in event_publisher.call_args_list]
    assert "current_baseline_learned" in event_types


def test_dry_run_cutoff_fires_after_confirm_window(monkeypatch):
    sensor = make_current_sensor()
    pconfig = make_protection_config(
        inrush_ignore_s=0.0,
        confirm_window_s=0.3,            # short window for test speed
    )
    monitor, relay_writer, event_publisher = make_monitor(sensor, pconfig)

    # Pre-learn baseline
    sensor.learn_baseline(5.0)
    monitor.notify_pump_started("pump1.relay")
    bundle = monitor._current_bundles[0]

    # Reading at 3.0 A is 40% below 5.0 A baseline → below 20% threshold
    monkeypatch.setattr(sensor, "read_rms", lambda: make_reading(3.0))

    # First step starts the below-threshold window
    monitor._step_current(bundle)
    assert relay_writer.call_count == 0
    assert bundle.below_threshold_since is not None

    # Wait less than confirm window — no cutoff yet
    time.sleep(0.15)
    monitor._step_current(bundle)
    assert relay_writer.call_count == 0

    # Wait long enough to cross the window — cutoff fires
    time.sleep(0.20)
    monitor._step_current(bundle)
    assert relay_writer.call_count == 1
    assert relay_writer.call_args == (("pump1.relay", 0),)

    # Event was published
    event_types = [call.args[0]["type"] for call in event_publisher.call_args_list]
    assert "dry_run_protection_triggered" in event_types

    # Cutoff event payload includes diagnostics
    cutoff_event = [c.args[0] for c in event_publisher.call_args_list
                    if c.args[0]["type"] == "dry_run_protection_triggered"][0]
    assert cutoff_event["payload"]["baselineCurrentA"] == 5.0
    assert cutoff_event["payload"]["observedCurrentA"] == 3.0
    assert cutoff_event["payload"]["cutPinLabel"] == "pump1.relay"


def test_inrush_window_suppresses_dry_run_check(monkeypatch):
    sensor = make_current_sensor()
    sensor.learn_baseline(5.0)
    pconfig = make_protection_config(inrush_ignore_s=10.0)  # long inrush
    monitor, relay_writer, _events = make_monitor(sensor, pconfig)

    monitor.notify_pump_started("pump1.relay")
    bundle = monitor._current_bundles[0]

    # Even a clearly-below-threshold reading should be ignored during inrush
    monkeypatch.setattr(sensor, "read_rms", lambda: make_reading(0.5))
    for _ in range(5):
        monitor._step_current(bundle)

    assert relay_writer.call_count == 0
    assert bundle.below_threshold_since is None


def test_recovery_before_confirm_window_does_not_cut(monkeypatch):
    sensor = make_current_sensor()
    sensor.learn_baseline(5.0)
    pconfig = make_protection_config(inrush_ignore_s=0.0, confirm_window_s=0.3)
    monitor, relay_writer, _events = make_monitor(sensor, pconfig)

    monitor.notify_pump_started("pump1.relay")
    bundle = monitor._current_bundles[0]

    # Dip
    monkeypatch.setattr(sensor, "read_rms", lambda: make_reading(2.0))
    monitor._step_current(bundle)
    assert bundle.below_threshold_since is not None

    # Recover before window — should clear the below-threshold marker
    monkeypatch.setattr(sensor, "read_rms", lambda: make_reading(5.0))
    monitor._step_current(bundle)
    assert bundle.below_threshold_since is None
    assert relay_writer.call_count == 0


def test_relay_writer_failure_still_emits_event(monkeypatch):
    """If the GPIO write fails for any reason, we still emit the event so the cloud knows."""
    sensor = make_current_sensor()
    sensor.learn_baseline(5.0)
    pconfig = make_protection_config(inrush_ignore_s=0.0, confirm_window_s=0.1)

    relay_writer = MagicMock(side_effect=RuntimeError("GPIO unplugged"))
    event_publisher = MagicMock()
    monitor = ProtectionMonitor(
        current_sensors=[sensor],
        protection_configs={sensor.source_key: pconfig},
        ultrasonic_sensors=[],
        relay_writer=relay_writer,
        event_publisher=event_publisher,
    )

    monitor.notify_pump_started("pump1.relay")
    bundle = monitor._current_bundles[0]

    monkeypatch.setattr(sensor, "read_rms", lambda: make_reading(2.0))
    monitor._step_current(bundle)
    time.sleep(0.15)
    monitor._step_current(bundle)

    assert relay_writer.call_count == 1
    event_types = [c.args[0]["type"] for c in event_publisher.call_args_list]
    assert "dry_run_protection_triggered" in event_types


# ----- Ultrasonic / low-water tests --------------------------------------


def make_ultrasonic_sensor(total_depth_m: float = 4.0) -> UltrasonicSensorJsnSr04t:
    pin = FakePin(
        physical_pin=16,
        protocol="ultrasonic",
        label="well1.level",
        sensor_type="ultrasonic_jsn_sr04t",
        sensor_params={
            "trig_gpio": 23,
            "echo_gpio": 24,
            "total_depth_m": total_depth_m,
            "low_level_threshold_pct": 15,
            "low_level_cooldown_s": 60,
        },
    )
    return UltrasonicSensorJsnSr04t(pin)


def test_low_water_event_fires_below_threshold(monkeypatch):
    sensor = make_ultrasonic_sensor(total_depth_m=4.0)
    relay_writer = MagicMock()
    event_publisher = MagicMock()
    monitor = ProtectionMonitor(
        current_sensors=[],
        protection_configs={},
        ultrasonic_sensors=[sensor],
        relay_writer=relay_writer,
        event_publisher=event_publisher,
    )

    # 10% full = 0.4 m depth, distance = 3.6 m
    low_reading = UltrasonicReading(
        timestamp=time.time(),
        distance_m=3.6,
        depth_m=0.4,
        percent_full=10.0,
        quality="good",
    )
    monkeypatch.setattr(sensor, "read", lambda: low_reading)

    monitor._step_ultrasonic(sensor)
    event_types = [c.args[0]["type"] for c in event_publisher.call_args_list]
    assert "low_water_level_warning" in event_types
    event_publisher.assert_called_once()


def test_low_water_event_respects_cooldown(monkeypatch):
    sensor = make_ultrasonic_sensor(total_depth_m=4.0)
    relay_writer = MagicMock()
    event_publisher = MagicMock()
    monitor = ProtectionMonitor(
        current_sensors=[],
        protection_configs={},
        ultrasonic_sensors=[sensor],
        relay_writer=relay_writer,
        event_publisher=event_publisher,
    )

    low_reading = UltrasonicReading(
        timestamp=time.time(),
        distance_m=3.6,
        depth_m=0.4,
        percent_full=10.0,
        quality="good",
    )
    monkeypatch.setattr(sensor, "read", lambda: low_reading)

    monitor._step_ultrasonic(sensor)
    monitor._step_ultrasonic(sensor)  # second step should NOT fire (cooldown)
    monitor._step_ultrasonic(sensor)
    assert event_publisher.call_count == 1


def test_above_threshold_does_not_fire(monkeypatch):
    sensor = make_ultrasonic_sensor(total_depth_m=4.0)
    relay_writer = MagicMock()
    event_publisher = MagicMock()
    monitor = ProtectionMonitor(
        current_sensors=[],
        protection_configs={},
        ultrasonic_sensors=[sensor],
        relay_writer=relay_writer,
        event_publisher=event_publisher,
    )

    full_reading = UltrasonicReading(
        timestamp=time.time(),
        distance_m=1.0,
        depth_m=3.0,
        percent_full=75.0,
        quality="good",
    )
    monkeypatch.setattr(sensor, "read", lambda: full_reading)

    monitor._step_ultrasonic(sensor)
    event_publisher.assert_not_called()


# ----- Baseline persistence tests (closes BENCH_TEST_REPORT §3.1) --------
#
# These tests cover the fix for "every Pi reboot loses the baseline and
# triggers another full 30s+ learning window with no dry-run protection in
# between." See agal_one_agent/sensors/current_sensor.py:_load_baseline_if_present
# and ::_save_baseline.


import json as _json  # noqa: E402  (kept local to the persistence block)


def _make_persistent_sensor(baseline_dir) -> CurrentSensorACS758:
    pin = FakePin(
        physical_pin=12,
        label="pump1.current",
        sensor_params={"ads1115_address": 0x48, "window_ms": 100},
    )
    return CurrentSensorACS758(pin, baseline_dir=baseline_dir)


def test_baseline_persists_across_sensor_instances(tmp_path):
    """learn_baseline() on instance #1 writes to disk; instance #2 loads it on init."""
    s1 = _make_persistent_sensor(tmp_path)
    s1.learn_baseline(4.82)

    baseline_file = tmp_path / "baseline.pump1.current.json"
    assert baseline_file.exists(), "learn_baseline should have written the file"
    saved = _json.loads(baseline_file.read_text())
    assert saved["baseline_a"] == pytest.approx(4.82)
    assert saved["source_key"] == "pump1.current"
    assert saved["schema_version"] == 1
    assert isinstance(saved["baseline_learned_at"], (int, float))

    # Fresh sensor instance — should pick up the persisted baseline at init time,
    # without going through another inrush + learning window.
    s2 = _make_persistent_sensor(tmp_path)
    assert s2.has_baseline()
    assert s2.state.baseline_a == pytest.approx(4.82)
    assert s2.state.baseline_learned_at is not None


def test_baseline_dir_none_disables_persistence(tmp_path):
    """baseline_dir=None means no read, no write — used in tests + opt-out config."""
    s = _make_persistent_sensor(None)
    s.learn_baseline(4.82)
    # In-memory state still works
    assert s.has_baseline()
    # ...but no file lands anywhere we could check; the contract is "no I/O".
    # We assert by reading: a fresh None-dir sensor should NOT pick anything up.
    s2 = _make_persistent_sensor(None)
    assert not s2.has_baseline()


def test_corrupt_baseline_file_does_not_crash(tmp_path):
    """A malformed baseline.json should be logged and ignored, not crash init."""
    (tmp_path / "baseline.pump1.current.json").write_text("not json at all {{")
    s = _make_persistent_sensor(tmp_path)
    assert not s.has_baseline()  # corrupt file → no baseline loaded


def test_invalid_baseline_value_rejected(tmp_path):
    """A baseline_a of 0 or negative or missing must be rejected — those would
    mis-trigger dry-run protection on the first reading."""
    (tmp_path / "baseline.pump1.current.json").write_text(
        _json.dumps({"baseline_a": 0.0, "baseline_learned_at": 1700000000.0})
    )
    s = _make_persistent_sensor(tmp_path)
    assert not s.has_baseline()

    (tmp_path / "baseline.pump1.current.json").write_text(
        _json.dumps({"baseline_a": -1.5})
    )
    s = _make_persistent_sensor(tmp_path)
    assert not s.has_baseline()

    (tmp_path / "baseline.pump1.current.json").write_text(_json.dumps({}))
    s = _make_persistent_sensor(tmp_path)
    assert not s.has_baseline()


def test_missing_baseline_dir_does_not_crash():
    """Unwritable / non-existent default dir (the macOS case for /var/lib/agal-one-agent)
    must not crash the daemon — learn_baseline still works in-memory; persistence
    is best-effort. This is the codepath the existing protection tests already
    exercise with baseline_dir=None; this test asserts the contract explicitly."""
    bad_dir = "/var/lib/agal-one-agent-nonexistent-xyz-test"
    s = _make_persistent_sensor(bad_dir)
    s.learn_baseline(4.82)  # must not raise even though we can't mkdir there
    assert s.has_baseline()
    assert s.state.baseline_a == pytest.approx(4.82)


def test_source_key_is_sanitized_for_filename(tmp_path):
    """source_key with awkward characters (slashes, spaces) must yield a safe
    filename — defends against config typos creating directory-traversal-ish
    paths or unwritable filenames."""
    pin = FakePin(
        physical_pin=12,
        label="pump/1 weird:key",
        sensor_params={"ads1115_address": 0x48, "window_ms": 100},
    )
    s = CurrentSensorACS758(pin, baseline_dir=tmp_path)
    s.learn_baseline(3.14)
    # All non-[A-Za-z0-9._-] runs replaced with _
    expected = tmp_path / "baseline.pump_1_weird_key.json"
    assert expected.exists()


def test_clear_baseline_deletes_persisted_file(tmp_path):
    s = _make_persistent_sensor(tmp_path)
    s.learn_baseline(4.82)
    baseline_file = tmp_path / "baseline.pump1.current.json"
    assert baseline_file.exists()

    s.clear_baseline()
    assert not s.has_baseline()
    assert not baseline_file.exists()

    # Idempotent — calling again on already-cleared sensor does not raise.
    s.clear_baseline()
