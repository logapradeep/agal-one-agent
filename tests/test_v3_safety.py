"""The E-stop latch and power sense (contracts v1.15.0, programs/README.md §7.8, §7.10) — no hardware: the pump
house of the golden vectors. The E-stop's aux contact is normally closed (pressed = it reads open), its reset
is a push button at the panel; mains and the UPS output are sensed on two inputs."""

from __future__ import annotations

import pytest

from v3_bench import ESTOP, MAINS, PUMP, UPS, V1, V2, Bench, pumphouse
from test_v3_valves import both_valves, pulses

RULE1 = {"containerKind": "field", "containerId": "field_north01", "slot": 1}


def press(b):
    b.set(ESTOP, "sensor.contact", False)


def release(b):
    b.set(ESTOP, "sensor.contact", True)


def reset_button(b, down: bool):
    b.set(ESTOP, "sensor.reset", down)


def running(b: Bench, until=(6, 5)) -> None:
    b.step()
    b.until(*until)
    assert b.pump_on() and b.level(V1, "valve.open")


# ---------------------------------------------------------------------- E-stop

def test_pressed_latches_every_motor_off_and_holds_the_blocks_that_drive_one():
    b = Bench(pumphouse())
    running(b)
    m = b.mark()
    press(b)
    b.run(1)
    assert b.pump_on() is False and b.physical(PUMP, "device.power") is False
    [e] = b.events_since(m, "estop_pressed")
    assert e["estop"]["motorsStopped"] == [PUMP] and e["estop"]["heldBlocks"] == [RULE1]
    assert b.source_of(e) == "node"
    assert b.level(V1, "valve.open") is True  # a block that drives no motor runs on
    assert b.rt.status_extra()["safety"]["estop"]["latched"] is True
    assert ("field:field_north01/rule/1", "estop") in {(h["key"], h["reason"]) for h in b.rt.status_extra()["held"]}


@pytest.mark.parametrize("command", [
    {"type": "setPower", "assetId": PUMP, "sourceKey": "device.power", "value": 1},
    {"type": "resumeAutomation"},
    {"type": "resetValve", "assetId": PUMP},
])
def test_no_command_restarts_or_clears_it(command):
    b = Bench(pumphouse())
    running(b)
    press(b)
    b.run(1)
    release(b)
    b.run(1)
    if command["type"] == "setPower":
        assert b.rt.apply_command({"type": "pauseAutomation"})["accepted"]
        b.run(1)
    r = b.rt.apply_command(command)
    assert r["accepted"] is False
    if command["type"] == "setPower":
        assert r["reason"] == "refused: the E-stop is latched at the panel"
    b.run(5)
    assert b.rt.prog.estop.latched and b.pump_on() is False


def test_it_clears_only_at_the_panel_released_first_then_the_reset():
    b = Bench(pumphouse())
    running(b)
    press(b)
    b.run(1)
    reset_button(b, True)  # reset while still pressed: nothing
    b.run(1)
    reset_button(b, False)
    b.run(1)
    assert b.rt.prog.estop.latched
    reset_button(b, True)  # held down while the button is released
    release(b)
    b.run(2)
    assert b.rt.prog.estop.latched  # a reset that was already down does not count
    reset_button(b, False)
    b.run(1)
    m = b.mark()
    reset_button(b, True)
    b.run(1)
    [c] = b.events_since(m, "estop_cleared")
    assert c["estop"]["resetKind"] == "reset_input" and c["estop"]["latchedSeconds"] >= 4
    # The held rule resumes under its own logic: the pump follows the open valve again, 3 s later.
    [w] = b.events_since(m, "watering_resumed")
    assert w["watering"]["cause"] == "estop_cleared"
    b.run(3)
    assert b.pump_on()


def test_a_broken_wire_or_an_unreadable_contact_reads_as_pressed():
    b = Bench(pumphouse())
    running(b)
    b.set(ESTOP, "sensor.contact", None)
    b.run(1)
    assert b.events("estop_pressed") and b.pump_on() is False


def test_a_normally_open_contact_and_a_panel_start_reset():
    bundle = pumphouse()
    bundle["safety"]["emergencyStop"]["contact"] = "no"
    bundle["safety"]["emergencyStop"]["resetKind"] = "panel_start"
    b = Bench(bundle, inputs={(ESTOP, "sensor.contact"): False, (ESTOP, "sensor.reset"): False, (MAINS, "sensor.power"): True,
                              (UPS, "sensor.power"): True, (PUMP, "sensor.current"): 0.0})
    running(b)
    b.set(ESTOP, "sensor.contact", True)  # closes when pressed
    b.run(1)
    assert b.pump_on() is False
    b.set(ESTOP, "sensor.contact", False)
    b.run(1)
    reset_button(b, True)
    b.run(1)
    assert b.events("estop_cleared")[0]["estop"]["resetKind"] == "panel_start"


def test_the_latch_survives_a_restart(tmp_path):
    b = Bench(pumphouse(), state_dir=str(tmp_path))
    running(b)
    press(b)
    b.run(1)
    b.rt.stop(safe_state=True)
    release(b)  # released while the node was down: still latched until the reset at the panel
    rt = b.new_runtime()
    rt.compile(b.bundle)
    b.step()
    b.run(10)
    assert rt.prog.estop.latched and b.pump_on() is False


def test_cleared_after_the_planned_end_logs_the_water_not_delivered():
    b = Bench(pumphouse())
    running(b, until=(6, 20))
    press(b)
    b.run(1)
    b.until(6, 30)
    assert b.level(V1, "valve.open") is False  # valves may still close: the run ended on its clock
    release(b)
    b.run(1)
    m = b.mark()
    reset_button(b, True)
    b.run(1)
    [n] = b.events_since(m, "watering_not_finished")
    assert n["watering"]["reason"] == "estop" and n["watering"]["scheduleId"] == "morning"
    assert b.events_since(m, "run_cut_short")


def test_a_valve_may_be_reset_while_the_estop_is_latched():
    b = Bench(both_valves(pumphouse()))
    running(b)
    b.set(UPS, "sensor.power", False)
    b.run(1)
    b.set(UPS, "sensor.power", True)
    press(b)
    b.run(1)
    assert b.rt.apply_command({"type": "resetValve", "assetId": V2})["accepted"]
    b.run(1)
    assert b.events("valve_reset") and b.rt.prog.estop.latched and b.pump_on() is False


# ----------------------------------------------------------------- power sense

def test_mains_drop_is_a_power_cut_motors_off_then_continuous_valves_and_the_latching_valve_stays():
    b = Bench(both_valves(pumphouse()))
    running(b, until=(6, 10))
    assert b.level(V2, "valve.open")
    m = b.mark()
    b.set(MAINS, "sensor.power", False)
    b.run(1)
    [c] = b.events_since(m, "power_cut")
    assert c["power"]["supply"] == "mains" and c["power"]["duringWatering"] is True
    assert c["power"]["runningBlocks"] == [{"containerKind": "field", "containerId": "field_north01", "slot": "instruction", "partId": "pumphouse"}]
    assert b.source_of(c) == "node"
    w = b.writes_since(m)
    assert [x["assetId"] for x in w if x.get("value") is False] == [PUMP, V1]
    assert pulses(w) == [] and b.level(V2, "valve.open") is True
    assert b.events_since(m, "watering_not_finished") == []  # a power cut never rings


def test_watering_resumes_30_s_after_the_power_is_back_for_the_time_it_had_left():
    b = Bench(pumphouse())
    running(b, until=(6, 10))
    b.set(MAINS, "sensor.power", False)
    b.until(6, 12)
    m = b.mark()
    b.set(MAINS, "sensor.power", True)
    b.run(1)  # 06:12:01
    [r] = b.events_since(m, "power_restored")
    assert r["power"]["resumeAfterSeconds"] == 30 and r["power"]["outageSeconds"] == pytest.approx(121, abs=1)
    b.run(29)  # 06:12:30
    assert b.level(V1, "valve.open") is False and b.pump_on() is False
    b.run(1)  # 06:12:31 = 30 s after the power came back
    assert b.level(V1, "valve.open") is True
    [w] = b.events_since(m, "watering_resumed")
    assert w["watering"]["cause"] == "power" and w["watering"]["remainingSeconds"] == pytest.approx(20 * 60 - 1, abs=1)
    b.run(3)
    assert b.pump_on()  # the rules start the pump as usual
    b.until(6, 32, 29)
    assert b.level(V1, "valve.open") is True
    b.run(2)
    assert b.level(V1, "valve.open") is False and b.pump_on() is False


def test_after_a_power_cut_the_rules_start_the_pump_even_when_a_latching_valve_stayed_open():
    """The latching Valve 2 stays open through the cut, so 'a valve is open' never stops being true: the rule
    slot restarts as after a program load when the run resumes, and starts the pump 3 s later as usual."""
    b = Bench(both_valves(pumphouse()))
    running(b, until=(6, 10))
    b.set(MAINS, "sensor.power", False)
    b.run(5)
    assert b.pump_on() is False and b.level(V2, "valve.open") is True
    b.set(MAINS, "sensor.power", True)
    b.run(31)
    assert b.level(V1, "valve.open") is True and b.pump_on() is False
    b.run(3)
    assert b.pump_on() is True


def test_power_back_after_the_window_the_run_is_cut_short_a_notice_never_a_ring():
    b = Bench(pumphouse())
    running(b, until=(6, 10))
    b.set(MAINS, "sensor.power", False)
    b.until(7, 40)  # the window: 06:00 to 06:30 + 60 min
    m = b.mark()
    b.set(MAINS, "sensor.power", True)
    b.run(31)
    assert b.events_since(m, "run_cut_short") and b.events_since(m, "watering_not_finished") == []
    assert b.events_since(m, "watering_resumed") == []
    assert b.level(V1, "valve.open") is False


def test_power_back_late_the_run_ends_with_its_window():
    b = Bench(pumphouse())
    running(b, until=(6, 10))
    b.set(MAINS, "sensor.power", False)
    b.until(7, 20)
    b.set(MAINS, "sensor.power", True)
    b.run(31)
    assert b.level(V1, "valve.open") is True
    m = b.mark()
    b.until(7, 30, 1)
    assert b.level(V1, "valve.open") is False
    assert b.events_since(m, "run_cut_short") and b.events_since(m, "watering_not_finished") == []


def test_the_clock_coming_good_no_longer_raises_power_restored():
    b = Bench(pumphouse())
    b.clock.ok = False
    b.step()
    b.run(5)
    b.clock.ok = True
    b.run(5)
    assert b.events("power_restored") == []


def test_a_schedule_due_during_a_power_cut_starts_when_the_power_is_back():
    b = Bench(pumphouse())
    b.step()
    b.set(MAINS, "sensor.power", False)
    b.until(6, 5)
    assert b.level(V1, "valve.open") is False
    b.set(MAINS, "sensor.power", True)
    b.run(31)
    assert b.level(V1, "valve.open") is True and b.events("schedule_started")


def test_a_restart_without_ups_faults_latching_valves_and_resumes_30_s_after_the_boot_pass(tmp_path):
    b = Bench(both_valves(pumphouse()), state_dir=str(tmp_path))
    running(b, until=(6, 10))
    b.run(60)  # the state is kept while a run is in progress
    # The node dies (no clean stop), and comes back 2 min later.
    b.clock.advance(120)
    rt = b.new_runtime()
    m = b.mark()
    rt.compile(b.bundle)
    b.step()
    [f] = b.events_since(m, "valve_fault")
    assert f["valve"]["reason"] == "unclean_boot"
    assert b.level(V1, "valve.open") is False and b.pump_on() is False
    b.run(35)
    # Valve 2 is in FAULT: the part that opens it holds, so nothing of it opens — or is said to resume — until a
    # person resets it; its run's clock runs from 30 s after the boot pass.
    assert b.events_since(m, "watering_resumed") == []
    assert b.level(V1, "valve.open") is False and b.pump_on() is False
    assert b.rt.apply_command({"type": "resetValve", "assetId": V2})["accepted"]
    b.run(5)
    assert b.level(V1, "valve.open") is True and b.level(V2, "valve.open") is True and b.pump_on() is True
    [w] = b.events_since(m, "watering_resumed")
    assert w["watering"]["cause"] == "valve_reset"
    # It had 19–20 min left when the node died (the state is kept every minute while a run is in progress); its
    # clock ran from the resume 30 s after the boot pass, and the reset came a few seconds later.
    assert 18 * 60 < w["watering"]["remainingSeconds"] < 20 * 60


def test_a_clean_restart_resumes_the_run_with_no_fault(tmp_path):
    b = Bench(pumphouse(), state_dir=str(tmp_path))
    running(b, until=(6, 10))
    b.rt.stop(safe_state=True)
    b.clock.advance(60)
    rt = b.new_runtime()
    m = b.mark()
    rt.compile(b.bundle)
    b.step()
    b.run(31)
    assert b.events_since(m, "valve_fault") == []
    [w] = b.events_since(m, "watering_resumed")
    assert w["watering"]["cause"] == "restart" and b.level(V1, "valve.open") is True
