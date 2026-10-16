"""Command acks to the backend over HTTPS — the one path every command ack takes.

The backend learns what became of a command (``/assetCommands/{commandId}.status``) only from
``telemetryIngress`` over HTTPS (``case "commandAck"`` → ``handleCommandAck``). Nothing consumes
the MQTT status topic — Cloud Functions cannot subscribe (``blocks/cloud.py``) — so while acks
went out on MQTT only, every command ended ``timeout`` (``checkCommandTimeouts`` times out a
command still in flight 60 s after it was created) although the node had switched.

Every call site keeps calling ``AgalOneMqttClient.publish_command_ack``; main.py gives the MQTT
client a ``CommandAcks`` as its ack sink (``wire_command_acks``), and each ack goes to the status
topic (when MQTT is connected — harmless, a future broker consumer may use it) AND into this
queue, whether MQTT is connected or not. A worker thread posts the queue through
``HttpReporter.report_command_ack`` (``{type: "commandAck", payload: {nodeUid, commandId,
status, appliedValue?, error?}}`` with the node's ``Authorization`` header — the ingress takes a
commandAck only with the node's verified token). Submitting never waits on the network: a
command is carried out, and the MQTT loop goes on, while the ack is still on its way.

What is posted. The ingress allows 1000 requests per node per hour, shared with heartbeats,
telemetry, card variables and alerts, so a command costs at most two posts, and a switch one:

- ``completed`` / ``failed`` (the final ack) — always posted, never dropped to make room, and
  retried while the ingress cannot be reached for ``FINAL_DEADLINE_S`` (5 minutes); then it is
  given up with an error in the log. A final ack the backend gets after its sweep still lands
  (the command did run), so a late one is worth sending;
- ``acknowledged`` — held ``COALESCE_S`` (0.5 s) first, and not posted when a later ack of the
  same command is queued by then: a switch that completes at once costs one post. A command
  that takes longer has its ``acknowledged`` posted after the hold: the backend records that the
  node has it (``acknowledgedAt``). It does not keep the command from timing out — the sweep
  ends ``acknowledged`` / ``executing`` 60 s after the command was created as well. Tried
  ``INTERMEDIATE_TRIES`` times;
- ``executing`` — after an ``acknowledged`` of the same command it tells the backend nothing
  new (the final ack follows), so it stays on MQTT; without one it is posted like
  ``acknowledged``;
- ids the backend keeps no command document for stay on MQTT (``UNTRACKED_PREFIXES``): a
  **boot-reconcile** ack (``boot_<assetId>``, minted by ``boot_reconciler``) and the node-level
  pokes the backend mints with a prefix (``sync_…``, ``scan_bus_…``, ``test_port_…``) — the
  backend answers them "untracked" and records nothing (a port test's and a bus scan's results
  come back as ``port_test`` / ``bus_scan``, a sync's as ``programAck``), so posting them would
  spend the budget for nothing. The OTA ``firmwareUpdate`` acks stay on MQTT at their call site
  (``publish_command_ack(..., ingress=False)``): the outcome comes back as ``firmware_update``
  events, and its id names a ``/nodeCommands`` doc, which commandAck does not track.

Order and bounds. Acks of one command are posted in the order they were made; an ack waits
while an earlier ack of its command is queued. Final acks also keep their order ACROSS commands:
when a final ack is not delivered and is to be tried again (the ingress not reached, 408, 429,
5xx), every ack waits out its back-off, so a switch ON whose ``completed`` met a 503 is never
overtaken by the ``completed`` of the OFF that followed it (the card would end up showing ON
with the relay off; the backend also refuses to echo an older command's value over a newer
one). An ``acknowledged`` / ``executing`` that arrives while ``MAX_WAITING`` acks are already
waiting (the ingress is not keeping up) is not queued; the final ack always is, up to
``HARD_CAP``. There the oldest waiting intermediate ack makes room, and only when every waiting
ack is final is the oldest one given up, with an error in the log — memory stays bounded
whatever happens. A 4xx other than 408 / 429 is never going to be accepted and is not retried.
On a clean stop each waiting ack gets one more try, holds and back-offs skipped, for a few
seconds.

Refusals (agent 0.3.4, contracts v1.14.2). A message the node refused before carrying anything
out — unsigned, forged, replayed, stale (command_signing.py) — is answered through this same
queue as a final ``failed`` ack with ``reason`` (enums.yaml#CommandRefusalReason) and ``error``
"refused: <reason> — …", for a tracked id only and at most 20 in 10 minutes (the gate decides
both). The backend lets ``unsigned`` / ``replayed`` move nothing and lets the node's own answer
for the command win over a ``bad_signature`` / ``stale`` one.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

FINAL_STATUSES = frozenset({"completed", "failed"})
BOOT_RECONCILE_PREFIX = "boot_"
# Command ids the backend keeps no /assetCommands doc for: its commandAck answers them "untracked"
# and records nothing (agal-one backend/functions/src/modules/mqtt/commandAck.ts, TRACKED_COMMAND_ID:
# only 20-character auto-ids are tracked). boot_ is minted here (boot_reconciler); sync_ (sync_<ts>,
# sync_program_…, sync_ports_…), scan_bus_ and test_port_ by the backend. Keep the two lists together.
UNTRACKED_PREFIXES = (BOOT_RECONCILE_PREFIX, "sync_", "scan_bus_", "test_port_")
MAX_ERROR_CHARS = 500  # the words of a failed ack, as programAck's reason


@dataclass(eq=False)
class _Ack:
    command_id: str
    status: str
    applied_value: Any
    error: Optional[str]
    queued_at: float
    due: float
    reason: Optional[str] = None  # a refusal's CommandRefusalReason (only with failed)
    tries: int = 0
    last: str = ""  # what the last try met, for the give-up log

    @property
    def final(self) -> bool:
        return self.status in FINAL_STATUSES


def _jsonable(value: Any) -> Any:
    """The applied value as the ingress can take it (an ack that cannot be serialised would be
    retried for nothing)."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


class CommandAcks:
    """The HTTPS leg of every command ack (see the module docstring)."""

    COALESCE_S = 0.5
    MAX_WAITING = 32
    HARD_CAP = 256
    INTERMEDIATE_TRIES = 3
    FINAL_DEADLINE_S = 300.0
    BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
    RATE_LIMITED_S = 60.0
    REMEMBERED_ACKNOWLEDGED = 512

    def __init__(self, reporter, *, coalesce_s: Optional[float] = None, max_waiting: Optional[int] = None,
                 hard_cap: Optional[int] = None, intermediate_tries: Optional[int] = None,
                 final_deadline_s: Optional[float] = None, backoff_s: Optional[tuple] = None,
                 rate_limited_s: Optional[float] = None):
        self.reporter = reporter
        self.coalesce_s = self.COALESCE_S if coalesce_s is None else coalesce_s
        self.max_waiting = self.MAX_WAITING if max_waiting is None else max_waiting
        self.hard_cap = max(self.max_waiting, self.HARD_CAP if hard_cap is None else hard_cap)
        self.intermediate_tries = self.INTERMEDIATE_TRIES if intermediate_tries is None else intermediate_tries
        self.final_deadline_s = self.FINAL_DEADLINE_S if final_deadline_s is None else final_deadline_s
        self.backoff_s = tuple(self.BACKOFF_S if backoff_s is None else backoff_s)
        self.rate_limited_s = self.RATE_LIMITED_S if rate_limited_s is None else rate_limited_s

        self._cond = threading.Condition()
        self._queue: deque[_Ack] = deque()
        self._in_flight: Optional[_Ack] = None
        self._acknowledged: OrderedDict[str, None] = OrderedDict()
        self._paused_until = 0.0
        self._flushing = False
        self._closed = False
        self._thread: Optional[threading.Thread] = None
        # Counters (the tests and a debugging session read them).
        self.posted = 0
        self.dropped = 0  # intermediate acks not posted (superseded, backed up, out of tries)
        self.given_up = 0  # final acks not delivered (deadline, refused, cap)

    # ------------------------------------------------------------------ submit

    def submit(self, command_id: str, status: str, applied_value: Any = None, error: Optional[str] = None,
               reason: Optional[str] = None) -> None:
        """Queue one ack for the ingress. Never blocks on the network. ``reason`` only with
        ``failed``: the node refused a message carrying this id (command_signing.py)."""
        if not command_id or str(command_id).startswith(UNTRACKED_PREFIXES):
            return  # no command doc on the backend: MQTT only (module docstring)
        command_id = str(command_id)
        with self._cond:
            if self._closed:
                logger.debug("command ack %s %s after stop: not posted", command_id, status)
                return
            if status == "acknowledged":
                self._acknowledged[command_id] = None
                self._acknowledged.move_to_end(command_id)
                while len(self._acknowledged) > self.REMEMBERED_ACKNOWLEDGED:
                    self._acknowledged.popitem(last=False)
            elif status == "executing" and command_id in self._acknowledged:
                return  # MQTT only: the backend knows the command arrived; its final ack follows
            now = time.monotonic()
            ack = _Ack(command_id, status, _jsonable(applied_value),
                       None if error is None else str(error)[:MAX_ERROR_CHARS], queued_at=now,
                       due=now if status in FINAL_STATUSES else now + self.coalesce_s,
                       reason=reason if status == "failed" and reason else None)
            if not ack.final and len(self._queue) >= self.max_waiting:
                self.dropped += 1
                logger.info("command ack %s %s not posted: %d acks are waiting for the ingress",
                            command_id, status, len(self._queue))
                return
            if ack.final and len(self._queue) >= self.hard_cap:
                self._make_room_locked()
            self._queue.append(ack)
            self._ensure_thread_locked()
            self._cond.notify_all()

    # ------------------------------------------------------------- lifecycle

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """True once nothing is queued or in flight (holds and back-offs are waited out)."""
        with self._cond:
            return self._cond.wait_for(lambda: not self._queue and self._in_flight is None, timeout)

    def close(self, timeout: float = 3.0) -> bool:
        """A clean stop: every waiting ack gets one more try now, for at most ``timeout`` s.
        True when nothing was left behind."""
        with self._cond:
            self._flushing = True
            self._cond.notify_all()
            done = self._cond.wait_for(lambda: not self._queue and self._in_flight is None, timeout)
            left = len(self._queue)
            self._closed = True
            self._cond.notify_all()
        if not done:
            logger.warning("stopping with %d command ack(s) not delivered to the ingress", left)
        return done

    @property
    def waiting(self) -> int:
        with self._cond:
            return len(self._queue)

    # ---------------------------------------------------------------- worker

    def _ensure_thread_locked(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="command-acks", daemon=True)
            self._thread.start()

    def _make_room_locked(self) -> None:
        for i, waiting in enumerate(self._queue):
            if not waiting.final:
                del self._queue[i]
                self.dropped += 1
                logger.info("command ack %s %s dropped: the ack queue is full", waiting.command_id, waiting.status)
                return
        oldest = min(self._queue, key=lambda a: a.queued_at)
        self._queue.remove(oldest)
        self.given_up += 1
        logger.error("command ack %s %s given up: %d final acks are waiting for the ingress (%s)",
                     oldest.command_id, oldest.status, len(self._queue) + 1, oldest.last or "not tried yet")

    def _drop_superseded_locked(self) -> None:
        """An intermediate ack with a later ack of its command queued behind it says nothing the
        later one does not. A refusal (``reason``) is about another message that carried the id —
        a forged copy — so it supersedes nothing of the command's own acks."""
        later: set[str] = set()
        keep: deque[_Ack] = deque()
        for ack in reversed(self._queue):
            if not ack.final and ack.command_id in later:
                self.dropped += 1
                logger.debug("command ack %s %s superseded", ack.command_id, ack.status)
                continue
            if ack.reason is None:
                later.add(ack.command_id)
            keep.appendleft(ack)
        self._queue = keep

    def _next_locked(self) -> tuple[Optional[_Ack], Optional[float]]:
        """The ack to post now (taken out of the queue), else how long to wait (None: until told)."""
        self._drop_superseded_locked()
        if not self._queue:
            return None, None
        now = time.monotonic()
        if not self._flushing and self._paused_until > now:
            return None, self._paused_until - now
        seen: set[str] = set()
        soonest: Optional[float] = None
        for i, ack in enumerate(self._queue):
            if ack.command_id in seen:
                continue  # an earlier ack of this command goes first
            seen.add(ack.command_id)
            if self._flushing or ack.due <= now:
                del self._queue[i]
                return ack, None
            soonest = ack.due if soonest is None else min(soonest, ack.due)
        return None, None if soonest is None else max(0.0, soonest - now)

    def _run(self) -> None:
        while True:
            try:
                with self._cond:
                    while True:
                        if self._closed:
                            return
                        ack, wait = self._next_locked()
                        if ack is not None:
                            self._in_flight = ack
                            break
                        self._cond.notify_all()  # wait_idle / close
                        self._cond.wait(wait)
                status = self._post(ack)
                with self._cond:
                    self._in_flight = None
                    self._settle_locked(ack, status)
                    self._cond.notify_all()
            except Exception:  # noqa: BLE001 — a worker that died would bring the timeouts back
                logger.exception("command ack worker")
                with self._cond:
                    self._in_flight = None
                    self._cond.notify_all()
                time.sleep(1.0)

    def _post(self, ack: _Ack) -> Optional[int]:
        """The HTTP status of the post; None when the ingress was not reached."""
        ack.tries += 1
        try:
            extra = {"reason": ack.reason} if ack.reason else {}
            status = self.reporter.report_command_ack(ack.command_id, ack.status,
                                                      applied_value=ack.applied_value, error=ack.error, **extra)
        except Exception as e:  # noqa: BLE001 — a reporter failure is a failed try, never the worker's end
            logger.warning("command ack %s %s: %s", ack.command_id, ack.status, e)
            return None
        if isinstance(status, bool):  # a reporter that answers True / False
            return 200 if status else None
        return status

    def _settle_locked(self, ack: _Ack, status: Optional[int]) -> None:
        if status is not None and 200 <= status < 300:
            self.posted += 1
            return
        ack.last = "the ingress was not reached" if status is None else f"HTTP {status}"
        if status is not None and status not in (408, 429) and status < 500:
            self._give_up(ack, f"the ingress refused it ({ack.last}); not retried")
            return
        now = time.monotonic()
        if self._flushing:
            self._give_up(ack, f"stopping ({ack.last})")
            return
        if ack.final:
            deadline = ack.queued_at + self.final_deadline_s
            if now >= deadline:
                self._give_up(ack, f"not delivered in {self.final_deadline_s:g} s ({ack.last}); "
                                   "the backend will show the command timed out")
                return
        elif ack.tries >= self.intermediate_tries:
            self._give_up(ack, f"not delivered after {ack.tries} tries ({ack.last})")
            return
        if status == 429:
            backoff = self.rate_limited_s
        else:
            backoff = self.backoff_s[min(ack.tries - 1, len(self.backoff_s) - 1)] if self.backoff_s else 0.0
        ack.due = now + backoff
        if ack.final:
            ack.due = min(ack.due, ack.queued_at + self.final_deadline_s)  # the last try lands on the deadline
        if status is None or status == 429 or ack.final:
            # The ingress unreachable or rate-limited: every ack waits (the ingress, not this ack,
            # is the problem). A final ack to be tried again: every ack waits too, so no later
            # command's final ack overtakes it (an older ON landing after a newer OFF).
            self._paused_until = max(self._paused_until, ack.due)
        logger.info("command ack %s %s: %s; trying again in %.0f s", ack.command_id, ack.status, ack.last,
                    max(0.0, ack.due - now))
        self._queue.appendleft(ack)  # still the first of its command

    def _give_up(self, ack: _Ack, why: str) -> None:
        if ack.final:
            self.given_up += 1
            logger.error("command ack %s %s given up: %s", ack.command_id, ack.status, why)
        else:
            self.dropped += 1
            logger.info("command ack %s %s dropped: %s", ack.command_id, ack.status, why)


def wire_command_acks(mqtt_client, reporter, *, offline: bool) -> Optional[CommandAcks]:
    """Give the MQTT client its ack sink: every ``publish_command_ack`` is also posted to the
    ingress (main.py calls this; leaving it out is how every command came to end ``timeout``).
    Offline (no cloud) nothing is wired and None is returned."""
    if offline:
        return None
    acks = CommandAcks(reporter)
    mqtt_client.set_ack_sink(acks.submit)
    return acks
