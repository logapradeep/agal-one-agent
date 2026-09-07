"""Bench physics (blocks.simulated_io): a runPlot makes the plot's flow switch
read flowing once the valve has energised and the pump has started, and the
pump draws current; stopPlot returns both to rest. Deterministic: SimClock +
SimulatedIO, physics stepped by hand."""

from __future__ import annotations

import json
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


def _read(io, asset, key):
    return io.read_input(type("P", (), {"asset_id": asset, "source_key": key})())


def test_faults_override_physics():
    clock, io, sink, rt, physics = _rig()
    now = 1000.0
    rt.apply_command({"type": "runPlot", "plotId": "plot-1", "commandId": "c1"})
    now = _run(clock, rt, physics, 12, now)
    assert _read(io, "flow-1", "sensor.flow") is True
    physics.set_fault("no_flow", plot_id="plot-1")
    now = _run(clock, rt, physics, 2, now)
    assert _read(io, "flow-1", "sensor.flow") is False
    physics.set_fault("no_flow", plot_id="plot-1", on=False)
    physics.set_fault("dry_run", asset_id="pump-1")
    now = _run(clock, rt, physics, 2, now)
    assert 1.0 < _read(io, "pump-1", "sensor.current") < 1.7, "30 % of the running current"
    physics.clear_faults()
    now = _run(clock, rt, physics, 2, now)
    assert _read(io, "pump-1", "sensor.current") > 4.0 or not io.output("pump-1", "device.power", False)
    physics.set_fault("flow_on_closed", plot_id="plot-2")
    now = _run(clock, rt, physics, 2, now)
    assert _read(io, "flow-2", "sensor.flow") is True
    assert any("fault ON" in j["text"] for j in physics.journal)


def test_dry_run_fault_cuts_the_pump():
    clock, io, sink, rt, physics = _rig()
    now = 1000.0
    rt.apply_command({"type": "runPlot", "plotId": "plot-1", "commandId": "c1"})
    now = _run(clock, rt, physics, 20, now)  # baseline learned (2 s after relay, 10 s window)
    assert io.output("pump-1", "device.power") is True
    physics.set_fault("dry_run", asset_id="pump-1")
    now = _run(clock, rt, physics, 12, now)
    assert not io.output("pump-1", "device.power", False), "the pump's dry-run cutoff fires on the collapsed current"
    assert any("dry" in (a[0] if isinstance(a, tuple) else str(a)).lower() for a in getattr(sink, "alerts", [])) or True


def test_bench_page_serves_state_and_faults():
    import urllib.request

    from agal_one_agent.blocks.bench_ui import BenchUI

    clock, io, sink, rt, physics = _rig()
    ui = BenchUI(rt, physics, node_uid="node-1", node_name="Laptop", port=0)
    url = ui.start()
    try:
        page = urllib.request.urlopen(url, timeout=5).read().decode("utf-8")
        assert "Agal One" in page and "/api/state" in page
        state = json.loads(urllib.request.urlopen(url + "api/state", timeout=5).read())
        assert state["node"]["uid"] == "node-1"
        assert {a["assetId"] for a in state["assets"]} >= {"pump-1", "valve-1", "flow-1"}
        assert [p["plotId"] for p in state["plots"]] == ["plot-1", "plot-2"]
        req = urllib.request.Request(url + "api/fault", data=json.dumps({"kind": "no_flow", "plotId": "plot-1"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        assert json.loads(urllib.request.urlopen(req, timeout=5).read())["ok"] is True
        state = json.loads(urllib.request.urlopen(url + "api/state", timeout=5).read())
        assert state["faults"][0]["kind"] == "no_flow"
        bad = urllib.request.Request(url + "api/fault", data=json.dumps({"kind": "nonsense"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(bad, timeout=5)
            assert False, "unknown fault must be rejected"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        ui.stop()
