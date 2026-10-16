"""Agent 0.4.0 = bundle v3 built on the released 0.3.4: what the merge had to join (contracts v1.15.0 SYNC.md
item 5, re-checked against v1.14.2 in item 8).

- An OTA update waits while the node has anything ON (0.3.4's busy-wait, ota_updater): the agent's runtime is
  now a ProgramHost, which names what either language's program has ON — a language-1 program exactly as
  agent 0.3.4's BlockRuntime does, a language-3 one its motors, its open valves (continuous or latching) and
  its running schedule runs.
- The bundle-v3 built-ins' events (source ``node``) go through 0.3.4's event queue — the only way they reach
  the cloud — and are, like the protection trips they replace on a migrated node, the last to leave it when
  it is full.

The end-to-end proofs — main() with fakes, every command type through the signed-command gate, and a v1
bundle on 0.4.0 compared with the released 0.3.4 — are in tests/test_v1_parity.py.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime

import pytest

import _contracts
from agal_one_agent import main as agent_main
from agal_one_agent.blocks.clock import SimClock
from agal_one_agent.blocks.host import ProgramHost
from agal_one_agent.blocks.io import SimulatedIO
from agal_one_agent.blocks.runtime import BlockRuntime, RecordingSink
from agal_one_agent.blocks.simulate import build_bundle_from_defaults
from agal_one_agent.node_events import NodeEvents
from v3_bench import PUMP, TZ, V1, V2, Bench, pumphouse

ACTOR = {"uid": "u_owner", "via": "app"}


@pytest.fixture(autouse=True)
def _no_legacy_outputs(monkeypatch):
    """outputs_on_for also names the legacy GPIO / PWM outputs (module state other tests leave behind)."""
    from agal_one_agent.handlers import gpio_handler, pwm_handler
    monkeypatch.setattr(gpio_handler, "_levels", {})
    monkeypatch.setattr(pwm_handler, "_duties", {})


def _farm():
    if not os.path.isdir(_contracts.DEFAULTS):
        pytest.skip(f"defaults not found at {_contracts.DEFAULTS}")
    return build_bundle_from_defaults(_contracts.DEFAULTS, "farm")


# ------------------------------------------------------------------ what an OTA update waits on

def test_a_program_host_on_a_v1_bundle_names_what_is_on_exactly_as_the_bare_runtime(tmp_path):
    """The same session through 0.3.4's BlockRuntime and through 0.4.0's ProgramHost: the same answer at every
    second (outputs and plot runs), so an update waits exactly as long as on 0.3.4."""
    def session(make):
        clock = SimClock(datetime(2026, 9, 7, 5, 0, tzinfo=TZ))
        rt = make(SimulatedIO(), clock)
        rt.compile(_farm())
        seen = []

        def run(seconds):
            for _ in range(seconds):
                rt.step()
                clock.advance(1)
                seen.append(agent_main.outputs_on_for(rt))

        run(1)
        rt.apply_command({"type": "runPlot", "plotId": "plot-1"})
        run(8)
        rt.apply_command({"type": "stopPlot", "plotId": "plot-1"})
        run(3)
        return seen

    bare = session(lambda io, c: BlockRuntime(io, RecordingSink(), clock=c, state_dir=str(tmp_path / "bare")))
    host = session(lambda io, c: ProgramHost(io, RecordingSink(), clock=c, state_dir=str(tmp_path / "host")))
    assert host == bare
    assert any("Pump (device.power)" in s and "the run of plot plot-1" in s for s in host)
    assert host[0] == [] and host[-1] == []


def test_no_program_and_no_host_name_nothing():
    host = ProgramHost(SimulatedIO(), RecordingSink(), clock=SimClock())
    assert host.outputs_on() == [] and agent_main.outputs_on_for(host) == []


def test_a_v3_program_names_its_running_run_its_motor_and_its_open_valves():
    b = Bench(pumphouse())  # 05:59:58 — the pump house's morning run starts at 06:00
    b.step()
    assert b.rt.outputs_on() == []
    b.until(6, 0, 10)
    assert b.rt.outputs_on() == ["Borewell pump (device.power)", "Valve 1 (valve open)",
                                 "the run of schedule morning on field:field_north01"]


def test_a_v3_program_names_a_latching_valve_left_open_by_hand_and_stops_naming_it_when_closed():
    b = Bench(pumphouse(), start=datetime(2026, 10, 15, 8, 0, tzinfo=TZ), load=False)  # no run; the host loads it
    host = ProgramHost(b.io, b.sink, clock=b.clock, state_dir=b.state_dir, node_uid="node_pumphouse")
    host.compile(pumphouse())
    host.step()
    assert agent_main.outputs_on_for(host) == []
    assert host.apply_command({"type": "pauseAutomation", "stopAll": False, "actor": ACTOR})["accepted"]
    host.step()
    for aid, key in ((V2, "valve.open"), (V1, "valve.open"), (PUMP, "device.power")):
        assert host.apply_command({"type": "setPower", "assetId": aid, "sourceKey": key, "value": True})["accepted"]
        b.clock.advance(1)
        host.step()
    assert agent_main.outputs_on_for(host) == ["Borewell pump (device.power)", "Valve 1 (valve open)", "Valve 2 (valve open)"]
    for aid, key in ((PUMP, "device.power"), (V1, "valve.open"), (V2, "valve.open")):
        assert host.apply_command({"type": "setPower", "assetId": aid, "sourceKey": key, "value": False})["accepted"]
        b.clock.advance(1)
        host.step()
    assert agent_main.outputs_on_for(host) == []


def test_a_latching_valve_whose_coil_is_not_known_off_is_named():
    b = Bench(pumphouse(), start=datetime(2026, 10, 15, 8, 0, tzinfo=TZ))
    b.step()
    drv = b.rt.prog.valves[V2]
    drv.coil_state[drv.close_key] = None  # an off-write that failed: the coil may be on
    assert b.rt.outputs_on() == ["Valve 2 (a valve coil not known off)"]


# ------------------------------------------------------------------ the v3 events in 0.3.4's queue

class _Gate:
    def __init__(self):
        self.release = threading.Event()
        self.tries: list[dict] = []
        self.delivered: list[str] = []

    def report_sensor_event(self, event, *, event_id, timestamp, timestamp_uncertain=False):
        self.tries.append(event)
        assert self.release.wait(10)
        self.delivered.append(event["type"])
        return 200


def test_the_built_ins_events_are_kept_longest_when_the_event_queue_is_full():
    rep = _Gate()
    events = NodeEvents(rep, backoff_s=(0.01,), rate_limited_s=0.05, budget_per_hour=0, clock_ok=lambda: True, cap=3)
    events.submit({"type": "schedule_started", "source": "program"})  # taken by the worker, held
    deadline = time.monotonic() + 5
    while not rep.tries and time.monotonic() < deadline:
        time.sleep(0.01)
    events.submit({"type": "valve_fault", "source": "node", "sourceKey": V2})
    events.submit({"type": "command_applied", "source": "program"})
    events.submit({"type": "estop_pressed", "source": "node"})
    events.submit({"type": "guard_tripped", "source": "node", "sourceKey": PUMP})  # full: the program event leaves
    rep.release.set()
    assert events.wait_idle(5)
    assert rep.delivered == ["schedule_started", "valve_fault", "estop_pressed", "guard_tripped"]
    assert events.given_up == 1


def test_a_v3_event_reaches_the_event_queue_with_its_source_and_its_card():
    """CloudSink.event (the v3 runtime's sink) → publish_event → 0.3.4's NodeEvents: source node, the card as
    sourceKey for a valve or motor event (functions.yaml 1.11.1: filed on the valve card)."""
    from agal_one_agent.blocks.cloud import CloudSink
    from agal_one_agent.config import MqttConfig
    from agal_one_agent.mqtt_client import AgalOneMqttClient
    from agal_one_agent.node_events import wire_node_events

    mqtt = AgalOneMqttClient(MqttConfig(broker="broker.example", username="shared", status_topic="agal/n/status",
                                        commands_topic="agal/n/commands"))
    posted = []

    class Rep:
        def report_sensor_event(self, event, *, event_id, timestamp, timestamp_uncertain=False):
            posted.append((event, event_id, timestamp_uncertain))
            return 200

    events = wire_node_events(mqtt, Rep(), offline=False, clock_ok=lambda: False)
    sink = CloudSink(mqtt, None, None, firmware_version="0.4.0")
    b = Bench(pumphouse(), start=datetime(2026, 10, 15, 8, 0, tzinfo=TZ), persisted=False)  # first start: V2 in FAULT
    b.rt.sink = sink
    b.step()
    assert events.wait_idle(5)
    fault = [(e, i, u) for e, i, u in posted if e["type"] == "valve_fault"]
    assert fault, [e["type"] for e, _, _ in posted]
    e, event_id, uncertain = fault[0]
    assert e["source"] == "node" and e["sourceKey"] == V2 and e["payload"]["valve"]["reason"] == "position_unknown"
    assert event_id and uncertain is True  # no trusted clock: the backend files it at receive time
    sink.close()
