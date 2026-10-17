"""Agent 0.4.1 (contracts v1.15.4 — the review of v1.15.3): no reload, rollback or port test lets go of a driven line, and
a line changes its use in two bundles.

The Pump house's relay board (active low, on GPIO 17 with its power-on pull-down) energises the moment line 17 is let go
of. The review of v1.15.3 found three more ways to get there than the switch of language itself:

* a LANGUAGE-1 reload that drops the Light (unlinked or deleted) — 0.3.4's runtime wrote the line off and let go of it;
  the Pump house runs v1 today. Since 0.4.1 the line is written off and KEPT driven (programs/README.md §4.8);
* a port test of a line the node keeps driven at off — the pulse ended by letting go of it. Since 0.4.1 a tested line is
  left as it was found;
* (regression) a language-3 reload that drops the Light, and a rollback whose v1 program does not drive a v3 output.

And a line that changes its use — the installer swaps the Light's relay and its wall switch between line 17 and line 27
(vector rewire-swap.json `lineRoleSwap`): in ONE bundle 0.4.1 refuses it (it would let go of a driven line to read it)
and keeps driving line 17; in TWO bundles — the Light unlinked first, then linked the other way — it goes through. The
backend writes no such one-step bundle (README §7.2 *A line keeps its role*); the node's refusal stays the last line.

The real ProgramHost / HardwareIO / LineIO on the BCM2835 line model of tests/test_v3_port_direction.py.
"""

from __future__ import annotations

import copy
import json
import os

import pytest

import test_v3_port_direction as T
from agal_one_agent.blocks.runtime import CompileError
from agal_one_agent.ports.gpiochip import ChipInfo, LineIO
from agal_one_agent.ports.port_test import run_port_test

import _contracts

LINE, LABEL = T.LINE, T.LABEL
SWAP = os.path.join(_contracts.CONTRACTS, "programs", "vectors", "rewire-swap.json")


def _migrated(tmp_path, light_on: bool) -> T.Pi:
    pi = T.Pi(tmp_path)
    pi.h.compile(pi.v1(2))
    pi.run(3)
    assert pi.power(1 if light_on else 0)["accepted"]
    pi.h.compile(pi.complete(3))  # the bundle the backend compiles since v1.15.3
    pi.run(3)
    assert pi.h.language == 3
    return pi


@pytest.mark.parametrize("light_on", [False, True])
def test_a_v1_reload_that_drops_the_light_keeps_line_17_driven_off(tmp_path, light_on):
    """The Pump house runs v1 (v4). The Light card is unlinked or deleted: the backend recompiles v1 without it."""
    pi = T.Pi(tmp_path)
    pi.h.compile(pi.v1(4))
    pi.run(3)
    assert pi.power(1 if light_on else 0)["accepted"]
    assert pi.chip.relay_energised() is light_on
    m = pi.chip.mark()
    b = pi.v1(5)
    b["assets"] = []
    pi.h.compile(b)
    pi.run(3)
    assert pi.chip.driven() and not pi.chip.relay_energised(), "a v1 reload let go of line 17: the active-low relay energised"
    assert pi.chip.since(m)["closes"] == []
    assert pi.chip.pin(LINE) is True  # off = HIGH, still driven
    assert "output_released" in [e["type"] for e in pi.sink.events]


@pytest.mark.parametrize("light_on", [False, True])
def test_a_v3_reload_that_drops_the_light_keeps_line_17_driven_off(tmp_path, light_on):
    pi = _migrated(tmp_path, light_on)
    m = pi.chip.mark()
    b = pi.complete(4)
    b["assets"] = []
    pi.h.compile(b)
    pi.run(3)
    assert pi.chip.driven() and not pi.chip.relay_energised()
    assert pi.chip.since(m)["closes"] == []


def test_a_rollback_whose_v1_program_does_not_drive_the_light_keeps_line_17_driven_off(tmp_path):
    pi = _migrated(tmp_path, True)
    v1 = pi.v1(4)
    v1["assets"][0]["aab"]["variables"] = [v for v in v1["assets"][0]["aab"]["variables"] if v.get("kind") not in ("node", "ui")]
    v1["assets"][0]["ports"] = {}
    pi.h.compile(v1)
    pi.run(3)
    assert pi.h.language == 1
    assert pi.chip.driven() and not pi.chip.relay_energised()


# ------------------------------------------------------------------ a port test leaves a line as it found it

def _lines():
    chip = T.BcmChip()
    return chip, LineIO(chips=lambda: [ChipInfo(T.PATH, "gpiochip0", LABEL, 54)], opener=chip.opener)


def _test(lines, action: str):
    return run_port_test({"portId": "DO1", "action": action, "seconds": 1,
                          "transport": {"kind": "gpio", "chip": LABEL, "line": LINE, "activeLow": True}},
                         lines, busy=lambda label, line: False, sleep=lambda s: None)


@pytest.mark.parametrize("action", ["pulse", "read"])
def test_a_port_test_of_a_line_held_driven_at_off_keeps_it_driven(action):
    chip, lines = _lines()
    lines.write(LABEL, LINE, False, active_low=True)  # what a dropped output leaves: driven at off (HIGH)
    assert chip.driven() and not chip.relay_energised()
    r = _test(lines, action)
    assert r["result"] == "passed", r
    assert lines.held_direction(LABEL, LINE) == "out"
    assert chip.driven() and chip.pin(LINE) is True and not chip.relay_energised()


def test_a_port_test_of_a_free_line_lets_go_of_it_as_before():
    chip, lines = _lines()
    r = _test(lines, "pulse")
    assert r["result"] == "passed" and lines.held_direction(LABEL, LINE) is None
    assert chip.closes and chip.closes[-1][0] == LINE


# ------------------------------------------------------------------ a line changes its use in two bundles

def _swap() -> dict:
    with open(SWAP) as f:
        return json.load(f)["lineRoleSwap"]


def _bundle(assets, version: int) -> dict:
    return {"language": 3, "version": version, "compiledAt": "2026-10-17T13:00:00Z", "timezone": "Asia/Kolkata",
            "assets": copy.deepcopy(assets), "rules": [], "instruction": [], "settings": {"resumeAfterPowerSeconds": 30},
            "handover": {"releases": []}}


def test_the_swap_of_two_lines_is_refused_in_one_bundle_and_taken_in_two(tmp_path):
    vec = _swap()
    a = vec["expect"]["assets"]
    light = a["running"][0]["assetId"]
    pi = T.Pi(tmp_path)
    pi.light = light
    pi.h.compile(_bundle(a["running"], 3))
    pi.run(3)
    assert pi.h.language == 3 and pi.chip.driven(LINE) and pi.chip.pin(LINE) is True  # the relay off = HIGH, driven
    assert not pi.chip.driven(27)                                                       # the wall switch, read

    # ONE bundle: line 17 driven → read and line 27 read → driven. Refused; the running program stays, 17 still driven.
    m = pi.chip.mark()
    with pytest.raises(CompileError) as e:
        pi.h.compile(_bundle(a["swapped"], 4))
    assert "line 17 of pinctrl-bcm2835" in str(e.value) and "would read it as an input" in str(e.value)
    pi.run(2)
    assert pi.h.version == 3 and pi.chip.driven(LINE) and pi.chip.since(m)["closes"] == []

    # TWO bundles. First the Light unlinked: both lines dropped — 17 kept driven at off, 27 no longer read.
    pi.h.compile(_bundle(a["unlinked"], 4))
    pi.run(3)
    assert pi.h.version == 4 and pi.chip.driven(LINE) and not pi.chip.relay_energised(LINE)
    assert pi.chip.since(m)["closes"] == []
    # …then linked the other way: taken. Line 27 now drives the relay (active high: off = LOW), line 17 reads the switch.
    pi.h.compile(_bundle(a["swapped"], 5))
    pi.run(3)
    assert pi.h.version == 5 and pi.h.language == 3
    assert pi.chip.driven(27) and pi.chip.pin(27) is False
    assert not pi.chip.driven(LINE)
    assert any(line == LINE and d == "in" for line, d, _ in pi.chip.since(m)["requests"])  # requested as the switch's input
    r = pi.h.apply_command({"type": "setPower", "commandId": "swap-on", "assetId": light, "sourceKey": "device.power", "value": 1})
    pi.run(2)
    assert r["accepted"], r
    assert pi.chip.driven(27) and pi.chip.pin(27) is True  # on = HIGH on the active-high line 27
