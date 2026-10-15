"""Command acks reach the backend over HTTPS (command_acks.py).

The backend reads a command's outcome only from telemetryIngress (``commandAck`` →
handleCommandAck); nothing consumes the MQTT status topic, so with MQTT-only acks every
/assetCommands doc ended ``timeout`` although the node switched. Every ack now also goes, from
``AgalOneMqttClient.publish_command_ack``, to ``CommandAcks``, which posts it through
``HttpReporter.report_command_ack`` from its own thread. A fake reporter stands in for the
ingress: no network, no broker.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
from unittest.mock import MagicMock

import pytest
import yaml

from agal_one_agent import command_executor
from agal_one_agent import main as agent_main
from agal_one_agent.command_acks import CommandAcks, wire_command_acks
from agal_one_agent.config import AgentConfig, MqttConfig
from agal_one_agent.http_reporter import HttpReporter
from agal_one_agent.mqtt_client import AgalOneMqttClient
from agal_one_agent.ports.links import handle_switch

FAST = dict(coalesce_s=0.0, backoff_s=(0.01, 0.02, 0.04), rate_limited_s=0.05, final_deadline_s=5.0)


class FakeReporter:
    """Stands in for HttpReporter.report_command_ack. ``answer(command_id, status, try_no)``
    gives the HTTP status (None: the ingress was not reached); ``gate`` holds every post until set."""

    def __init__(self, answer=None, gate: threading.Event | None = None):
        self.answer = answer or (lambda command_id, status, n: 200)
        self.gate = gate
        self.tries: list[tuple[str, str, float]] = []
        self.posts: list[tuple[str, str, object, object]] = []
        self.in_post = threading.Event()
        self._lock = threading.Lock()

    def report_command_ack(self, command_id, status, applied_value=None, error=None):
        with self._lock:
            n = sum(1 for c, s, _ in self.tries if (c, s) == (command_id, status)) + 1
            self.tries.append((command_id, status, time.monotonic()))
        self.in_post.set()
        if self.gate is not None:
            assert self.gate.wait(10), "the test never released the post"
        answer = self.answer(command_id, status, n)
        if answer == 200:
            with self._lock:
                self.posts.append((command_id, status, applied_value, error))
        return answer

    def posted(self):
        return [(c, s) for c, s, _, _ in self.posts]


def _mqtt(connected: bool = True) -> AgalOneMqttClient:
    client = AgalOneMqttClient(MqttConfig(broker="broker.example", username="node-1",
                                          status_topic="agal/node-1/status",
                                          commands_topic="agal/node-1/commands"))
    client._client = MagicMock()
    client._connected = connected
    return client


def _mqtt_acks(client: AgalOneMqttClient) -> list[tuple[str, str]]:
    out = []
    for call in client._client.publish.call_args_list:
        body = json.loads(call.args[1])
        if body.get("type") == "commandAck":
            out.append((body["payload"]["commandId"], body["payload"]["status"]))
    return out


@pytest.fixture
def gpio_config(tmp_path, monkeypatch):
    """A node without a port table: one relay output on GPIO 17, with a fake GPIO handler."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"node": {"uid": "node-1", "name": "Bench", "auth_token": "tok"},
                               "mqtt": {"broker": "broker.example"}}))
    config = AgentConfig.from_yaml(str(path))
    config.update_pins([{"physical_pin": 11, "gpio_number": 17, "protocol": "gpio_output",
                         "label": "pump"}], str(path))
    handler = MagicMock()
    handler.write.side_effect = lambda pin, value: 1 if value else 0
    monkeypatch.setitem(command_executor._handlers, "gpio_output", handler)
    return config, handler


def _set_power(command_id="cmd-1", value=True):
    return {"commandId": command_id, "type": "setPower", "gpioNumber": 17, "value": value,
            "assetId": "pump-1", "sourceKey": "device.power"}


# ------------------------------------------------------------- the bug itself

@pytest.mark.parametrize("connected", [True, False], ids=["mqtt-up", "mqtt-down"])
def test_a_setpower_completed_ack_is_posted_over_https(gpio_config, connected):
    config, handler = gpio_config
    reporter = FakeReporter()
    acks = CommandAcks(reporter, **FAST)
    mqtt = _mqtt(connected)
    mqtt.set_ack_sink(acks.submit)

    command_executor.execute(config, mqtt, _set_power())

    assert acks.wait_idle(5)
    handler.write.assert_called_once()
    # acknowledged → executing → completed: the backend gets the completed ack with the value
    # applied; executing after acknowledged stays on MQTT, acknowledged is superseded or posted.
    assert reporter.posts[-1] == ("cmd-1", "completed", 1, None)
    assert ("cmd-1", "executing") not in reporter.posted()
    assert set(reporter.posted()) <= {("cmd-1", "acknowledged"), ("cmd-1", "completed")}
    # The MQTT publish stays when connected (harmless; no consumer today).
    assert _mqtt_acks(mqtt) == ([("cmd-1", "acknowledged"), ("cmd-1", "executing"), ("cmd-1", "completed")]
                                if connected else [])


def test_a_switch_through_the_running_program_posts_its_completed_ack():
    class Runtime:  # a program that owns pump-1's device.power
        nab = object()
        lock = threading.RLock()

        def owns_port(self, asset_id, source_key):
            return asset_id == "pump-1"

        def apply_command(self, command):
            return {"accepted": True}

    class Guard:
        def check_switch(self, command, gpio=None):
            return None

    reporter = FakeReporter()
    acks = CommandAcks(reporter, **FAST)
    mqtt = _mqtt()
    mqtt.set_ack_sink(acks.submit)

    handle_switch(_set_power("cmd-7"), Guard(), Runtime(), mqtt, legacy=lambda c: pytest.fail("legacy path"))

    assert acks.wait_idle(5)
    assert reporter.posts[-1] == ("cmd-7", "completed", True, None)


def test_a_refused_switch_posts_its_failed_ack_in_words():
    class Guard:
        def check_switch(self, command, gpio=None):
            return "refused: device.power of card pump-1 is not linked to a port of this node"

    reporter = FakeReporter()
    acks = CommandAcks(reporter, **FAST)
    mqtt = _mqtt()
    mqtt.set_ack_sink(acks.submit)

    handle_switch(_set_power("cmd-8"), Guard(), None, mqtt, legacy=lambda c: pytest.fail("legacy path"))

    assert acks.wait_idle(5)
    assert reporter.posts == [("cmd-8", "failed", None,
                               "refused: device.power of card pump-1 is not linked to a port of this node")]


# ---------------------------------------------------------------------- order

def test_the_acks_of_a_command_are_posted_in_the_order_they_were_made():
    gate = threading.Event()
    reporter = FakeReporter(gate=gate)
    acks = CommandAcks(reporter, **FAST)

    acks.submit("A", "acknowledged")
    assert reporter.in_post.wait(5)  # A acknowledged is on its way, held by the ingress
    acks.submit("B", "acknowledged")
    acks.submit("A", "executing")    # MQTT only after an acknowledged
    acks.submit("A", "completed", applied_value=1)
    acks.submit("B", "failed", error="no matching pin")
    gate.set()

    assert acks.wait_idle(5)
    posted = reporter.posted()
    for command in ("A", "B"):
        statuses = [s for c, s in posted if c == command]
        order = ["acknowledged", "executing", "completed", "failed"]
        assert statuses == sorted(statuses, key=order.index), f"{command} posted out of order: {statuses}"
    assert posted == [("A", "acknowledged"), ("A", "completed"), ("B", "failed")]  # B acknowledged superseded


def test_an_ack_is_never_posted_after_a_later_ack_of_its_command():
    first_try = threading.Event()
    release = threading.Event()

    def answer(command_id, status, n):
        if status == "acknowledged":
            first_try.set()
            release.wait(5)
            return 503
        return 200

    reporter = FakeReporter(answer=answer)
    acks = CommandAcks(reporter, **FAST)
    acks.submit("A", "acknowledged")
    assert first_try.wait(5)
    acks.submit("A", "completed", applied_value=0)  # arrives while acknowledged is failing
    release.set()

    assert acks.wait_idle(5)
    assert [(c, s) for c, s, _ in reporter.tries] == [("A", "acknowledged"), ("A", "completed")]
    assert reporter.posted() == [("A", "completed")]


def test_a_switch_that_completes_at_once_costs_one_post_and_a_slow_command_two():
    reporter = FakeReporter()
    acks = CommandAcks(reporter, coalesce_s=0.3, backoff_s=(0.01,), final_deadline_s=5.0)

    acks.submit("fast", "acknowledged")
    acks.submit("fast", "completed", applied_value=1)
    assert acks.wait_idle(5)
    assert reporter.posted() == [("fast", "completed")]

    acks.submit("slow", "acknowledged")  # a command whose final ack comes seconds later
    time.sleep(0.6)
    assert reporter.posted()[-1] == ("slow", "acknowledged"), "the hold ended: the backend learns the node has it"
    acks.submit("slow", "completed")
    assert acks.wait_idle(5)
    assert reporter.posted() == [("fast", "completed"), ("slow", "acknowledged"), ("slow", "completed")]


def test_executing_is_posted_only_when_no_acknowledged_came_before():
    reporter = FakeReporter()
    acks = CommandAcks(reporter, **FAST)
    acks.submit("c0", "executing")  # no acknowledged before it: it is the first the backend hears
    acks.submit("c1", "acknowledged")
    assert acks.wait_idle(5)
    acks.submit("c1", "executing")
    acks.submit("c1", "completed")
    assert acks.wait_idle(5)
    assert reporter.posted() == [("c0", "executing"), ("c1", "acknowledged"), ("c1", "completed")]


@pytest.mark.parametrize("command_id", [
    "boot_pump-1",                    # boot_reconciler: the node's own boot state
    "sync_1760000000000",             # publishPinConfigSync
    "sync_program_3_1760000000000",   # a program pull poke (its result: programAck)
    "sync_ports_2_1760000000000",     # a port table poke
    "scan_bus_i2c-1_1760000000000",   # scanBus (its result: bus_scan)
    "test_port_DIO1_1760000000000",   # testPort (its result: port_test)
])
def test_acks_the_backend_keeps_no_command_doc_for_stay_on_mqtt(command_id):
    # The backend answers these ids "untracked" and records nothing: a post would only spend
    # the node's ingress budget (a port test or a bus scan cost two posts for nothing).
    reporter = FakeReporter()
    acks = CommandAcks(reporter, **FAST)
    mqtt = _mqtt()
    mqtt.set_ack_sink(acks.submit)
    for status in ("acknowledged", "executing", "completed"):
        mqtt.publish_command_ack(command_id, status)
    assert acks.wait_idle(5)
    assert reporter.tries == []
    assert [s for _, s in _mqtt_acks(mqtt)] == ["acknowledged", "executing", "completed"]


@pytest.mark.parametrize("fails", [False, True], ids=["ota-ok", "ota-failed"])
def test_the_ota_receipt_and_its_failure_stay_on_mqtt(monkeypatch, fails):
    # A firmwareUpdate is a /nodeCommands doc: commandAck does not track it, and its outcome
    # comes back as firmware_update events. Posting its acks cost the backend a lookup and a
    # second's wait for a doc that is never there.
    from agal_one_agent import ota_updater

    def perform_update(version, command_id="", rollout_id="", node_uid="", **_archive_and_outputs):
        if fails:
            raise RuntimeError("pip install failed")
        return f"installing {version}"

    monkeypatch.setattr(ota_updater, "perform_update", perform_update)
    reporter = FakeReporter()
    acks = CommandAcks(reporter, **FAST)
    mqtt = _mqtt()
    mqtt.set_ack_sink(acks.submit)

    agent_main.handle_firmware_update({"commandId": "Xq3k9TzLmN2pRs7VbW1c", "type": "firmwareUpdate",
                                       "version": "v0.3.3"}, mqtt, "node-1")

    assert acks.wait_idle(5)
    assert reporter.tries == []
    assert _mqtt_acks(mqtt) == [("Xq3k9TzLmN2pRs7VbW1c", "executing")] + \
        ([("Xq3k9TzLmN2pRs7VbW1c", "failed")] if fails else [])


# ------------------------------------------------------- never block the node

def test_a_slow_ingress_never_blocks_the_command_or_the_mqtt_loop(gpio_config):
    config, handler = gpio_config
    gate = threading.Event()
    reporter = FakeReporter(gate=gate)
    acks = CommandAcks(reporter, **FAST)
    mqtt = _mqtt()
    mqtt.set_ack_sink(acks.submit)
    mqtt.set_command_handler(lambda command: command_executor.execute(config, mqtt, command))

    msg = MagicMock()
    started = time.monotonic()
    for i in range(3):  # three commands through the MQTT loop's own callback
        msg.payload = json.dumps(_set_power(f"cmd-{i}", value=i % 2 == 0)).encode("utf-8")
        mqtt._on_message(None, None, msg)
    elapsed = time.monotonic() - started

    assert reporter.in_post.wait(5), "the first ack is held by the slow ingress"
    assert elapsed < 0.5, f"commands waited {elapsed:.2f} s on the ingress"
    assert handler.write.call_count == 3, "every command was carried out while its ack was held"
    gate.set()
    assert acks.wait_idle(5)
    assert [(c, s) for c, s in reporter.posted() if s == "completed"] == \
        [("cmd-0", "completed"), ("cmd-1", "completed"), ("cmd-2", "completed")]


# ---------------------------------------------------------------------- retry

def test_a_final_ack_is_retried_until_the_ingress_answers():
    answers = {1: None, 2: None, 3: 503, 4: 429}
    reporter = FakeReporter(answer=lambda c, s, n: answers.get(n, 200))
    acks = CommandAcks(reporter, **FAST)
    acks.submit("cmd-1", "completed", applied_value=1)
    assert acks.wait_idle(5)
    assert [s for _, s, _ in reporter.tries] == ["completed"] * 5
    assert reporter.posts == [("cmd-1", "completed", 1, None)]
    assert acks.given_up == 0


@pytest.mark.parametrize("first_answer", [None, 408, 429, 500, 503])
def test_final_acks_keep_their_order_across_commands_when_one_is_tried_again(first_answer):
    # The user switches the pump ON (A), then OFF (B); the node does both. A's completed meets a
    # transient failure. B's completed must not be posted before A's retry: the backend would echo
    # "off" and then "on" — the card showing ON with the relay off.
    a_failing = threading.Event()
    b_queued = threading.Event()

    def answer(command_id, status, n):
        if (command_id, n) == ("A", 1):
            a_failing.set()
            assert b_queued.wait(5)
            return first_answer
        return 200

    reporter = FakeReporter(answer=answer)
    acks = CommandAcks(reporter, coalesce_s=0.0, backoff_s=(0.05,), rate_limited_s=0.05, final_deadline_s=5.0)
    acks.submit("A", "completed", applied_value=1)
    assert a_failing.wait(5)
    acks.submit("B", "completed", applied_value=0)
    b_queued.set()

    assert acks.wait_idle(5)
    assert [(c, s) for c, s, _ in reporter.tries] == [("A", "completed"), ("A", "completed"), ("B", "completed")]
    assert [(c, v) for c, _, v, _ in reporter.posts] == [("A", 1), ("B", 0)]


def test_an_intermediate_ack_tried_again_does_not_hold_another_commands_final_ack():
    # Only final acks keep their order across commands; a retried acknowledged waits alone.
    def answer(command_id, status, n):
        return 503 if (command_id, status) == ("A", "acknowledged") else 200

    reporter = FakeReporter(answer=answer)
    acks = CommandAcks(reporter, coalesce_s=0.0, backoff_s=(0.3,), final_deadline_s=5.0)
    acks.submit("A", "acknowledged")
    deadline = time.monotonic() + 5
    while not reporter.tries and time.monotonic() < deadline:
        time.sleep(0.005)
    time.sleep(0.02)  # A acknowledged is waiting out its 0.3 s back-off
    started = time.monotonic()
    acks.submit("B", "completed", applied_value=1)
    while ("B", "completed") not in reporter.posted() and time.monotonic() - started < 5:
        time.sleep(0.005)
    assert time.monotonic() - started < 0.2, "B's final ack waited on A's intermediate retry"
    assert acks.wait_idle(5)


def test_a_reporter_that_raises_is_a_failed_try_not_the_end_of_the_worker():
    def answer(command_id, status, n):
        if n == 1:
            raise RuntimeError("socket closed")
        return 200

    reporter = FakeReporter(answer=answer)
    acks = CommandAcks(reporter, **FAST)
    acks.submit("cmd-1", "completed", applied_value={1, 2})  # a value JSON cannot carry
    acks.submit("cmd-2", "failed", error=ValueError("bad pin"))
    assert acks.wait_idle(5)
    assert sorted(reporter.posts) == [("cmd-1", "completed", "{1, 2}", None), ("cmd-2", "failed", None, "bad pin")]


def test_a_final_ack_the_ingress_never_takes_is_given_up_after_its_deadline_with_a_log(caplog):
    reporter = FakeReporter(answer=lambda c, s, n: None)  # offline
    acks = CommandAcks(reporter, coalesce_s=0.0, backoff_s=(0.02, 0.05), final_deadline_s=0.3)
    with caplog.at_level(logging.ERROR, logger="agal_one_agent.command_acks"):
        acks.submit("cmd-1", "completed")
        assert acks.wait_idle(5)
    assert acks.given_up == 1
    assert len(reporter.tries) >= 3, "kept in the queue and retried while the ingress was unreachable"
    assert reporter.tries[-1][2] - reporter.tries[0][2] >= 0.25, "tried until the deadline"
    assert "command ack cmd-1 completed given up: not delivered in 0.3 s (the ingress was not reached)" in caplog.text


def test_an_intermediate_ack_gets_a_few_tries_only():
    reporter = FakeReporter(answer=lambda c, s, n: 503)
    acks = CommandAcks(reporter, **FAST)
    acks.submit("cmd-1", "acknowledged")
    assert acks.wait_idle(5)
    assert len(reporter.tries) == CommandAcks.INTERMEDIATE_TRIES
    assert acks.dropped == 1 and acks.given_up == 0


def test_an_ack_the_ingress_refuses_is_not_retried(caplog):
    reporter = FakeReporter(answer=lambda c, s, n: 400)
    acks = CommandAcks(reporter, **FAST)
    with caplog.at_level(logging.ERROR, logger="agal_one_agent.command_acks"):
        acks.submit("cmd-1", "failed", error="boom")
        assert acks.wait_idle(5)
    assert len(reporter.tries) == 1
    assert acks.given_up == 1
    assert "the ingress refused it (HTTP 400); not retried" in caplog.text


def test_when_the_ingress_is_rate_limited_every_ack_waits_out_the_back_off():
    reporter = FakeReporter(answer=lambda c, s, n: 429 if (c, n) == ("A", 1) else 200)
    acks = CommandAcks(reporter, coalesce_s=0.0, backoff_s=(0.01,), rate_limited_s=0.3, final_deadline_s=5.0)
    acks.submit("A", "completed")
    assert reporter.in_post.wait(5)
    time.sleep(0.05)
    acks.submit("B", "completed")
    assert acks.wait_idle(5)
    first, second, third = reporter.tries
    assert (first[0], second[0], third[0]) == ("A", "A", "B") or (first[0], second[0], third[0]) == ("A", "B", "A")
    assert min(second[2], third[2]) - first[2] >= 0.25, "nothing was posted during the 429 back-off"
    assert sorted(reporter.posted()) == [("A", "completed"), ("B", "completed")]


def test_a_clean_stop_gives_each_waiting_ack_one_more_try():
    reporter = FakeReporter(answer=lambda c, s, n: None if n == 1 else 200)
    acks = CommandAcks(reporter, coalesce_s=0.0, backoff_s=(30.0,), final_deadline_s=300.0)
    acks.submit("cmd-1", "completed")
    deadline = time.monotonic() + 5
    while len(reporter.tries) < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)  # now waiting out a 30 s back-off
    started = time.monotonic()
    assert acks.close(timeout=3.0)
    assert time.monotonic() - started < 2.0
    assert reporter.posted() == [("cmd-1", "completed")]
    acks.submit("cmd-2", "completed")  # after the stop: not posted
    time.sleep(0.05)
    assert [c for c, _, _ in reporter.tries] == ["cmd-1", "cmd-1"]


# --------------------------------------------------------------------- bounds

def test_the_queue_is_bounded_and_a_final_ack_is_never_dropped_for_an_intermediate_one(caplog):
    gate = threading.Event()
    reporter = FakeReporter(gate=gate)
    acks = CommandAcks(reporter, coalesce_s=0.0, max_waiting=4, hard_cap=8, backoff_s=(0.01,), final_deadline_s=5.0)

    acks.submit("c0", "acknowledged")
    assert reporter.in_post.wait(5)  # in flight, held
    for i in range(1, 10):
        acks.submit(f"c{i}", "acknowledged")
    assert acks.waiting == 4, "intermediate acks are not queued past max_waiting (backed up)"
    assert acks.dropped == 5

    with caplog.at_level(logging.ERROR, logger="agal_one_agent.command_acks"):
        for i in range(10):
            acks.submit(f"d{i}", "completed")
            assert acks.waiting <= 8
    assert acks.waiting == 8
    assert acks.dropped == 9, "every waiting intermediate ack made room for a final one first"
    assert acks.given_up == 2, "only at the hard cap, with nothing but final acks waiting"
    assert "command ack d0 completed given up: 8 final acks are waiting for the ingress" in caplog.text

    gate.set()
    assert acks.wait_idle(5)
    assert reporter.posted() == [("c0", "acknowledged")] + [(f"d{i}", "completed") for i in range(2, 10)]


# ------------------------------------------------------------- the HTTPS post

class _Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_report_command_ack_posts_the_ingress_envelope_with_the_node_token(monkeypatch):
    seen = {}

    def urlopen(req, timeout):
        seen["url"], seen["auth"], seen["body"] = req.full_url, req.get_header("Authorization"), json.loads(req.data)
        return _Response(200)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    rep = HttpReporter("node-1", auth_token="tok", base_url="https://ingress.example/telemetryIngress")
    assert rep.report_command_ack("cmd-1", "completed", applied_value=1) == 200
    assert seen == {"url": "https://ingress.example/telemetryIngress", "auth": "Bearer tok",
                    "body": {"type": "commandAck",
                             "payload": {"nodeUid": "node-1", "commandId": "cmd-1", "status": "completed",
                                         "appliedValue": 1}}}


def test_report_command_ack_tells_a_refusal_from_an_unreachable_ingress(monkeypatch):
    rep = HttpReporter("node-1", auth_token="tok", base_url="https://ingress.example/telemetryIngress")

    def rate_limited(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", rate_limited)
    assert rep.report_command_ack("cmd-1", "failed", error="x") == 429

    def offline(req, timeout):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr("urllib.request.urlopen", offline)
    assert rep.report_command_ack("cmd-1", "failed", error="x") is None


# ------------------------------------------------------------- the wiring in main()
# The bug was wiring: report_command_ack existed and nothing called it. These fail if main()
# stops giving the MQTT client its ack sink.

def _node_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"node": {"uid": "node-1", "name": "Bench", "auth_token": "tok"},
                               "mqtt": {"broker": "broker.example"},
                               "telemetry": {"ingress_url": "https://ingress.example/telemetryIngress"}}))
    return str(path)


class _WiredHere(Exception):
    pass


@pytest.mark.parametrize("offline", [False, True], ids=["cloud", "offline"])
def test_main_wires_the_command_acks(tmp_path, monkeypatch, offline):
    seen = {}

    def wire(mqtt_client, reporter, *, offline):
        seen.update(mqtt=mqtt_client, reporter=reporter, offline=offline)
        raise _WiredHere  # stop main() here: nothing further is under test

    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(agent_main, "wire_command_acks", wire)
    monkeypatch.setattr("sys.argv", ["agal-one-agent", "--config", _node_config(tmp_path)]
                        + (["--offline"] if offline else []))
    with pytest.raises(_WiredHere):
        agent_main.main()
    assert isinstance(seen["mqtt"], AgalOneMqttClient)
    assert isinstance(seen["reporter"], HttpReporter) and seen["reporter"].node_uid == "node-1"
    assert seen["offline"] is offline


def test_a_wired_mqtt_client_posts_its_acks_through_the_http_reporter(monkeypatch):
    posted = []
    rep = HttpReporter("node-1", auth_token="tok", base_url="https://ingress.example/telemetryIngress")
    monkeypatch.setattr(rep, "_post_status", lambda data: posted.append(data) or 200)
    mqtt = _mqtt(connected=False)  # MQTT down: the HTTPS leg does not depend on it

    acks = wire_command_acks(mqtt, rep, offline=False)
    mqtt.publish_command_ack("Xq3k9TzLmN2pRs7VbW1c", "completed", applied_value=1)

    assert acks is not None and acks.wait_idle(5)
    assert posted == [{"type": "commandAck", "payload": {"nodeUid": "node-1", "commandId": "Xq3k9TzLmN2pRs7VbW1c",
                                                         "status": "completed", "appliedValue": 1}}]
    acks.close()


def test_offline_wires_no_ack_sink():
    mqtt = _mqtt()
    assert wire_command_acks(mqtt, MagicMock(), offline=True) is None
    assert mqtt._ack_sink is None
