"""Valve drivers — built in (contracts v1.15.0, ADR-029 decision 6; programs/README.md §7.9).

A block sets a valve card's logical ``valve.open`` (true = open, whatever its kind). The driver turns it
into what the coil needs; the coil ports are the driver's alone (a block that names one is refused).

* **Continuous** (``continuous_ac`` / ``continuous_dc``): one output. Powered is open for a
  normally-closed valve; the driver inverts for a normally-open one. It closes on any loss of power, so
  its state is known after any interruption, and it never faults.
* **Single-pulse DC latching** (``latching_dc``): an open output and a close output, each pulsed for
  ``pulseMs`` (default 100), never both at once. The node remembers the position across restarts,
  together with a pulse-in-progress mark. Its relay power and the node itself are on a UPS or battery,
  sensed on a node input (``supply``).

FAULT (latching only) comes from any interruption the node cannot vouch for: the supply sense drops or
cannot be read (``supply_lost``); the node starts after a stop it did not make cleanly
(``unclean_boot``); a pulse is cut short (``pulse_cut_short``); any other position it cannot vouch for —
a first start, a changed configuration (``position_unknown``). While a valve is in FAULT no pulse is sent
to it; the runtime holds only the blocks that use it. A reset sends one close pulse (refused while the
supply is not sensed present), records the valve closed and clears the fault.

This module holds the driver's configuration and state; the runtime does the I/O (one writer) and the
persistence (the position and the pulse mark are written before and after every pulse).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from ..io import PortRef

VALVE_STATES = ("open", "closed", "unknown", "fault")  # enums.yaml ValveState, in order (valve_state = the index)
FAULT_REASONS = ("supply_lost", "unclean_boot", "pulse_cut_short", "position_unknown")
DEFAULT_PULSE_MS = 100
FAULT_WORDS = {
    "supply_lost": "its relay supply is not sensed present",
    "unclean_boot": "the node started after a stop it did not make cleanly",
    "pulse_cut_short": "a pulse did not run its full length",
    "position_unknown": "its position cannot be vouched for (a first start or a changed configuration)",
}


def config_signature(cfg: dict) -> str:
    return json.dumps(cfg or {}, sort_keys=True, default=str)


class ValveDriver:
    def __init__(self, asset_id: str, cfg: dict, ports: dict[str, PortRef], name: str = ""):
        self.asset_id = asset_id
        self.name = name or asset_id
        self.cfg = dict(cfg)
        self.actuation = str(cfg.get("actuation"))
        self.latching = self.actuation == "latching_dc"
        self.normally_closed = cfg.get("normallyClosed", True) is not False
        self.open_key: str = str(cfg.get("open"))
        self.close_key: Optional[str] = cfg.get("close")
        self.open_port: PortRef = ports[self.open_key]
        self.close_port: Optional[PortRef] = ports.get(self.close_key) if self.close_key else None
        pm = cfg.get("pulseMs", DEFAULT_PULSE_MS)
        self.pulse_ms = max(20, min(2000, int(pm if isinstance(pm, (int, float)) and not isinstance(pm, bool) else DEFAULT_PULSE_MS)))
        sup = cfg.get("supply") or {}
        self.supply: Optional[tuple[str, str]] = (sup["assetId"], sup["sourceKey"]) if sup.get("assetId") and sup.get("sourceKey") else None
        self.signature = config_signature(cfg)
        # ---- state
        self.position: Optional[str] = None      # latching: "open" | "closed" while it can be vouched for
        self.fault: Optional[str] = None         # latching: a ValveFaultReason
        self.fault_detail: str = ""
        self.fault_at: Optional[datetime] = None
        self.pulse_mark: Optional[str] = None    # "open" | "close" while a pulse is in progress
        self.coil_written: Optional[bool] = None  # continuous: the coil level last written
        # Latching: what each coil line is known to be — False known off, True left on (a line the old program
        # drove as a level), None unknown (an off-write that failed, a pulse cut short). Every output starts off
        # at a cold load, so both coils start known off; a coil that is not known off is written off before
        # any pulse of the other one ("never both at once").
        self.coil_state: dict[str, Optional[bool]] = {k: False for k in (self.open_key, self.close_key) if k} if self.latching else {}

    def coil_ports(self) -> list[PortRef]:
        return [p for p in (self.open_port, self.close_port) if p is not None]

    def coils(self) -> list[tuple[str, PortRef]]:
        """Latching: (coil key, port) of the open and the close coil."""
        return [(k, p) for k, p in ((self.open_key, self.open_port), (self.close_key, self.close_port)) if k and p is not None]

    def coils_known_off(self) -> bool:
        return all(v is False for v in self.coil_state.values())

    def coil_level(self, want_open: bool) -> bool:
        """Continuous: the coil level that gives ``want_open``."""
        return bool(want_open) if self.normally_closed else not bool(want_open)

    def state(self, level) -> str:
        if self.latching:
            if self.fault:
                return "fault"
            return self.position or "unknown"
        return "open" if bool(level) else "closed"

    def state_index(self, level) -> int:
        return VALVE_STATES.index(self.state(level))

    def set_fault(self, reason: str, detail: str, wall: datetime) -> bool:
        """Put a latching valve in FAULT. True when it was not in fault before."""
        if not self.latching or self.fault:
            return False
        self.fault, self.fault_detail, self.fault_at = reason, detail, wall
        self.position = None
        return True

    def cleared(self) -> None:
        """After the reset's close pulse: closed, no fault."""
        self.fault, self.fault_detail, self.fault_at = None, "", None
        self.position = "closed"

    def carry_from(self, old: "ValveDriver") -> None:
        for f in ("position", "fault", "fault_detail", "fault_at", "pulse_mark"):
            setattr(self, f, getattr(old, f))
        if old.open_port.transport == self.open_port.transport and not old.latching and not self.latching:
            self.coil_written = old.coil_written  # the same line, the same use: nothing to write
        if old.latching and self.latching:
            # A coil on the line the old driver's same coil used keeps what it is known to be; any other coil line
            # is set by the runtime's take-over from what the old program drove there.
            for k, p in self.coils():
                ok = dict(old.coils()).get(k)
                if ok is not None and ok.transport == p.transport:
                    self.coil_state[k] = old.coil_state.get(k)

    def to_state(self) -> dict:
        if not self.latching:
            return {"config": self.signature}
        return {"position": "fault" if self.fault else self.position, "faultReason": self.fault,
                "pulse": self.pulse_mark, "config": self.signature}
