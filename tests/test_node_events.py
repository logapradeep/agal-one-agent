"""Node events reach the backend over HTTPS (node_events.py, contracts v1.14.2).

A node's events — protection trips, the block runtime's, buffer_overflow — went out on the MQTT
status topic only, which nothing consumes, in a body the ingress answers 400: no node alert
reached the cloud. Every ``publish_event`` now also goes to ``NodeEvents``, which posts
``{type: "sensor_event", payload: {nodeUid, event, timestamp, eventId}}`` with the node's token,
in order, retrying with the same eventId. A fake reporter stands in for the ingress.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
from unittest.mock import MagicMock

import pytest
import yaml

from agal_one_agent import main as agent_main
from agal_one_agent.blocks.clock import SystemClock
from agal_one_agent.blocks.cloud import CloudSink
from agal_one_agent.config import MqttConfig
from agal_one_agent.http_reporter import HttpReporter
from agal_one_agent.mqtt_client import AgalOneMqttClient
from agal_one_agent.node_events import NodeEvents, wire_node_events
from agal_one_agent.telemetry_buffer import TelemetryBatchConfig, TelemetryBuffer

FAST = dict(backoff_s=(0.01, 0.02, 0.04), rate_limited_s=0.05, budget_per_hour=0, clock_ok=lambda: True)
EVENT_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")  # functions.yaml SensorEventPayload.eventId
DRY_RUN = {"type": "dry_run_protection_triggered", "source": "protection", "sourceKey": "pump1.current",
           "payload": {"baselineCurrentA": 6.1, "observedCurrentA": 1.2, "cutPinLabel": "pump1.relay"}}


class FakeReporter:
    """Stands in for HttpReporter.report_sensor_event. ``answer(event_type, try_no)`` gives the HTTP
    status (None: the ingress was not reached)."""

    def __init__(self, answer=None, gate: threading.Event | None = None):
        self.answer = answer or (lambda type_, n: 200)
        self.gate = gate
        self.tries: list[dict] = []
        self.delivered: list[dict] = []
        self._lock = threading.Lock()

    def report_sensor_event(self, event, *, event_id, timestamp, timestamp_uncertain=False):
        rec = {"event": event, "eventId": event_id, "timestamp": timestamp, "uncertain": timestamp_uncertain}
        with self._lock:
            n = sum(1 for t in self.tries if t["eventId"] == event_id) + 1
            self.tries.append(rec)
        if self.gate is not None:
            assert self.gate.wait(10), "the test never released the post"
        status = self.answer(event["type"], n)
        if status == 200:
            with self._lock:
                self.delivered.append(rec)
        return status


def _mqtt(connected=True) -> AgalOneMqttClient:
    client = AgalOneMqttClient(MqttConfig(broker="broker.example", username="shared-device-login",
                                          status_topic="agal/node-1/status", commands_topic="agal/node-1/commands"))
    client._client = MagicMock()
    client._connected = connected
    return client


class _Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ------------------------------------------------------------------ the wire

def test_an_event_is_posted_as_a_sensor_event_with_the_nodes_own_uid_and_token(monkeypatch):
    bodies = []

    def urlopen(req, timeout):
        bodies.append((req.full_url, req.get_header("Authorization"), json.loads(req.data)))
        return _Response(200)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    rep = HttpReporter("node-1", auth_token="tok", base_url="https://ingress.example/telemetryIngress")
    mqtt = _mqtt()
    events = wire_node_events(mqtt, rep, offline=False, clock_ok=lambda: True)
    before = int(time.time() * 1000)
    mqtt.publish_event(DRY_RUN)
    assert events.wait_idle(5)
    events.close()
    assert len(bodies) == 1
    url, auth, body = bodies[0]
    assert url == "https://ingress.example/telemetryIngress" and auth == "Bearer tok"
    assert body["type"] == "sensor_event" and set(body) == {"type", "payload"}  # nothing beside type (400)
    p = body["payload"]
    assert set(p) == {"nodeUid", "event", "timestamp", "eventId"}
    assert p["nodeUid"] == "node-1"  # config.node.uid — never the shared broker login
    assert p["event"] == DRY_RUN
    assert EVENT_ID.fullmatch(p["eventId"])
    assert before <= p["timestamp"] <= int(time.time() * 1000)


def test_the_mqtt_copy_is_kept_and_mqtt_down_does_not_stop_the_https_one():
    rep = FakeReporter()
    up, down = _mqtt(connected=True), _mqtt(connected=False)
    e_up, e_down = NodeEvents(rep, **FAST), NodeEvents(rep, **FAST)
    up.set_event_sink(e_up.submit)
    down.set_event_sink(e_down.submit)
    up.publish_event(DRY_RUN)
    down.publish_event(DRY_RUN)
    assert e_up.wait_idle(5) and e_down.wait_idle(5)
    assert len(rep.delivered) == 2
    assert up._client.publish.call_count == 1 and down._client.publish.call_count == 0
    topic, wire = up._client.publish.call_args.args[:2]
    assert topic == "agal/node-1/status" and json.loads(wire)["event"] == DRY_RUN


def test_an_untrusted_clock_says_so():
    rep = FakeReporter()
    events = NodeEvents(rep, **{**FAST, "clock_ok": lambda: False})
    events.submit(DRY_RUN)
    assert events.wait_idle(5)
    assert rep.delivered[0]["uncertain"] is True

    sent = []
    http = HttpReporter("node-1", auth_token="tok")
    http._post_status = lambda data: sent.append(data) or 200
    http.report_sensor_event(DRY_RUN, event_id="Ab3dE6gH9jK2mN5pQ8rS", timestamp=5, timestamp_uncertain=True)
    http.report_sensor_event(DRY_RUN, event_id="Ab3dE6gH9jK2mN5pQ8rT", timestamp=5)
    assert sent[0]["payload"]["timestampUncertain"] is True and "timestampUncertain" not in sent[1]["payload"]


def test_a_value_json_cannot_carry_becomes_null_or_text():
    rep = FakeReporter()
    events = NodeEvents(rep, **FAST)
    events.submit({"type": "flow_fault", "source": "program", "payload": {"flow": float("nan"), "at": object(),
                                                                          "inf": float("inf"), "ok": [1, 2.5]}})
    assert events.wait_idle(5)
    p = rep.delivered[0]["event"]["payload"]
    assert p["flow"] is None and p["inf"] is None and isinstance(p["at"], str) and p["ok"] == [1, 2.5]
    json.dumps(rep.delivered[0]["event"], allow_nan=False)


# ------------------------------------------------------------------ order, retries, bounds

def test_an_event_not_delivered_is_tried_again_with_the_same_event_id_and_the_others_wait():
    answers = {"dry_run_protection_triggered": [None, 503, 200]}
    rep = FakeReporter(lambda t, n: answers.get(t, [200])[min(n, len(answers.get(t, [200]))) - 1])
    events = NodeEvents(rep, **FAST)
    first = events.submit(DRY_RUN)
    second = events.submit({"type": "low_water_level_warning", "source": "protection", "sourceKey": "well1.level"})
    assert events.wait_idle(5)
    tried = [t["eventId"] for t in rep.tries]
    assert tried == [first, first, first, second]  # the same id each time; the second waited its turn
    assert [d["eventId"] for d in rep.delivered] == [first, second]
    assert events.posted == 2 and events.given_up == 0


def test_an_event_the_ingress_refuses_is_given_up_and_the_next_one_goes(caplog):
    rep = FakeReporter(lambda t, n: 400 if t == "buffer_overflow" else 200)
    events = NodeEvents(rep, **FAST)
    with caplog.at_level(logging.ERROR, logger="agal_one_agent.node_events"):
        events.submit({"type": "buffer_overflow", "source": "node", "sourceKey": "telemetry.buffer"})
        events.submit(DRY_RUN)
        assert events.wait_idle(5)
    assert [t["event"]["type"] for t in rep.tries] == ["buffer_overflow", "dry_run_protection_triggered"]
    assert events.given_up == 1 and events.posted == 1
    assert any("given up" in r.getMessage() and "HTTP 400" in r.getMessage() for r in caplog.records)


def test_rate_limited_events_wait_the_rate_limit_out():
    rep = FakeReporter(lambda t, n: 429 if n == 1 else 200)
    events = NodeEvents(rep, **{**FAST, "rate_limited_s": 0.2})
    t0 = time.monotonic()
    events.submit(DRY_RUN)
    assert events.wait_idle(5)
    assert time.monotonic() - t0 >= 0.2 and events.posted == 1


def test_an_event_never_delivered_is_given_up_after_its_deadline(caplog):
    rep = FakeReporter(lambda t, n: None)
    events = NodeEvents(rep, **{**FAST, "deadline_s": 0.1})
    with caplog.at_level(logging.ERROR, logger="agal_one_agent.node_events"):
        events.submit(DRY_RUN)
        assert events.wait_idle(5)
    assert events.given_up == 1 and len(rep.tries) >= 2
    assert any("not delivered in" in r.getMessage() for r in caplog.records)


def test_the_queue_is_bounded_and_protection_events_are_kept_longest():
    gate = threading.Event()
    rep = FakeReporter(gate=gate)
    events = NodeEvents(rep, **{**FAST, "cap": 3})
    events.submit({"type": "plot_run_started", "source": "program"})  # taken by the worker, held at the gate
    deadline = time.monotonic() + 5
    while not rep.tries and time.monotonic() < deadline:
        time.sleep(0.01)
    events.submit(DRY_RUN)
    events.submit({"type": "command_applied", "source": "program"})
    events.submit({"type": "schedule_started", "source": "program"})
    events.submit({"type": "low_water_level_warning", "source": "protection"})  # full: a program event leaves
    gate.set()
    assert events.wait_idle(5)
    assert [d["event"]["type"] for d in rep.delivered] == [
        "plot_run_started", "dry_run_protection_triggered", "schedule_started", "low_water_level_warning"]
    assert events.given_up == 1


def test_the_budget_spaces_events_out_instead_of_dropping_them():
    rep = FakeReporter()
    events = NodeEvents(rep, **{**FAST, "budget_per_hour": 36000.0, "burst": 1})  # 10 a second
    t0 = time.monotonic()
    for _ in range(3):
        events.submit(DRY_RUN)
    assert events.wait_idle(5)
    assert len(rep.delivered) == 3 and time.monotonic() - t0 >= 0.18


def test_a_clean_stop_gives_each_waiting_event_one_more_try():
    rep = FakeReporter(lambda t, n: None if n == 1 else 200)
    events = NodeEvents(rep, **{**FAST, "backoff_s": (30.0,)})
    events.submit(DRY_RUN)
    deadline = time.monotonic() + 5
    while not rep.tries and time.monotonic() < deadline:
        time.sleep(0.01)
    assert events.close(timeout=3.0)
    assert len(rep.delivered) == 1


def test_submit_never_waits_on_the_network():
    gate = threading.Event()
    events = NodeEvents(FakeReporter(gate=gate), **FAST)
    t0 = time.monotonic()
    for _ in range(5):
        events.submit(DRY_RUN)
    assert time.monotonic() - t0 < 0.5
    gate.set()
    assert events.wait_idle(5)


def test_report_sensor_event_tells_a_refusal_from_an_unreachable_ingress(monkeypatch):
    rep = HttpReporter("node-1", auth_token="tok", base_url="https://ingress.example/telemetryIngress")
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout: (_ for _ in ()).throw(urllib.error.HTTPError(req.full_url, 401, "x", {}, None)))
    assert rep.report_sensor_event(DRY_RUN, event_id="Ab3dE6gH9jK2mN5pQ8rS", timestamp=1) == 401
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout: (_ for _ in ()).throw(urllib.error.URLError("no route")))
    assert rep.report_sensor_event(DRY_RUN, event_id="Ab3dE6gH9jK2mN5pQ8rS", timestamp=1) is None


# ------------------------------------------------------------------ every source of events

def _wired():
    rep = FakeReporter()
    mqtt = _mqtt(connected=False)
    events = NodeEvents(rep, **FAST)
    mqtt.set_event_sink(events.submit)
    return rep, mqtt, events


def test_the_block_runtimes_events_are_posted():
    rep, mqtt, events = _wired()
    sink = CloudSink(mqtt, MagicMock(), http_async=False)
    sink.event("plot_run_started", {"plotId": "plot_1", "durationMinutes": 20, "source": "app"})
    sink.event("output_released", {"assetId": "asset_pump", "sourceKey": "device.power", "level": "off"})
    assert events.wait_idle(5)
    assert [(d["event"]["type"], d["event"]["source"], d["event"]["sourceKey"]) for d in rep.delivered] == [
        ("plot_run_started", "program", "plot_1"), ("output_released", "program", "asset_pump")]


def test_buffer_overflow_is_posted():
    rep, mqtt, events = _wired()
    buf = TelemetryBuffer(db_path=":memory:", config=TelemetryBatchConfig(max_rows=3, retention_hours=999),
                          on_overflow=mqtt.publish_event)
    for i in range(6):
        buf.append([{"sourceKey": f"p{i}", "value": float(i)}])
    assert events.wait_idle(5)
    assert [d["event"]["type"] for d in rep.delivered] == ["buffer_overflow"]


def test_a_protection_trip_is_posted():
    from agal_one_agent.sensors.protection import ProtectionMonitor
    rep, mqtt, events = _wired()
    monitor = ProtectionMonitor(current_sensors=[], protection_configs={}, ultrasonic_sensors=[],
                                relay_writer=lambda label, v: None, event_publisher=mqtt.publish_event)
    monitor._event_publisher(DRY_RUN)  # what _trigger_cutoff calls
    assert events.wait_idle(5)
    assert rep.delivered[0]["event"] == DRY_RUN


def test_an_event_sink_failure_never_fails_the_caller(caplog):
    mqtt = _mqtt(connected=False)
    mqtt.set_event_sink(lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level(logging.ERROR, logger="agal_one_agent.mqtt_client"):
        mqtt.publish_event(DRY_RUN)  # the protection thread goes on
    assert any("not queued" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------ the wiring in main()

class _WiredHere(Exception):
    pass


@pytest.mark.parametrize("offline", [False, True], ids=["cloud", "offline"])
def test_main_wires_the_node_events(tmp_path, monkeypatch, offline):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"node": {"uid": "node-1", "name": "Bench", "auth_token": "tok"},
                               "mqtt": {"broker": "broker.example", "username": "shared-device-login"},
                               "blocks": {"state_dir": str(tmp_path / "state")},
                               "telemetry": {"ingress_url": "https://ingress.example/telemetryIngress"}}))
    seen = {}

    def wire(mqtt_client, reporter, *, offline, clock_ok=None):
        seen.update(mqtt=mqtt_client, reporter=reporter, offline=offline, clock_ok=clock_ok)
        raise _WiredHere

    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(agent_main, "wire_node_events", wire)
    monkeypatch.setattr("sys.argv", ["agal-one-agent", "--config", str(path)] + (["--offline"] if offline else []))
    with pytest.raises(_WiredHere):
        agent_main.main()
    assert isinstance(seen["mqtt"], AgalOneMqttClient)
    assert isinstance(seen["reporter"], HttpReporter) and seen["reporter"].node_uid == "node-1"
    assert seen["reporter"].auth_token == "tok"
    assert seen["offline"] is offline
    assert isinstance(seen["clock_ok"].__self__, SystemClock)


def test_offline_wires_no_event_sink():
    mqtt = _mqtt()
    assert wire_node_events(mqtt, MagicMock(), offline=True) is None
    assert mqtt._event_sink is None
