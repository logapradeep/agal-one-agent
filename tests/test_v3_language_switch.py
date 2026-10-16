"""The switch of language — the migration (v1 → v3) and its rollback (v3 → v1) — on the node (contracts v1.15.1,
programs/README.md §7.5a; agent 0.4.0 blocks/host.py).

Proved here, with the founder's Pi as the first case (one card, Light: aab_lighting_circuit without its wall switch,
device.power on DO1 = line 17 of pinctrl-bcm2835, active low, the empty NAB — rebuilt from the contracts, no prod
read) and a small pump house as the second (a pump, a valve, the Light, a NAB schedule):

* nothing is written for an output the two programs share — the Light on stays on, a valve's coil becomes the
  continuous valve's valve.open, a pump keeps running — in both directions;
* the switch waits for the end of a run in progress, and a schedule that ran today does not run again;
* a line the new program does not drive is switched off and let go of (output_released);
* a runtime thread makes the switch at the end of one of its passes;
* a v3 program reports each card's outputs (variables.outputs), and the Light still switches;
* a port test of the Light's line pulses it through the v3 program, as on v1; a motor's line stays busy.
"""

from __future__ import annotations

import copy
import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agal_one_agent.blocks.cloud import CloudSink, _VariableUploader
from agal_one_agent.blocks.clock import SimClock
from agal_one_agent.blocks.host import ProgramHost
from agal_one_agent.blocks.io import SimulatedIO
from agal_one_agent.blocks.runtime import CompileError, RecordingSink

import _contracts

TZ = ZoneInfo("Asia/Kolkata")
CHIP = "pinctrl-bcm2835"
LIGHT, PUMP, VALVE, EXTRA = "lightCard00000000001", "pumpCard000000000001", "valveCard00000000001", "extraCard00000000001"


def port(line: int, active_low: bool = False) -> dict:
    t = {"kind": "gpio", "chip": CHIP, "line": line, "direction": "out"}
    if active_low:
        t["activeLow"] = True
    return t


def _default_aab(name: str) -> dict:
    with open(os.path.join(_contracts.DEFAULTS, f"{name}.json"), encoding="utf-8") as f:
        aab = json.load(f)
    return {k: aab[k] for k in ("templateId", "templateVersion", "name", "variables", "rules", "settings", "version", "notes") if k in aab}


def light_aab() -> dict:
    """The Pi's Light block as the backend compiles it today: the default lighting circuit without the wall switch
    it has no port for (validateAab drops nothing; the stored block has no wall_switch — tests/parity_drive.py)."""
    aab = _default_aab("aab_lighting_circuit")
    aab["variables"] = [v for v in aab["variables"] if v["name"] != "wall_switch"]
    aab["rules"] = [r for r in aab["rules"] if "wall_switch" not in json.dumps(r)]
    aab["name"] = "Light"
    return aab


def light_v1(version: int) -> dict:
    return {"version": version, "compiledAt": "2026-10-16T10:00:00.000Z", "timezone": "Asia/Kolkata",
            "nab": {"variables": [], "schedules": [], "rules": []},
            "assets": [{"assetId": LIGHT, "aab": light_aab(), "ports": {"device.power": port(17, True)},
                        "assetType": "lighting_circuit", "name": "Light"}]}


def light_v3(version: int) -> dict:
    """What the backend's migrator compiles for the Pi (compileLayouts): every accepted card with its ports from the
    port table — the Light, device.power on DO1, active low — no rule, no instruction, no safety."""
    return {"language": 3, "version": version, "compiledAt": "2026-10-16T10:05:00Z", "timezone": "Asia/Kolkata",
            "assets": [{"assetId": LIGHT, "assetType": "lighting_circuit", "name": "Light",
                        "ports": {"device.power": port(17, True)}}],
            "rules": [], "instruction": [], "settings": {"resumeAfterPowerSeconds": 30},
            "handover": {"releases": []}}


def plain_relay_aab(name: str, var: str) -> dict:
    """A block whose only job is the card's output (a pump with no protection rules left, a valve station)."""
    return {"name": name, "version": 1, "rules": [], "settings": {},
            "variables": [{"name": var, "kind": "node", "type": "bool", "exposed": True, "settable": True, "default": False,
                           "node": {"sourceKey": "device.power", "direction": "out"}}]}


def farm_v1(version: int, extra: bool = False) -> dict:
    b = light_v1(version)
    b["assets"] += [
        {"assetId": PUMP, "aab": plain_relay_aab("Pump", "relay"), "ports": {"device.power": port(27)},
         "assetType": "motor_controller", "name": "Pump", "safeState": "off"},
        {"assetId": VALVE, "aab": plain_relay_aab("Valve", "coil"), "ports": {"device.power": port(22)},
         "assetType": "valve_controller", "name": "Valve"},
    ]
    if extra:
        b["assets"].append({"assetId": EXTRA, "aab": plain_relay_aab("Fan", "relay"), "ports": {"device.power": port(23)},
                            "assetType": "lighting_circuit", "name": "Fan"})
    b["nab"] = {
        "variables": [
            {"name": "pump", "kind": "asset", "type": "bool", "settable": True, "asset": {"assetId": PUMP, "variable": "relay", "access": "write"}},
            {"name": "valve", "kind": "asset", "type": "bool", "settable": True, "asset": {"assetId": VALVE, "variable": "coil", "access": "write"}},
        ],
        "schedules": [{"id": "morning", "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "start": "06:00",
                       "durationMinutes": 30, "startWindowMinutes": 60,
                       "actions": [{"type": "set", "variable": "valve", "value": True}, {"type": "set", "variable": "pump", "value": True}],
                       "endActions": [{"type": "set", "variable": "pump", "value": False}, {"type": "set", "variable": "valve", "value": False}]}],
        "rules": [],
    }
    return b


def farm_v3(version: int) -> dict:
    """The migrator's v3 bundle for the farm: the NAB as one every-day instruction part (partId migrated), the valve
    continuous and normally closed on its one output, the pump with its default guard."""
    b = light_v3(version)
    b["assets"] += [
        {"assetId": PUMP, "assetType": "motor_controller", "name": "Pump", "ports": {"device.power": port(27)}, "safeState": "off",
         "guard": {"output": "device.power", "settings": {"dryRun": {"dropPct": 30, "holdSeconds": 5},
                                                         "overload": {"multiple": 1.25, "holdSeconds": 5},
                                                         "inrushIgnoreSeconds": 3, "restartLockoutSeconds": 300}}},
        {"assetId": VALVE, "assetType": "valve_controller", "name": "Valve", "ports": {"device.power": port(22)},
         "valve": {"actuation": "continuous_ac", "open": "device.power", "normallyClosed": True}},
    ]
    b["instruction"] = [{
        "key": "field:fieldFarm00000001/instruction/migrated", "containerKind": "field", "containerId": "fieldFarm00000001",
        "partId": "migrated", "version": 1, "validity": "every_day",
        "program": {
            "variables": [
                {"name": "pump", "kind": "asset", "type": "bool", "settable": True, "asset": {"assetId": PUMP, "sourceKey": "device.power", "access": "write"}},
                {"name": "valve", "kind": "asset", "type": "bool", "settable": True, "asset": {"assetId": VALVE, "sourceKey": "valve.open", "access": "write"}},
            ],
            "schedules": copy.deepcopy(farm_v1(1)["nab"]["schedules"]),
            "rules": [],
        }}]
    return b


def host_at(tmp_path, when: datetime, **kw) -> tuple[ProgramHost, SimulatedIO, RecordingSink, SimClock]:
    clock = SimClock(when)
    io, sink = SimulatedIO(), RecordingSink()
    h = ProgramHost(io, sink, clock=clock, state_dir=str(tmp_path), node_uid="ApBp4jEjkZHFggoga4uC", **kw)
    return h, io, sink, clock


def run(h: ProgramHost, clock: SimClock, seconds: int) -> None:
    for _ in range(seconds):
        h.step()
        clock.advance(1.0)


def to(clock: SimClock, when: datetime) -> None:
    """Move both clocks to `when` (a node that ran through)."""
    clock.advance((when - clock.now("Asia/Kolkata")).total_seconds())


def switch(h: ProgramHost, aid: str, sk: str, value) -> dict:
    return h.apply_command({"type": "setPower", "commandId": f"c{time.monotonic_ns()}", "assetId": aid, "sourceKey": sk, "value": value})


# --------------------------------------------------------------------------- the Pi

def test_the_pi_moves_to_v3_with_the_light_on_and_nothing_is_written(tmp_path):
    h, io, sink, clock = host_at(tmp_path, datetime(2026, 10, 16, 19, 0, tzinfo=TZ))
    h.compile(light_v1(6))
    run(h, clock, 2)
    assert switch(h, LIGHT, "device.power", 1)["accepted"]
    run(h, clock, 2)
    assert io.output(LIGHT, "device.power") is True and h.language == 1
    before = len(io.writes)
    h.compile(light_v3(7))  # the migration: the runtime thread is not running here, so it switches at once
    assert h.language == 3 and h.version == 7
    run(h, clock, 5)
    assert io.writes[before:] == [], "the switch wrote an output"
    assert h.v3.level(LIGHT, "device.power") is True
    assert sink.acks[-1] == {"version": 7, "status": "applied", "reason": None}
    assert not [e for e in sink.events if e["type"] in ("boot", "output_released")]
    # The cloud is told the Light is on (variables.outputs) — the NOW lane's state.power.
    assert (LIGHT, {"device.power": True}) in sink.outputs_log
    # The Light still switches, through the v3 program (a plain output no block writes).
    assert switch(h, LIGHT, "device.power", 0)["accepted"]
    run(h, clock, 2)
    assert io.output(LIGHT, "device.power") is False and io.writes[before:] == [(LIGHT, "device.power", False)]
    assert sink.outputs_log[-1] == (LIGHT, {"device.power": False})


def test_the_pi_rolls_back_to_v1_with_the_light_on_and_nothing_is_written(tmp_path):
    h, io, sink, clock = host_at(tmp_path, datetime(2026, 10, 16, 19, 0, tzinfo=TZ))
    h.compile(light_v1(6))
    run(h, clock, 2)
    h.compile(light_v3(7))
    run(h, clock, 2)
    assert switch(h, LIGHT, "device.power", 1)["accepted"]
    run(h, clock, 2)
    assert io.output(LIGHT, "device.power") is True
    before, reported = len(io.writes), len(sink.variables_log)
    h.compile(light_v1(8))  # the rollback: the kept v1 program compiled again
    assert h.language == 1 and h.version == 8
    run(h, clock, 5)
    assert io.writes[before:] == [], "the rollback wrote an output"
    # The v1 program reports the card in full at its first pass: state.variables comes back, relay on.
    assert (LIGHT, {"relay": True, "on": True}) in sink.variables_log[reported:]
    assert switch(h, LIGHT, "device.power", 0)["accepted"]
    run(h, clock, 2)
    assert io.writes[before:] == [(LIGHT, "device.power", False)]


def test_the_light_off_stays_off_and_the_v3_program_reports_it(tmp_path):
    h, io, sink, clock = host_at(tmp_path, datetime(2026, 10, 16, 19, 0, tzinfo=TZ))
    h.compile(light_v1(6))
    run(h, clock, 2)
    before = len(io.writes)
    h.compile(light_v3(7))
    run(h, clock, 3)
    assert io.writes[before:] == [] and sink.outputs_log == [(LIGHT, {"device.power": False})]


def test_a_port_test_of_the_lights_line_goes_through_the_v3_program_and_a_motors_line_stays_busy(tmp_path):
    h, io, sink, clock = host_at(tmp_path, datetime(2026, 10, 16, 19, 0, tzinfo=TZ))
    h.compile(farm_v1(6))
    assert h.port_for_line(CHIP, 17) == (LIGHT, "device.power")  # v1: through the program, as 0.3.4 does
    h.compile(farm_v3(7))
    assert h.language == 3
    assert h.port_for_line(CHIP, 17) == (LIGHT, "device.power")  # v3: the plain output, through the program
    assert h.port_for_line(CHIP, 27) is None and h.line_in_use(CHIP, 27)  # the pump: its guard's line, busy
    assert h.port_for_line(CHIP, 22) is None and h.line_in_use(CHIP, 22)  # the valve's coil: its driver's, busy
    assert h.port_for_line(CHIP, 26) is None and not h.line_in_use(CHIP, 26)


# --------------------------------------------------------------------------- a pump house

def test_a_running_pump_and_an_open_valve_are_carried_across_and_back(tmp_path):
    h, io, sink, clock = host_at(tmp_path, datetime(2026, 10, 16, 9, 0, tzinfo=TZ))
    h.compile(farm_v1(6))
    run(h, clock, 2)
    for aid in (VALVE, PUMP, LIGHT):
        assert switch(h, aid, "device.power", 1)["accepted"]
    run(h, clock, 2)
    assert all(io.output(a, "device.power") is True for a in (VALVE, PUMP, LIGHT))
    before = len(io.writes)
    h.compile(farm_v3(7))
    assert h.language == 3
    run(h, clock, 10)
    assert io.writes[before:] == [], "the migration switched an output"
    assert h.v3.level(VALVE, "valve.open") is True and h.v3.level(PUMP, "device.power") is True
    assert h.v3.prog.guards[PUMP].started_at is not None  # carried on: its inrush window started at the switch
    assert sink.outputs_log[0:3] and dict(sink.outputs_log)[VALVE] == {"valve.open": True}
    # Back to v1 with all three on: nothing written; the valve's valve.open is its v1 coil again.
    h.compile(farm_v1(8))
    assert h.language == 1
    run(h, clock, 5)
    assert io.writes[before:] == [], "the rollback switched an output"
    assert h.v1.aabs[VALVE].vars["coil"].value is True and h.v1.aabs[PUMP].vars["relay"].value is True


def test_the_switch_waits_for_the_end_of_a_run_and_the_schedule_does_not_run_twice(tmp_path):
    h, io, sink, clock = host_at(tmp_path, datetime(2026, 10, 16, 5, 59, 50, tzinfo=TZ))
    h.compile(farm_v1(6))
    run(h, clock, 20)  # 06:00:10 — the morning run is in progress on v1
    assert io.output(VALVE, "device.power") is True and io.output(PUMP, "device.power") is True
    h.compile(farm_v3(7))
    assert h.language == 1 and h.pending_version == 7, "switched while a run was in progress"
    to(clock, datetime(2026, 10, 16, 6, 29, 55, tzinfo=TZ))
    before = len(io.writes)
    run(h, clock, 10)  # the v1 run ends at 06:30, then the node switches
    assert h.language == 3 and h.pending_version is None
    # Only the v1 run's own end wrote: the pump before the valve; nothing at the switch.
    assert io.writes[before:] == [(PUMP, "device.power", False), (VALVE, "device.power", False)]
    assert sink.acks[-1]["version"] == 7 and sink.acks[-1]["status"] == "applied"
    # 06:40, still inside the 06:00 schedule's start window: v3 knows it ran today and does not start it again.
    to(clock, datetime(2026, 10, 16, 6, 40, tzinfo=TZ))
    before = len(io.writes)
    run(h, clock, 10)
    assert io.writes[before:] == [] and not [e for e in sink.events if e["type"] == "schedule_started" and e.get("program")]
    # The next morning v3 runs it: the valve opens before the pump.
    to(clock, datetime(2026, 10, 17, 5, 59, 58, tzinfo=TZ))
    run(h, clock, 5)
    assert io.writes[before:] == [(VALVE, "device.power", True), (PUMP, "device.power", True)]


def test_a_bundle_of_the_running_language_replaces_a_held_switch(tmp_path):
    h, io, sink, clock = host_at(tmp_path, datetime(2026, 10, 16, 5, 59, 50, tzinfo=TZ))
    h.compile(farm_v1(6))
    run(h, clock, 20)
    h.compile(farm_v3(7))
    assert h.pending_version == 7
    h.compile(farm_v1(8))  # rolled back before the switch happened
    assert h.pending_version is None and h.language == 1 and h.version == 8
    to(clock, datetime(2026, 10, 16, 6, 31, tzinfo=TZ))
    run(h, clock, 5)
    assert h.language == 1


def test_a_line_the_new_program_does_not_drive_is_switched_off_and_let_go_of(tmp_path):
    h, io, sink, clock = host_at(tmp_path, datetime(2026, 10, 16, 9, 0, tzinfo=TZ))
    h.compile(farm_v1(6, extra=True))
    run(h, clock, 2)
    assert switch(h, EXTRA, "device.power", 1)["accepted"] and switch(h, LIGHT, "device.power", 1)["accepted"]
    run(h, clock, 2)
    before = len(io.writes)
    h.compile(farm_v3(7))  # the Fan is not in the v3 bundle (the node does not accept it any more)
    assert io.writes[before:] == [(EXTRA, "device.power", False)]
    assert [r[:2] for r in io.released] == [(EXTRA, "device.power")]
    assert {"type": "output_released", "assetId": EXTRA, "sourceKey": "device.power", "level": "off"} in sink.events
    assert io.output(LIGHT, "device.power") is True


def test_a_bundle_that_does_not_compile_keeps_the_running_program_and_its_outputs(tmp_path):
    h, io, sink, clock = host_at(tmp_path, datetime(2026, 10, 16, 9, 0, tzinfo=TZ))
    h.compile(farm_v1(6))
    run(h, clock, 2)
    assert switch(h, LIGHT, "device.power", 1)["accepted"]
    run(h, clock, 2)
    bad = farm_v3(7)
    del bad["assets"][1]["guard"]  # a motor is never compiled without its guard
    before = len(io.writes)
    with pytest.raises(CompileError):
        h.compile(bad)
    run(h, clock, 3)
    assert h.language == 1 and h.version == 6 and io.writes[before:] == [] and h.pending_version is None


def test_a_runtime_thread_switches_at_the_end_of_one_of_its_passes(tmp_path):
    h, io, sink, clock = host_at(tmp_path, datetime(2026, 10, 16, 19, 0, tzinfo=TZ), tick_seconds=0.02)
    h.compile(light_v1(6))
    h.start()
    try:
        deadline = time.monotonic() + 5
        while not sink.acks and time.monotonic() < deadline:
            time.sleep(0.01)
        assert switch(h, LIGHT, "device.power", 1)["accepted"]
        while io.output(LIGHT, "device.power") is not True and time.monotonic() < deadline:
            time.sleep(0.01)
        before = len(io.writes)
        h.compile(light_v3(7))  # from the pull thread: the switch is the runtime thread's, after its pass
        while h.language != 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert h.language == 3 and h.v3._running and not h.v1._running
        while not [a for a in sink.acks if a["version"] == 7] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [a for a in sink.acks if a["version"] == 7] == [{"version": 7, "status": "applied", "reason": None}]
        assert io.writes[before:] == []
    finally:
        h.stop(safe_state=False)


# --------------------------------------------------------------------------- the report to the cloud

class _Http:
    def __init__(self):
        self.calls: list[tuple] = []

    def report_variables(self, asset_id, values, **kw):
        self.calls.append((asset_id, values, kw.get("outputs")))
        return True


class _Mqtt:
    def __init__(self):
        self.calls: list[tuple] = []

    def publish_variables(self, asset_id, values):
        self.calls.append(("variables", asset_id, values))
        return True


def test_card_outputs_ride_the_variables_message_and_a_v1_snapshot_is_posted_as_before():
    http, mqtt = _Http(), _Mqtt()
    sink = CloudSink(mqtt, http, http_async=False)
    sink.outputs(LIGHT, {"device.power": True})
    sink.variables(LIGHT, {"relay": True, "on": True})
    assert http.calls == [(LIGHT, {}, {"device.power": True}), (LIGHT, {"relay": True, "on": True}, None)]
    assert mqtt.calls == [("variables", LIGHT, {"relay": True, "on": True})]  # no MQTT copy of the outputs
    up_http = _Http()
    up = _VariableUploader(up_http, min_interval=0.0, settle=0.01)
    up.submit(LIGHT, {}, outputs={"device.power": True})
    up.submit(LIGHT, {}, outputs={"device.power": False})  # coalesced: the latest level wins
    assert up.flush(5.0)
    up.stop()
    assert up_http.calls == [(LIGHT, {}, {"device.power": False})]


def test_the_http_reporter_adds_outputs_only_when_there_are_some():
    from agal_one_agent.http_reporter import HttpReporter
    sent = []
    r = HttpReporter.__new__(HttpReporter)
    r.node_uid = "ApBp4jEjkZHFggoga4uC"
    r._post = lambda body: sent.append(body) or True  # noqa: SLF001
    r.report_variables(LIGHT, {"relay": True})
    r.report_variables(LIGHT, {}, outputs={"device.power": True})
    assert "outputs" not in sent[0]["payload"] and sent[0]["payload"]["values"] == {"relay": True}
    assert sent[1]["type"] == "variables" and sent[1]["payload"]["outputs"] == {"device.power": True}


def test_a_latching_coil_on_a_line_the_v1_program_left_on_is_written_off_before_any_pulse(tmp_path):
    """Never both coils at once (§7.9), across languages too: a line v1 drove that is now a latching valve's coil is not
    known off, so the first pass writes it off."""
    from v3_bench import V2, pumphouse
    v3 = pumphouse(7)
    coil = next(a for a in v3["assets"] if a["assetId"] == V2)["ports"]["valve.open_coil"]
    h, io, sink, clock = host_at(tmp_path, datetime(2026, 10, 16, 9, 0, tzinfo=TZ))
    v1 = light_v1(6)
    v1["assets"].append({"assetId": EXTRA, "aab": plain_relay_aab("Fan", "relay"), "assetType": "lighting_circuit", "name": "Fan",
                         "ports": {"device.power": {"kind": "gpio", "chip": coil["chip"], "line": coil["line"], "direction": "out"}}})
    h.compile(v1)
    run(h, clock, 2)
    assert switch(h, EXTRA, "device.power", 1)["accepted"]
    run(h, clock, 2)
    before = len(io.writes)
    h.compile(v3)
    assert h.language == 3
    run(h, clock, 1)
    assert (V2, "valve.open_coil", False) in io.writes[before:]
    assert not io.pulses


# --------------------------------------------------------------------------- contracts v1.15.2 (the 0.4.0 review)

ESTOPC = "estopCard00000000001"


def farm_v3_with_estop(version: int) -> dict:
    """The farm's v3 bundle with an E-stop the node names (nc aux contact on line 5, the panel reset on line 19) and
    no schedule: nothing but the latch can hold a switch."""
    b = farm_v3(version)
    b["instruction"][0]["program"]["schedules"] = []
    b["assets"].append({"assetId": ESTOPC, "assetType": "emergency_stop", "name": "E-stop", "ports": {
        "sensor.contact": {"kind": "gpio", "chip": CHIP, "line": 5, "direction": "in", "pull": "up"},
        "sensor.reset": {"kind": "gpio", "chip": CHIP, "line": 19, "direction": "in", "pull": "up"}}})
    b["safety"] = {"emergencyStop": {"input": {"assetId": ESTOPC, "sourceKey": "sensor.contact"},
                                     "reset": {"assetId": ESTOPC, "sourceKey": "sensor.reset"},
                                     "contact": "nc", "resetKind": "reset_input"}}
    return b


def test_mains_restart_check_never_starts_a_second_runtime_during_a_switch(tmp_path, monkeypatch):
    """main.py's loop runs `if runtime.loaded and not runtime._running: runtime.start()` once a second. The switch runs
    on the old runtime's thread; while it ran, the host looked stopped and that check started the OLD runtime again —
    both languages then ran passes (v1.15.2 item 1(a)). Here the check runs every millisecond against switches made
    slow on purpose (a hand-over of 50 ms: on the Pi the sampler join alone takes ~60 ms)."""
    import threading
    h, io, sink, clock = host_at(tmp_path, datetime(2026, 10, 16, 19, 0, tzinfo=TZ), tick_seconds=0.01)
    for rt in (h.v1, h.v3):
        real = rt.take_over_language

        def slow(bundle, carried, days, _real=real):
            time.sleep(0.05)
            return _real(bundle, carried, days)
        monkeypatch.setattr(rt, "take_over_language", slow)
    h.compile(light_v1(6))
    h.start()
    started_by_check: list[int] = []
    done = threading.Event()

    def mains_check():
        while not done.is_set():
            if h.loaded and not h._running:
                started_by_check.append(h.language)
                h.start()
            time.sleep(0.001)
    t = threading.Thread(target=mains_check, name="mains-check", daemon=True)
    t.start()
    try:
        for version, bundle, lang in ((7, light_v3(7), 3), (8, light_v1(8), 1), (9, light_v3(9), 3), (10, light_v1(10), 1)):
            h.compile(bundle)
            deadline = time.monotonic() + 5
            while not [a for a in sink.acks if a["version"] == version] and time.monotonic() < deadline:
                time.sleep(0.005)
            time.sleep(0.1)  # a few more of the check's turns after the switch
            assert h.language == lang and h.version == version
            other = h.v1 if lang == 3 else h.v3
            assert h.active._running and not other._running, f"two runtimes run after the switch to v{version}"
            assert other._thread is None or not other._thread.is_alive(), f"the old runtime's thread lives on after v{version}"
            assert other._sampler is None or not other._sampler.is_alive()
        assert started_by_check == [], "main's check started a runtime during a switch"
    finally:
        done.set()
        t.join(timeout=2)
        h.stop(safe_state=False)


def test_a_held_switch_is_not_stored_and_a_restart_during_the_hold_resumes_the_running_language(tmp_path):
    """The migration arrives mid-run and is held. Before v1.15.2 ProgramSync stored it as program.json at once, and a
    restart during the hold booted v3 cold: it watered the 06:00 run again from scratch (item 1(b)). Now program.json
    stays the running v1 bundle: the restart boots v1, its own restart resume carries the run to its original end,
    and the next pull hands the held bundle to it again — switched after the run, which v3 does not start again."""
    from agal_one_agent.blocks.sync import ProgramStore, ProgramSync

    class Http:
        def __init__(self, bundle):
            self.bundle = bundle

        def fetch_program(self, current_version=None):
            return {"version": self.bundle["version"], "bundle": copy.deepcopy(self.bundle)}

        def report_program_ack(self, *a, **k):
            return True

    h, io, sink, clock = host_at(tmp_path, datetime(2026, 10, 16, 5, 59, 50, tzinfo=TZ))
    store = ProgramStore(str(tmp_path))
    sync = ProgramSync(h, store, Http(farm_v1(6)))
    sync._pull()
    assert store.load()["version"] == 6
    run(h, clock, 20)  # 06:00:10 — the morning run is in progress on v1
    assert io.output(VALVE, "device.power") is True
    sync.http = Http(farm_v3(7))
    sync._pull()  # the migrator's poke: held, and NOT stored
    assert h.pending_version == 7 and h.language == 1
    assert store.load()["version"] == 6 and store.load().get("language", 1) == 1
    run(h, clock, 600)  # 06:10:10 — still held
    h.stop(safe_state=True)  # the restart (a clean stop, as SIGTERM)

    io2, sink2 = SimulatedIO(), RecordingSink()
    h2 = ProgramHost(io2, sink2, clock=clock, state_dir=str(tmp_path), node_uid="ApBp4jEjkZHFggoga4uC")
    sync2 = ProgramSync(h2, ProgramStore(str(tmp_path)), Http(farm_v3(7)))
    assert sync2.load_persisted() and h2.language == 1 and h2.version == 6
    assert [e["type"] for e in sink2.events] == ["run_resumed"]  # v1's own restart resume (R-21) carries the run
    clock.advance(5)
    sync2._pull()  # the boot pull: the v3 bundle again — held again, the resumed run is in progress
    assert h2.language == 1 and h2.pending_version == 7
    languages: set[int] = set()
    while clock.now("Asia/Kolkata") < datetime(2026, 10, 16, 6, 29, 59, tzinfo=TZ):
        h2.step()
        languages.add(h2.language)
        clock.advance(1.0)
    assert languages == {1}, "the held v3 bundle ran before the resumed run's planned end"
    assert ProgramStore(str(tmp_path)).load()["version"] == 6
    for _ in range(60):  # 06:30 — the run's own end, then the switch
        h2.step()
        clock.advance(1.0)
    assert h2.language == 3 and h2.version == 7, "the held switch was not made after the run"
    assert ProgramStore(str(tmp_path)).load()["version"] == 7, "the switched bundle was not stored at the switch"
    to(clock, datetime(2026, 10, 16, 6, 45, tzinfo=TZ))  # still inside the 06:00 start window
    run(h2, clock, 30)
    assert not [e for e in sink2.events if e["type"] == "schedule_started"], "the 06:00 run was started again from scratch"
    assert not io2.output(VALVE, "device.power") and not io2.output(PUMP, "device.power")


def test_an_estop_latched_in_the_pass_that_ends_the_hold_keeps_the_node_on_v3(tmp_path, monkeypatch):
    """compile refuses to leave language 3 while the E-stop is latched, but the switch is made at the end of a pass:
    a latch that came in the pass that ended the hold let the node switch to v1, which has no latch, and a remote
    switch-on then ran the pump with no panel reset (item 1(c)). Now the hold waits for the panel reset."""
    h, io, sink, clock = host_at(tmp_path, datetime(2026, 10, 16, 9, 0, tzinfo=TZ))
    io.set_input(ESTOPC, "sensor.contact", True)  # nc contact closed: released
    io.set_input(ESTOPC, "sensor.reset", False)
    h.compile(farm_v1(6))
    run(h, clock, 2)
    h.compile(farm_v3_with_estop(7))
    run(h, clock, 3)
    assert h.language == 3 and not h.v3.prog.estop.latched
    # The rollback's bundle arrives while the v3 runtime thread runs: checked, then held to the end of a pass
    # (single-threaded here: the runtime "runs", and the v1 runtime's start is recorded instead of a thread).
    h.v3._running, h._started = True, True
    v1_started: list[bool] = []
    monkeypatch.setattr(h.v1, "start", lambda: v1_started.append(True))
    h.compile(farm_v1(8))
    assert h.pending_version == 8 and h.language == 3
    io.set_input(ESTOPC, "sensor.contact", False)  # the E-stop is pressed — in the pass that ends the hold
    h.v3.step()
    h._after_pass()
    assert h.v3.prog.estop.latched and h.language == 3 and h.pending_version == 8 and not v1_started
    # Twisted back with no panel reset: still on v3, still latched, and a remote switch-on of the pump is refused.
    io.set_input(ESTOPC, "sensor.contact", True)
    for _ in range(3):
        clock.advance(1.0)
        h.v3.step()
        h._after_pass()
    assert h.language == 3 and h.v3.prog.estop.latched
    r = switch(h, PUMP, "device.power", 1)
    assert r["accepted"] is False and "latched" in str(r.get("reason"))
    # The panel reset clears the latch: then the switch is made, between runs.
    io.set_input(ESTOPC, "sensor.reset", True)
    for _ in range(3):
        clock.advance(1.0)
        h.v3.step()
        h._after_pass()
    assert not h.v3.prog.estop.latched
    assert h.language == 1 and h.version == 8 and v1_started == [True]
    assert io.output(PUMP, "device.power") is not True
