"""The backend's time, as its HTTPS answers state it (contracts v1.14.2 review, command-signing.md §5.4).

Why. A command is signed with ``sigTs`` on the BACKEND's clock, and the node refuses one more than
10 minutes old or 2 minutes ahead (check 6). The node's own wall clock cannot always tell: a
Raspberry Pi has no RTC and comes back from a power cut at whatever time ``fake-hwclock`` saved,
until NTP syncs; a Pi 5 has ``/dev/rtc0`` but, with no battery, the same problem. Agent 0.3.4 as
first written skipped check 6 while the clock was not trusted, so a signed command captured from the
topic and never carried out stayed executable for 7 days (for ever after a damaged replay set).

Every HTTPS answer from the backend — telemetryIngress, getProgram, reportHardware, getBootState —
carries a ``Date`` header, authenticated by TLS. ``ServerTime.observe`` keeps the latest one with the
monotonic time it arrived; ``now_ms`` is that plus the monotonic time elapsed since. A ``Date`` has
whole seconds (the middle of its second is taken) and arrives after a round trip (the agent's
requests time out after at most 20 s): seconds, against windows of 10 and 2 minutes.

``trusted_now`` gives the node's trusted now for check 6, in order: the backend's time; before the
first answer since the agent started, the wall clock when ``clock_ok()`` (NTP / RTC) says so;
otherwise None — and the command gate then refuses every command as ``stale`` ("no trusted time
yet") until the backend has answered (the answer to that refusal's own ack is enough).

The offset (agent 0.4.4, contracts v1.26.0; nodes/README.md §14, ADR-031 §6). Each answer is also a
sample of how far the node's WALL clock is from the backend's: the wall clock when it was read minus
its Date + 500 ms (``add_sample``). ``offset`` is the median of the last 15 samples since the wall
clock last stepped against the monotonic clock by more than 1 s (NTP synchronising after a power cut,
a person setting the clock) — the reference's ``addSample`` / ``offsetOf`` (Agal/contracts/nodes/
reference/clock.mjs). The heartbeat's ``clock`` block carries it (clock_care.py).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: The offset is the median of at most this many answers (the reference's MAX_SAMPLES).
MAX_SAMPLES = 15
#: A step of the wall clock against the monotonic clock bigger than this (ms) restarts the samples (STEP_MS).
STEP_MS = 1000
#: A Date has whole seconds: the middle of that second is taken (DATE_MIDDLE_MS).
DATE_MIDDLE_MS = 500


def add_sample(history: list, wall_ms: float, mono_ms: float, date_ms: float) -> list:
    """The reference's addSample: one answer — the wall and monotonic clocks when it was read, its Date as ms — added to
    the samples since the wall clock last stepped; at most MAX_SAMPLES kept (the newest). Rounds as JavaScript's
    Math.round (half up), so the agent and the reference agree on every vector."""
    server = date_ms + DATE_MIDDLE_MS
    s = {"offsetMs": int(math.floor(wall_ms - server + 0.5)), "drift": wall_ms - mono_ms, "serverMs": server}
    prev = history[-1] if history else None
    kept = [] if prev is not None and abs(s["drift"] - prev["drift"]) > STEP_MS else list(history)
    kept.append(s)
    return kept[-MAX_SAMPLES:]


def offset_of(history: list) -> Optional[dict]:
    """The reference's offsetOf: {offsetMs (the median; an even count: the floor of the two middle ones' mean), samples,
    atMs (the backend's time of the newest)}, or None."""
    if not history:
        return None
    xs = sorted(int(h["offsetMs"]) for h in history)
    n = len(xs)
    median = xs[(n - 1) // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) // 2
    return {"offsetMs": median, "samples": n, "atMs": history[-1]["serverMs"]}


class ServerTime:
    """The latest backend ``Date`` with the monotonic time it arrived (see the module docstring)."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic,
                 wall_ms: Callable[[], float] = lambda: time.time() * 1000.0):
        self._monotonic = monotonic
        self._wall_ms = wall_ms
        self._lock = threading.Lock()
        self._anchor: Optional[tuple[float, float]] = None  # (server ms, monotonic s)
        self._samples: list = []  # agent 0.4.4: the wall clock against each answer's Date (add_sample)
        self.observations = 0

    def observe(self, headers: Any, url: str) -> bool:
        """Note the ``Date`` of one HTTPS answer (``headers``: the response's, ``url``: what was
        asked). An answer to anything but an ``https://`` URL, or without a readable ``Date``, is
        ignored. True when it was taken."""
        if not isinstance(url, str) or not url.lower().startswith("https://"):
            return False
        try:
            value = headers.get("Date") if headers is not None else None
        except Exception:  # noqa: BLE001 — whatever a test or an odd response hands us
            return False
        if not isinstance(value, str) or not value.strip():
            return False
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return False
        if dt is None:
            return False
        if dt.tzinfo is None:  # an HTTP-date is GMT
            dt = dt.replace(tzinfo=timezone.utc)
        date_ms = dt.timestamp() * 1000.0
        server_ms = date_ms + 500.0  # whole seconds: the middle of that second
        with self._lock:
            first = self._anchor is None
            mono = self._monotonic()
            self._anchor = (server_ms, mono)
            self.observations += 1
            try:
                self._samples = add_sample(self._samples, float(self._wall_ms()), mono * 1000.0, date_ms)
            except Exception as e:  # noqa: BLE001 — the offset is a report; the command check never depends on it
                logger.debug("server time: the offset sample was not taken: %s", e)
        if first:
            logger.info("the backend's time is known (from its HTTPS answers): commands are checked against it")
        return True

    def offset(self) -> Optional[dict]:
        """The node's wall clock minus the backend's time (agent 0.4.4, nodes/README.md §14): {offsetMs, samples, atMs}
        — offset_of the samples since the wall clock last stepped — or None before the first answer."""
        with self._lock:
            history = list(self._samples)
        return offset_of(history)

    def now_ms(self) -> Optional[int]:
        """The backend's time now, in ms since the epoch — None until an answer has been seen."""
        with self._lock:
            anchor = self._anchor
        if anchor is None:
            return None
        server_ms, at = anchor
        return int(server_ms + (self._monotonic() - at) * 1000.0)


def trusted_now(server_time: Optional[ServerTime], clock_ok: Callable[[], bool],
                wall_ms: Callable[[], int] = lambda: int(time.time() * 1000)) -> Callable[[], Optional[int]]:
    """The node's trusted now for check 6 (command-signing.md §5.4): the backend's time; else the
    wall clock when ``clock_ok()``; else None."""

    def now() -> Optional[int]:
        if server_time is not None:
            s = server_time.now_ms()
            if s is not None:
                return s
        try:
            ok = bool(clock_ok())
        except Exception:  # noqa: BLE001 — a probe failure: not trusted
            ok = False
        return int(wall_ms()) if ok else None

    return now
