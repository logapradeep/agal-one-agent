"""An input's wiring facts reach the line (contracts v1.15.0, programs/README.md §7.2; nodes/README.md §1): a GPIO
input's `pull` is the bias of its line request, a safety input with no pull stated is pulled up, and an input's
`debounceMs` from the bundle's port entry is applied before its value changes — the flow switch's 200 ms included.

Everything here runs against a FAKE gpiochip (LineIO's opener) on the real hardware adapter (HardwareIO), so what is
checked is the request the kernel would get. The chip models a bias as it acts on a wire: a line nothing drives (a
broken wire, an open contact) reads as its bias pulls it — HIGH pulled up, LOW pulled down — and floats with no bias
(it reads differently every time). Found by the wave-1 integration check: the agent ignored `pull` since 0.3.0, so
an E-stop input with no pull-up on the board floated.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
import tempfile
import types
from datetime import datetime
from typing import Optional

import pytest

import v3_vectors as V
from v3_bench import CLEAN, ESTOP, MAINS, PUMP, TZ, UPS, Bench, fieldbox, pumphouse
from agal_one_agent.blocks.clock import SimClock
from agal_one_agent.blocks.io import HardwareIO, PortRef, SimulatedIO
from agal_one_agent.blocks.runtime import BlockRuntime, RecordingSink
from agal_one_agent.blocks.v3 import program as P
from agal_one_agent.blocks.v3.runtime import STATE_FILE, RuntimeV3
from agal_one_agent.ports.gpiochip import PULL_TO_BIAS, BiasNotApplied, ChipInfo, LineIO, PortUnavailable
from agal_one_agent.ports.port_test import run_port_test

pytestmark = pytest.mark.skipif(not os.path.isdir(V.VECTORS), reason=f"vectors not found at {V.VECTORS}")

LABEL, PATH = "pinctrl-bcm2835", "/dev/gpiochip0"
FLOW = "asset_flow0001"


# ------------------------------------------------------------------ the fake chip

class GPIOError(IOError):
    """python-periphery's GPIOError, as its character-device GPIO raises it for a bias on a kernel older than 5.5."""


class Chip:
    """One gpiochip. ``driven`` is what drives each line from outside (a contact to ground: False; a sense module's
    output: True); a line absent from it is driven by nothing. ``requests`` records every line request as the kernel
    gets it: (line, direction, bias). ``bias_fails``: the lines (or True: every line) on which a bias raises, as on
    an old kernel; ``takes_bias=False``: an opener with no bias argument at all (a library without one)."""

    def __init__(self, driven: Optional[dict] = None, bias_fails=(), takes_bias: bool = True, busy=()):
        self.driven: dict[int, bool] = dict(driven or {})
        self.requests: list[tuple[int, str, Optional[str]]] = []
        self.out: dict[int, bool] = {}
        self.levels_set: list[tuple[int, bool]] = []  # every level this node drove a line to, in order
        self.bias_fails, self.takes_bias, self.busy = bias_fails, takes_bias, set(busy)
        self._float = False

    def opener(self, path, line, direction, **kw):
        assert path == PATH
        if kw and not self.takes_bias:
            raise TypeError("GPIO.__init__() got an unexpected keyword argument 'bias'")
        bias = kw.get("bias")
        if line in self.busy:
            raise OSError(16, "Device or resource busy")
        if bias is not None and (self.bias_fails is True or line in self.bias_fails):
            raise GPIOError(None, "Line bias configuration not supported by kernel version 5.4.")
        self.requests.append((line, direction, bias))
        if direction in ("high", "low"):
            self.out[line] = direction == "high"
            self.levels_set.append((line, direction == "high"))
        return _Line(self, line, direction, bias)

    def level(self, line: int, bias: Optional[str]) -> bool:
        if line in self.out:
            return self.out[line]
        if line in self.driven:
            return self.driven[line]
        if bias == "pull_up":
            return True
        if bias == "pull_down":
            return False
        self._float = not self._float  # nothing drives it and nothing pulls it: it floats
        return self._float

    def input_bias(self) -> dict[int, Optional[str]]:
        """Each input line → the bias of its LAST request."""
        return {line: bias for line, d, bias in self.requests if d == "in"}

    def lineio(self) -> LineIO:
        return LineIO(chips=lambda: [ChipInfo(PATH, "gpiochip0", LABEL, 54)], opener=self.opener)


class _Line:
    def __init__(self, chip: Chip, line: int, direction: str, bias: Optional[str]):
        self.chip, self.line, self.direction, self.bias = chip, line, direction, bias

    def read(self) -> bool:
        return self.chip.level(self.line, self.bias)

    def write(self, level: bool) -> None:
        self.chip.out[self.line] = bool(level)
        self.chip.levels_set.append((self.line, bool(level)))

    def close(self) -> None:
        self.chip.out.pop(self.line, None)


class Adc:
    """The pump's current input (AI1, an ADS1115 channel): a value per (bus, address, channel)."""

    def __init__(self):
        self.values: dict[tuple, float] = {}

    @staticmethod
    def supports(t: dict) -> bool:
        return (t or {}).get("kind") == "i2c" and bool((t or {}).get("measure") or (t or {}).get("transform"))

    def read(self, t: dict, fresh: bool = False) -> Optional[float]:
        return self.values.get((t.get("busId"), t.get("addr"), t.get("channel")), 0.0)


# The pump house wired and healthy (site-north.json): the E-stop's NC aux contact closed to ground on DI1 (line 5);
# its reset button open on DI4 (line 19: nothing drives it, the pull-up holds it HIGH); the mains and UPS sense
# modules driving DI2 / DI3 (lines 6, 13) HIGH.
PUMPHOUSE_WIRED = {5: False, 6: True, 13: True}


class HwBench:
    """A v3 node on the hardware adapter: RuntimeV3 → HardwareIO → LineIO → the fake chip."""

    def __init__(self, bundle: dict, chip: Optional[Chip] = None, persisted: Optional[dict] = None,
                 start: Optional[datetime] = None, node_uid: str = "node_pumphouse"):
        self.chip = chip or Chip(PUMPHOUSE_WIRED)
        self.adc = Adc()
        self.io = HardwareIO(lines=self.chip.lineio(), analog=self.adc)
        self.clock = SimClock(start or datetime(2026, 10, 15, 5, 59, 58, tzinfo=TZ))
        self.sink = RecordingSink()
        d = tempfile.mkdtemp(prefix="v3hw-")
        with open(os.path.join(d, STATE_FILE), "w") as f:
            json.dump(CLEAN if persisted is None else persisted, f)
        self.rt = RuntimeV3(self.io, self.sink, clock=self.clock, state_dir=d, node_uid=node_uid)
        self.rt.compile(bundle)

    def step(self) -> None:
        self.rt.step()

    def run(self, seconds: float, every: float = 1.0) -> None:
        t = 0.0
        while t < seconds - 1e-9:
            self.clock.advance(every)
            self.rt.step()
            t += every

    def input(self, aid: str, sk: str):
        return self.rt.st.inputs.get((aid, sk))

    def events(self, type_: str) -> list[dict]:
        return [e for e in self.sink.events if e["type"] == type_]


def gpio_inputs(bundle: dict):
    """(card, sourceKey, entry) for every GPIO input port of a bundle."""
    for a in bundle["assets"]:
        for sk, t in a["ports"].items():
            if t.get("kind") == "gpio" and t.get("direction") == "in":
                yield a, sk, t


VECTOR_BUNDLES = [("two-node-layout.json", "node_pumphouse"), ("two-node-layout.json", "node_fieldbox"),
                  ("instruction-switch.json", "v1"), ("instruction-switch.json", "v2"),
                  ("rule-edit-swap.json", "v1"), ("rule-edit-swap.json", "v2"), ("rule-edit-swap.json", "v3"),
                  ("rewire-swap.json", "v1"), ("rewire-swap.json", "v2")]


def vector_bundle(name: str, which: str) -> tuple[dict, dict]:
    vec = V.load(name)
    if "expect" in vec and "bundles" in (vec["expect"] or {}):
        b = copy.deepcopy(vec["expect"]["bundles"][which])
        b.setdefault("version", 1)
        b.setdefault("compiledAt", "2026-10-15T00:30:00Z")
        return b, {}
    return V.bundle(vec, which), vec


# ============================================= the golden vectors' wiring facts on the line

@pytest.mark.parametrize("name,which", VECTOR_BUNDLES)
def test_every_gpio_input_of_the_vector_bundles_is_requested_with_the_pull_its_entry_states(name, which):
    """The updated expectations of contracts v1.15.0: each GPIO input of every v3 bundle the vectors carry is
    requested with its entry's `pull` as the line's bias (none stated: no bias flag), and every safety input
    states one — the compiler's `up` (or the port's own `down`) reached the bundle."""
    bundle, vec = vector_bundle(name, which)
    node = vec.get("node") or which
    b = HwBench(bundle, persisted=(vec.get("initial") or {}).get("persisted"), node_uid=node)
    b.step()
    got = b.chip.input_bias()
    want = {int(t["line"]): PULL_TO_BIAS.get(t.get("pull")) for _, _, t in gpio_inputs(bundle)}
    assert want, f"{name} {which}: no GPIO input"
    assert {line: got.get(line, "never requested") for line in want} == want
    for a, sk, t in gpio_inputs(bundle):
        if a.get("assetType") in P.SAFETY_INPUT_TYPES:
            assert t.get("pull") in ("up", "down", "none"), f"{a['assetId']} {sk}: a safety input with no pull"


def test_the_pump_house_lines_are_requested_as_site_north_wires_them():
    """site-north.json: DI1 (E-stop contact, line 5) and DI4 (reset, line 19) pulled up; DI2 / DI3 (the mains and UPS
    sense modules, active high, lines 6 / 13) pulled down; the outputs with no bias. The field box's flow switch
    (DI1, line 5) states no pull: requested with no bias."""
    ph = HwBench(pumphouse())
    ph.step()
    assert ph.chip.input_bias() == {5: "pull_up", 19: "pull_up", 6: "pull_down", 13: "pull_down"}
    assert all(bias is None for _, d, bias in ph.chip.requests if d != "in")
    fb = HwBench(fieldbox(), chip=Chip({5: False}), node_uid="node_fieldbox")
    fb.step()
    assert fb.chip.input_bias() == {5: None}


def test_with_its_pull_up_a_broken_estop_wire_reads_pressed():
    """Why the pull matters: the contact switches the input to ground. A wire that comes off leaves the line to its
    bias — pulled up it reads HIGH, which the active-low NC contact reads as open: pressed. The motor never starts."""
    b = HwBench(pumphouse(), chip=Chip({6: True, 13: True}))  # line 5 driven by nothing: the broken wire
    b.step()
    b.run(10)
    assert b.rt.prog.estop.latched and b.events("estop_pressed")
    assert b.rt.level(PUMP, "device.power") is False
    ok = HwBench(pumphouse())  # the same, wired: the closed contact holds line 5 LOW
    ok.step()
    ok.run(10)
    assert not ok.rt.prog.estop.latched and ok.rt.level(PUMP, "device.power") is True


def test_a_safety_input_whose_entry_states_no_pull_is_pulled_up_and_a_stated_pull_wins():
    """README §7.2's default when an older compiler left it out: an E-stop or power-sense input with no pull is
    requested pulled up; the mains sense's stated `down` stays down; a plain input with none gets no bias."""
    bundle = pumphouse()
    for a in bundle["assets"]:
        if a["assetId"] == ESTOP:
            for t in a["ports"].values():
                t.pop("pull", None)
        if a["assetId"] == UPS:
            a["ports"]["sensor.power"].pop("pull")
    b = HwBench(bundle)
    b.step()
    assert b.chip.input_bias() == {5: "pull_up", 19: "pull_up", 6: "pull_down", 13: "pull_up"}
    assert all(b.rt.prog.cards[aid].ports[sk].safety for aid, sk in
               [(ESTOP, "sensor.contact"), (ESTOP, "sensor.reset"), (MAINS, "sensor.power"), (UPS, "sensor.power")])
    assert b.rt.prog.cards[PUMP].ports["sensor.current"].safety is False
    fb = HwBench(fieldbox(), chip=Chip({5: False}), node_uid="node_fieldbox")
    fb.step()
    assert fb.chip.input_bias() == {5: None} and fb.rt.prog.cards[FLOW].ports["sensor.flow"].safety is False


# ====================================================== a bias that cannot be applied

def test_an_estop_contact_that_cannot_be_biased_is_read_as_pressed(caplog):
    """The kernel refuses the bias on line 5 (as one older than 5.5 does): the contact is NOT read from a line that
    may float. It reads unknown, which the E-stop takes as pressed — latched, the motor never starts — and a warning
    names the port, even though the contact is in fact closed."""
    chip = Chip(PUMPHOUSE_WIRED, bias_fails={5})
    with caplog.at_level(logging.WARNING):
        b = HwBench(pumphouse(), chip=chip)
        b.step()
        b.run(10)
    assert b.input(ESTOP, "sensor.contact") is None
    assert b.rt.prog.estop.latched and [e["type"] for e in b.events("estop_pressed")] == ["estop_pressed"]
    assert b.rt.level(PUMP, "device.power") is False
    assert (5, "in", None) in chip.requests  # the line is open, with no bias — and not read as a value
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    named = [m for m in msgs if "sensor.contact of card asset_estop001" in m and "line 5 of pinctrl-bcm2835" in m]
    assert len(named) == 1 and "pull up could not be applied" in named[0] and "reads pressed" in named[0]
    assert any("line 5 of pinctrl-bcm2835: pull up could not be applied" in m for m in msgs)  # the line layer's


def test_a_reset_that_cannot_be_biased_never_clears_the_latch():
    """The reset is a push button closing to ground; with no pull-up its line floats — and a floating line would
    'press' it. Unbiasable, it is never seen: the latch stays until the bias can be applied."""
    def press_release_reset(b: HwBench) -> None:
        b.chip.driven.pop(5)          # the button pressed: the NC aux contact opens
        b.run(2)
        assert b.rt.prog.estop.latched
        b.chip.driven[5] = False      # released
        b.run(2)
        b.chip.driven[19] = False     # reset pressed (to ground)…
        b.run(2)
        b.chip.driven.pop(19)         # …and let go
        b.run(2)

    good = HwBench(pumphouse())
    good.step()
    press_release_reset(good)
    assert not good.rt.prog.estop.latched and good.events("estop_cleared")
    bad = HwBench(pumphouse(), chip=Chip(PUMPHOUSE_WIRED, bias_fails={19}))
    bad.step()
    assert not bad.rt.prog.estop.latched  # the contact itself is biased and closed
    press_release_reset(bad)
    assert bad.input(ESTOP, "sensor.reset") is None
    assert bad.rt.prog.estop.latched and bad.events("estop_cleared") == []


def test_on_a_kernel_with_no_bias_the_sense_inputs_are_never_sensed_present():
    """No bias anywhere: the mains and UPS sense read unknown (presence comes from a reading, never a guess); the
    latching Valve 2's supply is not sensed, so it is in FAULT and sends no pulse; the E-stop reads pressed."""
    b = HwBench(pumphouse(), chip=Chip(PUMPHOUSE_WIRED, bias_fails=True))
    b.step()
    b.run(10)
    s = b.rt.status_extra()["safety"]
    assert s["power"]["mains"] == "unknown" and s["power"]["ups"] == "unknown"
    assert s["estop"]["latched"] is True
    assert b.rt.prog.valves["asset_valve002"].fault == "supply_lost"
    assert [lv for lv in b.chip.levels_set if lv[0] in (22, 23) and lv[1]] == []  # no coil pulse


@pytest.mark.parametrize("chip", [Chip({5: True}, bias_fails=True), Chip({5: True}, takes_bias=False)],
                         ids=["kernel-refuses-bias", "library-has-no-bias"])
def test_a_plain_input_that_cannot_be_biased_is_read_with_no_bias_and_a_warning(chip, caplog):
    """Not a safety input — the field box's flow switch, here with a pull stated: read from the line with no bias
    (its value is used) and one warning names the port; the fail-safe is the safety inputs' only."""
    bundle = fieldbox()
    bundle["assets"][[a["assetId"] for a in bundle["assets"]].index(FLOW)]["ports"]["sensor.flow"]["pull"] = "up"
    with caplog.at_level(logging.WARNING):
        b = HwBench(bundle, chip=chip, node_uid="node_fieldbox")
        b.step()
        b.run(3)
    assert b.input(FLOW, "sensor.flow") is True
    assert chip.input_bias() == {5: None}
    named = [r.getMessage() for r in caplog.records if "sensor.flow of card asset_flow0001" in r.getMessage()]
    assert len(named) == 1 and "read with no bias" in named[0]


# ========================================================================= debounce

def _flow_bundle(debounce_ms: Optional[int]) -> dict:
    b = fieldbox()
    t = next(a for a in b["assets"] if a["assetId"] == FLOW)["ports"]["sensor.flow"]
    assert t["debounceMs"] == 200  # two-node-layout.json: the flow switch keeps its 200 ms
    if debounce_ms is None:
        t.pop("debounceMs")
    else:
        t["debounceMs"] = debounce_ms
    return b


def test_the_flow_switchs_200_ms_debounce_from_the_bundle_is_applied_on_the_line():
    """The field box's flow switch, its line on the fake chip, sampled every 50 ms: a 100 ms bounce never becomes a
    value; a change that holds is taken once it has held 200 ms — not at 150 ms or 200 ms after the line changed
    (held 100 / 150 ms at the samples), by 300 ms."""
    b = HwBench(_flow_bundle(200), chip=Chip({5: False}), node_uid="node_fieldbox")
    b.step()
    assert b.input(FLOW, "sensor.flow") is False  # the first reading is the value
    b.chip.driven[5] = True   # a bounce: HIGH for 100 ms
    b.run(0.1, every=0.05)
    assert b.input(FLOW, "sensor.flow") is False
    b.chip.driven[5] = False
    b.run(0.3, every=0.05)
    assert b.input(FLOW, "sensor.flow") is False
    b.chip.driven[5] = True   # flow that stays
    b.run(0.15, every=0.05)
    assert b.input(FLOW, "sensor.flow") is False
    b.run(0.05, every=0.05)
    assert b.input(FLOW, "sensor.flow") is False
    b.run(0.1, every=0.05)
    assert b.input(FLOW, "sensor.flow") is True


def test_without_a_debounce_in_its_entry_the_same_bounce_is_taken_at_once():
    """The control: the entry is what sets the filter — with no debounceMs the 50 ms sample is the value."""
    b = HwBench(_flow_bundle(None), chip=Chip({5: False}), node_uid="node_fieldbox")
    b.step()
    b.chip.driven[5] = True
    b.run(0.05, every=0.05)
    assert b.input(FLOW, "sensor.flow") is True


def test_a_digital_input_on_a_bus_is_debounced_by_its_entry_too():
    """CompiledTransport: a bus input (an I/O expander's) that states debounceMs is a digital input — read as on/off
    every 100 ms and debounced — not an analog reading taken once a pass."""
    bundle = fieldbox()
    bundle["assets"].append({"assetId": "asset_door0001", "assetType": "sensor", "name": "Panel door",
                             "ports": {"sensor.door": {"kind": "i2c", "busId": 1, "addr": 32, "channel": 3,
                                                       "direction": "in", "debounceMs": 200}}})
    b = Bench(bundle, node_uid="node_fieldbox", inputs={(FLOW, "sensor.flow"): False, ("asset_door0001", "sensor.door"): 0})
    key = ("asset_door0001", "sensor.door")
    assert P.is_digital_input(b.rt.prog.cards["asset_door0001"].ports["sensor.door"].transport)
    b.step()
    assert b.rt.st.inputs[key] is False
    b.set(*key, 1)
    for _ in range(3):
        b.clock.advance(0.05)
        b.rt._sample(b.clock.monotonic(), digital_only=True)  # the 100 ms sampler's pass (digital inputs only)
    assert b.rt.st.inputs[key] is False
    for _ in range(3):
        b.clock.advance(0.05)
        b.rt._sample(b.clock.monotonic(), digital_only=True)
    assert b.rt.st.inputs[key] is True


def test_a_language_1_input_takes_the_port_entrys_debounce_when_its_variable_states_none():
    """nodes/README.md §1: debounceMs is a wiring fact of the port; a variable-level value overrides it. The v1
    farm kit's flow switch with its 200 ms moved from the variable to the port entry is filtered the same."""
    import _contracts
    from agal_one_agent.blocks.simulate import build_bundle_from_defaults

    def bench(move: bool) -> tuple[BlockRuntime, SimulatedIO, SimClock]:
        bundle = build_bundle_from_defaults(_contracts.DEFAULTS, "farm")
        flow = next(a for a in bundle["assets"] if a["assetId"] == "flow-1")
        var = next(v for v in flow["aab"]["variables"] if v["kind"] == "node" and v["node"]["sourceKey"] == "sensor.flow")
        assert var["node"]["debounceMs"] == 200
        if move:
            flow["ports"]["sensor.flow"]["debounceMs"] = var["node"].pop("debounceMs")
        else:
            var["node"].pop("debounceMs")
        clock, io = SimClock(datetime(2026, 9, 7, 5, 0, tzinfo=TZ)), SimulatedIO()
        rt = BlockRuntime(io, RecordingSink(), clock=clock)
        rt.compile(bundle)
        rt.step()
        io.set_input("flow-1", "sensor.flow", 1)
        return rt, io, clock

    def at(rt, clock, seconds):
        t = 0.0
        while t < seconds - 1e-9:
            clock.advance(0.05)
            rt.step()
            t += 0.05
        return rt.snapshot()["assets"]["flow-1"]["flow"]

    rt, _, clock = bench(move=True)
    assert at(rt, clock, 0.15) is False and at(rt, clock, 0.15) is True
    rt, _, clock = bench(move=False)
    assert at(rt, clock, 0.05) is True  # neither states one: no filter


# ============================================================ the line layer itself

def test_the_pull_reaches_the_line_request_as_python_peripherys_bias():
    chip = Chip({})
    lines = chip.lineio()
    for line, pull in ((5, "up"), (6, "down"), (7, "none"), (8, None)):
        lines.read(LABEL, line, pull=pull)
    assert chip.requests == [(5, "in", "pull_up"), (6, "in", "pull_down"), (7, "in", "disable"), (8, "in", None)]
    assert lines.read(LABEL, 5, pull="up") is True and lines.read(LABEL, 6, pull="down") is False


def test_an_input_is_requested_again_when_its_pull_changes_and_kept_when_none_is_asked():
    chip = Chip({})
    lines = chip.lineio()
    lines.read(LABEL, 5, pull="up")
    lines.read(LABEL, 5)              # no pull asked: the open line is kept, with its bias
    lines.read(LABEL, 5, pull="up")
    assert chip.requests == [(5, "in", "pull_up")]
    lines.read(LABEL, 5, pull="down")  # a program that rewired it
    assert chip.requests == [(5, "in", "pull_up"), (5, "in", "pull_down")]


def test_a_bias_that_cannot_be_set_opens_the_line_without_it_and_says_why():
    chip = Chip({5: False}, bias_fails={5})
    lines = chip.lineio()
    with pytest.raises(BiasNotApplied, match="line 5 of pinctrl-bcm2835: pull up could not be applied"):
        lines.read(LABEL, 5, pull="up", require_bias=True)
    assert "kernel version 5.4" in lines.bias_error(LABEL, 5)
    assert lines.read(LABEL, 5, pull="up") is False  # a caller that does not need the bias gets the reading
    assert chip.requests == [(5, "in", None)]       # one request: the line is kept open, unbiased
    lines.read(LABEL, 6, pull="up")
    assert lines.bias_error(LABEL, 6) is None


def test_an_unknown_pull_is_never_taken_as_applied():
    chip = Chip({5: False})
    lines = chip.lineio()
    with pytest.raises(BiasNotApplied, match="unknown pull"):
        lines.read(LABEL, 5, pull="pullup", require_bias=True)
    assert chip.requests == [(5, "in", None)]


def test_a_busy_line_is_unavailable_whatever_its_pull():
    lines = Chip({}, busy={5}).lineio()
    with pytest.raises(PortUnavailable, match="Device or resource busy") as e:
        lines.read(LABEL, 5, pull="up", require_bias=True)
    assert not isinstance(e.value, BiasNotApplied)


def test_the_production_opener_hands_the_bias_to_python_periphery(monkeypatch):
    """LineIO's own opener: three positional arguments (the character-device GPIO) and `bias=` only when one is
    asked for — python-periphery's CdevGPIO(path, line, direction, edge, bias, …)."""
    calls = []

    class FakeGPIO:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setitem(sys.modules, "periphery", types.SimpleNamespace(GPIO=FakeGPIO))
    LineIO._periphery_open(PATH, 5, "in", bias="pull_up")
    LineIO._periphery_open(PATH, 17, "high")
    assert calls == [((PATH, 5, "in"), {"bias": "pull_up"}), ((PATH, 17, "high"), {})]


def test_watching_an_input_in_a_port_test_applies_its_pull_and_says_when_it_could_not():
    chip = Chip({})
    out = run_port_test({"portId": "DI1", "action": "read", "seconds": 1,
                         "transport": {"kind": "gpio", "chip": LABEL, "line": 5, "direction": "in", "pull": "up"}},
                        chip.lineio(), sleep=lambda s: None)
    assert out["result"] == "passed" and out["value"] is True and chip.requests == [(5, "in", "pull_up")]
    chip = Chip({5: False}, bias_fails=True)
    out = run_port_test({"portId": "DI1", "action": "read", "seconds": 1,
                         "transport": {"kind": "gpio", "chip": LABEL, "line": 5, "direction": "in", "pull": "up"}},
                        chip.lineio(), sleep=lambda s: None)
    assert out["result"] == "passed" and "pull up could not be applied" in out["detail"]


def test_a_broadcom_numbered_input_is_set_up_with_its_pull(monkeypatch):
    """The legacy path (a port named by gpioNumber, RPi.GPIO): the pull is the pin's pull_up_down at set-up."""
    from agal_one_agent.handlers import gpio_handler
    calls = []

    class FakeRPiGPIO:
        IN, OUT, PUD_UP, PUD_DOWN, PUD_OFF = 1, 0, 22, 21, 20

        @staticmethod
        def setup(gpio, mode, initial=None, pull_up_down=None):
            calls.append(("setup", gpio, mode, pull_up_down))

        @staticmethod
        def input(gpio):
            return 1

    monkeypatch.setattr(gpio_handler, "GPIO", FakeRPiGPIO, raising=False)
    monkeypatch.setattr(gpio_handler, "_GPIO_AVAILABLE", True)
    monkeypatch.setattr(gpio_handler, "_setup_pins", {})
    io = HardwareIO(configured_gpios={5, 6}, sensor_by_key={}, lines=None)
    contact = PortRef(ESTOP, "sensor.contact", {"kind": "gpio", "gpioNumber": 5, "direction": "in", "pull": "up", "activeLow": True})
    plain = PortRef(FLOW, "sensor.flow", {"kind": "gpio", "gpioNumber": 6, "direction": "in"})
    assert io.read_input(contact) is False and io.read_input(contact) is False
    io.read_input(plain)
    assert calls == [("setup", 5, FakeRPiGPIO.IN, FakeRPiGPIO.PUD_UP), ("setup", 6, FakeRPiGPIO.IN, None)]
