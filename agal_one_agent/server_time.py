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
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class ServerTime:
    """The latest backend ``Date`` with the monotonic time it arrived (see the module docstring)."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic):
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._anchor: Optional[tuple[float, float]] = None  # (server ms, monotonic s)
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
        server_ms = dt.timestamp() * 1000.0 + 500.0  # whole seconds: the middle of that second
        with self._lock:
            first = self._anchor is None
            self._anchor = (server_ms, self._monotonic())
            self.observations += 1
        if first:
            logger.info("the backend's time is known (from its HTTPS answers): commands are checked against it")
        return True

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
