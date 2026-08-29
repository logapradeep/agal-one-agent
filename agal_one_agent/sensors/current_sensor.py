"""ACS758 Hall-effect current sensor, read via ADS1115 ADC.

Default v1 launch hardware:
  - ACS758LCB-050B (bidirectional, ±50 A, 40 mV/A sensitivity, 5 V VCC)
  - ADS1115 ADC at I2C address 0x48, channel 0, gain ±4.096 V
  - ACS758 V_OUT → ADS1115 A0 (single-ended, AGND tied to ADS1115 GND)

The sensor outputs `VCC/2` (≈ 2.5 V) at zero current. Output deviates by
`sensitivity_mv_per_a * I` for current I.

For single-phase mains pumps we measure RMS current across a 100 ms window —
five full cycles at 50 Hz — sampling as fast as the ADS1115 allows (860 SPS in
single-shot ≈ ~2 ms per sample → ~50 samples per window).

A baseline is learned automatically: the first stable post-inrush window after
a pump start is captured as `baseline_current_a`. Dry-run detection compares
subsequent RMS readings against this baseline (a pump running dry unloads the
motor and current drops by 20–40%).

The class itself does NOT cut power — that is the protection module's
responsibility. This class just reads RMS values.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from .ads1115 import Ads1115, is_hardware_available

logger = logging.getLogger(__name__)

#: Default directory the daemon persists per-sensor baselines under.
#: Matches the Docker volume mount in `docker-compose.yml` and the install
#: location written by `scripts/install.sh`. Pass ``baseline_dir=None`` to
#: ``CurrentSensorACS758`` to disable persistence entirely (used in tests).
DEFAULT_BASELINE_DIR = Path("/var/lib/agal-one-agent")


@dataclass
class CurrentSensorReading:
    """One RMS reading sampled over a window."""

    timestamp: float  # epoch seconds at end of window
    rms_a: float
    samples: int
    window_s: float
    quality: str = "good"  # "good" | "uncertain" | "bad"


@dataclass
class CurrentSensorState:
    """In-memory state for one current sensor instance. Baseline values are
    persisted by ``CurrentSensorACS758`` to ``{baseline_dir}/baseline.{source_key}.json``
    so a daemon restart does not force a fresh inrush + learning window before
    dry-run protection re-arms."""

    baseline_a: Optional[float] = None
    baseline_learned_at: Optional[float] = None
    last_reading: Optional[CurrentSensorReading] = None
    inrush_blocking_until: float = 0.0
    history: list[CurrentSensorReading] = field(default_factory=list)


class CurrentSensorACS758:
    """ACS758 current sensor driver.

    `pin.sensor_params` accepts:
      ads1115_address:    int   (default 0x48)
      ads1115_channel:    int   (default 0)
      ads1115_bus:        int   (default 1)
      ads1115_gain_v:     float (default 4.096)
      sensitivity_mv_per_a: float (default 40.0 — for ±50 A variant; use 60 for ±20 A)
      vcc_v:              float (default 5.0)
      window_ms:          int   (default 100)
      sample_count:       int   (default 30 — caps actual samples per window)
      sensor_label:       str   (defaults to pin.label)
    """

    def __init__(
        self,
        pin,
        state: Optional[CurrentSensorState] = None,
        baseline_dir: Optional[Union[str, Path]] = DEFAULT_BASELINE_DIR,
    ) -> None:
        self.pin = pin
        params = pin.sensor_params or {}

        self.ads_address = int(params.get("ads1115_address", 0x48))
        self.ads_channel = int(params.get("ads1115_channel", 0))
        self.ads_bus = int(params.get("ads1115_bus", 1))
        self.ads_gain_v = float(params.get("ads1115_gain_v", 4.096))

        self.sensitivity_v_per_a = float(params.get("sensitivity_mv_per_a", 40.0)) / 1000.0
        self.vcc_v = float(params.get("vcc_v", 5.0))
        self.zero_offset_v = self.vcc_v / 2.0

        self.window_s = float(params.get("window_ms", 100)) / 1000.0
        self.sample_count = int(params.get("sample_count", 30))

        self.source_key = pin.label or f"current_pin_{pin.physical_pin}"
        self.state = state or CurrentSensorState()

        # Baseline persistence: defaults to /var/lib/agal-one-agent/baseline.{safe-source-key}.json
        # but degrades gracefully on any I/O failure (e.g. running on macOS where /var/lib
        # isn't writable, or before the install script has created the dir). Pass
        # baseline_dir=None to opt out entirely — tests use this to keep stderr clean.
        # Per-sensor config can also override via `pin.sensor_params["baseline_dir"]`.
        self._baseline_path: Optional[Path] = None
        param_baseline_dir = params.get("baseline_dir")
        effective_dir = Path(param_baseline_dir) if param_baseline_dir is not None else (
            Path(baseline_dir) if baseline_dir is not None else None
        )
        if effective_dir is not None:
            # Sanitize source_key for filesystem safety: keep alnum + . _ -, replace anything else.
            safe_key = re.sub(r"[^A-Za-z0-9._-]", "_", self.source_key) or "current"
            self._baseline_path = effective_dir / f"baseline.{safe_key}.json"
            self._load_baseline_if_present()

        self._adc = Ads1115(
            bus_number=self.ads_bus,
            address=self.ads_address,
            gain_v=self.ads_gain_v,
            data_rate_sps=860,
        )

    # ---- Reading -------------------------------------------------------

    def read_rms(self) -> CurrentSensorReading:
        """Sample the channel over the configured window, return RMS current in amps.

        On non-Pi dev boxes (smbus2 unavailable), returns a deterministic mock
        based on the configured baseline (or 0 A if no baseline). This lets the
        protection module be unit-tested end-to-end without hardware.
        """
        if not is_hardware_available():
            return self._read_rms_simulated()

        samples = []
        deadline = time.monotonic() + self.window_s
        end_time = time.time()
        while time.monotonic() < deadline and len(samples) < self.sample_count:
            v = self._adc.read_v(self.ads_channel)
            if v == v:  # NaN check
                samples.append(v)
            end_time = time.time()

        if not samples:
            return CurrentSensorReading(
                timestamp=end_time,
                rms_a=float("nan"),
                samples=0,
                window_s=self.window_s,
                quality="bad",
            )

        # Compute RMS around the zero-current offset.
        squared_sum = 0.0
        for v in samples:
            delta_v = v - self.zero_offset_v
            current_a = delta_v / self.sensitivity_v_per_a
            squared_sum += current_a * current_a

        rms_a = math.sqrt(squared_sum / len(samples))

        # Plausibility check: ACS758-050B physical range is ±50 A. Anything well
        # outside that on a single-phase pump signals broken wiring or saturated ADC.
        quality = "good"
        if rms_a > 100.0:
            quality = "bad"
        elif rms_a > 60.0:
            quality = "uncertain"

        reading = CurrentSensorReading(
            timestamp=end_time,
            rms_a=rms_a,
            samples=len(samples),
            window_s=self.window_s,
            quality=quality,
        )
        self.state.last_reading = reading
        self.state.history.append(reading)
        if len(self.state.history) > 60:
            self.state.history.pop(0)
        return reading

    def _read_rms_simulated(self) -> CurrentSensorReading:
        """Deterministic fallback when smbus2 is not available."""
        baseline = self.state.baseline_a or 0.0
        return CurrentSensorReading(
            timestamp=time.time(),
            rms_a=baseline,
            samples=0,
            window_s=self.window_s,
            quality="uncertain",  # mark simulated readings as uncertain
        )

    # ---- Baseline learning ---------------------------------------------

    def mark_pump_started(self, inrush_ignore_s: float) -> None:
        """Called when the linked relay turns the pump ON. Suppresses readings
        from contributing to the baseline (or to dry-run detection) until inrush
        is over."""
        self.state.inrush_blocking_until = time.monotonic() + inrush_ignore_s
        logger.info(
            "[%s] pump-start marker recorded; ignoring next %.1fs of readings",
            self.source_key, inrush_ignore_s,
        )

    def mark_pump_stopped(self) -> None:
        self.state.inrush_blocking_until = 0.0

    def is_in_inrush(self) -> bool:
        return time.monotonic() < self.state.inrush_blocking_until

    def learn_baseline(self, current_a: float) -> None:
        """Persist a learned baseline. Caller is responsible for picking a stable
        sample (typically: average over 30 s starting after inrush_ignore_s)."""
        self.state.baseline_a = current_a
        self.state.baseline_learned_at = time.time()
        logger.info("[%s] baseline learned: %.2f A", self.source_key, current_a)
        self._save_baseline()

    def has_baseline(self) -> bool:
        return self.state.baseline_a is not None

    def clear_baseline(self) -> None:
        """Drop the in-memory baseline AND delete the persisted file. Used when
        an operator wants to force re-learning (e.g. after a motor swap)."""
        self.state.baseline_a = None
        self.state.baseline_learned_at = None
        if self._baseline_path is None:
            return
        try:
            self._baseline_path.unlink(missing_ok=True)
            logger.info("[%s] cleared persisted baseline at %s", self.source_key, self._baseline_path)
        except OSError as e:
            logger.warning("[%s] failed to delete baseline file %s: %s",
                           self.source_key, self._baseline_path, e)

    # ---- Baseline persistence (internal) -------------------------------

    def _load_baseline_if_present(self) -> None:
        """Read ``{baseline_dir}/baseline.{safe_source_key}.json`` if it exists
        and populate ``self.state.baseline_a`` + ``baseline_learned_at``. Silent
        on missing file; warns and continues on corrupt content."""
        path = self._baseline_path
        if path is None or not path.exists():
            return
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[%s] could not load persisted baseline at %s: %s",
                           self.source_key, path, e)
            return

        baseline_a = data.get("baseline_a")
        baseline_learned_at = data.get("baseline_learned_at")
        if not isinstance(baseline_a, (int, float)) or baseline_a <= 0:
            logger.warning("[%s] ignoring invalid baseline_a in %s: %r",
                           self.source_key, path, baseline_a)
            return
        self.state.baseline_a = float(baseline_a)
        if isinstance(baseline_learned_at, (int, float)):
            self.state.baseline_learned_at = float(baseline_learned_at)
        logger.info("[%s] loaded persisted baseline: %.2f A (from %s)",
                    self.source_key, baseline_a, path)

    def _save_baseline(self) -> None:
        """Atomic-write the current baseline to disk. Best-effort: any I/O
        failure (no permission on /var/lib, dir missing, full disk) logs a
        warning and returns. The in-memory baseline still works for this
        process — only restart resilience is lost."""
        path = self._baseline_path
        if path is None or self.state.baseline_a is None:
            return
        payload = {
            "source_key": self.source_key,
            "baseline_a": float(self.state.baseline_a),
            "baseline_learned_at": self.state.baseline_learned_at,
            "schema_version": 1,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(path)  # atomic on POSIX
        except OSError as e:
            logger.warning("[%s] could not persist baseline to %s: %s",
                           self.source_key, path, e)

    # ---- Telemetry shape ------------------------------------------------

    def to_telemetry(self, reading: CurrentSensorReading) -> dict:
        """Convert a reading to the canonical telemetry wire shape consumed by
        the cloud telemetryIngress function and the AssetTelemetryReading schema."""
        return {
            "sourceKey": self.source_key,
            "kind": "current",
            "value": reading.rms_a if reading.rms_a == reading.rms_a else None,
            "unit": "A",
            "samples": reading.samples,
            "windowMs": int(reading.window_s * 1000),
            "quality": reading.quality,
        }

    def close(self) -> None:
        self._adc.close()
