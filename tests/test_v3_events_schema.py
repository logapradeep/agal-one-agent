"""Every event the v3 runtime sends is a valid event document (contracts v1.15.0 event.schema.json).

The events of the golden-vector timelines and of each built-in (guard trip, valve fault and reset, E-stop,
power cut and resume, pause and resume) are collected and validated with AJV, as the contracts validate
theirs (`AJV_PATH` = an ajv/dist/2020.js, e.g. Agal/contracts/programs/node_modules/ajv/dist/2020.js).
Skipped when AJV_PATH is not set or node is not installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

import _contracts
import v3_vectors as V
from v3_bench import ESTOP, MAINS, PUMP, UPS, V2, Bench, pumphouse
from test_v3_valves import both_valves

AJV = os.environ.get("AJV_PATH")
pytestmark = pytest.mark.skipif(not AJV or not shutil.which("node"), reason="AJV_PATH not set or node not installed")


def _scenarios(tmp_path) -> list[dict]:
    out: list[dict] = []
    for name in ("instruction-switch.json", "rule-edit-swap.json", "rewire-swap.json"):
        run = V.VectorRun(V.load(name), str(tmp_path / name))
        run.run()
        out += [dict(e, _source=s) for e, s in zip(run.sink.events, run.sink.sources)]
    b = Bench(both_valves(pumphouse()), state_dir=str(tmp_path / "builtins"))
    b.step()
    b.until(6, 0, 3)
    b.set(PUMP, "sensor.current", 6.8)
    b.until(6, 3)  # the baseline is learnt (06:01:16)
    b.set(UPS, "sensor.power", False)          # valve fault
    b.run(2)
    b.set(UPS, "sensor.power", True)
    b.run(1)  # the supply is sensed again (a reset is refused until it is)
    assert b.rt.apply_command({"type": "resetValve", "assetId": V2, "actor": {"uid": "u_owner01", "via": "app"}})["accepted"]
    b.run(5)
    b.set(ESTOP, "sensor.contact", False)      # E-stop
    b.run(2)
    b.set(ESTOP, "sensor.contact", True)
    b.run(1)
    b.set(ESTOP, "sensor.reset", True)
    b.run(5)
    b.set(MAINS, "sensor.power", False)        # power cut and resume
    b.run(5)
    b.set(MAINS, "sensor.power", True)
    b.run(40)
    b.set(PUMP, "sensor.current", 2.0)         # dry run
    b.run(10)
    b.rt.apply_command({"type": "pauseAutomation", "actor": {"uid": "u_op", "via": "assist"}, "stopAll": True})
    b.run(2)
    b.rt.apply_command({"type": "resumeAutomation", "actor": {"uid": "u_op", "via": "app"}})
    b.run(2)
    b.rt.stop(safe_state=True)  # agent 0.4.4: the clean stop closes the single-pulse valve (valve_closed, shutdown)
    out += [dict(e, _source=s) for e, s in zip(b.sink.events, b.sink.sources)]
    return out


def test_every_event_the_runtime_sends_is_a_valid_event_document(tmp_path):
    raw = _scenarios(tmp_path)
    types = {e["type"] for e in raw}
    for t in ("instruction_switched", "output_released", "valve_fault", "valve_reset", "estop_pressed", "estop_cleared",
              "power_cut", "power_restored", "watering_resumed", "guard_tripped", "watering_not_finished",
              "automation_paused", "automation_resumed", "valve_closed"):
        assert t in types, t
    # agent 0.4.4 (contracts v1.26.0): both causes of valve_closed are among them — the vectors' clean start, the stop
    assert {e["valve"]["cause"] for e in raw if e["type"] == "valve_closed"} == {"start_up", "shutdown"}
    events = [{"type": e["type"], "source": e["_source"],
               "payload": {k: v for k, v in e.items() if k not in ("type", "_source")}} for e in raw]
    path = tmp_path / "events.json"
    path.write_text(json.dumps(events, default=str))
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run(["node", os.path.join(here, "validate_events.mjs"), os.path.join(_contracts.CONTRACTS, "schemas"), str(path)],
                       capture_output=True, text=True, env=dict(os.environ, AJV_PATH=AJV), timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
