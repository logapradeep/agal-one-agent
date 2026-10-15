"""The bundle-v3 golden vectors (Agal/contracts/programs/vectors, contracts v1.15.0) through the agent's runtime.

The runtime vectors are SimClock timelines in 1 s passes (programs/README.md §7.12): an instruction switch
while Valve 1 and the pump run, a rule edit during the morning run, a link change during it. The compile
vector's two bundles must load and run on their nodes.
"""

from __future__ import annotations

import copy
import os

import pytest

import v3_vectors as V
from v3_bench import Bench, fieldbox, pumphouse

pytestmark = pytest.mark.skipif(not os.path.isdir(V.VECTORS), reason=f"vectors not found at {V.VECTORS}")

RUNTIME_VECTORS = ["instruction-switch.json", "rule-edit-swap.json", "rewire-swap.json"]


@pytest.mark.parametrize("name", RUNTIME_VECTORS)
def test_runtime_vector(name, tmp_path):
    vec = V.load(name)
    run = V.VectorRun(vec, str(tmp_path))
    failures = run.run()
    assert failures == [], "\n".join(failures)
    # Every second from the first step to the last was a pass.
    first, last = run.at(vec["steps"][0]["at"]), run.at(vec["steps"][-1]["at"])
    assert run.passes == int((last - first).total_seconds()) + 1


def test_the_runner_notices_a_wrong_expectation(tmp_path):
    """The runner compares what it is told to: a vector with a wrong write fails."""
    vec = copy.deepcopy(V.load("instruction-switch.json"))
    step = next(s for s in vec["steps"] if s["at"] == "2026-10-15T06:10:00")
    step["expect"]["writes"] = [{"assetId": "asset_valve002", "sourceKey": "valve.open_coil", "pulseMs": 50}]
    step["expect"]["levels"]["asset_pump0001 device.power"] = False
    failures = V.VectorRun(vec, str(tmp_path)).run()
    assert any("writes" in f for f in failures) and any("level asset_pump0001" in f for f in failures)


def test_instruction_switch_keeps_the_motor_and_valve_1_on(tmp_path):
    """ADR-029 decision 8, the founder's case: 'add Plot 2 while Plot 1 runs' — on approval Valve 2 opens and
    the motor and Valve 1 do not stop, not even for a pass."""
    vec = V.load("instruction-switch.json")
    seen = []

    def watch(step, run):
        if "2026-10-15T06:10:00" <= step["at"] <= "2026-10-15T06:29:59":
            seen.append((step["at"], run.rt.level("asset_pump0001", "device.power"), run.rt.level("asset_valve001", "valve.open")))

    run = V.VectorRun(vec, str(tmp_path))
    pump_off_writes = []
    orig = run.io.write_output

    def spy(port, value):
        if port.asset_id in ("asset_pump0001", "asset_valve001") and value is False:
            pump_off_writes.append((run.clock.now("Asia/Kolkata").strftime("%H:%M:%S"), port.asset_id))
        orig(port, value)

    run.io.write_output = spy
    assert run.run(watch) == []
    assert all(p and v for _, p, v in seen)
    # The only switch-offs of the pump and Valve 1 are the cold load and the end of the run at 06:30.
    assert [t for t, _ in pump_off_writes if t.startswith("06:1") or t.startswith("06:2")] == []


def test_both_bundles_of_the_compile_vector_load_and_run(tmp_path):
    """two-node-layout.json: each node's bundle loads on the node; the field box runs its part (Valve 3 at
    06:30 for 20 min) and its interlock (rule slot 2, stop only)."""
    ph = Bench(pumphouse(), state_dir=str(tmp_path / "ph"))
    assert set(ph.rt.prog.guards) == {"asset_pump0001"}
    assert set(ph.rt.prog.valves) == {"asset_valve001", "asset_valve002"}
    assert ph.rt.prog.estop is not None and ph.rt.prog.power is not None
    assert [r.key for r in ph.rt.prog.rules] == ["field:field_north01/rule/1"]
    assert ph.rt.prog.rules[0].interlock is False
    fb = Bench(fieldbox(), state_dir=str(tmp_path / "fb"), inputs={("asset_flow0001", "sensor.flow"): False})
    assert fb.rt.prog.estop is None and fb.rt.prog.power is None
    assert fb.rt.prog.rules[0].interlock is True
    fb.step()
    fb.until(6, 30)
    assert fb.level("asset_valve003", "valve.open") is True and fb.physical("asset_valve003", "valve.coil") is True
    fb.until(6, 50)
    assert fb.level("asset_valve003", "valve.open") is False


def test_the_rewired_field_box_lets_the_flow_switch_go_without_a_write(tmp_path):
    """rewire-swap.json, the field box's side: its new assets no longer carry the flow switch (an input)."""
    vec = V.load("rewire-swap.json")
    fb = Bench(fieldbox(), state_dir=str(tmp_path), inputs={("asset_flow0001", "sensor.flow"): True})
    fb.step()
    fb.until(6, 35)
    assert fb.level("asset_valve003", "valve.open") is True
    m = fb.mark()
    b2 = copy.deepcopy(fb.rt.bundle)
    b2["version"] = 2
    b2["assets"] = copy.deepcopy(vec["expect"]["assets"]["node_fieldbox"])
    fb.rt.compile(b2)
    fb.run(5)
    assert fb.writes_since(m) == []
    assert fb.level("asset_valve003", "valve.open") is True
    assert "asset_flow0001" not in fb.rt.prog.cards


def _slot3(program: dict) -> dict:
    return {"key": "field:field_north01/rule/3", "containerKind": "field", "containerId": "field_north01",
            "slot": 3, "version": 1, "program": copy.deepcopy(program)}


@pytest.mark.parametrize("case", [0, 1])
def test_a_cross_node_rule_cannot_be_loaded_on_either_node(case, tmp_path):
    """cross-node-refused.json: the checker refuses these (cross_node); a node handed one refuses it too —
    it names a card that is not on that node — and keeps its program."""
    from agal_one_agent.blocks.runtime import CompileError
    program = V.load("cross-node-refused.json")["cases"][case]["program"]
    for make in (pumphouse, fieldbox):
        b = make()
        b["rules"].append(_slot3(program))
        with pytest.raises(CompileError, match="not on this node"):
            Bench(b, state_dir=str(tmp_path / make.__name__))


def test_the_rule_that_passed_runs_on_its_node(tmp_path):
    """cross-node-refused.json case 3 (passed, node_fieldbox): Valve 3 open with no flow for a minute closes."""
    case = V.load("cross-node-refused.json")["cases"][2]
    assert case["expect"]["nodeUid"] == "node_fieldbox"
    b = fieldbox()
    b["rules"].append(_slot3(case["program"]))
    fb = Bench(b, state_dir=str(tmp_path), inputs={("asset_flow0001", "sensor.flow"): False})
    assert fb.rt.prog.block("field:field_north01/rule/3").interlock
    fb.step()
    fb.until(6, 30)
    assert fb.level("asset_valve003", "valve.open") is True
    fb.until(6, 31)
    assert fb.level("asset_valve003", "valve.open") is False  # stopped by the interlock
    fb.until(6, 45)
    assert fb.level("asset_valve003", "valve.open") is False  # the writer does not re-open it inside its run


def test_no_flow_after_start_is_an_interlock_not_a_guard_trip(tmp_path):
    """ADR-029 decision 1: 'stop the pump if there is no flow 10 s after start' is a rule (case 1 of
    cross-node-refused.json), which runs on the pump house once the rewire of rewire-swap.json moved the flow
    switch there. It stops the pump; the guard does not trip and nothing is locked out."""
    vec = V.load("rewire-swap.json")
    b = pumphouse()
    b["assets"] = copy.deepcopy(vec["bundles"]["v2"]["assets"])
    b["rules"].append(_slot3(V.load("cross-node-refused.json")["cases"][0]["program"]))
    ph = Bench(b, state_dir=str(tmp_path))
    ph.set("asset_flow0001", "sensor.flow", False)
    ph.step()
    ph.until(6, 0, 3)
    assert ph.pump_on()
    ph.set("asset_pump0001", "sensor.current", 6.8)
    ph.until(6, 0, 12)
    assert ph.pump_on()
    ph.until(6, 0, 13)
    assert ph.pump_on() is False and ph.level("asset_valve001", "valve.open") is True
    assert ph.events("guard_tripped") == [] and not ph.rt.prog.guards["asset_pump0001"].locked_out(ph.clock.monotonic())
