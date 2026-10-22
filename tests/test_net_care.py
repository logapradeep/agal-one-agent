"""Agent 0.4.2 (contracts v1.16.0, nodes/README.md §11): the node's Wi-Fi care, its `network` block, its gaps.

The Pump house (2026-10-18): a Raspberry Pi 3B on Raspberry Pi OS (trixie, NetworkManager), agent 0.4.1, went silent for
about five minutes (10:40:13Z → 10:45:23Z) without restarting, and nothing it reported told a Wi-Fi drop — the brcmfmac
power saving is a common cause on a Pi 3B — from a router or internet drop. Here, on a /sys/class/net and a
/proc/net/wireless written in a temporary directory, with `iw` and `iwconfig` answered as a Pi 3B answers them:

* the power saving: on → turned off → read back; already off → left; NetworkManager turning it on again → turned off at
  the next check, not before; `iw` missing → `iwconfig`; neither → unknown, logged once; a turning off that fails →
  logged once, tried again; a program that does not end → killed, reported unknown, and one that will not die is left and
  no other started while it lives;
* the link: Wi-Fi associated or not, Ethernet up with carrier first (and then no power-saving action at all), Ethernet
  without carrier, nothing;
* the contract's vectors (nodes/vectors/network.json): every /proc/net/wireless sample — printed in the kernel's own
  format — the signal words, the outputs of iw and iwconfig, every gap-watch timeline;
* the gap watch end to end with the real sampling: the Pump house's 5 min 10 s with its Wi-Fi up, and one where it
  dropped; through the real HttpReporter against a local ingress that stops answering: one connectivity_gap posted, with
  an eventId, through the node-event queue;
* nothing of it runs on the runtime's thread, and an `iw` that hangs never delays a pass;
* main() wires it, and what the agent sends validates against the contract (AJV, when AJV_PATH is set).

Agent 0.4.3 (contracts v1.18.0, nodes/README.md §12): every gap also carries failures, router, internet, nameLookup and
cause — the 0.4.2 gap vectors are checked on their v1.16.0 fields (and the five checked present, the cause the table's),
the `diagnose` vectors whole; the probes themselves are tests/test_net_why.py's.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import _contracts
import test_v3_language_switch as S
from agal_one_agent import net_care
from agal_one_agent.blocks.clock import SystemClock
from agal_one_agent.blocks.host import ProgramHost
from agal_one_agent.blocks.io import SimulatedIO
from agal_one_agent.blocks.runtime import RecordingSink
from agal_one_agent.config import MqttConfig
from agal_one_agent.http_reporter import HttpReporter
from agal_one_agent.mqtt_client import AgalOneMqttClient
from agal_one_agent.net_care import (LinkSample, LinkWatch, NetworkCare, ToolHung, ToolTimeout, parse_iw_power_save,
                                     parse_iwconfig_power, parse_wireless, signal_of, with_network)
from agal_one_agent.node_events import wire_node_events

with open(_contracts.NETWORK_VECTORS, encoding="utf-8") as _f:
    V = json.load(_f)

LOGGER = "agal_one_agent.net_care"
RUNTIME_THREADS = ("blocks-runtime", "blocks-v3-runtime", "blocks-sampler")
NODE = "ApBp4jEjkZHFggoga4uC"
TOKEN = "node-token-for-the-net-care-tests-000000000000"
AJV = os.environ.get("AJV_PATH")
needs_ajv = pytest.mark.skipif(not AJV or not shutil.which("node"), reason="AJV_PATH not set or node not installed")


def _text(name: str) -> str:
    return next(c["text"] for c in V["wireless"] if c["name"] == name)


ASSOC_64 = _text("Raspberry Pi 3B (brcmfmac), associated, -64 dBm")
WEAK_74 = _text("Raspberry Pi 3B, associated, weak, -74 dBm")
NOT_ASSOC = _text("Raspberry Pi 3B, not associated (the kernel's null stats)")
HEADER_ONLY = _text("no wireless interface: the two header lines only")
IWCONFIG = next(c["output"] for c in V["powerSave"]["iwconfig"] if c["expect"] == "on" and "Pi 3B" in c["name"])


def ms_of(at: str) -> float:
    return datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp() * 1000.0


AT_MS = ms_of("2026-10-18T10:45:23.500Z")


# ------------------------------------------------------------------------------------------------- the fakes

class Board:
    """/sys/class/net and /proc/net/wireless of a board, in a temporary directory."""

    def __init__(self, tmp_path):
        self.sys = tmp_path / "sys_class_net"
        self.sys.mkdir()
        (self.sys / "lo").mkdir()  # the loopback: never the link
        (self.sys / "lo" / "type").write_text("772\n")
        self.proc = tmp_path / "proc_net_wireless"
        self.proc.write_text(HEADER_ONLY)

    def _iface(self, name: str, wireless: bool) -> None:
        d = self.sys / name
        d.mkdir(exist_ok=True)
        (d / "type").write_text("1\n")
        (d / "device").mkdir(exist_ok=True)
        if wireless:
            (d / "wireless").mkdir(exist_ok=True)

    def wifi(self, name: str = "wlan0", operstate: str = "up", down_count: int = 3, wireless: str = ASSOC_64) -> "Board":
        self._iface(name, True)
        self.set(name, operstate=operstate, down_count=down_count)
        self.proc.write_text(wireless)
        return self

    def ethernet(self, name: str = "eth0", carrier: str = "1", operstate: str = "up", down_count: int = 0) -> "Board":
        self._iface(name, False)
        self.set(name, operstate=operstate, down_count=down_count, carrier=carrier)
        return self

    def set(self, name: str, operstate=None, down_count=None, carrier=None, wireless=None) -> None:
        d = self.sys / name
        if operstate is not None:
            (d / "operstate").write_text(operstate + "\n")
        if down_count is not None:
            (d / "carrier_down_count").write_text(f"{down_count}\n")
        if carrier is not None:
            (d / "carrier").write_text(carrier + "\n")
        if wireless is not None:
            self.proc.write_text(wireless)

    def care(self, tools: "Tools", clock: "Clock", **kw) -> NetworkCare:
        return NetworkCare(sys_net=str(self.sys), proc_wireless=str(self.proc), run=tools.run, which_tool=tools.which,
                           monotonic=clock, now_ms=lambda: AT_MS, **kw)


class Tools:
    """iw and iwconfig as a Pi 3B (brcmfmac) answers them: the driver's power saving, which `… off` changes."""

    def __init__(self, power_save: str = "on", iw: bool = True, iwconfig: bool = False, set_fails: bool = False,
                 get_fails: bool = False, timeout: bool = False):
        self.state = power_save
        self.has_iw, self.has_iwconfig = iw, iwconfig
        self.set_fails, self.get_fails, self.timeout = set_fails, get_fails, timeout
        self.calls: list[list[str]] = []
        self.threads: list[str] = []

    def which(self, name: str):
        return {"iw": "/usr/sbin/iw" if self.has_iw else None, "iwconfig": "/usr/sbin/iwconfig" if self.has_iwconfig else None}.get(name)

    def run(self, argv: list, timeout: float):
        self.calls.append([os.path.basename(argv[0]), *argv[1:]])
        self.threads.append(threading.current_thread().name)
        if self.timeout:
            raise ToolTimeout(f"{os.path.basename(argv[0])} did not end in {timeout:g} s (killed)")
        tool, args = os.path.basename(argv[0]), list(argv[1:])
        if tool == "iw" and args[2:] == ["get", "power_save"]:
            return (1, "command failed: Operation not supported (-95)\n") if self.get_fails else (0, f"Power save: {self.state}\n")
        if tool == "iw" and args[2:] == ["set", "power_save", "off"]:
            if self.set_fails:
                return 255, "command failed: Operation not permitted (-1)\n"
            self.state = "off"
            return 0, ""
        if tool == "iwconfig" and len(args) == 1:
            return 0, IWCONFIG.replace("Power Management:on", f"Power Management:{self.state}")
        if tool == "iwconfig" and args[1:] == ["power", "off"]:
            self.state = "off"
            return 0, ""
        return 1, "unexpected call"


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


GET = ["iw", "dev", "wlan0", "get", "power_save"]
SET = ["iw", "dev", "wlan0", "set", "power_save", "off"]


# ------------------------------------------------------------------------------------------------- the power saving

def test_power_saving_that_is_on_is_turned_off_and_read_back(tmp_path, caplog):
    b, tools = Board(tmp_path).wifi(), Tools("on")
    care = b.care(tools, Clock())
    with caplog.at_level(logging.INFO, logger=LOGGER):
        care.step()
    assert tools.calls == [GET, SET, GET]
    assert care.status() == {"interface": "wlan0", "kind": "wifi", "connected": True, "rssiDbm": -64, "signal": "fair",
                             "powerSave": "off", "powerSaveHeld": True, "at": "2026-10-18T10:45:23.500Z"}
    assert "Wi-Fi power saving on wlan0 was on — turned off with iw" in caplog.text
    assert care.turned_off == 1


def test_power_saving_already_off_is_read_and_left_alone(tmp_path):
    b, tools = Board(tmp_path).wifi(), Tools("off")
    care = b.care(tools, Clock())
    care.step()
    assert tools.calls == [GET]
    s = care.status()
    assert s["powerSave"] == "off" and s["powerSaveHeld"] is False  # off by itself: the agent holds nothing


def test_networkmanager_turning_it_on_again_is_turned_off_at_the_next_check(tmp_path, caplog):
    b, tools, clock = Board(tmp_path).wifi(), Tools("on"), Clock()
    care = b.care(tools, clock)
    care.step()
    tools.state = "on"  # a reconnect: NetworkManager applies its default again
    tools.calls.clear()
    clock.t += 30
    care.step()
    assert tools.calls == [], "checked once a minute, not at every sample"
    clock.t += 30
    with caplog.at_level(logging.INFO, logger=LOGGER):
        care.step()
    assert tools.calls == [GET, SET, GET] and tools.state == "off"
    assert care.status()["powerSave"] == "off" and care.status()["powerSaveHeld"] is True
    assert "was on again (NetworkManager turns it on at a reconnect) — turned off again" in caplog.text
    assert care.turned_off == 2 and care.checks == 2


def test_without_iw_it_is_iwconfig(tmp_path):
    b, tools = Board(tmp_path).wifi(), Tools("on", iw=False, iwconfig=True)
    care = b.care(tools, Clock())
    care.step()
    assert tools.calls == [["iwconfig", "wlan0"], ["iwconfig", "wlan0", "power", "off"], ["iwconfig", "wlan0"]]
    assert care.status()["powerSave"] == "off" and care.status()["powerSaveHeld"] is True


def test_without_iw_or_iwconfig_it_is_unknown_and_said_once(tmp_path, caplog):
    b, tools, clock = Board(tmp_path).wifi(), Tools("on", iw=False, iwconfig=False), Clock()
    care = b.care(tools, clock)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        care.step()
        clock.t += 61
        care.step()
    assert tools.calls == [] and care.checks == 2
    assert care.status()["powerSave"] == "unknown" and care.status()["powerSaveHeld"] is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "neither iw nor iwconfig is installed" in warnings[0].getMessage()


def test_a_turning_off_that_fails_is_said_once_and_tried_again(tmp_path, caplog):
    b, tools, clock = Board(tmp_path).wifi(), Tools("on", set_fails=True), Clock()
    care = b.care(tools, clock)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        care.step()
        clock.t += 61
        care.step()
    assert tools.calls == [GET, SET, GET] * 2, "tried again at the next check"
    s = care.status()
    assert s["powerSave"] == "on" and s["powerSaveHeld"] is False
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "could not be turned off with iw (command failed: Operation not permitted (-1))" in warnings[0]


def test_an_iw_that_cannot_read_it_falls_back_to_iwconfig(tmp_path):
    b, tools = Board(tmp_path).wifi(), Tools("off", get_fails=True, iwconfig=True)
    care = b.care(tools, Clock())
    care.step()
    assert tools.calls == [GET, ["iwconfig", "wlan0"]]
    assert care.status()["powerSave"] == "off"


def test_a_program_that_does_not_end_is_reported_unknown(tmp_path, caplog):
    b, tools = Board(tmp_path).wifi(), Tools("on", timeout=True)
    care = b.care(tools, Clock())
    with caplog.at_level(logging.INFO, logger=LOGGER):
        care.step()
    assert care.status()["powerSave"] == "unknown"
    assert "did not end in 5 s (killed)" in caplog.text


def test_a_program_that_will_not_die_is_left_and_no_other_starts_while_it_lives(tmp_path):
    b, clock = Board(tmp_path).wifi(), Clock()

    class Stuck:
        alive = True

        def poll(self):
            return None if self.alive else 0

    stuck, calls = Stuck(), []

    def run(argv, timeout):
        calls.append(argv)
        if len(calls) == 1:
            raise ToolHung("iw did not end in 5 s and did not die when killed", stuck)
        return 0, "Power save: off\n"

    care = NetworkCare(sys_net=str(b.sys), proc_wireless=str(b.proc), run=run, which_tool=lambda n: f"/usr/sbin/{n}" if n == "iw" else None,
                       monotonic=clock, now_ms=lambda: AT_MS)
    care.step()
    assert len(calls) == 1 and care.status()["powerSave"] == "unknown"
    clock.t += 61
    care.step()
    assert len(calls) == 1, "nothing is started while the stuck one lives"
    stuck.alive = False
    clock.t += 61
    care.step()
    assert len(calls) == 2 and care.status()["powerSave"] == "off"


def test_run_tool_kills_a_program_that_does_not_end(tmp_path):
    script = tmp_path / "iw"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(0o755)
    t0 = time.monotonic()
    with pytest.raises(ToolTimeout) as e:
        net_care.run_tool([str(script), "dev", "wlan0", "get", "power_save"], 0.3)
    assert time.monotonic() - t0 < 2.0
    assert not isinstance(e.value, ToolHung) and "did not end in 0.3 s (killed)" in str(e.value)
    assert net_care.run_tool(["/bin/sh", "-c", "echo Power save: on"], 5.0) == (0, "Power save: on\n")


# ------------------------------------------------------------------------------------------------- the link

def test_ethernet_up_with_carrier_is_the_link_and_nothing_is_run(tmp_path):
    b, tools = Board(tmp_path).wifi().ethernet(), Tools("on")
    care = b.care(tools, Clock())
    care.step()
    assert care.status() == {"interface": "eth0", "kind": "ethernet", "connected": True, "at": "2026-10-18T10:45:23.500Z"}
    assert tools.calls == [], "no power-saving action on Ethernet"


def test_ethernet_without_carrier_leaves_the_wifi_the_link(tmp_path):
    b, tools = Board(tmp_path).wifi().ethernet(carrier="0", operstate="down"), Tools("off")
    care = b.care(tools, Clock())
    care.step()
    assert care.status()["interface"] == "wlan0" and care.status()["kind"] == "wifi"


def test_an_ethernet_without_carrier_and_no_wifi_is_reported_down(tmp_path):
    b, tools = Board(tmp_path).ethernet(carrier="0", operstate="down"), Tools()
    care = b.care(tools, Clock())
    care.step()
    assert care.status() == {"interface": "eth0", "kind": "ethernet", "connected": False, "at": "2026-10-18T10:45:23.500Z"}
    assert tools.calls == []


def test_no_interface_no_block_and_a_bridge_is_not_ethernet(tmp_path):
    b, tools = Board(tmp_path), Tools()
    (b.sys / "docker0").mkdir()  # a bridge: type 1, no device
    (b.sys / "docker0" / "type").write_text("1\n")
    (b.sys / "docker0" / "carrier").write_text("1\n")
    care = b.care(tools, Clock())
    care.step()
    assert care.status() is None and tools.calls == []
    assert with_network({"capabilities": {"blockLanguage": 3}}, care) == {"capabilities": {"blockLanguage": 3}}
    assert with_network(None, care) is None


def test_wifi_not_associated_has_no_level(tmp_path):
    b, tools = Board(tmp_path).wifi(operstate="down", wireless=NOT_ASSOC), Tools("off")
    care = b.care(tools, Clock())
    care.step()
    assert care.status() == {"interface": "wlan0", "kind": "wifi", "connected": False, "powerSave": "off",
                             "powerSaveHeld": False, "at": "2026-10-18T10:45:23.500Z"}


def test_the_heartbeat_extra_carries_the_block(tmp_path):
    b, tools = Board(tmp_path).wifi(wireless=WEAK_74), Tools("on")
    care = b.care(tools, Clock())
    care.step()
    extra = with_network({"capabilities": {"blockLanguage": 3}, "programVersion": 5}, care)
    assert extra["capabilities"] == {"blockLanguage": 3} and extra["programVersion"] == 5
    assert extra["network"] == {"interface": "wlan0", "kind": "wifi", "connected": True, "rssiDbm": -74, "signal": "weak",
                                "powerSave": "off", "powerSaveHeld": True, "at": "2026-10-18T10:45:23.500Z"}
    posted = []
    rep = HttpReporter(NODE, auth_token=TOKEN, base_url="https://ingress.example/telemetryIngress")
    rep._post_status = lambda data: posted.append(data) or 200
    rep.report_status(online=True, uptime=12, firmware_version="0.4.2", extra=extra)
    assert posted[0]["payload"]["network"] == extra["network"]


# ------------------------------------------------------------------------------------------------- the contract's vectors

@pytest.mark.parametrize("case", V["wireless"], ids=lambda c: c["name"])
def test_proc_net_wireless_vectors(case):
    assert parse_wireless(case["text"]) == case["expect"]


@pytest.mark.parametrize("case", V["signal"], ids=lambda c: str(c["rssiDbm"]))
def test_signal_words(case):
    assert signal_of(case["rssiDbm"]) == case["expect"]


@pytest.mark.parametrize("case", V["powerSave"]["iw"], ids=lambda c: c["name"])
def test_iw_outputs(case):
    assert parse_iw_power_save(case["output"]) == case["expect"]


@pytest.mark.parametrize("case", V["powerSave"]["iwconfig"], ids=lambda c: c["name"])
def test_iwconfig_outputs(case):
    assert parse_iwconfig_power(case["output"]) == case["expect"]


def _replay(steps: list, emitted: list, min_gap_s: float = V["_meta"]["minGapS"]) -> LinkWatch:
    """A timeline of the contract's vectors on the real LinkWatch: answers, misses (with their failure kind, 0.4.3),
    link samples, and (0.4.3) probe rounds — handed over as NetworkProbe hands them, for the silence that lasts."""
    clock = {"t": 0.0, "ms": 0.0}
    w = LinkWatch(emitted.append, monotonic=lambda: clock["t"], now_ms=lambda: clock["ms"], min_gap_s=min_gap_s)
    for s in steps:
        clock["t"] = float(s["t"])
        if s["kind"] == "answer":
            clock["ms"] = ms_of(s["at"])
            w.answered()
        elif s["kind"] == "miss":
            w.no_answer(s.get("failure"))
        elif s["kind"] == "probe":
            silence = w.silence()
            if silence is not None:
                w.found(silence, router=s.get("router"), internet=s.get("internet"), name_lookup=s.get("nameLookup"))
        else:
            w.sample(LinkSample.from_reference(s["sample"]))
    return w


#: The fields of agent 0.4.2's gap (v1.16.0); agent 0.4.3 adds DIAGNOSTICS.
V1160 = ("from", "to", "seconds", "interface", "kind", "wifiConnectedThroughout", "wifiDropped", "rssiDbmBefore",
         "signalBefore", "powerSave")
DIAGNOSTICS = ("failures", "router", "internet", "nameLookup", "cause")


@pytest.mark.parametrize("case", V["gaps"], ids=lambda c: c["name"])
def test_the_gap_watch_answers_every_vector(case):
    emitted: list = []
    _replay(case["steps"], emitted)
    got = [e["payload"]["connectivity"] for e in emitted]
    if case.get("diagnose"):
        assert got == case["expect"]
    else:  # an agent 0.4.2 timeline: its fields as they were, and the five of 0.4.3 beside them
        assert [{k: g[k] for k in V1160 if k in g} for g in got] == case["expect"]
        assert all(set(DIAGNOSTICS) <= set(g) and g["cause"] == net_care.cause_of(g) for g in got)
    assert all(e["type"] == "connectivity_gap" and e["source"] == "node" and "sourceKey" not in e for e in emitted)


# ------------------------------------------------------------------------------------------------- the gap, end to end

def _pump_house(tmp_path, drop_at=None):
    """The Pump house's morning on the real sampling: answers every 30 s until t = 1000 (10:40:13.500Z), then the
    heartbeats fail until t = 1310 (10:45:23.500Z). `drop_at`: (start, end) of a Wi-Fi drop inside the gap."""
    b, tools, clock = Board(tmp_path).wifi(), Tools("on"), Clock(0.0)
    emitted: list = []
    t0_ms = ms_of("2026-10-18T10:40:13.500Z") - 1000 * 1000.0
    watch = LinkWatch(emitted.append, monotonic=clock, now_ms=lambda: t0_ms + clock.t * 1000.0)
    care = NetworkCare(sys_net=str(b.sys), proc_wireless=str(b.proc), run=tools.run, which_tool=tools.which,
                       monotonic=clock, now_ms=lambda: t0_ms + clock.t * 1000.0, on_sample=watch.sample)
    down = 3
    for t in range(900, 1311, 5):
        clock.t = float(t)
        if drop_at and t == drop_at[0]:
            down += 1
            b.set("wlan0", operstate="down", down_count=down, wireless=NOT_ASSOC)
        if drop_at and t == drop_at[1]:
            b.set("wlan0", operstate="up", wireless=ASSOC_64)
        care.step()
        if t <= 1000 and t % 30 == 10:
            watch.answered()          # a heartbeat answered (t = 910, 940, 970, 1000)
        elif 1000 < t < 1310 and t % 30 == 10:
            watch.no_answer()         # a heartbeat that got nothing
        elif t == 1310:
            watch.answered()          # back
    return emitted


#: What a gap of the Pump house's morning carries beside its v1.16.0 fields on agent 0.4.3 with no probe thread (here):
#: ten heartbeats that got nothing (no kind given: other), nothing found.
NOT_PROBED = {"failures": {"dns": 0, "connect": 0, "tls": 0, "timeout": 0, "other": 10}, "router": "unknown",
              "internet": "unknown", "nameLookup": "unknown"}


def test_the_pump_house_was_cut_off_with_its_wifi_up(tmp_path):
    emitted = _pump_house(tmp_path)
    assert [e["payload"] for e in emitted] == [{"connectivity": {
        "from": "2026-10-18T10:40:13.500Z", "to": "2026-10-18T10:45:23.500Z", "seconds": 310, "interface": "wlan0",
        "kind": "wifi", "wifiConnectedThroughout": True, "wifiDropped": False, "rssiDbmBefore": -64, "signalBefore": "fair",
        "powerSave": "off", **NOT_PROBED, "cause": "unknown"}}]


def test_the_pump_house_was_cut_off_and_its_wifi_dropped(tmp_path):
    emitted = _pump_house(tmp_path, drop_at=(1050, 1200))
    assert [e["payload"]["connectivity"] for e in emitted] == [{
        "from": "2026-10-18T10:40:13.500Z", "to": "2026-10-18T10:45:23.500Z", "seconds": 310, "interface": "wlan0",
        "kind": "wifi", "wifiConnectedThroughout": False, "wifiDropped": True, "rssiDbmBefore": -64, "signalBefore": "fair",
        "powerSave": "off", **NOT_PROBED, "cause": "wifi"}]


class _Ingress:
    """A local telemetryIngress: every body recorded, answered 200 (or `status`), with a Date."""

    def __init__(self):
        outer = self
        self.bodies: list = []
        self.status = 200

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                outer.bodies.append(json.loads(self.rfile.read(n) or b"{}"))
                body = json.dumps({"success": outer.status == 200}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/telemetryIngress"
        threading.Thread(target=self.server.serve_forever, name="test-ingress", daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _dead_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens there: connection refused, no answer
    return f"http://127.0.0.1:{port}/telemetryIngress"


def test_the_reporter_tells_the_watch_and_the_gap_is_posted_once_through_the_node_event_queue():
    ingress = _Ingress()
    try:
        rep = HttpReporter(NODE, auth_token=TOKEN, base_url=ingress.url)
        mqtt = AgalOneMqttClient(MqttConfig(broker="broker.example", username="shared", status_topic="agal/n/status",
                                            commands_topic="agal/n/commands"))  # MQTT down: the HTTPS leg goes alone
        events = wire_node_events(mqtt, rep, offline=False, clock_ok=lambda: True)
        watch = LinkWatch(mqtt.publish_event, min_gap_s=0.2)
        rep.link_watch = watch
        rep.report_status(online=True, uptime=1, firmware_version="0.4.2")        # answered
        good = rep.base_url
        rep.base_url = _dead_url()
        rep.report_status(online=True, uptime=2, firmware_version="0.4.2")        # no answer
        assert rep.last_status is None
        time.sleep(0.3)
        rep.base_url = good
        rep.report_status(online=True, uptime=3, firmware_version="0.4.2")        # answered: the gap ends
        assert rep.last_status == 200 and len(watch.gaps) == 1
        assert events.wait_idle(10.0)
        posted = [b for b in ingress.bodies if b.get("type") == "sensor_event"]
        assert len(posted) == 1
        p = posted[0]["payload"]
        assert p["nodeUid"] == NODE and len(p["eventId"]) == 20 and isinstance(p["timestamp"], int)
        assert p["event"]["type"] == "connectivity_gap" and p["event"]["source"] == "node"
        gap = p["event"]["payload"]["connectivity"]
        assert gap == watch.gaps[0] and gap["seconds"] == 0 and ms_of(gap["to"]) >= ms_of(gap["from"])
        # nothing known of the link (no net-care here): no interface, no kind, the five null
        assert {k: gap[k] for k in ("wifiConnectedThroughout", "wifiDropped", "rssiDbmBefore", "signalBefore", "powerSave")} == \
            dict.fromkeys(("wifiConnectedThroughout", "wifiDropped", "rssiDbmBefore", "signalBefore", "powerSave"))
        # 0.4.3: the request to the dead port was refused — a connect failure; no probe ran here: unknown
        assert gap["failures"] == {"dns": 0, "connect": 1, "tls": 0, "timeout": 0, "other": 0}
        assert (gap["router"], gap["internet"], gap["nameLookup"], gap["cause"]) == ("unknown",) * 4
        # answered again at once: no second gap
        rep.report_status(online=True, uptime=4, firmware_version="0.4.2")
        assert len(watch.gaps) == 1
        events.close(timeout=1.0)
    finally:
        ingress.close()


def test_an_error_answer_is_an_answer():
    """A 500 (or a 401, a 429) came from the cloud: the request reached it — the link was up."""
    ingress = _Ingress()
    try:
        watch = LinkWatch(None, min_gap_s=0.2)
        rep = HttpReporter(NODE, auth_token=TOKEN, base_url=ingress.url, link_watch=watch)
        rep.report_status(online=True, uptime=1)
        good, rep.base_url = rep.base_url, _dead_url()
        rep.report_status(online=True, uptime=2)
        time.sleep(0.3)
        rep.base_url = good
        ingress.status = 500
        rep.report_status(online=True, uptime=3)
        assert rep.last_status == 500 and len(watch.gaps) == 1, "the 500 ended the gap"
        ingress.status = 429
        rep.report_status(online=True, uptime=4)
        assert len(watch.gaps) == 1
    finally:
        ingress.close()


def test_every_request_of_the_reporter_reaches_the_watch():
    """getProgram, reportHardware and getBootState go through the same door as the ingress posts."""
    seen = []

    class W:
        def answered(self):
            seen.append("answer")

        def no_answer(self, kind=None):
            seen.append(f"miss {kind}")

    dead = _dead_url()
    rep = HttpReporter(NODE, auth_token=TOKEN, base_url=dead, link_watch=W())
    import agal_one_agent.http_reporter as hr
    old = hr.BOOT_STATE_URL
    hr.BOOT_STATE_URL = dead
    try:
        assert rep.fetch_program(5) is None
        assert rep.report_hardware({"model": "Raspberry Pi 3 Model B Rev 1.2"}) is None
        assert rep.fetch_boot_state(TOKEN) == []
        assert rep.report_command_ack("Xq3k9TzLmN2pRs7VbW1c", "completed") is None
    finally:
        hr.BOOT_STATE_URL = old
    assert seen == ["miss connect"] * 4, "0.4.3: each with how it got no answer (the dead port refuses)"
    ingress = _Ingress()
    try:
        rep.base_url = ingress.url
        rep.fetch_program(5)
        assert seen[-1] == "answer"
    finally:
        ingress.close()


# ------------------------------------------------------------------------------------------------- the runtime's thread

def test_the_care_runs_on_its_own_thread_and_a_hung_iw_never_delays_a_pass(tmp_path):
    b = Board(tmp_path).wifi()
    iw = tmp_path / "bin" / "iw"
    iw.parent.mkdir()
    iw.write_text("#!/bin/sh\nsleep 30\n")  # an iw that never answers
    iw.chmod(0o755)
    seen: list = []

    def run(argv, timeout):
        seen.append(("program", threading.current_thread().name))
        return net_care.run_tool(argv, timeout)

    care = NetworkCare(sys_net=str(b.sys), proc_wireless=str(b.proc), run=run, which_tool=lambda n: str(iw) if n == "iw" else None,
                       sample_s=0.05, check_s=0.2, tool_timeout_s=0.5)
    read = care._read_text

    def recorded_read(path):
        seen.append(("file", threading.current_thread().name))
        return read(path)

    care._read_text = recorded_read
    h = ProgramHost(SimulatedIO(), RecordingSink(), clock=SystemClock(assume_ok=True), state_dir=str(tmp_path / "state"),
                    tick_seconds=0.1, node_uid=NODE)
    h.compile(S.light_v1(6))
    passes: list = []
    rt = h.active
    real_step = rt.step

    def step():
        passes.append(time.monotonic())
        seen.append(("pass", threading.current_thread().name))
        return real_step()

    rt.step = step
    h.start()
    assert care.start()
    try:
        deadline = time.monotonic() + 5
        while not any(w == "program" for w, _ in seen) and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)  # iw is now hanging for its whole 0.5 s, on the net-care thread
        t0 = time.monotonic()
        for _ in range(20):
            care.status()
        assert time.monotonic() - t0 < 0.05, "the heartbeat's read of the block waited"
        t0 = time.monotonic()
        assert S.switch(h, S.LIGHT, "device.power", 1)["accepted"]
        while h.io.output(S.LIGHT, "device.power") is not True and time.monotonic() - t0 < 2:
            time.sleep(0.005)
        assert time.monotonic() - t0 < 0.35, "the switch waited for iw"
        mark = len(passes)
        time.sleep(1.5)  # three hung checks in a row
        window = passes[mark:]
        gaps = [b2 - a for a, b2 in zip(window, window[1:])]
        assert len(window) >= 10 and max(gaps) < 0.35, f"passes stalled: {len(window)} passes, longest gap {max(gaps):.2f} s"
    finally:
        care.stop()
        h.stop(safe_state=False)
    assert care.status()["powerSave"] == "unknown"
    io_threads = {n for w, n in seen if w in ("program", "file")}
    assert io_threads == {net_care.THREAD}, io_threads
    assert {n for w, n in seen if w == "pass"} <= set(RUNTIME_THREADS)
    assert sum(1 for w, _ in seen if w == "program") >= 2, "the hung iw was tried again at later checks"


# ------------------------------------------------------------------------------------------------- main() and the contract

class _WiredHere(Exception):
    pass


@pytest.mark.parametrize("offline", [False, True])
def test_main_wires_the_network(tmp_path, monkeypatch, offline):
    from agal_one_agent import main as agent_main
    from test_command_acks import _node_config
    seen = {}

    def wire(mqtt_client, reporter, *, offline, clock_ok=None, server_time=None):
        seen.update(mqtt=mqtt_client, reporter=reporter, offline=offline, clock_ok=clock_ok, server_time=server_time)
        raise _WiredHere

    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(agent_main, "wire_network", wire)
    monkeypatch.setattr("sys.argv", ["agal-one-agent", "--config", _node_config(tmp_path)] + (["--offline"] if offline else []))
    with pytest.raises(_WiredHere):
        agent_main.main()
    assert isinstance(seen["mqtt"], AgalOneMqttClient) and isinstance(seen["reporter"], HttpReporter)
    assert seen["offline"] is offline and callable(seen["clock_ok"]) and seen["server_time"] is seen["reporter"].server_time


def test_wire_network_gives_the_reporter_its_watch_and_nothing_offline(tmp_path):
    mqtt = AgalOneMqttClient(MqttConfig(broker="b", username="u", status_topic="s", commands_topic="c"))
    rep = HttpReporter(NODE, auth_token=TOKEN, base_url="https://ingress.example/telemetryIngress")
    assert net_care.wire_network(mqtt, rep, offline=True) is None and rep.link_watch is None
    care = net_care.wire_network(mqtt, rep, offline=False, clock_ok=lambda: True, sys_net=str(tmp_path / "no-such-dir"))
    try:
        assert isinstance(rep.link_watch, LinkWatch) and care.watch is rep.link_watch
        assert care.on_sample == rep.link_watch.sample and rep.link_watch.emit == mqtt.publish_event
        assert care._thread is None, "no /sys/class/net: nothing to watch, no thread"
    finally:
        care.stop()


def _node(name: str, script: str, *args: str) -> subprocess.CompletedProcess:
    here = os.path.dirname(os.path.abspath(__file__))
    return subprocess.run(["node", os.path.join(here, script), os.path.join(_contracts.CONTRACTS, "schemas"), *args],
                          capture_output=True, text=True, env=dict(os.environ, AJV_PATH=AJV), timeout=120)


@needs_ajv
def test_every_network_block_the_agent_sends_is_an_iot_node_network(tmp_path):
    blocks = []
    for build in (lambda b: b.wifi(), lambda b: b.wifi(wireless=WEAK_74), lambda b: b.wifi(operstate="down", wireless=NOT_ASSOC),
                  lambda b: b.wifi().ethernet(), lambda b: b.ethernet(carrier="0", operstate="down")):
        for tools in (Tools("on"), Tools("off"), Tools("on", set_fails=True), Tools(iw=False)):
            d = tmp_path / f"b{len(blocks)}"
            d.mkdir()
            care = build(Board(d)).care(tools, Clock())
            care.step()
            blocks.append(care.status())
    path = tmp_path / "blocks.json"
    path.write_text(json.dumps(blocks))
    r = _node("network", "validate_docs.mjs", "https://menvayal.com/schemas/iot-node.schema.json#/properties/network", str(path))
    assert r.returncode == 0, r.stdout + r.stderr


@needs_ajv
def test_every_gap_the_agent_raises_is_a_valid_event_document(tmp_path):
    raised: list = []
    for case in V["gaps"]:
        _replay(case["steps"], raised)
    for name, drop in (("stayed", None), ("dropped", (1050, 1200))):
        (tmp_path / name).mkdir()
        raised += _pump_house(tmp_path / name, drop_at=drop)
    assert len(raised) >= 10
    path = tmp_path / "events.json"
    path.write_text(json.dumps([{"type": e["type"], "source": e["source"], "payload": e["payload"]} for e in raised]))
    r = _node("events", "validate_events.mjs", str(path))
    assert r.returncode == 0, r.stdout + r.stderr
