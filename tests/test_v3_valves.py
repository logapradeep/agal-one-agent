"""The valve drivers (contracts v1.15.0, programs/README.md §7.9) — no hardware: the pump house of the golden
vectors. Valve 1 is continuous (AC, normally closed) on one output; Valve 2 is single-pulse DC latching with
its relay supply sensed on the UPS input. In these tests the morning run (06:00, 30 min) opens both."""

from __future__ import annotations

import json
import os

import pytest

from agal_one_agent.blocks.runtime import CompileError
from agal_one_agent.blocks.v3.runtime import STATE_FILE
from v3_bench import PUMP, UPS, V1, V2, Bench, asset_var, card, nxt, part, pumphouse, rule_block

PART = {"containerKind": "field", "containerId": "field_north01", "slot": "instruction", "partId": "pumphouse"}
RULE1 = {"containerKind": "field", "containerId": "field_north01", "slot": 1}


def both_valves(bundle: dict) -> dict:
    prog = part(bundle, "pumphouse")["program"]
    prog["variables"].append(asset_var("valve_2", V2, "write", "valve.open"))
    s = prog["schedules"][0]
    s["actions"].append({"type": "set", "variable": "valve_2", "value": True})
    s["endActions"].append({"type": "set", "variable": "valve_2", "value": False})
    return bundle


def fault_interlock(bundle: dict) -> dict:
    """Rule slot 3 of rule-edit-swap.json: stop the pump when Valve 2 faults — it reads the valve's built-in."""
    bundle["rules"].append(rule_block(3, {"name": "Stop the pump when Valve 2 faults", "variables": [
        asset_var("pump", PUMP, "stop", "device.power"),
        asset_var("valve_2_fault", V2, "read", builtin="valve_fault")],
        "rules": [{"id": "pump_off_on_valve_2_fault", "kind": "when", "when": {"condition": "valve_2_fault"},
                   "actions": [{"type": "set", "variable": "pump", "value": False}]}]}))
    return bundle


def pulses(writes, aid=V2):
    return [(w["sourceKey"], w["pulseMs"]) for w in writes if w["assetId"] == aid and "pulseMs" in w]


def state(b: Bench) -> dict:
    with open(os.path.join(b.state_dir, STATE_FILE)) as f:
        return json.load(f)


# ------------------------------------------------------------------ continuous

def test_continuous_normally_closed_is_powered_open():
    b = Bench(pumphouse())
    b.step()
    assert b.physical(V1, "valve.coil") is False
    b.until(6, 0)
    assert b.physical(V1, "valve.coil") is True and b.level(V1, "valve.open") is True
    b.until(6, 30)
    assert b.physical(V1, "valve.coil") is False


def test_continuous_normally_open_is_inverted_by_the_driver():
    bundle = pumphouse()
    card(bundle, V1)["valve"]["normallyClosed"] = False
    b = Bench(bundle)
    b.step()
    assert b.physical(V1, "valve.coil") is True  # powered = closed
    b.until(6, 0)
    assert b.physical(V1, "valve.coil") is False and b.level(V1, "valve.open") is True


def test_a_continuous_valve_never_faults():
    b = Bench(pumphouse(), persisted=False)  # a first start
    b.step()
    st = {v["assetId"]: v for v in b.rt.status_extra()["valves"]}
    assert st[V1] == {"assetId": V1, "state": "closed"}
    assert st[V2]["state"] == "fault"


# -------------------------------------------------------------------- latching

def test_latching_pulses_open_then_close_and_never_both():
    b = Bench(both_valves(pumphouse()))
    m = b.mark()
    b.step()
    b.until(6, 0)
    assert pulses(b.writes_since(m)) == [("valve.open_coil", 100)]
    assert b.level(V2, "valve.open") is True
    b.until(6, 30)
    assert pulses(b.writes_since(m)) == [("valve.open_coil", 100), ("valve.close_coil", 100)]
    # The coils are only ever pulsed, one at a time: never written as a level.
    assert [w for w in b.writes_since(m) if w["assetId"] == V2 and "value" in w] == []
    assert state(b)["valves"][V2]["position"] == "closed" and state(b)["valves"][V2]["pulse"] is None


def test_latching_pulse_length_is_the_cards():
    bundle = both_valves(pumphouse())
    card(bundle, V2)["valve"]["pulseMs"] = 250
    b = Bench(bundle, persisted={"cleanStop": True, "valves": {V2: {"position": "closed", "config": None}}})
    m = b.mark()
    b.step()
    b.until(6, 0)
    assert pulses(b.writes_since(m)) == [("valve.open_coil", 250)]


def test_the_position_is_persisted_and_a_clean_restart_keeps_it(tmp_path):
    b = Bench(both_valves(pumphouse()), state_dir=str(tmp_path))
    b.step()
    b.until(6, 5)
    assert state(b)["valves"][V2]["position"] == "open"
    b.rt.stop(safe_state=True)
    assert state(b)["cleanStop"] is True
    rt = b.new_runtime()
    m = b.mark()
    rt.compile(b.bundle)
    st = {v["assetId"]: v for v in rt.status_extra()["valves"]}
    assert st[V2]["state"] == "open"  # vouched for: no FAULT after a clean stop
    b.step()
    # Outputs start off after a restart: the open valve gets one close pulse at the boot pass.
    assert pulses(b.writes_since(m)) == [("valve.close_coil", 100)]
    assert b.events_since(m, "valve_fault") == []


@pytest.mark.parametrize("persisted,reason", [
    ({"cleanStop": False, "valves": {V2: "closed"}}, "unclean_boot"),
    ({"cleanStop": True, "valves": {V2: {"position": "closed", "pulse": "open"}}}, "pulse_cut_short"),
    (False, "position_unknown"),
    ({"cleanStop": True, "valves": {V2: {"position": "closed", "config": "another configuration"}}}, "position_unknown"),
])
def test_fault_at_boot_whenever_the_node_cannot_vouch_for_the_position(persisted, reason):
    b = Bench(both_valves(pumphouse()), persisted=persisted)
    m = b.mark()
    b.step()
    [f] = b.events_since(m, "valve_fault")
    assert f["valve"]["reason"] == reason and f["valve"]["assetId"] == V2
    assert {"containerKind": "field", "containerId": "field_north01", "slot": "instruction", "partId": "pumphouse"} in f["valve"]["heldBlocks"]
    assert b.source_of(f) == "node"
    assert pulses(b.writes_since(m)) == []  # no pulse is sent to a valve in FAULT


def test_supply_drop_puts_it_in_fault_and_only_the_blocks_that_use_it_hold():
    b = Bench(fault_interlock(both_valves(pumphouse())))
    b.step()
    b.until(6, 1)
    assert b.pump_on() and b.level(V1, "valve.open") and b.level(V2, "valve.open")
    m = b.mark()
    b.set(UPS, "sensor.power", False)
    b.run(1)
    [f] = b.events_since(m, "valve_fault")
    assert f["valve"]["reason"] == "supply_lost"
    held = f["valve"]["heldBlocks"]
    assert PART in held and RULE1 in held  # the part writes Valve 2; rule 1 reads its position
    assert {"containerKind": "field", "containerId": "field_north01", "slot": 3} not in held  # reads only its built-in
    # Held blocks' outputs off, motors first; the faulted valve itself is not driven.
    w = b.writes_since(m)
    assert [x["assetId"] for x in w if x.get("value") is False] == [PUMP, V1]
    assert pulses(w) == []
    keys = {(h["key"], h["reason"]) for h in b.rt.status_extra()["held"]}
    assert keys == {("field:field_north01/instruction/pumphouse", "valve_fault"), ("field:field_north01/rule/1", "valve_fault")}
    # The run goes on by its clock while it holds; nothing re-opens.
    b.run(30)
    assert b.pump_on() is False and b.level(V1, "valve.open") is False


def test_a_rule_that_reacts_to_the_fault_keeps_running_and_acts():
    bundle = both_valves(pumphouse())
    bundle["rules"].append(rule_block(4, {"variables": [asset_var("faulted", V2, "read", builtin="valve_fault"),
                                                        {"name": "say", "kind": "alert", "type": "string"}],
                                          "rules": [{"id": "tell", "kind": "when", "when": {"condition": "faulted"},
                                                     "actions": [{"type": "alert", "text": "Valve 2 is in fault", "severity": "critical"}]}]}))
    b = Bench(bundle)
    b.step()
    b.until(6, 1)
    b.set(UPS, "sensor.power", None)  # cannot be read: supply_lost too
    b.run(1)
    assert b.events("valve_fault")[0]["valve"]["reason"] == "supply_lost"
    assert [a["text"] for a in b.sink.alerts] == ["Valve 2 is in fault"]


def test_reset_is_refused_while_the_supply_is_not_sensed_and_idempotent_when_not_in_fault():
    b = Bench(both_valves(pumphouse()))
    b.step()
    # Not in fault: not refused — its pass answers completed and sends no pulse (contracts v1.15.2 item 15; the cloud
    # refuses not_in_fault first, so this is a reset an earlier one already did).
    m = b.mark()
    assert b.rt.apply_command({"type": "resetValve", "assetId": V2, "commandId": "c0"}) == {"accepted": True}
    b.run(1)
    assert b.sink.command_acks == [{"commandId": "c0", "status": "completed", "error": None}]
    assert pulses(b.writes_since(m)) == [] and b.events_since(m, "valve_reset") == []
    b.set(UPS, "sensor.power", False)
    b.run(1)
    r = b.rt.apply_command({"type": "resetValve", "assetId": V2})
    assert r == {"accepted": False, "reason": "refused: the valve's supply on line 13 of pinctrl-bcm2835 is not present"}
    assert b.rt.apply_command({"type": "resetValve", "assetId": V1})["accepted"] is False  # not a latching valve


def test_reset_sends_one_close_pulse_and_the_run_carries_on_by_its_own_clock():
    b = Bench(both_valves(pumphouse()))
    b.step()
    b.until(6, 1)
    b.set(UPS, "sensor.power", False)
    b.run(10)
    b.set(UPS, "sensor.power", True)
    b.run(5)
    assert b.level(V1, "valve.open") is False  # still held: the supply is back, the FAULT stays until a reset
    m = b.mark()
    actor = {"uid": "u_owner01", "via": "app"}
    assert b.rt.apply_command({"type": "resetValve", "assetId": V2, "commandId": "c1", "actor": actor})["accepted"]
    b.run(1)
    [r] = b.events_since(m, "valve_reset")
    assert {k: r["valve"]["by"][k] for k in ("uid", "via")} == actor and r["valve"]["by"]["at"].startswith("2026-10-15T06:01:16")
    assert PART in r["valve"]["resumedBlocks"] and RULE1 in r["valve"]["resumedBlocks"]
    assert pulses(b.writes_since(m))[0] == ("valve.close_coil", 100)
    # The held part resumes under its own logic: before its planned end (06:30) it re-opens what it had open.
    [w] = b.events_since(m, "watering_resumed")
    assert w["watering"]["cause"] == "valve_reset"
    assert w["watering"]["remainingSeconds"] == pytest.approx((29 * 60) - 16, abs=2)
    assert pulses(b.writes_since(m)) == [("valve.close_coil", 100), ("valve.open_coil", 100)]
    assert b.level(V1, "valve.open") is True
    b.run(3)
    assert b.pump_on()  # rule 1 starts the pump 3 s after a valve opens, as at any start
    b.until(6, 30)
    assert b.pump_on() is False and b.level(V2, "valve.open") is False  # its planned end: no time added


def test_reset_after_the_planned_end_ends_the_run_and_logs_it():
    b = Bench(both_valves(pumphouse()))
    b.step()
    b.until(6, 10)
    b.set(UPS, "sensor.power", False)
    b.until(6, 40)
    b.set(UPS, "sensor.power", True)
    b.run(1)
    m = b.mark()
    assert b.rt.apply_command({"type": "resetValve", "assetId": V2})["accepted"]
    b.run(1)
    assert b.events_since(m, "watering_resumed") == []
    assert b.events_since(m, "run_cut_short")
    [n] = b.events_since(m, "watering_not_finished")
    assert n["watering"]["reason"] == "valve_fault"
    b.run(5)
    assert b.level(V1, "valve.open") is False and b.level(V2, "valve.open") is False and b.pump_on() is False


def test_a_faulted_valve_cannot_be_switched_and_a_coil_never_by_a_command():
    b = Bench(both_valves(pumphouse()), persisted=False)
    b.step()
    assert b.rt.apply_command({"type": "pauseAutomation"})["accepted"]
    b.run(1)
    r = b.rt.apply_command({"type": "setPower", "assetId": V2, "sourceKey": "valve.open", "value": 1})
    assert r["accepted"] is False and "in fault" in r["reason"]
    r = b.rt.apply_command({"type": "setPower", "assetId": V2, "sourceKey": "valve.open_coil", "value": 1})
    assert r["accepted"] is False and "valve driver" in r["reason"]


def test_a_block_never_names_a_coil():
    bundle = pumphouse()
    part(bundle, "pumphouse")["program"]["variables"][0]["asset"]["sourceKey"] = "valve.coil"
    with pytest.raises(CompileError, match="valve.open"):
        Bench(bundle)


def test_a_latching_valve_without_its_sensed_supply_is_refused():
    bundle = pumphouse()
    del card(bundle, V2)["valve"]["supply"]
    with pytest.raises(CompileError, match="sensed supply"):
        Bench(bundle)


def test_a_changed_configuration_in_a_reload_puts_it_in_fault():
    b = Bench(both_valves(pumphouse()))
    b.step()
    b.run(5)
    m = b.mark()
    b.rt.compile(nxt(b.rt.bundle, lambda x: card(x, V2)["valve"].update(pulseMs=150)))
    b.run(1)
    [f] = b.events_since(m, "valve_fault")
    assert f["valve"]["reason"] == "position_unknown"


def test_a_reload_keeps_the_fault_and_the_position():
    b = Bench(both_valves(pumphouse()))
    b.step()
    b.until(6, 1)
    b.set(UPS, "sensor.power", False)
    b.run(1)
    b.rt.compile(nxt(b.rt.bundle))
    b.run(1)
    assert b.rt.prog.valves[V2].fault == "supply_lost"
    assert len(b.events("valve_fault")) == 1  # not announced again
