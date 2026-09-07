"""Clock abstraction for the block runtime.

Schedules and hour()/minute()/weekday() use *wall* time in the bundle's timezone;
holds, limits and since() use a *monotonic* clock. The runtime never calls
time.time() directly so the simulator and the tests can drive it.

``clock_ok()`` says whether the wall clock is trustworthy: on the node that means
NTP has synchronised or a battery-backed RTC is present (the kit carries a
DS3231 — _audit/98 §5, R-21). While it is False the runtime holds schedules
(rules keep running) and buffered readings are marked ts_uncertain.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo


class Clock:
    def monotonic(self) -> float:  # seconds
        raise NotImplementedError

    def now(self, tz: str) -> datetime:
        raise NotImplementedError

    def clock_ok(self) -> bool:
        return True


class SystemClock(Clock):
    """Real clocks. ``clock_ok`` is probed at most every ``probe_interval`` seconds."""

    def __init__(self, probe_interval: float = 60.0, assume_ok: Optional[bool] = None):
        self._probe_interval = probe_interval
        self._assume_ok = assume_ok
        self._last_probe = 0.0
        self._ok = True

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self, tz: str) -> datetime:
        try:
            return datetime.now(ZoneInfo(tz or "Asia/Kolkata"))
        except Exception:  # noqa: BLE001 — unknown zone name
            return datetime.now(timezone.utc)

    def clock_ok(self) -> bool:
        if self._assume_ok is not None:
            return self._assume_ok
        now = time.monotonic()
        if now - self._last_probe < self._probe_interval:
            return self._ok
        self._last_probe = now
        self._ok = _probe_clock()
        return self._ok


def _probe_clock() -> bool:
    if sys.platform != "linux":
        return True  # laptop / CI: trust the OS clock
    if os.path.exists("/dev/rtc0") or os.path.exists("/dev/rtc"):
        return True
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=3,
        )
        return out.returncode == 0 and out.stdout.strip().lower() == "yes"
    except Exception:  # noqa: BLE001
        return False


class SimClock(Clock):
    """Settable clock for tests and the simulator. ``advance`` moves both clocks."""

    def __init__(self, start: Optional[datetime] = None, tz: str = "Asia/Kolkata", ok: bool = True):
        self.tz = tz
        self._mono = 1000.0
        self._wall = start or datetime(2026, 9, 7, 5, 0, tzinfo=ZoneInfo(tz))
        self.ok = ok

    def monotonic(self) -> float:
        return self._mono

    def now(self, tz: str) -> datetime:
        try:
            return self._wall.astimezone(ZoneInfo(tz or self.tz))
        except Exception:  # noqa: BLE001
            return self._wall

    def clock_ok(self) -> bool:
        return self.ok

    def advance(self, seconds: float) -> None:
        self._mono += seconds
        from datetime import timedelta
        self._wall = self._wall + timedelta(seconds=seconds)

    def set_wall(self, dt: datetime) -> None:
        self._wall = dt
