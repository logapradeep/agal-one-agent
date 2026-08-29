"""Edge protection logic: dry-run cutoff and low-water-level warnings.

Runs in its own background thread. Polls each configured current sensor at a
short cadence (default 1 s), compares against the learned baseline, and cuts
the linked relay (and publishes an event) when current drops by more than the
configured threshold for the configured confirm window.

The protection thread is INTENTIONALLY decoupled from the telemetry thread —
telemetry can lag, drop, or rate-limit, but protection must run reliably even
when the network is down. All protection decisions are made locally; nothing
goes through the cloud round-trip.

Wire-up assumes a PinConfig that links a current-sensing pin to a relay pin:

    pins:
      - physical_pin: 12        # current sensor
        protocol: analog_input
        label: pump1.current
        sensor_type: current_acs758
        sensor_params:
          ads1115_address: 0x48
        protection:
          cut_pin_label: pump1.relay
          threshold_pct: 20
          confirm_window_s: 5
          inrush_ignore_s: 2
          baseline_learn_after_s: 30
          baseline_window_s: 10
          auto_restart_after_s: 0
      - physical_pin: 16        # relay for the same pump
        protocol: gpio_output
        label: pump1.relay
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .current_sensor import CurrentSensorACS758, CurrentSensorReading
from .ultrasonic_sensor import UltrasonicSensorJsnSr04t, UltrasonicReading

logger = logging.getLogger(__name__)


@dataclass
class ProtectionConfig:
    """Per-current-sensor protection parameters. Sourced from pin.protection."""

    cut_pin_label: str
    threshold_pct: float = 20.0
    confirm_window_s: float = 5.0
    inrush_ignore_s: float = 2.0
    baseline_learn_after_s: float = 30.0
    baseline_window_s: float = 10.0
    auto_restart_after_s: float = 0.0


class _SensorBundle:
    """All runtime state for one protection-enabled current sensor."""

    def __init__(self, sensor: CurrentSensorACS758, config: ProtectionConfig) -> None:
        self.sensor = sensor
        self.config = config
        self.below_threshold_since: Optional[float] = None
        self.cutoff_active: bool = False
        self.cutoff_at: float = 0.0
        # Baseline-learning window: collect samples after inrush, average them.
        self._learning_samples: list[float] = []
        self._learning_started_at: Optional[float] = None


class ProtectionMonitor:
    """Background thread that runs protection on all current + ultrasonic sensors."""

    def __init__(
        self,
        current_sensors: list[CurrentSensorACS758],
        protection_configs: dict[str, ProtectionConfig],  # sensor.source_key → config
        ultrasonic_sensors: list[UltrasonicSensorJsnSr04t],
        relay_writer: Callable[[str, int], None],
        event_publisher: Callable[[dict], None],
        poll_interval_s: float = 1.0,
        ultrasonic_poll_interval_s: float = 300.0,
    ) -> None:
        self._current_bundles = [
            _SensorBundle(s, protection_configs[s.source_key])
            for s in current_sensors
            if s.source_key in protection_configs
        ]
        self._ultrasonic_sensors = ultrasonic_sensors
        self._relay_writer = relay_writer
        self._event_publisher = event_publisher
        self._poll_interval_s = poll_interval_s
        self._ultrasonic_poll_interval_s = ultrasonic_poll_interval_s
        self._running = False
        self._current_thread: Optional[threading.Thread] = None
        self._ultrasonic_thread: Optional[threading.Thread] = None
        self._last_ultrasonic_check = 0.0

    # ---- Lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._current_bundles:
            self._current_thread = threading.Thread(
                target=self._current_loop, name="protection-current", daemon=True
            )
            self._current_thread.start()
            logger.info(
                "Protection monitor: current loop started (%d sensors, %.1fs poll)",
                len(self._current_bundles), self._poll_interval_s,
            )
        if self._ultrasonic_sensors:
            self._ultrasonic_thread = threading.Thread(
                target=self._ultrasonic_loop, name="protection-ultrasonic", daemon=True
            )
            self._ultrasonic_thread.start()
            logger.info(
                "Protection monitor: ultrasonic loop started (%d sensors, %.1fs poll)",
                len(self._ultrasonic_sensors), self._ultrasonic_poll_interval_s,
            )

    def stop(self) -> None:
        self._running = False
        for t in (self._current_thread, self._ultrasonic_thread):
            if t is not None:
                t.join(timeout=2.0)

    # ---- Pump-start hook (called by command executor on relay turn-on) ---

    def notify_pump_started(self, relay_pin_label: str) -> None:
        """Called by the command path when a relay turns ON. Sets the inrush
        window on the linked current sensor so dry-run logic doesn't false-trip
        on starting current."""
        for bundle in self._current_bundles:
            if bundle.config.cut_pin_label == relay_pin_label:
                bundle.sensor.mark_pump_started(bundle.config.inrush_ignore_s)
                bundle.cutoff_active = False
                bundle.below_threshold_since = None
                # Reset baseline learning state too
                bundle._learning_samples.clear()
                bundle._learning_started_at = time.monotonic() + bundle.config.inrush_ignore_s + 0.5
                logger.info(
                    "[protection:%s] pump start linked to %s; inrush=%.1fs, baseline-learn from t+%.1fs",
                    bundle.sensor.source_key, relay_pin_label,
                    bundle.config.inrush_ignore_s, bundle.config.inrush_ignore_s + 0.5,
                )

    def notify_pump_stopped(self, relay_pin_label: str) -> None:
        for bundle in self._current_bundles:
            if bundle.config.cut_pin_label == relay_pin_label:
                bundle.sensor.mark_pump_stopped()
                bundle.below_threshold_since = None
                # Cut-off resolved by operator; reset state for next run
                bundle.cutoff_active = False

    # ---- Loops ----------------------------------------------------------

    def _current_loop(self) -> None:
        while self._running:
            for bundle in self._current_bundles:
                try:
                    self._step_current(bundle)
                except Exception as e:  # noqa: BLE001
                    logger.error("[protection:%s] step error: %s", bundle.sensor.source_key, e)
            time.sleep(self._poll_interval_s)

    def _ultrasonic_loop(self) -> None:
        # Stagger the first poll so it doesn't fight the current loop on first tick
        time.sleep(2.0)
        while self._running:
            for sensor in self._ultrasonic_sensors:
                try:
                    self._step_ultrasonic(sensor)
                except Exception as e:  # noqa: BLE001
                    logger.error("[protection:%s] step error: %s", sensor.source_key, e)
            time.sleep(self._ultrasonic_poll_interval_s)

    # ---- Per-sensor steps ----------------------------------------------

    def _step_current(self, bundle: _SensorBundle) -> None:
        if bundle.sensor.is_in_inrush():
            return  # ignore inrush samples entirely

        reading = bundle.sensor.read_rms()
        if reading.quality == "bad":
            return  # don't act on broken readings

        # Baseline-learning window: collect post-inrush samples for the configured time.
        if (
            not bundle.sensor.has_baseline()
            and bundle._learning_started_at is not None
            and time.monotonic() >= bundle._learning_started_at
        ):
            bundle._learning_samples.append(reading.rms_a)
            if time.monotonic() >= bundle._learning_started_at + bundle.config.baseline_window_s:
                avg = sum(bundle._learning_samples) / len(bundle._learning_samples)
                bundle.sensor.learn_baseline(avg)
                self._event_publisher({
                    "type": "current_baseline_learned",
                    "source": "protection",
                    "sourceKey": bundle.sensor.source_key,
                    "payload": {
                        "baselineCurrentA": avg,
                        "samplesUsed": len(bundle._learning_samples),
                        "windowSec": bundle.config.baseline_window_s,
                    },
                })
                bundle._learning_samples.clear()
                bundle._learning_started_at = None
            return

        if not bundle.sensor.has_baseline():
            return  # nothing to compare against yet

        baseline = bundle.sensor.state.baseline_a or 0.0
        threshold_a = baseline * (1.0 - bundle.config.threshold_pct / 100.0)

        if reading.rms_a < threshold_a:
            now = time.monotonic()
            if bundle.below_threshold_since is None:
                bundle.below_threshold_since = now
                logger.info(
                    "[protection:%s] %.2f A < threshold %.2f A (baseline %.2f A); window-start",
                    bundle.sensor.source_key, reading.rms_a, threshold_a, baseline,
                )
            elif (
                not bundle.cutoff_active
                and (now - bundle.below_threshold_since) >= bundle.config.confirm_window_s
            ):
                self._trigger_cutoff(bundle, reading, baseline, threshold_a)
        else:
            if bundle.below_threshold_since is not None:
                bundle.below_threshold_since = None  # recovered before confirm

    def _step_ultrasonic(self, sensor: UltrasonicSensorJsnSr04t) -> None:
        reading = sensor.read()
        if reading.quality == "bad":
            return
        if sensor.should_emit_low_water_event(reading):
            logger.warning(
                "[protection:%s] low water: %.1f%% (%.2f m / %.2f m)",
                sensor.source_key, reading.percent_full, reading.depth_m, sensor.total_depth_m,
            )
            self._event_publisher({
                "type": "low_water_level_warning",
                "source": "protection",
                "sourceKey": sensor.source_key,
                "payload": {
                    "depthM": reading.depth_m,
                    "percentFull": reading.percent_full,
                    "thresholdPct": sensor.low_level_threshold_pct,
                    "totalDepthM": sensor.total_depth_m,
                },
            })

    # ---- Cutoff action --------------------------------------------------

    def _trigger_cutoff(
        self,
        bundle: _SensorBundle,
        reading: CurrentSensorReading,
        baseline: float,
        threshold_a: float,
    ) -> None:
        logger.error(
            "[protection:%s] DRY-RUN CUTOFF — %.2f A < %.2f A for %.1fs (baseline %.2f A). Cutting %s.",
            bundle.sensor.source_key, reading.rms_a, threshold_a,
            bundle.config.confirm_window_s, baseline, bundle.config.cut_pin_label,
        )
        try:
            self._relay_writer(bundle.config.cut_pin_label, 0)
            bundle.cutoff_active = True
            bundle.cutoff_at = time.monotonic()
        except Exception as e:  # noqa: BLE001
            logger.error("[protection:%s] cutoff relay write failed: %s — emitting event anyway", bundle.sensor.source_key, e)

        self._event_publisher({
            "type": "dry_run_protection_triggered",
            "source": "protection",
            "sourceKey": bundle.sensor.source_key,
            "payload": {
                "baselineCurrentA": baseline,
                "observedCurrentA": reading.rms_a,
                "thresholdPct": bundle.config.threshold_pct,
                "thresholdA": threshold_a,
                "confirmedOverSec": bundle.config.confirm_window_s,
                "cutPinLabel": bundle.config.cut_pin_label,
            },
        })
