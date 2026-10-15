"""RuntimeV3's corners (contracts v1.15.0, programs/README.md §7) — the paths a review of agent 0.4.0 found where a
built-in could be lost or a line left driven: a latched E-stop and a program without it, debounced safety inputs at
boot, a state directory that cannot be written, both coils of a latching valve, an analog sense, guard settings out
of bounds, a run interrupted and then removed, a line an old output drove taken by an input or a latching coil, a
plain output that becomes a motor, a clock that cannot be trusted, and a today-only part that keeps a schedule id.
No hardware: the pump house of the golden vectors on a SimClock and a SimulatedIO."""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime

import pytest

import agal_one_agent.blocks.clock as clock_mod
import agal_one_agent.blocks.v3.runtime as R
from agal_one_agent.blocks.clock import SimClock, SystemClock
from agal_one_agent.blocks.host import ProgramHost
from agal_one_agent.blocks.io import SimulatedIO
from agal_one_agent.blocks.runtime import CompileError, RecordingSink
from agal_one_agent.blocks.v3.guard import MotorGuard
from agal_one_agent.blocks.v3.runtime import STATE_FILE
from test_v3_valves import both_valves, pulses
from v3_bench import ESTOP, MAINS, PUMP, TZ, UPS, V1, V2, Bench, card, nxt, part, pumphouse

LAMP, DOOR, M2 = "asset_lamp0001", "asset_door0001", "asset_pump0002"
EIGHT = datetime(2026, 10, 15, 8, 0, tzinfo=TZ)
ACTOR = {"uid": "u_owner01", "via": "app"}


def line(n: int, direction: str) -> dict:
    return {"kind": "gpio", "chip": "pinctrl-bcm2835", "line": n, "direction": direction}


def with_lamp(bundle: dict, n: int = 24) -> dict:
    bundle["assets"].append({"assetId": LAMP, "assetType": "lighting_circuit", "name": "Yard light",
                             "ports": {"device.power": line(n, "out")}})
    return bundle


def without_estop(b: dict) -> None:
    b["safety"].pop("emergencyStop")
    b["assets"] = [a for a in b["assets"] if a["assetId"] != ESTOP]


def press_and_release(b: Bench) -> None:
    b.set(ESTOP, "sensor.contact", False)
    b.run(2)
    b.set(ESTOP, "sensor.contact", True)  # released, but not reset at the panel
    b.run(3)
    assert b.rt.prog.estop.latched


def state(b: Bench) -> dict:
    with open(os.path.join(b.state_dir, STATE_FILE)) as f:
        return json.load(f)


class _NoWrite:
    """The runtime module's `os`, with a state directory that went read-only (an SD card does)."""

    def __getattr__(self, name):
        return getattr(os, name)

    @staticmethod
    def replace(src, dst):
        raise OSError(30, "Read-only file system")


def never_both(io: SimulatedIO, aid: str, coils=("valve.open_coil", "valve.close_coil")) -> None:
    """Never both coils of a latching valve at once: while one is pulsed the other is not on."""
    for (a, k), on in io.on_during_pulse:
        if a == aid:
            other = [c for c in coils if c != k]
            assert not any((aid, c) in on for c in other), f"{k} pulsed while {other} was on"


# ============================================================ the E-stop latch

def test_a_program_without_the_estop_is_refused_while_it_is_latched_and_the_motors_stay_off():
    b = Bench(pumphouse())
    b.step()
    b.until(6, 2)
    assert b.pump_on()
    press_and_release(b)
    with pytest.raises(CompileError, match="the E-stop is latched at the panel"):
        b.rt.compile(nxt(b.rt.bundle, without_estop))
    b.run(40)
    assert b.rt.version == 1 and b.rt.prog.estop.latched
    assert b.pump_on() is False and b.physical(PUMP, "device.power") is False
    assert b.events("watering_resumed") == []
    # Cleared at the panel, the same program is taken.
    b.set(ESTOP, "sensor.reset", True)
    b.run(1)
    assert not b.rt.prog.estop.latched
    b.rt.compile(nxt(b.rt.bundle, without_estop))
    assert b.rt.version == 2 and b.rt.prog.estop is None


def test_a_restart_with_the_latch_persisted_refuses_a_program_without_the_estop(tmp_path):
    b = Bench(pumphouse(), state_dir=str(tmp_path))
    b.step()
    b.until(6, 2)
    press_and_release(b)
    b.rt.stop(safe_state=True)
    rt = b.new_runtime()
    with pytest.raises(CompileError, match="the E-stop is latched at the panel"):
        rt.compile(nxt(b.bundle, without_estop))
    assert not rt.loaded and state(b)["estop"]["latched"] is True  # the latch is still on disk
    rt.compile(b.bundle)  # with the E-stop: the latch is restored, and holds
    b.step()
    b.run(40)
    assert rt.prog.estop.latched and b.pump_on() is False


def test_the_host_does_not_leave_block_language_3_while_the_estop_is_latched(tmp_path):
    from test_v3_runtime import _v1_farm
    clock = SimClock(datetime(2026, 10, 15, 5, 59, 58, tzinfo=TZ))
    io = SimulatedIO()
    for (aid, sk), v in {(ESTOP, "sensor.contact"): True, (MAINS, "sensor.power"): True, (UPS, "sensor.power"): True}.items():
        io.set_input(aid, sk, v)
    with open(os.path.join(str(tmp_path), STATE_FILE), "w") as f:
        json.dump({"cleanStop": True, "valves": {V2: "closed"}}, f)
    h = ProgramHost(io, RecordingSink(), clock=clock, state_dir=str(tmp_path), node_uid="node_pumphouse")
    h.compile(pumphouse())
    h.step()
    io.set_input(ESTOP, "sensor.contact", False)
    clock.advance(1)
    h.step()
    assert h.v3.prog.estop.latched
    with pytest.raises(CompileError, match="does not leave block language 3"):
        h.compile(_v1_farm())
    assert h.language == 3 and h.v3.prog.estop.latched


def test_an_unreadable_state_reads_as_latched():
    b = Bench(pumphouse(), load=False)
    with open(os.path.join(b.state_dir, STATE_FILE), "w") as f:
        f.write("{not json")
    b.rt.compile(b.bundle)
    b.step()
    b.until(6, 5)
    assert b.rt.prog.estop.latched and b.pump_on() is False
    assert b.rt.prog.valves[V2].fault == "position_unknown"


# ============================================================== debounced inputs

def test_debounced_safety_inputs_boot_with_no_latch_and_no_fault():
    bundle = both_valves(pumphouse())
    for a in bundle["assets"]:
        if a["assetId"] in (ESTOP, MAINS, UPS):
            for t in a["ports"].values():
                t["debounceMs"] = 50
    b = Bench(bundle)
    b.step()  # the boot pass
    assert not b.rt.prog.estop.latched and b.rt.prog.valves[V2].fault is None
    assert [e["type"] for e in b.sink.events] == ["program_applied"]
    b.until(6, 5)
    assert b.pump_on() and b.level(V2, "valve.open") is True
    b.set(ESTOP, "sensor.contact", False)
    b.run(2)
    assert b.rt.prog.estop.latched  # a press after the boot is still seen


# ==================================================== a state that cannot be written

def test_no_latching_pulse_is_sent_when_the_pulse_cannot_be_recorded(monkeypatch):
    b = Bench(both_valves(pumphouse()))
    b.step()
    monkeypatch.setattr(R, "os", _NoWrite())
    m = b.mark()
    b.until(6, 1)
    assert pulses(b.writes_since(m)) == []
    d = b.rt.prog.valves[V2]
    assert d.fault == "position_unknown" and "recorded" in d.fault_detail
    [f] = b.events_since(m, "valve_fault")
    assert f["valve"]["reason"] == "position_unknown"


def test_a_restart_that_cannot_record_its_start_vouches_for_no_position(monkeypatch):
    b = Bench(both_valves(pumphouse()), load=False)  # the vectors' clean stop: Valve 2 closed
    monkeypatch.setattr(R, "os", _NoWrite())
    rt = b.rt
    rt.compile(b.bundle)
    b.step()
    assert rt.prog.valves[V2].fault == "position_unknown"
    assert state(b)["cleanStop"] is True  # what the file still says: it is not believed
    m = b.mark()
    b.until(6, 5)
    assert pulses(b.writes_since(m)) == [] and b.level(V2, "valve.open") is False


# ======================================================= both coils of a latching valve

class _StuckOff(SimulatedIO):
    """A coil whose off-write fails `n` times: the pulse leaves it on."""

    def __init__(self, coil, n):
        super().__init__()
        self.coil, self.n = coil, n

    def _fails(self, port) -> bool:
        if (port.asset_id, port.source_key) == self.coil and self.n > 0:
            self.n -= 1
            return True
        return False

    def pulse(self, port, ms):
        if (port.asset_id, port.source_key) == self.coil and self.n > 0:
            self.n -= 1
            self.outputs[self.coil] = True
            self.log.append({"assetId": port.asset_id, "sourceKey": port.source_key, "pulseMs": int(ms)})
            raise OSError("the off-write failed")
        super().pulse(port, ms)

    def write_output(self, port, value):
        if not value and self._fails(port):
            raise OSError("the off-write failed")
        super().write_output(port, value)


def test_a_pulse_cut_short_leaves_no_coil_on_under_the_other_coils_pulse():
    b = Bench(both_valves(pumphouse()), load=False)
    b.io = io = _StuckOff((V2, "valve.open_coil"), 3)  # the pulse, the retry, and the next pass's switch-off fail
    for (aid, sk), v in {(ESTOP, "sensor.contact"): True, (MAINS, "sensor.power"): True, (UPS, "sensor.power"): True}.items():
        io.set_input(aid, sk, v)
    b.rt = b.new_runtime()
    b.rt.compile(b.bundle)
    b.step()
    b.until(6, 0, 1)
    d = b.rt.prog.valves[V2]
    assert d.fault == "pulse_cut_short" and io.output(V2, "valve.open_coil") is True
    assert b.rt.apply_command({"type": "resetValve", "assetId": V2, "actor": ACTOR})["accepted"]
    b.run(1)
    # The open coil was written off before the reset's close pulse: one pass's switch-offs failed, the reset's own
    # write of it succeeded — and only then the close pulse.
    assert io.output(V2, "valve.open_coil") is False
    assert ("valve.close_coil", 100) in pulses(io.log)
    never_both(io, V2)


def test_a_reset_sends_no_close_pulse_while_the_open_coil_cannot_be_written_off():
    b = Bench(both_valves(pumphouse()), load=False)
    b.io = io = _StuckOff((V2, "valve.open_coil"), 50)
    for (aid, sk), v in {(ESTOP, "sensor.contact"): True, (MAINS, "sensor.power"): True, (UPS, "sensor.power"): True}.items():
        io.set_input(aid, sk, v)
    b.rt = b.new_runtime()
    b.rt.compile(b.bundle)
    b.step()
    b.until(6, 0, 1)
    assert b.rt.prog.valves[V2].fault == "pulse_cut_short"
    m = b.mark()
    assert b.rt.apply_command({"type": "resetValve", "assetId": V2, "actor": ACTOR})["accepted"]
    b.run(2)
    assert pulses(b.writes_since(m)) == [] and b.rt.prog.valves[V2].fault
    assert b.events_since(m, "valve_reset") == []


def test_a_normal_day_never_has_both_coils_on():
    b = Bench(both_valves(pumphouse()))
    b.step()
    b.until(6, 31)
    assert pulses(b.io.log) == [("valve.open_coil", 100), ("valve.close_coil", 100)]
    never_both(b.io, V2)


def test_a_continuous_valve_made_latching_on_its_line_while_open_has_its_coil_written_off_first():
    b = Bench(pumphouse())
    b.step()
    b.until(6, 5)
    assert b.physical(V1, "valve.coil") is True

    def relatch(x):
        c = card(x, V1)
        c["ports"]["valve.close_coil"] = line(24, "out")
        c["valve"] = {"actuation": "latching_dc", "open": "valve.coil", "close": "valve.close_coil", "pulseMs": 100,
                      "supply": {"assetId": UPS, "sourceKey": "sensor.power"}, "coilVoltage": 12}
    m = b.mark()
    b.rt.compile(nxt(b.rt.bundle, relatch))
    b.run(1)
    w = b.writes_since(m)
    assert {"assetId": V1, "sourceKey": "valve.coil", "value": False} in w
    assert w.index({"assetId": PUMP, "sourceKey": "device.power", "value": False}) < w.index(
        {"assetId": V1, "sourceKey": "valve.coil", "value": False})  # motors first
    assert b.physical(V1, "valve.coil") is False and b.rt.prog.valves[V1].fault == "position_unknown"
    assert b.rt.apply_command({"type": "resetValve", "assetId": V1, "actor": ACTOR})["accepted"]
    b.run(2)
    never_both(b.io, V1, ("valve.coil", "valve.close_coil"))


# ================================================================ an analog sense

@pytest.mark.parametrize("aid", [MAINS, UPS])
def test_an_analog_input_is_refused_as_a_power_or_supply_sense(aid):
    bundle = pumphouse()
    card(bundle, aid)["ports"]["sensor.power"] = {"kind": "i2c", "busId": 1, "addr": 72, "channel": 2, "driver": "ads1115",
                                                  "measure": {"mode": "ac_rms", "windowMs": 100}, "unit": "V"}
    with pytest.raises(CompileError, match="analog input"):
        Bench(bundle)


# ============================================================ guard settings bounds

def test_guard_settings_are_held_to_their_bounds():
    g = MotorGuard(PUMP, {"output": "device.power", "current": ["sensor.current"], "ratedCurrentA": float("inf"),
                          "settings": {"dryRun": {"dropPct": 100, "holdSeconds": float("nan")},
                                       "overload": {"multiple": 1000, "holdSeconds": 0},
                                       "inrushIgnoreSeconds": 1e6, "restartLockoutSeconds": 5,
                                       "voltage": {"minV": float("nan"), "maxV": 1e9}}})
    assert (g.drop_pct, g.dry_hold, g.multiple, g.over_hold, g.inrush, g.lockout_seconds) == (80.0, 5.0, 3.0, 1.0, 30.0, 30)
    assert g.rated is None and g.min_v is None and g.max_v == 480.0


def test_a_guard_set_to_never_trip_still_trips_on_overload():
    bundle = pumphouse()
    card(bundle, PUMP)["guard"]["settings"].update(
        {"dryRun": {"dropPct": 100, "holdSeconds": 5}, "overload": {"multiple": 1000, "holdSeconds": 5},
         "inrushIgnoreSeconds": 1e6})
    b = Bench(bundle)
    b.step()
    b.until(6, 0, 3)
    assert b.pump_on()
    b.set(PUMP, "sensor.current", 30.0)  # > 3 × 7.5 A, the highest multiple a guard may have
    b.run(40)  # an inrush of at most 30 s, then held 5 s
    assert b.pump_on() is False and b.events("guard_tripped")[0]["guard"]["reason"] == "overload"


# =========================================== a run interrupted, then removed

def test_a_run_removed_during_a_power_cut_runs_its_end_actions_when_the_power_is_back():
    b = Bench(both_valves(pumphouse()))
    b.step()
    b.until(6, 5)
    assert b.pump_on() and b.level(V2, "valve.open") is True
    b.set(MAINS, "sensor.power", False)
    b.run(2)

    def rename(x):
        s = part(x, "pumphouse")["program"]["schedules"][0]
        s["id"], s["start"] = "evening", "18:00"
        part(x, "pumphouse")["version"] = 2
    m = b.mark()
    b.rt.compile(nxt(b.rt.bundle, rename))
    b.run(2)
    assert b.level(V2, "valve.open") is True  # nothing runs during the cut
    [c] = b.events_since(m, "run_cut_short")
    assert c["scheduleId"] == "morning"
    b.set(MAINS, "sensor.power", True)
    b.run(31)
    assert ("valve.close_coil", 100) in pulses(b.writes_since(m))
    assert b.level(V2, "valve.open") is False
    b.until(6, 45)
    assert b.pump_on() is False and b.physical(PUMP, "device.power") is False


# ================================================ a line taken by an input or a coil

def test_a_removed_output_whose_line_becomes_an_input_is_switched_off_before_it_is_read():
    b = Bench(with_lamp(pumphouse()), start=EIGHT)
    b.step()
    assert b.rt.apply_command({"type": "setPower", "assetId": LAMP, "sourceKey": "device.power", "value": 1})["accepted"]
    b.run(1)
    assert b.physical(LAMP, "device.power") is True

    def door(x):
        x["assets"] = [a for a in x["assets"] if a["assetId"] != LAMP]
        x["assets"].append({"assetId": DOOR, "assetType": "sensor", "name": "Door", "ports": {"sensor.contact": line(24, "in")}})
    m = b.mark()
    b.rt.compile(nxt(b.rt.bundle, door))
    # Written off and let go of as the take-over is committed: before any pass (or the sampler) reads line 24.
    assert b.writes_since(m) == [{"assetId": LAMP, "sourceKey": "device.power", "value": False}]
    assert (LAMP, "device.power") in [(a, k) for a, k, _ in b.io.released]
    b.run(1)
    assert [(e["assetId"], e["level"]) for e in b.events_since(m, "output_released")] == [(LAMP, "off")]


def test_a_moved_output_whose_old_line_becomes_an_input_is_switched_off_there():
    b = Bench(with_lamp(pumphouse()), start=EIGHT)
    b.step()
    assert b.rt.apply_command({"type": "setPower", "assetId": LAMP, "sourceKey": "device.power", "value": 1})["accepted"]
    b.run(1)

    def move(x):
        card(x, LAMP)["ports"]["device.power"] = line(25, "out")
        x["assets"].append({"assetId": DOOR, "assetType": "sensor", "name": "Door", "ports": {"sensor.contact": line(24, "in")}})
    m = b.mark()
    b.rt.compile(nxt(b.rt.bundle, move))
    b.run(1)
    w = b.writes_since(m)
    assert w[0] == {"assetId": LAMP, "sourceKey": "device.power", "value": False}  # line 24, at the commit
    assert w[1:] == [{"assetId": LAMP, "sourceKey": "device.power", "value": True}]  # line 25, at the switch pass
    assert b.io.released[-1][2]["line"] == 24


def test_an_output_whose_line_is_reused_by_another_output_is_set_by_that_outputs_first_write():
    b = Bench(with_lamp(pumphouse()), start=EIGHT)
    b.step()
    assert b.rt.apply_command({"type": "setPower", "assetId": LAMP, "sourceKey": "device.power", "value": 1})["accepted"]
    b.run(1)

    def relay(x):
        x["assets"] = [a for a in x["assets"] if a["assetId"] != LAMP]
        x["assets"].append({"assetId": "asset_relay001", "assetType": "relay", "name": "Relay",
                            "ports": {"device.power": line(24, "out")}})
    m = b.mark()
    b.rt.compile(nxt(b.rt.bundle, relay))
    b.run(1)
    assert b.writes_since(m) == [{"assetId": "asset_relay001", "sourceKey": "device.power", "value": False}]


# ================================================== a plain output becomes a motor

def test_a_plain_output_that_becomes_a_guarded_motor_while_on_is_judged_from_the_switch():
    bundle = pumphouse()
    bundle["assets"].append({"assetId": M2, "assetType": "relay", "name": "Pump 2", "ports": {
        "device.power": line(24, "out"),
        "sensor.current": {"kind": "i2c", "busId": 1, "addr": 72, "channel": 1, "driver": "ads1115",
                           "measure": {"mode": "ac_rms", "windowMs": 100}, "transform": {"scale": 30, "offset": 0}, "unit": "A"}}})
    b = Bench(bundle, start=EIGHT)
    b.io.set_input(M2, "sensor.current", 5.0)
    b.step()
    assert b.rt.apply_command({"type": "setPower", "assetId": M2, "sourceKey": "device.power", "value": 1})["accepted"]
    b.run(2)

    def motorise(x):
        c = card(x, M2)
        c["assetType"] = "motor_controller"
        c["guard"] = dict(copy.deepcopy(card(x, PUMP)["guard"]), current=["sensor.current"], ratedCurrentA=5.0)
    b.rt.compile(nxt(b.rt.bundle, motorise))
    b.run(1)
    assert b.rt.prog.guards[M2].running
    b.io.set_input(M2, "sensor.current", 50.0)
    b.run(10)
    assert b.rt.prog.guards[M2].tripped and b.physical(M2, "device.power") is False


# ================================================ a clock that cannot be trusted

def untrusted_restart(b: Bench, wall: datetime):
    b.clock.set_wall(wall)
    b.clock.ok = False
    rt = b.new_runtime()
    rt.compile(b.bundle)
    return rt


def test_a_restored_lockout_is_never_longer_than_the_lockout(tmp_path):
    for wall, ok in ((datetime(1970, 1, 1, 5, 30, tzinfo=TZ), False), (datetime(2026, 10, 15, 5, 0, tzinfo=TZ), True)):
        b = Bench(pumphouse(), state_dir=str(tmp_path / f"{ok}"))
        b.step()
        b.until(6, 0, 3)
        b.set(PUMP, "sensor.current", 10.0)  # overload against 7.5 A
        b.run(10)
        assert b.rt.prog.guards[PUMP].tripped
        b.rt.stop(safe_state=True)
        b.clock.set_wall(wall)  # 1970 untrusted; or a saved clock an hour behind, trusted
        b.clock.ok = ok
        rt = b.new_runtime()
        rt.compile(b.bundle)
        g = rt.prog.guards[PUMP]
        assert g.tripped and 0 < g.lockout_until - b.clock.monotonic() <= g.lockout_seconds


def test_a_run_waits_for_a_trusted_clock_before_it_resumes_after_a_restart():
    b = Bench(pumphouse())
    b.step()
    b.until(6, 10)
    b.rt.stop(safe_state=True)
    rt = untrusted_restart(b, datetime(1970, 1, 1, 0, 0, 5, tzinfo=TZ))
    b.run(60)
    assert b.events("watering_resumed") == [] and rt.level(V1, "valve.open") is False and b.pump_on() is False
    assert rt.st.runs["field:field_north01/morning"].state == "interrupted"
    # The node is not held while the run waits for the clock: a person may still run the pump, pause-first.
    assert rt.st.power_wait is None
    assert rt.apply_command({"type": "pauseAutomation", "actor": ACTOR, "then": {"assetId": PUMP, "power": "on"}})["accepted"]
    b.run(1)
    assert b.physical(PUMP, "device.power") is True
    assert rt.apply_command({"type": "resumeAutomation", "actor": ACTOR})["accepted"]
    b.run(1)
    # NTP: the real time, two minutes after the stop.
    b.clock.set_wall(datetime(2026, 10, 15, 6, 12, tzinfo=TZ))
    b.clock.ok = True
    b.run(5)
    [w] = b.events("watering_resumed")
    assert w["watering"]["cause"] == "restart" and rt.level(V1, "valve.open") is True


def test_a_run_whose_window_has_certainly_passed_is_cut_short_on_an_untrusted_clock():
    b = Bench(pumphouse())
    b.step()
    b.until(6, 10)
    b.rt.stop(safe_state=True)
    rt = untrusted_restart(b, datetime(1970, 1, 1, 0, 0, 5, tzinfo=TZ))
    # 20 min left of the run + 60 min of window: by 81 min after the boot the window has passed, whatever the time.
    b.run(81 * 60)
    assert rt.st.runs == {} and rt.st.power_wait is None
    assert [e["scheduleId"] for e in b.events("run_cut_short")] == ["morning"] and b.events("watering_resumed") == []


def test_no_midnight_switch_on_an_untrusted_clock_and_none_when_it_comes_good_on_the_same_day(tmp_path):
    bundle = pumphouse()
    today_part(bundle, "10:00")
    b = Bench(bundle, state_dir=str(tmp_path), start=EIGHT)
    b.step()
    b.rt.stop(safe_state=True)
    rt = untrusted_restart(b, datetime(1970, 1, 1, 0, 0, 5, tzinfo=TZ))
    b.run(5)
    # The last date a trusted clock gave: today's part keeps running, not the every-day part of a date in 1970.
    assert rt.st.date == "2026-10-15" and rt.status_extra()["instruction"][0]["partId"] == "pumphouse_today"
    b.clock.set_wall(datetime(2026, 10, 15, 8, 2, tzinfo=TZ))
    b.clock.ok = True
    b.run(5)
    assert b.events("instruction_switched") == []


def test_a_timed_pause_is_bounded_and_waits_for_a_trusted_clock_after_a_restart(tmp_path):
    b = Bench(pumphouse(), state_dir=str(tmp_path), start=EIGHT)
    b.step()
    years = datetime(2099, 1, 1, tzinfo=TZ).isoformat()
    assert b.rt.apply_command({"type": "pauseAutomation", "until": years, "actor": ACTOR})["accepted"]
    b.run(1)
    assert b.rt.st.pause_until - b.clock.monotonic() <= R.PAUSE_MAX_SECONDS
    b.rt.stop(safe_state=True)
    rt = b.new_runtime()  # a pause of 10 minutes, restored on a 1970 clock
    with open(os.path.join(str(tmp_path), STATE_FILE)) as f:
        doc = json.load(f)
    doc["automation"]["resumeAt"] = datetime(2026, 10, 15, 8, 10, tzinfo=TZ).isoformat()
    with open(os.path.join(str(tmp_path), STATE_FILE), "w") as f:
        json.dump(doc, f)
    b.clock.set_wall(datetime(1970, 1, 1, tzinfo=TZ))
    b.clock.ok = False
    rt.compile(b.bundle)
    b.step()
    assert rt.st.paused and rt.st.pause_until is None
    b.clock.set_wall(datetime(2026, 10, 15, 8, 5, tzinfo=TZ))
    b.clock.ok = True
    b.run(1)
    assert 0 < rt.st.pause_until - b.clock.monotonic() <= 5 * 60


def test_the_system_clock_probes_on_its_first_call(monkeypatch):
    monkeypatch.setattr(clock_mod, "_probe_clock", lambda: False)
    monkeypatch.setattr(clock_mod.time, "monotonic", lambda: 5.0)  # five seconds after the boot
    assert SystemClock().clock_ok() is False


# ======================================= a today-only part that keeps a schedule id

def today_part(bundle: dict, start: str) -> None:
    t = copy.deepcopy(part(bundle, "pumphouse"))
    t.update(key="field:field_north01/instruction/pumphouse_today", partId="pumphouse_today", validity="today_only",
             validOn="2026-10-15", version=2)
    t["program"]["schedules"][0]["start"] = start
    bundle["instruction"].append(t)


def test_a_today_only_part_that_keeps_the_id_at_a_later_start_runs_it_again():
    b = Bench(pumphouse())
    b.step()
    b.until(6, 40)  # the morning run is over
    b.rt.compile(nxt(b.rt.bundle, lambda x: today_part(x, "10:00")))
    b.until(10, 5)
    assert b.level(V1, "valve.open") is True and len(b.events("schedule_started")) == 2


def test_a_run_carried_into_a_part_that_moves_its_start_does_not_run_twice():
    b = Bench(pumphouse())
    b.step()
    b.until(6, 5)
    b.rt.compile(nxt(b.rt.bundle, lambda x: today_part(x, "06:10")))  # carried: it keeps its start
    b.until(7, 30)
    assert len(b.events("schedule_started")) == 1 and b.events("run_missed") == []
