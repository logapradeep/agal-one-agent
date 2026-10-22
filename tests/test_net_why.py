"""Agent 0.4.3 (contracts v1.17.0, nodes/README.md §12): why the cloud could not be reached.

The Pump house (2026-10-22): from 16:18Z a Raspberry Pi 3B on Raspberry Pi OS (trixie, NetworkManager), agent 0.4.2,
lost the cloud about once a minute for 25+ minutes — its MQTT connection reconnected every ~61–92 s and most HTTPS
heartbeats between got no answer — while every gap said its Wi-Fi stayed connected (−42 … −44 dBm, power saving off).
Nothing said whether the router, the internet behind it, the name lookup or the cloud had failed. Here:

* every error urllib raises has its kind (dns, connect, tls, timeout, other) — constructed, and REAL: a name that does
  not resolve, a refused port, a TLS server that speaks no TLS, one that never finishes the handshake, an HTTP server
  that never answers, one that closes — through the real HttpReporter; the error's text never reaches the cloud;
* the contract's vectors (nodes/vectors/network.json): `ip -4 route show default`, `ip neigh show`, iputils `ping`'s exit
  codes, glibc's `getent ahosts`, each probe's finding, how a silence's findings add up, the decision table;
* the probe round on a Pi 3B's tools: its exact argv lists and bounds, the router silent ends the round, the cloud's
  last-known address (looked up after the first answer, then hourly; a round's lookup refreshes it), no third-party host;
* rounds at most every 10 s and at most 12 per silence; a round of a silence that ended is dropped; a probe that will not
  die starts no other program and delays nothing; a ping that does not end is killed at its bound; a gap with no probe
  possible ends unknown; nothing before the cloud has answered once;
* the probes run on their own thread (`net-probe`): a pass is never delayed, a request never waits;
* end to end, the real HttpReporter, LinkWatch, NetworkProbe and node-event queue against a local ingress: the gap says
  why; main()'s wiring starts the probe with the care and stops it with it; every gap validates (AJV).
"""

from __future__ import annotations

import errno
import http.client
import json
import logging
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request

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
from agal_one_agent.net_care import (LinkWatch, NetworkProbe, ToolHung, ToolTimeout, cause_of, cloud_address_of,
                                     cloud_endpoint, failure_kind, internet_of, merge_finding, name_lookup_of,
                                     parse_ahosts, parse_default_route, parse_neigh_state, router_of)
from agal_one_agent.node_events import wire_node_events
from test_net_care import _Ingress, _dead_url, _replay

with open(_contracts.NETWORK_VECTORS, encoding="utf-8") as _f:
    V = json.load(_f)

LOGGER = "agal_one_agent.net_care"
RUNTIME_THREADS = ("blocks-runtime", "blocks-v3-runtime", "blocks-sampler")
NODE = "ApBp4jEjkZHFggoga4uC"
TOKEN = "node-token-for-the-net-why-tests-0000000000000"
HOST = "asia-south1-agal-one-prod.cloudfunctions.net"
INGRESS = f"https://{HOST}/telemetryIngress"
GW = "192.168.1.1"
AJV = os.environ.get("AJV_PATH")
needs_ajv = pytest.mark.skipif(not AJV or not shutil.which("node"), reason="AJV_PATH not set or node not installed")
FIVE = ("failures", "router", "internet", "nameLookup", "cause")


def _vector(section: str, name_part: str) -> dict:
    return next(c for c in V[section] if name_part in c["name"])


ROUTE_WIFI = _vector("routes", "a Raspberry Pi 3B on Wi-Fi")["text"]
NEIGH = {c["expect"]: c["text"] for c in V["neigh"] if c["dev"] == "wlan0" and c["expect"] and "two links" not in c["name"]
         and "protocol" not in c["name"]}
PING_OUT = {c["ping"]: c["pingOutput"] for c in V["router"] if c.get("pingOutput")}
AHOSTS_V4 = _vector("ahosts", "IPv4 only")["text"]
AHOSTS_V6_FIRST = _vector("ahosts", "IPv6 first")["text"]
ADDRESS = _vector("ahosts", "IPv4 only")["address"]


# ------------------------------------------------------------------------------------------------- 1. the failure kinds

class _Http:
    """A local TCP server that behaves badly on purpose: `silent` accepts and never writes (a TLS handshake that never
    finishes, an HTTP request never answered); `garbage` answers a TLS hello with plain HTTP; `close` reads the request
    and closes without an answer."""

    def __init__(self, behaviour: str):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.behaviour = behaviour
        self.conns: list = []
        threading.Thread(target=self._serve, name="test-bad-server", daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(c)
            try:
                if self.behaviour == "garbage":
                    c.recv(4096)
                    c.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 0\r\n\r\n")
                    c.close()
                elif self.behaviour == "close":
                    c.recv(4096)
                    c.close()
            except OSError:
                pass

    def url(self, scheme: str) -> str:
        return f"{scheme}://127.0.0.1:{self.port}/telemetryIngress"

    def close(self):
        self.sock.close()
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass


CONSTRUCTED = [
    ("the name did not resolve", urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, "Name or service not known")), "dns"),
    ("no DNS answer (EAI_AGAIN)", urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")), "dns"),
    ("refused", urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")), "connect"),
    ("no route to the network", urllib.error.URLError(OSError(errno.ENETUNREACH, "Network is unreachable")), "connect"),
    ("no route to the host (ARP for the gateway failed)", urllib.error.URLError(OSError(errno.EHOSTUNREACH, "No route to host")), "connect"),
    ("the connection timed out (socket timeout)", urllib.error.URLError(socket.timeout("timed out")), "connect"),
    ("the kernel gave up connecting (ETIMEDOUT)", urllib.error.URLError(OSError(errno.ETIMEDOUT, "Connection timed out")), "connect"),
    ("its address went (EADDRNOTAVAIL)", urllib.error.URLError(OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")), "connect"),
    ("a certificate not accepted (a clock far off)", urllib.error.URLError(ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate is not yet valid (_ssl.c:1028)")), "tls"),
    ("TLS refused", urllib.error.URLError(ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1082)")), "tls"),
    ("the handshake cut", urllib.error.URLError(ssl.SSLEOFError(8, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")), "tls"),
    ("the handshake timed out", urllib.error.URLError(socket.timeout("_ssl.c:1064: The handshake operation timed out")), "tls"),
    ("connected, no answer in time", socket.timeout("timed out"), "timeout"),
    ("connected, no answer in time (TimeoutError)", TimeoutError("The read operation timed out"), "timeout"),
    ("closed without an answer", http.client.RemoteDisconnected("Remote end closed connection without response"), "other"),
    ("reset while waiting", ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"), "other"),
    ("reset while sending", urllib.error.URLError(ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")), "other"),
    ("a TLS error after the handshake", ssl.SSLEOFError(8, "EOF occurred in violation of protocol"), "other"),
    ("a short answer", http.client.IncompleteRead(b""), "other"),
    ("a URLError with words only", urllib.error.URLError("unknown url type: ftpx"), "other"),
    ("not even a request", ValueError("unknown url type: 'telemetryIngress'"), "other"),
]


@pytest.mark.parametrize("label,error,kind", CONSTRUCTED, ids=[c[0] for c in CONSTRUCTED])
def test_each_error_urlopen_raises_has_its_kind(label, error, kind):
    assert failure_kind(error) == kind


class _Recorder:
    def __init__(self):
        self.kinds: list = []
        self.answers = 0

    def answered(self):
        self.answers += 1

    def no_answer(self, kind=None):
        self.kinds.append(kind)


def test_real_failures_through_the_real_reporter_have_their_kinds(monkeypatch):
    rec = _Recorder()
    rep = HttpReporter(NODE, auth_token=TOKEN, link_watch=rec)
    silent, garbage, closing = _Http("silent"), _Http("garbage"), _Http("close")
    try:
        cases = [(_dead_url(), "connect"), (garbage.url("https"), "tls"), (silent.url("https"), "tls"),
                 (silent.url("http"), "timeout"), (closing.url("http"), "other")]
        for url, _ in cases:
            req = urllib.request.Request(url, data=b"{}", method="POST")
            with pytest.raises(Exception):
                rep._open(req, 0.4)
        assert rec.kinds == [k for _, k in cases]

        def no_name(*a, **k):
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", no_name)
        with pytest.raises(urllib.error.URLError):
            rep._open(urllib.request.Request("https://cloud.invalid/telemetryIngress", data=b"{}", method="POST"), 0.4)
        assert rec.kinds[-1] == "dns" and rec.answers == 0
    finally:
        for s in (silent, garbage, closing):
            s.close()


def test_the_report_paths_still_fail_as_before_and_count_by_kind():
    """The posts keep their answers (None when not reached, the status of an answer); the watch counts by kind."""
    emitted: list = []
    watch = LinkWatch(emitted.append, min_gap_s=0.2)
    ingress = _Ingress()
    try:
        rep = HttpReporter(NODE, auth_token=TOKEN, base_url=ingress.url, link_watch=watch)
        rep.report_status(online=True, uptime=1)
        rep.base_url = _dead_url()
        assert rep.report_command_ack("Xq3k9TzLmN2pRs7VbW1c", "completed") is None
        assert rep.fetch_program(5) is None
        time.sleep(0.25)
        rep.base_url = ingress.url
        ingress.status = 503
        rep.report_status(online=True, uptime=2)  # a 503 is an answer: the gap ends
        assert rep.last_status == 503 and len(watch.gaps) == 1
        assert watch.gaps[0]["failures"] == {"dns": 0, "connect": 2, "tls": 0, "timeout": 0, "other": 0}
    finally:
        ingress.close()


# ------------------------------------------------------------------------------------------------- 2. the contract's vectors

@pytest.mark.parametrize("case", V["routes"], ids=lambda c: c["name"])
def test_ip_route_vectors(case):
    assert parse_default_route(case["text"]) == case["expect"]


@pytest.mark.parametrize("case", V["neigh"], ids=lambda c: c["name"])
def test_ip_neigh_vectors(case):
    assert parse_neigh_state(case["text"], case["gateway"], case["dev"]) == case["expect"]


@pytest.mark.parametrize("case", V["router"], ids=lambda c: c["name"])
def test_router_vectors(case):
    assert router_of(case["gateway"], case["ping"], case["neigh"]) == case["expect"]


@pytest.mark.parametrize("case", V["ahosts"], ids=lambda c: c["name"])
def test_getent_ahosts_vectors(case):
    assert parse_ahosts(case["text"]) == case["expect"]
    assert cloud_address_of(parse_ahosts(case["text"])) == case["address"]


@pytest.mark.parametrize("case", V["nameLookup"], ids=lambda c: c["name"])
def test_name_lookup_vectors(case):
    assert name_lookup_of(case["exit"], case.get("timedOut", False), case["output"]) == case["expect"]


@pytest.mark.parametrize("case", V["internet"], ids=lambda c: c["name"])
def test_internet_vectors(case):
    assert internet_of(case["address"], case["connected"]) == case["expect"]


@pytest.mark.parametrize("case", V["merge"], ids=lambda c: f"{c['what']} {c['a']} + {c['b']}")
def test_findings_add_up_vectors(case):
    assert merge_finding(case["what"], case["a"], case["b"]) == case["expect"]


@pytest.mark.parametrize("case", V["causes"], ids=lambda c: c["name"])
def test_the_decision_table(case):
    assert cause_of(case["connectivity"]) == case["expect"]


def test_the_bounds_are_the_contracts():
    p = V["_meta"]["probe"]
    assert (net_care.PROBE_EVERY_S, net_care.PROBE_ROUNDS, net_care.PING_WAIT_S, net_care.CONNECT_S, net_care.LOOKUP_S,
            net_care.CLOUD_PORT, net_care.MAX_FAILURES) == (p["everyS"], p["rounds"], p["pingWaitS"], p["connectS"],
                                                             p["lookupS"], p["port"], p["maxFailures"])
    assert net_care.LOOKUP_S <= 3 and net_care.CONNECT_S <= 3 and net_care.PING_TIMEOUT_S <= 3 and net_care.IP_TIMEOUT_S <= 3


def test_a_count_stops_at_a_million():
    w = LinkWatch(None)
    w.answered()
    w._failures["dns"] = net_care.MAX_FAILURES - 1
    w.no_answer("dns")
    w.no_answer("dns")
    assert w._failures["dns"] == net_care.MAX_FAILURES


# ------------------------------------------------------------------------------------------------- 3. the probe round

class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t
        self.waits: list = []

    def __call__(self) -> float:
        return self.t

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.t += seconds
        return False


class PiTools:
    """ip, ping and getent as a Raspberry Pi 3B on Raspberry Pi OS answers them (the contract's samples)."""

    def __init__(self, route: str = ROUTE_WIFI, ping: int = 0, neigh: str = "REACHABLE", getent: int = 0,
                 ahosts: str = AHOSTS_V4, missing: tuple = (), hang=None, timeout=None):
        self.route, self.ping, self.neigh, self.getent, self.ahosts = route, ping, neigh, getent, ahosts
        self.missing = set(missing)
        self.hang, self.timeout = hang, timeout  # a tool name: it will not die / it is killed at its bound
        self.calls: list = []

    def which(self, name: str):
        return None if name in self.missing else {"ip": "/usr/bin/ip", "ping": "/usr/bin/ping", "getent": "/usr/bin/getent"}.get(name)

    def run(self, argv: list, timeout: float):
        name = os.path.basename(argv[0])
        self.calls.append((name, list(argv[1:]), timeout, threading.current_thread().name))
        if name == self.hang:
            raise ToolHung(f"{name} did not end in {timeout:g} s and did not die when killed", self.stuck)
        if name == self.timeout:
            raise ToolTimeout(f"{name} did not end in {timeout:g} s (killed)")
        if name == "ip" and argv[1:] == ["-4", "route", "show", "default"]:
            return 0, self.route
        if name == "ip" and argv[1:3] == ["neigh", "show"]:
            return 0, NEIGH.get(self.neigh, "") if self.neigh else ""
        if name == "ping":
            return self.ping, PING_OUT.get(self.ping, "")
        if name == "getent":
            return self.getent, self.ahosts if self.getent == 0 else ""
        return 1, "unexpected call"

    class _Stuck:
        alive = True

        def poll(self):
            return None if self.alive else 0

    stuck = _Stuck()

    def programs(self) -> list:
        return [(n, a) for n, a, _, _ in self.calls]


class Net:
    """The TCP connect of the internet probe: answers `ok` and records what it was asked."""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list = []

    def __call__(self, address: str, port: int, timeout: float) -> bool:
        self.calls.append((address, port, timeout, threading.current_thread().name))
        return self.ok


def _silence(watch: LinkWatch, kind: str = "connect") -> int:
    """The cloud answered once, then a request got none: the silence the probes are for."""
    if not watch.has_answered():
        watch.answered()
    watch.no_answer(kind)
    s = watch.silence()
    assert s is not None
    return s


def _probe(watch: LinkWatch, tools: PiTools, net: Net, clock: Clock, **kw) -> NetworkProbe:
    return NetworkProbe(watch, lambda: cloud_endpoint(INGRESS), run=tools.run, which_tool=tools.which, connect=net,
                        monotonic=clock, wait=clock.wait, **kw)


def test_a_round_asks_the_router_then_the_internet_then_the_name_lookup():
    clock, tools, net = Clock(), PiTools(), Net(ok=False)
    watch = LinkWatch(None, monotonic=clock)
    probe = _probe(watch, tools, net, clock)
    watch.answered()
    probe.step()                      # the first answer: the cloud's address is looked up (the only program)
    assert tools.programs() == [("getent", ["ahosts", HOST])] and probe.lookups == 1
    tools.calls.clear()
    silence = _silence(watch)
    got = probe.round(silence)
    assert tools.programs() == [("ip", ["-4", "route", "show", "default"]), ("ping", ["-n", "-c1", "-W1", GW]),
                                ("ip", ["neigh", "show", GW]), ("getent", ["ahosts", HOST])]
    assert [t for _, _, t, _ in tools.calls] == [net_care.IP_TIMEOUT_S, net_care.PING_TIMEOUT_S, net_care.IP_TIMEOUT_S,
                                                 net_care.LOOKUP_S]
    assert net.calls == [(ADDRESS, 443, 3.0, threading.current_thread().name)]
    assert got == {"router": "answered", "internet": "unreachable", "nameLookup": "ok"}
    assert watch._found == {"router": "answered", "internet": "unreachable", "nameLookup": "ok"}


def test_a_silent_router_ends_the_round():
    clock, tools, net = Clock(), PiTools(ping=1, neigh="FAILED"), Net()
    watch = LinkWatch(None, monotonic=clock)
    probe = _probe(watch, tools, net, clock)
    got = probe.round(_silence(watch))
    assert got == {"router": "silent"}
    assert [n for n, _ in tools.programs()] == ["ip", "ping", "ip"] and net.calls == [], "nothing behind a silent router"


@pytest.mark.parametrize("neigh,ping,expect", [("INCOMPLETE", 1, "silent"), ("REACHABLE", 1, "answered"), ("STALE", 1, "unknown"),
                                               ("DELAY", 0, "answered"), (None, 2, "unknown")])
def test_the_router_finding_from_the_ping_and_the_entry_after_it(neigh, ping, expect):
    clock, tools = Clock(), PiTools(ping=ping, neigh=neigh)
    watch = LinkWatch(None, monotonic=clock)
    assert _probe(watch, tools, Net(), clock).round(_silence(watch))["router"] == expect


def test_no_default_route_is_unknown_and_the_round_goes_on():
    clock, tools, net = Clock(), PiTools(route=""), Net()
    watch = LinkWatch(None, monotonic=clock)
    probe = _probe(watch, tools, net, clock)
    got = probe.round(_silence(watch))
    assert got == {"router": "unknown", "internet": "unknown", "nameLookup": "ok"}
    assert tools.programs() == [("ip", ["-4", "route", "show", "default"]), ("getent", ["ahosts", HOST])]
    assert net.calls == [], "no address known before this round's lookup"
    assert probe.round(watch.silence(), 2)["internet"] == "reachable", "the round's lookup gave the address"
    assert net.calls[-1][:2] == (ADDRESS, 443)


def test_the_cloud_address_is_the_last_successful_lookups_and_survives_a_failing_one():
    clock, tools, net = Clock(), PiTools(ahosts=AHOSTS_V6_FIRST), Net()
    watch = LinkWatch(None, monotonic=clock)
    probe = _probe(watch, tools, net, clock)
    watch.answered()
    probe.step()                                  # seeded after the first answer: the IPv4 address of an IPv6-first answer
    tools.getent = 2                              # then DNS fails …
    got = probe.round(_silence(watch, "dns"))
    assert got == {"router": "answered", "internet": "reachable", "nameLookup": "failed"}
    assert net.calls == [(ADDRESS, 443, 3.0, threading.current_thread().name)], "… and the internet is still tried"


def test_the_lookup_outside_a_silence_is_once_after_the_first_answer_then_hourly():
    clock, tools, net = Clock(), PiTools(getent=2), Net()
    watch = LinkWatch(None, monotonic=clock)
    probe = _probe(watch, tools, net, clock)
    probe.step()
    assert tools.calls == [], "nothing before the cloud has answered once"
    watch.answered()
    probe.step()                                  # fails: tried again a minute later, not before
    clock.t += 30
    probe.step()
    assert probe.lookups == 1
    tools.getent = 0
    clock.t += 31
    probe.step()
    assert probe.lookups == 2 and probe._address == ADDRESS
    clock.t += 1800
    probe.step()
    assert probe.lookups == 2, "fresh for an hour"
    clock.t += 1801
    probe.step()
    assert probe.lookups == 3
    assert {n for n, _ in tools.programs()} == {"getent"}, "outside a silence getent is the only program"
    assert net.calls == []


def test_no_third_party_host_is_ever_asked():
    clock, tools, net = Clock(), PiTools(ping=1, neigh="REACHABLE"), Net(ok=False)
    watch = LinkWatch(None, monotonic=clock)
    probe = _probe(watch, tools, net, clock)
    watch.answered()
    probe.step()
    s = _silence(watch)
    for n in range(1, 4):
        probe.round(s, n)
    hosts = {a for _, args in tools.programs() for a in args if "." in a or ":" in a}
    assert hosts <= {GW, HOST}, hosts
    assert {a for a, _, _, _ in net.calls} == {ADDRESS}


def test_the_endpoint_follows_the_reporters_url():
    assert cloud_endpoint(INGRESS) == (HOST, 443)
    assert cloud_endpoint("http://127.0.0.1:8099/telemetryIngress") == ("127.0.0.1", 8099)
    assert cloud_endpoint("https://[2001:db8::1]:8443/x") == ("2001:db8::1", 8443)
    assert cloud_endpoint("") is None and cloud_endpoint(None) is None and cloud_endpoint("not a url") is None


def test_a_new_host_is_looked_up_before_its_internet_is_tried():
    clock, tools, net = Clock(), PiTools(), Net()
    watch = LinkWatch(None, monotonic=clock)
    url = {"v": INGRESS}
    probe = NetworkProbe(watch, lambda: cloud_endpoint(url["v"]), run=tools.run, which_tool=tools.which, connect=net,
                         monotonic=clock, wait=clock.wait)
    watch.answered()
    probe.step()
    url["v"] = "https://europe-west1-agal-one-prod.cloudfunctions.net/telemetryIngress"
    got = probe.round(_silence(watch))
    assert got["internet"] == "unknown", "the old host's address is not the new host's"
    assert tools.programs()[-1] == ("getent", ["ahosts", "europe-west1-agal-one-prod.cloudfunctions.net"])


# ------------------------------------------------------------------------------------------------- 4. rate, bounds, hangs

def test_rounds_are_ten_seconds_apart_and_twelve_at_most_in_a_silence():
    clock, tools, net = Clock(), PiTools(), Net()
    watch = LinkWatch(None, monotonic=clock)
    starts: list = []
    probe = _probe(watch, tools, net, clock)
    real_round = probe.round

    def round_(silence, n=1):
        starts.append(clock.t)
        clock.t += 1.5  # a round takes a while (its programs)
        return real_round(silence, n)

    probe.round = round_
    first = _silence(watch)
    probe.step()                                   # the whole silence: it never ends here
    assert len(starts) == 12 and probe.rounds == 12
    assert all(b - a >= 10.0 for a, b in zip(starts, starts[1:])), starts
    probe.step()
    probe.step()
    assert len(starts) == 12, "twelve, and no more, for as long as that silence lasts"
    watch.answered()                               # back, and lost again at once
    second = _silence(watch)
    assert second == first + 1
    probe.step()
    assert len(starts) == 24 and starts[12] - starts[11] >= 10.0, "a new silence: ten seconds after the last round still"


def test_a_round_of_a_silence_that_ended_is_dropped():
    clock, tools = Clock(), PiTools()
    watch = LinkWatch(None, monotonic=clock)

    class AnswerMeanwhile(Net):
        def __call__(self, address, port, timeout):
            watch.answered()  # the cloud answers while the internet is being probed
            return super().__call__(address, port, timeout)

    net = AnswerMeanwhile(ok=False)
    probe = _probe(watch, tools, net, clock)
    watch.answered()
    probe.step()
    s = _silence(watch)
    tools.calls.clear()
    got = probe.round(s)
    assert got == {"router": "answered", "internet": "unreachable"}, "the lookup is not run once the silence is over"
    assert watch.found(s, router="silent") is False, "a finding for an ended silence is not taken"
    later = _silence(watch)
    assert watch._found == {"router": "unknown", "internet": "unknown", "nameLookup": "unknown"} and later == s + 1
    # A round of the old silence that ends while a NEW one lasts: its finding belongs to the old one — dropped.
    assert watch.found(s, router="silent", internet="unreachable") is False
    assert watch._found == {"router": "unknown", "internet": "unknown", "nameLookup": "unknown"}
    assert watch.found(later, router="answered") is True and watch._found["router"] == "answered"


def test_a_probe_that_will_not_die_starts_no_other_program():
    clock, net = Clock(), Net()
    tools = PiTools(hang="ping")
    tools.stuck = PiTools._Stuck()
    watch = LinkWatch(None, monotonic=clock)
    probe = _probe(watch, tools, net, clock)
    watch.answered()
    probe.step()
    s = _silence(watch)
    tools.calls.clear()
    got = probe.round(s, 1)
    assert [n for n, _ in tools.programs()] == ["ip", "ping"], "after the hung ping: neither ip neigh nor getent"
    assert got == {"router": "unknown", "internet": "reachable", "nameLookup": "unknown"}
    assert len(net.calls) == 1, "the connection (no program) is still tried, bounded"
    tools.calls.clear()
    probe.round(s, 2)
    assert tools.calls == [], "nothing is started while the hung one lives"
    tools.stuck.alive = False
    tools.hang = None
    probe.round(s, 3)
    assert [n for n, _ in tools.programs()] == ["ip", "ping", "ip", "getent"]


def test_a_lookup_killed_at_its_bound_is_failed():
    clock, tools, net = Clock(), PiTools(timeout="getent"), Net()
    watch = LinkWatch(None, monotonic=clock)
    probe = _probe(watch, tools, net, clock)
    assert probe.round(_silence(watch))["nameLookup"] == "failed"


def test_a_ping_that_does_not_end_is_killed_at_its_bound(tmp_path):
    """The real run_tool on a `ping` that sleeps: killed at its bound; the round ends within it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("ping", "sleep 30"), ("ip", f"case \"$2\" in route) printf '%s' '{ROUTE_WIFI}';; "
                                                  f"*) printf '%s' '{NEIGH['STALE']}';; esac"),
                       ("getent", f"printf '%s' '{AHOSTS_V4}'")):
        p = bin_dir / name
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(0o755)
    watch = LinkWatch(None)
    probe = NetworkProbe(watch, lambda: cloud_endpoint(INGRESS), which_tool=lambda n: str(bin_dir / n), connect=Net(),
                         ping_timeout_s=0.4)
    t0 = time.monotonic()
    got = probe.round(_silence(watch))
    took = time.monotonic() - t0
    assert took < 0.4 + net_care.KILL_WAIT_S + 1.0, took
    assert got == {"router": "unknown", "internet": "unknown", "nameLookup": "ok"}  # STALE after no answer: not known


def test_a_gap_with_no_probe_possible_ends_unknown(caplog):
    clock = Clock()
    tools, net = PiTools(missing=("ip", "ping", "getent")), Net()
    emitted: list = []
    watch = LinkWatch(emitted.append, monotonic=clock, now_ms=lambda: clock.t * 1000.0)
    probe = _probe(watch, tools, net, clock)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        watch.answered()
        probe.step()
        _silence(watch, "connect")
        probe.step()                               # twelve rounds, nothing to run
        clock.t += 61
        watch.answered()
    assert tools.calls == [] and net.calls == []
    gap = emitted[0]["payload"]["connectivity"]
    assert (gap["router"], gap["internet"], gap["nameLookup"], gap["cause"]) == ("unknown",) * 4
    missing = [r.getMessage() for r in caplog.records if "no `" in r.getMessage()]
    assert len(missing) == 2, missing  # ip and getent, said once each (ping is never reached without a route)


def test_nothing_is_probed_before_the_cloud_has_answered_once():
    clock, tools, net = Clock(), PiTools(), Net()
    watch = LinkWatch(None, monotonic=clock)
    probe = _probe(watch, tools, net, clock)
    watch.no_answer("dns")
    watch.no_answer("dns")
    assert watch.silence() is None
    probe.step()
    assert tools.calls == [] and net.calls == [] and probe.rounds == 0


# ------------------------------------------------------------------------------------------------- 5. threads

def test_the_probes_run_on_their_own_thread_and_never_delay_a_pass(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("ping", "sleep 30"), ("ip", f"case \"$2\" in route) printf '%s' '{ROUTE_WIFI}';; *) sleep 30;; esac"),
                       ("getent", "sleep 30")):
        p = bin_dir / name
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(0o755)
    seen: list = []

    def run(argv, timeout):
        seen.append(("program", threading.current_thread().name))
        return net_care.run_tool(argv, timeout)

    def connect(address, port, timeout):
        seen.append(("connect", threading.current_thread().name))
        time.sleep(min(timeout, 0.2))
        return False

    watch = LinkWatch(None)
    probe = NetworkProbe(watch, lambda: cloud_endpoint(INGRESS), run=run, which_tool=lambda n: str(bin_dir / n),
                         connect=connect, every_s=0.05, ip_timeout_s=0.2, ping_timeout_s=0.2, lookup_s=0.2, idle_s=0.2)
    probe._address, probe._address_host = ADDRESS, HOST
    watch.on_wake = probe.wake
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
    probe.start()
    try:
        watch.answered()
        t0 = time.monotonic()
        watch.no_answer("timeout")                 # a request's thread: wakes the probe, never waits for it
        assert time.monotonic() - t0 < 0.05
        deadline = time.monotonic() + 5
        while not any(w == "program" for w, _ in seen) and time.monotonic() < deadline:
            time.sleep(0.01)
        t0 = time.monotonic()
        assert S.switch(h, S.LIGHT, "device.power", 1)["accepted"]
        while h.io.output(S.LIGHT, "device.power") is not True and time.monotonic() - t0 < 2:
            time.sleep(0.005)
        assert time.monotonic() - t0 < 0.35, "the switch waited for a probe"
        mark = len(passes)
        time.sleep(1.5)                            # rounds of hung programs, killed at their bounds
        window = passes[mark:]
        gaps = [b - a for a, b in zip(window, window[1:])]
        assert len(window) >= 10 and max(gaps) < 0.35, f"passes stalled: {len(window)} passes, longest gap {max(gaps):.2f} s"
    finally:
        probe.stop()
        h.stop(safe_state=False)
    probe_threads = {n for w, n in seen if w in ("program", "connect")}
    assert probe_threads == {net_care.PROBE_THREAD}, probe_threads
    assert {n for w, n in seen if w == "pass"} <= set(RUNTIME_THREADS)
    assert probe.rounds >= 2


# ------------------------------------------------------------------------------------------------- 6. end to end, main()

class _Switch:
    """The local ingress, reachable or not: `down` sends the reporter to a dead port."""

    def __init__(self, rep: HttpReporter, ingress: _Ingress):
        self.rep, self.good, self.dead = rep, ingress.url, _dead_url()

    def down(self):
        self.rep.base_url = self.dead

    def up(self):
        self.rep.base_url = self.good


def test_the_gap_says_why_end_to_end():
    """The real reporter, watch, probe thread and node-event queue: the router answers, the cloud's address cannot be
    reached, its name does not resolve — the gap is posted once, saying the internet behind the router was down."""
    ingress = _Ingress()
    try:
        rep = HttpReporter(NODE, auth_token=TOKEN, base_url=ingress.url)
        mqtt = AgalOneMqttClient(MqttConfig(broker="broker.example", username="shared", status_topic="agal/n/status",
                                            commands_topic="agal/n/commands"))
        events = wire_node_events(mqtt, rep, offline=False, clock_ok=lambda: True)
        watch = LinkWatch(mqtt.publish_event, min_gap_s=0.6)
        rep.link_watch = watch
        tools, net = PiTools(getent=2), Net(ok=False)
        probe = NetworkProbe(watch, lambda: cloud_endpoint(INGRESS), run=tools.run, which_tool=tools.which, connect=net,
                             every_s=0.1, idle_s=0.2)
        probe._address, probe._address_host = ADDRESS, HOST  # looked up while the cloud answered
        watch.on_wake = probe.wake
        probe.start()
        sw = _Switch(rep, ingress)
        try:
            rep.report_status(online=True, uptime=1, firmware_version="0.4.3")      # answered
            sw.down()
            rep.report_status(online=True, uptime=2, firmware_version="0.4.3")      # no answer: connect
            deadline = time.monotonic() + 5
            while probe.rounds < 3 and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.5)
            sw.up()
            rep.report_status(online=True, uptime=3, firmware_version="0.4.3")      # answered: the gap ends
        finally:
            probe.stop()
        assert len(watch.gaps) == 1 and events.wait_idle(10.0)
        posted = [b for b in ingress.bodies if b.get("type") == "sensor_event"]
        assert len(posted) == 1 and len(posted[0]["payload"]["eventId"]) == 20
        gap = posted[0]["payload"]["event"]["payload"]["connectivity"]
        assert gap == watch.gaps[0]
        assert gap["failures"] == {"dns": 0, "connect": 1, "tls": 0, "timeout": 0, "other": 0}
        assert (gap["router"], gap["internet"], gap["nameLookup"], gap["cause"]) == ("answered", "unreachable", "failed", "internet")
        body = json.dumps(posted[0])
        assert "refused" not in body.lower() and "errno" not in body.lower(), "the error's own words stay on the node"
        assert {n for n, _, _, _ in tools.calls} <= {"ip", "ping", "getent"}
        assert {t for _, _, _, t in tools.calls} == {net_care.PROBE_THREAD}
        events.close(timeout=1.0)
    finally:
        ingress.close()


def test_wire_network_starts_the_probe_with_the_care_and_stops_it_with_it(tmp_path):
    sys_net = tmp_path / "sys_class_net"
    sys_net.mkdir()
    mqtt = AgalOneMqttClient(MqttConfig(broker="b", username="u", status_topic="s", commands_topic="c"))
    rep = HttpReporter(NODE, auth_token=TOKEN, base_url=INGRESS)
    care = net_care.wire_network(mqtt, rep, offline=False, clock_ok=lambda: True, sys_net=str(sys_net))
    try:
        probe = care.probe
        assert isinstance(probe, NetworkProbe) and rep.link_watch is probe.watch and rep.link_watch.on_wake == probe.wake
        assert probe._thread is not None and probe._thread.is_alive() and probe._thread.name == net_care.PROBE_THREAD
        assert probe._endpoint() == (HOST, 443)
        rep.base_url = "https://europe-west1-agal-one-prod.cloudfunctions.net/telemetryIngress"
        assert probe._endpoint()[0] == "europe-west1-agal-one-prod.cloudfunctions.net", "read at each probe"
    finally:
        care.stop()
    assert not probe._thread.is_alive()
    # No /sys/class/net (a Mac, a container): no care thread, so no probe thread either; offline: nothing.
    care2 = net_care.wire_network(mqtt, rep, offline=False, clock_ok=lambda: True, sys_net=str(tmp_path / "none"))
    assert care2.probe is None and rep.link_watch.on_wake is None
    care2.stop()
    assert net_care.wire_network(mqtt, rep, offline=True) is None


# ------------------------------------------------------------------------------------------------- 7. the contract (AJV)

def _gap_of(steps: list) -> list:
    emitted: list = []
    _replay(steps, emitted)
    return emitted


@needs_ajv
def test_every_gap_with_every_cause_is_a_valid_event_document(tmp_path):
    raised: list = []
    for case in V["gaps"]:
        raised += _gap_of(case["steps"])
    causes = {e["payload"]["connectivity"]["cause"] for e in raised}
    assert causes == set(net_care.CAUSES), causes
    assert all(set(FIVE) <= set(e["payload"]["connectivity"]) for e in raised)
    path = tmp_path / "events.json"
    path.write_text(json.dumps([{"type": e["type"], "source": e["source"], "payload": e["payload"]} for e in raised]))
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run(["node", os.path.join(here, "validate_events.mjs"), os.path.join(_contracts.CONTRACTS, "schemas"), str(path)],
                       capture_output=True, text=True, env=dict(os.environ, AJV_PATH=AJV), timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
