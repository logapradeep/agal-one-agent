"""The node's clock: its source and its offset from the backend's time (agent 0.4.4, contracts v1.24.0;
nodes/README.md §14; ADR-031 §6 — "each node reports its clock source and offset").

Why. A booking of a shared pump runs in two halves on two nodes — the pump on its owner's node, the well
valve on the booker's — each on its own clock, and nothing in the cloud coordinates them while they run. The
backend will warn both owners when two nodes of one booking disagree by more than a few seconds; this is what
it will compare. Nothing reads it yet.

The source. Every ``CHECK_S`` on a thread of its own (``clock-care`` — never the runtime's, never under its
lock; programs/README.md §4.8): ``timedatectl show`` (NTPSynchronized), ``systemctl is-active`` of the time
services (the first that is active) and whether ``/dev/rtc0`` or ``/dev/rtc`` exists. Each program an argv
list, never a shell, bounded (``TOOL_TIMEOUT_S``), killed with its process group when it does not end; one that
will not die is left, and no program is started while it lives (net_care.run_tool, as the network's checks).

The offset. server_time.ServerTime notes the ``Date`` of every HTTPS answer from the backend (for checking a
command's age); each answer is also a sample — the wall clock when it was read minus its Date + 500 ms — and the
offset is the median of the last 15 since the wall clock last stepped (``server_time.add_sample`` /
``offset_of``, the reference's ``addSample`` / ``offsetOf``). Read from memory by the heartbeat.

The reference is Agal/contracts/nodes/reference/clock.mjs; tests/test_clock_care.py replays its vectors.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from .net_care import ToolHung, ToolTimeout, run_tool, which

logger = logging.getLogger(__name__)

CHECK_S = 60.0
TOOL_TIMEOUT_S = 3.0
SOURCES = ("ntp", "rtc", "none", "unknown")            # enums.yaml#NodeClockSource
SERVICES = ("timesyncd", "chrony", "ntpd", "none", "unknown")  # enums.yaml#NodeTimeService
#: The units `systemctl is-active` is asked about, in this order, and the service each one is (the reference's UNITS).
UNITS = (
    ("systemd-timesyncd.service", "timesyncd"),
    ("chrony.service", "chrony"),
    ("chronyd.service", "chrony"),
    ("ntpsec.service", "ntpd"),
    ("ntp.service", "ntpd"),
    ("ntpd.service", "ntpd"),
)
RTC_PATHS = ("/dev/rtc0", "/dev/rtc")
THREAD = "clock-care"


# ------------------------------------------------------------------------------------------ the parsers

def parse_timedatectl_show(text: Optional[str]) -> dict:
    """``timedatectl show``: one ``Key=Value`` per line → {ntpSynchronized, ntp, canNtp, localRtc}: True for ``yes``,
    False for ``no``, None when the key is missing or says anything else (the reference's parseTimedatectlShow)."""
    kv: dict = {}
    for line in str(text or "").split("\n"):
        i = line.find("=")
        if i <= 0:
            continue
        kv[line[:i].strip()] = line[i + 1:].strip()

    def yes_no(k: str) -> Optional[bool]:
        return True if kv.get(k) == "yes" else False if kv.get(k) == "no" else None
    return {"ntpSynchronized": yes_no("NTPSynchronized"), "ntp": yes_no("NTP"), "canNtp": yes_no("CanNTP"),
            "localRtc": yes_no("LocalRTC")}


def service_of(text: Optional[str], units=UNITS) -> str:
    """``systemctl is-active <the units, in order>``: one state per unit per line → the service of the FIRST unit that is
    ``active``; ``none`` when every unit has a line and none is; ``unknown`` when there are fewer lines than units."""
    lines = [ln.strip() for ln in str(text or "").split("\n") if ln.strip()]
    if len(lines) < len(units):
        return "unknown"
    for i, (_unit, service) in enumerate(units):
        if lines[i] == "active":
            return service
    return "none"


def source_of(ntp_synchronized: Optional[bool], rtc: Optional[bool]) -> str:
    """ntp while synchronised; not synchronised: rtc with a hardware clock, else none; not known: unknown."""
    if ntp_synchronized is True:
        return "ntp"
    if ntp_synchronized is False:
        return "rtc" if rtc is True else "none"
    return "unknown"


def iso_ms(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ------------------------------------------------------------------------------------------ the thread

class ClockCare:
    """The ``clock-care`` thread: reads the clock's source every CHECK_S and gives the heartbeat its ``clock`` block."""

    def __init__(self, *, server_time=None, now_ms: Optional[Callable[[], float]] = None, check_s: float = CHECK_S,
                 tool_timeout_s: float = TOOL_TIMEOUT_S, run: Callable = run_tool, find: Callable = which,
                 exists: Callable[[str], bool] = os.path.exists, rtc_paths=RTC_PATHS, platform: Optional[str] = None):
        self.server_time = server_time
        self._now_ms = now_ms or (lambda: time.time() * 1000.0)
        self.check_s = check_s
        self.tool_timeout_s = tool_timeout_s
        self._run_tool = run
        self._which = find
        self._exists = exists
        self._rtc_paths = tuple(rtc_paths)
        self._platform = platform or sys.platform
        self._lock = threading.Lock()
        self._reading: Optional[dict] = None
        self._hung: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._logged: set = set()

    def start(self) -> bool:
        if self._thread is not None:
            return True
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
            try:
                self.step()
            except Exception as e:  # noqa: BLE001 — a reading that fails never stops the thread
                self._log_once(("step", type(e).__name__), logging.WARNING, "clock: the reading failed: %s", e)
            self._stop.wait(self.check_s)

    # ---------------------------------------------------------------------------------------- a reading

    def step(self) -> dict:
        """Read the source now (on this thread) and keep it: {source, service, rtc, atMs}."""
        rtc = any(self._exists(p) for p in self._rtc_paths)
        synced: Optional[bool] = None
        service = "unknown"
        if self._platform.startswith("linux"):
            tdc = self._which("timedatectl")
            if tdc:
                rc, out, why = self._run([tdc, "show"])
                if why:
                    self._log_once(("timedatectl", why), logging.INFO, "clock: timedatectl show: %s", why)
                else:
                    synced = parse_timedatectl_show(out)["ntpSynchronized"]
            sysctl = self._which("systemctl")
            if sysctl:
                rc, out, why = self._run([sysctl, "is-active", *[u for u, _ in UNITS]])
                if why:
                    self._log_once(("systemctl", why), logging.INFO, "clock: systemctl is-active: %s", why)
                else:
                    service = service_of(out)
        reading = {"source": source_of(synced, rtc), "service": service, "rtc": rtc, "atMs": float(self._now_ms())}
        with self._lock:
            before = self._reading
            self._reading = reading
        if before is None or (before["source"], before["service"]) != (reading["source"], reading["service"]):
            logger.info("clock: %s (time service %s%s)", reading["source"], reading["service"], ", an RTC" if rtc else "")
        return reading

    def status(self) -> Optional[dict]:
        """The heartbeat's ``clock`` block (functions.yaml 1.19.0 NodeStatusClock), from memory — None before the
        first reading."""
        with self._lock:
            r = dict(self._reading) if self._reading else None
        if r is None:
            return None
        block: dict = {"source": r["source"], "service": r["service"], "rtc": bool(r["rtc"])}
        off = self.server_time.offset() if self.server_time is not None and hasattr(self.server_time, "offset") else None
        if off:
            block["offsetMs"] = int(off["offsetMs"])
            block["offsetSamples"] = int(off["samples"])
            block["offsetAt"] = iso_ms(off["atMs"])
        block["at"] = iso_ms(r["atMs"])
        return block

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


def with_clock(extra: Optional[dict], care: Optional[ClockCare]) -> Optional[dict]:
    """The heartbeat's extra fields with the ``clock`` block added when there is one (main.py)."""
    block = care.status() if care is not None else None
    if not block:
        return extra
    out = dict(extra or {})
    out["clock"] = block
    return out


def wire_clock(*, offline: bool, server_time=None, now_ms: Optional[Callable[[], float]] = None) -> Optional[ClockCare]:
    """main.py: the clock-care thread, started; offline (no cloud, no heartbeat): None."""
    if offline:
        return None
    care = ClockCare(server_time=server_time, now_ms=now_ms)
    care.start()
    return care
