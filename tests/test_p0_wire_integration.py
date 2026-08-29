"""Wire-shape + integration tests for the P0 durable path and LoRa ingress types.

Pins the shapes the backend agent's ingress handlers key off:
  * `telemetry_batch` HTTP envelope (ADR-013 §6.2)
  * `lora_survey_sample` / `lora_child_status` MQTT envelopes (ADR-011 §10.7)
And that the publisher appends to the durable buffer before publishing live.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from agal_one_agent.config import (
    AgentConfig, NodeConfig, MqttConfig, TelemetryConfig, BoardConfig,
)
from agal_one_agent.mqtt_client import AgalOneMqttClient
from agal_one_agent.http_reporter import HttpReporter
from agal_one_agent.telemetry_buffer import TelemetryBuffer, TelemetryBatchConfig
from agal_one_agent.telemetry_publisher import TelemetryPublisher


# ----- http_reporter telemetry_batch envelope -------------------------------


def test_report_telemetry_batch_envelope(monkeypatch):
    posted = {}

    def fake_post(self, data):
        posted.update(data)
        return True

    monkeypatch.setattr(HttpReporter, "_post", fake_post)
    rep = HttpReporter("node-9", auth_token="tok")
    ok = rep.report_telemetry_batch(
        "batch-abc",
        [{"sourceKey": "pump.current", "value": 4.8, "tsMs": 111, "seq": 1}],
        boot_session_id="boot-xyz",
    )
    assert ok is True
    assert posted["type"] == "telemetry_batch"
    assert posted["payload"]["nodeUid"] == "node-9"
    assert posted["payload"]["batchId"] == "batch-abc"
    assert posted["payload"]["bootSessionId"] == "boot-xyz"
    assert posted["payload"]["readings"][0]["seq"] == 1


def test_report_telemetry_batch_returns_false_on_failure(monkeypatch):
    monkeypatch.setattr(HttpReporter, "_post", lambda self, data: False)
    rep = HttpReporter("n", auth_token="t")
    assert rep.report_telemetry_batch("b", [{"sourceKey": "x", "value": 1}]) is False


# ----- lora ingress envelopes ------------------------------------------------


def _connected_client() -> AgalOneMqttClient:
    c = AgalOneMqttClient(MqttConfig(broker="b", username="node-1",
                                     status_topic="agal/node-1/status"))
    c._client = MagicMock()
    c._connected = True
    return c


def test_lora_survey_sample_envelope():
    c = _connected_client()
    c.publish_lora_survey_sample({"childShortId": 204, "rssi": -100, "snr": 6,
                                  "sf": 9, "quality": "excellent"})
    topic = c._client.publish.call_args.args[0]
    payload = json.loads(c._client.publish.call_args.args[1])
    assert topic == "agal/node-1/status"
    assert payload["type"] == "lora_survey_sample"
    assert payload["nodeUid"] == "node-1"
    assert payload["sample"]["childShortId"] == 204
    assert payload["sample"]["quality"] == "excellent"


def test_lora_child_status_envelope():
    c = _connected_client()
    c.publish_lora_child_status({"childShortId": 5, "rssi": -110, "snr": -2,
                                 "quality": "ok", "batteryMv": 3900})
    payload = json.loads(c._client.publish.call_args.args[1])
    assert payload["type"] == "lora_child_status"
    assert payload["nodeUid"] == "node-1"
    assert payload["child"]["batteryMv"] == 3900


def test_lora_publishers_noop_when_disconnected():
    c = AgalOneMqttClient(MqttConfig(broker="b", username="n",
                                     status_topic="agal/n/status"))
    c._client = MagicMock()
    c._connected = False
    c.publish_lora_survey_sample({"childShortId": 1})
    c.publish_lora_child_status({"childShortId": 1})
    c._client.publish.assert_not_called()


# ----- publisher buffers FIRST, then publishes live --------------------------


def _min_config() -> AgentConfig:
    return AgentConfig(
        node=NodeConfig(uid="n", name="n", auth_token="t"),
        mqtt=MqttConfig(broker="b", username="n"),
        telemetry=TelemetryConfig(interval_seconds=10),
        board=BoardConfig(),
        pins=[],
    )


def test_publisher_appends_to_buffer_before_live_publish():
    buf = TelemetryBuffer(db_path=":memory:", config=TelemetryBatchConfig())
    mqtt = MagicMock()
    pub = TelemetryPublisher(_min_config(), mqtt, buffer=buf)
    # Stub the input read so the cycle has deterministic readings + no timers.
    pub._read_all_inputs = lambda: [{"sourceKey": "pump.current", "value": 4.8}]
    pub._schedule_next = lambda: None      # don't arm a real timer
    pub._publish_cycle()
    # Durable path captured it...
    assert buf.pending_count() == 1
    # ...and the live channel published it.
    mqtt.publish_telemetry.assert_called_once()


def test_publisher_marks_uncertain_clock_when_probe_false():
    buf = TelemetryBuffer(db_path=":memory:", config=TelemetryBatchConfig())
    pub = TelemetryPublisher(_min_config(), MagicMock(), buffer=buf,
                             clock_ok=lambda: False)
    pub._read_all_inputs = lambda: [{"sourceKey": "s", "value": 1.0}]
    pub._schedule_next = lambda: None
    pub._publish_cycle()
    r = buf.fetch_batch().readings[0]
    assert r["tsUncertain"] is True


def test_publisher_live_only_without_buffer():
    mqtt = MagicMock()
    pub = TelemetryPublisher(_min_config(), mqtt, buffer=None)
    pub._read_all_inputs = lambda: [{"sourceKey": "s", "value": 1.0}]
    pub._schedule_next = lambda: None
    pub._publish_cycle()   # must not raise with no buffer
    mqtt.publish_telemetry.assert_called_once()
