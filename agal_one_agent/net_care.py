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
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

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

class LinkWatch:
    """The node's answers from the cloud, and the gaps between them (nodes/README.md §11 item 4; reference
    ``watchGaps``). ``answered`` / ``no_answer`` are called by ``HttpReporter`` from whichever thread posted; they do
    arithmetic under a lock and nothing else, and a gap is handed to ``emit`` (the node-event queue) outside it."""

    def __init__(self, emit: Optional[Callable[[dict], None]] = None, *, monotonic: Callable[[], float] = time.monotonic,
                 now_ms: Optional[Callable[[], float]] = None, min_gap_s: float = MIN_GAP_S):
        self.emit = emit
        self._monotonic = monotonic
        self._now_ms = now_ms or _wall_ms
        self.min_gap_s = min_gap_s
        self._lock = threading.Lock()
        self._last: Optional[tuple] = None   # (monotonic s, ms) of the last answer
        self._missed = False
        self._base: Optional[LinkSample] = None
        self._latest: Optional[LinkSample] = None
        self._samples = 0
        self._down = False
        self._rssi_before: Optional[int] = None
        self._ps_before: Optional[str] = None
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

    def no_answer(self) -> None:
        with self._lock:
            self._missed = True

    def answered(self) -> Optional[dict]:
        gap = None
        with self._lock:
            t = self._monotonic()
            at = self._now_ms()
            if self._last is not None and self._missed and t - self._last[0] >= self.min_gap_s:
                gap = self._gap_locked(t, at)
                self.gaps.append(gap)
            self._last = (t, at)
            self._missed = False
            self._samples = 0
            self._down = False
            self._base = self._latest
        if gap is not None:
            self._raise(gap)
        return gap

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
        return gap

    def _raise(self, gap: dict) -> None:
        if gap.get("wifiConnectedThroughout") is True:
            how = "its Wi-Fi stayed connected (the router or the internet dropped)"
        elif gap.get("wifiDropped") is True:
            how = f"its Wi-Fi dropped (signal {gap.get('signalBefore') or 'unknown'}" + (
                f", {gap['rssiDbmBefore']} dBm)" if gap.get("rssiDbmBefore") is not None else ")")
        elif gap.get("kind") == "ethernet":
            how = "it is on Ethernet"
        else:
            how = "its link could not be read"
        logger.warning("no answer from the cloud for %s (%s → %s); %s — reported as connectivity_gap",
                       duration_words(gap["seconds"]), gap["from"], gap["to"], how)
        if self.emit is None:
            return
        try:
            self.emit({"type": "connectivity_gap", "source": "node", "payload": {"connectivity": dict(gap)}})
        except Exception as e:  # noqa: BLE001 — reporting never fails the request that ended the gap
            logger.error("connectivity_gap not queued: %s", e)


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
    """main.py: a ``LinkWatch`` on the reporter (every answer and every request that got none), whose gaps go to
    ``mqtt_client.publish_event`` (the node-event queue); and the ``NetworkCare`` thread feeding it the link, started
    when the board has /sys/class/net. Offline (no cloud): nothing, None. tests/test_net_care.py checks main() makes
    this call."""
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
    care.start()
    return care
