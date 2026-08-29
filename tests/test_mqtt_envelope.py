"""Wire-envelope tests for AgalOneMqttClient publishers.

Closes BENCH_TEST_REPORT_2026-05-31.md §3.2 — the bug where publish_telemetry
and publish_status emitted bare {nodeUid, ...} payloads instead of the
canonical {type, payload: {...}} envelope the cloud telemetryIngress webhook
expects (cf. agal-one/backend/functions/src/modules/mqtt/telemetryIngress.ts
line 120 — `const {type, payload} = req.body; if (!type || !payload) → 400`).

The tests pin the wire shape so a future refactor can't silently regress it.
No paho broker required — we mock the underlying client and assert on the
JSON that gets handed to ``client.publish(topic, payload, qos=…)``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agal_one_agent.config import MqttConfig
from agal_one_agent.mqtt_client import AgalOneMqttClient


@pytest.fixture
def mqtt_client() -> AgalOneMqttClient:
    """A AgalOneMqttClient with the underlying paho client mocked + 'connected'."""
    config = MqttConfig(
        broker="broker.example",
        port=8883,
        tls=True,
        username="node-test",
        password="not-a-real-password",
        commands_topic="agal/node-test/commands",
        telemetry_topic="agal/node-test/telemetry",
        status_topic="agal/node-test/status",
    )
    client = AgalOneMqttClient(config)
    # Inject a mock paho client + mark connected so the early-return guard passes
    client._client = MagicMock()
    client._connected = True
    return client


def _published(client: AgalOneMqttClient) -> tuple[str, dict, dict]:
    """Return (topic, parsed-JSON, publish-kwargs) from the most recent publish."""
    assert client._client.publish.called, "no message was published"
    call = client._client.publish.call_args
    topic = call.args[0]
    raw_payload = call.args[1] if len(call.args) > 1 else call.kwargs["payload"]
    parsed = json.loads(raw_payload)
    return topic, parsed, call.kwargs


# ----- publish_telemetry --------------------------------------------------


def test_publish_telemetry_uses_type_payload_envelope(mqtt_client):
    readings = [
        {"sourceKey": "pump1.current", "kind": "current", "value": 4.82, "unit": "A"},
        {"sourceKey": "well1.level", "kind": "water_level_depth", "value": 2.45, "unit": "m"},
    ]
    mqtt_client.publish_telemetry(readings)

    topic, payload, _ = _published(mqtt_client)
    assert topic == "agal/node-test/telemetry"
    assert payload["type"] == "telemetry", (
        "publish_telemetry MUST emit a top-level `type` for the cloud "
        "telemetryIngress switch — see BENCH_TEST_REPORT §3.2"
    )
    assert "payload" in payload, "cloud function destructures req.body.payload"
    assert payload["payload"]["nodeUid"] == "node-test"
    assert payload["payload"]["readings"] == readings
    assert isinstance(payload["payload"]["timestamp"], int)


def test_publish_telemetry_noop_when_disconnected(mqtt_client):
    mqtt_client._connected = False
    mqtt_client.publish_telemetry([{"sourceKey": "x", "value": 1}])
    mqtt_client._client.publish.assert_not_called()


# ----- publish_status -----------------------------------------------------


def test_publish_status_uses_type_payload_envelope(mqtt_client):
    mqtt_client.publish_status(online=True, uptime=864000, firmware_version="0.1.5")

    topic, payload, _ = _published(mqtt_client)
    assert topic == "agal/node-test/status"
    assert payload["type"] == "status"
    assert payload["payload"]["nodeUid"] == "node-test"
    assert payload["payload"]["online"] is True
    assert payload["payload"]["uptime"] == 864000
    assert payload["payload"]["firmwareVersion"] == "0.1.5"


def test_publish_status_noop_when_disconnected(mqtt_client):
    mqtt_client._connected = False
    mqtt_client.publish_status(online=False, uptime=0)
    mqtt_client._client.publish.assert_not_called()


# ----- publish_command_ack (already-wrapped path; regression guard) -------


def test_publish_command_ack_uses_type_payload_envelope(mqtt_client):
    mqtt_client.publish_command_ack(
        command_id="cmd-xyz",
        status="completed",
        applied_value=1,
    )
    topic, payload, _ = _published(mqtt_client)
    assert topic == "agal/node-test/status"
    assert payload["type"] == "commandAck"
    assert payload["payload"]["commandId"] == "cmd-xyz"
    assert payload["payload"]["status"] == "completed"
    assert payload["payload"]["appliedValue"] == 1
    assert payload["payload"]["nodeUid"] == "node-test"


# ----- publish_event (flat shape — cloud reads req.body directly) ---------


def test_publish_event_uses_flat_sensor_event_shape(mqtt_client):
    """Sensor events use the flat shape because the cloud handler is
    `handleSensorEvent(db, req.body as SensorEventPayload)` — it reads the
    whole body, not body.payload. This test pins the contract so we don't
    accidentally wrap it into `{type, payload}` and break the cloud handler."""
    mqtt_client.publish_event({
        "type": "dry_run_protection_triggered",
        "source": "protection",
        "sourceKey": "pump1.current",
        "payload": {"baselineCurrentA": 5.10, "observedCurrentA": 0.42},
    })
    topic, payload, _ = _published(mqtt_client)
    assert topic == "agal/node-test/status"
    assert payload["type"] == "sensor_event"      # outer envelope type
    assert payload["nodeUid"] == "node-test"      # FLAT — not nested under .payload
    assert payload["event"]["type"] == "dry_run_protection_triggered"
    assert "payload" not in payload, (
        "publish_event must stay flat — handleSensorEvent in the cloud reads "
        "req.body as SensorEventPayload, not req.body.payload"
    )
