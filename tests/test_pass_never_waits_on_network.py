"""A pass never waits on the network, and the switch of language never has two runtime threads (agent 0.4.0 review).

The review's end-to-end runs (the real 0.4.0 ``main()`` against the functions emulator) found:

* the first pass after any program load — and any pass that raised an alert — posted over HTTPS (``urlopen``, 10 s
  timeout) on the runtime thread, holding the runtime lock: the input sampler, every command and, on bundle v3, the
  E-stop latch and the motor guards waited behind the network. Released 0.3.4 does the same for a v1 program;
* the old and the new runtime threads of a switch of language were alive together for 25 ms – 4.7 s in every run:
  the switch ran on the old runtime's thread, started the new runtime, then waited for the lock while the new
  runtime's first pass posted its ack;
* after a rollback (v3 → v1) the v1 program skipped its first report when it equalled what it sent before the
  migration, so the card's ``state.variables`` — removed by the cloud while v3 ran — stayed absent.

Here the ingress is a server that accepts every connection and never answers (each post waits its full 10 s), with
the real HttpReporter, AgalOneMqttClient (a recording paho underneath), NodeEvents and CloudSink, and the real
ProgramHost on its own threads. **The pass period is the agent's default tick, 1 s** (``blocks.tick_seconds``; an
input change or a command also wakes a pass at once).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import datetime

import pytest

import test_v3_language_switch as S
from agal_one_agent.blocks.clock import SimClock, SystemClock
from agal_one_agent.blocks.cloud import CloudSink
from agal_one_agent.blocks.host import ProgramHost
from agal_one_agent.blocks.io import HardwareIO, SimulatedIO
from agal_one_agent.blocks.runtime import BlockRuntime, RecordingSink
from agal_one_agent.blocks.sync import ProgramStore, ProgramSync
from agal_one_agent.config import MqttConfig
from agal_one_agent.http_reporter import HttpReporter
from agal_one_agent.mqtt_client import AgalOneMqttClient
from agal_one_agent.node_events import wire_node_events
from agal_one_agent.ports import gpiochip

PASS_S = 1.0  # the agent's default pass period (config.blocks.tick_seconds)
RUNTIME_THREADS = ("blocks-runtime", "blocks-v3-runtime")
CHIP = "pinctrl-bcm2835"
NODE = "PassNetworkNode00001"
ESTOP, PUMP, LIGHT = "estopCard00000000001", "pumpCard000000000001", "lightCard00000000001"


# ------------------------------------------------------------------------------------------------- the fakes

class StalledIngress:
    """Accepts every connection and never answers: a post waits for its whole timeout (urlopen, 10 s)."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.port = self.sock.getsockname()[1]
        self.accepted: list[socket.socket] = []
        self._closed = False
        threading.Thread(target=self._accept, name="stalled-ingress", daemon=True).start()

    def _accept(self) -> None:
        while not self._closed:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted.append(conn)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/telemetryIngress"

    def wait_for_posts(self, n: int, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while len(self.accepted) < n and time.monotonic() < deadline:
            time.sleep(0.005)
        return len(self.accepted) >= n

    def close(self) -> None:
        self._closed = True
        try:
            self.sock.close()
        except OSError:
            pass
        for c in list(self.accepted):
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()


class Paho:
    """paho's publish as the agent uses it (loop_start): queued, never a wait on the network."""

    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload)))


class Wired:
    """The agent's cloud side as main.py wires it, against the stalled ingress."""

    def __init__(self, ingress: StalledIngress):
        self.reporter = HttpReporter(NODE, auth_token="node-token-0000000000000000000000000", base_url=ingress.url)
        self.mqtt = AgalOneMqttClient(MqttConfig(broker="broker.example", username="shared", status_topic="agal/n/status",
                                                 commands_topic="agal/n/commands"))
        self.mqtt._client, self.mqtt._connected = Paho(), True
        self.events = wire_node_events(self.mqtt, self.reporter, offline=False, clock_ok=lambda: True)
        self.sink = CloudSink(self.mqtt, self.reporter, None, firmware_version="0.4.0")
        # The alerts the passes raised (what the runtime handed the sink), by their text.
        self.alerts: list[str] = []
        real_alert = self.sink.alert

        def alert(text, severity, rule_id, asset_id=None):
            self.alerts.append(text)
            return real_alert(text, severity, rule_id, asset_id)
        self.sink.alert = alert

    def close(self) -> None:
        self.sink.close()
        self.events.close(timeout=0.05)


@pytest.fixture
def ingress():
    s = StalledIngress()
    yield s
    s.close()


@pytest.fixture
def wired(ingress):
    w = Wired(ingress)
    yield w
    w.close()


def wait(pred, timeout: float = 5.0, step: float = 0.002) -> float:
    """Seconds until pred() held (asserting it did within timeout)."""
    t0 = time.monotonic()
    while not pred():
        if time.monotonic() - t0 > timeout:
            raise AssertionError(f"not within {timeout} s")
        time.sleep(step)
    return time.monotonic() - t0


def set_power(h: ProgramHost, aid: str, value, sk: str = "device.power") -> dict:
    return h.apply_command({"type": "setPower", "commandId": f"c{time.monotonic_ns()}", "assetId": aid, "sourceKey": sk,
                            "value": value})


# ------------------------------------------------------------------------------------------------- the outbox

class _GatedHttp:
    def __init__(self):
        self.gate = threading.Event()
        self.calls: list[tuple] = []
        self.stamps: list = []

    def report_program_ack(self, version, status, reason=None, firmware_version=None, timestamp=None):
        self.gate.wait(10)
        self.calls.append(("programAck", version, status, reason, firmware_version))
        self.stamps.append(timestamp)
        return True

    def report_alert(self, text, severity, rule_id=None, asset_id=None, timestamp=None):
        self.gate.wait(10)
        self.calls.append(("alert", text, severity))
        self.stamps.append(timestamp)
        return True

    def report_variables(self, asset_id, values, **kw):
        return True


class _Mqtt:
    def __init__(self):
        self.calls: list[tuple] = []
        self.stamps: list = []

    def publish_program_ack(self, version, status, reason=None, firmware_version=None, timestamp=None):
        self.calls.append(("programAck", version, status))
        self.stamps.append(timestamp)

    def publish_alert(self, text, severity, rule_id=None, asset_id=None, timestamp=None):
        self.calls.append(("alert", text))
        self.stamps.append(timestamp)


def test_acks_and_alerts_leave_the_callers_thread_in_order():
    http, mqtt = _GatedHttp(), _Mqtt()
    sink = CloudSink(mqtt, http, firmware_version="0.4.0")
    t0 = time.monotonic()
    sink.program_ack(5, "applied")
    sink.alert("The pump draws more than 8 A", "warning", "high_current", None)
    sink.program_ack(6, "rejected", "could not take over from the running program: x")
    assert time.monotonic() - t0 < 0.1, "a pass waited for the ingress"
    assert sink.last_ack == {"version": 6, "status": "rejected", "reason": "could not take over from the running program: x"}
    assert http.calls == [] and sink.outbox.waiting == 3
    http.gate.set()
    assert sink.flush(5.0)
    assert http.calls == [("programAck", 5, "applied", None, "0.4.0"), ("alert", "The pump draws more than 8 A", "warning"),
                          ("programAck", 6, "rejected", "could not take over from the running program: x", "0.4.0")]
    # Each as 0.3.4 sent it: an ack's HTTPS post then its MQTT copy; an alert's MQTT copy then its post.
    assert mqtt.calls == [("programAck", 5, "applied"), ("alert", "The pump draws more than 8 A"), ("programAck", 6, "rejected")]
    sink.close()


def test_a_pulled_bundle_that_is_rejected_is_acked_behind_the_acks_of_the_passes(tmp_path):
    """ProgramSync's rejection goes through the same outbox (main.py), HTTPS only as 0.3.4 posts it: an `applied` a pass
    handed over is never overtaken by a later `rejected`."""
    http, mqtt = _GatedHttp(), _Mqtt()
    sink = CloudSink(mqtt, http, firmware_version="0.4.0")

    class Pull:
        def fetch_program(self, current_version=None):
            bad = S.light_v1(7)
            bad["language"] = 2
            return {"version": 7, "bundle": bad}

    h = ProgramHost(SimulatedIO(), sink, clock=SimClock(), state_dir=str(tmp_path), node_uid=NODE)
    h.compile(S.light_v1(6))
    h.step()  # the first pass: applied v6, handed over
    sync = ProgramSync(h, ProgramStore(str(tmp_path)), Pull(), ack=sink.post_program_ack)
    sync._pull()
    http.gate.set()
    assert sink.flush(5.0)
    assert [c[:3] for c in http.calls] == [("programAck", 6, "applied"), ("programAck", 7, "rejected")]
    assert http.calls[1][4] is None, "the rejection is posted as 0.3.4's ProgramSync posts it (no firmwareVersion)"
    assert mqtt.calls == [("programAck", 6, "applied")], "and with no MQTT copy, as before"
    assert sink.last_ack["version"] == 6  # the heartbeat still names what runs
    sink.close()


def test_a_full_outbox_lets_an_alert_go_before_an_ack():
    from agal_one_agent.blocks.cloud import _Outbox
    box = _Outbox(name="test-outbox")
    gate = threading.Event()
    box.submit("programAck", lambda: gate.wait(5))  # in flight
    time.sleep(0.05)
    for i in range(_Outbox.CAP):
        box.submit("alert" if i == 3 else "programAck", lambda: None)
    box.submit("programAck", lambda: None)
    assert box.dropped == 1
    with box._cond:
        assert all(k == "programAck" for k, _ in box._queue)
    gate.set()
    assert box.flush(5.0)
    box.close()


# ------------------------------------------------------------------------------------------------- v1, stalled

def test_v1_a_set_power_is_carried_out_in_its_normal_time_while_the_ingress_stalls(tmp_path, ingress, wired):
    h = ProgramHost(SimulatedIO(), wired.sink, clock=SystemClock(assume_ok=True), state_dir=str(tmp_path),
                    tick_seconds=PASS_S, node_uid=NODE)
    h.compile(S.light_v1(6))
    h.start()
    try:
        wait(lambda: wired.sink.last_ack == {"version": 6, "status": "applied", "reason": None})
        assert ingress.wait_for_posts(1), "the program ack was not posted"
        # The ack waits for the ingress (10 s) — on the outbox's thread. The command is carried out at once.
        t0 = time.monotonic()
        assert set_power(h, S.LIGHT, 1)["accepted"]
        answered = time.monotonic() - t0
        took = wait(lambda: h.io.output(S.LIGHT, "device.power") is True)
        assert answered < 0.2, f"the command's answer waited {answered:.2f} s"
        assert took < PASS_S, f"the light switched {took:.2f} s after the command (one pass is {PASS_S} s)"
        assert wired.sink.outbox.waiting >= 1, "the ingress answered: the test proves nothing"
        t0 = time.monotonic()
        assert set_power(h, S.LIGHT, 0)["accepted"]
        assert wait(lambda: h.io.output(S.LIGHT, "device.power") is False) < PASS_S
    finally:
        h.stop(safe_state=False)


# ------------------------------------------------------------------------------------------------- v3, stalled

def _gpio_in(line: int) -> dict:
    return {"kind": "gpio", "chip": CHIP, "line": line, "direction": "in", "pull": "up", "activeLow": True}


def _gpio_out(line: int) -> dict:
    return {"kind": "gpio", "chip": CHIP, "line": line, "direction": "out", "activeLow": False}


def safety_bundle(version: int = 7) -> dict:
    """An E-stop (nc contact, panel reset), a guarded pump no block writes (switched by command: overload 1.25 ×
    7.5 A for 1 s, no inrush window, the 30 s lockout floor) and a Light — with one rule slot whose alerts fire as the
    light comes on and as the pump's current passes 8 A: before this review each of those passes waited for its alert's
    HTTPS post."""
    return {
        "language": 3, "version": version, "compiledAt": "2026-10-17T00:00:00Z", "timezone": "Asia/Kolkata",
        "assets": [
            {"assetId": ESTOP, "assetType": "emergency_stop", "name": "E-stop",
             "ports": {"sensor.contact": _gpio_in(5), "sensor.reset": _gpio_in(19)}},
            {"assetId": PUMP, "assetType": "motor_controller", "name": "Borewell pump", "safeState": "off",
             "ports": {"device.power": _gpio_out(17),
                       "sensor.current": {"kind": "i2c", "busId": 1, "addr": 72, "channel": 0, "driver": "ads1115", "direction": "in",
                                          "measure": {"mode": "ac_rms", "windowMs": 100}, "transform": {"scale": 30, "offset": 0},
                                          "unit": "A"}},
             "guard": {"output": "device.power", "current": ["sensor.current"], "ratedCurrentA": 7.5, "phaseType": "single_phase",
                       "settings": {"dryRun": {"dropPct": 30, "holdSeconds": 5}, "overload": {"multiple": 1.25, "holdSeconds": 1},
                                    "inrushIgnoreSeconds": 0, "restartLockoutSeconds": 30}}},
            {"assetId": LIGHT, "assetType": "lighting_circuit", "name": "Light", "ports": {"device.power": _gpio_out(18)}},
        ],
        "safety": {"emergencyStop": {"input": {"assetId": ESTOP, "sourceKey": "sensor.contact"}, "contact": "nc",
                                     "reset": {"assetId": ESTOP, "sourceKey": "sensor.reset"}, "resetKind": "reset_input"}},
        "rules": [{
            "key": "field:fieldSafety0000001/rule/1", "containerKind": "field", "containerId": "fieldSafety0000001",
            "slot": 1, "version": 1, "interlock": False,
            "program": {"name": "Tell me", "variables": [
                {"name": "light", "kind": "asset", "type": "bool", "asset": {"assetId": LIGHT, "sourceKey": "device.power", "access": "read"}},
                {"name": "amps", "kind": "asset", "type": "number", "asset": {"assetId": PUMP, "sourceKey": "sensor.current", "access": "read"}},
                {"name": "note", "kind": "alert", "type": "string"}],
                "rules": [
                    {"id": "light_on", "kind": "when", "when": {"condition": "light"},
                     "actions": [{"type": "alert", "text": "The light is on", "severity": "info"}]},
                    {"id": "high_current", "kind": "when", "when": {"condition": "amps > 8"},
                     "actions": [{"type": "alert", "text": "The pump draws more than 8 A", "severity": "warning"}]}]}}],
        "instruction": [],
        "settings": {"resumeAfterPowerSeconds": 30},
    }


def test_v3_the_estop_and_the_motor_guard_act_within_one_pass_while_the_ingress_stalls(tmp_path, ingress, wired):
    io = SimulatedIO()
    io.set_input(ESTOP, "sensor.contact", True)  # the nc contact closed: released
    io.set_input(ESTOP, "sensor.reset", False)
    io.set_input(PUMP, "sensor.current", 0.0)
    h = ProgramHost(io, wired.sink, clock=SystemClock(assume_ok=True), state_dir=str(tmp_path), tick_seconds=PASS_S,
                    node_uid=NODE)
    h.compile(safety_bundle(7))
    h.start()
    pump = lambda: io.output(PUMP, "device.power")  # noqa: E731
    try:
        wait(lambda: wired.sink.last_ack == {"version": 7, "status": "applied", "reason": None})
        assert ingress.wait_for_posts(1), "the program ack was not posted"
        # 1. Commands, while the program ack waits for the ingress.
        t0 = time.monotonic()
        assert set_power(h, PUMP, 1)["accepted"]
        assert time.monotonic() - t0 < 0.2
        assert wait(lambda: pump() is True) < PASS_S
        # 2. The light comes on: its pass raises an alert — and the E-stop is pressed the moment the alert is raised.
        #    (Before, that pass posted the alert itself, mid-pass, and held the lock 10 s: the E-stop waited for it.)
        assert set_power(h, LIGHT, 1)["accepted"]
        wait(lambda: "The light is on" in wired.alerts, timeout=PASS_S)
        io.set_input(ESTOP, "sensor.contact", False)
        took = wait(lambda: pump() is False, timeout=15.0)
        assert took < PASS_S, f"the E-stop stopped the pump {took:.2f} s after it was pressed (one pass is {PASS_S} s)"
        assert h.v3.prog.estop.latched and io.output(LIGHT, "device.power") is True
        # The panel: released, then the reset pressed and let go.
        io.set_input(ESTOP, "sensor.contact", True)
        time.sleep(0.05)
        io.set_input(ESTOP, "sensor.reset", True)
        time.sleep(0.05)
        io.set_input(ESTOP, "sensor.reset", False)
        wait(lambda: not h.v3.prog.estop.latched, timeout=3 * PASS_S)
        assert set_power(h, PUMP, 1)["accepted"]
        wait(lambda: pump() is True, timeout=PASS_S)
        # 3. Over-current: above 1.25 × 7.5 A from now; the guard holds 1 s, so the pump is off within that and one pass
        #    — though the same pass raises the rule's alert.
        t0 = time.monotonic()
        io.set_input(PUMP, "sensor.current", 10.0)
        took = wait(lambda: pump() is False, timeout=15.0)
        assert took < 1.0 + PASS_S + 0.25, f"the guard tripped {took:.2f} s after the over-current (hold 1 s + one pass)"
        assert h.v3.prog.guards[PUMP].tripped
        assert "The pump draws more than 8 A" in wired.alerts
        assert len(ingress.accepted) >= 2 and wired.sink.outbox.waiting >= 1, "the ingress answered: the test proves nothing"
    finally:
        h.stop(safe_state=False)


# ------------------------------------------------------------------------------------------------- the switch

class _Line:
    def __init__(self, path, line, direction, bias=None):
        self.level = direction == "high"

    def read(self):
        return False

    def write(self, v):
        self.level = bool(v)

    def close(self):
        pass


def _hardware_io() -> HardwareIO:
    """The Pi's I/O path with a fake chip, so each runtime runs its sampler thread as on the Pi."""
    chip = gpiochip.ChipInfo(path="/dev/gpiochip0", name="gpiochip0", label=CHIP, lines=54)
    return HardwareIO(lines=gpiochip.LineIO(chips=lambda: [chip], opener=_Line))


def test_never_two_runtime_threads_at_once_across_four_switches_with_a_stalled_ingress(tmp_path, ingress, wired):
    """At the instant each runtime starts no runtime thread is alive (the switch joined the old one first), and a probe
    every half millisecond never sees the v1 and the v3 runtime threads together."""
    h = ProgramHost(_hardware_io(), wired.sink, clock=SystemClock(assume_ok=True), state_dir=str(tmp_path),
                    tick_seconds=0.02, node_uid=NODE)
    alive_at_start: list[list[str]] = []
    for rt in (h.v1, h.v3):
        real = rt.start

        def start(_real=real):
            alive_at_start.append(sorted(t.name for t in threading.enumerate() if t.name in RUNTIME_THREADS and t.is_alive()))
            return _real()
        rt.start = start
    both: list[list[str]] = []
    done = threading.Event()

    def probe():
        while not done.is_set():
            names = {t.name for t in threading.enumerate()}
            if all(n in names for n in RUNTIME_THREADS):
                both.append(sorted(names))
            time.sleep(0.0005)
    p = threading.Thread(target=probe, name="probe", daemon=True)
    p.start()
    h.compile(S.light_v1(6))
    h.start()
    try:
        wait(lambda: wired.sink.last_ack is not None)
        for version, bundle, lang in ((7, S.light_v3(7), 3), (8, S.light_v1(8), 1), (9, S.light_v3(9), 3), (10, S.light_v1(10), 1)):
            h.compile(bundle)
            wait(lambda: h.language == lang and h.version == version and h.active._running and not h._switching)
            wait(lambda: (wired.sink.last_ack or {}).get("version") == version)  # its first pass ran
            other = h.v3 if lang == 1 else h.v1
            assert other._thread is None or not other._thread.is_alive()
            assert other._sampler is None or not other._sampler.is_alive()
            time.sleep(0.05)
        assert ingress.wait_for_posts(1) and wired.sink.outbox.waiting >= 1, "the ingress answered: the test proves nothing"
    finally:
        done.set()
        p.join(timeout=2)
        h.stop(safe_state=False)
    assert alive_at_start == [[], [], [], [], []], alive_at_start
    assert both == [], f"the v1 and the v3 runtime threads were alive together {len(both)} times"


def test_a_command_that_arrives_during_the_switch_is_carried_out_by_the_new_program(tmp_path, monkeypatch):
    """Before, a command accepted by the old program while the node changed language was queued on a runtime that ran
    no pass again: lost. Now it waits for the switch and goes to the program that runs."""
    h = ProgramHost(SimulatedIO(), RecordingSink(), clock=SystemClock(assume_ok=True), state_dir=str(tmp_path),
                    tick_seconds=0.02, node_uid=NODE)
    real = h.v3.take_over_language
    in_switch = threading.Event()

    def slow(bundle, carried, days):
        in_switch.set()
        time.sleep(0.3)
        return real(bundle, carried, days)
    monkeypatch.setattr(h.v3, "take_over_language", slow)
    h.compile(S.light_v1(6))
    h.start()
    try:
        wait(lambda: h.sink.acks)
        h.compile(S.light_v3(7))
        assert in_switch.wait(3)
        answer: dict = {}
        t = threading.Thread(target=lambda: answer.update(set_power(h, S.LIGHT, 1)))
        t0 = time.monotonic()
        t.start()
        t.join(5)
        assert answer == {"accepted": True} and h.language == 3
        assert wait(lambda: h.io.output(S.LIGHT, "device.power") is True) < 1.0
        assert time.monotonic() - t0 < 1.5
    finally:
        h.stop(safe_state=False)


def test_a_stop_during_a_switch_stops_the_program_that_took_over(tmp_path, monkeypatch):
    h = ProgramHost(SimulatedIO(), RecordingSink(), clock=SystemClock(assume_ok=True), state_dir=str(tmp_path),
                    tick_seconds=0.02, node_uid=NODE)
    real = h.v3.take_over_language
    in_switch = threading.Event()

    def slow(bundle, carried, days):
        in_switch.set()
        time.sleep(0.2)
        return real(bundle, carried, days)
    monkeypatch.setattr(h.v3, "take_over_language", slow)
    h.compile(S.light_v1(6))
    h.start()
    wait(lambda: h.sink.acks)
    h.compile(S.light_v3(7))
    assert in_switch.wait(3)
    h.stop(safe_state=True)  # waits for the switch, which then starts nothing, and stops what took over
    assert h.language == 3 and h.version == 7
    assert not h.v3._running and not h.v1._running and not h._running
    assert not any(rt._thread is not None and rt._thread.is_alive() for rt in (h.v1, h.v3))


# ------------------------------------------------------------------------------------------------- reported in full

def _host(tmp_path, when=datetime(2026, 10, 16, 19, 0, tzinfo=S.TZ)):
    clock = SimClock(when)
    io, sink = SimulatedIO(), RecordingSink()
    return ProgramHost(io, sink, clock=clock, state_dir=str(tmp_path), node_uid=NODE), io, sink, clock


def test_after_a_rollback_the_v1_program_reports_its_cards_at_once_even_unchanged(tmp_path):
    """The Light on before the migration, still on after the rollback, all within the 30 s keep-alive: the v1 program's
    first pass after the rollback reports the card (the cloud removed state.variables while v3 ran)."""
    h, io, sink, clock = _host(tmp_path)
    h.compile(S.light_v1(6))
    S.run(h, clock, 2)
    assert S.switch(h, S.LIGHT, "device.power", 1)["accepted"]
    S.run(h, clock, 2)
    assert sink.variables_log[-1] == (S.LIGHT, {"relay": True, "on": True})
    h.compile(S.light_v3(7))
    S.run(h, clock, 3)
    reported = len(sink.variables_log)
    h.compile(S.light_v1(8))
    S.run(h, clock, 1)
    assert sink.variables_log[reported:] == [(S.LIGHT, {"relay": True, "on": True})]


def test_every_start_of_the_v1_runtime_reports_every_card_in_full(tmp_path):
    rt = BlockRuntime(SimulatedIO(), RecordingSink(), clock=SimClock(), state_dir=str(tmp_path), tick_seconds=3600)
    rt.compile(S.light_v1(6))
    rt.step()
    assert len(rt.sink.variables_log) == 1
    rt.step()
    assert len(rt.sink.variables_log) == 1, "nothing changed: nothing reported"
    rt.start()  # its loop runs one pass at once, then waits an hour
    try:
        wait(lambda: len(rt.sink.variables_log) == 2)
    finally:
        rt.stop(safe_state=False)
    assert rt.sink.variables_log[1] == rt.sink.variables_log[0]
