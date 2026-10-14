"""A program reload takes over from the running program without a bump (wave 0, G1).

The cloud recompiles the whole node program on every block, card or link write, so a reload
during a plot run is the normal case: an output keeps its level, an unchanged rule keeps its
state, a run and a schedule carry on to their end, and only an output the new program no
longer has is let go of. Deterministic: SimClock + SimulatedIO (or LineIO on a fake chip),
``rt.step()`` per simulated second, no hardware.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agal_one_agent.blocks.clock import SimClock
from agal_one_agent.blocks.io import HardwareIO, SimulatedIO
from agal_one_agent.blocks.runtime import BlockRuntime, RecordingSink
from agal_one_agent.blocks.simulate import build_bundle_from_defaults
from agal_one_agent.ports.gpiochip import ChipInfo, LineIO

import _contracts  # noqa: E402 — tests/ is on sys.path (pytest rootdir insertion)

DEFAULTS = _contracts.DEFAULTS
TZ = ZoneInfo("Asia/Kolkata")


def _defaults_or_skip():
    if not os.path.isdir(DEFAULTS):
        pytest.skip(f"defaults not found at {DEFAULTS}")


def _farm():
    _defaults_or_skip()
    return build_bundle_from_defaults(DEFAULTS, "farm")


def _template(name):
    _defaults_or_skip()
    with open(os.path.join(DEFAULTS, name)) as f:
        return json.load(f)


def _next(bundle, change=None):
    """The same program one version later, as the cloud would recompile it, optionally edited."""
    b = copy.deepcopy(bundle)
    b["version"] = bundle["version"] + 1
    if change:
        change(b)
    return b


class Bench:
    def __init__(self, bundle, start=None, io=None, state_dir=None):
        self.clock = SimClock(start or datetime(2026, 9, 7, 5, 0, tzinfo=TZ))
        self.io = io or SimulatedIO()
        self.sink = RecordingSink()
        self.rt = BlockRuntime(self.io, self.sink, clock=self.clock, state_dir=state_dir)
        self.bundle = bundle
        self.rt.compile(bundle)

    def run(self, seconds, tick=1.0):
        t = 0.0
        while t < seconds:
            self.rt.step()
            self.clock.advance(tick)
            t += tick

    def reload(self, change=None):
        self.bundle = _next(self.bundle, change)
        self.rt.compile(self.bundle)

    def out(self, asset, key="device.power"):
        return self.io.output(asset, key, False)

    def events(self, type_):
        return [e for e in self.sink.events if e["type"] == type_]


# ------------------------------------------------- the bug, as it was found

def test_a_reload_during_a_plot_run_keeps_the_pump_and_the_valve_on():
    """scratchpad/understand/reload_sim.py: before the fix, the first pass after the reload
    switched the pump and Valve 1 OFF (every variable was rebuilt at its default)."""
    b = Bench(_farm())
    b.run(2)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-1"})
    b.io.set_input("flow-1", "sensor.flow", True)
    b.run(12)
    assert (b.out("pump-1"), b.out("valve-1")) == (True, True)
    mark = len(b.io.writes)

    b.reload()
    for _ in range(8):
        b.run(1)
        assert (b.out("pump-1"), b.out("valve-1")) == (True, True), "no pass after the reload may switch them off"
    assert not [w for w in b.io.writes[mark:] if w[2] is False], "not even for one pass"
    assert b.sink.acks[-1] == {"version": 2, "status": "applied", "reason": None}
    assert "plot-1" in b.rt.snapshot()["activeRuns"], "the run is still the program's"

    # ... and the run still ends the ordinary way, pump before valve.
    b.rt.apply_command({"type": "stopPlot", "plotId": "plot-1"})
    b.run(1)
    assert (b.out("pump-1"), b.out("valve-1")) == (False, False)
    offs = [w[0] for w in b.io.writes[mark:] if w[2] is False]
    assert offs == ["pump-1", "valve-1"]


def test_an_identical_bundle_reload_is_a_no_op_on_the_outputs():
    b = Bench(_farm())
    b.run(2)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-2"})
    b.io.set_input("flow-2", "sensor.flow", True)
    b.run(20)
    before = b.rt.snapshot()
    mark, events = len(b.io.writes), len(b.sink.events)

    b.reload()
    loaded = b.rt.snapshot()
    assert loaded["assets"] == before["assets"] and loaded["nab"] == before["nab"], "every variable carried as it was"
    assert loaded["activeRuns"] == before["activeRuns"]
    b.run(10)
    assert b.io.writes[mark:] == [], "an identical program writes nothing"
    after = b.rt.snapshot()
    assert after["assets"]["valve-2"]["open_since"] == before["assets"]["valve-2"]["open_since"] + 10, "since() went on counting"
    new_types = {e["type"] for e in b.sink.events[events:]}
    assert new_types <= {"program_applied"}, f"nothing else happens: {new_types}"


def test_an_edit_to_one_rule_keeps_the_irrigation_running():
    """What every Agal Assist rule edit does: the cloud recompiles the node."""
    b = Bench(_farm())
    b.run(2)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-1"})
    b.io.set_input("flow-1", "sensor.flow", True)
    b.run(15)
    mark = len(b.io.writes)

    def edit(bundle):
        rule = next(r for r in bundle["nab"]["rules"] if r["id"] == "no_flow_plot_2")
        rule["when"]["holdSeconds"] = 30
        rule["actions"][0]["text"] = "Plot 2: the valve is open and nothing flows"

    b.reload(edit)
    b.run(10)
    assert (b.out("pump-1"), b.out("valve-1")) == (True, True)
    assert b.io.writes[mark:] == []


# ------------------------------------------------------- runs and schedules

def test_a_timed_run_still_ends_on_time_after_a_reload():
    b = Bench(_farm())
    b.run(1)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-1", "durationMinutes": 1})
    b.run(30)
    b.reload()
    b.run(20)
    assert b.out("valve-1") is True and b.out("pump-1") is True
    b.run(20)
    assert b.out("valve-1") is False and b.out("pump-1") is False


def test_a_running_schedule_still_ends_on_time_after_a_reload():
    bundle = _farm()
    bundle["nab"]["schedules"][0]["enabled"] = True
    b = Bench(bundle, start=datetime(2026, 9, 7, 5, 58, tzinfo=TZ))
    b.run(180)  # past 06:00: the 30-minute morning run has started
    assert b.events("schedule_started") and b.out("valve-1") is True
    b.reload()
    b.run(10 * 60, tick=10)
    assert b.out("valve-1") is True, "the reload neither ends nor restarts it"
    assert len(b.events("schedule_started")) == 1
    b.run(25 * 60, tick=10)
    assert b.events("schedule_ended"), "it ends at 06:30 as planned"
    assert b.out("valve-1") is False and b.out("pump-1") is False


def test_with_the_state_directory_of_a_node_a_reload_neither_resumes_nor_restarts_anything(tmp_path):
    """On a node the runtime persists its state; a reload must not read it back as if the
    node had restarted (that re-announced the running schedule as resumed)."""
    bundle = _farm()
    bundle["nab"]["schedules"][0]["enabled"] = True
    b = Bench(bundle, start=datetime(2026, 9, 7, 5, 59, tzinfo=TZ), state_dir=str(tmp_path))
    b.run(120)
    assert b.out("valve-1") is True and (tmp_path / "blocks_state.json").exists()
    mark, events = len(b.io.writes), len(b.sink.events)
    b.reload()
    b.run(30)
    assert b.io.writes[mark:] == []
    assert {e["type"] for e in b.sink.events[events:]} <= {"program_applied"}
    assert json.loads((tmp_path / "blocks_state.json").read_text())["version"] == 2


def test_a_plot_taken_out_while_it_runs_ends_its_run_and_closes_its_valve():
    b = Bench(_farm())
    b.run(1)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-2"})
    b.run(6)
    assert b.out("valve-2") is True and b.out("pump-1") is True
    mark = len(b.io.writes)

    b.reload(lambda bundle: bundle["nab"].__setitem__("plots", [p for p in bundle["nab"]["plots"] if p["plotId"] != "plot-2"]))
    b.run(2)
    assert b.out("valve-2") is False and b.out("pump-1") is False
    assert [w[0] for w in b.io.writes[mark:] if w[2] is False] == ["pump-1", "valve-2"], "pump before valve"
    assert {"type": "plot_run_ended", "plotId": "plot-2", "source": "program"} in b.sink.events


# ------------------------------------------------------- outputs released

def _two_valves(**valve_b):
    aab = _template("aab_valve.json")
    assets = [
        {"assetId": "valve-a", "assetType": "valve_controller", "aab": copy.deepcopy(aab),
         "ports": {"device.power": {"kind": "gpio", "gpioNumber": 27, "direction": "out"}}},
        {"assetId": "valve-b", "assetType": "valve_controller", "aab": copy.deepcopy(aab),
         "ports": {"device.power": {"kind": "gpio", "gpioNumber": 22, "direction": "out"}}, **valve_b},
    ]
    return {"version": 1, "timezone": "Asia/Kolkata", "nab": {"variables": [], "rules": []}, "assets": assets}


def _open_both(b):
    b.run(1)
    for aid in ("valve-a", "valve-b"):
        assert b.rt.apply_command({"type": "setPower", "assetId": aid, "sourceKey": "device.power", "value": 1})["accepted"]
    b.run(2)
    assert b.out("valve-a") is True and b.out("valve-b") is True


def _drop_card(bundle):
    bundle["assets"] = [a for a in bundle["assets"] if a["assetId"] != "valve-b"]


def _drop_port(bundle):
    card = next(a for a in bundle["assets"] if a["assetId"] == "valve-b")
    card["aab"] = {"variables": [{"name": "note", "kind": "local", "type": "string", "default": "kept for later"}], "rules": []}
    card["ports"] = {}


@pytest.mark.parametrize("change", [_drop_card, _drop_port], ids=["card left the program", "port left the card"])
def test_an_output_the_new_program_no_longer_has_is_switched_off_and_released(change):
    b = Bench(_two_valves())
    _open_both(b)
    mark = len(b.io.writes)

    b.reload(change)
    assert b.io.writes[mark:] == [], "nothing is written between passes"
    b.run(1)
    assert b.io.writes[mark:] == [("valve-b", "device.power", False)], "only the output that left is written"
    assert [(aid, key, t["gpioNumber"]) for aid, key, t in b.io.released] == [("valve-b", "device.power", 22)]
    assert b.out("valve-a") is True
    assert {"type": "output_released", "assetId": "valve-b", "sourceKey": "device.power", "level": "off"} in b.sink.events
    b.run(5)
    assert b.io.writes[mark:] == [("valve-b", "device.power", False)], "and only once"


def test_an_output_whose_safe_state_is_last_state_is_left_as_it_is():
    b = Bench(_two_valves(safeState="last_state"))
    _open_both(b)
    mark = len(b.io.writes)
    b.reload(_drop_card)
    b.run(3)
    assert b.io.writes[mark:] == [] and b.io.released == []
    assert b.out("valve-b") is True
    assert {"type": "output_released", "assetId": "valve-b", "sourceKey": "device.power", "level": "kept"} in b.sink.events


def test_a_clean_stop_lets_go_of_an_output_a_reload_took_out_before_any_pass():
    b = Bench(_two_valves())
    _open_both(b)
    b.reload(_drop_card)
    b.rt.stop(safe_state=True)
    assert b.io.output("valve-b", "device.power") is False
    assert [r[0] for r in b.io.released] == ["valve-b"]


class _Line:
    def __init__(self, log, line, direction):
        self.log, self.line = log, line
        log.append(("open", line, direction))

    def write(self, level):
        self.log.append(("write", self.line, bool(level)))

    def read(self):
        return False

    def close(self):
        self.log.append(("close", self.line))


def test_an_output_moved_to_another_line_carries_its_level_there_and_lets_go_of_the_old_one():
    """A port table edit (the card's port is re-stamped onto another line): the card stays on,
    on its new line; the old line is switched off and freed. Nothing goes LOW on an active-low
    relay that should be off, and the new line never sees an OFF first."""
    log: list = []
    chips = [ChipInfo("/dev/gpiochip0", "gpiochip0", "pinctrl-bcm2835", 54)]
    lines = LineIO(chips=lambda: chips, opener=lambda path, line, direction: _Line(log, line, direction))
    bundle = _two_valves()
    for card, line in zip(bundle["assets"], (27, 22)):
        card["ports"]["device.power"] = {"kind": "gpio", "chip": "pinctrl-bcm2835", "line": line, "direction": "out", "activeLow": True}
    b = Bench(bundle, io=HardwareIO(sensor_by_key={}, lines=lines))
    b.run(1)
    assert b.rt.apply_command({"type": "setPower", "assetId": "valve-a", "sourceKey": "device.power", "value": 1})["accepted"]
    b.run(2)
    mark = len(log)

    def move(bundle):
        bundle["assets"][0]["ports"]["device.power"]["line"] = 23

    b.reload(move)
    b.run(3)
    # active low: OFF is HIGH, ON is LOW. Line 27 is let go of (HIGH, closed); line 23 opens ON.
    assert log[mark:] == [("write", 27, True), ("close", 27), ("open", 23, "low"), ("write", 23, False)]
    assert not [e for e in log[mark:] if e[:2] == ("write", 22)], "valve-b is untouched"


# ------------------------------------------------------------- rule state

def _flow_and_two_valves():
    bundle = _two_valves()
    bundle["assets"].insert(0, {"assetId": "flow-1", "assetType": "flow_switch", "aab": _template("aab_flow_switch.json"),
                                "ports": {"sensor.flow": {"kind": "gpio", "gpioNumber": 5, "direction": "in"}}})
    bundle["nab"] = {
        "variables": [
            {"name": "flow", "kind": "asset", "type": "bool", "asset": {"assetId": "flow-1", "variable": "flow", "access": "read"}},
            {"name": "a", "kind": "asset", "type": "bool", "settable": True, "asset": {"assetId": "valve-a", "variable": "coil", "access": "write"}},
            {"name": "b", "kind": "asset", "type": "bool", "settable": True, "asset": {"assetId": "valve-b", "variable": "coil", "access": "write"}},
        ],
        "rules": [
            {"id": "rule_a", "kind": "when", "title": "A follows a steady flow", "when": {"condition": "flow", "holdSeconds": 10},
             "actions": [{"type": "set", "variable": "a", "value": True}, {"type": "log", "text": "A on"}]},
            {"id": "rule_b", "kind": "when", "title": "B follows a steady flow", "when": {"condition": "flow", "holdSeconds": 10},
             "actions": [{"type": "set", "variable": "b", "value": True}, {"type": "log", "text": "B on"}]},
        ],
    }
    return bundle


def test_a_changed_rule_resets_its_own_state_only():
    b = Bench(_flow_and_two_valves())
    b.run(1)
    b.io.set_input("flow-1", "sensor.flow", True)
    b.run(6)  # both holds are 6 s into their 10 s

    def edit_b(bundle):
        bundle["nab"]["rules"][1]["title"] = "B opens once the flow has held"

    b.reload(edit_b)
    b.run(5)
    assert b.out("valve-a") is True, "rule_a is unchanged: its hold went on through the reload"
    assert b.out("valve-b") is False, "rule_b changed: its hold started again at the reload"
    b.run(6)
    assert b.out("valve-b") is True
    assert [entry["text"] for entry in b.sink.logs] == ["A on", "B on"], "each fired once; nothing re-fired at the reload"


def test_an_unchanged_rule_that_already_fired_does_not_fire_again():
    b = Bench(_flow_and_two_valves())
    b.run(1)
    b.io.set_input("flow-1", "sensor.flow", True)
    b.run(15)
    assert [entry["text"] for entry in b.sink.logs] == ["A on", "B on"]
    b.reload()
    b.run(15)
    assert [entry["text"] for entry in b.sink.logs] == ["A on", "B on"]


def test_a_changed_variable_starts_at_its_new_default_and_an_unchanged_one_keeps_its_value():
    bundle = _two_valves()
    for card in bundle["assets"]:
        card["aab"]["variables"].append({"name": "runs", "kind": "local", "type": "number", "settable": True, "default": 0})
    b = Bench(bundle)
    b.run(1)
    for aid in ("valve-a", "valve-b"):
        assert b.rt.apply_command({"type": "setVariable", "assetId": aid, "variable": "runs", "value": 7})["accepted"]
    b.run(1)

    def edit(bundle):
        runs = next(v for v in bundle["assets"][1]["aab"]["variables"] if v["name"] == "runs")
        runs["default"] = 2

    b.reload(edit)
    b.run(1)
    snap = b.rt.snapshot()["assets"]
    assert snap["valve-a"]["runs"] == 7.0
    assert snap["valve-b"]["runs"] == 2.0


# ------------------------------------------------------------ cold loads

def test_a_program_loaded_after_a_stop_starts_cold_as_after_a_restart():
    b = Bench(_farm())
    b.run(1)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-1"})
    b.run(6)
    b.rt.stop(safe_state=True)
    b.rt.compile(b.bundle)  # what the bench's outage button does
    assert b.rt.snapshot()["assets"]["pump-1"]["relay"] is False, "nothing is carried over a stop"
