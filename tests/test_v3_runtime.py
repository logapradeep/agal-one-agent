"""RuntimeV3 beyond the golden vectors (contracts v1.15.0, programs/README.md §7): what the node refuses, the
swap's corners (atomic, releases, the today-only part at midnight), pause-first, commands, the host that runs
language 1 and 3 side by side, and the heartbeat. No hardware."""

from __future__ import annotations

import copy
from datetime import datetime

import pytest

from agal_one_agent.blocks.clock import SimClock
from agal_one_agent.blocks.cloud import heartbeat_extra
from agal_one_agent.blocks.host import BLOCK_LANGUAGE, ProgramHost
from agal_one_agent.blocks.io import SimulatedIO
from agal_one_agent.blocks.runtime import BlockRuntime, CompileError, RecordingSink
from agal_one_agent.blocks.simulate import build_bundle_from_defaults
from agal_one_agent.ports.links import LinkGuard, handle_switch
import _contracts
from v3_bench import PUMP, TZ, V1, V2, Bench, asset_var, card, nxt, part, pumphouse, rule, rule_block

LAMP = "asset_lamp0001"
EIGHT = datetime(2026, 10, 15, 8, 0, tzinfo=TZ)  # no run in progress


def with_lamp(bundle: dict) -> dict:
    bundle["assets"].append({"assetId": LAMP, "assetType": "lighting_circuit", "name": "Yard light",
                             "ports": {"device.power": {"kind": "gpio", "chip": "pinctrl-bcm2835", "line": 24, "direction": "out"}}})
    return bundle


def refused(bundle: dict, match: str) -> None:
    with pytest.raises(CompileError, match=match):
        Bench(bundle)


# ------------------------------------------------------------ what is refused

def test_refusals_a_node_can_see_for_itself():
    b = pumphouse()
    rule(b, 1)["program"]["variables"][1]["asset"] = {"assetId": V1, "variable": "open", "access": "read"}
    refused(b, "no AAB")
    b = pumphouse()
    rule(b, 1)["program"]["rules"][0]["actions"] = [{"type": "run_plot", "plotId": "p1"}]
    refused(b, "run_plot is v1")
    b = pumphouse()
    rule(b, 1)["program"]["rules"].append({"id": "x", "kind": "on", "on": {"event": "interruption"}, "actions": []})
    refused(b, "not a v3 event")
    b = pumphouse()
    rule(b, 1)["program"]["schedules"] = [copy.deepcopy(part(b, "pumphouse")["program"]["schedules"][0])]
    refused(b, "a rule has no schedules")
    b = pumphouse()
    part(b, "pumphouse")["program"]["schedules"][0]["plotId"] = "plot_1"
    refused(b, "plotId is v1")
    b = pumphouse()
    rule(b, 1)["program"]["variables"][1]["asset"]["access"] = "write"
    rule(b, 1)["program"]["variables"][1]["settable"] = True
    refused(b, "two writers")
    b = pumphouse()
    b["rules"].append(rule_block(3, {"variables": [asset_var("pump", PUMP, "stop", "device.power")],
                                     "rules": [{"id": "x", "kind": "when", "when": {"condition": "true"},
                                                "actions": [{"type": "set", "variable": "pump", "value": True}]}]}))
    refused(b, "may only be set off")
    b = pumphouse()
    rule(b, 1)["program"]["variables"][1]["asset"]["assetId"] = "asset_stranger"
    refused(b, "not on this node")
    b = pumphouse()
    card(b, V1)["ports"]["valve.coil"]["line"] = 17  # the pump's line
    refused(b, "a port is linked to one card")
    b = pumphouse()
    b["language"] = 1
    refused(b, "not a language-3 bundle")


def test_a_port_the_node_does_not_have_is_refused():
    class Pins(SimulatedIO):
        def has_port(self, port):
            return port.transport.get("line") != 27

    with pytest.raises(CompileError, match="this node has no port"):
        from agal_one_agent.blocks.v3.runtime import RuntimeV3
        RuntimeV3(Pins(), RecordingSink(), clock=SimClock()).compile(pumphouse())


def test_a_today_only_part_and_an_every_day_part_are_not_two_writers():
    b = pumphouse()
    today = copy.deepcopy(part(b, "pumphouse"))
    today.update(key="field:field_north01/instruction/pumphouse_today", partId="pumphouse_today",
                 validity="today_only", validOn="2026-10-15", version=2)
    b["instruction"].append(today)
    Bench(b)  # loads


def test_any_number_of_interlocks_may_stop_an_output_with_one_writer():
    b = pumphouse()
    for slot, cond in ((3, "hour() == 6 and minute() == 10"), (4, "false")):
        b["rules"].append(rule_block(slot, {"variables": [asset_var("pump", PUMP, "stop", "device.power")],
                                            "rules": [{"id": "s", "kind": "when", "when": {"condition": cond},
                                                       "actions": [{"type": "set", "variable": "pump", "value": False}]}]}))
    bench = Bench(b)
    assert all(r.interlock for r in bench.rt.prog.rules[1:])
    bench.step()
    bench.until(6, 5)
    assert bench.pump_on()
    bench.until(6, 10)
    assert bench.pump_on() is False and bench.level(V1, "valve.open") is True  # stopped; the writer did not restart it


# ------------------------------------------------------------------- the swap

def test_a_take_over_that_fails_half_way_leaves_everything_as_it_was(monkeypatch):
    b = Bench(pumphouse())
    b.step()
    b.until(6, 5)
    before = (b.rt.version, b.pump_on(), b.level(V1, "valve.open"), dict(b.rt.st.runs))
    monkeypatch.setattr(type(b.rt), "_plan_releases", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(CompileError, match="could not take over from the running program: boom"):
        b.rt.compile(nxt(b.rt.bundle))
    monkeypatch.undo()
    assert (b.rt.version, b.pump_on(), b.level(V1, "valve.open"), dict(b.rt.st.runs)) == before
    m = b.mark()
    b.run(5)
    assert b.writes_since(m) == [] and b.pump_on()


def test_an_identical_reload_writes_nothing():
    b = Bench(pumphouse())
    b.step()
    b.until(6, 5)
    m = b.mark()
    b.rt.compile(nxt(b.rt.bundle))
    b.run(10)
    assert b.writes_since(m) == [] and b.events_since(m, "instruction_switched") == []


def test_handover_now_releases_at_the_switch_pass():
    b = Bench(pumphouse())
    b.step()
    b.until(6, 5)
    m = b.mark()

    def drop_rule_1(x):
        x["rules"] = []
        x["handover"] = {"fromVersion": 1, "releases": [{"assetId": PUMP, "sourceKey": "device.power", "when": "now"}]}

    b.rt.compile(nxt(b.rt.bundle, drop_rule_1))
    b.run(1)
    assert b.writes_since(m) == [{"assetId": PUMP, "sourceKey": "device.power", "value": False}]
    assert [e["assetId"] for e in b.events_since(m, "output_released")] == [PUMP]
    assert b.level(V1, "valve.open") is True


def test_no_run_in_progress_releases_now_and_an_off_output_is_simply_let_go_of():
    bundle = pumphouse()
    rule(bundle, 1)["program"]["rules"][0]["when"]["condition"] = "true"  # the pump runs with no run
    b = Bench(bundle, start=EIGHT)
    b.step()
    b.run(5)
    assert b.pump_on()
    m = b.mark()
    b.rt.compile(nxt(b.rt.bundle, lambda x: x.update(rules=[])))
    b.run(1)
    assert b.pump_on() is False and [e["assetId"] for e in b.events_since(m, "output_released")] == [PUMP]
    # An output that is off and loses its writer: let go of, nothing written.
    b2 = Bench(pumphouse(), start=EIGHT)
    b2.step()
    m = b2.mark()
    b2.rt.compile(nxt(b2.rt.bundle, lambda x: x.update(rules=[])))
    b2.run(1)
    assert b2.writes_since(m) == [] and [e["assetId"] for e in b2.events_since(m, "output_released")] == [PUMP]


def test_a_card_that_leaves_the_bundle_is_switched_off_and_its_line_freed():
    b = Bench(with_lamp(pumphouse()), start=EIGHT)
    b.step()
    assert b.rt.apply_command({"type": "setPower", "assetId": LAMP, "sourceKey": "device.power", "value": 1})["accepted"]
    b.run(1)
    assert b.physical(LAMP, "device.power") is True
    m = b.mark()
    b.rt.compile(nxt(b.rt.bundle, lambda x: x.update(assets=[a for a in x["assets"] if a["assetId"] != LAMP])))
    b.run(1)
    assert b.writes_since(m) == [{"assetId": LAMP, "sourceKey": "device.power", "value": False}]
    assert (LAMP, "device.power") in [(a, k) for a, k, _ in b.io.released]
    assert [e["assetId"] for e in b.events_since(m, "output_released")] == [LAMP]


def today_only_night(bundle: dict, every_day_night: bool) -> dict:
    """A today-only part (2026-10-15) with a `night` run 23:50 for 20 min opening Valve 1."""
    today = copy.deepcopy(part(bundle, "pumphouse"))
    today.update(key="field:field_north01/instruction/pumphouse_today", partId="pumphouse_today",
                 validity="today_only", validOn="2026-10-15", version=2)
    s = today["program"]["schedules"][0]
    s.update(id="night", start="23:50", durationMinutes=20)
    bundle["instruction"].append(today)
    if every_day_night:
        ed = part(bundle, "pumphouse")["program"]["schedules"]
        ed.append(dict(copy.deepcopy(s), durationMinutes=40))
    return bundle


def test_a_run_in_progress_at_midnight_is_carried_into_the_every_day_part_by_its_id():
    b = Bench(today_only_night(pumphouse(), every_day_night=True), start=datetime(2026, 10, 15, 23, 49, 58, tzinfo=TZ))
    b.step()
    b.until(23, 50)
    assert b.level(V1, "valve.open")
    b.until(23, 59, 59)
    assert b.pump_on()
    m = b.mark()
    b.until(0, 0, 0, day=16)
    [sw] = b.events_since(m, "instruction_switched")
    assert sw["instruction"]["cause"] == "midnight" and sw["instruction"]["toPartId"] == "pumphouse"
    assert sw["instruction"]["kept"] == [PUMP, V1]
    assert b.writes_since(m) == []
    b.until(0, 10, 0, day=16)
    assert b.level(V1, "valve.open")  # carried: start 23:50 + the every-day part's 40 min
    b.until(0, 30, 0, day=16)
    assert b.level(V1, "valve.open") is False


def test_a_run_whose_schedule_the_every_day_part_lacks_runs_its_old_end_at_its_planned_end():
    b = Bench(today_only_night(pumphouse(), every_day_night=False), start=datetime(2026, 10, 15, 23, 49, 58, tzinfo=TZ))
    b.step()
    b.until(23, 55)
    assert b.level(V1, "valve.open")
    b.until(0, 9, 59, day=16)
    assert b.level(V1, "valve.open")
    m = b.mark()
    b.until(0, 10, 0, day=16)
    w = b.writes_since(m)
    assert [x["assetId"] for x in w] == [PUMP, V1]  # pump first, then the valve
    assert b.events_since(m, "schedule_ended")


def test_a_today_only_part_for_another_day_does_not_run_today():
    bundle = pumphouse()
    today = copy.deepcopy(part(bundle, "pumphouse"))
    today.update(key="field:field_north01/instruction/pumphouse_today", partId="pumphouse_today",
                 validity="today_only", validOn="2026-10-16", version=2)
    today["program"]["schedules"][0]["start"] = "07:00"
    bundle["instruction"].append(today)
    b = Bench(bundle)
    b.step()
    b.until(6, 1)
    assert b.level(V1, "valve.open")  # the every-day part runs today
    assert b.rt.status_extra()["instruction"][0]["partId"] == "pumphouse"


# ---------------------------------------------------------------- pause-first

def test_while_running_a_written_output_is_not_switched_an_unwritten_one_is():
    b = Bench(with_lamp(pumphouse()))
    b.step()
    r = b.rt.apply_command({"type": "setPower", "assetId": PUMP, "sourceKey": "device.power", "value": 1})
    assert r == {"accepted": False, "reason": "automation_running"}
    r = b.rt.apply_command({"type": "setPower", "assetId": LAMP, "sourceKey": "device.power", "value": 1, "minutes": 1})
    assert r["accepted"]
    b.run(1)
    assert b.physical(LAMP, "device.power") is True
    b.run(60)
    assert b.physical(LAMP, "device.power") is False  # its own timer, on the node


def test_pause_then_switch_interlocks_keep_running_resume_returns_to_the_program():
    bundle = pumphouse()
    bundle["rules"].append(rule_block(3, {"variables": [asset_var("pump", PUMP, "stop", "device.power")],
                                          "rules": [{"id": "s", "kind": "when", "when": {"condition": "hour() == 6 and minute() == 40"},
                                                     "actions": [{"type": "set", "variable": "pump", "value": False}]}]}))
    b = Bench(bundle)
    b.step()
    b.until(6, 5)
    m = b.mark()
    actor = {"uid": "u_op", "via": "app"}
    assert b.rt.apply_command({"type": "pauseAutomation", "actor": actor, "then": {"assetId": V1, "power": "off"}})["accepted"]
    b.run(1)
    [p] = b.events_since(m, "automation_paused")
    assert p["automation"]["by"] == dict(actor, at="2026-10-15T06:05:01+05:30")
    assert p["automation"]["then"] == {"assetId": V1, "power": "off"}
    assert b.level(V1, "valve.open") is False
    assert b.pump_on() is True  # outputs stay as they are; nothing follows anything now
    assert b.rt.status_extra()["automation"]["state"] == "paused"
    b.run(5)
    assert b.pump_on() is True  # rule 1 waits while paused
    b.until(6, 40)
    assert b.pump_on() is False  # the interlock (stop only) keeps running
    assert b.rt.apply_command({"type": "setPower", "assetId": PUMP, "sourceKey": "device.power", "value": 1})["accepted"]
    b.run(1)
    assert b.rt.status_extra()["automation"]["manualOn"] == [PUMP]
    b.until(6, 45)  # the morning run's end (06:30) passed during the pause: its end actions wait
    m = b.mark()
    assert b.rt.apply_command({"type": "resumeAutomation", "actor": actor})["accepted"]
    b.run(1)
    [r] = b.events_since(m, "automation_resumed")
    assert r["automation"]["switchedOff"] == [PUMP] and r["automation"]["pausedSeconds"] >= 39 * 60
    assert b.pump_on() is False and b.level(V1, "valve.open") is False
    assert b.events_since(m, "schedule_ended")


def test_stop_all_and_a_timed_pause():
    b = Bench(pumphouse())
    b.step()
    b.until(6, 5)
    until = datetime(2026, 10, 15, 6, 7, tzinfo=TZ).isoformat()
    assert b.rt.apply_command({"type": "pauseAutomation", "stopAll": True, "until": until})["accepted"]
    b.run(1)
    assert b.pump_on() is False and b.level(V1, "valve.open") is False
    m = b.mark()
    b.until(6, 7, 1)
    [r] = b.events_since(m, "automation_resumed")
    assert r["automation"]["cause"] == "timer"
    b.run(5)
    assert b.level(V1, "valve.open") is True and b.pump_on()  # the run is still inside its time: re-applied


def test_commands_of_the_v1_program_are_refused():
    b = Bench(pumphouse())
    assert b.rt.apply_command({"type": "runPlot", "plotId": "p"})["reason"].startswith("refused: plot runs ended")
    assert b.rt.apply_command({"type": "setVariable", "assetId": PUMP, "variable": "x", "value": 1})["accepted"] is False


# --------------------------------------------------------------- the host

def _v1_farm():
    return build_bundle_from_defaults(_contracts.DEFAULTS, "farm")


def _drive(rt, clock, seconds):
    for _ in range(seconds):
        rt.step()
        clock.advance(1.0)


def test_the_host_runs_a_language_1_bundle_exactly_as_the_v1_runtime(tmp_path):
    """Coexistence (README §7.2): until its node is migrated, an agent on v3 runs the v1 NAB bundle as 0.3.3 does."""
    def run(make):
        clock = SimClock(datetime(2026, 9, 7, 5, 0, tzinfo=TZ))
        io = SimulatedIO()
        rt = make(io, clock)
        rt.compile(_v1_farm())
        _drive(rt, clock, 30)
        rt.apply_command({"type": "runPlot", "plotId": "$plot1", "durationMinutes": 1})
        _drive(rt, clock, 120)
        return io.writes
    host_writes = run(lambda io, c: ProgramHost(io, RecordingSink(), clock=c, state_dir=str(tmp_path / "h")))
    v1_writes = run(lambda io, c: BlockRuntime(io, RecordingSink(), clock=c, state_dir=str(tmp_path / "v")))
    assert host_writes == v1_writes and host_writes


def test_the_host_runs_v3_and_refuses_language_2(tmp_path):
    h = ProgramHost(SimulatedIO(), RecordingSink(), clock=SimClock(datetime(2026, 10, 15, 5, 59, 58, tzinfo=TZ)),
                    state_dir=str(tmp_path), node_uid="node_pumphouse")
    h.compile(pumphouse())
    assert h.language == 3 and h.loaded and h.version == 1
    with pytest.raises(CompileError, match="language 2"):
        h.compile(dict(pumphouse(2), language=2))
    assert h.language == 3 and h.version == 1


def test_the_migration_switches_language_a_bad_v3_bundle_keeps_v1_running(tmp_path):
    clock = SimClock(datetime(2026, 10, 15, 5, 0, tzinfo=TZ))
    io = SimulatedIO()
    h = ProgramHost(io, RecordingSink(), clock=clock, state_dir=str(tmp_path), node_uid="node_pumphouse")
    h.compile(_v1_farm())
    assert h.language == 1
    bad = pumphouse(5)
    del card(bad, PUMP)["guard"]
    with pytest.raises(CompileError):
        h.compile(bad)
    assert h.language == 1 and h.version == 1
    h.compile(pumphouse(5))
    assert h.language == 3 and h.version == 5 and not h.v1._running


def test_the_heartbeat_reports_block_language_3_and_the_built_ins():
    class Cfg:
        pins = []
    clock = SimClock(datetime(2026, 10, 15, 5, 59, 58, tzinfo=TZ))
    h = ProgramHost(SimulatedIO(), RecordingSink(), clock=clock, node_uid="node_pumphouse")
    assert heartbeat_extra(h, Cfg())["capabilities"]["blockLanguage"] == BLOCK_LANGUAGE == 3
    b = Bench(pumphouse())
    b.step()
    extra = heartbeat_extra(b.rt, Cfg())
    assert extra["capabilities"]["blockLanguage"] == 3 and extra["programVersion"] == 1
    assert extra["safety"]["estop"] == {"latched": False} and extra["safety"]["power"]["mains"] == "present"
    assert {v["assetId"]: v["state"] for v in extra["valves"]} == {V1: "closed", V2: "closed"}
    assert extra["guards"] == [{"assetId": PUMP, "tripped": False}]
    assert extra["held"] == [] and extra["automation"] == {"state": "running"}
    assert extra["instruction"] == [{"containerKind": "field", "containerId": "field_north01", "partId": "pumphouse",
                                     "validity": "every_day", "version": 1}]


def test_a_port_test_on_a_line_the_v3_program_uses_is_busy():
    h = ProgramHost(SimulatedIO(), RecordingSink(), clock=SimClock(datetime(2026, 10, 15, 8, 0, tzinfo=TZ)),
                    node_uid="node_pumphouse")
    h.compile(pumphouse())
    assert h.line_in_use("pinctrl-bcm2835", 17) and h.line_in_use("pinctrl-bcm2835", 22)  # the pump; a latching coil
    assert h.line_in_use("pinctrl-bcm2835", 5)                                           # the E-stop's contact
    assert not h.line_in_use("pinctrl-bcm2835", 26)                                      # DI5, linked to nothing
    assert h.port_for_line("pinctrl-bcm2835", 17) is None  # never handed to a port test through the runtime


class _Mqtt:
    def __init__(self):
        self.acks = []

    def publish_command_ack(self, command_id, status, error=None, applied_value=None):
        self.acks.append((command_id, status, error))


def test_a_switch_reaches_the_v3_runtime_through_the_node_check():
    """RBAC-01 on a v3 node: the port links are every card's ports plus each valve's logical valve.open."""
    b = Bench(with_lamp(pumphouse()))
    b.step()
    guard = LinkGuard(b.rt.port_links)
    guard.note_ports_version(3)
    mqtt, legacy = _Mqtt(), []
    handle_switch({"type": "setPower", "commandId": "c1", "assetId": LAMP, "sourceKey": "device.power", "value": 1},
                  guard, b.rt, mqtt, legacy=legacy.append)
    handle_switch({"type": "setPower", "commandId": "c2", "assetId": V1, "sourceKey": "valve.open", "value": 1},
                  guard, b.rt, mqtt, legacy=legacy.append)
    handle_switch({"type": "setPower", "commandId": "c3", "assetId": "asset_stranger", "sourceKey": "device.power", "value": 1},
                  guard, b.rt, mqtt, legacy=legacy.append)
    acks = {c: (s, e) for c, s, e in mqtt.acks}
    assert acks["c1"] == ("completed", None)
    assert acks["c2"] == ("failed", "automation_running")
    assert acks["c3"][0] == "failed" and "not linked" in acks["c3"][1]
    assert legacy == []
    b.run(1)
    assert b.physical(LAMP, "device.power") is True
