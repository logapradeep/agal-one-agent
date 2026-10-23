"""A single-pulse valve is closed at start-up and before a shutdown — built in (agent 0.4.4, contracts v1.26.0;
programs/README.md §7.9; the founder's rule of 2026-10-14) — and its words name its port (the founder's answer 7, SF-5).

The founder's rule: a single-pulse (latching DC) valve holds its position without power, so the node CLOSES every one at
start-up and before it shuts down, built in, never a layout rule; if the valve's relay supply or the node's own supply is
interrupted, the valve goes into FAULT and needs a reset before automation resumes. So, path by path:

* a clean shutdown (the agent's own stop — systemd's stop or restart, an update's restart): the safe state, then one close
  pulse per single-pulse valve not in FAULT, recorded (the pulse mark first), ``valve_closed`` (shutdown) — and only THEN
  the clean stop; its supply not sensed present: FAULT, no pulse;
* a clean start: the boot pass sends each valve it vouches for one close pulse, whatever position it recorded,
  ``valve_closed`` (start_up); a run the restart interrupted re-opens it 30 s later;
* an unclean start (no clean-stop record, a pulse in progress, the supply not sensed present): FAULT, no pulse;
* a reload and the switch of language: no pulse.

No hardware: the pump house of the golden vectors (Valve 2 latching — open DO3, close DO4, its supply sensed on DI3 by the
"UPS supply" card), a SimClock, a SimulatedIO.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

import pytest

from agal_one_agent.blocks.io import SimulatedIO
from agal_one_agent.blocks.runtime import CompileError
from agal_one_agent.blocks.v3.runtime import STATE_FILE
from agal_one_agent.blocks.v3.valve import same_configuration
from test_v3_valves import both_valves, pulses
from v3_bench import CLEAN, HEALTHY, PUMP, TZ, UPS, V1, V2, Bench, card, nxt, pumphouse

CLOSE, OPEN = ("valve.close_coil", 100), ("valve.open_coil", 100)


def state(b: Bench) -> dict:
    with open(os.path.join(b.state_dir, STATE_FILE)) as f:
        return json.load(f)


def closes(b: Bench, mark) -> list[str]:
    return [e["valve"]["cause"] for e in b.events_since(mark, "valve_closed")]


def restart(b: Bench):
    """A start of the agent on the same state directory (and the same ports): a new runtime, the bundle compiled."""
    rt = b.new_runtime()
    rt.compile(b.bundle)
    return rt


# ================================================================== a clean start

@pytest.mark.parametrize("position", ["closed", "open"])
def test_a_clean_start_closes_it_whatever_it_recorded(position):
    b = Bench(both_valves(pumphouse()), persisted={"cleanStop": True, "valves": {V2: {"position": position}}},
              start=datetime(2026, 10, 15, 5, 0, 0, tzinfo=TZ))  # an hour before the morning run
    m = b.mark()
    b.step()  # the boot pass
    assert pulses(b.writes_since(m)) == [CLOSE]
    [c] = b.events_since(m, "valve_closed")
    assert c["valve"] == {"assetId": V2, "nodeUid": "node_pumphouse", "cause": "start_up"} and c["assetId"] == V2
    assert b.source_of(c) == "node"
    assert b.events_since(m, "valve_fault") == []
    assert b.rt.prog.valves[V2].position == "closed" and b.rt.prog.valves[V2].fault is None
    assert state(b)["valves"][V2]["position"] == "closed" and state(b)["valves"][V2]["pulse"] is None
    b.run(5)
    assert pulses(b.writes_since(m)) == [CLOSE], "one close pulse, once"


def test_the_start_up_close_goes_after_the_switch_offs_and_before_any_open_pulse():
    """06:00:00 is the boot pass and the morning schedule's start: the close pulse first, the open pulse at the next pass —
    never both coils at once, never both pulses in one pass."""
    b = Bench(both_valves(pumphouse()), start=datetime(2026, 10, 15, 6, 0, 0, tzinfo=TZ))
    m = b.mark()
    b.step()
    w = b.writes_since(m)
    # The switch-offs (the pump), Valve 2's close pulse with them, then the switch-ons (Valve 1's coil, the schedule's).
    assert w == [{"assetId": PUMP, "sourceKey": "device.power", "value": False},
                 {"assetId": V2, "sourceKey": "valve.close_coil", "pulseMs": 100},
                 {"assetId": V1, "sourceKey": "valve.coil", "value": True}]
    b.run(1)
    assert pulses(b.writes_since(m)) == [CLOSE, OPEN]
    assert b.level(V2, "valve.open") is True


def test_a_run_the_restart_interrupted_re_opens_it_30_s_after_the_boot_pass(tmp_path):
    b = Bench(both_valves(pumphouse()), state_dir=str(tmp_path))
    b.step()
    b.until(6, 5)
    assert b.level(V2, "valve.open") is True
    m = b.mark()
    b.rt.stop(safe_state=True)  # an update's restart: systemd's stop, then the new process
    assert pulses(b.writes_since(m)) == [CLOSE] and closes(b, m) == ["shutdown"]
    restart(b)
    m = b.mark()
    b.step()  # the boot pass: closed again (it is closed: harmless), the interrupted run waits 30 s
    assert pulses(b.writes_since(m)) == [CLOSE] and closes(b, m) == ["start_up"]
    b.run(29)
    assert pulses(b.writes_since(m)) == [CLOSE]
    b.run(2)
    assert pulses(b.writes_since(m)) == [CLOSE, OPEN], "after a restart, set as the schedule says (J6)"
    [r] = b.events_since(m, "watering_resumed")
    assert r["watering"]["cause"] == "restart"


def test_the_start_up_close_is_recorded_before_the_pulse(tmp_path):
    """As every pulse: the pulse mark is on disk before the coil moves — a power cut in the middle is pulse_cut_short."""
    seen = []

    class Watching(SimulatedIO):
        def pulse(self, port, ms):
            with open(os.path.join(str(tmp_path), STATE_FILE)) as f:
                seen.append(json.load(f)["valves"][V2]["pulse"])
            super().pulse(port, ms)

    io = Watching()
    b = Bench(load=False, state_dir=str(tmp_path))
    b.io = io
    for (aid, sk), v in HEALTHY.items():
        io.set_input(aid, sk, v)
    b.bundle = both_valves(pumphouse())
    b.rt = b.new_runtime()
    b.rt.compile(b.bundle)
    b.step()
    assert seen == ["close"]


# ================================================================== an unclean start, or no supply

def test_an_unclean_start_faults_it_and_sends_no_pulse():
    b = Bench(both_valves(pumphouse()), persisted={"cleanStop": False, "valves": {V2: {"position": "closed"}}})
    m = b.mark()
    b.step()
    assert pulses(b.writes_since(m)) == [] and closes(b, m) == []
    [f] = b.events_since(m, "valve_fault")
    assert f["valve"]["reason"] == "unclean_boot"
    b.run(10)
    assert pulses(b.writes_since(m)) == [], "it waits for a reset"


@pytest.mark.parametrize("supply", [False, None])
def test_a_supply_not_sensed_at_the_boot_pass_faults_it_and_sends_no_pulse(supply):
    inputs = dict(HEALTHY)
    inputs[(UPS, "sensor.power")] = supply
    b = Bench(both_valves(pumphouse()), inputs=inputs)
    m = b.mark()
    b.step()
    assert pulses(b.writes_since(m)) == [] and closes(b, m) == []
    [f] = b.events_since(m, "valve_fault")
    assert f["valve"]["reason"] == "supply_lost"
    assert f["valve"]["detail"] == "the supply on DI3 (UPS supply) " + ("dropped" if supply is False else "cannot be read")
    assert b.rt.prog.valves[V2].start_close is False
    b.set(UPS, "sensor.power", True)
    b.run(5)
    assert pulses(b.writes_since(m)) == [], "the supply back is not a reset: it stays in FAULT"


def test_a_pulse_in_progress_at_the_stop_is_pulse_cut_short_and_no_pulse():
    b = Bench(both_valves(pumphouse()), persisted={"cleanStop": True, "valves": {V2: {"position": "closed", "pulse": "close"}}})
    m = b.mark()
    b.step()
    assert pulses(b.writes_since(m)) == [] and closes(b, m) == []
    assert b.events_since(m, "valve_fault")[0]["valve"]["reason"] == "pulse_cut_short"


# ================================================================== a clean shutdown

def test_a_clean_stop_closes_it_and_only_then_records_the_clean_stop(tmp_path):
    seen = []

    class Watching(SimulatedIO):
        def pulse(self, port, ms):
            with open(os.path.join(str(tmp_path), STATE_FILE)) as f:
                d = json.load(f)
            seen.append((port.source_key, d["valves"][V2]["pulse"], d["cleanStop"]))
            super().pulse(port, ms)

    io = Watching()
    b = Bench(load=False, state_dir=str(tmp_path))
    b.io = io
    for (aid, sk), v in HEALTHY.items():
        io.set_input(aid, sk, v)
    b.bundle = both_valves(pumphouse())
    b.rt = b.new_runtime()
    b.rt.compile(b.bundle)
    b.step()
    b.until(6, 5)
    seen.clear()
    m = b.mark()
    b.rt.stop(safe_state=True)
    # The pump off first, then the continuous valve, then the close pulse — with its mark on disk and NOT a clean stop yet.
    w = b.writes_since(m)
    assert [(x["assetId"], x["sourceKey"]) for x in w][:2] == [(PUMP, "device.power"), (V1, "valve.coil")]
    assert pulses(w) == [CLOSE]
    assert seen == [("valve.close_coil", "close", False)]
    st = state(b)
    assert st["cleanStop"] is True and st["valves"][V2]["position"] == "closed" and st["valves"][V2]["pulse"] is None
    assert closes(b, m) == ["shutdown"]


def test_a_clean_stop_closes_a_valve_recorded_closed_too():
    b = Bench(both_valves(pumphouse()))
    b.step()
    m = b.mark()
    b.rt.stop(safe_state=True)
    assert pulses(b.writes_since(m)) == [CLOSE] and closes(b, m) == ["shutdown"]


@pytest.mark.parametrize("supply", [False, None])
def test_a_clean_stop_with_no_supply_sensed_faults_it_and_sends_no_pulse(supply):
    b = Bench(both_valves(pumphouse()))
    b.step()
    b.io.set_input(UPS, "sensor.power", supply)  # gone between two passes: the stop reads it itself
    m = b.mark()
    b.rt.stop(safe_state=True)
    assert pulses(b.writes_since(m)) == [] and closes(b, m) == []
    [f] = b.events_since(m, "valve_fault")
    assert f["valve"]["reason"] == "supply_lost"
    assert f["valve"]["detail"].startswith("Valve 2's supply on DI3 (UPS supply) ")
    assert state(b)["cleanStop"] is True and state(b)["valves"][V2]["position"] == "fault"
    # The next start keeps it in FAULT: no pulse, whatever the supply says then.
    b.io.set_input(UPS, "sensor.power", True)
    restart(b)
    m = b.mark()
    b.step()
    assert pulses(b.writes_since(m)) == [] and closes(b, m) == []
    assert b.rt.prog.valves[V2].fault == "supply_lost"


def test_a_valve_in_fault_gets_no_pulse_at_the_stop_and_stays_in_fault():
    b = Bench(both_valves(pumphouse()), persisted=False)  # a first start: position_unknown
    b.step()
    assert b.rt.prog.valves[V2].fault == "position_unknown"
    m = b.mark()
    b.rt.stop(safe_state=True)
    assert pulses(b.writes_since(m)) == [] and closes(b, m) == []
    restart(b)
    b.step()
    assert b.rt.prog.valves[V2].fault == "position_unknown"


class _CloseFails(SimulatedIO):
    armed = False

    def pulse(self, port, ms):
        if self.armed and (port.asset_id, port.source_key) == (V2, "valve.close_coil"):
            raise OSError("the relay driver did not answer")
        super().pulse(port, ms)


def test_a_close_pulse_that_fails_at_the_stop_is_pulse_cut_short_and_stays_in_fault():
    io = _CloseFails()
    b = Bench(load=False)
    b.io = io
    for (aid, sk), v in HEALTHY.items():
        io.set_input(aid, sk, v)
    b.bundle = both_valves(pumphouse())
    b.rt = b.new_runtime()
    b.rt.compile(b.bundle)
    b.step()
    io.armed = True
    m = b.mark()
    b.rt.stop(safe_state=True)
    [f] = b.events_since(m, "valve_fault")
    assert f["valve"]["reason"] == "pulse_cut_short" and f["valve"]["detail"] == "the close pulse on DO4 could not be sent"
    assert closes(b, m) == []
    io.armed = False
    restart(b)
    m = b.mark()
    b.step()
    assert pulses(b.writes_since(m)) == [] and b.rt.prog.valves[V2].fault == "pulse_cut_short"


def test_a_continuous_valve_gets_no_pulse_and_closes_by_its_coil():
    b = Bench(pumphouse())
    b.step()
    b.until(6, 5)
    assert b.physical(V1, "valve.coil") is True
    m = b.mark()
    b.rt.stop(safe_state=True)
    assert b.physical(V1, "valve.coil") is False  # its coil off: closed by itself
    assert all(w["assetId"] != V1 for w in b.writes_since(m) if "pulseMs" in w)  # never a pulse for Valve 1
    assert pulses(b.writes_since(m)) == [CLOSE]  # the single-pulse Valve 2 of the same node


# ================================================================== neither a start nor a shutdown

def test_a_reload_sends_no_close_pulse():
    b = Bench(both_valves(pumphouse()))
    b.step()
    b.until(6, 5)
    m = b.mark()
    b.rt.compile(nxt(b.rt.bundle))
    b.run(2)
    assert pulses(b.writes_since(m)) == [] and closes(b, m) == []
    assert b.level(V2, "valve.open") is True


def test_a_stop_without_the_safe_state_sends_none():
    """The switch of language stops the old runtime without its safe state (programs/README.md §7.5a): no pulse."""
    b = Bench(both_valves(pumphouse()))
    b.step()
    b.until(6, 5)
    m = b.mark()
    b.rt.stop(safe_state=False)
    assert pulses(b.writes_since(m)) == [] and closes(b, m) == []
    assert state(b)["valves"][V2]["position"] == "open"


def test_the_switch_of_language_takes_over_without_a_start_up_close():
    b = Bench(both_valves(pumphouse()), load=False)
    b.rt.take_over_language(b.bundle, {}, {})
    m = b.mark()
    b.step()
    assert pulses(b.writes_since(m)) == [] and closes(b, m) == []


# ================================================================== the configuration and the port ids

def test_same_configuration_leaves_out_a_port_id_stated_on_one_side_only():
    a = {"actuation": "latching_dc", "open": "valve.open_coil", "close": "valve.close_coil", "pulseMs": 100}
    b = dict(a, openPortId="DO3", closePortId="DO4", supplyPortId="DI3")
    assert same_configuration(a, b) and same_configuration(json.dumps(a, sort_keys=True), b)
    assert not same_configuration(dict(b, openPortId="DO5"), b), "a coil moved to another port is a change"
    assert not same_configuration(dict(a, pulseMs=150), b)
    assert not same_configuration("another configuration", b)


def test_a_state_agent_0_4_3_wrote_is_vouched_for_on_a_bundle_that_names_the_ports(tmp_path):
    """0.4.3 → 0.4.4: the state file's config is the signature of a valve entry with no port ids; the bundle now names
    them. Not a changed configuration: no FAULT, and the clean start closes it."""
    bundle = both_valves(pumphouse())
    old = {k: v for k, v in card(bundle, V2)["valve"].items() if k not in ("openPortId", "closePortId", "supplyPortId")}
    persisted = {"cleanStop": True, "valves": {V2: {"position": "open", "pulse": None, "config": json.dumps(old, sort_keys=True)}}}
    b = Bench(bundle, persisted=persisted, state_dir=str(tmp_path))
    m = b.mark()
    b.step()
    assert b.events_since(m, "valve_fault") == [] and pulses(b.writes_since(m)) == [CLOSE]
    # …while a changed pulse length still is one.
    persisted["valves"][V2]["config"] = json.dumps(dict(old, pulseMs=250), sort_keys=True)
    b2 = Bench(bundle, persisted=persisted)
    b2.step()
    assert b2.rt.prog.valves[V2].fault == "position_unknown"


def test_a_reload_that_only_adds_the_port_ids_is_not_a_change():
    bundle = both_valves(pumphouse())
    for k in ("openPortId", "closePortId", "supplyPortId"):
        card(bundle, V2)["valve"].pop(k)
    b = Bench(bundle)
    b.step()
    b.until(6, 5)
    m = b.mark()
    b.rt.compile(nxt(b.rt.bundle, lambda x: card(x, V2)["valve"].update(openPortId="DO3", closePortId="DO4", supplyPortId="DI3")))
    b.run(2)
    assert b.events_since(m, "valve_fault") == [] and b.level(V2, "valve.open") is True


def test_a_supply_drop_names_the_port_and_the_card():
    b = Bench(both_valves(pumphouse()))
    b.step()
    b.until(6, 1)
    m = b.mark()
    b.set(UPS, "sensor.power", False)
    b.run(1)
    [f] = b.events_since(m, "valve_fault")
    assert f["valve"]["detail"] == "the supply on DI3 (UPS supply) dropped"


def test_a_coil_the_card_does_not_have_is_refused_naming_the_port():
    bundle = both_valves(pumphouse())
    del card(bundle, V2)["ports"]["valve.close_coil"]
    with pytest.raises(CompileError, match=r"valve Valve 2 \(asset_valve002\): coil DO4 \('valve\.close_coil'\) is not an output port"):
        Bench(bundle)


def test_a_coil_entry_with_no_direction_is_refused_naming_the_port():
    bundle = both_valves(pumphouse())
    del card(bundle, V2)["ports"]["valve.close_coil"]["direction"]
    with pytest.raises(CompileError, match=r"refused: DO4 of Valve 2 \(valve\.close_coil\) states no direction"):
        Bench(bundle)


def test_a_switch_of_a_coil_names_the_port():
    b = Bench(both_valves(pumphouse()))
    b.step()
    assert b.rt.apply_command({"type": "pauseAutomation"})["accepted"]
    b.run(1)
    r = b.rt.apply_command({"type": "setPower", "assetId": V2, "sourceKey": "valve.open_coil", "value": 1})
    assert r == {"accepted": False, "reason": "refused: DO3 of Valve 2 is driven by its valve driver — switch valve.open"}


def test_without_the_port_ids_the_words_are_agent_0_4_3s():
    bundle = both_valves(pumphouse())
    for k in ("openPortId", "closePortId", "supplyPortId"):
        card(bundle, V2)["valve"].pop(k)
    inputs = dict(HEALTHY)
    inputs[(UPS, "sensor.power")] = False
    b = Bench(bundle, inputs=inputs)
    b.step()
    assert b.events("valve_fault")[0]["valve"]["detail"] == "the supply on sensor.power of card asset_ups00001 dropped"
    r = b.rt.apply_command({"type": "resetValve", "assetId": V2})
    assert r == {"accepted": False, "reason": "refused: the valve's supply on line 13 of pinctrl-bcm2835 is not present"}


def test_clean_state_of_the_vectors_is_a_clean_start():
    """The golden vectors start from {cleanStop: true, Valve 2 closed} (CLEAN): their first pass closes Valve 2."""
    assert CLEAN == {"cleanStop": True, "valves": {V2: "closed"}}
    b = Bench(both_valves(pumphouse()))
    m = b.mark()
    b.step()
    assert closes(b, m) == ["start_up"]


# ================================================================== the agent's own stop and start (an update's restart)

def test_the_hosts_stop_and_start_close_it_as_main_does_at_sigterm(tmp_path):
    """main(): SIGTERM (systemd's stop or restart — an update's restart too) ends the main loop, and its `finally` stops
    the program host with the safe state; the next process loads the stored bundle into a new host. Both ends close it."""
    import inspect

    from agal_one_agent import main as agent_main
    from agal_one_agent.blocks.clock import SimClock
    from agal_one_agent.blocks.host import ProgramHost
    from agal_one_agent.blocks.runtime import RecordingSink

    src = inspect.getsource(agent_main.main)
    assert "signal.signal(signal.SIGTERM, shutdown)" in src and "runtime.stop(safe_state=True)" in src

    clock = SimClock(datetime(2026, 10, 15, 5, 0, 0, tzinfo=TZ))
    io = SimulatedIO()
    for (aid, sk), v in HEALTHY.items():
        io.set_input(aid, sk, v)
    with open(os.path.join(str(tmp_path), STATE_FILE), "w") as f:
        json.dump(CLEAN, f)
    sink = RecordingSink()
    host = ProgramHost(io, sink, clock=clock, state_dir=str(tmp_path), node_uid="node_pumphouse")
    host.compile(both_valves(pumphouse()))
    host.step()
    mark = len(io.log)
    host.stop(safe_state=True)
    assert [(w["sourceKey"], w["pulseMs"]) for w in io.log[mark:] if "pulseMs" in w] == [CLOSE]
    assert [e["valve"]["cause"] for e in sink.events if e["type"] == "valve_closed"] == ["start_up", "shutdown"]
    host2 = ProgramHost(io, sink, clock=clock, state_dir=str(tmp_path), node_uid="node_pumphouse")
    host2.compile(both_valves(pumphouse()))
    mark = len(io.log)
    host2.step()
    assert [(w["sourceKey"], w["pulseMs"]) for w in io.log[mark:] if "pulseMs" in w] == [CLOSE]
    assert [e["valve"]["cause"] for e in sink.events if e["type"] == "valve_closed"] == ["start_up", "shutdown", "start_up"]
