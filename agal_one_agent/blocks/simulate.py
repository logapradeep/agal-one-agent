"""agal-one-agent-sim — run a compiled bundle against a scenario without hardware.

    agal-one-agent-sim --bundle bundle.json --scenario scenario.json [--tick 1] [--json]

Scenario file:
    {
      "start": "2026-09-07T05:59:00+05:30",   # wall clock at t=0 (optional)
      "steps": [
        {"at": 0,   "input": {"asset": "pump-1", "sourceKey": "sensor.current", "value": 0}},
        {"at": 5,   "command": {"type": "runPlot", "plotId": "plot-1", "durationMinutes": 1}},
        {"at": 20,  "input": {"asset": "pump-1", "sourceKey": "sensor.current", "value": 4.5}},
        {"at": 120, "assert": {"output": ["pump-1", "device.power"], "equals": false}}
      ],
      "until": 200
    }
The runner advances a SimClock one tick at a time, applies inputs/commands at
their times, checks assertions, and prints outputs, alerts, events and logs.
Exit code 1 if any assertion fails. This is the "simulated bench" of _audit/99 P1;
tests/test_blocks_runtime.py drives the same runtime directly.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from .clock import SimClock
from .io import SimulatedIO
from .runtime import BlockRuntime, RecordingSink, CompileError


def build_bundle_from_defaults(defaults_dir: str, kit: str = "farm") -> dict:
    """Compose a compiled bundle from Agal/contracts/programs/defaults for the simulator."""
    import os

    def load(name):
        with open(os.path.join(defaults_dir, name)) as f:
            return json.load(f)

    if kit == "farm":
        nab = load("nab_farm.json")
        subst = {"$pump": "pump-1", "$valve1": "valve-1", "$valve2": "valve-2", "$flow1": "flow-1", "$flow2": "flow-2", "$plot1": "plot-1", "$plot2": "plot-2"}
        assets = [
            {"assetId": "pump-1", "assetType": "motor_controller", "name": "Pump", "aab": load("aab_pump.json"), "ports": {"device.power": {"kind": "gpio", "gpioNumber": 17, "direction": "out"}, "sensor.current": {"kind": "i2c", "busId": 1, "addr": 72, "channel": 0}}, "safeState": "off", "phaseType": "single_phase", "ratedCurrentA": 5},
            {"assetId": "valve-1", "assetType": "valve_controller", "name": "Valve 1", "aab": load("aab_valve.json"), "ports": {"device.power": {"kind": "gpio", "gpioNumber": 27, "direction": "out"}}},
            {"assetId": "valve-2", "assetType": "valve_controller", "name": "Valve 2", "aab": load("aab_valve.json"), "ports": {"device.power": {"kind": "gpio", "gpioNumber": 22, "direction": "out"}}},
            {"assetId": "flow-1", "assetType": "flow_switch", "name": "Flow 1", "aab": load("aab_flow_switch.json"), "ports": {"sensor.flow": {"kind": "gpio", "gpioNumber": 5, "direction": "in"}}},
            {"assetId": "flow-2", "assetType": "flow_switch", "name": "Flow 2", "aab": load("aab_flow_switch.json"), "ports": {"sensor.flow": {"kind": "gpio", "gpioNumber": 6, "direction": "in"}}},
        ]
    else:
        nab = load("nab_building.json")
        subst = {"$tank": "tank-1", "$pump": "pump-1", "$light1": "light-1"}
        assets = [
            {"assetId": "tank-1", "assetType": "level_sensor", "name": "Tank", "aab": load("aab_level_sensor.json"), "ports": {"sensor.level.percent": {"kind": "gpio", "gpioNumber": 23, "direction": "in"}}},
            {"assetId": "pump-1", "assetType": "motor_controller", "name": "Pump", "aab": load("aab_pump.json"), "ports": {"device.power": {"kind": "gpio", "gpioNumber": 17, "direction": "out"}, "sensor.current": {"kind": "i2c", "busId": 1, "addr": 72, "channel": 0}}, "safeState": "off", "ratedCurrentA": 3},
            {"assetId": "light-1", "assetType": "lighting_circuit", "name": "Lights", "aab": load("aab_lighting_circuit.json"), "ports": {"device.power": {"kind": "gpio", "gpioNumber": 24, "direction": "out"}, "sensor.wall_switch": {"kind": "gpio", "gpioNumber": 25, "direction": "in"}}},
        ]
    text = json.dumps(nab)
    for k, v in subst.items():
        text = text.replace(k, v)
    nab = json.loads(text)
    return {"version": 1, "compiledAt": "2026-09-07T00:00:00Z", "timezone": "Asia/Kolkata", "location": {"latitude": 11.0, "longitude": 77.0}, "nab": nab, "assets": assets}


def run_scenario(bundle: dict, scenario: dict, tick: float = 1.0, quiet: bool = False) -> tuple[bool, RecordingSink, SimulatedIO]:
    start = scenario.get("start")
    clock = SimClock(datetime.fromisoformat(start) if start else None)
    io = SimulatedIO()
    sink = RecordingSink()
    rt = BlockRuntime(io, sink, clock=clock)
    rt.compile(bundle)
    steps = sorted(scenario.get("steps", []), key=lambda s: s.get("at", 0))
    until = float(scenario.get("until", (steps[-1]["at"] + 5) if steps else 60))
    t, ok, i = 0.0, True, 0
    while t <= until:
        while i < len(steps) and steps[i].get("at", 0) <= t:
            s = steps[i]
            i += 1
            if "input" in s:
                inp = s["input"]
                io.set_input(inp["asset"], inp["sourceKey"], inp["value"])
            if "command" in s:
                rt.apply_command(s["command"])
            if "assert" in s:
                a = s["assert"]
                actual = io.output(a["output"][0], a["output"][1]) if "output" in a else rt.snapshot()["nab"].get(a.get("nab"))
                good = actual == a.get("equals")
                ok = ok and good
                if not quiet:
                    print(f"[t={t:>6.0f}] assert {a} → {actual} {'OK' if good else 'FAIL'}")
        rt.step()
        clock.advance(tick)
        t += tick
    if not quiet:
        for e in sink.alerts:
            print("alert:", e)
        for e in sink.events:
            print("event:", e)
        for e in sink.logs:
            print("log:", e)
        print("outputs:", {f"{k[0]}.{k[1]}": v for k, v in io.outputs.items()})
    return ok, sink, io


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Run an automation-block bundle against a scenario (no hardware)")
    ap.add_argument("--bundle", help="compiled bundle JSON (default: farm kit built from contracts defaults)")
    ap.add_argument("--defaults", help="Agal/contracts/programs/defaults directory (when --bundle is not given)")
    ap.add_argument("--kit", default="farm", choices=["farm", "building"])
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--tick", type=float, default=1.0)
    ap.add_argument("--json", action="store_true", help="print the final snapshot as JSON only")
    args = ap.parse_args(argv)
    if args.bundle:
        with open(args.bundle) as f:
            bundle = json.load(f)
    else:
        if not args.defaults:
            ap.error("--defaults is required without --bundle")
        bundle = build_bundle_from_defaults(args.defaults, args.kit)
    with open(args.scenario) as f:
        scenario = json.load(f)
    try:
        ok, sink, io = run_scenario(bundle, scenario, args.tick, quiet=args.json)
    except CompileError as e:
        print(f"program rejected: {e}", file=sys.stderr)
        sys.exit(2)
    if args.json:
        print(json.dumps({"ok": ok, "alerts": sink.alerts, "events": sink.events, "outputs": {f"{k[0]}.{k[1]}": v for k, v in io.outputs.items()}}, indent=2))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
