"""Three-phase pump bench — the refined default AAB (aab_pump_three_phase.json, template
version 2) on the first three-phase site: one three-phase pump, four solenoid valves, no
flow switches (_audit/103 §7). R-9, R-11, R-13 on three phases, plus overload, voltage
range, and the two starter-panel detections (no_start, manual_run).

Deterministic like test_blocks_runtime: SimClock + SimulatedIO, ``rt.step()`` per second.
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agal_one_agent.blocks.clock import SimClock
from agal_one_agent.blocks.io import SimulatedIO
from agal_one_agent.blocks.runtime import BlockRuntime, RecordingSink
from agal_one_agent.blocks.simulate import build_bundle_from_defaults

HERE = os.path.dirname(__file__)
DEFAULTS = os.path.normpath(os.path.join(HERE, "..", "..", "..", "contracts", "programs", "defaults"))
TZ = ZoneInfo("Asia/Kolkata")
PUMP = "pump-1"


class Bench3:
    def __init__(self, rated_current: float = 0.0):
        if not os.path.isdir(DEFAULTS):
            pytest.skip(f"defaults not found at {DEFAULTS}")
        bundle = build_bundle_from_defaults(DEFAULTS, "farm3")
        bundle["assets"][0]["aab"]["settings"]["rated_current"] = rated_current
        self.clock = SimClock(datetime(2026, 9, 21, 5, 0, tzinfo=TZ))
        self.io = SimulatedIO()
        self.sink = RecordingSink()
        self.rt = BlockRuntime(self.io, self.sink, clock=self.clock)
        self.supply(True, 235.0)
        self.amps(0.0)
        self.rt.compile(bundle)

    def run(self, seconds: float):
        t = 0.0
        while t < seconds:
            self.rt.step()
            self.clock.advance(1.0)
            t += 1.0

    def supply(self, present: bool, volts: float):
        for ph in "ryb":
            self.io.set_input(PUMP, f"sensor.mains.{ph}", present)
            self.io.set_input(PUMP, f"sensor.voltage.{ph}", volts)

    def amps(self, r: float, y: float | None = None, b: float | None = None):
        for ph, v in zip("ryb", (r, r if y is None else y, r if b is None else b)):
            self.io.set_input(PUMP, f"sensor.current.{ph}", v)

    def relay(self) -> bool:
        return self.io.output(PUMP, "device.power", False)

    def var(self, name: str):
        return self.rt.snapshot()["assets"][PUMP][name]

    def logs(self) -> str:
        return " | ".join(str(entry) for entry in self.sink.logs)

    def start(self, amps: float = 10.0, plot: str = "plot-1"):
        """Run a plot, let the pump start, then let the baseline be learned at `amps`."""
        self.rt.apply_command({"type": "runPlot", "plotId": plot})
        self.run(6)  # valve, 3 s energise delay, contactor
        assert self.relay() is True
        self.amps(amps)
        self.run(14)  # 2 s after the start + 10 s window
        assert self.var("running") is True


def test_runs_healthy_with_three_balanced_phases():
    b = Bench3()
    b.run(1)
    b.start(10.0)
    assert b.relay() is True
    assert b.var("healthy") is True and b.var("fault") is False
    assert b.var("current") == pytest.approx(10.0)
    assert b.var("voltage") == pytest.approx(235.0)
    assert b.var("current_imbalance") == pytest.approx(0.0)
    assert b.io.output("valve-1", "device.power", False) is True


def test_phase_loss_while_running_cuts_the_pump():
    b = Bench3()
    b.run(1)
    b.start(10.0)
    b.io.set_input(PUMP, "sensor.mains.y", False)
    b.run(3)
    assert b.relay() is False
    assert b.var("phase_loss") is True and b.var("fault") is True
    assert "Phase loss" in b.logs()


def test_start_is_refused_on_a_missing_phase():
    b = Bench3()
    b.io.set_input(PUMP, "sensor.mains.b", False)
    b.run(1)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-1"})
    b.run(8)
    assert b.relay() is False
    assert "Start refused: a phase is missing" in b.logs()


def test_phase_imbalance_cuts_the_pump():
    b = Bench3()
    b.run(1)
    b.start(10.0)
    b.amps(10.0, 10.0, 6.0)  # weakest phase 31 % below the mean of 8.67 A; the limit is 25 %
    b.run(4)
    assert b.relay() is True, "not before the 5 s hold"
    b.run(4)
    assert b.relay() is False
    assert "Phase imbalance" in b.logs()


def test_dry_run_trips_on_a_drop_below_the_learned_running_current():
    b = Bench3()
    b.run(1)
    b.start(10.0)
    b.amps(6.0)  # a dry pump still draws current: 40 % below the learned 10 A
    b.run(4)
    assert b.relay() is True, "not before the 5 s hold"
    b.run(4)
    assert b.relay() is False
    assert b.var("dry_run") is True and b.var("no_start") is False
    assert b.io.output("valve-1", "device.power", True) is False, "the NAB closes the valves on a dry run"


def test_overload_trips_above_the_rated_current():
    b = Bench3(rated_current=10.0)
    b.run(1)
    b.start(10.0)
    b.amps(13.0)  # 130 % of rated; the limit is 120 % held for 10 s
    b.run(8)
    assert b.relay() is True, "not before the 10 s hold"
    b.run(5)
    assert b.relay() is False
    assert b.var("overload") is True
    assert "Overload" in b.logs()


def test_overload_is_inert_until_a_rated_current_is_set():
    b = Bench3(rated_current=0.0)
    b.run(1)
    b.start(10.0)
    b.amps(13.0, 13.0, 13.0)
    b.run(30)
    assert b.relay() is True
    assert b.var("overload") is False


def test_voltage_out_of_range_cuts_the_pump_and_clears_when_the_supply_returns():
    b = Bench3()
    b.run(1)
    b.start(10.0)
    b.supply(True, 170.0)  # below min_voltage 190 V
    b.run(8)
    assert b.relay() is False
    assert b.var("voltage_fault") is True
    assert "Voltage out of range" in b.logs()
    b.amps(0.0)
    b.supply(True, 232.0)
    b.run(3)
    assert b.var("voltage_fault") is False


def test_start_is_refused_on_bad_voltage():
    b = Bench3()
    b.supply(True, 280.0)  # above max_voltage 265 V
    b.run(1)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-1"})
    b.run(8)
    assert b.relay() is False
    assert "Start refused: voltage" in b.logs()


def test_commanded_but_no_current_is_reported_as_did_not_start_not_as_a_dry_run():
    b = Bench3()
    b.run(1)
    b.start(10.0)  # a first run learns the baseline, so dry-run logic is armed
    b.rt.apply_command({"type": "stopPlot", "plotId": "plot-1"})
    b.amps(0.0)
    b.run(6)
    assert b.relay() is False
    # Second start: the overload relay at the panel has tripped, the contactor never pulls in.
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-2"})
    b.run(6)
    assert b.relay() is True
    b.run(12)
    assert b.relay() is False
    assert b.var("no_start") is True
    assert b.var("dry_run") is False
    assert "did not start" in b.logs()


def test_a_run_started_by_hand_at_the_panel_is_detected_and_clears():
    b = Bench3()
    b.run(2)
    assert b.relay() is False
    b.amps(9.5)  # someone pressed the starter's green button
    b.run(4)
    assert b.var("manual_run") is False, "not before the 5 s hold"
    b.run(3)
    assert b.var("manual_run") is True
    assert "started at the panel" in b.logs()
    b.amps(0.0)
    b.run(3)
    assert b.var("manual_run") is False
    assert "ended" in b.logs()


def test_trip_flags_clear_on_the_next_start():
    b = Bench3()
    b.run(1)
    b.start(10.0)
    b.amps(6.0)
    b.run(8)
    assert b.var("dry_run") is True
    b.amps(0.0)
    b.run(2)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-3"})
    b.run(6)
    assert b.relay() is True
    assert b.var("dry_run") is False


# ------------------------------------------------ the NAB's response to pump faults

def _alerts(b: Bench3) -> list[str]:
    return [a["text"] for a in b.sink.alerts]


def test_nab_closes_the_valves_and_alerts_when_the_pump_does_not_start():
    b = Bench3()
    b.run(1)
    b.rt.apply_command({"type": "runPlot", "plotId": "plot-2"})
    b.run(20)  # contactor never pulls in: no current
    assert b.relay() is False and b.var("no_start") is True
    assert b.io.output("valve-2", "device.power", True) is False, "no open valve left behind a pump that is off"
    assert any("Pump stopped on a fault" in a for a in _alerts(b))


def test_nab_alerts_on_phase_loss_and_closes_the_valves():
    b = Bench3()
    b.run(1)
    b.start(10.0)
    b.io.set_input(PUMP, "sensor.mains.r", False)
    b.run(4)
    assert b.relay() is False
    assert b.io.output("valve-1", "device.power", True) is False
    assert any("Pump stopped on a fault" in a for a in _alerts(b))


def test_dry_run_raises_its_own_alert_once_not_the_generic_fault_alert():
    b = Bench3()
    b.run(1)
    b.start(10.0)
    b.amps(6.0)
    b.run(9)
    assert b.var("dry_run") is True
    texts = _alerts(b)
    assert any("running dry" in a for a in texts)
    assert not any("Pump stopped on a fault" in a for a in texts)


def test_hand_start_with_every_valve_closed_raises_a_critical_alert():
    b = Bench3()
    b.run(2)
    b.amps(9.5)
    b.run(8)
    assert any("started at the starter panel" in a for a in _alerts(b))
    assert not any("every valve closed" in a for a in _alerts(b)), "not before the 10 s hold"
    b.run(10)
    crit = [a for a in b.sink.alerts if "every valve closed" in a["text"]]
    assert crit and crit[0]["severity"] == "critical"


def test_hand_start_with_a_plot_open_is_only_a_warning():
    b = Bench3()
    b.run(1)
    b.start(10.0)
    b.rt.apply_command({"type": "setPower", "assetId": PUMP, "sourceKey": "device.power", "value": 0})
    b.run(1)
    # The farmer pushes the green button at the panel while plot 1 is still open.
    b.run(12)
    assert b.var("manual_run") is True
    assert any("started at the starter panel" in a for a in _alerts(b))
    assert not any("every valve closed" in a for a in _alerts(b))

