"""Node events to the backend over HTTPS — the path every event the node raises takes (contracts v1.14.2).

Until 0.3.4 a node's events — the protection trips (``sensors/protection.py``: dry-run cut-off,
low water, baseline learned), the block runtime's (``blocks/cloud.py`` ``CloudSink.event``: plot
and schedule runs, commands applied or failed, outputs released, a run cut short by an outage …)
and ``buffer_overflow`` (``telemetry_buffer.py``) — went out on the MQTT status topic only, which
nothing consumes (Cloud Functions cannot subscribe), in a flat body the ingress answers 400. No
node alert reached the cloud (SYNC.md v1.14.1 3e).

Agent 0.4.0: the bundle-v3 built-ins' events (``blocks/v3/runtime.py``, through the same
``CloudSink.event``, source ``node`` — valve_fault, valve_reset, guard_tripped, estop_pressed,
estop_cleared, power_cut, power_restored, watering_resumed, watering_not_finished,
instruction_switched) take this path too, and only this one reaches the cloud (contracts v1.15.0
SYNC.md item 8 c; functions.yaml 1.11.1 telemetryIngress).

Every call site keeps calling ``AgalOneMqttClient.publish_event``; main.py gives the MQTT client a
``NodeEvents`` as its event sink (``wire_node_events``), and each event goes to the status topic
(when connected — harmless) AND into this queue, connected or not. A worker thread posts it
through ``HttpReporter.report_sensor_event``: ``{type: "sensor_event", payload: {nodeUid, event,
timestamp, eventId, timestampUncertain?}}`` with the node's ``Authorization: Bearer`` token
(functions.yaml 1.10.2 SensorEventPayload; the ingress takes a sensor_event only with the token,
and files it under the node the token belongs to). Submitting never waits on the network: a
protection trip cuts the relay first and its report follows.

- ``nodeUid`` is the node's own uid (config.yaml ``node.uid``); the MQTT copy has always carried
  ``mqtt.username``, which on a node with the shared device login is the shared login's name.
- ``eventId`` (20 random ``[A-Za-z0-9]``) is minted when the event is raised and is the same on
  every retry: the backend writes ``/events/{nodeUid}_{eventId}`` once, so a retry of an event
  that did land changes nothing and rings no phone twice.
- ``timestamp`` is when it was raised: by the backend's time from its HTTPS answers when the node has
  seen one (``server_time.py``; v1.14.2 review), else by the node's clock; while neither is trusted
  (no backend answer yet, ``blocks/clock.py`` ``clock_ok()`` false — no NTP sync yet, no RTC) the
  event says ``timestampUncertain: true`` and the backend files it at receive time.
- In order, one at a time: an event not delivered (the ingress not reached, 408, 429, 5xx) is
  tried again after a back-off, and the events behind it wait — for up to ``DEADLINE_S`` (24 h:
  a dry-run trip during a long outage still arrives); then it is given up with an error in the
  log. Any other 4xx is never going to be accepted: given up at once, logged.
- The ingress allows 1000 requests per node per hour, shared with heartbeats, telemetry, acks
  and card variables: events get a budget of ``BUDGET_PER_HOUR`` with a burst of ``BURST``; an
  event over budget waits, it is not dropped.
- Memory stays bounded: past ``CAP`` waiting events the oldest that is not a protection event
  leaves (logged), and only when all are protection events the oldest of them. A bundle-v3
  program's built-in events (source ``node``, agent 0.4.0) count as protection events here.
- On a clean stop each waiting event gets one more try, back-offs skipped, for a few seconds.
"""

from __future__ import annotations

import logging
import math
import secrets
import string
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_ID_ALPHABET = string.ascii_letters + string.digits


def new_event_id() -> str:
    """The node's own id for one event (SensorEventPayload.eventId: [A-Za-z0-9_-]{8,64})."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(20))


def _jsonable(value: Any) -> Any:
    """The event as the ingress can take it: NaN / Infinity (a sensor that read nothing) become
    null — JSON has neither, and the ingress would answer 400 — and any other value JSON cannot
    carry becomes its text (an event that cannot be serialised would be retried for nothing)."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


@dataclass(eq=False)
class _Event:
    event: dict
    event_id: str
    timestamp: int
    uncertain: bool
    queued_at: float
    tries: int = 0
    last: str = ""

    @property
    def protection(self) -> bool:
        # The legacy protection trips, and (agent 0.4.0) the built-ins of a bundle-v3 program — source `node`:
        # valve_fault, estop_pressed, guard_tripped, power_cut … (blocks/v3/runtime.py). They take the place
        # of the protection thread on a migrated node, so they too are the last to leave a full queue. A
        # language-1 program raises none (its events are source `program`).
        return self.event.get("source") in ("protection", "node")


class NodeEvents:
    """The HTTPS leg of every node event (see the module docstring)."""

    BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0)
    RATE_LIMITED_S = 60.0
    DEADLINE_S = 24 * 3600.0
    CAP = 256
    BUDGET_PER_HOUR = 300.0
    BURST = 30

    def __init__(self, reporter, *, clock_ok: Optional[Callable[[], bool]] = None,
                 trusted_now: Optional[Callable[[], Optional[int]]] = None,
                 backoff_s: Optional[tuple] = None, rate_limited_s: Optional[float] = None,
                 deadline_s: Optional[float] = None, cap: Optional[int] = None,
                 budget_per_hour: Optional[float] = None, burst: Optional[int] = None,
                 now_ms: Optional[Callable[[], int]] = None):
        self.reporter = reporter
        if clock_ok is None:
            from .blocks.clock import SystemClock
            clock_ok = SystemClock().clock_ok
        self.clock_ok = clock_ok
        self.trusted_now = trusted_now
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.backoff_s = tuple(self.BACKOFF_S if backoff_s is None else backoff_s)
        self.rate_limited_s = self.RATE_LIMITED_S if rate_limited_s is None else rate_limited_s
        self.deadline_s = self.DEADLINE_S if deadline_s is None else deadline_s
        self.cap = max(1, self.CAP if cap is None else cap)
        self.budget_per_hour = self.BUDGET_PER_HOUR if budget_per_hour is None else budget_per_hour
        self.burst = float(self.BURST if burst is None else burst)
        self._tokens = self.burst
        self._tokens_at = time.monotonic()

        self._cond = threading.Condition()
        self._queue: deque[_Event] = deque()
        self._in_flight: Optional[_Event] = None
        self._due = 0.0
        self._flushing = False
        self._closed = False
        self._thread: Optional[threading.Thread] = None
        # Counters (the tests and a debugging session read them).
        self.posted = 0
        self.given_up = 0

    # ------------------------------------------------------------------ submit

    def submit(self, event: dict) -> Optional[str]:
        """Queue one event for the ingress; its eventId, or None when it was not queued. Never
        blocks on the network."""
        if not isinstance(event, dict) or not event.get("type"):
            logger.warning("node event without a type not reported: %r", event)
            return None
        at: Optional[int] = None
        if self.trusted_now is not None:
            try:
                at = self.trusted_now()
            except Exception:  # noqa: BLE001
                at = None
        if at is not None:
            uncertain = False
        else:
            try:
                uncertain = not bool(self.clock_ok())
            except Exception:  # noqa: BLE001 — a clock probe failure: say the time may be off
                uncertain = True
            at = self.now_ms()
        item = _Event(_jsonable(dict(event)), new_event_id(), int(at), uncertain, time.monotonic())
        with self._cond:
            if self._closed:
                logger.warning("node event %s after stop: not reported", event.get("type"))
                return None
            if len(self._queue) >= self.cap:
                self._make_room_locked()
            self._queue.append(item)
            self._ensure_thread_locked()
            self._cond.notify_all()
        return item.event_id

    # ------------------------------------------------------------- lifecycle

    def wait_idle(self, timeout: float = 5.0) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: not self._queue and self._in_flight is None, timeout)

    def close(self, timeout: float = 3.0) -> bool:
        """A clean stop: every waiting event gets one more try now, for at most ``timeout`` s."""
        with self._cond:
            self._flushing = True
            self._cond.notify_all()
            done = self._cond.wait_for(lambda: not self._queue and self._in_flight is None, timeout)
            left = len(self._queue)
            self._closed = True
            self._cond.notify_all()
        if not done:
            logger.warning("stopping with %d node event(s) not delivered to the ingress", left)
        return done

    @property
    def waiting(self) -> int:
        with self._cond:
            return len(self._queue)

    # ---------------------------------------------------------------- worker

    def _ensure_thread_locked(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="node-events", daemon=True)
            self._thread.start()

    def _make_room_locked(self) -> None:
        victim = next((e for e in self._queue if not e.protection), None) or self._queue[0]
        self._queue.remove(victim)
        self.given_up += 1
        logger.error("node event %s (%s) dropped: %d events are waiting for the ingress",
                     victim.event.get("type"), victim.event_id, len(self._queue) + 1)

    def _take_token_locked(self) -> float:
        """0.0 when a post may go out now, else the seconds until the budget allows one."""
        if self.budget_per_hour <= 0:
            return 0.0
        now = time.monotonic()
        self._tokens = min(self.burst, self._tokens + (now - self._tokens_at) * self.budget_per_hour / 3600.0)
        self._tokens_at = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        return (1.0 - self._tokens) * 3600.0 / self.budget_per_hour

    def _next_locked(self) -> tuple[Optional[_Event], Optional[float]]:
        if not self._queue:
            return None, None
        if not self._flushing:
            wait = self._due - time.monotonic()
            if wait > 0:
                return None, wait
            wait = self._take_token_locked()
            if wait > 0:
                return None, wait
        return self._queue.popleft(), None

    def _run(self) -> None:
        while True:
            try:
                with self._cond:
                    while True:
                        if self._closed:
                            return
                        item, wait = self._next_locked()
                        if item is not None:
                            self._in_flight = item
                            break
                        self._cond.notify_all()  # wait_idle / close
                        self._cond.wait(wait)
                status = self._post(item)
                with self._cond:
                    self._in_flight = None
                    self._settle_locked(item, status)
                    self._cond.notify_all()
            except Exception:  # noqa: BLE001 — a worker that died would silence every alert
                logger.exception("node event worker")
                with self._cond:
                    self._in_flight = None
                    self._cond.notify_all()
                time.sleep(1.0)

    def _post(self, item: _Event) -> Optional[int]:
        item.tries += 1
        try:
            status = self.reporter.report_sensor_event(item.event, event_id=item.event_id,
                                                       timestamp=item.timestamp,
                                                       timestamp_uncertain=item.uncertain)
        except Exception as e:  # noqa: BLE001 — a reporter failure is a failed try
            logger.warning("node event %s (%s): %s", item.event.get("type"), item.event_id, e)
            return None
        if isinstance(status, bool):
            return 200 if status else None
        return status

    def _settle_locked(self, item: _Event, status: Optional[int]) -> None:
        if status is not None and 200 <= status < 300:
            self.posted += 1
            logger.info("node event %s reported (%s)", item.event.get("type"), item.event_id)
            return
        item.last = "the ingress was not reached" if status is None else f"HTTP {status}"
        if status is not None and status not in (408, 429) and status < 500:
            self._give_up(item, f"the ingress refused it ({item.last}); not retried")
            return
        now = time.monotonic()
        if self._flushing:
            self._give_up(item, f"stopping ({item.last})")
            return
        if now - item.queued_at >= self.deadline_s:
            self._give_up(item, f"not delivered in {self.deadline_s:g} s ({item.last})")
            return
        if status == 429:
            backoff = self.rate_limited_s
        else:
            backoff = self.backoff_s[min(item.tries - 1, len(self.backoff_s) - 1)] if self.backoff_s else 0.0
        self._due = now + backoff
        logger.info("node event %s (%s): %s; trying again in %.0f s", item.event.get("type"), item.event_id,
                    item.last, backoff)
        self._queue.appendleft(item)  # still the first: the events behind it keep their order

    def _give_up(self, item: _Event, why: str) -> None:
        self.given_up += 1
        logger.error("node event %s (%s) given up: %s", item.event.get("type"), item.event_id, why)


def wire_node_events(mqtt_client, reporter, *, offline: bool,
                     clock_ok: Optional[Callable[[], bool]] = None, server_time=None) -> Optional[NodeEvents]:
    """Give the MQTT client its event sink: every ``publish_event`` is also posted to the ingress
    as a sensor_event (main.py calls this; tests/test_node_events.py checks that it does). An event's
    time is the backend's (``server_time``) when known, else the node's clock (``clock_ok``). Offline
    (no cloud) nothing is wired and None is returned."""
    if offline:
        return None
    trusted = None
    if server_time is not None:
        if clock_ok is None:
            from .blocks.clock import SystemClock
            clock_ok = SystemClock().clock_ok
        from .server_time import trusted_now
        trusted = trusted_now(server_time, clock_ok)
    events = NodeEvents(reporter, clock_ok=clock_ok, trusted_now=trusted)
    mqtt_client.set_event_sink(events.submit)
    return events

