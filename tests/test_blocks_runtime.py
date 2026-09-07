"""Simulated bench for the block runtime — the exit tests of _audit/99 P1 against the
contracts' default programs (R-7, R-8, R-10, R-11, R-12, R-13, R-15, R-16, R-19–R-23, R-25).

Deterministic: SimClock + SimulatedIO, no threads, ``rt.step()`` per simulated second.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agal_one_agent.blocks.clock import SimClock
from agal_one_agent.blocks.io import SimulatedIO
from agal_one_agent.blocks.runtime import BlockRuntime, CompileError, RecordingSink
from agal_one_agent.blocks.simulate import build_bundle_from_defaults, run_scenario

HERE = os.path.dirname(__file__)
DEFAULTS = os.path.normpath(os.path.join(HERE, "..", "..", "..", "contracts", "programs", "defaults"))
TZ = ZoneInfo("Asia/Kolkata")


def _bundle(kit="farm"):
    if not os.path.isdir(DEFAULTS):
        pytest.skip(f"defaults not found at {DEFAULTS}")
    return build_bundle_from_defaults(DEFAULTS, kit)


class Bench:
    def __init__(self, kit="farm", start=None, state_dir=None):
        self.clock = SimClock(start or datetime(2026, 9, 7, 5, 0, tzinfo=TZ))
        self.io = SimulatedIO()
        self.sink = RecordingSink()
        self.rt = BlockRuntime(self.io, self.sink, clock=self.clock, state_dir=state_dir)
        self.rt.compile(_bundle(kit))

    def run(self, seconds: float, tick: float = 1.0):
        t = 0.0
        while t < seconds:
            self.rt.step()
            self.clock.advance(tick)
            t += tick

    def out(self, asset, key="device.power"):
        return self.io.output(asset, key, False)

    def alerts(self):
        return [a["text"] for a in self.sink.alerts]

    def events(self, type_):
        return [e for e in self.sink.events if e["type"] == type_]


# ------------------------------------------------------------------ compile

def test_compile_ack_applied():
    b = Bench()
    b.run(2)
    assert b.sink.acks == [{"version": 1, "status": "applied", "reason": None}]
    assert b.events("program_applied")


def test_rejects_unknown_port():
    bundle = _bundle()
    bundle["assets"][0]["aab"]["variables"][0]["node"]["sourceKey"] = "device.nowhere"
    rt = BlockRuntime(SimulatedIO(), RecordingSink(), clock=SimClock())
    with pytest.raises(CompileError, match="nowhere"):
        rt.compile(bundle)


def test_rejects_port_the_node_does_not_have():
    bundle = _bundle()
    rt = BlockRuntime(SimulatedIO(allowed_gpios={17, 27}), RecordingSink(), clock=SimClock())
    with pytest.raises(CompileError, match="no port"):
        rt.compile(bundle)


def test_rejects_reference_to_asset_not_on_node():
    bundle = _bundle()
    bundle["nab"]["variables"][0]["asset"]["assetId"] = "some-other-node-pump"
    rt = BlockRuntime(SimulatedIO(), RecordingSink(), clock=SimClock())
    with pytest.raises(CompileError, match="not on this node"):
        rt.compile(bundle)


def test_rejects_bad_expression_and_keeps_previous_program():
    b = Bench()
    b.run(1)
    bad = _bundle()
    bad["version"] = 2
    bad["nab"]["rules"][2]["when"]["condition"] = "pump_relay and not sqrt(1)"
    with pytest.raises(CompileError):
        b.rt.compile(bad)
    assert b.rt.version == 1


# ---------------------------------------------------------- R-7 / R-10 plots

def test_run_plot_energises_valve_then_pump_after_delay():
    b = Bench()
    b.run(1)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-1"})
    b.run(1)
    assert b.out("valve-1") is True
    assert b.out("pump-1") is False, "pump must wait for the valve-energise delay"
    b.run(4)
    assert b.out("pump-1") is True
    # stop: pump off, valve off (pump written before valve in the same pass)
    b.rt.apply_command({"type": "stopPlot", "plotId": "plot-1"})
    b.run(1)
    assert b.out("pump-1") is False and b.out("valve-1") is False
    writes = [w for w in b.io.writes if w[2] is False]
    assert writes[0][0] == "pump-1", "pump must be de-energised before the valve closes"


def test_timed_run_ends_on_time():
    b = Bench()
    b.run(1)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-1", "durationMinutes": 1})
    b.run(30)
    assert b.out("valve-1") is True and b.out("pump-1") is True
    b.run(40)
    assert b.out("valve-1") is False and b.out("pump-1") is False


def test_pump_toggle_with_no_valve_is_refused_within_3s():
    b = Bench()
    b.run(1)
    b.rt.apply_command({"type": "setPower", "assetId": "pump-1", "sourceKey": "device.power", "value": 1})
    b.run(1)
    assert b.out("pump-1") is True
    b.run(4)
    assert b.out("pump-1") is False
    assert any("no valve open" in a for a in b.alerts())
    assert b.events("interruption")


# ------------------------------------------------------------ R-11 dry run

def _start_pump_with_current(b: Bench, amps: float):
    b.io.set_input("pump-1", "sensor.current", 0.0)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-1"})
    b.run(6)  # valve + delay + pump on
    assert b.out("pump-1") is True
    b.io.set_input("pump-1", "sensor.current", amps)
    b.run(14)  # 2 s after + 10 s window → baseline learned
    assert b.rt.snapshot()["baselines"]["pump-1"]["current"] == pytest.approx(amps)


def test_dry_run_cuts_relative_to_learned_baseline():
    b = Bench()
    b.run(1)
    _start_pump_with_current(b, 4.5)
    b.io.set_input("pump-1", "sensor.current", 3.0)  # 33 % drop (a dry pump still draws current)
    b.run(4)
    assert b.out("pump-1") is True, "not before the 5 s hold"
    b.run(3)
    assert b.out("pump-1") is False
    snap = b.rt.snapshot()
    assert snap["assets"]["pump-1"]["dry_run"] is True
    assert any("running dry" in a for a in b.alerts())
    assert b.out("valve-1") is False, "the NAB closes the valves on dry run"


def test_dry_run_does_not_trip_on_startup_surge_or_small_dip():
    b = Bench()
    b.run(1)
    _start_pump_with_current(b, 4.5)
    b.io.set_input("pump-1", "sensor.current", 4.0)  # 11 % dip
    b.run(20)
    assert b.out("pump-1") is True
    assert not any("running dry" in a for a in b.alerts())


# ------------------------------------------------------------ R-12 max run

def test_max_run_time_stops_pump_and_closes_valves():
    b = Bench()
    b.run(1)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-1"})
    b.run(10)
    assert b.out("pump-1") is True
    b.run(7150, tick=10)
    assert b.out("pump-1") is True, "not before 7200 s"
    b.run(80, tick=10)
    assert b.out("pump-1") is False and b.out("valve-1") is False
    assert any("maximum run time" in a for a in b.alerts())


# --------------------------------------------------------- R-23 flow faults

def test_valve_open_but_no_flow_alerts_after_20s():
    b = Bench()
    b.run(1)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-1"})
    b.io.set_input("pump-1", "sensor.current", 4.5)
    b.run(30)
    assert any("no flow" in a for a in b.alerts())


def test_flow_confirms_and_unexpected_flow_alerts():
    b = Bench()
    b.run(1)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-1"})
    b.io.set_input("pump-1", "sensor.current", 4.5)
    b.io.set_input("flow-1", "sensor.flow", 1)
    b.run(30)
    assert b.rt.snapshot()["assets"]["flow-1"]["flowing"] is True
    assert not any("no flow" in a for a in b.alerts())
    b.rt.apply_command({"type": "stopPlot", "plotId": "plot-1"})
    b.run(15)  # flow keeps coming with the valve closed
    assert any("flowing while the valve is closed" in a for a in b.alerts())


def test_flow_switch_debounce_and_hold():
    b = Bench()
    b.run(1)
    b.io.set_input("flow-1", "sensor.flow", 1)
    b.run(1)
    assert b.rt.snapshot()["assets"]["flow-1"]["flowing"] is False
    b.run(4)
    assert b.rt.snapshot()["assets"]["flow-1"]["flowing"] is True


# --------------------------------------------------------- R-25 schedules

def test_schedule_runs_plot_at_start_and_stops_after_duration():
    bundle_bench = Bench(start=datetime(2026, 9, 7, 5, 58, tzinfo=TZ))
    bundle = _bundle()
    bundle["nab"]["schedules"][0]["enabled"] = True
    bundle_bench.rt.compile(bundle)
    b = bundle_bench
    b.run(60)
    assert b.out("valve-1") is False
    b.run(120)  # past 06:00
    assert b.out("valve-1") is True and b.out("pump-1") is True
    assert b.events("schedule_started")
    b.run(31 * 60, tick=10)
    assert b.out("valve-1") is False and b.out("pump-1") is False
    assert b.events("schedule_ended")


def test_schedule_missed_when_window_elapsed_during_outage():
    b = Bench(start=datetime(2026, 9, 7, 7, 30, tzinfo=TZ))  # boot after the 06:00–07:00 window
    bundle = _bundle()
    bundle["nab"]["schedules"][0]["enabled"] = True
    b.rt.compile(bundle)
    b.run(3)
    assert b.events("run_missed")
    assert b.out("valve-1") is False


def test_schedule_held_while_clock_untrusted():
    b = Bench(start=datetime(2026, 9, 7, 6, 1, tzinfo=TZ))
    bundle = _bundle()
    bundle["nab"]["schedules"][0]["enabled"] = True
    b.rt.compile(bundle)
    b.clock.ok = False
    b.run(5)
    assert b.out("valve-1") is False
    b.clock.ok = True
    b.run(5)
    assert b.out("valve-1") is True


def test_interrupted_run_resumes_after_short_outage(tmp_path):
    start = datetime(2026, 9, 7, 6, 1, tzinfo=TZ)
    b = Bench(start=start, state_dir=str(tmp_path))
    bundle = _bundle()
    bundle["nab"]["schedules"][0]["enabled"] = True
    b.rt.compile(bundle)
    b.run(10)
    assert b.out("valve-1") is True
    b.rt.stop(safe_state=True)  # clean stop persists state; pump off
    # restart 2 minutes later
    b2 = Bench(start=start.replace(minute=3), state_dir=str(tmp_path))
    b2.rt.compile(bundle)
    b2.run(5)
    assert b2.events("run_resumed")
    assert b2.out("valve-1") is True


def test_interrupted_run_cut_short_after_long_outage(tmp_path):
    start = datetime(2026, 9, 7, 6, 1, tzinfo=TZ)
    b = Bench(start=start, state_dir=str(tmp_path))
    bundle = _bundle()
    bundle["nab"]["schedules"][0]["enabled"] = True
    b.rt.compile(bundle)
    b.run(10)
    b.rt.stop(safe_state=True)
    b2 = Bench(start=start.replace(minute=25), state_dir=str(tmp_path))
    b2.rt.compile(bundle)
    b2.run(3)
    assert b2.events("run_cut_short")
    assert b2.out("valve-1") is False


# ---------------------------------------------------------- building kit

def test_building_fill_rule_and_alerts():
    b = Bench(kit="building")
    b.run(1)
    b.io.set_input("tank-1", "sensor.level.percent", 15.0)
    b.run(12)
    assert b.out("pump-1") is True
    b.io.set_input("tank-1", "sensor.level.percent", 96.0)
    b.run(7)
    assert b.out("pump-1") is False
    b.io.set_input("tank-1", "sensor.level.percent", 99.0)
    b.run(3)
    assert any("overflowing" in a for a in b.alerts())


def test_building_max_fill_time():
    b = Bench(kit="building")
    b.run(1)
    b.io.set_input("tank-1", "sensor.level.percent", 15.0)
    b.run(12)
    assert b.out("pump-1") is True
    b.run(1900, tick=10)
    assert b.out("pump-1") is False
    assert any("maximum fill time" in a for a in b.alerts())


def test_wall_switch_toggles_lighting_locally():
    b = Bench(kit="building")
    b.run(1)
    assert b.out("light-1") is False
    b.io.set_input("light-1", "sensor.wall_switch", 1)
    b.run(1)
    assert b.out("light-1") is True
    b.io.set_input("light-1", "sensor.wall_switch", 0)
    b.run(1)
    assert b.out("light-1") is False


def test_sunset_schedule_switches_lights():
    b = Bench(kit="building", start=datetime(2026, 9, 7, 17, 0, tzinfo=TZ))
    b.run(3 * 3600, tick=60)  # sunset near 18:20 IST at 11°N/77°E in September
    assert b.out("light-1") is True
    assert b.events("schedule_started")


# ---------------------------------------------------------- variables/datalog

def test_variables_reported_and_datalog_emitted():
    b = Bench()
    b.io.set_input("pump-1", "sensor.current", 4.5)
    b.run(70)
    assert any(aid == "pump-1" and "current" in vals for aid, vals in b.sink.variables_log)
    assert any(r["assetId"] == "pump-1" and r["sourceKey"] == "sensor.current" for r in b.sink.readings)


def test_set_variable_command_forwards_ui_to_source():
    b = Bench()
    b.run(1)
    r = b.rt.apply_command({"type": "setVariable", "assetId": "valve-1", "variable": "open", "value": True})
    assert r["accepted"]
    b.run(1)
    assert b.out("valve-1") is True


def test_safe_state_off_on_clean_stop():
    b = Bench()
    b.run(1)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-1"})
    b.run(6)
    assert b.out("pump-1") is True
    b.rt.stop(safe_state=True)
    assert b.out("pump-1") is False


# ---------------------------------------------------------- scenario runner

def test_scenario_runner_farm():
    bundle = _bundle()
    scenario = {
        "steps": [
            {"at": 1, "command": {"type": "runPlot", "plotId": "plot-2", "durationMinutes": 1}},
            {"at": 3, "input": {"asset": "pump-1", "sourceKey": "sensor.current", "value": 4.2}},
            {"at": 30, "assert": {"output": ["pump-1", "device.power"], "equals": True}},
            {"at": 90, "assert": {"output": ["pump-1", "device.power"], "equals": False}},
        ],
        "until": 100,
    }
    ok, sink, io = run_scenario(bundle, scenario, quiet=True)
    assert ok
