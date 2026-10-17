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
from agal_one_agent.blocks.runtime import BlockRuntime, CompileError, RecordingSink
from agal_one_agent.blocks.sync import ProgramStore, ProgramSync
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
def test_an_output_the_new_program_no_longer_has_is_switched_off_and_kept_driven_at_off(change):
    """0.4.1 (contracts v1.15.4, programs/README.md §4.8): switched off and KEPT driven — its line is never let go of to
    its pull (0.3.x let go of it: an active-low relay board energises on a pulled-down line)."""
    b = Bench(_two_valves())
    _open_both(b)
    mark = len(b.io.writes)

    b.reload(change)
    assert b.io.writes[mark:] == [], "nothing is written between passes"
    b.run(1)
    assert b.io.writes[mark:] == [("valve-b", "device.power", False)], "only the output that left is written"
    assert b.io.released == [], "kept driven at off, never let go of (0.4.1)"
    assert b.out("valve-a") is True
    assert {"type": "output_released", "assetId": "valve-b", "sourceKey": "device.power", "level": "off"} in b.sink.events
    b.run(5)
    assert b.io.writes[mark:] == [("valve-b", "device.power", False)], "and only once"


def test_an_output_that_leaves_the_program_is_switched_off_whatever_its_safe_state():
    """safeState (R-14) is what a pump does when the node's software stops or its power goes. An
    output the program no longer has is supervised by no rule any more (no maximum run, no dry-run
    protection) and, on a port-table node, not even its own card can switch it: it never stays on.
    Before, a last_state motor was left running, and its owner's OFF was refused."""
    b = Bench(_two_valves(safeState="last_state", assetType="motor_controller"))
    _open_both(b)
    mark = len(b.io.writes)
    b.reload(_drop_card)
    b.run(3)
    assert b.io.writes[mark:] == [("valve-b", "device.power", False)]
    assert b.io.released == []  # 0.4.1: kept driven at off
    assert {"type": "output_released", "assetId": "valve-b", "sourceKey": "device.power", "level": "off"} in b.sink.events
    assert b.out("valve-a") is True


def test_on_a_clean_stop_a_last_state_output_still_keeps_its_level():
    """... while a software stop still honours safeState, as before."""
    b = Bench(_two_valves(safeState="last_state"))
    _open_both(b)
    b.rt.stop(safe_state=True)
    assert b.out("valve-b") is True and b.out("valve-a") is False


def test_a_clean_stop_switches_off_an_output_a_reload_took_out_before_any_pass():
    b = Bench(_two_valves())
    _open_both(b)
    b.reload(_drop_card)
    b.rt.stop(safe_state=True)
    assert b.io.output("valve-b", "device.power") is False
    assert b.io.released == []  # 0.4.1: switched off and kept driven at off (the agent's exit closes every line)


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


def test_an_output_moved_to_another_line_carries_its_level_there_and_keeps_the_old_one_driven_off():
    """A port table edit (the card's port is re-stamped onto another line): the card stays on,
    on its new line; the old line is switched off and KEPT driven at off (0.4.1 — 0.3.x freed it,
    and a freed line falls to its pull). Nothing goes LOW on an active-low relay that should be
    off, and the new line never sees an OFF first."""
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
    # active low: OFF is HIGH, ON is LOW. Line 27 is switched off and kept (HIGH, never closed); line 23 opens ON.
    assert log[mark:] == [("write", 27, True), ("open", 23, "low"), ("write", 23, False)]
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


# ------------------------------------------- several runs ended by one reload

@pytest.mark.parametrize("minutes", [None, 1], ids=["untimed runs", "timed runs"])
def test_two_running_plots_taken_out_in_one_reload_both_end_and_the_program_goes_on(minutes):
    """Review G1 blocker: ending the first removed run looked the second one up in the new
    program (KeyError), after the program had been swapped in — half-loaded, never acked, and
    with timed runs every later pass failed before writing any output."""
    b = Bench(_farm())
    b.run(2)
    for pid in ("plot-1", "plot-2"):
        cmd = {"type": "runPlot", "plotId": pid, **({"durationMinutes": minutes} if minutes else {})}
        assert b.rt.apply_command(cmd)["accepted"]
    b.io.set_input("flow-1", "sensor.flow", True)
    b.io.set_input("flow-2", "sensor.flow", True)
    b.run(12)
    assert (b.out("pump-1"), b.out("valve-1"), b.out("valve-2")) == (True, True, True)
    mark = len(b.io.writes)

    def no_plots(bundle):
        bundle["nab"]["plots"] = []
        bundle["nab"]["schedules"] = []

    b.reload(no_plots)
    assert b.rt.version == 2 and b.rt.snapshot()["activeRuns"] == {}
    b.run(1)
    assert b.sink.acks[-1] == {"version": 2, "status": "applied", "reason": None}
    assert (b.out("pump-1"), b.out("valve-1"), b.out("valve-2")) == (False, False, False)
    offs = [w[0] for w in b.io.writes[mark:] if w[2] is False]
    assert offs[0] == "pump-1" and sorted(offs[1:]) == ["valve-1", "valve-2"], "pump before valves"
    ended = sorted(e["plotId"] for e in b.events("plot_run_ended") if e["source"] == "program")
    assert ended == ["plot-1", "plot-2"]
    b.run(150)  # past the end the timed runs had: every pass runs (rt.step raises if one fails)
    assert b.rt.apply_command({"type": "setPower", "assetId": "valve-1", "sourceKey": "device.power", "value": 1})["accepted"]
    b.run(1)
    assert b.out("valve-1") is True, "outputs are still written"


def test_a_take_over_that_fails_leaves_the_running_program_as_it_was(monkeypatch):
    """compile is atomic: if taking over fails half-way, everything is put back, nothing is
    announced, and the bundle is rejected (a CompileError, which ProgramSync acks as such)."""
    b = Bench(_farm())
    b.run(1)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-1"})
    b.io.set_input("flow-1", "sensor.flow", True)
    b.run(10)
    before, mark, events = b.rt.snapshot(), len(b.io.writes), len(b.sink.events)

    def boom(old_outputs):
        raise RuntimeError("half-way")

    def no_plot_1(bundle):
        bundle["nab"]["plots"] = [p for p in bundle["nab"]["plots"] if p["plotId"] != "plot-1"]
        bundle["nab"]["schedules"] = []

    monkeypatch.setattr(b.rt, "_plan_releases", boom)
    with pytest.raises(CompileError, match="could not take over from the running program: half-way"):
        b.reload(no_plot_1)
    assert b.rt.version == 1
    snap = b.rt.snapshot()
    assert snap["assets"] == before["assets"] and snap["nab"] == before["nab"]
    assert list(snap["activeRuns"]) == ["plot-1"], "the run the failed reload had ended is back"
    assert b.sink.events[events:] == [], "nothing was announced"
    b.run(5)
    assert (b.out("pump-1"), b.out("valve-1")) == (True, True) and b.io.writes[mark:] == []


def test_program_sync_rejects_a_bundle_whose_take_over_failed_and_keeps_the_old_one(tmp_path, monkeypatch):
    class Http:
        def __init__(self, bundle):
            self.bundle, self.acks = bundle, []

        def fetch_program(self, current_version=None):
            return {"bundle": self.bundle}

        def report_program_ack(self, version, status, reason=None, firmware_version=None, timestamp=None):
            self.acks.append((version, status, reason))

    b = Bench(_farm())
    b.run(1)
    monkeypatch.setattr(b.rt, "_carry_over", lambda *a: (_ for _ in ()).throw(KeyError("plot-2")))
    http = Http(_next(b.bundle))
    ProgramSync(b.rt, ProgramStore(str(tmp_path)), http)._pull()
    assert http.acks == [(2, "rejected", "could not take over from the running program: 'plot-2'")]
    assert b.rt.version == 1 and not (tmp_path / "program.json").exists()

    class Exploding:
        version = 1

        def compile(self, bundle):
            raise ValueError("not a CompileError")

    http = Http(_next(b.bundle))
    ProgramSync(Exploding(), ProgramStore(str(tmp_path)), http)._pull()
    assert http.acks == [(2, "rejected", "not a CompileError")], "an unexpected error is a rejection, not a dead pull"


# ---------------------------------------- what a removed plot leaves behind

def test_a_plot_taken_out_with_its_valve_variable_closes_the_valve_card_it_opened():
    """Deleting a field: the plot and its valve variable leave the NAB, the valve card stays on
    the node. Before, the pump was switched off, the run reported ended — and the valve left open."""
    b = Bench(_farm())
    b.run(1)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-2"})
    b.io.set_input("flow-2", "sensor.flow", True)
    b.run(12)
    assert (b.out("pump-1"), b.out("valve-2")) == (True, True)
    mark = len(b.io.writes)

    def drop_field_2(bundle):
        nab = bundle["nab"]
        nab["plots"] = [p for p in nab["plots"] if p["plotId"] != "plot-2"]
        nab["variables"] = [v for v in nab["variables"] if v["name"] not in ("valve_2", "flow_2")]
        for v in nab["variables"]:
            if v.get("expression"):
                v["expression"] = v["expression"].replace(" or valve_2", "").replace("valve_2 or ", "")
        nab["rules"] = [r for r in nab["rules"] if not any(t in json.dumps(r) for t in ("valve_2", "flow_2", "plot-2"))]

    b.reload(drop_field_2)
    b.run(3)
    assert (b.out("pump-1"), b.out("valve-2")) == (False, False)
    assert [w[0] for w in b.io.writes[mark:] if w[2] is False] == ["pump-1", "valve-2"], "pump before valve"
    assert b.rt.snapshot()["assets"]["valve-2"]["coil"] is False


# --------------------------------------------- schedules a reload took out

def _schedule_on_a(**extra):
    bundle = _flow_and_two_valves()
    bundle["nab"]["rules"] = []
    bundle["nab"]["schedules"] = [{
        "id": "valve_a_morning", "title": "Valve A every morning", "enabled": True,
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "start": "06:00", "durationMinutes": 30,
        "startWindowMinutes": 60, "actions": [{"type": "set", "variable": "a", "value": True}],
        "endActions": [{"type": "set", "variable": "a", "value": False}], **extra}]
    return bundle


def _at_six():
    return datetime(2026, 9, 7, 5, 59, tzinfo=TZ)


def test_a_running_schedule_taken_out_still_ends_when_it_would_have():
    """Before, it never ended: what its start switched on stayed on (the reload kept the level)."""
    b = Bench(_schedule_on_a(), start=_at_six())
    b.run(120)
    assert b.out("valve-a") is True
    b.reload(lambda bundle: bundle["nab"].__setitem__("schedules", []))
    b.run(27 * 60, tick=10)  # 06:28
    assert b.out("valve-a") is True, "the reload neither ends it early nor restarts it"
    b.run(3 * 60, tick=10)  # 06:31
    assert b.out("valve-a") is False
    assert [e["type"] for e in b.sink.events if e["type"].startswith("schedule_")] == ["schedule_started", "schedule_ended"]
    b.run(3 * 3600, tick=60)
    assert b.out("valve-a") is False and len(b.events("schedule_ended")) == 1


def test_a_running_schedule_taken_out_ends_through_the_names_it_was_written_with():
    """The valve's NAB variable is renamed in the same reload: the end still closes the same valve."""
    b = Bench(_schedule_on_a(), start=_at_six())
    b.run(120)

    def rename(bundle):
        bundle["nab"]["schedules"] = []
        next(v for v in bundle["nab"]["variables"] if v["name"] == "a")["name"] = "valve_a"

    b.reload(rename)
    b.run(32 * 60, tick=10)
    assert b.out("valve-a") is False


@pytest.mark.parametrize("named", ["plotId", "run_plot only"])
def test_a_running_plot_schedule_taken_out_stops_its_plot_on_time(named):
    bundle = _farm()
    s = bundle["nab"]["schedules"][0]
    s["enabled"] = True
    if named == "run_plot only":
        s.pop("plotId")  # its actions name the plot: run_plot plot-1 … stop_plot plot-1
    b = Bench(bundle, start=_at_six())
    b.io.set_input("flow-1", "sensor.flow", True)
    b.run(120)
    assert (b.out("pump-1"), b.out("valve-1")) == (True, True)
    b.reload(lambda bundle: bundle["nab"].__setitem__("schedules", []))
    b.run(27 * 60, tick=10)
    assert (b.out("pump-1"), b.out("valve-1")) == (True, True)
    b.run(3 * 60, tick=10)
    assert (b.out("pump-1"), b.out("valve-1")) == (False, False), "06:30, not when a protection rule fires"
    assert {"type": "plot_run_ended", "plotId": "plot-1", "source": "plot_1_morning"} in b.sink.events


def test_a_running_schedule_whose_end_changed_still_switches_off_what_it_started():
    b = Bench(_schedule_on_a(), start=_at_six())
    b.run(120)
    assert b.out("valve-a") is True

    def now_on_b(bundle):
        s = bundle["nab"]["schedules"][0]
        s["actions"] = [{"type": "set", "variable": "b", "value": True}]
        s["endActions"] = [{"type": "set", "variable": "b", "value": False}]

    b.reload(now_on_b)
    b.run(32 * 60, tick=10)
    assert b.out("valve-a") is False, "the end it was started with ran too"
    assert len(b.events("schedule_ended")) == 1


def test_a_running_schedule_taken_out_and_put_back_is_the_same_run():
    original = _schedule_on_a()
    b = Bench(original, start=_at_six())
    b.run(120)
    schedules = copy.deepcopy(b.bundle["nab"]["schedules"])
    b.reload(lambda bundle: bundle["nab"].__setitem__("schedules", []))
    b.run(60)
    b.reload(lambda bundle: bundle["nab"].__setitem__("schedules", schedules))
    b.run(27 * 60, tick=10)
    assert b.out("valve-a") is True
    b.run(3 * 60, tick=10)
    assert b.out("valve-a") is False
    kinds = [e["type"] for e in b.sink.events if e["type"].startswith("schedule_")]
    assert kinds == ["schedule_started", "schedule_ended"], "started once, ended once, at 06:30"


# ------------------------------------------------ releases and other lines

def _line_bench(extra_assets=()):
    log: list = []
    chips = [ChipInfo("/dev/gpiochip0", "gpiochip0", "pinctrl-bcm2835", 54)]
    lines = LineIO(chips=lambda: chips, opener=lambda path, line, direction: _Line(log, line, direction))
    bundle = _two_valves()
    for card, line in zip(bundle["assets"], (27, 22)):
        card["ports"]["device.power"] = {"kind": "gpio", "chip": "pinctrl-bcm2835", "line": line, "direction": "out"}
    bundle["assets"].extend(copy.deepcopy(list(extra_assets)))
    return Bench(bundle, io=HardwareIO(sensor_by_key={}, lines=lines)), log


def test_two_reloads_before_a_pass_never_switch_off_a_line_the_program_holds_on():
    """Reload 1 takes valve-b (line 22) out; reload 2, before any pass, moves valve-a onto line 22.
    Before, reload 1's release still ran: line 22 OFF and closed, then reopened ON."""
    b, log = _line_bench()
    b.run(1)
    for aid in ("valve-a", "valve-b"):
        assert b.rt.apply_command({"type": "setPower", "assetId": aid, "sourceKey": "device.power", "value": 1})["accepted"]
    b.run(2)
    assert ("write", 27, True) in log and ("write", 22, True) in log
    mark = len(log)
    b.reload(_drop_card)

    def a_to_22(bundle):
        bundle["assets"][0]["ports"]["device.power"]["line"] = 22

    b.reload(a_to_22)
    b.run(3)
    assert log[mark:] == [("write", 27, False), ("write", 22, True)]  # 0.4.1: line 27 kept driven at off


def test_an_output_line_re_stamped_as_an_input_is_never_driven_again():
    """valve-a moves from line 27 to 23 and the flow switch is now read on line 27. Before, the
    release requested line 27 as an output and drove it LOW for one pass, against the sensor."""
    flow = {"assetId": "flow-1", "assetType": "flow_switch", "aab": _template("aab_flow_switch.json"),
            "ports": {"sensor.flow": {"kind": "gpio", "chip": "pinctrl-bcm2835", "line": 5, "direction": "in"}}}
    b, log = _line_bench([flow])
    b.run(1)
    assert b.rt.apply_command({"type": "setPower", "assetId": "valve-a", "sourceKey": "device.power", "value": 1})["accepted"]
    b.run(2)
    mark = len(log)

    def rewire(bundle):
        bundle["assets"][0]["ports"]["device.power"]["line"] = 23
        bundle["assets"][2]["ports"]["sensor.flow"]["line"] = 27

    b.reload(rewire)
    b.run(3)
    on_27 = [e for e in log[mark:] if e[1] == 27]
    assert [e for e in on_27 if e[0] == "write" or (e[0] == "open" and e[2] != "in")] == [], on_27
    assert ("open", 23, "high") in log[mark:] and ("write", 23, True) in log[mark:]
