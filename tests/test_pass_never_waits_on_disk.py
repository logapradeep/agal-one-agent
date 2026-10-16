"""A pass never waits on the disk, nor on a clock probe; a report keeps its pass's time (the second agent 0.4.0 review;
programs/README.md §4.8).

The review simulated a slow SD card (``os.fsync`` sleeping first) and a slow clock probe (``timedatectl`` over D-Bus,
on a board with no RTC) under the real ProgramHost and RuntimeV3 on their own threads, with the agent's default pass
period (1 s) and no network at all:

* every pass that changed what the node must remember (a switch-on by command, an E-stop pressed, a guard trip, a run's
  start or end) wrote the v3 state file and fsynced it on the runtime thread, holding the runtime lock — with a 1 s
  fsync an E-stop pressed just after a ``setPower`` stopped the pump after 0.98 s, an over-current with a 1 s hold
  after 1.98 s, and a command waited 0.99 s;
* ``SystemClock.clock_ok()`` ran ``timedatectl`` on the caller's thread once a minute — the runtimes ask under the lock
  on every pass — so an E-stop pressed during a 1 s probe stopped the pump after 0.97 s;
* the outbox stamped an alert or a ``programAck`` when its post went out, behind the posts ahead of it, not when its
  pass raised it (the backend files the alert's event, and ``program.appliedAt``, at that time).

Each test here fails on 36c366a.
"""

from __future__ import annotations

import json
import os
import threading
import time

from test_pass_never_waits_on_network import ESTOP, LIGHT, NODE, PUMP, _GatedHttp, _Mqtt, safety_bundle, set_power, wait
from agal_one_agent.blocks import clock as clock_mod
from agal_one_agent.blocks.clock import SystemClock
from agal_one_agent.blocks.cloud import CloudSink
from agal_one_agent.blocks.host import ProgramHost
from agal_one_agent.blocks.io import SimulatedIO
from agal_one_agent.blocks.runtime import RecordingSink
from agal_one_agent.blocks.v3 import runtime as v3rt
from agal_one_agent.blocks.v3.runtime import STATE_FILE, STATE_WRITER_THREAD, RuntimeV3

PASS_S = 1.0
SLOW_FSYNC_S = 1.0


def _slow_fsync(monkeypatch, seconds: float) -> list[str]:
    """Every fsync of the process sleeps first (a slow SD card); returns the names of the threads that fsynced."""
    real = os.fsync
    threads: list[str] = []

    def slow(fd):
        threads.append(threading.current_thread().name)
        time.sleep(seconds)
        return real(fd)
    monkeypatch.setattr(os, "fsync", slow)
    return threads


def _safety_host(tmp_path, clock=None) -> tuple[ProgramHost, SimulatedIO]:
    io = SimulatedIO()
    io.set_input(ESTOP, "sensor.contact", True)  # the nc contact closed: released
    io.set_input(ESTOP, "sensor.reset", False)
    io.set_input(PUMP, "sensor.current", 0.0)
    h = ProgramHost(io, RecordingSink(), clock=clock or SystemClock(assume_ok=True), state_dir=str(tmp_path),
                    tick_seconds=PASS_S, node_uid=NODE)
    h.compile(safety_bundle(7))
    h.start()
    return h, io


# ------------------------------------------------------------------------------------------------- the disk

def test_v3_the_estop_the_guard_and_commands_never_wait_for_a_slow_sd_card(tmp_path, monkeypatch):
    fsyncs = _slow_fsync(monkeypatch, SLOW_FSYNC_S)
    h, io = _safety_host(tmp_path)
    pump = lambda: io.output(PUMP, "device.power")  # noqa: E731
    try:
        time.sleep(PASS_S + 2 * SLOW_FSYNC_S)  # the first passes and their records are over
        # 1. A switch-on by command: its pass records it (a manual on is remembered) — and the E-stop is pressed 20 ms
        #    after the pump came on, while that record is being fsynced.
        t0 = time.monotonic()
        assert set_power(h, PUMP, 1)["accepted"]
        assert time.monotonic() - t0 < 0.2
        wait(lambda: pump() is True, timeout=PASS_S)
        time.sleep(0.02)
        io.set_input(ESTOP, "sensor.contact", False)
        took = wait(lambda: pump() is False, timeout=10.0)
        assert took < 0.3, f"the E-stop stopped the pump {took:.2f} s after it was pressed (fsync {SLOW_FSYNC_S} s)"
        # 2. A command 20 ms after the E-stop's pass (which records the latch).
        time.sleep(0.02)
        t0 = time.monotonic()
        set_power(h, LIGHT, 1)
        assert time.monotonic() - t0 < 0.3, "a command waited for the SD card"
        # The panel: released, then reset.
        io.set_input(ESTOP, "sensor.contact", True)
        time.sleep(0.05)
        io.set_input(ESTOP, "sensor.reset", True)
        time.sleep(0.05)
        io.set_input(ESTOP, "sensor.reset", False)
        wait(lambda: not h.v3.prog.estop.latched, timeout=3 * PASS_S + 2 * SLOW_FSYNC_S)
        time.sleep(PASS_S)
        # 3. Over-current 20 ms after a switch-on (hold 1 s): off within the hold and one pass.
        assert set_power(h, PUMP, 1)["accepted"]
        wait(lambda: pump() is True, timeout=PASS_S + 0.3)
        time.sleep(0.02)
        io.set_input(PUMP, "sensor.current", 10.0)
        took = wait(lambda: pump() is False, timeout=10.0)
        assert took < 1.0 + 0.3, f"the guard tripped {took:.2f} s after the over-current (hold 1 s)"
        assert h.v3.prog.guards[PUMP].tripped
    finally:
        h.stop(safe_state=False)
    assert fsyncs and set(fsyncs) == {STATE_WRITER_THREAD}, f"fsync on {sorted(set(fsyncs))}"
    # The clean stop waited for its record: a restart reads it.
    with open(os.path.join(str(tmp_path), STATE_FILE)) as f:
        saved = json.load(f)
    assert saved["cleanStop"] is True and saved["estop"]["latched"] is False and saved["guards"][PUMP]["tripped"] is True


def test_the_state_records_land_in_the_order_they_were_made_the_newest_wins_and_a_read_waits(tmp_path, monkeypatch):
    gate = threading.Event()
    real = os.fsync
    written: list[str] = []

    def gated(fd):
        gate.wait(5)
        return real(fd)
    monkeypatch.setattr(os, "fsync", gated)
    real_write = v3rt._write_atomically

    def write(path, text):
        real_write(path, text)
        written.append(json.loads(text)["n"])
    monkeypatch.setattr(v3rt, "_write_atomically", write)
    path = os.path.join(str(tmp_path), STATE_FILE)
    w = v3rt._StateWriter.for_path(path)
    assert v3rt._StateWriter.for_path(path) is w, "one writer per state file"
    done: list = []
    for n in (1, 2, 3):  # 1 is picked up at once and waits for the disk; 2 is replaced by 3 before it is picked up
        assert w.submit(json.dumps({"n": n}), done.append, wait=False) is True
    time.sleep(0.05)
    reader = threading.Thread(target=v3rt._StateWriter.flush_path, args=(path,))
    reader.start()
    reader.join(0.2)
    assert reader.is_alive(), "a read did not wait for the records handed over before it"
    gate.set()
    reader.join(5)
    assert written == [1, 3] and done == [None, None]
    with open(path) as f:
        assert json.load(f) == {"n": 3}
    assert w.submit(json.dumps({"n": 4}), done.append, wait=True) is True
    with open(path) as f:
        assert json.load(f) == {"n": 4}


def test_a_record_that_cannot_be_written_is_said_to_the_caller_that_waits_for_it(tmp_path):
    rt = RuntimeV3(SimulatedIO(), RecordingSink(), clock=SystemClock(assume_ok=True), state_dir=str(tmp_path / "ok"),
                   node_uid=NODE)
    rt.compile(safety_bundle(7))
    assert rt._persist() is True and not rt._persist_failing
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    rt.state_dir = str(blocker / "state")  # a directory that cannot be made
    assert rt._persist() is False and rt._persist_failing
    assert rt._persist(wait=False) is True, "handed over: the writer says the failure (logged once)"
    v3rt._StateWriter.flush_path(rt._state_path())
    assert rt._persist_failing
    rt.state_dir = str(tmp_path / "ok")
    assert rt._persist() is True and not rt._persist_failing, "written again"


def test_a_switch_from_v3_does_not_wait_for_the_clean_stop_record_and_a_later_v3_load_reads_it(tmp_path, monkeypatch):
    import test_v3_language_switch as S
    h = ProgramHost(SimulatedIO(), RecordingSink(), clock=SystemClock(assume_ok=True), state_dir=str(tmp_path),
                    tick_seconds=0.05, node_uid="cw1PumpHouse")
    h.compile(S.light_v1(6))
    h.compile(S.light_v3(7))  # no runtime thread: switched on this thread
    assert h.language == 3
    h.step()
    fsyncs = _slow_fsync(monkeypatch, 0.5)
    t0 = time.monotonic()
    h.compile(S.light_v1(8))  # v3 -> v1: the v3 runtime's clean stop
    assert h.language == 1
    assert time.monotonic() - t0 < 0.4, "the switch waited for the clean-stop record's fsync"
    assert fsyncs == [STATE_WRITER_THREAD] or not fsyncs
    rt = RuntimeV3(SimulatedIO(), RecordingSink(), clock=SystemClock(assume_ok=True), state_dir=str(tmp_path))
    assert rt._read_state()["cleanStop"] is True, "a later v3 load reads the clean stop (it waits for the writer)"


# ------------------------------------------------------------------------------------------------- the clock probe

def test_only_the_first_clock_probe_runs_on_the_callers_thread(monkeypatch):
    probes: list[str] = []
    answer = {"ok": False}

    def slow_probe():
        probes.append(threading.current_thread().name)
        time.sleep(0.5)
        return answer["ok"]
    monkeypatch.setattr(clock_mod, "_probe_clock", slow_probe)
    c = SystemClock(probe_interval=0.05)
    me = threading.current_thread().name
    t0 = time.monotonic()
    assert c.clock_ok() is False  # the first answer is a probe's (main.py makes this call at start-up)
    assert time.monotonic() - t0 >= 0.45 and probes == [me]
    answer["ok"] = True
    time.sleep(0.06)
    t0 = time.monotonic()
    assert c.clock_ok() is False, "the last answer, while a probe runs"
    assert time.monotonic() - t0 < 0.05, "a later probe ran on the caller's thread"
    for _ in range(5):
        assert c.clock_ok() is False  # one probe at a time
    wait(lambda: c.clock_ok() is True, timeout=2.0)
    assert probes[0] == me and probes[1] == clock_mod.PROBE_THREAD and me not in probes[1:]
    assert len(probes) <= 3


def test_v3_an_estop_pressed_during_a_clock_probe_stops_the_pump_at_once(tmp_path, monkeypatch):
    started = threading.Event()

    def slow_probe():
        started.set()
        time.sleep(1.0)
        return True
    monkeypatch.setattr(clock_mod, "_probe_clock", lambda: True)
    clock = SystemClock(probe_interval=2.0)
    clock.clock_ok()  # the start-up probe (main.py)
    monkeypatch.setattr(clock_mod, "_probe_clock", slow_probe)
    h, io = _safety_host(tmp_path, clock=clock)
    pump = lambda: io.output(PUMP, "device.power")  # noqa: E731
    try:
        assert set_power(h, PUMP, 1)["accepted"]
        wait(lambda: pump() is True, timeout=PASS_S)
        started.clear()
        assert started.wait(5), "no probe was due"
        time.sleep(0.02)
        io.set_input(ESTOP, "sensor.contact", False)
        took = wait(lambda: pump() is False, timeout=10.0)
        assert took < 0.3, f"the E-stop waited {took:.2f} s for the clock probe"
        t0 = time.monotonic()
        set_power(h, LIGHT, 1)
        assert time.monotonic() - t0 < 0.3
    finally:
        h.stop(safe_state=False)


# ------------------------------------------------------------------------------------------------- the stamps

def test_a_report_keeps_the_time_its_pass_handed_it_over(tmp_path):
    http, mqtt = _GatedHttp(), _Mqtt()
    sink = CloudSink(mqtt, http, firmware_version="0.4.0")
    now = lambda: int(time.time() * 1000)  # noqa: E731
    try:
        b1 = now()
        sink.program_ack(5, "applied")
        a1 = now()
        time.sleep(0.3)
        b2 = now()
        sink.alert("The pump draws more than 8 A", "warning", "high_current", None)
        a2 = now()
        time.sleep(0.3)
        b3 = now()
        sink.post_program_ack(6, "rejected", "bundle v6 did not compile")
        a3 = now()
        time.sleep(0.3)
        http.gate.set()  # the ingress answers only now, 0.6 s after the first hand-over
        assert sink.flush(5.0)
        assert [c[0] for c in http.calls] == ["programAck", "alert", "programAck"]
        assert b1 <= http.stamps[0] <= a1 and b2 <= http.stamps[1] <= a2 and b3 <= http.stamps[2] <= a3, \
            (http.stamps, (b1, a1), (b2, a2), (b3, a3))
        assert mqtt.stamps == http.stamps[:2], "the MQTT copy carries the same time"
    finally:
        sink.close()
