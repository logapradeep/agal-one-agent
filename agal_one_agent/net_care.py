"""The node's network: its Wi-Fi care, the ``network`` block of its heartbeat, and the gaps in its link to the cloud
(agent 0.4.2, contracts v1.16.0 — nodes/README.md §11; the reference is nodes/reference/network.mjs, the vectors
nodes/vectors/network.json, which tests/test_net_care.py runs this module against).

Why (founder, 2026-10-18). The Pump house — a Raspberry Pi 3B on Raspberry Pi OS (trixie, NetworkManager), agent 0.4.1
— went silent for about five minutes (10:40:13Z → 10:45:23Z) without restarting: no request reached the cloud, so it
lost its network. A Pi 3B's brcmfmac Wi-Fi power saving is a common cause of short drops, but the node reported nothing
about its Wi-Fi, so it could not be told from a router or internet drop.

Three parts, none of them on the runtime's thread or under its lock (programs/README.md §4.8: a pass never waits on the
network, the disk or a probe):

* ``NetworkCare`` — a thread of its own, ``net-care``. Every ``SAMPLE_S`` (5 s) it reads the link the node uses from
  files: an Ethernet interface that is up with carrier first, else the wireless one (/sys/class/net/*/wireless), its
  operstate, the kernel's count of the link going down (carrier_down_count) and, on Wi-Fi, the level from
  /proc/net/wireless (a file — no program is run for it). Every ``CHECK_S`` (60 s), and only while the link is Wi-Fi, it
  reads the driver's power saving with ``iw dev <if> get power_save``; when it is on, ``iw dev <if> set power_save off``
  and reads it back. NetworkManager may turn it on again at a reconnect: the next check turns it off again. Without
  ``iw``: ``iwconfig <if>`` and ``iwconfig <if> power off``. Without either: ``unknown``, logged once. A program runs
  with a timeout (``TOOL_TIMEOUT_S``); one that does not end is killed, and one that will not die is left — no other is
  started while it lives. It edits no file: nothing under /etc, no NetworkManager configuration. Any failure is logged
  (the same one once), never raised.
* ``NetworkCare.status()`` — the block the heartbeat carries as ``network``: the last sample and the last check, from
  memory (no I/O on the heartbeat's thread).
* ``LinkWatch`` — ``HttpReporter`` tells it of every answer from the cloud (any HTTP status: the request reached it) and
  every request that got none; at the first answer after the last one, when a request in between got none and the two
  answers are ``MIN_GAP_S`` (60 s) or more apart, it raises one ``connectivity_gap`` event through the node-event queue
  (``mqtt_client.publish_event`` → node_events.py: HTTPS, ordered, retried, with an eventId).

Agent 0.4.3 (contracts v1.17.0 — nodes/README.md §12; the reference's part F, the same vectors, which
tests/test_net_why.py runs this module against): WHY the cloud could not be reached. The Pump house (2026-10-22): from
16:18Z its link to the cloud dropped about once a minute for 25+ minutes, every gap said its Wi-Fi stayed connected
(−42 … −44 dBm, power saving off), and nothing said whether the router, the internet behind it, the name lookup or the
cloud had failed.

* ``failure_kind`` — ``HttpReporter._open`` hands the watch the KIND of every request that got no answer (dns, connect,
  tls, timeout, other), from the error's type; the error's text never leaves the node.
* ``NetworkProbe`` — a thread of its own, ``net-probe``, woken when a silence begins after an answer: while it lasts, one
  probe at a time, a round at most every ``PROBE_EVERY_S`` (10 s) and at most ``PROBE_ROUNDS`` (12) per silence — the
  router (``ip -4 route show default``, ``ping -n -c1 -W1 <gateway>``, ``ip neigh show <gateway>``), the internet behind
  it (a TCP connection, 3 s, to the cloud's last-known address on 443) and the name lookup (``getent ahosts <host>``,
  3 s). Every program an argv list, never a shell, bounded; nothing on the runtime's thread.
* The gap carries ``failures``, ``router``, ``internet``, ``nameLookup`` and ``cause`` — the decision table
  (``cause_of``).
"""

from __future__ import annotations

import errno
import logging
import math
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

THREAD = "net-care"
SAMPLE_S = 5.0
CHECK_S = 60.0
TOOL_TIMEOUT_S = 5.0
#: How long a killed program may take to end before it is left (it is in the kernel and cannot be reaped yet).
KILL_WAIT_S = 1.0
MIN_GAP_S = 60.0
SYS_NET = "/sys/class/net"
PROC_WIRELESS = "/proc/net/wireless"

#: nodes/reference/network.mjs — a dBm level, the WifiSignal thresholds, the power-saving words.
RSSI_MIN, RSSI_MAX = -120, -1
GOOD_AT, FAIR_AT = -60, -70
POWER_SAVE = ("off", "on", "unknown")
KINDS = ("wifi", "ethernet")
_IFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
#: Where iw and iwconfig live on Raspberry Pi OS (/usr/sbin), searched before the agent's own PATH.
TOOL_PATH = os.pathsep.join(["/usr/sbin", "/sbin", "/usr/bin", "/bin", "/usr/local/sbin", "/usr/local/bin"])

# Agent 0.4.3 (nodes/README.md §12; nodes/reference/network.mjs part F).
PROBE_THREAD = "net-probe"
#: enums.yaml#ConnectivityFailureKind, #RouterAnswer, #InternetReach, #NameLookupResult, #ConnectivityCause.
FAILURE_KINDS = ("dns", "connect", "tls", "timeout", "other")
ROUTER = ("answered", "silent", "unknown")
INTERNET = ("reachable", "unreachable", "unknown")
NAME_LOOKUP = ("ok", "failed", "unknown")
CAUSES = ("wifi", "router", "internet", "dns", "cloud", "unknown")
#: A probe round at most every 10 s, at most 12 rounds in one silence.
PROBE_EVERY_S = 10.0
PROBE_ROUNDS = 12
#: ping waits 1 s for its answer; the internet's TCP connect and the name lookup are bounded at 3 s.
PING_WAIT_S = 1
CONNECT_S = 3.0
LOOKUP_S = 3.0
#: The bounds of the programs a round runs: ``ip`` reads the kernel's tables and answers at once; ``ping`` has its 1 s.
IP_TIMEOUT_S = 2.0
PING_TIMEOUT_S = 3.0
#: The cloud's address is looked up again at most once an hour outside a silence (a round looks it up too).
LOOKUP_REFRESH_S = 3600.0
#: A first lookup that fails is tried again after this long (the cloud has answered, so it will resolve).
LOOKUP_RETRY_S = 60.0
#: The probe thread also wakes this often by itself (the hourly lookup).
PROBE_IDLE_S = 60.0
CLOUD_PORT = 443
#: The highest count of one failure kind a gap carries.
MAX_FAILURES = 1000000
NEIGH_STATES = ("PERMANENT", "NOARP", "REACHABLE", "STALE", "NONE", "INCOMPLETE", "DELAY", "PROBE", "FAILED")
_CONNECT_ERRNOS = frozenset(e for e in (getattr(errno, n, None) for n in (
    "ECONNREFUSED", "ENETUNREACH", "EHOSTUNREACH", "ENETDOWN", "EHOSTDOWN", "ETIMEDOUT", "EADDRNOTAVAIL")) if e is not None)
_IPV4_RE = re.compile(r"^(?:(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])$")
_IPV6_RE = re.compile(r"^(?=[^:]*:)[0-9A-Fa-f:.]{2,45}$")


# ------------------------------------------------------------------------------------------- the pure parts

def _is_rssi(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and RSSI_MIN <= v <= RSSI_MAX


_NUM_RE = re.compile(r"^(-?\d+)\.?$")


def parse_wireless(text: Optional[str]) -> dict:
    """/proc/net/wireless → {interface: {"rssiDbm": int | None, "quality": int | None}} (reference ``parseWireless``).
    The kernel prints one line per wireless interface under two header lines ("%6s: %04x  %3d%c  %3d%c  %3d%c …":
    name, status, link quality, level, noise …; "." after a value read just now). A cfg80211 driver — brcmfmac on a Pi
    3B — prints the level in dBm while associated and zeros when it is not."""
    out: dict = {}
    for line in str(text or "").split("\n"):
        if re.match(r"^\s*(Inter-\||face \|)", line):
            continue
        i = line.find(":")
        if i < 0:
            continue
        name = line[:i].strip()
        if not _IFACE_RE.match(name):
            continue
        fields = line[i + 1:].split()
        if len(fields) < 3:
            continue

        def num(tok: str) -> Optional[int]:
            m = _NUM_RE.match(tok)
            return int(m.group(1)) if m else None

        level = num(fields[2])
        out[name] = {"rssiDbm": level if _is_rssi(level) else None, "quality": num(fields[1])}
    return out


def signal_of(rssi_dbm: Any) -> Optional[str]:
    """enums.yaml#WifiSignal: good ≥ −60 dBm, fair −70 … −61, weak below −70 (reference ``signalOf``)."""
    if not _is_rssi(rssi_dbm):
        return None
    if rssi_dbm >= GOOD_AT:
        return "good"
    if rssi_dbm >= FAIR_AT:
        return "fair"
    return "weak"


def parse_iw_power_save(output: Optional[str]) -> Optional[str]:
    """``iw dev <if> get power_save`` → "on" | "off" | None ("Power save: on")."""
    m = re.search(r"Power save:\s*(on|off)\b", str(output or ""), re.I)
    return m.group(1).lower() if m else None


def parse_iwconfig_power(output: Optional[str]) -> Optional[str]:
    """``iwconfig <if>`` → "on" | "off" | None ("Power Management:on", or "Power Management=off")."""
    m = re.search(r"Power Management\s*[:=]\s*(on|off)\b", str(output or ""), re.I)
    return m.group(1).lower() if m else None


def iso(ms: float) -> str:
    """An instant in ms as the node writes it: 2026-10-18T10:45:23.500Z."""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def duration_words(seconds: float) -> str:
    """For the log only (the apps word the event themselves): 45 s, 5 min, 5 min 10 s, 1 h, 1 h 2 min."""
    t = max(0, int(seconds))
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    if t < 60:
        return f"{s} s"
    if t < 3600:
        return f"{m} min {s} s" if s else f"{m} min"
    return f"{h} h {m} min" if m else f"{h} h"


# ------------------------------------------------------------------------------------------- why (agent 0.4.3)

def failure_kind(exc: BaseException) -> str:
    """How a request got NO answer (enums.yaml#ConnectivityFailureKind; nodes/README.md §12 item 1), from the TYPE of
    the error ``urllib.request.urlopen`` raised — its text is read for one thing only, CPython's own words for a TLS
    handshake that timed out, and never leaves the node. ``urlopen`` wraps in ``URLError`` what fails while it opens the
    connection and sends the request (the name lookup, the TCP connect, the TLS handshake), and raises unwrapped what
    fails while it waits for the answer."""
    wrapped = isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError)
    reason = exc.reason if wrapped else exc
    if isinstance(reason, socket.gaierror):
        return "dns"
    if isinstance(reason, ssl.SSLError):
        return "tls" if wrapped else "other"  # after the handshake (reading the answer): not the handshake
    if isinstance(reason, (socket.timeout, TimeoutError)):
        if not wrapped:
            return "timeout"  # connected, the request sent, no answer in time
        return "tls" if "handshake" in str(reason).lower() else "connect"
    if wrapped and isinstance(reason, ConnectionRefusedError):
        return "connect"
    if wrapped and isinstance(reason, OSError) and getattr(reason, "errno", None) in _CONNECT_ERRNOS:
        return "connect"
    return "other"


def is_ipv4(v: Any) -> bool:
    return isinstance(v, str) and bool(_IPV4_RE.match(v))


def is_ip_address(v: Any) -> bool:
    return is_ipv4(v) or (isinstance(v, str) and bool(_IPV6_RE.match(v)))


def parse_default_route(text: Optional[str]) -> Optional[dict]:
    """``ip -4 route show default`` → {"gateway", "dev"} of the route the kernel uses, or None (no router to ask) —
    reference ``parseDefaultRoute``: the lowest metric (none: 0; the first of equals), a ``linkdown`` route only when no
    other is listed, a multipath route by its first nexthop; a route without ``via`` is not taken."""
    routes: list = []
    cur: Optional[dict] = None
    for raw in str(text or "").split("\n"):
        if not raw.strip():
            continue
        words = raw.split()

        def val(k: str) -> Optional[str]:
            i = words.index(k) if k in words else -1
            return words[i + 1] if 0 <= i < len(words) - 1 else None

        if raw[:1].isspace() and words[0] == "nexthop":
            if cur is not None and not cur["gateway"]:
                cur.update(gateway=val("via"), dev=val("dev"), linkdown="linkdown" in words)
            continue
        cur = None
        if words[0] != "default":
            continue
        try:
            metric = float(val("metric") or 0)
        except ValueError:
            metric = 0.0
        cur = {"gateway": val("via"), "dev": val("dev"), "metric": metric, "linkdown": "linkdown" in words}
        routes.append(cur)
    usable = [r for r in routes if is_ipv4(r["gateway"])]
    pool = [r for r in usable if not r["linkdown"]] or usable
    if not pool:
        return None
    best = pool[0]
    for r in pool[1:]:
        if r["metric"] < best["metric"]:
            best = r
    dev = best["dev"] if isinstance(best["dev"], str) and _IFACE_RE.match(best["dev"]) else None
    return {"gateway": best["gateway"], "dev": dev}


def parse_neigh_state(text: Optional[str], gateway: Optional[str], dev: Optional[str] = None) -> Optional[str]:
    """``ip neigh show <gateway>`` → the kernel's state of its entry for the router (REACHABLE, STALE, FAILED …), or None
    when it lists none — reference ``parseNeighState``: the gateway's line, on ``dev`` when given and listed; its state
    the last of its words that is one."""
    lines = [ln.split() for ln in str(text or "").split("\n")]
    lines = [w for w in lines if w and w[0] == gateway]
    on_dev = [w for w in lines if dev and "dev" in w and w.index("dev") + 1 < len(w) and w[w.index("dev") + 1] == dev] if dev else []
    pick = (on_dev or lines)[:1]
    if not pick:
        return None
    states = [w for w in pick[0] if w in NEIGH_STATES]
    return states[-1] if states else None


def router_of(gateway: Optional[str], ping: Optional[int], neigh: Optional[str]) -> str:
    """One round's router finding (reference ``routerOf``): no gateway → unknown; the ping answered (exit 0) → answered;
    else the entry read right after the ping: FAILED / INCOMPLETE → silent, REACHABLE → answered, else unknown."""
    if not is_ipv4(gateway):
        return "unknown"
    if ping == 0:
        return "answered"
    if neigh in ("FAILED", "INCOMPLETE"):
        return "silent"
    if neigh == "REACHABLE":
        return "answered"
    return "unknown"


def parse_ahosts(text: Optional[str]) -> list:
    """``getent ahosts <host>`` → the host's addresses, each once, in the order printed (reference ``parseAhosts``)."""
    out: list = []
    for line in str(text or "").split("\n"):
        words = line.split()
        if len(words) < 2 or words[1] not in ("STREAM", "DGRAM", "RAW") or not is_ip_address(words[0]):
            continue
        if words[0] not in out:
            out.append(words[0])
    return out


def cloud_address_of(addresses: Any) -> Optional[str]:
    """The address the internet probe connects to: the first IPv4 address, else the first (reference ``cloudAddressOf``)."""
    found = [a for a in (addresses if isinstance(addresses, list) else []) if is_ip_address(a)]
    return next((a for a in found if is_ipv4(a)), found[0] if found else None)


def name_lookup_of(exit_code: Optional[int], timed_out: bool = False, output: str = "") -> str:
    """One round's name-lookup finding (reference ``nameLookupOf``): killed at its bound → failed; exit 0 with an
    address → ok (with none read: unknown); exit 2 → failed; anything else → unknown."""
    if timed_out:
        return "failed"
    if exit_code == 0:
        return "ok" if parse_ahosts(output) else "unknown"
    if exit_code == 2:
        return "failed"
    return "unknown"


def internet_of(address: Optional[str], connected: Optional[bool]) -> str:
    """One round's internet finding (reference ``internetOf``)."""
    if not is_ip_address(address) or (connected is not True and connected is not False):
        return "unknown"
    return "reachable" if connected else "unreachable"


_WORSE = {"router": ("unknown", "answered", "silent"), "internet": ("unknown", "reachable", "unreachable"),
          "nameLookup": ("unknown", "ok", "failed")}


def merge_finding(what: str, a: Any, b: Any) -> str:
    """Two findings of one probe across a silence → the worse (reference ``mergeFinding``)."""
    order = _WORSE.get(what)
    if order is None:
        return "unknown"

    def rank(x: Any) -> int:
        return order.index(x) if x in order else 0

    return order[max(rank(a), rank(b))]


def stored_failures(v: Any) -> Optional[dict]:
    """``failures`` as the contract keeps it: the five counts, each a whole number 0 … MAX_FAILURES; else None."""
    if not isinstance(v, dict):
        return None
    out = {}
    for k in FAILURE_KINDS:
        n = v.get(k)
        if not isinstance(n, int) or isinstance(n, bool) or n < 0 or n > MAX_FAILURES:
            return None
        out[k] = n
    return out


def cause_of(c: Any) -> str:
    """THE DECISION TABLE (nodes/README.md §12 item 5; reference ``causeOf``), on a gap's own fields — the first row that
    matches: wifi (the Wi-Fi dropped); router (silent, the Wi-Fi up throughout — else unknown); internet (the router
    answered, the cloud's address unreachable — else unknown); dns (the address reached; the lookup failed, or every
    request failed at its own lookup); cloud (the address reached, the lookup ok); unknown."""
    x = c if isinstance(c, dict) else {}
    if x.get("kind") == "wifi" and x.get("wifiDropped") is True:
        return "wifi"
    if x.get("router") == "silent":
        return "router" if x.get("kind") == "wifi" and x.get("wifiConnectedThroughout") is True else "unknown"
    if x.get("internet") == "unreachable":
        return "internet" if x.get("router") == "answered" else "unknown"
    if x.get("internet") == "reachable":
        f = stored_failures(x.get("failures"))
        only_dns = bool(f and f["dns"] > 0 and f["connect"] + f["tls"] + f["timeout"] + f["other"] == 0)
        if x.get("nameLookup") == "failed" or only_dns:
            return "dns"
        if x.get("nameLookup") == "ok":
            return "cloud"
    return "unknown"


def cloud_endpoint(url: Any) -> Optional[tuple]:
    """(host, port) of the cloud endpoint a URL names — the ingress, which every heartbeat goes to; None when it names
    none. The port is the URL's, else 443 for https (80 for http, a test's local ingress)."""
    try:
        u = urlsplit(str(url or ""))
        host, port = u.hostname, u.port
    except ValueError:
        return None
    if not host:
        return None
    return host, port or (CLOUD_PORT if u.scheme == "https" else 80)


def tcp_connect(address: str, port: int, timeout: float) -> bool:
    """A TCP connection to a NUMERIC address, opened and closed at once — no name looked up, no TLS, nothing sent —
    bounded by ``timeout``: True when it opened; False when refused, unreachable or not answered in time."""
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    s = socket.socket(family, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect((address, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ------------------------------------------------------------------------------------------- running a program

class ToolTimeout(Exception):
    """The program did not end within its timeout; it was killed."""


class ToolHung(ToolTimeout):
    """The program did not end and did not die when killed (it is in the kernel): it is left, and ``process`` is it."""

    def __init__(self, message: str, process: subprocess.Popen):
        super().__init__(message)
        self.process = process


def _kill(p: subprocess.Popen) -> None:
    """SIGKILL to the program's whole process group (it was started in a session of its own): a child it started
    must not keep its output open after it."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        try:
            p.kill()
        except OSError:
            pass


def run_tool(argv: list, timeout: float) -> tuple:
    """Run a program, bounded by ``timeout``: (returncode, its output and errors). One that does not end is killed,
    with anything it started; one that will not die within ``KILL_WAIT_S`` (in the kernel) is left (``ToolHung``),
    our ends of its pipes closed — never waited for without a bound."""
    env = dict(os.environ, LC_ALL="C", PATH=TOOL_PATH + os.pathsep + os.environ.get("PATH", ""))
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         env=env, close_fds=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out or ""
    except subprocess.TimeoutExpired:
        _kill(p)
        try:
            p.communicate(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            for f in (p.stdout, p.stderr):
                try:
                    if f is not None:
                        f.close()
                except OSError:
                    pass
            raise ToolHung(f"{os.path.basename(str(argv[0]))} did not end in {timeout:g} s and did not die when killed", p) from None
        raise ToolTimeout(f"{os.path.basename(str(argv[0]))} did not end in {timeout:g} s (killed)") from None


def which(name: str) -> Optional[str]:
    return shutil.which(name, path=TOOL_PATH + os.pathsep + os.environ.get("PATH", ""))


# ------------------------------------------------------------------------------------------- the link

@dataclass
class LinkSample:
    """The link as the node read it at ``at_ms`` (the reference's sample: kind, interface, connected, rssiDbm,
    powerSave, carrierDownCount)."""

    kind: Optional[str]
    interface: Optional[str]
    connected: bool
    rssi_dbm: Optional[int]
    power_save: Optional[str]
    carrier_down_count: Optional[int]
    at_ms: float

    @classmethod
    def from_reference(cls, s: dict, at_ms: float = 0.0) -> "LinkSample":
        return cls(kind=s.get("kind"), interface=s.get("interface"), connected=s.get("connected") is True,
                   rssi_dbm=s.get("rssiDbm"), power_save=s.get("powerSave"), carrier_down_count=s.get("carrierDownCount"),
                   at_ms=at_ms)


def _wall_ms() -> float:
    return time.time() * 1000.0


class NetworkCare:
    """The ``net-care`` thread (see the module docstring). ``step()`` is one turn — the tests drive it."""

    def __init__(self, *, sys_net: str = SYS_NET, proc_wireless: str = PROC_WIRELESS,
                 run: Optional[Callable[[list, float], tuple]] = None, which_tool: Optional[Callable[[str], Optional[str]]] = None,
                 monotonic: Callable[[], float] = time.monotonic, now_ms: Optional[Callable[[], float]] = None,
                 sample_s: float = SAMPLE_S, check_s: float = CHECK_S, tool_timeout_s: float = TOOL_TIMEOUT_S,
                 on_sample: Optional[Callable[[LinkSample], None]] = None):
        self.sys_net = sys_net
        self.proc_wireless = proc_wireless
        self._run_tool = run or run_tool
        self._which = which_tool or which
        self._monotonic = monotonic
        self._now_ms = now_ms or _wall_ms
        self.sample_s = sample_s
        self.check_s = check_s
        self.tool_timeout_s = tool_timeout_s
        self.on_sample = on_sample
        #: The LinkWatch the samples feed (wire_network sets it; the tests read it).
        self.watch: Optional["LinkWatch"] = None
        #: Agent 0.4.3: the probe thread started with this one (wire_network), stopped with it.
        self.probe: Optional["NetworkProbe"] = None
        self._lock = threading.Lock()
        self._snapshot: Optional[dict] = None
        self._ps_iface: Optional[str] = None      # the interface the power-saving state below is about
        self._power_save: Optional[str] = None    # on | off | unknown, at the last check
        self._held = False                        # the agent turned it off and it read off
        self._next_check = -math.inf
        self._hung: Optional[subprocess.Popen] = None
        self._logged: set = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Counters (the tests and a debugging session read them).
        self.samples = 0
        self.checks = 0
        self.turned_off = 0

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> bool:
        """Start the thread; False (nothing to watch) when this board has no /sys/class/net (macOS, a container
        without it)."""
        if not os.path.isdir(self.sys_net):
            logger.info("network care: no %s on this board — the link is not watched (gaps are still reported)", self.sys_net)
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=THREAD, daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self.probe is not None:
            self.probe.stop(timeout=timeout)
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.step()
            self._stop.wait(self.sample_s)

    # ------------------------------------------------------------------ what the heartbeat carries

    def status(self) -> Optional[dict]:
        """The ``network`` block of the next heartbeat (iot-node.schema.json network, without reportedAt), or None
        when no link was ever read. From memory: no file, no program."""
        with self._lock:
            return dict(self._snapshot) if self._snapshot else None

    # ------------------------------------------------------------------ one turn

    def step(self) -> Optional[LinkSample]:
        try:
            kind, iface, connected, rssi, down_count = self._read_link()
            at_ms = self._now_ms()  # when the link was read — a check below may take up to its timeout
            if kind == "wifi":
                if iface != self._ps_iface:
                    self._ps_iface, self._power_save, self._held, self._next_check = iface, None, False, -math.inf
                now = self._monotonic()
                if now >= self._next_check:
                    self._next_check = now + self.check_s
                    self._check_power_save(iface)
            sample = LinkSample(kind=kind, interface=iface, connected=connected, rssi_dbm=rssi if kind == "wifi" else None,
                                power_save=(self._power_save or "unknown") if kind == "wifi" else None,
                                carrier_down_count=down_count, at_ms=at_ms)
            block = self._block(sample)
            with self._lock:
                self._snapshot = block
            self.samples += 1
            if self.on_sample is not None:
                self.on_sample(sample)
            return sample
        except Exception as e:  # noqa: BLE001 — the care never takes the agent down
            self._log_once(("step", type(e).__name__), logging.WARNING, "network care: %s", e)
            return None

    def _block(self, s: LinkSample) -> Optional[dict]:
        if s.kind not in KINDS or not s.interface:
            return None
        b: dict = {"interface": s.interface, "kind": s.kind, "connected": bool(s.connected)}
        if s.kind == "wifi":
            if s.connected and _is_rssi(s.rssi_dbm):
                b["rssiDbm"] = s.rssi_dbm
                b["signal"] = signal_of(s.rssi_dbm)
            ps = self._power_save if self._power_save in POWER_SAVE else "unknown"
            b["powerSave"] = ps
            b["powerSaveHeld"] = bool(self._held and ps == "off")
        b["at"] = iso(s.at_ms)
        return b

    # ------------------------------------------------------------------ the link, from files

    def _read_text(self, path: str) -> Optional[str]:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:  # gone, not readable, or (carrier of an interface that is down) EINVAL
            return None

    def _attr(self, iface: str, name: str) -> Optional[str]:
        v = self._read_text(os.path.join(self.sys_net, iface, name))
        return v.strip() if v is not None else None

    def _interfaces(self) -> tuple:
        try:
            names = sorted(os.listdir(self.sys_net))
        except OSError:
            return [], []
        ethernet, wireless = [], []
        for n in names:
            if n == "lo" or not _IFACE_RE.match(n):
                continue
            d = os.path.join(self.sys_net, n)
            if os.path.isdir(os.path.join(d, "wireless")) or os.path.exists(os.path.join(d, "phy80211")):
                wireless.append(n)
            elif self._attr(n, "type") == "1" and os.path.exists(os.path.join(d, "device")):
                ethernet.append(n)  # a board's own or a USB adapter — not a bridge, a veth or a tunnel (no device)
        wireless.sort(key=lambda n: (n != "wlan0", n))
        return ethernet, wireless

    def _down_count(self, iface: str) -> Optional[int]:
        for name in ("carrier_down_count", "carrier_changes"):
            v = self._attr(iface, name)
            if v is not None and v.isdigit():
                return int(v)
        return None

    def _read_link(self) -> tuple:
        """(kind, interface, connected, rssiDbm, carrierDownCount) of the link in use (nodes/README.md §11 item 1)."""
        ethernet, wireless = self._interfaces()
        for n in ethernet:
            if self._attr(n, "carrier") == "1" and self._attr(n, "operstate") in ("up", "unknown", None):
                return "ethernet", n, True, None, self._down_count(n)
        if wireless:
            n = wireless[0]
            rssi = parse_wireless(self._read_text(self.proc_wireless)).get(n, {}).get("rssiDbm")
            state = self._attr(n, "operstate")
            connected = state == "up" if state not in (None, "unknown") else rssi is not None
            return "wifi", n, connected, rssi if connected else None, self._down_count(n)
        if ethernet:
            n = ethernet[0]
            return "ethernet", n, False, None, self._down_count(n)
        return None, None, False, None, None

    # ------------------------------------------------------------------ the power saving (nodes/README.md §11 item 2)

    def _check_power_save(self, iface: str) -> None:
        self.checks += 1
        state, tool, why = self._read_power_save(iface)
        if state == "on":
            done, err = self._turn_off(iface, tool)
            after, _, why_after = self._read_power_save(iface, prefer=tool)
            if after == "off":
                if self._held:
                    logger.info("Wi-Fi power saving on %s was on again (NetworkManager turns it on at a reconnect) — "
                                "turned off again", iface)
                else:
                    logger.info("Wi-Fi power saving on %s was on — turned off with %s; the agent keeps it off "
                                "(checked every %g s)", iface, tool, self.check_s)
                self._held = True
                self.turned_off += 1
                self._power_save = "off"
                return
            self._held = False
            # Still on when the turning off failed; not known when it was taken but cannot be read back.
            self._power_save = after or ("unknown" if done else "on")
            self._log_once(("set", iface, tool, err or why_after), logging.WARNING,
                           "Wi-Fi power saving on %s is on and could not be turned off with %s (%s); tried again in %g s",
                           iface, tool, err or why_after or f"it still reads {after}", self.check_s)
            return
        if state == "off":
            self._power_save = "off"  # held stays as it was: the agent's own turning off, if it was that
            return
        self._held = False
        self._power_save = "unknown"
        self._log_once(("read", iface, why), logging.WARNING,
                       "Wi-Fi power saving on %s cannot be read (%s): reported unknown", iface, why)

    def _tools(self, prefer: Optional[str]) -> list:
        found = [(name, self._which(name)) for name in ("iw", "iwconfig")]
        found = [(n, p) for n, p in found if p]
        if prefer:
            found.sort(key=lambda t: t[0] != prefer)
        return found

    def _read_power_save(self, iface: str, prefer: Optional[str] = None) -> tuple:
        """(on | off | None, the tool that read it, why not)."""
        tools = self._tools(prefer)
        if not tools:
            return None, None, "neither iw nor iwconfig is installed"
        why = []
        for name, path in tools:
            argv = [path, "dev", iface, "get", "power_save"] if name == "iw" else [path, iface]
            rc, out, err = self._run(argv)
            if err:
                why.append(f"{name}: {err}")
                continue
            state = (parse_iw_power_save if name == "iw" else parse_iwconfig_power)(out) if rc == 0 else None
            if state:
                return state, name, None
            why.append(f"{name}: {(out or '').strip()[:120] or f'exit {rc}'}")
        return None, None, "; ".join(why)

    def _turn_off(self, iface: str, tool: Optional[str]) -> tuple:
        path = self._which(tool) if tool else None
        if not path:
            return False, f"{tool or 'no tool'} is not installed"
        argv = [path, "dev", iface, "set", "power_save", "off"] if tool == "iw" else [path, iface, "power", "off"]
        rc, out, err = self._run(argv)
        if err:
            return False, err
        if rc != 0:
            return False, (out or "").strip()[:160] or f"exit {rc}"
        return True, None

    def _run(self, argv: list) -> tuple:
        """(returncode, output, why it did not run) — never raises, never starts a program while one left hung lives."""
        if self._hung is not None:
            if self._hung.poll() is None:
                return None, "", "an earlier call has not ended"
            self._hung = None
        try:
            rc, out = self._run_tool(argv, self.tool_timeout_s)
            return rc, out, None
        except ToolHung as e:
            self._hung = e.process
            return None, "", str(e)
        except ToolTimeout as e:
            return None, "", str(e)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            return None, "", f"{os.path.basename(str(argv[0]))}: {e}"

    def _log_once(self, key: tuple, level: int, msg: str, *args) -> None:
        if key in self._logged:
            logger.debug(msg, *args)
            return
        self._logged.add(key)
        logger.log(level, msg, *args)


# ------------------------------------------------------------------------------------------- the gaps

#: The log's words for a cause (the apps' own: notifications/catalog.json 1.7.0 `notices`).
_CAUSE_WORDS = {
    "router": "its Wi-Fi stayed connected, but its router stopped answering",
    "internet": "its router answered, but the internet behind it was down",
    "dns": "the internet worked, but names could not be looked up (DNS)",
    "cloud": "its internet worked, but Agal One's cloud did not answer",
}


class LinkWatch:
    """The node's answers from the cloud, and the gaps between them (nodes/README.md §11 item 4; reference
    ``watchGaps``). ``answered`` / ``no_answer`` are called by ``HttpReporter`` from whichever thread posted; they do
    arithmetic under a lock and nothing else, and a gap is handed to ``emit`` (the node-event queue) outside it.

    Agent 0.4.3 (§12; reference ``watchGaps`` with ``diagnose``): ``no_answer(kind)`` counts the request by how it
    failed; a silence that begins after an answer gets a number and wakes ``on_wake`` (``NetworkProbe.wake``: an Event,
    set outside the lock — a request's thread never waits for a probe); ``found`` takes a probe round's findings for
    that silence only, the worse finding of each probe kept; the gap carries failures, router, internet, nameLookup and
    cause (``cause_of``)."""

    def __init__(self, emit: Optional[Callable[[dict], None]] = None, *, monotonic: Callable[[], float] = time.monotonic,
                 now_ms: Optional[Callable[[], float]] = None, min_gap_s: float = MIN_GAP_S,
                 on_wake: Optional[Callable[[], None]] = None):
        self.emit = emit
        self._monotonic = monotonic
        self._now_ms = now_ms or _wall_ms
        self.min_gap_s = min_gap_s
        #: Told when a silence begins after an answer, and at the first answer since the start (wire_network: the
        #: probe thread's wake).
        self.on_wake = on_wake
        self._lock = threading.Lock()
        self._last: Optional[tuple] = None   # (monotonic s, ms) of the last answer
        self._missed = False
        self._base: Optional[LinkSample] = None
        self._latest: Optional[LinkSample] = None
        self._samples = 0
        self._down = False
        self._rssi_before: Optional[int] = None
        self._ps_before: Optional[str] = None
        self._silence = 0                    # the number of the silence that lasts (or lasted last)
        self._failures = dict.fromkeys(FAILURE_KINDS, 0)
        self._found = {"router": "unknown", "internet": "unknown", "nameLookup": "unknown"}
        self.gaps: list = []  # every gap raised (the tests read it)

    def sample(self, s: LinkSample) -> None:
        with self._lock:
            self._latest = s
            if s.kind == "wifi":
                self._samples += 1
                if not s.connected:
                    self._down = True
                if not self._missed:
                    if s.power_save in POWER_SAVE:
                        self._ps_before = s.power_save
                    if s.connected and _is_rssi(s.rssi_dbm):
                        self._rssi_before = s.rssi_dbm

    def no_answer(self, kind: Optional[str] = None) -> None:
        """A request got no answer; ``kind`` how (``failure_kind``) — anything else counts as other."""
        began = False
        with self._lock:
            if not self._missed and self._last is not None:
                self._silence += 1
                began = True
            self._missed = True
            k = kind if kind in FAILURE_KINDS else "other"
            self._failures[k] = min(self._failures[k] + 1, MAX_FAILURES)
        if began:
            self._wake()

    def silence(self) -> Optional[int]:
        """The number of the silence that lasts now — after an answer — or None (the probes run only then)."""
        with self._lock:
            return self._silence if self._missed and self._last is not None else None

    def has_answered(self) -> bool:
        with self._lock:
            return self._last is not None

    def found(self, silence: int, router: Optional[str] = None, internet: Optional[str] = None,
              name_lookup: Optional[str] = None) -> bool:
        """A probe round's findings for silence number ``silence``: taken only while that silence lasts (a round that
        ends after the answer is dropped), each probe's worse finding kept (``merge_finding``). True when taken."""
        with self._lock:
            if not (self._missed and self._last is not None and silence == self._silence):
                return False
            for what, v in (("router", router), ("internet", internet), ("nameLookup", name_lookup)):
                if v is not None:
                    self._found[what] = merge_finding(what, self._found[what], v)
            return True

    def answered(self) -> Optional[dict]:
        gap = None
        with self._lock:
            t = self._monotonic()
            at = self._now_ms()
            first = self._last is None
            if self._last is not None and self._missed and t - self._last[0] >= self.min_gap_s:
                gap = self._gap_locked(t, at)
                self.gaps.append(gap)
            self._last = (t, at)
            self._missed = False
            self._samples = 0
            self._down = False
            self._base = self._latest
            self._failures = dict.fromkeys(FAILURE_KINDS, 0)
            self._found = {"router": "unknown", "internet": "unknown", "nameLookup": "unknown"}
        if first:
            self._wake()  # the probe thread may look the cloud's address up now (its first lookup)
        if gap is not None:
            self._raise(gap)
        return gap

    def _wake(self) -> None:
        if self.on_wake is None:
            return
        try:
            self.on_wake()
        except Exception as e:  # noqa: BLE001 — a probe never fails the request that woke it
            logger.debug("network probe not woken: %s", e)

    def _gap_locked(self, t: float, at: float) -> dict:
        link = self._base or self._latest
        gap: dict = {"from": iso(self._last[1]), "to": iso(at), "seconds": int(math.floor(t - self._last[0]))}
        if link is not None and link.interface and _IFACE_RE.match(str(link.interface)):
            gap["interface"] = link.interface
        if link is not None and link.kind in KINDS:
            gap["kind"] = link.kind
        if gap.get("kind") == "wifi":
            base, latest = self._base, self._latest
            same = bool(base and latest and base.interface == latest.interface)
            counted = same and isinstance(base.carrier_down_count, int) and isinstance(latest.carrier_down_count, int)
            rose = bool(counted and latest.carrier_down_count > base.carrier_down_count)
            known = self._samples > 0 or counted
            dropped = (self._down or rose) if known else None
            gap.update({"wifiConnectedThroughout": None if dropped is None else not dropped, "wifiDropped": dropped,
                        "rssiDbmBefore": self._rssi_before, "signalBefore": signal_of(self._rssi_before),
                        "powerSave": self._ps_before or "unknown"})
        else:
            gap.update({"wifiConnectedThroughout": None, "wifiDropped": None, "rssiDbmBefore": None,
                        "signalBefore": None, "powerSave": None})
        # Agent 0.4.3 (§12): why — the requests' failures, the probes' findings, the decision table.
        gap["failures"] = dict(self._failures)
        gap.update(self._found)
        gap["cause"] = cause_of(gap)
        return gap

    def _raise(self, gap: dict) -> None:
        cause = gap.get("cause")
        if cause in _CAUSE_WORDS:
            how = _CAUSE_WORDS[cause]
        elif cause == "wifi" or (cause is None and gap.get("wifiDropped") is True):
            how = f"its Wi-Fi dropped (signal {gap.get('signalBefore') or 'unknown'}" + (
                f", {gap['rssiDbmBefore']} dBm)" if gap.get("rssiDbmBefore") is not None else ")")
        elif gap.get("wifiConnectedThroughout") is True:
            how = "its Wi-Fi stayed connected (the router or the internet dropped)"
        elif gap.get("kind") == "ethernet":
            how = "it is on Ethernet"
        else:
            how = "its link could not be read"
        if cause == "unknown":
            how += "; the probes could not tell why"
        found = ""
        if "router" in gap:
            counts = ", ".join(f"{n} {k}" for k, n in (gap.get("failures") or {}).items() if n)
            found = (f" (router {gap['router']}, internet {gap['internet']}, name lookup {gap['nameLookup']}; "
                     f"no answer: {counts or 'none counted'})")
        logger.warning("no answer from the cloud for %s (%s → %s); %s — reported as connectivity_gap%s",
                       duration_words(gap["seconds"]), gap["from"], gap["to"], how, found)
        if self.emit is None:
            return
        try:
            self.emit({"type": "connectivity_gap", "source": "node", "payload": {"connectivity": dict(gap)}})
        except Exception as e:  # noqa: BLE001 — reporting never fails the request that ended the gap
            logger.error("connectivity_gap not queued: %s", e)


# ------------------------------------------------------------------------------------------- the probes (agent 0.4.3)

class NetworkProbe:
    """The ``net-probe`` thread (nodes/README.md §12 item 2): while a silence lasts after an answer, one probe at a time,
    a round at most every ``every_s`` (10 s) and at most ``max_rounds`` (12) per silence — the router, then (unless it was
    silent) the internet behind it and the name lookup — each finding handed to the ``LinkWatch`` as soon as it is known.
    Outside a silence it runs one program only: ``getent ahosts <host>``, once after the cloud first answers and then at
    most once an hour, to keep the cloud's last-known address for the internet probe. Never on the runtime's thread;
    every program an argv list with a bound, killed with its process group when it does not end; one that will not die
    is left and no program is started while it lives. ``step()`` is one wake and ``round()`` one round — the tests drive
    them."""

    def __init__(self, watch: LinkWatch, endpoint: Callable[[], Optional[tuple]], *,
                 run: Optional[Callable[[list, float], tuple]] = None,
                 which_tool: Optional[Callable[[str], Optional[str]]] = None,
                 connect: Optional[Callable[[str, int, float], bool]] = None,
                 monotonic: Callable[[], float] = time.monotonic, wait: Optional[Callable[[float], bool]] = None,
                 every_s: float = PROBE_EVERY_S, max_rounds: int = PROBE_ROUNDS, refresh_s: float = LOOKUP_REFRESH_S,
                 retry_s: float = LOOKUP_RETRY_S, idle_s: float = PROBE_IDLE_S, ip_timeout_s: float = IP_TIMEOUT_S,
                 ping_timeout_s: float = PING_TIMEOUT_S, connect_s: float = CONNECT_S, lookup_s: float = LOOKUP_S):
        self.watch = watch
        self._endpoint = endpoint
        self._run_tool = run or run_tool
        self._which = which_tool or which
        self._connect = connect or tcp_connect
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._wake = threading.Event()
        #: Waits up to its argument; True when the thread is being stopped (the tests pass a clock's).
        self._wait = wait or self._stop.wait
        self.every_s, self.max_rounds = every_s, max_rounds
        self.refresh_s, self.retry_s, self.idle_s = refresh_s, retry_s, idle_s
        self.ip_timeout_s, self.ping_timeout_s = ip_timeout_s, ping_timeout_s
        self.connect_s, self.lookup_s = connect_s, lookup_s
        self._hung: Optional[subprocess.Popen] = None
        self._last_round = -math.inf          # when the last round began (monotonic): every_s between rounds
        self._probed: Optional[tuple] = None  # (silence number, rounds made in it)
        self._address: Optional[str] = None   # the cloud's last-known address …
        self._address_host: Optional[str] = None  # … of this host
        self._looked_up_at = -math.inf        # when a lookup last gave it
        self._next_attempt = -math.inf        # outside a silence: no lookup before this
        self._logged: set = set()
        self._thread: Optional[threading.Thread] = None
        # Counters and the last findings (the tests and a debugging session read them).
        self.rounds = 0
        self.lookups = 0
        self.findings: list = []

    # ------------------------------------------------------------------ lifecycle

    def wake(self) -> None:
        """A silence began, or the cloud answered for the first time: called from a request's thread — sets an Event."""
        self._wake.set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=PROBE_THREAD, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.idle_s)
            self._wake.clear()
            if self._stop.is_set():
                break
            self.step()

    # ------------------------------------------------------------------ one wake

    def step(self) -> None:
        """The rounds of the silence that lasts now, or, when none does, the address lookup if one is due."""
        try:
            silence = self.watch.silence()
            if silence is not None:
                self._probe_silence(silence)
            elif self.watch.has_answered():
                self._lookup_outside_silence()
        except Exception as e:  # noqa: BLE001 — the probes never take the agent down
            self._log_once(("step", type(e).__name__), logging.WARNING, "network probe: %s", e)

    def _probe_silence(self, silence: int) -> None:
        made = self._probed[1] if self._probed is not None and self._probed[0] == silence else 0
        while made < self.max_rounds and not self._stop.is_set():
            delay = self._last_round + self.every_s - self._monotonic()
            if delay > 0 and self._wait(delay):
                return  # being stopped
            if self.watch.silence() != silence:
                return  # it ended (or another began: the next wake takes it)
            self._last_round = self._monotonic()
            made += 1
            self._probed = (silence, made)
            self.round(silence, made)

    def round(self, silence: int, n: int = 1) -> dict:
        """One round: the router; when it was not silent, the internet and the name lookup. Each finding goes to the
        watch for ``silence`` as soon as it is known; the rest of a round whose silence ended is not run."""
        self.rounds += 1
        ep = self._endpoint()
        got: dict = {"router": self._router()}
        self.watch.found(silence, router=got["router"])
        if got["router"] != "silent" and self.watch.silence() == silence:
            got["internet"] = self._internet(ep)
            self.watch.found(silence, internet=got["internet"])
            if self.watch.silence() == silence:
                got["nameLookup"] = self._lookup(ep)
                self.watch.found(silence, name_lookup=got["nameLookup"])
        self.findings.append({"silence": silence, "round": n, **got})
        del self.findings[:-50]
        logger.debug("network probe, silence %d round %d: %s", silence, n, got)
        return got

    # ------------------------------------------------------------------ the probes

    def _router(self) -> str:
        ip = self._which("ip")
        if not ip:
            self._log_once(("missing", "ip"), logging.WARNING,
                           "network probe: no `ip` on this board — its router is not probed (reported unknown)")
            return "unknown"
        rc, out, why, _ = self._run([ip, "-4", "route", "show", "default"], self.ip_timeout_s)
        route = parse_default_route(out) if rc == 0 and why is None else None
        if route is None:
            return "unknown"  # no default route (or ip could not tell): no router to ask
        gateway, dev = route["gateway"], route["dev"]
        ping_rc = None
        ping = self._which("ping")
        if ping:
            rc, _out, why, _ = self._run([ping, "-n", "-c1", f"-W{PING_WAIT_S}", gateway], self.ping_timeout_s)
            ping_rc = rc if why is None else None
        else:
            self._log_once(("missing", "ping"), logging.WARNING,
                           "network probe: no `ping` on this board — its router is judged by the kernel's neighbour entry alone")
        rc, out, why, _ = self._run([ip, "neigh", "show", gateway], self.ip_timeout_s)
        neigh = parse_neigh_state(out, gateway, dev) if rc == 0 and why is None else None
        return router_of(gateway, ping_rc, neigh)

    def _internet(self, ep: Optional[tuple]) -> str:
        if ep is None:
            return "unknown"
        host, port = ep
        address = self._address if self._address_host == host else None
        if address is None:
            return "unknown"  # no lookup has given the cloud's address yet
        try:
            connected: Optional[bool] = bool(self._connect(address, port, self.connect_s))
        except Exception as e:  # noqa: BLE001
            self._log_once(("connect", type(e).__name__), logging.WARNING, "network probe: the connection could not be tried: %s", e)
            connected = None
        return internet_of(address, connected)

    def _lookup(self, ep: Optional[tuple]) -> str:
        if ep is None:
            return "unknown"
        host = ep[0]
        getent = self._which("getent")
        if not getent:
            self._log_once(("missing", "getent"), logging.WARNING,
                           "network probe: no `getent` on this board — the name lookup is not probed (reported unknown)")
            return "unknown"
        rc, out, why, timed_out = self._run([getent, "ahosts", host], self.lookup_s)
        finding = name_lookup_of(rc if why is None else None, timed_out, out)
        if finding == "ok":
            address = cloud_address_of(parse_ahosts(out))
            if address:
                self._address, self._address_host = address, host
                self._looked_up_at = self._monotonic()
        return finding

    def _lookup_outside_silence(self) -> None:
        ep = self._endpoint()
        if ep is None:
            return
        now = self._monotonic()
        have = self._address is not None and self._address_host == ep[0]
        if (have and now - self._looked_up_at < self.refresh_s) or now < self._next_attempt:
            return
        self.lookups += 1
        if self._lookup(ep) != "ok":
            self._next_attempt = now + self.retry_s

    def _run(self, argv: list, timeout: float) -> tuple:
        """(returncode, output, why it did not run or end, killed at its bound) — never raises; never starts a program
        while one left hung lives."""
        if self._hung is not None:
            if self._hung.poll() is None:
                return None, "", "an earlier probe has not ended", False
            self._hung = None
        name = os.path.basename(str(argv[0]))
        try:
            rc, out = self._run_tool(argv, timeout)
            return rc, out or "", None, False
        except ToolHung as e:
            self._hung = e.process
            self._log_once(("hung", name), logging.WARNING, "network probe: %s — no probe runs a program until it ends", e)
            return None, "", str(e), False
        except ToolTimeout as e:
            return None, "", str(e), True
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            self._log_once(("run", name, type(e).__name__), logging.WARNING, "network probe: %s: %s", name, e)
            return None, "", f"{name}: {e}", False

    def _log_once(self, key: tuple, level: int, msg: str, *args) -> None:
        if key in self._logged:
            logger.debug(msg, *args)
            return
        self._logged.add(key)
        logger.log(level, msg, *args)


# ------------------------------------------------------------------------------------------- main.py

def with_network(extra: Optional[dict], care: Optional[NetworkCare]) -> Optional[dict]:
    """The heartbeat's extra fields with the ``network`` block added when there is one (main.py)."""
    block = care.status() if care is not None else None
    if not block:
        return extra
    out = dict(extra or {})
    out["network"] = block
    return out


def wire_network(mqtt_client, reporter, *, offline: bool, clock_ok: Optional[Callable[[], bool]] = None,
                 server_time=None, sys_net: str = SYS_NET) -> Optional[NetworkCare]:
    """main.py: a ``LinkWatch`` on the reporter (every answer and every request that got none, with how it failed), whose
    gaps go to ``mqtt_client.publish_event`` (the node-event queue); the ``NetworkCare`` thread feeding it the link, and
    (agent 0.4.3) the ``NetworkProbe`` thread probing while a silence lasts — both started when the board has
    /sys/class/net, the probe stopped with the care (``care.stop()``). The probes' cloud is the host of the reporter's
    ingress URL. Offline (no cloud): nothing, None. tests/test_net_care.py checks main() makes this call."""
    if offline:
        return None
    trusted = None
    if server_time is not None:
        from .server_time import trusted_now
        if clock_ok is None:
            from .blocks.clock import SystemClock
            clock_ok = SystemClock().clock_ok
        trusted = trusted_now(server_time, clock_ok)

    def now_ms() -> float:
        # The backend's time when the node has seen one (the Date of the very answer that ends a gap), else the wall
        # clock — an untrusted one too: the event's own time is marked uncertain by the node-event queue then.
        if trusted is not None:
            try:
                v = trusted()
            except Exception:  # noqa: BLE001
                v = None
            if v is not None:
                return float(v)
        return _wall_ms()

    watch = LinkWatch(mqtt_client.publish_event, now_ms=now_ms)
    reporter.link_watch = watch
    care = NetworkCare(sys_net=sys_net, now_ms=now_ms, on_sample=watch.sample)
    care.watch = watch
    if care.start():
        probe = NetworkProbe(watch, lambda: cloud_endpoint(getattr(reporter, "base_url", None)))
        watch.on_wake = probe.wake
        care.probe = probe
        probe.start()
    return care
