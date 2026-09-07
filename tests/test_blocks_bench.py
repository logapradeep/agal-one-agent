"""Bench physics (blocks.simulated_io): a runPlot makes the plot's flow switch
read flowing once the valve has energised and the pump has started, and the
pump draws current; stopPlot returns both to rest. Deterministic: SimClock +
SimulatedIO, physics stepped by hand."""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from agal_one_agent.blocks.bench import BenchPhysics
from agal_one_agent.blocks.clock import SimClock
from agal_one_agent.blocks.io import SimulatedIO
from agal_one_agent.blocks.runtime import BlockRuntime, RecordingSink
from agal_one_agent.blocks.simulate import build_bundle_from_defaults
from agal_one_agent.config import AgentConfig

HERE = os.path.dirname(__file__)
DEFAULTS = os.path.normpath(os.path.join(HERE, "..", "..", "..", "contracts", "programs", "defaults"))
TZ = ZoneInfo("Asia/Kolkata")


def _rig():
    clock = SimClock(datetime(2026, 9, 7, 5, 0, tzinfo=TZ))
    io = SimulatedIO()
    sink = RecordingSink()
    rt = BlockRuntime(io, sink, clock=clock)
    rt.compile(build_bundle_from_defaults(DEFAULTS, "farm"))
    physics = BenchPhysics(rt, io, flow_delay=5.0, current_delay=2.0)
    return clock, io, sink, rt, physics


def _run(clock, rt, physics, seconds, now):
    for _ in range(int(seconds)):
        physics.step(now)
        rt.step()
        clock.advance(1.0)
        now += 1.0
    return now


def test_run_plot_produces_flow_and_current():
    clock, io, sink, rt, physics = _rig()
    now = 1000.0
    now = _run(clock, rt, physics, 2, now)
    assert not io.output("pump-1", "device.power", False)

    rt.apply_command({"type": "runPlot", "plotId": "plot-1", "commandId": "c1"})
    now = _run(clock, rt, physics, 12, now)
    assert io.output("valve-1", "device.power") is True
    assert io.output("pump-1", "device.power") is True, "pump starts after the valve has energised"
    assert io.read_input(type("P", (), {"asset_id": "flow-1", "source_key": "sensor.flow"})()) is True
    current = io.read_input(type("P", (), {"asset_id": "pump-1", "source_key": "sensor.current"})())
    assert 4.0 < current < 5.0, current

    rt.apply_command({"type": "stopPlot", "plotId": "plot-1", "commandId": "c2"})
    now = _run(clock, rt, physics, 8, now)
    assert not io.output("pump-1", "device.power", False)
    assert not io.output("valve-1", "device.power", False)
    assert io.read_input(type("P", (), {"asset_id": "flow-1", "source_key": "sensor.flow"})()) is False
    assert io.read_input(type("P", (), {"asset_id": "pump-1", "source_key": "sensor.current"})()) == 0.0


def test_config_flag_parses(tmp_path):
    base = "node:\n  uid: n\n  name: n\n  auth_token: t\nmqtt:\n  broker: b\n"
    on = tmp_path / "on.yaml"
    on.write_text(base + "blocks:\n  simulated_io: true\n")
    off = tmp_path / "off.yaml"
    off.write_text(base)
    assert AgentConfig.from_yaml(str(on)).blocks.simulated_io is True
    assert AgentConfig.from_yaml(str(off)).blocks.simulated_io is False
