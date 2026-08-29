"""Adaptive live-cadence controller — ADR-013 §4.4 / §9 decision #3 (ratified).

The *live* telemetry channel (``state.byPort`` denorms, never persisted per
reading) is the single biggest fleet-cost lever left: a node publishing live
every 10 s costs ~₹250–380/node-mo; publishing every 60 s when nobody is
watching cuts that to ~₹40–65/node-mo, and it changes nothing a user ever sees
except during the first seconds of opening a screen.

Policy (ratified 2026-07-07):
* Publish the live snapshot every **10 s while an app session is actively
  watching** this node's data.
* Otherwise fall back to **60 s**.

Watch-signal source (documented, v0.1.8): a simple in-process flag driven by a
"watch" hint. In this release the hint is wired to two cheap sources:
  1. a config default (``telemetry.live_watch_default``) for bench/dev, and
  2. an explicit :meth:`set_watching` call, which the MQTT command handler can
     invoke when the backend forwards a presence/onSnapshot heartbeat (a
     ``liveWatch`` command — a follow-up backend hook; the daemon side is ready).
A watch is held for ``watch_ttl_sec`` after the last signal, then decays back to
the idle cadence — so a closed app relaxes the node automatically without a
"stop watching" round-trip. Backend-driven presence docs (ADR-013 §4.4) can
replace the flag later with zero daemon change beyond the signal wiring.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

DEFAULT_WATCHING_INTERVAL_SEC = 10   # active-watch live cadence
DEFAULT_IDLE_INTERVAL_SEC = 60       # nobody watching
DEFAULT_WATCH_TTL_SEC = 90           # a watch lapses this long after last signal


class LiveCadenceController:
    """Tracks whether an app is watching and yields the current live interval.

    Thread-safe: the sampling thread reads :meth:`current_interval` /
    :meth:`is_watching`; the command thread calls :meth:`set_watching`.
    """

    def __init__(
        self,
        watching_interval_sec: int = DEFAULT_WATCHING_INTERVAL_SEC,
        idle_interval_sec: int = DEFAULT_IDLE_INTERVAL_SEC,
        watch_ttl_sec: int = DEFAULT_WATCH_TTL_SEC,
        default_watching: bool = False,
        _clock=time.monotonic,
    ) -> None:
        self.watching_interval_sec = watching_interval_sec
        self.idle_interval_sec = idle_interval_sec
        self.watch_ttl_sec = watch_ttl_sec
        self._clock = _clock
        self._lock = threading.Lock()
        # None => never signalled. A default-watching node starts "watched now".
        self._last_watch_at: float | None = self._clock() if default_watching else None

    def set_watching(self, watching: bool = True) -> None:
        """Record a watch signal (or clear it).

        ``watching=True`` refreshes the TTL — every presence heartbeat from a
        live-viewing app extends the fast cadence. ``watching=False`` drops back
        to idle immediately (an explicit "stopped watching")."""
        with self._lock:
            self._last_watch_at = self._clock() if watching else None
        logger.debug("Live cadence watch signal: %s", watching)

    def is_watching(self) -> bool:
        with self._lock:
            if self._last_watch_at is None:
                return False
            return (self._clock() - self._last_watch_at) <= self.watch_ttl_sec

    def current_interval(self) -> int:
        """The live-publish interval to use right now (seconds)."""
        return self.watching_interval_sec if self.is_watching() else self.idle_interval_sec
