"""An update that waits says so (agent 0.4.4, contracts v1.24.0; nodes/README.md §13; _audit/107 follow-up 32).

On 2026-10-23 the Pump house's update to 0.4.3 waited seven minutes for Mtr1 to go off and the node said nothing but
"updating" — the founder thought it was stuck. Agent 0.4.4's update thread keeps a record of its wait (wait_report): the
heartbeat's ``otaWait`` — phase ``waiting_idle`` with the cards that are on, since, until — and, once the 30 minutes run
out, phase ``busy`` with what stayed on, until the next update starts. One heartbeat is asked for at once when the wait
begins, when what is on changes and when it ends busy. The block is what the contract's storedOtaWait keeps.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time

import pytest

import _contracts
from agal_one_agent import main as agent_main
from agal_one_agent import ota_updater
from agal_one_agent.heartbeat import HeartbeatPublisher
from test_ota_rollback import _phases, _update, ota  # noqa: F401 — the fixture

PUMP = {"name": "Borewell pump", "assetId": "asset_pump0001"}
V1 = {"name": "Valve 1", "assetId": "asset_valve001"}


@pytest.fixture(autouse=True)
def _no_wait_left():
    ota_updater._set_wait(None, None)
    yield
    ota_updater._set_wait(None, None)


class Nudges:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1


def test_a_wait_says_what_is_on_until_it_is_off_then_says_nothing(ota):  # noqa: F811
    on = {"n": 0}
    seen = []

    def outputs_on():
        on["n"] += 1
        seen.append(ota_updater.wait_report())
        return ["Borewell pump (device.power)"] if on["n"] <= 3 else []

    nudges = Nudges()
    _update(ota, outputs_on=outputs_on, outputs_report=lambda: {"on": [PUMP], "runs": 0}, on_wait_change=nudges,
            now_ms=lambda: 1792729800000.0)
    w = seen[1]  # read while it waited
    assert w["phase"] == "waiting_idle" and w["on"] == [PUMP] and w["runs"] == 0
    assert w["since"] == "2026-10-23T04:30:00.000Z" and w["until"] == "2026-10-23T04:40:00.000Z"  # BUSY_WAIT_S 600 here
    assert ota_updater.wait_report() is None, "the outputs went off: the wait is over"
    assert nudges.n == 1, "one heartbeat at once when it began; none for the same things on"
    assert _phases(ota) == ["started"] and ota.restarts == 1


def test_a_change_of_what_is_on_asks_for_a_heartbeat(ota):  # noqa: F811
    reports = iter([{"on": [PUMP], "runs": 1}, {"on": [PUMP, V1], "runs": 1}, {"on": [V1], "runs": 0}])
    last = {"r": {"on": [V1], "runs": 0}}

    def report():
        last["r"] = next(reports, last["r"])
        return last["r"]

    calls = {"n": 0}

    def outputs_on():
        calls["n"] += 1
        return ["x"] if calls["n"] <= 3 else []

    nudges = Nudges()
    ota.now = 100.0
    _update(ota, outputs_on=outputs_on, outputs_report=report, on_wait_change=nudges)
    # began (forced) + two changes 10 s apart (≥ 5 s: each asks); the end of the wait asks for nothing.
    assert nudges.n == 3


def test_a_wait_that_runs_out_is_busy_until_the_next_update(ota):  # noqa: F811
    nudges = Nudges()
    _update(ota, outputs_on=lambda: ["Borewell pump (device.power)"], outputs_report=lambda: {"on": [PUMP], "runs": 0},
            on_wait_change=nudges, now_ms=lambda: 1792729800000.0)
    w = ota_updater.wait_report()
    assert w["phase"] == "busy" and w["on"] == [PUMP] and w["endedAt"] == w["at"] and "until" not in w
    st = ota_updater.read_state()
    assert st["phase"] == "failed" and st["error"].startswith("busy: Borewell pump")
    assert _phases(ota) == ["failed"]
    assert nudges.n == 2, "when it began and when it ran out"
    # The next update clears it at once (here it updates: nothing on).
    _update(ota, tag="v0.3.5", outputs_on=lambda: [])
    assert ota_updater.wait_report() is None


def test_the_second_wait_after_the_install_says_so_too(ota):  # noqa: F811
    seq = iter([[], ["Valve 1 (valve open)"], []])
    seen = []

    def outputs_on():
        v = next(seq, [])
        seen.append(ota_updater.wait_report())
        return v

    _update(ota, outputs_on=outputs_on, outputs_report=lambda: {"on": [V1], "runs": 0})
    assert seen[2]["phase"] == "waiting_idle" and seen[2]["on"] == [V1]
    assert ota.restarts == 1 and ota_updater.wait_report() is None


def test_no_report_given_names_nothing_but_still_says_it_waits(ota):  # noqa: F811
    calls = {"n": 0}

    def outputs_on():
        calls["n"] += 1
        return ["x"] if calls["n"] <= 2 else []

    seen = []
    orig = ota_updater._sleep

    def sleep(s):
        seen.append(ota_updater.wait_report())
        orig(s)

    ota_updater._sleep = sleep
    try:
        _update(ota, outputs_on=outputs_on)
    finally:
        ota_updater._sleep = orig
    assert seen[0]["phase"] == "waiting_idle" and seen[0]["on"] == [] and seen[0]["runs"] == 0


def test_the_report_is_cleaned_as_the_contract_keeps_it():
    long = "N" * 100
    on, runs = ota_updater._wait_items(lambda: {"on": [PUMP, PUMP, {"name": "  "}, "x", {"name": long, "assetId": ""},
                                                       *({"name": f"V{i}", "assetId": f"a{i}"} for i in range(30))], "runs": 99})
    assert on[0] == PUMP and on[1] == {"name": "N" * 80} and len(on) == 16 and runs == 64
    assert ota_updater._wait_items(lambda: 1 / 0) == ([], 0)


def test_the_heartbeat_carries_it_while_it_waits():
    assert agent_main.with_ota_wait({"capabilities": {}}) == {"capabilities": {}}
    ota_updater._set_wait({"phase": "waiting_idle", "on": [PUMP], "runs": 0, "since": "s", "until": "u", "at": "a"}, None)
    assert agent_main.with_ota_wait({"capabilities": {}})["otaWait"]["on"] == [PUMP]
    assert agent_main.with_ota_wait(None)["otaWait"]["phase"] == "waiting_idle"


def test_the_block_is_what_the_contracts_keeper_keeps(ota):  # noqa: F811
    """The block the agent sends survives nodes/reference/ota.mjs storedOtaWait whole (the backend's twin)."""
    if not shutil.which("node"):
        pytest.skip("node not installed")
    _update(ota, outputs_on=lambda: ["Borewell pump (device.power)"], outputs_report=lambda: {"on": [PUMP, {"name": "Light"}], "runs": 1},
            now_ms=lambda: 1792729800000.0)
    block = ota_updater.wait_report()
    ref = os.path.join(_contracts.CONTRACTS, "nodes", "reference", "ota.mjs")
    code = ("const m = await import(process.argv[1]); const b = JSON.parse(process.argv[2]);"
            "console.log(JSON.stringify(m.storedOtaWait(b, '2026-10-23T05:00:00.000Z')));")
    r = subprocess.run(["node", "--input-type=module", "-e", code, ref, json.dumps(block)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    kept = json.loads(r.stdout)
    assert kept == dict(block, reportedAt="2026-10-23T05:00:00.000Z")
    words = ("const m = await import(process.argv[1]); console.log(JSON.stringify(m.otaWaitLine('failed', JSON.parse(process.argv[2]))));")
    r = subprocess.run(["node", "--input-type=module", "-e", words, ref, json.dumps(kept)], capture_output=True, text=True, timeout=60)
    assert json.loads(r.stdout) == "The update stopped: Borewell pump and Light stayed on. Switch them off, then press Update again."


def test_outputs_report_for_names_the_cards_that_are_on_once():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from agal_one_agent.blocks.clock import SimClock
    from agal_one_agent.blocks.host import ProgramHost
    from agal_one_agent.blocks.io import SimulatedIO
    from agal_one_agent.blocks.runtime import RecordingSink
    from agal_one_agent.blocks.v3.runtime import STATE_FILE
    from v3_bench import CLEAN, HEALTHY, pumphouse
    import tempfile

    io = SimulatedIO()
    for (aid, sk), v in HEALTHY.items():
        io.set_input(aid, sk, v)
    clock = SimClock(datetime(2026, 10, 15, 5, 59, 58, tzinfo=ZoneInfo("Asia/Kolkata")))
    state_dir = tempfile.mkdtemp()
    with open(os.path.join(state_dir, STATE_FILE), "w") as f:
        json.dump(CLEAN, f)  # a clean stop with Valve 2 closed: no FAULT, rule 1 runs the pump
    host = ProgramHost(io, RecordingSink(), clock=clock, state_dir=state_dir, node_uid="node_pumphouse")
    host.compile(pumphouse())
    host.step()
    assert agent_main.outputs_report_for(host) == {"on": [], "runs": 0}
    for _ in range(10):  # 06:00:08 — the morning run: Valve 1 open, the pump on
        clock.advance(1.0)
        host.step()
    r = agent_main.outputs_report_for(host)
    assert r == {"on": [{"name": "Borewell pump", "assetId": "asset_pump0001"}, {"name": "Valve 1", "assetId": "asset_valve001"}],
                 "runs": 1}
    assert agent_main.outputs_report_for(None) == {"on": [], "runs": 0}


def test_a_nudge_sends_one_heartbeat_now_and_keeps_one_timer():
    sent = []

    class Mqtt:
        def publish_status(self, **kw):
            sent.append(kw)

    class Config:
        class telemetry:
            heartbeat_seconds = 3600

    hb = HeartbeatPublisher(Config(), Mqtt(), http_reporter=None, agent_version="0.4.4")
    assert hb.nudge() is False, "not running: nothing"
    hb.start()
    assert len(sent) == 1
    first = hb._timer
    assert hb.nudge() is True
    deadline = time.monotonic() + 3
    while len(sent) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(sent) == 2
    time.sleep(0.05)
    assert hb._timer is not first and not first.is_alive()
    assert sum(1 for t in threading.enumerate() if isinstance(t, threading.Timer) and t.is_alive()) >= 1
    hb.stop()
    assert hb._timer is None
