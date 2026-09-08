"""Cloud-facing glue for the block runtime: the event sink that fans out to
MQTT / HTTPS / the durable telemetry buffer, the hardware I/O factory, and the
capabilities the heartbeat reports (ADR-017 §5; contracts v1.5.0 telemetryIngress
``programAck`` / ``variables`` / ``alert`` messages; iot-node.capabilities).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any, Optional

from .runtime import EventSink

logger = logging.getLogger(__name__)


class _VariableUploader:
    """Posts card variable snapshots to the ingress from its own thread — in
    order, one asset at a time, coalescing bursts (the latest snapshot of an
    asset wins while an earlier post is in flight), at most one post per asset
    per ``min_interval``. A failed post keeps the snapshot pending and retries
    after ``retry_after``. The runtime thread never waits on the network.

    Why HTTPS on every change: the instance has no MQTT consumer for the
    status topic (Cloud Functions cannot subscribe), so the ingress is the
    only path that reaches Firestore — and the phone."""

    def __init__(self, http, min_interval: float = 1.0, settle: float = 0.15, retry_after: float = 5.0):
        self.http = http
        self.min_interval = min_interval
        self.settle = settle
        self.retry_after = retry_after
        self._pending: dict[str, dict] = {}
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.posted = 0
        self.failed = 0

    def submit(self, asset_id: str, values: dict) -> None:
        with self._lock:
            merged = dict(self._pending.pop(asset_id, {}))
            merged.update(values)
            self._pending[asset_id] = merged
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
                ok = False
                try:
                    ok = bool(self.http.report_variables(aid, values))
                except Exception as e:  # noqa: BLE001
                    logger.debug("variables http: %s", e)
                self._last_sent[aid] = time.monotonic()
                if ok:
                    self.posted += 1
                else:
                    self.failed += 1
                    with self._lock:
                        # newer values (if any) win over the failed snapshot
                        merged = dict(values)
                        merged.update(self._pending.pop(aid, {}))
                        self._pending[aid] = merged
                        self._pending = {aid: self._pending.pop(aid), **self._pending}
                        self._idle.clear()
                    time.sleep(self.retry_after)


class CloudSink(EventSink):
    """Runtime → cloud. MQTT is the fast path when connected; HTTPS is the durable
    twin for acks and alerts; readings ride the existing buffer + live channel.
    Card variables go to the ingress on every change (see _VariableUploader)."""

    def __init__(self, mqtt_client, http_reporter, telemetry_buffer=None, firmware_version: str = "",
                 http_async: bool = True, min_interval: float = 1.0):
        self.mqtt = mqtt_client
        self.http = http_reporter
        self.buffer = telemetry_buffer
        self.firmware_version = firmware_version
        self.last_ack: Optional[dict] = None
        self.uploader: Optional[_VariableUploader] = _VariableUploader(http_reporter, min_interval=min_interval) if http_async else None

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

    def flush(self, timeout: float = 5.0) -> bool:
        return self.uploader.flush(timeout) if self.uploader is not None else True

    def close(self) -> None:
        if self.uploader is not None:
            self.uploader.stop()

    def alert(self, text: str, severity: str, rule_id: Optional[str], asset_id: Optional[str] = None) -> None:
        logger.warning("ALERT [%s] %s (rule %s)", severity, text, rule_id)
        try:
            self.mqtt.publish_alert(text, severity, rule_id, asset_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("alert mqtt: %s", e)
        try:
            self.http.report_alert(text, severity, rule_id, asset_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("alert http: %s", e)

    def event(self, type_: str, payload: dict) -> None:
        try:
            self.mqtt.publish_event({"type": type_, "source": "program", "payload": payload,
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
        self.last_ack = {"version": version, "status": status, "reason": reason}
        try:
            self.http.report_program_ack(version, status, reason, self.firmware_version)
        except Exception as e:  # noqa: BLE001
            logger.debug("ack http: %s", e)
        try:
            self.mqtt.publish_program_ack(version, status, reason, self.firmware_version)
        except Exception as e:  # noqa: BLE001
            logger.debug("ack mqtt: %s", e)


def build_hardware_io(config, sensor_by_key: Optional[dict] = None):
    """HardwareIO gated on the node's configured BCM numbers, with the registered
    sensor drivers keyed by their pin label (= PortBinding.sourceKey)."""
    from .io import HardwareIO
    gpios = {p.gpio_number for p in config.pins if p.gpio_number is not None}
    return HardwareIO(configured_gpios=gpios or None, sensor_by_key=sensor_by_key or {})


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
    """The block-runtime fields the heartbeat carries."""
    extra: dict = {"capabilities": capabilities_from_pins(config)}
    if runtime is not None and runtime.nab is not None:
        extra["programVersion"] = runtime.version
        ack = sink.last_ack if sink else None
        extra["programStatus"] = (ack or {}).get("status", "applied")
    return extra
