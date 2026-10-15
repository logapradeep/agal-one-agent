"""The motor guard (contracts v1.15.0, programs/README.md §7.11) — built in, always on. No hardware: the
pump house of the golden vectors on a SimClock and a SimulatedIO. The morning run opens Valve 1 at 06:00;
rule slot 1 starts the pump 3 s later; the guard ignores the inrush for 3 s, learns its baseline from the
next 10 s and commits it after the run has gone a minute more without a trip (06:01:16)."""

from __future__ import annotations

from datetime import datetime

import pytest

from agal_one_agent.blocks.runtime import CompileError
from agal_one_agent.blocks.v3.guard import MotorGuard
from v3_bench import PUMP, TZ, V1, Bench, card, nxt, pumphouse, rule

GUARD = {"output": "device.power", "current": ["sensor.current"], "ratedCurrentA": 7.5, "phaseType": "single_phase",
         "settings": {"dryRun": {"dropPct": 30, "holdSeconds": 5}, "overload": {"multiple": 1.25, "holdSeconds": 5},
                      "inrushIgnoreSeconds": 3, "restartLockoutSeconds": 300}}


def morning(b: Bench, current: float = 6.8) -> None:
    """Run to 06:00:03 (the pump starts), then draw `current` from 06:00:04."""
    b.until(6, 0, 3)
    assert b.pump_on() and b.physical(PUMP, "device.power") is True
    b.set(PUMP, "sensor.current", current)


# ------------------------------------------------------------------ pure guard

def test_guard_alone_dry_run_after_its_baseline():
    g = MotorGuard(PUMP, GUARD)
    readings = {"sensor.current": 0.0}
    t = 100.0
    g.started(t)
    wall = datetime(2026, 10, 15, 6, 0, tzinfo=TZ)
    for s in range(1, 90):  # inrush, then 10 s of samples, then the minute: the baseline commits
        readings["sensor.current"] = 6.8 if s >= 1 else 0.0
        trip, learned = g.evaluate(t + s, wall, readings.get)
        assert trip is None
    assert g.baseline == pytest.approx(6.8)
    readings["sensor.current"] = 3.0  # < 6.8 × 0.7
    trips = [g.evaluate(t + 90 + s, wall, readings.get)[0] for s in range(0, 6)]
    assert [x.reason for x in trips if x] == ["dry_run"]
    assert g.tripped and g.reason == "dry_run" and g.locked_out(t + 96)


def test_guard_alone_a_broken_reading_is_never_acted_on():
    g = MotorGuard(PUMP, GUARD)
    g.started(0.0)
    wall = datetime(2026, 10, 15, 6, 0, tzinfo=TZ)
    for s in range(1, 30):
        assert g.evaluate(float(s), wall, lambda k: None)[0] is None
    assert not g.tripped


def test_the_lockout_is_never_under_30_seconds():
    g = MotorGuard(PUMP, dict(GUARD, settings=dict(GUARD["settings"], restartLockoutSeconds=5)))
    assert g.lockout_seconds == 30


# ------------------------------------------------------------- in the runtime

def test_dry_run_trips_after_the_baseline_ends_the_run_and_locks_out():
    b = Bench(pumphouse())
    morning(b)
    b.until(6, 2)
    assert b.rt.prog.guards[PUMP].baseline == pytest.approx(6.8)
    m = b.mark()
    b.set(PUMP, "sensor.current", 3.0)
    b.run(5)
    assert b.pump_on() is True  # held 5 s
    b.run(1)
    assert b.pump_on() is False and b.physical(PUMP, "device.power") is False
    [trip] = b.events_since(m, "guard_tripped")
    g = trip["guard"]
    assert g["reason"] == "dry_run" and g["measured"] == pytest.approx(3.0) and g["limit"] == pytest.approx(6.8 * 0.7)
    assert g["lockoutSeconds"] == 300 and g["baselineA"] == pytest.approx(6.8)
    assert g["endedBlocks"] == [{"containerKind": "field", "containerId": "field_north01", "slot": "instruction", "partId": "pumphouse"}]
    assert b.source_of(trip) == "node"
    # The trip ended the run of the block that had the pump on: Valve 1 closed, the water was not delivered.
    assert b.level(V1, "valve.open") is False
    [short] = b.events_since(m, "watering_not_finished")
    assert short["watering"]["reason"] == "guard_tripped" and short["watering"]["plannedSeconds"] == 1800
    # Motor first, then the valve.
    offs = [w for w in b.writes_since(m) if w.get("value") is False]
    assert [w["assetId"] for w in offs] == [PUMP, V1]
    assert b.rt.status_extra()["guards"][0]["tripped"] is True


def test_dry_run_does_not_trip_before_the_baseline_is_learned():
    b = Bench(pumphouse())
    morning(b, current=0.5)  # dry from the start: nothing to compare against
    b.until(6, 1, 0)
    assert b.pump_on() and b.events("guard_tripped") == []


def test_overload_against_the_rated_current():
    b = Bench(pumphouse())
    morning(b, current=10.0)  # > 1.25 × 7.5 = 9.375
    b.until(6, 0, 10)
    assert b.pump_on() is True  # inrush to 06:00:06, then held 5 s
    b.until(6, 0, 11)
    assert b.pump_on() is False
    assert b.events("guard_tripped")[0]["guard"]["reason"] == "overload"
    assert b.events("guard_tripped")[0]["guard"]["limit"] == pytest.approx(9.375)
    # never learnt from a run that tripped
    assert b.rt.prog.guards[PUMP].baseline is None


def test_overload_without_a_rated_current_waits_for_the_learned_running_current():
    bundle = pumphouse()
    del card(bundle, PUMP)["guard"]["ratedCurrentA"]
    b = Bench(bundle)
    morning(b, current=20.0)
    b.until(6, 1, 15)
    assert b.pump_on() and b.events("guard_tripped") == []  # no reference yet: overload does not trip
    assert b.rt.prog.guards[PUMP].baseline is None
    b.run(1)  # 06:01:16 — a minute after the 10 s of samples: learnt as the running current
    assert b.rt.prog.guards[PUMP].baseline == pytest.approx(20.0)


def test_overload_without_a_rated_current_uses_the_learned_running_current():
    bundle = pumphouse()
    del card(bundle, PUMP)["guard"]["ratedCurrentA"]
    b = Bench(bundle)
    morning(b, current=6.8)
    b.until(6, 2)
    assert b.rt.prog.guards[PUMP].baseline == pytest.approx(6.8)
    b.set(PUMP, "sensor.current", 6.8 * 1.3)  # > 1.25 × 6.8
    b.run(5)
    assert b.pump_on() and b.events("guard_tripped") == []  # held 5 s
    b.run(1)
    trips = b.events("guard_tripped")
    assert trips and trips[0]["guard"]["reason"] == "overload" and trips[0]["guard"]["limit"] == pytest.approx(6.8 * 1.25)


def test_nothing_is_learnt_from_a_run_that_draws_no_current_the_next_run_learns():
    b = Bench(pumphouse())
    morning(b, current=0.0)  # the pump does not draw: not a running current
    b.until(6, 2)
    assert b.rt.prog.guards[PUMP].baseline is None
    b.until(6, 31)  # the run ends
    b.until(6, 0, 3, day=16)
    b.set(PUMP, "sensor.current", 6.8)
    b.until(6, 2, 0, day=16)
    assert b.rt.prog.guards[PUMP].baseline == pytest.approx(6.8)


def test_inrush_is_not_judged():
    b = Bench(pumphouse())
    b.until(6, 0, 3)
    b.set(PUMP, "sensor.current", 40.0)  # a start
    b.run(2)  # 06:00:05, inside the 3 s window
    b.set(PUMP, "sensor.current", 6.8)
    b.until(6, 1, 0)
    assert b.pump_on() and b.events("guard_tripped") == []


def test_nothing_restarts_the_motor_during_the_lockout_not_even_a_rule_that_keeps_asking():
    bundle = pumphouse()
    r1 = rule(bundle, 1)["program"]["rules"][0]
    r1["when"] = {"condition": "true", "retriggerSeconds": 1}  # asks for the pump every second, whatever the valves
    r1["elseActions"] = []
    b = Bench(bundle)
    b.step()
    b.set(PUMP, "sensor.current", 10.0)
    b.run(12)  # inrush 3 s + 5 s held: overload
    assert b.events("guard_tripped") and b.pump_on() is False
    m = b.mark()
    b.run(299 - 4)  # inside the 300 s lockout
    assert [w for w in b.writes_since(m) if w["assetId"] == PUMP] == []
    assert b.physical(PUMP, "device.power") is False
    b.set(PUMP, "sensor.current", 6.8)
    b.run(10)  # the lockout ended: the rule's own logic starts it again
    assert b.physical(PUMP, "device.power") is True


def test_a_command_cannot_start_a_locked_out_motor():
    b = Bench(pumphouse())
    morning(b, current=10.0)
    b.until(6, 0, 12)
    assert b.events("guard_tripped")
    assert b.rt.apply_command({"type": "pauseAutomation", "commandId": "p1"})["accepted"]
    b.run(1)
    r = b.rt.apply_command({"type": "setPower", "assetId": PUMP, "sourceKey": "device.power", "value": 1})
    assert r["accepted"] is False and r["reason"].startswith("refused: Borewell pump is locked out after a guard trip (overload)")


def test_a_motor_is_never_compiled_without_its_guard():
    bundle = pumphouse()
    del card(bundle, PUMP)["guard"]
    with pytest.raises(CompileError, match="never compiled without one"):
        Bench(bundle)


def test_a_block_cannot_write_around_the_guard():
    bundle = pumphouse()
    card(bundle, PUMP)["ports"]["device.star"] = {"kind": "gpio", "chip": "pinctrl-bcm2835", "line": 24, "direction": "out"}
    v = rule(bundle, 1)["program"]["variables"][0]
    v["asset"]["sourceKey"] = "device.star"
    with pytest.raises(CompileError, match="not the output its guard supervises"):
        Bench(bundle)


def test_a_reload_never_resets_the_guard_and_a_restart_keeps_its_lockout_and_baseline(tmp_path):
    b = Bench(pumphouse(), state_dir=str(tmp_path))
    morning(b)
    b.until(6, 2)
    b.set(PUMP, "sensor.current", 3.0)
    b.run(6)
    g = b.rt.prog.guards[PUMP]
    assert g.tripped and g.baseline == pytest.approx(6.8)
    b.rt.compile(nxt(b.rt.bundle))  # a reload
    b.run(1)
    g = b.rt.prog.guards[PUMP]
    assert g.tripped and g.locked_out(b.clock.monotonic()) and g.baseline == pytest.approx(6.8)
    b.rt.stop(safe_state=True)
    b.run(0)
    rt = b.new_runtime()  # a restart
    rt.compile(b.rt.bundle if b.rt.bundle else pumphouse(3))
    g = rt.prog.guards[PUMP]
    assert g.tripped and g.reason == "dry_run" and g.locked_out(b.clock.monotonic()) and g.baseline == pytest.approx(6.8)


def three_phase(bundle: dict, phase_sense: bool = True, currents: int = 3, voltage: bool = False) -> dict:
    c = card(bundle, PUMP)
    g = c["guard"]
    g["phaseType"] = "three_phase"
    if phase_sense:
        for i, (ph, line) in enumerate((("r", 20), ("y", 21), ("b", 16))):
            c["ports"][f"sensor.phase_{ph}"] = {"kind": "gpio", "chip": "pinctrl-bcm2835", "line": line, "direction": "in"}
        g["phaseSense"] = ["sensor.phase_r", "sensor.phase_y", "sensor.phase_b"]
    if currents == 3:
        for ch, ph in ((1, "y"), (2, "b")):
            c["ports"][f"sensor.current_{ph}"] = {"kind": "i2c", "busId": 1, "addr": 72, "channel": ch, "driver": "ads1115",
                                                  "measure": {"mode": "ac_rms", "windowMs": 100}, "unit": "A"}
        g["current"] = ["sensor.current", "sensor.current_y", "sensor.current_b"]
    if voltage:
        c["ports"]["sensor.voltage"] = {"kind": "i2c", "busId": 1, "addr": 72, "channel": 3, "driver": "ads1115", "unit": "V"}
        g["voltage"] = "sensor.voltage"
        g["settings"]["voltage"] = {"minV": 190, "maxV": 265, "holdSeconds": 5}
    return bundle


def healthy_phases(b: Bench, amps=(6.8, 6.8, 6.8)) -> None:
    for ph in ("r", "y", "b"):
        b.set(PUMP, f"sensor.phase_{ph}", True)
    b.set(PUMP, "sensor.current", amps[0])
    b.set(PUMP, "sensor.current_y", amps[1])
    b.set(PUMP, "sensor.current_b", amps[2])


def test_phase_loss_where_phase_sense_is_wired():
    b = Bench(three_phase(pumphouse()), load=False)
    healthy_phases(b)
    b.rt.compile(b.bundle)
    morning(b)
    healthy_phases(b)
    b.until(6, 0, 30)
    assert b.pump_on()
    b.set(PUMP, "sensor.phase_y", False)
    b.run(3)
    assert b.pump_on()  # held 3 s
    b.run(1)
    assert b.pump_on() is False and b.events("guard_tripped")[0]["guard"]["reason"] == "phase_loss"


def test_phase_imbalance_with_a_current_per_phase():
    b = Bench(three_phase(pumphouse()), load=False)
    healthy_phases(b)
    b.rt.compile(b.bundle)
    morning(b)
    healthy_phases(b, (6.8, 6.8, 4.0))  # mean 5.87, one phase 32 % off
    b.until(6, 0, 12)
    t = b.events("guard_tripped")
    assert t and t[0]["guard"]["reason"] == "phase_imbalance" and t[0]["guard"]["unit"] == "%"


def test_no_phase_checks_on_a_single_phase_motor():
    b = Bench(pumphouse())
    morning(b)
    b.until(6, 1)
    assert b.pump_on() and b.events("guard_tripped") == []


def test_the_voltage_window_where_voltage_is_measured():
    b = Bench(three_phase(pumphouse(), phase_sense=False, currents=1, voltage=True), load=False)
    b.set(PUMP, "sensor.voltage", 230.0)
    b.rt.compile(b.bundle)
    morning(b)
    b.until(6, 0, 20)
    assert b.pump_on()
    b.set(PUMP, "sensor.voltage", 150.0)
    b.run(5)
    assert b.pump_on()  # held 5 s
    b.run(1)
    t = b.events("guard_tripped")
    assert t and t[0]["guard"]["reason"] == "voltage_low" and t[0]["guard"]["unit"] == "V"


def test_rules_read_the_guard_through_its_builtins():
    bundle = pumphouse()
    bundle["rules"].append({"key": "field:field_north01/rule/4", "containerKind": "field", "containerId": "field_north01",
                            "slot": 4, "version": 1, "program": {"variables": [
                                {"name": "tripped", "kind": "asset", "type": "bool", "asset": {"assetId": PUMP, "builtin": "guard_tripped", "access": "read"}},
                                {"name": "why", "kind": "asset", "type": "enum", "enumValues": ["none", "dry_run", "overload"],
                                 "asset": {"assetId": PUMP, "builtin": "guard_reason", "access": "read"}},
                                {"name": "note", "kind": "alert", "type": "string"}],
                                "rules": [{"id": "tell", "kind": "when", "when": {"condition": "tripped and why == 2"},
                                           "actions": [{"type": "alert", "text": "The pump stopped on overload", "severity": "warning"}]}]}})
    b = Bench(bundle)
    morning(b, current=10.0)
    b.until(6, 0, 13)
    assert [a["text"] for a in b.sink.alerts] == ["The pump stopped on overload"]
