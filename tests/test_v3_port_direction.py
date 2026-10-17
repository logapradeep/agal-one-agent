"""Agent 0.4.1 (contracts v1.15.3, programs/README.md §7.2 *Every port entry is complete*, §7.5a): the Pump house.

On 2026-10-17 at 12:42:32Z the migrator served the Pump house a v3 bundle whose only port entry, the Light's DO1, stated
no direction (`{kind gpio, chip pinctrl-bcm2835, line 17, activeLow true}`). Agent 0.4.0 took it as an INPUT: at the
switch it wrote the line off and let go of it, then requested it as an input — the active-low relay energised on the
pulled-down line — and refused every setPower ("is an input on this node"). These tests replay the EXACT prod shapes
(Agal/contracts/programs/vectors/port-direction.json `prod`, a read-only export) through the agent's real ProgramHost,
HardwareIO and LineIO; only the kernel is faked, through LineIO's injectable opener (as tests/test_input_pull_debounce.py
does), with a BCM2835 line model: a closed output request falls back to an input (bcm2835_pmx_free), an undriven line
sits at its pull (GPIO 17: pull-down at power-on), and the relay board's IN energises the relay when it is LOW.

* the bundle served at 12:42:32Z is REFUSED (programAck rejected, in the node's words naming the card and the port) and
  the line is not touched: no request, no write, no close — the v1 program keeps driving it;
* a complete bundle (the entry the backend compiles since v1.15.3) switches with the Light's level unchanged — on stays
  on (LOW), off stays off (HIGH) — and the Light then switches on v3: on = LOW, off = HIGH;
* the rollback keeps the level too, and an output the v3 program did not hand over is driven again by v1's first pass;
* every other refusal of the entry (an unknown direction, an output with no active level, a PWM input), and an output
  port never read as an input.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agal_one_agent.blocks.clock import SimClock
from agal_one_agent.blocks.host import ProgramHost
from agal_one_agent.blocks.io import HardwareIO, PortRef
from agal_one_agent.blocks.runtime import CompileError, RecordingSink
from agal_one_agent.blocks.v3 import program as P3
from agal_one_agent.ports.gpiochip import ChipInfo, LineIO

import _contracts

TZ = ZoneInfo("Asia/Kolkata")
PATH, LABEL, LINE = "/dev/gpiochip0", "pinctrl-bcm2835", 17
VECTOR = os.path.join(_contracts.CONTRACTS, "programs", "vectors", "port-direction.json")


def prod() -> dict:
    with open(VECTOR) as f:
        return json.load(f)["prod"]


class BcmChip:
    """The kernel side of one BCM2835 bank, as far as a relay on GPIO 17 can tell."""

    def __init__(self):
        self.fsel = {LINE: "in"}      # power-on: an input
        self.level: dict[int, bool] = {}
        self.pull = {LINE: "down"}    # GPIO 9..27 power-on pull-down
        self.requests: list[tuple[int, str, object]] = []
        self.writes: list[tuple[int, bool]] = []
        self.closes: list[tuple[int, str]] = []

    def opener(self, path, line, direction, **kw):
        assert path == PATH
        bias = kw.get("bias")
        if direction in ("high", "low"):
            self.fsel[line], self.level[line] = "out", direction == "high"
        else:
            self.fsel[line] = "in"
            if bias == "pull_up":
                self.pull[line] = "up"
            elif bias == "pull_down":
                self.pull[line] = "down"
        self.requests.append((int(line), direction, bias))
        return _Handle(self, int(line), direction)

    def pin(self, line: int) -> bool:
        if self.fsel.get(line) == "out":
            return self.level[line]
        return self.pull.get(line) == "up"

    def driven(self, line: int = LINE) -> bool:
        return self.fsel.get(line) == "out"

    def relay_energised(self, line: int = LINE) -> bool:
        return not self.pin(line)  # an active-low board: IN LOW = energised

    def mark(self) -> tuple[int, int, int]:
        return len(self.requests), len(self.writes), len(self.closes)

    def since(self, m: tuple[int, int, int]) -> dict:
        return {"requests": self.requests[m[0]:], "writes": self.writes[m[1]:], "closes": self.closes[m[2]:]}


class _Handle:
    def __init__(self, chip: BcmChip, line: int, direction: str):
        self.chip, self.line, self.direction = chip, line, direction

    def read(self) -> bool:
        return self.chip.pin(self.line)

    def write(self, level: bool) -> None:
        self.chip.level[self.line] = bool(level)
        self.chip.writes.append((self.line, bool(level)))

    def close(self) -> None:
        if self.direction != "in":
            self.chip.fsel[self.line] = "in"  # bcm2835_pmx_free: "disable by setting to GPIO_IN"
        self.chip.closes.append((self.line, self.direction))


class Pi:
    """The Pump house Pi: the real host on the real hardware I/O, a faked chip."""

    def __init__(self, tmp_path):
        self.shape = prod()
        self.light = self.shape["assetId"]
        self.chip = BcmChip()
        lines = LineIO(chips=lambda: [ChipInfo(PATH, "gpiochip0", LABEL, 54)], opener=self.chip.opener)
        self.io = HardwareIO(configured_gpios=None, lines=lines)
        self.sink = RecordingSink()
        self.clock = SimClock(datetime(2026, 10, 17, 18, 10, tzinfo=TZ))  # 12:40Z: no run, nothing paused
        self.h = ProgramHost(self.io, self.sink, clock=self.clock, state_dir=str(tmp_path), node_uid=self.shape["nodeUid"])

    def v1(self, version: int) -> dict:
        b = copy.deepcopy(self.shape["v1Bundle"])
        b["version"] = version
        return b

    def served_1242(self) -> dict:
        return copy.deepcopy(self.shape["v3BundleServedAt1242"])

    def complete(self, version: int = 3) -> dict:
        """The bundle of 12:42:32Z with the entry the backend compiles since v1.15.3 (the vector's expectation)."""
        b = self.served_1242()
        b["version"] = version
        b["assets"][0]["ports"] = copy.deepcopy(self.shape["expect"]["ports"])
        return b

    def run(self, n: int) -> None:
        for _ in range(n):
            self.h.step()
            self.clock.advance(1.0)

    def power(self, value) -> dict:
        r = self.h.apply_command({"type": "setPower", "commandId": f"c{len(self.chip.writes)}{value}", "assetId": self.light,
                                  "sourceKey": "device.power", "value": value})
        self.run(2)
        return r


def test_the_vector_carries_the_prod_shapes_the_agent_replays():
    p = prod()
    served = p["v3BundleServedAt1242"]["assets"][0]["ports"]["device.power"]
    assert served == {"kind": "gpio", "chip": LABEL, "line": LINE, "activeLow": True}  # no direction: what 0.4.0 took as an input
    assert p["expect"]["ports"]["device.power"] == {"kind": "gpio", "chip": LABEL, "line": LINE, "direction": "out", "activeLow": True}
    assert p["v1Bundle"]["assets"][0]["ports"]["device.power"] == p["expect"]["ports"]["device.power"]
    assert p["nodeDoc"]["ports"][0]["transport"] == {"kind": "gpio", "chip": LABEL, "line": LINE}  # the port row states none


def test_the_bundle_served_at_1242_is_refused_and_the_line_is_not_touched(tmp_path):
    pi = Pi(tmp_path)
    pi.h.compile(pi.v1(2))
    pi.run(3)
    assert pi.chip.driven() and pi.chip.pin(LINE) is True and not pi.chip.relay_energised()  # v1: off = HIGH, driven
    assert pi.power(1)["accepted"]
    assert pi.chip.driven() and pi.chip.pin(LINE) is False and pi.chip.relay_energised()      # on = LOW
    m = pi.chip.mark()
    with pytest.raises(CompileError) as e:
        pi.h.compile(pi.served_1242())
    words = str(e.value)
    assert words.startswith("refused: device.power of card P9tZJr59FfqFy05d6qNy (line 17 of pinctrl-bcm2835) states no direction")
    pi.run(5)
    # Nothing touched: no request, no write, no close of line 17; v1 runs on, the Light on as it was.
    assert pi.chip.since(m) == {"requests": [], "writes": [], "closes": []}
    assert pi.h.language == 1 and pi.h.version == 2 and pi.h.pending_version is None
    assert pi.chip.driven() and pi.chip.pin(LINE) is False
    assert "output_released" not in [ev["type"] for ev in pi.sink.events]
    # …and the Light still switches, on v1.
    assert pi.power(0)["accepted"] and pi.chip.pin(LINE) is True and pi.chip.driven()


def test_the_pull_that_carries_the_refusal_acks_it_rejected_in_the_nodes_words(tmp_path):
    from agal_one_agent.blocks.sync import ProgramSync

    pi = Pi(tmp_path)
    pi.h.compile(pi.v1(2))
    pi.run(2)
    acks: list[tuple] = []

    class Http:
        def fetch_program(self, current_version=None):
            return {"version": 3, "bundle": pi.served_1242()}

        def report_program_ack(self, *a, **kw):
            acks.append(("http",) + a)

    class Store:
        import threading
        _lock = threading.Lock()
        saved: list = []

        def save(self, b):
            self.saved.append(b)

        def _save(self, b):
            self.saved.append(b)

        def load(self):
            return None

    sync = ProgramSync(pi.h, Store(), Http(), ack=lambda v, s, r: acks.append((v, s, r)))
    sync._pull()
    assert len(acks) == 1 and acks[0][0] == 3 and acks[0][1] == "rejected"
    assert "device.power of card P9tZJr59FfqFy05d6qNy (line 17 of pinctrl-bcm2835) states no direction" in acks[0][2]
    assert Store.saved == [] and pi.h.language == 1


def test_a_complete_bundle_switches_with_the_lights_level_unchanged_then_the_light_switches_on_v3(tmp_path):
    pi = Pi(tmp_path)
    pi.h.compile(pi.v1(2))
    pi.run(3)
    assert pi.power(1)["accepted"] and pi.chip.pin(LINE) is False   # left ON for the switch, as on the founder's Pi
    m = pi.chip.mark()
    pi.h.compile(pi.complete(3))
    pi.run(3)
    assert pi.h.language == 3 and pi.h.version == 3
    # Bumpless: line 17 is neither requested again, written nor closed; it stays driven LOW (ON).
    assert pi.chip.since(m) == {"requests": [], "writes": [], "closes": []}
    assert pi.chip.driven() and pi.chip.pin(LINE) is False and pi.chip.relay_energised()
    assert any(a["version"] == 3 and a["status"] == "applied" for a in pi.sink.acks)
    # On v3 the Light is an output: off = HIGH, on = LOW, off = HIGH — always driven, never read.
    for value, level in ((0, True), (1, False), (0, True)):
        r = pi.power(value)
        assert r["accepted"], r
        assert pi.chip.driven() and pi.chip.pin(LINE) is level
    assert all(d != "in" for line, d, _ in pi.chip.requests if line == LINE)


def test_the_rollback_keeps_the_level_and_v1_drives_the_light_again(tmp_path):
    pi = Pi(tmp_path)
    pi.h.compile(pi.v1(2))
    pi.run(3)
    pi.h.compile(pi.complete(3))
    pi.run(3)
    assert pi.power(1)["accepted"] and pi.chip.pin(LINE) is False
    m = pi.chip.mark()
    pi.h.compile(pi.v1(4))  # the rollback's v4
    pi.run(3)
    assert pi.h.language == 1 and pi.h.version == 4
    assert pi.chip.since(m) == {"requests": [], "writes": [], "closes": []} and pi.chip.pin(LINE) is False
    assert pi.power(0)["accepted"] and pi.chip.driven() and pi.chip.pin(LINE) is True
    assert pi.power(1)["accepted"] and pi.chip.driven() and pi.chip.pin(LINE) is False


def test_an_output_the_v3_program_did_not_hand_over_is_driven_again_by_v1s_first_pass(tmp_path):
    """README §7.5a: what a switch did not carry at its level is written by the new program's first pass. The Pump house
    after 12:49:30Z: v1 remembered False from before 12:42 and wrote nothing, while line 17 sat as an input (the relay
    energised, outputs_on() empty). Here the carried levels are empty, as they were."""
    pi = Pi(tmp_path)
    pi.h.compile(pi.v1(2))
    pi.run(3)
    assert (pi.light, "device.power") in pi.h.v1._last_written
    # What 0.4.0's v3 program had done to the line: let go of it, read it as an input.
    pi.io._lines.release(LABEL, LINE)
    pi.io._lines.read(LABEL, LINE, True)
    assert not pi.chip.driven() and pi.chip.relay_energised()
    pi.h.v1.stop(safe_state=False)
    pi.h.v1.take_over_language(pi.v1(4), {}, {})
    pi.h.active = pi.h.v1
    pi.run(2)
    assert pi.chip.driven() and pi.chip.pin(LINE) is True and not pi.chip.relay_energised()  # off, driven HIGH again


@pytest.mark.parametrize("entry, words", [
    ({"kind": "gpio", "chip": LABEL, "line": LINE, "direction": "both", "activeLow": True}, "states direction 'both', which is neither in nor out"),
    ({"kind": "gpio", "chip": LABEL, "line": LINE, "direction": "out"}, "is an output that states no active level"),
    ({"kind": "i2c", "busId": 1, "addr": 32, "channel": 0, "driver": "pcf8574", "direction": "out"}, "is an output that states no active level"),
    ({"kind": "i2c", "busId": 1, "addr": 32, "channel": 0, "driver": "pcf8574", "activeLow": True}, "states no direction"),
    ({"kind": "pwm", "gpioNumber": 18, "frequencyHz": 1000, "dutyCyclePct": 0, "direction": "in"}, "is a PWM port, which is an output"),
])
def test_an_incomplete_entry_is_refused_in_the_nodes_words(tmp_path, entry, words):
    pi = Pi(tmp_path)
    b = pi.served_1242()
    b["assets"][0]["ports"]["device.power"] = entry
    with pytest.raises(CompileError, match=words):
        P3.build(b, pi.io)
    assert P3.entry_refusal("a", "device.power", entry) is not None


def test_a_complete_entry_passes_and_a_pwm_entry_may_leave_its_direction_out():
    assert P3.entry_refusal("a", "device.power", {"kind": "gpio", "chip": LABEL, "line": LINE, "direction": "out", "activeLow": True}) is None
    assert P3.entry_refusal("a", "sensor.flow", {"kind": "gpio", "chip": LABEL, "line": 5, "direction": "in"}) is None
    assert P3.entry_refusal("a", "device.dim", {"kind": "pwm", "gpioNumber": 18, "frequencyHz": 1000, "dutyCyclePct": 0}) is None


def test_an_output_port_is_never_opened_as_an_input(tmp_path):
    pi = Pi(tmp_path)
    port = PortRef(pi.light, "device.power", {"kind": "gpio", "chip": LABEL, "line": LINE, "direction": "out", "activeLow": True})
    pi.io.write_output(port, False)  # off: driven HIGH
    m = pi.chip.mark()
    assert pi.io.read_input(port) is None
    assert pi.chip.since(m) == {"requests": [], "writes": [], "closes": []} and pi.chip.driven() and pi.chip.pin(LINE) is True
