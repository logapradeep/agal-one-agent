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
* one reset per valve per pass: a second reset of the same valve in the same pass takes the first's answer and sends
  no pulse of its own (contracts v1.15.2 item 16);
* a command that arrives once the runtime has stopped — main() stops it first and disconnects MQTT last — is refused
  on arrival, in words, until the runtime is started again (item 16);
* ``completed`` with NO pulse for a latching valve no longer in FAULT (idempotent);
* the words name the supply's PORT as the node knows it, never a card id — agent 0.4.4 (contracts v1.26.0, the
  founder's answer 7): its node port id with the card's name (DI3 (UPS supply)) when the bundle names it, else its line;
* through the node's HTTPS ack queue (command_acks.CommandAcks): a pass never waits on the network, also while the
  ingress is stalled.

No hardware: the pump house of the golden vectors (Valve 2 latching on DO3 and DO4, its supply sensed on DI3 — line 13
of pinctrl-bcm2835 — by the UPS supply card) on a SimClock and a SimulatedIO.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime

import pytest

import agal_one_agent.blocks.v3.runtime as R
from agal_one_agent import main as agent_main
from agal_one_agent.blocks.clock import SimClock
from agal_one_agent.blocks.cloud import CloudSink
from agal_one_agent.blocks.host import ProgramHost
from agal_one_agent.blocks.io import SimulatedIO
from agal_one_agent.blocks.runtime import RecordingSink
from agal_one_agent.command_acks import wire_command_acks
from agal_one_agent.config import MqttConfig
from agal_one_agent.mqtt_client import AgalOneMqttClient
from agal_one_agent.node_events import wire_node_events
from test_v3_corners import _NoWrite, _StuckOff
from test_v3_valves import both_valves, pulses
from v3_bench import CLEAN, ESTOP, HEALTHY, PUMP, STATE_FILE, TZ, UPS, V1, V2, Bench, card, nxt, pumphouse

ACTOR = {"uid": "u_owner01", "via": "app"}
SUPPLY_GONE = "refused: Valve 2's supply on DI3 (UPS supply) is not present"
# A bundle compiled before contracts v1.26.0 names no port id: the words name the line, as agent 0.4.3 did.
SUPPLY_GONE_LINE = "refused: the valve's supply on line 13 of pinctrl-bcm2835 is not present"
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


def _without_port_ids(bundle: dict) -> dict:
    for a in bundle["assets"]:
        for k in ("openPortId", "closePortId", "supplyPortId"):
            (a.get("valve") or {}).pop(k, None)
    return bundle


def test_a_bundle_compiled_before_v1_24_0_names_the_line_as_before():
    """Agent 0.4.4 on a bundle with no port ids (a backend before contracts v1.26.0): the words of agent 0.4.3."""
    b = bench(bundle=_without_port_ids(both_valves(pumphouse())))
    faulted(b)
    b.set(UPS, "sensor.power", None)
    b.run(1)
    assert b.rt.apply_command(reset("cL")) == {"accepted": False, "reason": SUPPLY_GONE_LINE}


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
    """Valve 2's close pulse fails once ``armed`` — after the boot pass, whose close pulse (agent 0.4.4, a clean start
    closes it, README §7.9) goes through: the reset's is the one these tests mean."""
    armed = False

    def pulse(self, port, ms):
        if self.armed and (port.asset_id, port.source_key) == (V2, "valve.close_coil"):
            raise OSError("the relay driver did not answer")
        super().pulse(port, ms)


class _SupplyGoesWithTheClose(SimulatedIO):
    armed = False

    def pulse(self, port, ms):
        super().pulse(port, ms)
        if self.armed and (port.asset_id, port.source_key) == (V2, "valve.close_coil"):
            self._inputs[(UPS, "sensor.power")] = False  # the supply went during the pulse (no listener: not a pass)


def test_a_reset_whose_close_pulse_cannot_be_sent_is_answered_failed_and_stays_in_fault():
    b = bench(io=_CloseFails())
    faulted(b)
    b.io.armed = True
    m = b.mark()
    assert b.rt.apply_command(reset("c6"))["accepted"]
    b.step()
    assert acks(b) == [("c6", "failed", "the close pulse on DO4 could not be sent — Valve 2 stays in fault")]
    assert b.rt.prog.valves[V2].fault == "pulse_cut_short" and b.events_since(m, "valve_reset") == []


def test_a_reset_after_whose_pulse_the_supply_is_gone_is_answered_failed_and_stays_in_fault():
    b = bench(io=_SupplyGoesWithTheClose())
    faulted(b)
    b.io.armed = True
    m = b.mark()
    assert b.rt.apply_command(reset("c7"))["accepted"]
    b.step()
    assert pulses(b.writes_since(m)) == [CLOSE]
    assert acks(b) == [("c7", "failed", "Valve 2's supply on DI3 (UPS supply) was not present after the "
                                         "close pulse — Valve 2 stays in fault")]
    d = b.rt.prog.valves[V2]
    assert d.fault == "pulse_cut_short" and "after the reset's close pulse" in d.fault_detail
    assert b.events_since(m, "valve_reset") == []


# ======================================================================== one reset per valve per pass

GONE_AFTER = "Valve 2's supply on DI3 (UPS supply) was not present after the close pulse — Valve 2 stays in fault"


def test_two_resets_in_one_pass_send_one_close_pulse_when_the_supply_is_read_absent_after_it():
    """Contracts v1.15.2 item 16: two resets of Valve 2 reach one pass (two people press Reset, or the app and Agal
    Assist) and the supply is read absent right after the first one's close pulse. The second takes the first's answer
    — checked again it would read the supply as sampled at the start of the pass, which still says present, and pulse
    again — so exactly one close pulse and two failed answers in the same words."""
    b = bench(io=_SupplyGoesWithTheClose())
    faulted(b)
    b.io.armed = True
    m = b.mark()
    assert b.rt.apply_command(reset("cA"))["accepted"] and b.rt.apply_command(reset("cB"))["accepted"]
    b.step()
    assert pulses(b.writes_since(m)).count(CLOSE) == 1
    assert acks(b) == [("cA", "failed", GONE_AFTER), ("cB", "failed", GONE_AFTER)]
    assert b.rt.prog.valves[V2].fault == "pulse_cut_short" and b.events_since(m, "valve_reset") == []
    # The next pass samples the supply afresh: a reset now is refused on arrival, and nothing more is pulsed.
    b.run(1)
    assert b.rt.apply_command(reset("cC")) == {"accepted": False, "reason": SUPPLY_GONE}
    assert pulses(b.writes_since(m)).count(CLOSE) == 1


class _CloseFailsCounted(_CloseFails):
    def __init__(self):
        super().__init__()
        self.close_tries = 0

    def pulse(self, port, ms):
        if self.armed and (port.asset_id, port.source_key) == (V2, "valve.close_coil"):
            self.close_tries += 1  # the reset's tries (the boot pass's close pulse, before it was armed, went through)
        super().pulse(port, ms)


def test_a_second_reset_in_the_same_pass_does_not_retry_a_close_pulse_that_could_not_be_sent():
    b = bench(io=_CloseFailsCounted())
    faulted(b)
    b.io.armed = True
    assert b.rt.apply_command(reset("cD"))["accepted"] and b.rt.apply_command(reset("cE"))["accepted"]
    b.step()
    words = "the close pulse on DO4 could not be sent — Valve 2 stays in fault"
    assert b.io.close_tries == 1
    assert acks(b) == [("cD", "failed", words), ("cE", "failed", words)]


def test_a_second_reset_in_the_same_pass_takes_the_first_ones_error(monkeypatch):
    b = bench()
    faulted(b)
    calls = []

    def broken(drv, key):
        calls.append(key)
        raise RuntimeError("the line request was lost")
    monkeypatch.setattr(b.rt, "_send_pulse", broken)
    assert b.rt.apply_command(reset("cF"))["accepted"] and b.rt.apply_command(reset("cG"))["accepted"]
    b.step()
    words = "the reset could not be carried out: the line request was lost"
    assert len(calls) == 1 and acks(b) == [("cF", "failed", words), ("cG", "failed", words)]


def test_a_reset_that_cannot_switch_the_open_coil_off_is_answered_failed():
    b = bench(io=_StuckOff((V2, "valve.open_coil"), 50))
    b.step()
    b.until(6, 0, 1)
    assert b.rt.prog.valves[V2].fault == "pulse_cut_short"
    m = b.mark()
    assert b.rt.apply_command(reset("c8"))["accepted"]
    b.step()
    assert acks(b) == [("c8", "failed", "the open coil of Valve 2 on DO3 could not be switched off: no close pulse sent — "
                                         "Valve 2 stays in fault")]
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


# ======================================================================== a command after the stop

def test_a_command_that_arrives_after_the_runtime_stopped_is_refused_on_arrival_until_it_starts_again():
    """Contracts v1.15.2 item 16: main() stops the runtime first and disconnects MQTT last, so for a few seconds
    commands still arrive. Accepted, a reset got no answer at all (no pass runs it: the document stayed `sent` until
    the cloud's timeout) and stayed pending for a later start to pulse; a setPower was acked completed and never
    applied. Now each is refused on arrival, in words — also after a compile (a program pulled while the agent shuts
    down) — until the runtime is started again."""
    mqtt = _Mqtt()
    b = bench()
    faulted(b)
    b.rt.stop(safe_state=True)
    m = b.mark()
    assert R.RESET_STOPPING == "refused: the node is stopping — no close pulse sent"
    assert R.STOPPING == "refused: the node is stopping — no pass will carry it out"
    assert b.rt.apply_command(reset("e1")) == {"accepted": False, "reason": R.RESET_STOPPING}
    assert b.rt.apply_command({"type": "setPower", "assetId": PUMP, "value": 1, "commandId": "e2"}) == \
        {"accepted": False, "reason": R.STOPPING}
    agent_main.answer_program_command(reset("e3"), b.rt, mqtt)
    agent_main.answer_program_command({"type": "pauseAutomation", "commandId": "e4", "actor": ACTOR}, b.rt, mqtt)
    assert mqtt.acks == [("e3", "failed", R.RESET_STOPPING), ("e4", "failed", R.STOPPING)]
    assert b.rt._pending_commands == []
    b.rt.compile(b.bundle)  # a program pulled while the agent shuts down: still no pass will run
    assert b.rt.apply_command(reset("e5")) == {"accepted": False, "reason": R.RESET_STOPPING}
    b.run(2)  # stepped by hand anyway: nothing was left pending, so nothing is pulsed
    assert CLOSE not in pulses(b.writes_since(m)) and b.rt.prog.valves[V2].fault
    assert acks(b) == []
    # Started again, the runtime accepts a reset and its pass carries it out, as ever.
    b.rt.start()
    try:
        assert b.rt.apply_command(reset("e6")) == {"accepted": True}
        deadline = time.monotonic() + 3.0
        while not acks(b, "e6") and time.monotonic() < deadline:
            time.sleep(0.005)
        assert acks(b, "e6") == [("e6", "completed", None)]
        assert pulses(b.writes_since(m)).count(CLOSE) == 1
    finally:
        b.rt.stop(safe_state=False)


def test_the_host_main_holds_refuses_a_reset_once_it_has_stopped(tmp_path):
    """The same through the ProgramHost that main() holds: host.stop() in main's finally, then a reset from MQTT."""
    with open(tmp_path / STATE_FILE, "w") as f:
        json.dump(CLEAN, f)
    clock = SimClock(datetime(2026, 10, 15, 10, 0, 0, tzinfo=TZ))
    io = SimulatedIO()
    for (aid, sk), v in HEALTHY.items():
        io.set_input(aid, sk, v)
    h = ProgramHost(io, RecordingSink(), clock=clock, state_dir=str(tmp_path), node_uid="node_pumphouse")
    h.compile(both_valves(pumphouse()))
    h.step()
    io.set_input(UPS, "sensor.power", False)
    clock.advance(1.0)
    h.step()
    io.set_input(UPS, "sensor.power", True)
    clock.advance(1.0)
    h.step()
    assert h.v3.prog.valves[V2].fault == "supply_lost"
    h.stop(safe_state=True)
    mqtt = _Mqtt()
    n = len(io.log)
    agent_main.answer_program_command(reset("h1"), h, mqtt)
    assert mqtt.acks == [("h1", "failed", R.RESET_STOPPING)]
    assert h.v3._pending_commands == [] and CLOSE not in pulses(io.log[n:])


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
