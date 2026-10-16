"""Cloud-facing glue for the block runtime: the event sink that fans out to
MQTT / HTTPS / the durable telemetry buffer, the hardware I/O factory, and the
capabilities the heartbeat reports (ADR-017 §5; contracts v1.5.0 telemetryIngress
``programAck`` / ``variables`` / ``alert`` messages; iot-node.capabilities).

**A pass never waits on the network** (agent 0.4.0 review; programs/README.md §4.8). Every sink call is made by a
runtime pass, under the runtime lock — the lock the input sampler, every command and (bundle v3) the E-stop latch and
the motor guards wait for. So no sink call does network I/O on the caller's thread:

* ``variables`` / ``outputs`` → the budgeted ``_VariableUploader`` queue (as since 0.2.x);
* ``event`` → ``AgalOneMqttClient.publish_event`` → ``node_events.NodeEvents`` (as since 0.3.4);
* ``program_ack`` and ``alert`` → ``_Outbox`` (new): until this review both posted over HTTPS (``urlopen``, 10 s
  timeout) on the runtime thread — the first pass after every program load, in released 0.3.4 too;
* the MQTT copies are paho ``publish`` calls, which only queue the packet (paho 2.x with ``loop_start``: an in-memory
  deque and one byte on a non-blocking socket pair; the network thread writes the socket);
* ``reading`` → the SQLite telemetry buffer (a local file) and an MQTT ``publish``.

``http_async=False`` (tests and the simulator only) posts on the caller's thread, as before.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from .runtime import EventSink

logger = logging.getLogger(__name__)


class _Outbox:
    """The reports a pass hands over that have no queue of their own — a ``programAck`` and an ``alert``, each with
    its MQTT copy — sent by one worker thread, **in the order they were handed over**, each tried once, exactly as
    0.3.4 sent them on the runtime thread (a failed post is logged at debug level and not retried: a program's state
    also rides every heartbeat — ``programVersion`` / ``programStatus`` — and an alert rule fires again after its
    cooldown). Handing over never waits on the network.

    Memory stays bounded: past ``CAP`` waiting reports the oldest alert leaves (logged), and only when every waiting
    report is a ``programAck`` the oldest of them. On a clean stop what waits gets ``close``'s few seconds."""

    CAP = 256

    def __init__(self, name: str = "cloud-outbox"):
        self.name = name
        self._cond = threading.Condition()
        self._queue: deque[tuple[str, Callable[[], None]]] = deque()
        self._busy = False
        self._closed = False
        self._thread: Optional[threading.Thread] = None
        self.sent = 0
        self.dropped = 0

    def submit(self, kind: str, send: Callable[[], None]) -> bool:
        with self._cond:
            if self._closed:
                logger.warning("%s after stop: not reported", kind)
                return False
            if len(self._queue) >= self.CAP:
                victim = next((i for i in self._queue if i[0] != "programAck"), None) or self._queue[0]
                self._queue.remove(victim)
                self.dropped += 1
                logger.error("%s dropped: %d reports are waiting for the ingress", victim[0], len(self._queue) + 1)
            self._queue.append((kind, send))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
                self._thread.start()
            self._cond.notify_all()
        return True

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return  # close() said what was left
                kind, send = self._queue.popleft()
                self._busy = True
            try:
                send()
            except Exception as e:  # noqa: BLE001 — a report that fails never stops the ones behind it
                logger.debug("%s: %s", kind, e)
            finally:
                with self._cond:
                    self._busy = False
                    self.sent += 1
                    self._cond.notify_all()

    @property
    def waiting(self) -> int:
        with self._cond:
            return len(self._queue) + (1 if self._busy else 0)

    def flush(self, timeout: float = 5.0) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: not self._queue and not self._busy, timeout)

    def close(self, timeout: float = 3.0) -> bool:
        done = self.flush(timeout)
        with self._cond:
            left = len(self._queue)
            self._closed = True
            self._cond.notify_all()
        if not done:
            logger.warning("stopping with %d program ack(s) / alert(s) not sent", left)
        return done


class _VariableUploader:
    """Posts card variable snapshots to the ingress from its own thread — in
    order, one asset at a time, coalescing bursts (the latest snapshot of an
    asset wins while an earlier post is in flight), at most one post per asset
    per ``min_interval``. A failed post keeps the snapshot pending and retries
    after ``retry_after``. The runtime thread never waits on the network.

    Why HTTPS on every change: the instance has no MQTT consumer for the
    status topic (Cloud Functions cannot subscribe), so the ingress is the
    only path that reaches Firestore — and the phone."""

    # The ingress allows 1000 requests per node per hour (backend RateLimits.
    # telemetryIngress); heartbeats, acks, alerts and telemetry batches share
    # it. Card snapshots get a token bucket of 600/h (one every 6 s sustained,
    # a burst of 20 for start-up) so they can never spend the node's budget —
    # a snapshot that has to wait is simply coalesced into the next one.
    BUDGET_PER_HOUR = 600.0
    BURST = 20
    RATE_LIMITED_BACKOFF = 60.0

    def __init__(self, http, min_interval: float = 1.0, settle: float = 0.15, retry_after: float = 5.0):
        self.http = http
        self.min_interval = min_interval
        self.settle = settle
        self.retry_after = retry_after
        self._tokens = float(self.BURST)
        self._tokens_at = time.monotonic()
        self._pending: dict[str, dict] = {}
        # Contracts v1.15.1: a v3 program's card outputs (variables.outputs), coalesced like the values.
        self._pending_outputs: dict[str, dict] = {}
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.posted = 0
        self.failed = 0

    def submit(self, asset_id: str, values: dict, outputs: Optional[dict] = None) -> None:
        with self._lock:
            merged = dict(self._pending.pop(asset_id, {}))
            merged.update(values)
            self._pending[asset_id] = merged
            if outputs:
                outs = dict(self._pending_outputs.get(asset_id, {}))
                outs.update(outputs)
                self._pending_outputs[asset_id] = outs
            self._idle.clear()
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="variables-upload", daemon=True)
                self._thread.start()
        self._wake.set()

    def flush(self, timeout: float = 5.0) -> bool:
        return self._idle.wait(timeout)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _take_token(self) -> float:
        """0.0 when a post may go out now, else the seconds until a token is back."""
        now = time.monotonic()
        self._tokens = min(float(self.BURST), self._tokens + (now - self._tokens_at) * self.BUDGET_PER_HOUR / 3600.0)
        self._tokens_at = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        return (1.0 - self._tokens) * 3600.0 / self.BUDGET_PER_HOUR

    def _next(self) -> tuple[Optional[str], Optional[dict], float]:
        """The first pending asset that may be sent now, else how long to wait."""
        now = time.monotonic()
        with self._lock:
            wait = self.settle
            for aid, values in self._pending.items():
                due = self._last_sent.get(aid, 0.0) + self.min_interval
                if due <= now:
                    del self._pending[aid]
                    return aid, values, 0.0
                wait = min(wait, due - now)
            if not self._pending:
                self._idle.set()
            return None, None, wait

    def _requeue(self, aid: str, values: dict, outputs: Optional[dict], first: bool) -> None:
        """Put a snapshot back (newer values, if any, win over it), at the front when `first`. Lock held."""
        merged = dict(values)
        merged.update(self._pending.pop(aid, {}))
        self._pending = {aid: merged, **self._pending} if first else {**self._pending, aid: merged}
        if outputs:
            outs = dict(outputs)
            outs.update(self._pending_outputs.get(aid, {}))
            self._pending_outputs[aid] = outs

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self.settle)
            self._wake.clear()
            time.sleep(self.settle)  # let a burst settle into one snapshot
            while not self._stop.is_set():
                aid, values, wait = self._next()
                if aid is None:
                    if wait > 0 and self._pending:
                        time.sleep(wait)
                        continue
                    break
                with self._lock:
                    outputs = self._pending_outputs.pop(aid, None)
                token_wait = self._take_token()
                if token_wait > 0:
                    with self._lock:  # put it back, wait for the budget
                        self._requeue(aid, values, outputs, first=True)
                    time.sleep(min(token_wait, 30.0))
                    continue
                ok = False
                try:
                    # A v3 program's card outputs ride as `outputs` (functions.yaml 1.11.2); a v1 snapshot is posted
                    # exactly as before.
                    ok = bool(self.http.report_variables(aid, values, outputs=outputs) if outputs
                              else self.http.report_variables(aid, values))
                except Exception as e:  # noqa: BLE001
                    logger.debug("variables http: %s", e)
                self._last_sent[aid] = time.monotonic()
                if ok:
                    self.posted += 1
                else:
                    self.failed += 1
                    rate_limited = getattr(self.http, "last_status", None) == 429
                    with self._lock:
                        # newer values (if any) win over the failed snapshot
                        self._requeue(aid, values, outputs, first=True)
                        self._idle.clear()
                    if rate_limited:
                        logger.warning("ingress rate-limited (429): card snapshots paused for %.0f s", self.RATE_LIMITED_BACKOFF)
                        time.sleep(self.RATE_LIMITED_BACKOFF)
                    else:
                        time.sleep(self.retry_after)


class CloudSink(EventSink):
    """Runtime → cloud. MQTT is the fast path when connected; HTTPS is the durable
    twin for acks and alerts; readings ride the existing buffer + live channel.
    Card variables go to the ingress on every change (see _VariableUploader).
    Nothing here waits on the network on the caller's thread — a runtime pass (see the module docstring)."""

    def __init__(self, mqtt_client, http_reporter, telemetry_buffer=None, firmware_version: str = "",
                 http_async: bool = True, min_interval: float = 1.0):
        self.mqtt = mqtt_client
        self.http = http_reporter
        self.buffer = telemetry_buffer
        self.firmware_version = firmware_version
        self.last_ack: Optional[dict] = None
        self.uploader: Optional[_VariableUploader] = _VariableUploader(http_reporter, min_interval=min_interval) if http_async else None
        # programAck and alert, both legs, in order, off the runtime thread (agent 0.4.0 review).
        self.outbox: Optional[_Outbox] = _Outbox() if http_async else None

    def variables(self, asset_id: str, values: dict[str, Any]) -> None:
        try:
            self.mqtt.publish_variables(asset_id, values)
        except Exception as e:  # noqa: BLE001
            logger.debug("variables mqtt: %s", e)
        if self.uploader is not None:
            self.uploader.submit(asset_id, values)
            return
        try:
            self.http.report_variables(asset_id, values)
        except Exception as e:  # noqa: BLE001
            logger.debug("variables http: %s", e)

    def outputs(self, asset_id: str, levels: dict[str, Any]) -> None:
        """A v3 program's card outputs (contracts v1.15.1, functions.yaml 1.11.2 `variables.outputs`): over HTTPS
        through the same budgeted queue as a v1 card snapshot (no MQTT copy: nothing reads it)."""
        if self.uploader is not None:
            self.uploader.submit(asset_id, {}, outputs=levels)
            return
        try:
            self.http.report_variables(asset_id, {}, outputs=levels)
        except Exception as e:  # noqa: BLE001
            logger.debug("outputs http: %s", e)

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        ok = self.outbox.flush(timeout) if self.outbox is not None else True
        if self.uploader is not None:
            ok = self.uploader.flush(max(0.0, deadline - time.monotonic())) and ok
        return ok

    def close(self) -> None:
        if self.uploader is not None:
            self.uploader.stop()
        if self.outbox is not None:
            self.outbox.close(timeout=0.0)  # main.py flushed it first; a test's sink just stops

    def alert(self, text: str, severity: str, rule_id: Optional[str], asset_id: Optional[str] = None) -> None:
        logger.warning("ALERT [%s] %s (rule %s)", severity, text, rule_id)
        if self.outbox is not None:
            self.outbox.submit("alert", lambda: self._send_alert(text, severity, rule_id, asset_id))
            return
        self._send_alert(text, severity, rule_id, asset_id)

    def _send_alert(self, text: str, severity: str, rule_id: Optional[str], asset_id: Optional[str]) -> None:
        """The alert as 0.3.4 sent it from the pass: the MQTT copy, then the HTTPS post (the one the backend reads)."""
        try:
            self.mqtt.publish_alert(text, severity, rule_id, asset_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("alert mqtt: %s", e)
        try:
            self.http.report_alert(text, severity, rule_id, asset_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("alert http: %s", e)

    def event(self, type_: str, payload: dict, source: str = "program") -> None:
        try:
            self.mqtt.publish_event({"type": type_, "source": source or "program", "payload": payload,
                                     "sourceKey": payload.get("assetId") or payload.get("plotId") or ""})
        except Exception as e:  # noqa: BLE001
            logger.debug("event mqtt: %s", e)

    def log(self, text: str, asset_id: Optional[str] = None) -> None:
        logger.info("program%s: %s", f" [{asset_id}]" if asset_id else "", text)

    def reading(self, asset_id: str, source_key: str, value: Any, kind_uri: Optional[str], unit: Optional[str]) -> None:
        reading: dict = {"sourceKey": source_key, "value": value, "assetId": asset_id}
        if kind_uri:
            reading["kindUri"] = kind_uri
        if unit:
            reading["unit"] = unit
        if self.buffer is not None:
            try:
                self.buffer.append([reading], ts_ms=int(time.time() * 1000), ts_uncertain=False)
            except Exception as e:  # noqa: BLE001
                logger.debug("datalog buffer: %s", e)
        try:
            self.mqtt.publish_telemetry([reading])
        except Exception as e:  # noqa: BLE001
            logger.debug("datalog mqtt: %s", e)

    def program_ack(self, version: int, status: str, reason: Optional[str] = None) -> None:
        # The heartbeat's programStatus is the node's own state: it is known now, whenever the post goes out.
        self.last_ack = {"version": version, "status": status, "reason": reason}
        version = int(version)
        if self.outbox is not None:
            self.outbox.submit("programAck", lambda: self._send_program_ack(version, status, reason))
            return
        self._send_program_ack(version, status, reason)

    def _send_program_ack(self, version: int, status: str, reason: Optional[str]) -> None:
        """The ack as 0.3.4 sent it from the pass: the HTTPS post (the one the backend reads), then the MQTT copy."""
        try:
            self.http.report_program_ack(version, status, reason, self.firmware_version)
        except Exception as e:  # noqa: BLE001
            logger.debug("ack http: %s", e)
        try:
            self.mqtt.publish_program_ack(version, status, reason, self.firmware_version)
        except Exception as e:  # noqa: BLE001
            logger.debug("ack mqtt: %s", e)

    def post_program_ack(self, version: int, status: str, reason: Optional[str] = None) -> None:
        """ProgramSync's ``rejected`` (a pulled bundle that did not compile): the HTTPS post only, with no
        firmwareVersion and without touching ``last_ack`` — exactly as 0.3.4's ``ProgramSync`` posts it — but through
        the same ordered outbox as the runtime's acks, so the backend gets the node's acks in the order it made them
        (an ``applied`` still waiting in the outbox is never overtaken by a later ``rejected``)."""
        version = int(version)

        def send() -> None:
            self.http.report_program_ack(version, status, reason=reason)
        if self.outbox is not None:
            self.outbox.submit("programAck", send)
            return
        send()


def build_hardware_io(config, sensor_by_key: Optional[dict] = None, lines=None, analog=None):
    """HardwareIO gated on the node's configured BCM numbers, with the registered
    sensor drivers keyed by their pin label (= PortBinding.sourceKey). ``lines`` is the
    character-device path (ports.gpiochip.LineIO): ports that name a chip LABEL + line
    (contracts v1.9.0, ADR-024) go through it, on every Linux board."""
    from .io import HardwareIO
    gpios = {p.gpio_number for p in config.pins if p.gpio_number is not None}
    return HardwareIO(configured_gpios=gpios or None, sensor_by_key=sensor_by_key or {}, lines=lines, analog=analog)


def sensors_by_key(config) -> dict:
    """sourceKey → sensor instance, from the instances main.py registered per pin."""
    from ..command_executor import get_sensor_instance
    out: dict = {}
    for p in config.pins:
        if p.sensor_type and p.label:
            inst = get_sensor_instance(p)
            if inst is not None:
                out[p.label] = inst
    return out


def has_rtc() -> bool:
    return sys.platform == "linux" and (os.path.exists("/dev/rtc0") or os.path.exists("/dev/rtc"))


def capabilities_from_pins(config) -> dict:
    """iot-node.capabilities (R-5): counted from the configured pins by label and driver.
    Labels follow the sourceKey conventions of the default programs
    (device.valve*, sensor.flow*, sensor.mains*, sensor.current*)."""
    valves = relays = flows = mains = currents = 0
    for p in config.pins:
        label = (p.label or "").lower()
        if p.protocol == "gpio_output":
            relays += 1
            if "valve" in label:
                valves += 1
        elif p.protocol in ("gpio_input", "analog_input"):
            if "flow" in label:
                flows += 1
            elif "mains" in label:
                mains += 1
        if p.sensor_type == "current_acs758" or "current" in label:
            currents += 1
    return {
        "valveOutputs": valves,
        "flowInputs": flows,
        "relayOutputs": relays,
        "currentInputs": currents,
        "mainsSenseInputs": mains,
        "hasRtc": has_rtc(),
    }


def heartbeat_extra(runtime, config, sink: Optional[CloudSink] = None) -> dict:
    """The block-runtime fields the heartbeat carries.

    ``capabilities.blockLanguage`` is 3 (contracts v1.15.0): this agent runs bundle v3, and still runs a
    language-1 NAB bundle as agent 0.3.x does until the node is migrated — the cloud serves v3 only to a
    node that reports 3 AND has no unmigrated NAB (programs/README.md §7.2). While a v3 bundle runs, the
    status also carries its built-ins: ``safety``, ``valves``, ``guards``, ``held``, ``automation`` and the
    active instruction part per layout (functions.yaml 1.11.0 telemetryIngress)."""
    from .host import BLOCK_LANGUAGE
    caps = capabilities_from_pins(config)
    caps["blockLanguage"] = BLOCK_LANGUAGE
    extra: dict = {"capabilities": caps}
    loaded = getattr(runtime, "loaded", None)
    if loaded is None:
        loaded = runtime is not None and getattr(runtime, "nab", None) is not None
    if runtime is not None and loaded:
        extra["programVersion"] = runtime.version
        ack = sink.last_ack if sink else None
        extra["programStatus"] = (ack or {}).get("status", "applied")
        status = getattr(runtime, "status_extra", None)
        if callable(status):
            extra.update(status() or {})
    return extra
