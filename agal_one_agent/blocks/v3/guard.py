"""The motor guard — built in, always on (contracts v1.15.0, ADR-029 decision 4; programs/README.md §7.11).

Every motor card of a v3 bundle carries its guard (``assets[].guard``, automation-block.schema.json
``CompiledGuard``). The runtime evaluates it in the built-ins phase of every pass, before any rule or
instruction part, and at the end of the pass it vetoes an ``on`` of the motor while the guard is
tripped or locked out. No rule, instruction, person or assistant can switch it off.

Each check runs where its input is wired:

* **dry run** — the current stays ``dryRun.dropPct`` below the learned running baseline for
  ``dryRun.holdSeconds``. Until the baseline is learned, dry run does not trip;
* **overload** — the (highest) current stays above ``overload.multiple`` × the motor's reference
  current — its rated current when it is set, otherwise the learned baseline — for
  ``overload.holdSeconds``. Until one of the two is known, overload does not trip;
* **phase loss** — a three-phase motor whose phase sense is wired: a phase reads absent for
  ``phase.holdSeconds`` while the motor is on;
* **phase imbalance** — a three-phase motor with a current per phase: one phase's current differs from
  the mean by more than ``phase.imbalancePct`` % for ``phase.holdSeconds``;
* **the voltage window** — where voltage is measured and ``voltage.minV`` / ``maxV`` are set.

Current (and voltage) is not judged for ``inrushIgnoreSeconds`` after each start. The baseline is
learned ONCE, from the first 10 s after the inrush window of a run that then runs a minute with no
trip, and kept on the node; it is never relearned from a run that tripped.

A trip switches the motor off in that pass (the runtime's veto), ends the run of the block that had it
on, and starts the restart lockout (``restartLockoutSeconds``, never under 30 s), during which nothing
can start it. The guard reports itself tripped for as long as the lockout runs: a tripped guard that
kept vetoing after its lockout could never be started again, and nothing in the contract resets it.

Ported from the agent's own ``sensors/protection.py`` (the confirm window and the post-inrush baseline
average), without its thread and without its relay writes: here the runtime is the one writer.
"""

from __future__ import annotations

import copy
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

logger = logging.getLogger(__name__)

GUARD_TRIP_REASONS = ("dry_run", "overload", "phase_loss", "phase_imbalance", "voltage_low", "voltage_high")
GUARD_REASON_NAMES = ("none",) + GUARD_TRIP_REASONS  # enums.yaml BuiltinVariable guard_reason: none, then the reasons
GUARD_WORDS = {"dry_run": "running dry", "overload": "overload", "phase_loss": "a phase lost",
               "phase_imbalance": "phases unbalanced", "voltage_low": "voltage too low", "voltage_high": "voltage too high"}

DEFAULT_SETTINGS = {  # reference/layout.mjs DEFAULT_GUARD_SETTINGS + the asset.schema.json defaults
    "dryRun": {"dropPct": 30.0, "holdSeconds": 5.0},
    "overload": {"multiple": 1.25, "holdSeconds": 5.0},
    "inrushIgnoreSeconds": 3.0,
    "restartLockoutSeconds": 300,
}
PHASE_DEFAULTS = {"imbalancePct": 20.0, "holdSeconds": 3.0}
VOLTAGE_HOLD_DEFAULT = 5.0
MIN_LOCKOUT_SECONDS = 30
BASELINE_WINDOW_SECONDS = 10.0   # "the first 10 s after the inrush window"
BASELINE_CONFIRM_SECONDS = 60.0  # "of a run that then runs a minute with no trip"
# A motor that draws (next to) nothing is not running: nothing is learnt from that run — it would be kept for
# good, and a dry run could never trip against it. The next run learns instead.
MIN_BASELINE_A = 0.1
THREE_PHASE = ("three_phase", "three_or_two_phase")


@dataclass
class Trip:
    reason: str
    measured: Optional[float]
    limit: Optional[float]
    unit: str


# asset.schema.json MotorGuardSettings: the bounds of each setting. The cloud checks a guard on write; the node
# is the last line, so a bundle can never switch the guard off in all but name (a dry-run limit of 0, an
# overload at 1000 ×, an inrush that never ends, a NaN that makes every comparison false).
BOUNDS = {
    "dryRun.dropPct": (5.0, 80.0),
    "dryRun.holdSeconds": (1.0, 60.0),
    "overload.multiple": (1.05, 3.0),
    "overload.holdSeconds": (1.0, 60.0),
    "phase.imbalancePct": (5.0, 50.0),
    "phase.holdSeconds": (0.5, 30.0),
    "voltage.holdSeconds": (1.0, 60.0),
    "voltage.minV": (0.0, 480.0),
    "voltage.maxV": (0.0, 480.0),
    "inrushIgnoreSeconds": (0.0, 30.0),
    "restartLockoutSeconds": (float(30), 86400.0),
}


def _setting(asset_id: str, name: str, v, default: Optional[float]) -> Optional[float]:
    """A guard setting within its MotorGuardSettings bounds. Absent: the default. Not a finite number (a string,
    a bool, NaN, an infinity): the default, logged. Outside its bounds: clamped to them, logged."""
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        logger.warning("guard %s: %s %r is not a number — %s is used", asset_id, name, v, default)
        return default
    lo, hi = BOUNDS[name]
    f = float(v)
    c = min(hi, max(lo, f))
    if c != f:
        logger.warning("guard %s: %s %s is outside %s..%s — %s is used", asset_id, name, f, lo, hi, c)
    return c


class MotorGuard:
    """One motor's guard: its settings (from the bundle) and its state (carried across reloads,
    persisted across restarts — a trip, the lockout and the baseline are never reset by a reload)."""

    def __init__(self, asset_id: str, cfg: dict, name: str = ""):
        self.asset_id = asset_id
        self.name = name or asset_id
        self.cfg = copy.deepcopy(cfg)
        self.output: str = str(cfg["output"])
        self.current: list[str] = list(cfg.get("current") or [])
        self.phase_sense: list[str] = list(cfg.get("phaseSense") or [])
        self.voltage: Optional[str] = cfg.get("voltage")
        s = cfg.get("settings") or {}
        dry, over = s.get("dryRun") or {}, s.get("overload") or {}

        def setting(name: str, v, default: Optional[float]) -> Optional[float]:
            return _setting(asset_id, name, v, default)
        self.drop_pct = setting("dryRun.dropPct", dry.get("dropPct"), DEFAULT_SETTINGS["dryRun"]["dropPct"])
        self.dry_hold = setting("dryRun.holdSeconds", dry.get("holdSeconds"), DEFAULT_SETTINGS["dryRun"]["holdSeconds"])
        self.multiple = setting("overload.multiple", over.get("multiple"), DEFAULT_SETTINGS["overload"]["multiple"])
        self.over_hold = setting("overload.holdSeconds", over.get("holdSeconds"), DEFAULT_SETTINGS["overload"]["holdSeconds"])
        ph = s.get("phase") or {}
        self.imbalance_pct = setting("phase.imbalancePct", ph.get("imbalancePct"), PHASE_DEFAULTS["imbalancePct"])
        self.phase_hold = setting("phase.holdSeconds", ph.get("holdSeconds"), PHASE_DEFAULTS["holdSeconds"])
        vo = s.get("voltage") or {}
        self.min_v = setting("voltage.minV", vo.get("minV"), None)  # not set (or not a number): not judged
        self.max_v = setting("voltage.maxV", vo.get("maxV"), None)
        self.volt_hold = setting("voltage.holdSeconds", vo.get("holdSeconds"), VOLTAGE_HOLD_DEFAULT)
        self.inrush = setting("inrushIgnoreSeconds", s.get("inrushIgnoreSeconds"), DEFAULT_SETTINGS["inrushIgnoreSeconds"])
        # "at least 30": a lockout can never be shorter, whatever a bundle says.
        self.lockout_seconds = max(MIN_LOCKOUT_SECONDS, int(setting("restartLockoutSeconds", s.get("restartLockoutSeconds"),
                                                                    float(DEFAULT_SETTINGS["restartLockoutSeconds"]))))
        rated = cfg.get("ratedCurrentA")  # CompiledGuard: 0..200 A; not a finite positive number = not set
        ok = isinstance(rated, (int, float)) and not isinstance(rated, bool) and math.isfinite(rated) and rated > 0
        self.rated: Optional[float] = min(float(rated), 200.0) if ok else None
        pt = cfg.get("phaseType")
        self.three_phase = pt in THREE_PHASE if pt else (len(self.current) == 3 or len(self.phase_sense) == 3)
        # ---- state
        self.baseline: Optional[float] = None
        self.tripped = False
        self.reason: Optional[str] = None
        self.tripped_at: Optional[datetime] = None
        self.lockout_until_wall: Optional[datetime] = None
        self.lockout_until: Optional[float] = None  # monotonic
        self.running = False
        self.started_at: Optional[float] = None
        self._learn: Optional[dict] = None
        self._since: dict[str, float] = {}

    # ------------------------------------------------------------ carry / persist

    def inputs(self) -> list[str]:
        out = list(self.current) + list(self.phase_sense)
        if self.voltage:
            out.append(self.voltage)
        return out

    def carry_from(self, old: "MotorGuard") -> None:
        """A reload never resets a guard: its trip, lockout and baseline, and the run it is judging."""
        for f in ("baseline", "tripped", "reason", "tripped_at", "lockout_until_wall", "lockout_until",
                  "running", "started_at", "_learn", "_since"):
            setattr(self, f, copy.copy(getattr(old, f)))

    def to_state(self) -> dict:
        return {"baselineA": self.baseline, "tripped": self.tripped, "reason": self.reason,
                "trippedAt": self.tripped_at.isoformat() if self.tripped_at else None,
                "lockoutUntil": self.lockout_until_wall.isoformat() if self.lockout_until_wall else None}

    def load_state(self, st: dict, now: float, wall: datetime, clock_ok: bool = True) -> None:
        """After a restart. A lockout still running is restored for what it had left — never more than its full
        length, whatever the wall clock says. On a clock that cannot be trusted (no RTC, no NTP yet) what it had
        left cannot be known: it runs its full length from now (safe, and bounded)."""
        b = st.get("baselineA")
        if isinstance(b, (int, float)) and not isinstance(b, bool) and math.isfinite(b) and b >= MIN_BASELINE_A:
            self.baseline = float(b)
        until = _parse(st.get("lockoutUntil"))
        if not st.get("tripped") or until is None:
            return
        if clock_ok:
            if until.tzinfo is None and wall.tzinfo is not None:
                until = until.replace(tzinfo=wall.tzinfo)
            left = min(max(0.0, (until - wall).total_seconds()), float(self.lockout_seconds))
        else:
            left = float(self.lockout_seconds)
        if left <= 0:
            return
        self.tripped, self.reason = True, st.get("reason")
        self.tripped_at = _parse(st.get("trippedAt"))
        self.lockout_until = now + left
        self.lockout_until_wall = wall + timedelta(seconds=left)

    # --------------------------------------------------------------- the motor

    def started(self, now: float) -> None:
        """The runtime switched the motor on (written at the end of a pass)."""
        if self.running:
            return
        self.running, self.started_at = True, now
        self._since.clear()
        if self.baseline is None:
            start = now + self.inrush
            self._learn = {"from": start, "to": start + BASELINE_WINDOW_SECONDS,
                           "commit": start + BASELINE_WINDOW_SECONDS + BASELINE_CONFIRM_SECONDS, "samples": []}

    def stopped(self, now: float) -> None:
        self.running, self.started_at = False, None
        self._since.clear()
        self._learn = None  # a run that ends before it has run a minute teaches nothing

    def locked_out(self, now: float) -> bool:
        return self.tripped and self.lockout_until is not None and now < self.lockout_until

    def vetoes(self, now: float) -> bool:
        return self.locked_out(now)

    def reason_index(self) -> int:
        return GUARD_REASON_NAMES.index(self.reason) if self.tripped and self.reason in GUARD_REASON_NAMES else 0

    def expire(self, now: float) -> bool:
        """The lockout ran out: the guard is no longer tripped. True when that happened now."""
        if self.tripped and (self.lockout_until is None or now >= self.lockout_until):
            self.tripped, self.reason = False, None
            self.lockout_until = self.lockout_until_wall = None
            return True
        return False

    # ---------------------------------------------------------------- one pass

    def evaluate(self, now: float, wall: datetime, read: Callable[[str], object]) -> tuple[Optional[Trip], Optional[float]]:
        """Judge the running motor. Returns (a trip, if one happened now; a baseline, if one was
        learned now). ``read(sourceKey)`` is the sampled value of one of the card's inputs (None when it
        could not be read: a broken reading is never acted on)."""
        self.expire(now)
        if not self.running or self.tripped:
            return None, None
        trip = self._phase_loss(now, read)
        if trip:
            return self._trip(trip, now, wall), None
        if self.started_at is not None and now < self.started_at + self.inrush:
            return None, None  # a motor's inrush is normal
        learned = None
        currents = [read(k) for k in self.current]
        have_current = bool(currents) and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in currents)
        if have_current:
            cur = [float(c) for c in currents]
            mean = sum(cur) / len(cur)
            learned = self._learn_step(now, mean)
            for t in (self._dry_run(now, mean), self._overload(now, max(cur)), self._imbalance(now, cur, mean)):
                if t:
                    return self._trip(t, now, wall), learned
        trip = self._voltage(now, read)
        if trip:
            return self._trip(trip, now, wall), learned
        return None, learned

    def _held(self, key: str, cond: bool, now: float, hold: float) -> bool:
        if not cond:
            self._since.pop(key, None)
            return False
        since = self._since.setdefault(key, now)
        return now - since >= hold

    def _phase_loss(self, now: float, read) -> Optional[Trip]:
        if not (self.three_phase and self.phase_sense):
            return None
        vals = [read(k) for k in self.phase_sense]
        if any(v is None for v in vals):
            self._since.pop("phase_loss", None)
            return None
        present = sum(1 for v in vals if bool(v))
        if self._held("phase_loss", present < len(vals), now, self.phase_hold):
            return Trip("phase_loss", float(present), float(len(vals)), "phases")
        return None

    def _learn_step(self, now: float, mean: float) -> Optional[float]:
        lw = self._learn
        if lw is None or self.baseline is not None:
            return None
        if lw["from"] <= now < lw["to"]:
            lw["samples"].append(mean)
        if now >= lw["commit"] and lw["samples"]:
            mean = sum(lw["samples"]) / len(lw["samples"])
            self._learn = None
            if mean < MIN_BASELINE_A:
                logger.warning("guard %s: the run drew %.3f A — not a running current, nothing learnt (the next run learns)",
                               self.asset_id, mean)
                return None
            self.baseline = mean
            return self.baseline
        return None

    def _dry_run(self, now: float, mean: float) -> Optional[Trip]:
        if self.baseline is None:
            return None
        limit = self.baseline * (1.0 - self.drop_pct / 100.0)
        if self._held("dry_run", mean < limit, now, self.dry_hold):
            return Trip("dry_run", mean, limit, "A")
        return None

    def _overload(self, now: float, high: float) -> Optional[Trip]:
        ref = self.rated if self.rated is not None else self.baseline
        if ref is None:
            return None
        limit = self.multiple * ref
        if self._held("overload", high > limit, now, self.over_hold):
            return Trip("overload", high, limit, "A")
        return None

    def _imbalance(self, now: float, cur: list[float], mean: float) -> Optional[Trip]:
        if not (self.three_phase and len(cur) == 3) or mean <= 0:
            return None
        pct = max(abs(c - mean) for c in cur) / mean * 100.0
        if self._held("phase_imbalance", pct > self.imbalance_pct, now, self.phase_hold):
            return Trip("phase_imbalance", pct, self.imbalance_pct, "%")
        return None

    def _voltage(self, now: float, read) -> Optional[Trip]:
        if not self.voltage or (self.min_v is None and self.max_v is None):
            return None
        v = read(self.voltage)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return None
        v = float(v)
        if self.min_v is not None and self._held("voltage_low", v < self.min_v, now, self.volt_hold):
            return Trip("voltage_low", v, self.min_v, "V")
        if self.max_v is not None and self._held("voltage_high", v > self.max_v, now, self.volt_hold):
            return Trip("voltage_high", v, self.max_v, "V")
        return None

    def _trip(self, t: Trip, now: float, wall: datetime) -> Trip:
        self.tripped, self.reason = True, t.reason
        self.tripped_at = wall
        self.lockout_until = now + self.lockout_seconds
        self.lockout_until_wall = wall + timedelta(seconds=self.lockout_seconds)
        self._learn = None  # never learnt from a run that tripped
        self._since.clear()
        logger.error("guard %s tripped: %s (%s %s, limit %s)", self.asset_id, t.reason, t.measured, t.unit, t.limit)
        return t


def _parse(s) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except ValueError:
        return None
