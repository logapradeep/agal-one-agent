"""Analog inputs, read from what the PORT says (contracts v1.9.1, ADR-024).

An AI port is an ADC channel plus wiring facts that travel WITH its transport — in the
compiled bundle and in a ``testPort`` command:

* ``measure``   ``{mode: dc | ac_rms, windowMs}`` — how the channel is read;
* ``transform`` ``{scale, offset, clampMin, clampMax}`` — volts → engineering units
  (``scale`` is units per volt).

So a current sensor on a mains motor is not a driver and needs no entry in ``config.pins``:
it is ``ac_rms`` and a scale on a channel. ``ac_rms`` removes the mean of the window before it
squares, so a Hall sensor idling at half its supply, or a current transformer on a mid-rail
bias, needs no zero calibration — and a supply that drifts does not read as current.

Only what the agent can really read is supported: today one driver, ``ads1115`` on I2C. A
port it cannot read answers ``None`` (the program keeps its last value) and a port test says
so in words; nothing is ever simulated here.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable, Optional

from .gpiochip import PortUnavailable

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_MS = 100   # five cycles at 50 Hz, six at 60 Hz
MIN_AC_SAMPLES = 8        # fewer than this over a window says nothing about an alternating signal
#: The widest range the converter has: a sensor powered at 5 V idles at 2.5 V and swings to
#: 4.5 V, which the ±4.096 V range would clip. 0.19 mV per step is finer than any sensor here.
ADS1115_RANGE_V = 6.144


def summarise(samples: list[float], mode: str) -> Optional[float]:
    """Volts for one window: the mean (``dc``) or the RMS of the alternating part (``ac_rms``)."""
    vals = [v for v in samples if v == v]  # a failed conversion is NaN
    if not vals:
        return None
    mean = sum(vals) / len(vals)
    if mode != "ac_rms":
        return mean
    if len(vals) < MIN_AC_SAMPLES:
        return None
    return math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))


def scaled(volts: float, transform: Optional[dict]) -> float:
    """``volts × scale + offset``, bounded by ``clampMin`` / ``clampMax``."""
    t = transform or {}
    out = volts * float(t.get("scale", 1.0)) + float(t.get("offset", 0.0))
    if t.get("clampMin") is not None:
        out = max(float(t["clampMin"]), out)
    if t.get("clampMax") is not None:
        out = min(float(t["clampMax"]), out)
    return out


class AnalogReader:
    """Reads an AI port by its transport. One ADC object per (bus, address); a channel is
    not sampled again within ``min_interval_s`` — a program pass reads every input, and three
    phase currents at 100 ms each must not stretch the pass."""

    def __init__(self, adc_factory: Optional[Callable[[int, int], object]] = None,
                 clock: Callable[[], float] = time.monotonic, min_interval_s: float = 0.5):
        self._adc_factory = adc_factory
        self._clock = clock
        self._min_interval_s = min_interval_s
        self._adcs: dict[tuple[int, int], object] = {}
        self._last: dict[tuple, tuple[float, Optional[float]]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def supports(transport: dict) -> bool:
        return (transport.get("kind") == "i2c" and transport.get("driver") == "ads1115"
                and transport.get("channel") in (0, 1, 2, 3) and transport.get("addr") is not None)

    def _adc(self, bus: int, addr: int):
        key = (bus, addr)
        if key not in self._adcs:
            if self._adc_factory is not None:
                self._adcs[key] = self._adc_factory(bus, addr)
            else:
                from ..sensors.ads1115 import Ads1115, is_hardware_available  # noqa: PLC0415
                if not is_hardware_available():
                    raise PortUnavailable("the I2C library (smbus2) is not installed on this node")
                try:
                    self._adcs[key] = Ads1115(bus_number=bus, address=addr, gain_v=ADS1115_RANGE_V, data_rate_sps=860)
                except OSError as e:
                    raise PortUnavailable(f"I2C bus {bus} cannot be opened: {e}") from e
        return self._adcs[key]

    def read_volts(self, transport: dict, fresh: bool = False) -> Optional[float]:
        """One window on the channel, in volts at the ADC pin. ``None`` = nothing answered."""
        if not self.supports(transport):
            raise PortUnavailable(f"this agent cannot read a {transport.get('driver') or transport.get('kind')} port")
        measure = transport.get("measure") or {}
        mode = "ac_rms" if measure.get("mode") == "ac_rms" else "dc"
        window_s = max(20, min(2000, int(measure.get("windowMs") or DEFAULT_WINDOW_MS))) / 1000.0
        bus, addr, channel = int(transport.get("busId", 1)), int(transport["addr"]), int(transport["channel"])
        key = (bus, addr, channel, mode, window_s)
        with self._lock:
            now = self._clock()
            last = self._last.get(key)
            if not fresh and last is not None and (now - last[0]) < self._min_interval_s:
                return last[1]
            adc = self._adc(bus, addr)
            samples: list[float] = []
            deadline = self._clock() + window_s
            while self._clock() < deadline and len(samples) < 2000:
                samples.append(adc.read_v(channel))
            volts = summarise(samples, mode)
            self._last[key] = (self._clock(), volts)
            return volts

    def read(self, transport: dict, fresh: bool = False) -> Optional[float]:
        """The port's value in its own unit (amps, bar, % …)."""
        volts = self.read_volts(transport, fresh=fresh)
        return None if volts is None else scaled(volts, transport.get("transform"))

    def close(self) -> None:
        with self._lock:
            for adc in self._adcs.values():
                try:
                    adc.close()
                except Exception:  # noqa: BLE001
                    pass
            self._adcs.clear()
