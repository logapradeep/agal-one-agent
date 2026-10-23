"""The node's clock: its source and its offset from the backend's time (agent 0.4.4, contracts v1.26.0; nodes/README.md
§14; ADR-031 §6 — "each node reports its clock source and offset").

Replays Agal/contracts/nodes/vectors/clock.json: what ``timedatectl show`` and ``systemctl is-active`` print and what is
read from them, the clock source, and the offset from the Date of the backend's answers across a steady node, a node a
minute behind, a wall clock that steps, and more than 15 answers. Then the clock-care thread with its programs faked,
ServerTime's samples, the heartbeat's block (what the contract's storedClock keeps) and main()'s wiring.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from email.utils import formatdate

import pytest

import _contracts
from agal_one_agent import clock_care
from agal_one_agent.clock_care import ClockCare, parse_timedatectl_show, service_of, source_of, with_clock
from agal_one_agent.net_care import ToolHung, ToolTimeout
from agal_one_agent.server_time import ServerTime, add_sample, offset_of

VECTORS = os.path.join(_contracts.CONTRACTS, "nodes", "vectors", "clock.json")


@pytest.fixture(scope="module")
def vec():
    if not os.path.exists(VECTORS):
        pytest.skip(f"clock vectors not found at {VECTORS}")
    with open(VECTORS) as f:
        return json.load(f)


# ------------------------------------------------------------------------------------------ the vectors

def test_the_units_asked_about_are_the_contracts(vec):
    assert [u for u, _ in clock_care.UNITS] == vec["units"]


def test_timedatectl_show_as_the_reference_reads_it(vec):
    for c in vec["timedatectl"]:
        assert parse_timedatectl_show(c["text"]) == c["expect"], c["name"]


def test_systemctl_is_active_as_the_reference_reads_it(vec):
    for c in vec["isActive"]:
        assert service_of(c["text"]) == c["expect"], c["name"]


def test_the_source_as_the_reference_says(vec):
    for c in vec["sources"]:
        assert source_of(c["ntpSynchronized"], c["rtc"]) == c["expect"], c


def test_the_offset_as_the_reference_computes_it(vec):
    for s in vec["samples"]:
        history: list = []
        for o in s["observations"]:
            history = add_sample(history, o["wallMs"], o["monoMs"], o["serverMs"])
            assert offset_of(history) == o["expect"], (s["name"], o)


# ------------------------------------------------------------------------------------------ ServerTime

def test_every_https_answer_is_a_sample_of_the_wall_clocks_offset():
    mono = [1000.0]
    wall = [1792729800000.0 + 61_200.0]  # the node a minute ahead (and the 0.7 s of the answer's second)
    st = ServerTime(monotonic=lambda: mono[0], wall_ms=lambda: wall[0])
    assert st.offset() is None
    date = formatdate(1792729800, usegmt=True)  # 2026-10-23T04:30:00Z
    assert st.observe({"Date": date}, "https://ingress.example/x")
    off = st.offset()
    assert off == {"offsetMs": 60_700, "samples": 1, "atMs": 1792729800500.0}
    assert not st.observe({"Date": date}, "http://not-https.example/")  # never from an answer that is not HTTPS
    assert st.offset()["samples"] == 1
    # The wall clock steps back a minute (NTP): the history restarts from the next sample.
    mono[0] += 30.0
    wall[0] = 1792729830000.0 + 400.0
    st.observe({"Date": formatdate(1792729830, usegmt=True)}, "https://ingress.example/x")
    assert st.offset() == {"offsetMs": -100, "samples": 1, "atMs": 1792729830500.0}


# ------------------------------------------------------------------------------------------ the thread

PI_SYNCED = "Timezone=Asia/Kolkata\nLocalRTC=no\nCanNTP=yes\nNTP=yes\nNTPSynchronized=yes\n"
TIMESYNCD = "active\ninactive\ninactive\ninactive\ninactive\ninactive\n"


class Runner:
    def __init__(self, outputs: dict):
        self.outputs = outputs
        self.calls: list = []

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout, threading.current_thread().name))
        out = self.outputs[os.path.basename(argv[0])]
        if isinstance(out, Exception):
            raise out
        return 0, out


def _care(runner, *, rtc=False, server_time=None, platform="linux", now=1792729900000.0):
    return ClockCare(server_time=server_time, now_ms=lambda: now, run=runner, find=lambda n: f"/usr/bin/{n}",
                     exists=lambda p: rtc and p == "/dev/rtc0", platform=platform)


def test_a_pi_synchronised_by_timesyncd():
    r = Runner({"timedatectl": PI_SYNCED, "systemctl": TIMESYNCD})
    care = _care(r)
    assert care.status() is None, "nothing before the first reading"
    care.step()
    assert care.status() == {"source": "ntp", "service": "timesyncd", "rtc": False, "at": "2026-10-23T04:31:40.000Z"}
    assert [c[0] for c in r.calls] == [["/usr/bin/timedatectl", "show"],
                                       ["/usr/bin/systemctl", "is-active", *[u for u, _ in clock_care.UNITS]]]
    assert all(c[1] == clock_care.TOOL_TIMEOUT_S for c in r.calls)


def test_not_synchronised_with_an_rtc_and_the_offset_from_the_backend():
    st = ServerTime(monotonic=lambda: 50.0, wall_ms=lambda: 1792729800000.0 - 2_000.0)
    st.observe({"Date": formatdate(1792729800, usegmt=True)}, "https://ingress.example/x")
    r = Runner({"timedatectl": PI_SYNCED.replace("NTPSynchronized=yes", "NTPSynchronized=no"), "systemctl": TIMESYNCD})
    care = _care(r, rtc=True, server_time=st)
    care.step()
    assert care.status() == {"source": "rtc", "service": "timesyncd", "rtc": True, "offsetMs": -2500, "offsetSamples": 1,
                             "offsetAt": "2026-10-23T04:30:00.500Z", "at": "2026-10-23T04:31:40.000Z"}


def test_programs_that_cannot_be_read_are_unknown_never_a_failure():
    r = Runner({"timedatectl": ToolTimeout("timedatectl did not end in 3 s (killed)"), "systemctl": OSError("no such file")})
    care = _care(r)
    care.step()
    assert care.status()["source"] == "unknown" and care.status()["service"] == "unknown"


def test_off_linux_nothing_is_run_and_the_source_is_unknown():
    r = Runner({})
    care = _care(r, platform="darwin")
    care.step()
    assert r.calls == [] and care.status() == {"source": "unknown", "service": "unknown", "rtc": False, "at": "2026-10-23T04:31:40.000Z"}


def test_no_program_is_started_while_one_that_would_not_die_lives():
    class Stuck:
        def poll(self):
            return None

    hung = ToolHung("timedatectl did not end in 3 s and did not die when killed", Stuck())
    r = Runner({"timedatectl": hung, "systemctl": TIMESYNCD})
    care = _care(r)
    care.step()
    assert [os.path.basename(c[0][0]) for c in r.calls] == ["timedatectl"], "systemctl was not started next to it"
    care.step()
    assert [os.path.basename(c[0][0]) for c in r.calls] == ["timedatectl"]
    assert care.status()["source"] == "unknown"


def test_the_thread_reads_on_its_own_never_the_callers():
    r = Runner({"timedatectl": PI_SYNCED, "systemctl": TIMESYNCD})
    care = ClockCare(now_ms=lambda: 0.0, run=r, find=lambda n: f"/usr/bin/{n}", exists=lambda p: False, platform="linux", check_s=0.05)
    care.start()
    deadline = time.monotonic() + 3
    while not r.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    care.stop()
    assert r.calls and {c[2] for c in r.calls} == {clock_care.THREAD}


def test_with_clock_adds_the_block_when_there_is_one():
    assert with_clock({"capabilities": {}}, None) == {"capabilities": {}}
    care = _care(Runner({"timedatectl": PI_SYNCED, "systemctl": TIMESYNCD}))
    assert with_clock({"a": 1}, care) == {"a": 1}
    care.step()
    assert with_clock({"a": 1}, care)["clock"]["source"] == "ntp"
    assert clock_care.wire_clock(offline=True) is None


def test_the_block_is_what_the_contracts_keeper_keeps():
    """The heartbeat's block survives nodes/reference/clock.mjs storedClock whole (the backend's twin)."""
    if not shutil.which("node"):
        pytest.skip("node not installed")
    st = ServerTime(monotonic=lambda: 50.0, wall_ms=lambda: 1792729800000.0 + 1_300.0)
    st.observe({"Date": formatdate(1792729800, usegmt=True)}, "https://ingress.example/x")
    care = _care(Runner({"timedatectl": PI_SYNCED, "systemctl": TIMESYNCD}), server_time=st)
    care.step()
    block = care.status()
    ref = os.path.join(_contracts.CONTRACTS, "nodes", "reference", "clock.mjs")
    code = ("const m = await import(process.argv[1]); const b = JSON.parse(process.argv[2]);"
            "console.log(JSON.stringify(m.storedClock(b, '2026-10-23T05:00:00.000Z')));")
    r = subprocess.run(["node", "--input-type=module", "-e", code, ref, json.dumps(block)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == dict(block, reportedAt="2026-10-23T05:00:00.000Z")


# ------------------------------------------------------------------------------------------ main()

class _WiredHere(Exception):
    pass


@pytest.mark.parametrize("offline", [False, True])
def test_main_wires_the_clock_with_the_backends_time(tmp_path, monkeypatch, offline):
    from agal_one_agent import main as agent_main
    from test_command_acks import _node_config
    seen = {}

    def wire(*, offline, server_time=None, now_ms=None):
        seen.update(offline=offline, server_time=server_time, now_ms=now_ms)
        raise _WiredHere

    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(clock_care, "wire_clock", wire)
    monkeypatch.setattr("sys.argv", ["agal-one-agent", "--config", _node_config(tmp_path)] + (["--offline"] if offline else []))
    with pytest.raises(_WiredHere):
        agent_main.main()
    assert seen["offline"] is offline and isinstance(seen["server_time"], ServerTime) and isinstance(seen["now_ms"](), float)
