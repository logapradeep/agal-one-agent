"""The node's safety built-ins: the E-stop latch and power sense (contracts v1.15.0, ADR-029 decisions
5 and 9; programs/README.md §7.8, §7.10). Pure state machines; the runtime samples their inputs, acts on
what they report (every motor off, runs interrupted and resumed) and raises the events.

E-stop. The button breaks the motors' control circuit in hardware — the node is never in the stop path.
Its aux contact is a node input (``contact``: ``nc`` by default, so a broken wire reads as pressed). When it
reads pressed the node latches. No role, assistant, rule or command clears the latch: it clears only at the
panel — the input reads released, THEN the local reset is seen (a rising edge of the reset input, or of the
panel start's aux contact). A reset held while the button is released does not count until it is let go
and pressed again. An input that cannot be read is taken as pressed: the safe reading.

Power sense. Mains (and the UPS output) presence comes from sense inputs, never from the clock.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional


def _key(pk: dict) -> tuple[str, str]:
    return (str(pk["assetId"]), str(pk["sourceKey"]))


class EStop:
    def __init__(self, cfg: dict):
        self.input = _key(cfg["input"])
        self.reset = _key(cfg["reset"])
        self.contact = cfg.get("contact") or "nc"
        self.reset_kind = cfg.get("resetKind") or "reset_input"
        self.signature = (self.input, self.reset, self.contact, self.reset_kind)
        self.latched = False
        self.pressed_at: Optional[datetime] = None
        self.latched_mono: Optional[float] = None
        self._released = False
        self._prev_reset: Optional[bool] = None

    def pressed(self, contact_value) -> bool:
        if contact_value is None:
            return True  # cannot be read: the safe reading
        closed = bool(contact_value)
        return (not closed) if self.contact == "nc" else closed

    def update(self, now: float, wall: datetime, contact_value, reset_value) -> Optional[str]:
        """``"pressed"`` when it latched now, ``"cleared"`` when it was cleared at the panel now."""
        pressed = self.pressed(contact_value)
        reset = bool(reset_value) if reset_value is not None else False
        prev, self._prev_reset = self._prev_reset, reset
        if not self.latched:
            if pressed:
                self.latched, self.pressed_at, self.latched_mono = True, wall, now
                self._released = False
                return "pressed"
            return None
        if pressed:
            self._released = False
            return None
        if not self._released:
            self._released = True  # released first; the reset must come after it
            return None
        if reset and prev is False:
            self.latched, self._released = False, False
            return "cleared"
        return None

    def carry_from(self, old: "EStop") -> None:
        self.latched, self.pressed_at, self.latched_mono = old.latched, old.pressed_at, old.latched_mono
        self._released, self._prev_reset = old._released, old._prev_reset

    def to_state(self) -> dict:
        return {"latched": self.latched, "pressedAt": self.pressed_at.isoformat() if self.pressed_at else None}


class PowerSense:
    def __init__(self, cfg: dict):
        self.mains = _key(cfg["mains"])
        self.ups: Optional[tuple[str, str]] = _key(cfg["ups"]) if cfg.get("ups") else None
        self.signature = (self.mains, self.ups)
        self.mains_present: Optional[bool] = None
        self.ups_present: Optional[bool] = None
        self.since: Optional[datetime] = None
        self.cut_at: Optional[float] = None  # monotonic, while mains is absent

    @staticmethod
    def _word(v: Optional[bool]) -> str:
        return "unknown" if v is None else ("present" if v else "absent")

    def update(self, now: float, wall: datetime, mains_value, ups_value) -> list[tuple[str, str]]:
        """[(supply, "cut" | "restored")] for every change sensed now. An input that cannot be read
        changes nothing (presence comes from a reading, never from a guess)."""
        out: list[tuple[str, str]] = []
        if mains_value is not None:
            m = bool(mains_value)
            if self.mains_present is None:
                self.mains_present, self.since = m, wall
                if not m:
                    self.cut_at = now
                    out.append(("mains", "cut"))
            elif m != self.mains_present:
                self.mains_present, self.since = m, wall
                if not m:
                    self.cut_at = now
                out.append(("mains", "restored" if m else "cut"))
        if self.ups is not None and ups_value is not None:
            u = bool(ups_value)
            if self.ups_present is not None and u != self.ups_present:
                out.append(("ups", "restored" if u else "cut"))
            self.ups_present = u
        return out

    def carry_from(self, old: "PowerSense") -> None:
        self.mains_present, self.ups_present, self.since, self.cut_at = old.mains_present, old.ups_present, old.since, old.cut_at

    def status(self) -> dict:
        st: dict = {"mains": self._word(self.mains_present)}
        if self.ups is not None:
            st["ups"] = self._word(self.ups_present)
        if self.since is not None:
            st["since"] = self.since.isoformat()
        return st
