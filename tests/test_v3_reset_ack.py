"""A valve reset's answer (contracts v1.15.2 item 15; functions.yaml resetValve 1.11.3; programs/README.md §7.9).

Agent 0.4.0 acked a ``resetValve`` ``completed`` when its runtime ACCEPTED it — before the close pulse of the next
pass — and a reset that pass refused was only logged, so the /assetCommands document said completed while the valve
stayed in FAULT (SYNC.md v1.15.2 12(e)). Now the node answers once, from the pass that carries the reset out:

* nothing on arrival (a refusal then is ``failed`` in words, as before);
* ``completed`` once the close pulse has run, the valve is recorded closed and its FAULT cleared — handed over after
  the pass's ``valve_reset`` event;
* ``failed`` in the node's words when that pass refuses it or cannot carry it out: the supply not present, not a
  latching valve, the open coil not switched off, the reset not recorded, the close pulse not sent or the supply
  gone after it, an error in the pass, the node stopping before the pass;
* ``completed`` with NO pulse for a latching valve no longer in FAULT (idempotent);
* the words name the supply's PORT as the node knows it, never a card id;
* through the node's HTTPS ack queue (command_acks.CommandAcks): a pass never waits on the network, also while the
  ingress is stalled.

No hardware: the pump house of the golden vectors (Valve 2 latching, its supply sensed on line 13 of
pinctrl-bcm2835 — the vectors' DI3) on a SimClock and a SimulatedIO.
"""

from __future__ import annotations

import threading
import time

import pytest

import agal_one_agent.blocks.v3.runtime as R
from agal_one_agent import main as agent_main
from agal_one_agent.blocks.cloud import CloudSink
from agal_one_agent.blocks.io import SimulatedIO
from agal_one_agent.blocks.runtime import RecordingSink
from agal_one_agent.command_acks import wire_command_acks
from agal_one_agent.config import MqttConfig
from agal_one_agent.mqtt_client import AgalOneMqttClient
from agal_one_agent.node_events import wire_node_events
from test_v3_corners import _NoWrite, _StuckOff
from test_v3_valves import both_valves, pulses
from v3_bench import ESTOP, HEALTHY, PUMP, UPS, V1, V2, Bench, card, nxt, pumphouse

ACTOR = {"uid": "u_owner01", "via": "app"}
SUPPLY_GONE = "refused: the valve's supply on line 13 of pinctrl-bcm2835 is not present"
CLOSE = ("valve.close_coil", 100)


def reset(cid: str, aid: str = V2) -> dict:
    return {"type": "resetValve", "assetId": aid, "commandId": cid, "actor": ACTOR}


class OrderedSink(RecordingSink):
    """A RecordingSink that also keeps the order of events and command answers."""

    def __init__(self):
        super().__init__()
        self.order: list[tuple] = []

    def event(self, type_, payload, source="program"):
        super().event(type_, payload, source)
        self.order.append(("event", type_))

    def command_ack(self, command_id, status, error=None):
        super().command_ack(command_id, status, error)
        self.order.append(("ack", command_id, status))


def bench(io: SimulatedIO = None, bundle: dict = None, sink=None) -> Bench:
    b = Bench(bundle or both_valves(pumphouse()), load=False)
    if io is not None:
        b.io = io
        for (aid, sk), v in HEALTHY.items():
            io.set_input(aid, sk, v)
    b.sink = sink if sink is not None else OrderedSink()
    b.rt = b.new_runtime()
    b.rt.compile(b.bundle)
    return b


def faulted(b: Bench) -> None:
    """06:01, the morning run in progress: Valve 2's supply drops (FAULT, supply_lost), then comes back — the valve
    stays in FAULT until a reset."""
    b.step()
    b.until(6, 1)
    b.set(UPS, "sensor.power", False)
    b.run(1)
    b.set(UPS, "sensor.power", True)
    b.run(1)
    assert b.rt.prog.valves[V2].fault == "supply_lost"


def acks(b: Bench, cid: str = None) -> list[tuple]:
    return [(a["commandId"], a["status"], a["error"]) for a in b.sink.command_acks if cid is None or a["commandId"] == cid]


# ======================================================================== completed after the pulse

def test_a_reset_is_answered_completed_by_the_pass_that_sends_its_close_pulse_and_nothing_on_arrival():
    b = bench()
    faulted(b)
    m = b.mark()
    assert b.rt.apply_command(reset("c1")) == {"accepted": True}
    assert acks(b) == [] and pulses(b.writes_since(m)) == []  # accepted: no answer yet, no pulse yet
    b.step()  # the next pass
    assert pulses(b.writes_since(m))[0] == CLOSE
    assert acks(b) == [("c1", "completed", None)]
    assert b.rt.prog.valves[V2].fault is None
    assert {v["assetId"]: v["state"] for v in b.rt.status_extra()["valves"]}[V2] != "fault"
    # The answer goes with the pass's reports, after its valve_reset event.
    assert b.sink.order.index(("event", "valve_reset")) < b.sink.order.index(("ack", "c1", "completed"))
    b.run(3)
    assert acks(b) == [("c1", "completed", None)]  # one answer, never another


def test_a_reset_while_the_estop_is_latched_is_carried_out_and_completed():
    """README §7.10: a latched E-stop is not a refusal — closing a valve is the safe direction; the motor stays off."""
    b = bench()
    faulted(b)
    b.set(ESTOP, "sensor.contact", False)
    b.run(1)
    assert b.rt.prog.estop.latched
    m = b.mark()
    assert b.rt.apply_command(reset("c2"))["accepted"]
    b.run(1)
    assert CLOSE in pulses(b.writes_since(m)) and acks(b) == [("c2", "completed", None)]
    assert b.rt.prog.estop.latched and b.pump_on() is False


# ======================================================================== refused, in the node's words

def test_a_reset_refused_at_the_next_pass_is_answered_failed_in_the_nodes_words():
    """Accepted while the supply was there; it dropped before the pass that would carry it out."""
    b = bench()
    faulted(b)
    assert b.rt.apply_command(reset("c3"))["accepted"]
    b.set(UPS, "sensor.power", False)
    m = b.mark()
    b.step()
    assert acks(b) == [("c3", "failed", SUPPLY_GONE)]
    assert pulses(b.writes_since(m)) == [] and b.events_since(m, "valve_reset") == []
    assert b.rt.prog.valves[V2].fault == "supply_lost"


def test_the_supply_refusal_names_the_port_never_a_card_id():
    b = bench()
    faulted(b)
    b.set(UPS, "sensor.power", None)  # cannot be read: not sensed present either
    b.run(1)
    r = b.rt.apply_command(reset("c4"))
    assert r == {"accepted": False, "reason": SUPPLY_GONE}
    assert UPS not in r["reason"] and "card" not in r["reason"] and "sensor.power" not in r["reason"]


def test_a_reset_of_a_valve_a_reload_made_continuous_is_refused_at_the_pass():
    b = bench(bundle=pumphouse())
    faulted(b)
    assert b.rt.apply_command(reset("c5"))["accepted"]

    def continuous(x):
        v = card(x, V2)
        v["ports"].pop("valve.close_coil")
        v["valve"] = {"actuation": "continuous_dc", "open": "valve.open_coil", "normallyClosed": True, "coilVoltage": 12}
    b.rt.compile(nxt(b.rt.bundle, continuous))
    m = b.mark()
    b.step()
    assert acks(b) == [("c5", "failed", f"refused: {V2} is not a latching valve of this node")]
    assert pulses(b.writes_since(m)) == []


# ======================================================================== idempotent

def test_a_reset_of_a_valve_no_longer_in_fault_is_completed_with_no_pulse():
    """Two people press Reset at once, and a third while the card still shows the fault: one close pulse, three
    completed answers (the cloud refuses not_in_fault only once its mirror has caught up)."""
    b = bench()
    faulted(b)
    m = b.mark()
    assert b.rt.apply_command(reset("cA"))["accepted"]
    assert b.rt.apply_command(reset("cB"))["accepted"]
    b.step()
    assert acks(b) == [("cA", "completed", None), ("cB", "completed", None)]
    assert pulses(b.writes_since(m)).count(CLOSE) == 1 and len(b.events_since(m, "valve_reset")) == 1
    b.run(2)
    m2 = b.mark()
    assert b.rt.apply_command(reset("cC"))["accepted"]
    b.step()
    assert acks(b, "cC") == [("cC", "completed", None)]
    assert CLOSE not in pulses(b.writes_since(m2)) and b.events_since(m2, "valve_reset") == []


# ======================================================================== could not be carried out

class _CloseFails(SimulatedIO):
    def pulse(self, port, ms):
        if (port.asset_id, port.source_key) == (V2, "valve.close_coil"):
            raise OSError("the relay driver did not answer")
        super().pulse(port, ms)


class _SupplyGoesWithTheClose(SimulatedIO):
    def pulse(self, port, ms):
        super().pulse(port, ms)
        if (port.asset_id, port.source_key) == (V2, "valve.close_coil"):
            self._inputs[(UPS, "sensor.power")] = False  # the supply went during the pulse (no listener: not a pass)


def test_a_reset_whose_close_pulse_cannot_be_sent_is_answered_failed_and_stays_in_fault():
    b = bench(io=_CloseFails())
    faulted(b)
    m = b.mark()
    assert b.rt.apply_command(reset("c6"))["accepted"]
    b.step()
    assert acks(b) == [("c6", "failed", "the close pulse could not be sent — Valve 2 stays in fault")]
    assert b.rt.prog.valves[V2].fault == "pulse_cut_short" and b.events_since(m, "valve_reset") == []


def test_a_reset_after_whose_pulse_the_supply_is_gone_is_answered_failed_and_stays_in_fault():
    b = bench(io=_SupplyGoesWithTheClose())
    faulted(b)
    m = b.mark()
    assert b.rt.apply_command(reset("c7"))["accepted"]
    b.step()
    assert pulses(b.writes_since(m)) == [CLOSE]
    assert acks(b) == [("c7", "failed", "the valve's supply on line 13 of pinctrl-bcm2835 was not present after the "
                                         "close pulse — Valve 2 stays in fault")]
    d = b.rt.prog.valves[V2]
    assert d.fault == "pulse_cut_short" and "after the reset's close pulse" in d.fault_detail
    assert b.events_since(m, "valve_reset") == []


def test_a_reset_that_cannot_switch_the_open_coil_off_is_answered_failed():
    b = bench(io=_StuckOff((V2, "valve.open_coil"), 50))
    b.step()
    b.until(6, 0, 1)
    assert b.rt.prog.valves[V2].fault == "pulse_cut_short"
    m = b.mark()
    assert b.rt.apply_command(reset("c8"))["accepted"]
    b.step()
    assert acks(b) == [("c8", "failed", "the open coil could not be switched off: no close pulse sent — Valve 2 stays in fault")]
    assert pulses(b.writes_since(m)) == []


def test_a_reset_that_cannot_be_recorded_is_answered_failed(monkeypatch):
    b = bench()
    faulted(b)
    monkeypatch.setattr(R, "os", _NoWrite())
    m = b.mark()
    assert b.rt.apply_command(reset("c9"))["accepted"]
    b.step()
    assert acks(b) == [("c9", "failed", "the reset could not be recorded in the state directory: no close pulse sent — "
                                         "Valve 2 stays in fault")]
    assert pulses(b.writes_since(m)) == [] and b.rt.prog.valves[V2].fault


def test_an_error_while_carrying_out_a_reset_is_answered_failed(monkeypatch):
    b = bench()
    faulted(b)

    def broken(drv, key):
        raise RuntimeError("the line request was lost")
    monkeypatch.setattr(b.rt, "_send_pulse", broken)
    assert b.rt.apply_command(reset("c10"))["accepted"]
    b.step()
    assert acks(b) == [("c10", "failed", "the reset could not be carried out: the line request was lost")]
    assert b.rt.prog.valves[V2].fault


def test_a_reset_carried_out_by_a_pass_that_fails_later_on_is_still_answered(monkeypatch):
    b = bench()
    faulted(b)
    assert b.rt.apply_command(reset("c11"))["accepted"]

    def broken(now, wall):
        raise RuntimeError("a bug after the commands")
    monkeypatch.setattr(b.rt, "_holds", broken)
    with pytest.raises(RuntimeError):
        b.step()
    assert acks(b) == [("c11", "completed", None)]  # the pulse was sent and the valve cleared before the failure


def test_a_reset_no_pass_ran_is_answered_failed_when_the_runtime_stops():
    b = bench()
    faulted(b)
    m = b.mark()
    assert b.rt.apply_command(reset("c12"))["accepted"]
    b.rt.stop(safe_state=True)
    assert acks(b) == [("c12", "failed", R.RESET_STOPPED)]
    assert R.RESET_STOPPED == "the node stopped before the reset ran: no close pulse sent"
    # Started again (the same runtime), it does not pulse the reset it answered.
    b.rt.compile(b.bundle)
    b.run(2)
    assert CLOSE not in pulses(b.writes_since(m)) and b.rt.prog.valves[V2].fault
    assert acks(b) == [("c12", "failed", R.RESET_STOPPED)]


# ======================================================================== main.py: who answers

class _Mqtt:
    """The agent's MQTT client as a pass and main.py see it: every ack recorded; every other report a no-op."""

    def __init__(self):
        self.acks: list[tuple] = []

    def publish_command_ack(self, command_id, status, applied_value=None, error=None, *, ingress=True, reason=None):
        self.acks.append((command_id, status, error))

    def __getattr__(self, name):
        return lambda *a, **k: None


class _Nothing:
    def __getattr__(self, name):
        return lambda *a, **k: None


def test_main_leaves_an_accepted_reset_to_its_pass_and_answers_every_other_command_now():
    mqtt = _Mqtt()
    b = bench(sink=CloudSink(mqtt, _Nothing(), None, firmware_version="0.4.0", http_async=False))
    faulted(b)
    agent_main.answer_program_command(reset("d1"), b.rt, mqtt)
    assert mqtt.acks == []  # accepted: the pass answers it
    b.step()
    assert mqtt.acks == [("d1", "completed", None)]  # through CloudSink.command_ack → publish_command_ack
    agent_main.answer_program_command(reset("d2", aid=V1), b.rt, mqtt)  # refused on arrival: answered now
    assert mqtt.acks[-1] == ("d2", "failed", f"refused: {V1} is not a latching valve of this node")
    agent_main.answer_program_command({"type": "pauseAutomation", "commandId": "d3", "actor": ACTOR}, b.rt, mqtt)
    assert mqtt.acks[-1] == ("d3", "completed", None)  # every other command: completed on acceptance, as before
    b.step()
    assert [a[0] for a in mqtt.acks] == ["d1", "d2", "d3"]  # the pause's pass answers nothing more
    agent_main.answer_program_command(reset("d4"), None, mqtt)
    assert mqtt.acks[-1] == ("d4", "failed", "no automation program loaded")


# ======================================================================== a stalled ingress

class _StalledReporter:
    """Every HTTPS call of the agent blocks until `release` is set (an ingress that does not answer), then
    succeeds; the calls are recorded as they start."""

    def __init__(self):
        self.release = threading.Event()
        self.started: list[tuple] = []
        self.done: list[tuple] = []

    def __getattr__(self, name):
        if not name.startswith("report_"):
            raise AttributeError(name)

        def call(*a, **k):
            self.started.append((name, a))
            self.release.wait(30)
            self.done.append((name, a))
            return 200
        return call


class _Paho:
    def publish(self, topic, payload, qos=0, retain=False):
        return None


def test_a_reset_during_a_stalled_ingress_pulses_at_once_and_its_answer_waits_in_the_ack_queue():
    """The real AgalOneMqttClient, CommandAcks, NodeEvents and CloudSink, every HTTPS post stalled, and the runtime
    on its own thread at the agent's 1 s tick: the reset's pass pulses at once and hands its completed ack to the
    queue; the passes go on (the E-stop latches at once); the ack is posted when the ingress answers again."""
    reporter = _StalledReporter()
    mqtt = AgalOneMqttClient(MqttConfig(broker="broker.example", username="shared", status_topic="agal/n/status",
                                        commands_topic="agal/n/commands"))
    mqtt._client, mqtt._connected = _Paho(), True
    queue = wire_command_acks(mqtt, reporter, offline=False)
    events = wire_node_events(mqtt, reporter, offline=False, clock_ok=lambda: True)
    sink = CloudSink(mqtt, reporter, None, firmware_version="0.4.0")
    b = bench(sink=sink)
    try:
        faulted(b)
        before = len(reporter.started)
        b.rt.start()
        t0 = time.monotonic()
        agent_main.answer_program_command(reset("s1aaaaaaaaaaaaaaaaaa"), b.rt, mqtt)
        while CLOSE not in pulses(b.io.log) and time.monotonic() - t0 < 3.0:
            time.sleep(0.002)
        pulsed = time.monotonic() - t0
        assert CLOSE in pulses(b.io.log) and pulsed < 0.5, pulsed  # the next pass, not a pass behind the network
        assert not reporter.release.is_set() and reporter.done == []  # nothing has been delivered
        # The passes go on while the ingress is stalled: the E-stop is latched at once.
        t1 = time.monotonic()
        b.set(ESTOP, "sensor.contact", False)
        while not b.rt.prog.estop.latched and time.monotonic() - t1 < 3.0:
            time.sleep(0.002)
        assert b.rt.prog.estop.latched and time.monotonic() - t1 < 0.5
        # The answer is in the ack queue: waiting, or on its way (the worker's post is the one that is stalled).
        deadline = time.monotonic() + 3.0
        while not any(n == "report_command_ack" for n, _ in reporter.started[before:]) and time.monotonic() < deadline:
            time.sleep(0.005)
        assert [(a[0], a[1]) for n, a in reporter.started[before:] if n == "report_command_ack"] == \
            [("s1aaaaaaaaaaaaaaaaaa", "completed")]
        reporter.release.set()
        assert queue.wait_idle(5.0)
        assert [(a[0], a[1]) for n, a in reporter.done if n == "report_command_ack"] == [("s1aaaaaaaaaaaaaaaaaa", "completed")]
        assert b.rt.prog.valves[V2].fault is None
    finally:
        reporter.release.set()
        b.rt.stop(safe_state=False)
        sink.close()
        queue.close(timeout=0.5)
        events.close(timeout=0.5)
