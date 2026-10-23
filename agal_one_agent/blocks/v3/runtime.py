"""RuntimeV3 — runs a language-3 bundle on the node (contracts v1.15.0, ADR-029; programs/README.md §7).

A pass (README §7.5):

1. sample the inputs;
2. the built-ins: power sense (§7.8), the E-stop latch (§7.10), the valve drivers (§7.9), the motor guards
   (§7.11) — then the commands, and which blocks hold (a latching valve in FAULT holds the blocks that use
   it; a latched E-stop holds the blocks that drive a motor);
3. the instruction parts — for each layout, its active part (§7.6) — in bundle order;
4. the rule slots, in bundle order; a rule sees the outputs as the instruction left them in this pass;
5. outputs are written once, at the end of the pass, through the built-ins' veto (a tripped or locked-out
   motor, a latched E-stop, a power cut keeps the motor off whatever a block set): switch-offs first
   (motors before valves), then switch-ons (valves before motors), each class in ``assets[]`` order.

A bundle loaded while another runs takes over bumplessly (§7.5): outputs are carried by their stable key
(the card and its port; a valve by its ``valve.open``), unchanged variables and rules keep their state, the
built-ins' state is never reset, running schedules are carried by (layout, schedule id) across the parts
of their layout, and an output no block writes any more keeps its level until the end of the run that set
it — or ``now`` when the handover says so or no run is in progress. A take-over that fails half-way leaves
everything as it was and the bundle is rejected.

Pause-first manual operation (§4a, §7.7): while paused, instruction parts and ordinary rule slots wait;
the built-ins and the interlock rule slots keep running.

A single-pulse valve is closed at start-up and before a shutdown, built in (agent 0.4.4, contracts v1.26.0; §7.9 —
the founder's rule of 2026-10-14): a clean stop sends every single-pulse valve not in FAULT one close pulse before it
records the clean stop (``apply_safe_state``); a clean start's boot pass sends each one it vouches for one close pulse,
whatever position it recorded (``start_close``); an unclean start sends none — FAULT, a reset needed. ``valve_closed``.

Deterministic under a SimClock and a SimulatedIO — the golden vectors of programs/vectors run through it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from .. import expr as E
from ..clock import Clock, SystemClock
from ..io import IOAdapter, PortRef, SimulatedIO, describe_port, physical_port, same_output_line
from ..runtime import CompileError, EventSink, _coerce, _signature
from ..sun import sun_event_local
from . import program as P
from .guard import GUARD_WORDS
from .program import Block, Output, Program, is_on, off_value
from .valve import FAULT_WORDS, same_configuration

logger = logging.getLogger(__name__)

STATE_FILE = "blocks_v3_state.json"
KEEPALIVE_PERSIST_SECONDS = 60.0  # while a run is in progress: what a restart needs to know how long it had left
NODE = "node"                      # event source of the built-ins (functions.yaml 1.11.0: "source node")
PAUSE_MAX_SECONDS = 7 * 86400      # functions.yaml pauseAutomation `until`: at most 7 days ahead
# README §7.10: removing the E-stop is refused while it is latched, and never clears a latch. The cloud refuses it
# too, but it sees the latch only through the heartbeat; the node is the one that knows.
LATCH_KEPT = ("refused: the E-stop is latched at the panel — a program without it is not taken while it is latched; "
              "it clears only at the panel")
UNREADABLE = "__unreadable__"
STATE_WRITER_THREAD = "v3-state-writer"
# A reset accepted but never carried out: the runtime stopped before a pass ran it (contracts v1.15.2 item 15).
RESET_STOPPED = "the node stopped before the reset ran: no close pulse sent"
# A command that reaches a runtime that has stopped — the agent shutting down: its MQTT stays connected for a few
# seconds after the runtime's stop — is refused on arrival: no pass will carry it out (contracts v1.15.2 item 16).
STOPPING = "refused: the node is stopping — no pass will carry it out"
RESET_STOPPING = "refused: the node is stopping — no close pulse sent"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


AUTHOR_KEYS = ("uid", "via", "connectorId", "assistantName", "proposalId", "at")
AUTHOR_VIA = ("app", "assist", "connector", "live")


def _author(actor, wall: datetime) -> Optional[dict]:
    """A command's `actor` as an Author (automation-block.schema.json): who, through what, and when — `at` is
    the node's own time of the act when the command did not say. None when it does not name who and how."""
    if not isinstance(actor, dict) or not actor.get("uid") or actor.get("via") not in AUTHOR_VIA:
        return None
    a = {k: actor[k] for k in AUTHOR_KEYS if actor.get(k) is not None}
    a.setdefault("at", wall.isoformat())
    return a


def _parse(s) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except ValueError:
        return None


def _start_spec(sd: dict) -> str:
    """What a schedule starts at, in words that survive a swap: its HH:MM, or its sun event and offset."""
    if sd.get("start"):
        return str(sd["start"])
    return f"{sd.get('sunEvent')}{int(sd.get('offsetMinutes') or 0):+d}"


class Run:
    """A schedule run in progress on a layout — carried by (layout, schedule id) across the layout's parts."""

    def __init__(self, layout: str, sid: str, part_key: str, start_wall: datetime, duration_s: float,
                 window_s: float, now: float):
        self.layout, self.sid, self.part_key = layout, sid, part_key
        self.key = f"{layout}/{sid}"
        self.start_wall = start_wall
        self.duration = duration_s
        self.window_s = window_s
        self.planned_end = start_wall + timedelta(seconds=duration_s)
        self.end_mono: Optional[float] = now + duration_s
        self.state = "running"                  # running | interrupted
        self.remaining: Optional[float] = None  # interrupted: what it had left
        self.interrupted_at: Optional[float] = None
        self.lost = 0.0
        self.on_keys: set[tuple[str, str]] = set()
        self.pending: Optional[list] = None     # actions its part applies at its next evaluation
        self.short_reason: Optional[str] = None  # it ends now because of …
        self.cut_short_at_end = False           # it ends at its window's end, before its time was up
        self.over_while_held = False            # its planned end passed while its block held
        self.resumed_at: Optional[float] = None
        # Interrupted: the monotonic time by which its window has certainly passed, whatever the wall clock says
        # (from the room it had when it was interrupted, or at the stop) — what decides it on a clock not trusted.
        self.window_mono_end: Optional[float] = None

    @property
    def window_end(self) -> datetime:
        return self.planned_end + timedelta(seconds=self.window_s)

    def copy(self) -> "Run":
        r = Run.__new__(Run)
        r.__dict__.update(self.__dict__)
        r.on_keys = set(self.on_keys)
        r.pending = list(self.pending) if self.pending is not None else None
        return r

    def to_state(self) -> dict:
        return {"layout": self.layout, "sid": self.sid, "partKey": self.part_key, "start": _iso(self.start_wall),
                "duration": self.duration, "window": self.window_s, "plannedEnd": _iso(self.planned_end),
                "state": self.state, "remaining": self.remaining, "lost": self.lost,
                "onKeys": [f"{a} {k}" for a, k in sorted(self.on_keys)]}


class _State:
    """Everything the runtime remembers between passes that is not part of the compiled program."""

    def __init__(self):
        self.runs: dict[str, Run] = {}
        self.sched: dict[str, dict] = {}
        self.orphans: list[dict] = []
        self.releases: list[dict] = []        # an output kept to the end of the run that set it
        self.now_releases: list[dict] = []    # released at the next pass
        self.line_releases: list[tuple[PortRef, bool]] = []
        self.release_events: list[tuple[str, str]] = []
        self.switches: list[dict] = []
        self.held: dict[str, set] = {}
        self.hold_runs: dict[str, dict[str, float]] = {}
        self.pending_events: list[tuple[str, dict]] = []
        self.inputs: dict[tuple[str, str], Any] = {}
        self.input_changed: dict[tuple[str, str], float] = {}
        self.debounce: dict[tuple[str, str], dict] = {}
        self.builtin_seen: dict[tuple[str, str], tuple[Any, float]] = {}
        self.paused = False
        self.automation: dict = {"state": "running"}
        self.paused_mono: Optional[float] = None
        self.pause_until: Optional[float] = None
        self.manual_on: list[str] = []
        self.manual_timers: dict[tuple[str, str], float] = {}
        self.resuming: Optional[dict] = None
        self.power_wait: Optional[str] = None   # "cut" | "restoring" | "restart"
        self.resume_at: Optional[float] = None
        self.resume_cause = "power"
        self.date: Optional[str] = None
        self.date_trusted = True                # st.date came from a clock that could be trusted
        self.pause_rederive = False             # a timed pause restored on a clock not trusted: re-derived when it is
        self.await_clock: Optional[str] = None  # interrupted runs wait for a trusted clock to resume (the cause)
        self.commit_offs: list[tuple[PortRef, bool]] = []  # lines written off when the take-over is committed

    def copy(self) -> "_State":
        s = _State.__new__(_State)
        s.__dict__.update(self.__dict__)
        s.runs = {k: r.copy() for k, r in self.runs.items()}
        s.sched = {k: dict(v) for k, v in self.sched.items()}
        for f in ("orphans", "releases", "now_releases", "line_releases", "release_events", "switches",
                  "pending_events", "manual_on", "commit_offs"):
            setattr(s, f, list(getattr(self, f)))
        s.held = {k: set(v) for k, v in self.held.items()}
        s.hold_runs = {k: dict(v) for k, v in self.hold_runs.items()}
        for f in ("inputs", "input_changed", "builtin_seen", "manual_timers", "automation"):
            setattr(s, f, dict(getattr(self, f)))
        s.debounce = {k: dict(v) for k, v in self.debounce.items()}
        return s


class RuntimeV3:
    LANGUAGE = P.LANGUAGE

    def __init__(self, io: IOAdapter, sink: EventSink, clock: Optional[Clock] = None, state_dir: Optional[str] = None,
                 tick_seconds: float = 1.0, node_uid: str = "", lock: Optional[threading.RLock] = None,
                 listen: bool = True):
        self.io = io
        self.sink = sink
        self.clock = clock or SystemClock()
        self.state_dir = state_dir
        self.tick_seconds = tick_seconds
        self.node_uid = node_uid
        self.prog: Optional[Program] = None
        self.st = _State()
        self._lock = lock or threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._sampler: Optional[threading.Thread] = None
        self._running = False
        # Stopped and not started since (contracts v1.15.2 item 16): no pass will run, so no command is accepted. Set
        # by stop() together with _running, cleared only by start() — a compile after the stop (a program pulled
        # while the agent shuts down) runs no pass either.
        self._stopped = False
        self._wake = threading.Event()
        self._cold = True
        self._booted = False
        self._applied_pending = False
        self._pending_commands: list[dict] = []
        # The answers of the commands a pass carried out itself — a resetValve's (contracts v1.15.2 item 15):
        # (commandId, status, the node's words), handed to the sink when the pass ends.
        self._pass_acks: list[tuple[str, str, Optional[str]]] = []
        self._persist_due = False
        self._last_persist: Optional[float] = None
        self._clean_stop_persisted = False
        self._pass_events: list[tuple[str, dict, str]] = []
        self._noted_runs: set[str] = set()
        self._events_now: list[tuple[str, dict]] = []
        self._alert_last: dict[tuple[str, str], float] = {}
        self._persist_failing = False
        # The outputs reported per card (variables.outputs, functions.yaml 1.11.2) and "report every card at the
        # next pass" (after a load).
        self._outputs_reported: dict[str, dict] = {}
        self._report_all = True
        # Called after every pass of this runtime's own loop (a ProgramHost holding a switch of language).
        self.after_pass: Optional[Callable[[], None]] = None
        if listen:
            self.io.set_change_listener(self._on_input_change)

    # ------------------------------------------------------------- properties

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def loaded(self) -> bool:
        return self.prog is not None

    @property
    def version(self) -> int:
        return self.prog.version if self.prog else 0

    @property
    def bundle(self) -> Optional[dict]:
        return self.prog.bundle if self.prog else None

    @property
    def tz(self) -> str:
        return self.prog.tz if self.prog else "Asia/Kolkata"

    # ---------------------------------------------------------------- compile

    def compile(self, bundle: dict) -> None:
        """Check a v3 bundle and load it — atomic: on any error the running program stays."""
        prog = P.build(bundle, self.io)
        with self._lock:
            warm = self.prog is not None and not self._cold
            refusal = self.latch_refusal(prog)
            if refusal:
                logger.error("program v%s refused: %s", prog.version, refusal)
                raise CompileError(refusal)
            try:
                st = self._take_over(prog) if warm else self._cold_load(prog)
            except CompileError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error("program v%s could not take over from the running program: %s", prog.version, e, exc_info=True)
                raise CompileError(f"could not take over from the running program: {e}") from e
            if not warm and not self._persist(prog=prog, st=st):
                # The start is recorded as not clean before anything moves (README §7.9): if it cannot be, a later
                # restart would vouch for positions that changed since. Nothing pulses a latching valve until the
                # node can remember what it did.
                self._cannot_record(prog, st)
            self.prog, self.st = prog, st
            self._cold = False
            self._applied_pending = True
            self._persist_due = True
            self._report_all = True
            self._commit_offs()
        logger.info("program v%d (language 3) compiled%s: %d cards, %d rule slots, %d instruction parts",
                    prog.version, " (took over from the running program)" if warm else "", len(prog.cards),
                    len(prog.rules), len(prog.parts))

    def latch_refusal(self, prog: Program) -> Optional[str]:
        """README §7.10: a program without the E-stop is refused while the E-stop is latched — the latch belongs
        to no program then, and nothing would keep the motors off until the panel reset. Checked against the
        running program, and, before a cold load, against the latch the node persisted."""
        if prog.estop is not None:
            return None
        with self._lock:
            if self.prog is not None and self.prog.estop is not None and self.prog.estop.latched:
                return LATCH_KEPT
            if self.prog is None or self._cold:
                saved = self._read_state() or {}
                if (saved.get("estop") or {}).get("latched"):
                    return LATCH_KEPT
        return None

    def _cannot_record(self, prog: Program, st: _State) -> None:
        wall = self.clock.now(prog.tz)
        detail = "the node cannot write its state directory: it could not vouch for the position after a restart"
        for aid, drv in prog.valves.items():
            if drv.latching and drv.set_fault("position_unknown", detail, wall):
                st.pending_events.append(("__valve_fault__", {"assetId": aid, "reason": "position_unknown", "detail": detail}))
        logger.error("the v3 state cannot be written: every latching valve is in FAULT until it can be")

    def _commit_offs(self) -> None:
        """Lines an old output drove that the new program reads as inputs: written off and let go of as the
        take-over is committed — before anything samples them as an input (the sampler runs between passes)."""
        offs, self.st.commit_offs = self.st.commit_offs, []
        for port, _ in sorted(offs, key=lambda o: 0 if o[1] else 1):  # motors first
            try:
                self.io.write_output(port, False)
                self.io.release_output(port)
            except Exception as e:  # noqa: BLE001
                logger.error("switching off %s of card %s before it is read as an input failed: %s", port.source_key,
                             port.asset_id, e)

    # --------------------------------------------------------- the cold load

    def _cold_load(self, prog: Program) -> _State:
        """The first program, or the first after a stop: built from the persisted state, as after a restart."""
        now, wall = self.clock.monotonic(), self.clock.now(prog.tz)
        clock_ok = self.clock.clock_ok()
        st = _State()
        saved = self._read_state()
        first_start = saved is None
        saved = saved or {}
        unreadable = bool(saved.get(UNREADABLE))
        # The node's date. On a clock that cannot be trusted (no RTC, no NTP yet) it is the last date a trusted
        # clock gave, kept in the state; the midnight switch waits for a trusted clock (README §7.6).
        if clock_ok:
            st.date, st.date_trusted = wall.date().isoformat(), True
        else:
            kept = saved.get("date") if saved.get("dateTrusted") else None
            st.date, st.date_trusted = (kept or wall.date().isoformat()), bool(kept)
        clean = bool(saved.get("cleanStop"))
        valves = saved.get("valves") or {}
        for aid, drv in prog.valves.items():
            if not drv.latching:
                continue
            ent = valves.get(aid)
            if isinstance(ent, str):
                ent = {"position": ent}
            ent = ent if isinstance(ent, dict) else None
            reason = None
            if first_start or ent is None:
                reason = "position_unknown"
            elif ent.get("config") and not same_configuration(ent["config"], drv.signature):
                # A changed configuration (contracts v1.26.0: a port id stated on one side only — a state agent 0.4.3
                # wrote, a bundle compiled before v1.26.0 — is not a change).
                reason = "position_unknown"
            elif ent.get("pulse"):
                reason = "pulse_cut_short"
            elif ent.get("position") == "fault":
                drv.fault, drv.fault_detail = ent.get("faultReason") or "position_unknown", "in fault before the restart"
                continue
            elif not clean:
                reason = "unclean_boot"
            elif ent.get("position") in ("open", "closed"):
                # Vouched for: a clean start. Agent 0.4.4 (contracts v1.26.0, README §7.9 — the founder's rule): the boot
                # pass sends it one close pulse, whatever position it recorded (start_close); an interrupted run re-opens
                # it when it resumes, 30 s later.
                drv.position = ent["position"]
                drv.start_close = True
            else:
                reason = "position_unknown"
            if reason:
                drv.set_fault(reason, FAULT_WORDS[reason], wall)
                st.pending_events.append(("__valve_fault__", {"assetId": aid, "reason": reason}))
            if isinstance(ent, dict) and ent.get("pulse") in ("open", "close"):
                # A pulse was in progress when the node stopped: that coil may still be on. Written off at the
                # boot pass, before anything else is pulsed.
                drv.coil_state[drv.open_key if ent["pulse"] == "open" else drv.close_key] = None
        for aid, g in prog.guards.items():
            ent = (saved.get("guards") or {}).get(aid)
            if isinstance(ent, dict):
                g.load_state(ent, now, wall, clock_ok)
        es = saved.get("estop") or {}
        if prog.estop is not None and (es.get("latched") or unreadable):
            # An unreadable state is read as latched — the safe reading, as for an unreadable contact.
            prog.estop.latched = True
            prog.estop.pressed_at = _parse(es.get("pressedAt"))
            prog.estop.latched_mono = now
            if unreadable:
                logger.error("the v3 state is unreadable: the E-stop is taken as latched — it clears at the panel")
        auto = saved.get("automation") or {}
        if auto.get("state") == "paused":
            st.paused = True
            st.automation = {k: v for k, v in auto.items() if k in ("state", "pausedAt", "pausedBy", "resumeAt", "manualOn")}
            st.manual_on = list(auto.get("manualOn") or [])
            st.paused_mono = now
            until = _parse(auto.get("resumeAt"))
            if until is not None and not clock_ok:
                st.pause_until, st.pause_rederive = None, True  # how long it has left is known once the clock is
            else:
                st.pause_until = self._pause_end(until, now, wall)
        for k, v in (saved.get("schedules") or {}).items():
            if isinstance(v, dict):
                st.sched[k] = {"lastRunDate": v.get("lastRunDate"), "missedDate": v.get("missedDate")}
                if v.get("lastRunStart") is not None:
                    st.sched[k]["lastRunStart"] = v["lastRunStart"]
        # Runs in progress when the node stopped were interrupted by the restart: they resume 30 s after
        # the boot pass, inside their window (README §7.8). Their outputs start off, as every output does.
        saved_at = _parse(saved.get("savedAt"))
        for k, r in (saved.get("runs") or {}).items():
            run = self._run_from_state(r, prog, now, saved_at)
            if run is None:
                continue
            part = prog.active_part(run.layout, st.date)
            if (part is None or run.sid not in part.schedules) and not clock_ok:
                # On a clock not trusted the date may be wrong: the part it ran in, if the program still has it.
                # Whether it resumes is decided once the clock is trusted (or its window has certainly passed).
                kept = prog.block(run.part_key)
                part = kept if kept is not None and kept.kind == "part" and run.sid in kept.schedules else part
            if part is None or run.sid not in part.schedules:
                logger.info("run %s was in progress at the stop; its schedule is not in the program now: it is over", run.key)
                continue
            run.part_key = part.key
            st.runs[run.key] = run
        if st.runs:
            st.power_wait, st.resume_cause = "restart", "restart"
            st.resume_at = None  # set at the boot pass
        return st

    def _run_from_state(self, r: dict, prog: Program, now: float, saved_at: Optional[datetime]) -> Optional[Run]:
        try:
            start, planned_end = _parse(r["start"]), _parse(r["plannedEnd"])
            if start is None or planned_end is None:
                return None
            run = Run(r["layout"], r["sid"], r["partKey"], start, float(r["duration"]), float(r.get("window") or 3600), now)
            run.planned_end = planned_end
            run.state = "interrupted"
            ref = saved_at or start
            if r.get("state") == "interrupted" and r.get("remaining") is not None:
                run.remaining = float(r["remaining"])
            else:
                run.remaining = max(0.0, (planned_end - ref).total_seconds())
            run.end_mono = None
            run.interrupted_at = now
            run.window_mono_end = now + max(0.0, (run.window_end - ref).total_seconds())
            run.lost = float(r.get("lost") or 0.0)
            for s in r.get("onKeys") or []:
                a, _, k = str(s).partition(" ")
                run.on_keys.add((a, k))
            return run
        except Exception as e:  # noqa: BLE001 — a state file that cannot be used never keeps a program out
            logger.warning("persisted run %s not used: %s", r, e)
            return None

    # -------------------------------------------------------------- the swap

    def _take_over(self, new: Program) -> _State:
        """Seed the program just built from the one that runs (README §7.5). Mutates only `new` and a copy of
        the state, so a failure leaves the running program untouched."""
        old, st = self.prog, self.st.copy()
        now, wall = self.clock.monotonic(), self.clock.now(new.tz)
        today = st.date or wall.date().isoformat()
        # The built-ins' state is never reset by a reload.
        for aid, g in new.guards.items():
            if aid in old.guards:
                g.carry_from(old.guards[aid])
        for aid, drv in new.valves.items():
            od = old.valves.get(aid)
            if od is None:
                if drv.latching:
                    drv.set_fault("position_unknown", FAULT_WORDS["position_unknown"], wall)
                    st.pending_events.append(("__valve_fault__", {"assetId": aid, "reason": "position_unknown"}))
                continue
            if same_configuration(od.cfg, drv.cfg) or (not drv.latching and not od.latching):
                # Contracts v1.26.0: a bundle that only adds the port ids (words) is not a changed configuration.
                drv.carry_from(od)
            else:  # a changed configuration: its position cannot be vouched for
                drv.carry_from(od)
                drv.position = None
                if drv.latching:
                    drv.fault = None
                    drv.set_fault("position_unknown", FAULT_WORDS["position_unknown"], wall)
                    st.pending_events.append(("__valve_fault__", {"assetId": aid, "reason": "position_unknown"}))
        if new.estop is not None and old.estop is not None:
            new.estop.carry_from(old.estop)
        elif old.estop is not None and old.estop.latched:
            raise CompileError(LATCH_KEPT)  # compile() refuses it first; never taken whatever the path
        if new.power is not None and old.power is not None:
            new.power.carry_from(old.power)
        # Outputs: carried by their stable key.
        for key, out in new.outputs.items():
            o = old.outputs.get(key)
            if o is None:
                continue
            out.level, out.changed_at, out.last_writer = o.level, o.changed_at, o.last_writer
            if out.port is not None and o.port is not None and (out.port.transport == o.port.transport or (
                    str(out.port.transport.get("kind", "gpio")) == "gpio" and same_output_line(o.port.transport, out.port.transport))):
                # 0.4.1: the same line driven the same way — a recompile that only states what was left out before (an
                # explicit activeLow false) writes nothing (bumpless).
                out.written = o.written
        for key in old.outputs:
            if key not in new.outputs:
                st.release_events.append(key)
        # Every line the old program drove, by what uses it now (§7.5: the swap switches nothing by itself, and
        # what it lets go of is switched off — 0.4.1: and kept driven at off, never let go of to its pull):
        # * nothing — written off at the switch pass, its request kept (an active-low relay on an undriven, pulled-down
        #   line energises — the Pump house, 2026-10-17);
        # * the same output, or the same valve coil — carried, nothing written;
        # * an input — REFUSED (0.4.1, contracts v1.15.3, README §7.5a): the node never turns a line it drives into one it
        #   reads; the running program stays and no line is touched;
        # * a latching coil — the driver writes it off before any pulse ("never both coils at once");
        # * another output or a continuous coil — its first write at the switch pass sets the line.
        now_roles = self._line_roles(new)
        for line, (port, is_motor, level, role) in self._lines_driven(old).items():
            use = now_roles.get(line)
            if use is None:
                st.line_releases.append((port, is_motor))
            elif use == role:
                continue
            elif use[0] == "in":
                raise CompileError(f"refused: {port.source_key} of card {port.asset_id} ({describe_port(port.transport)}) is driven by "
                                   "the running program and this program would read it as an input — the node never lets go of a "
                                   "driven line to its pull; the running program stays")
            elif use[0] == "coil" and use[2] != "continuous":
                new.valves[use[1]].coil_state[use[2]] = level
        # …and a line the old program READS that the new one would drive: the line would change direction (0.4.1).
        old_roles = self._line_roles(old)
        for line, role in old_roles.items():
            use = now_roles.get(line)
            if role[0] == "in" and use is not None and use[0] != "in":
                ref = next((r for aid in old.card_order for r in old.cards[aid].ports.values() if physical_port(r.transport) == line), None)
                named = f"{ref.source_key} of card {ref.asset_id} ({describe_port(ref.transport)})" if ref is not None else str(line)
                raise CompileError(f"refused: {named} is read by the running program and this program would drive it — the line "
                                   "would change direction; the running program stays")
        # A card that becomes a guarded motor while its output is on: the guard judges it from the switch (its
        # inrush window starts now) — it is always on (§7.11).
        for aid, g in new.guards.items():
            out = new.outputs.get((aid, g.output))
            if aid not in old.guards and out is not None and out.written is not None and is_on(out.written):
                g.started(now)
        # Inputs keep their sampled value (and since()) by their key.
        keep = {(r.asset_id, r.source_key) for r in new.inputs}
        for d in (st.inputs, st.input_changed, st.debounce):
            for k in list(d):
                if k not in keep:
                    d.pop(k)
        # Blocks: variables by (block key, name) when unchanged; rules by (block key, rule id).
        for blk in new.blocks():
            ob = old.block(blk.key)
            if ob is not None:
                self._carry_block(ob, blk)
        st.held = {k: v for k, v in st.held.items() if new.block(k) is not None}
        st.hold_runs = {k: v for k, v in st.hold_runs.items() if k in st.held}
        # Layouts: the active part of each, and the runs carried across its parts.
        for lk in list(dict.fromkeys(old.layout_order + new.layout_order)):
            op, np_ = old.active_part(lk, today), new.active_part(lk, today)
            same = (op is None and np_ is None) or (op is not None and np_ is not None and op.key == np_.key
                                                    and op.version == np_.version and op.signature == np_.signature)
            self._switch_runs(st, lk, old, op, new, np_, now, wall)
            if not same:
                st.switches.append(self._switch_record(lk, op, np_, "confirmed"))
        # What no block writes any more (ADR-029 decision 7).
        self._plan_releases(st, old, new, today, now, wall)
        return st

    @staticmethod
    def _line_roles(prog: Program) -> dict:
        """physical line → what the program uses it as: ("in",), ("out", key), ("coil", assetId, "continuous") or a
        latching coil ("coil", assetId, coil key)."""
        roles: dict = {}
        for aid in prog.card_order:
            drv = prog.valves.get(aid)
            coils = ({k: k for k, _ in drv.coils()} if drv.latching else {drv.open_key: "continuous"}) if drv is not None else {}
            for sk, ref in prog.cards[aid].ports.items():
                line = physical_port(ref.transport)
                if not P.is_output_port(ref.transport):
                    roles.setdefault(line, ("in",))
                elif sk in coils:
                    roles[line] = ("coil", aid, coils[sk])
                else:
                    roles[line] = ("out", (aid, sk))
        return roles

    @classmethod
    def _lines_driven(cls, prog: Program) -> dict:
        """physical line → (port, is a motor, how the program left it — True on, False off, None unknown, role) for
        every output line of the program. A line never written is off: every output starts off at a cold load."""
        roles = cls._line_roles(prog)
        out: dict = {}
        for aid in prog.card_order:
            drv = prog.valves.get(aid)
            for sk, ref in prog.cards[aid].ports.items():
                if not P.is_output_port(ref.transport):
                    continue
                line = physical_port(ref.transport)
                role = roles[line]
                if role[0] == "coil" and role[2] != "continuous":
                    level = drv.coil_state.get(sk, False)
                elif role[0] == "coil":
                    level = bool(drv.coil_written) if drv.coil_written is not None else False
                else:
                    o = prog.outputs.get(role[1])
                    level = is_on(o.written) if o is not None and o.written is not None else False
                    out[line] = (ref, bool(o is not None and o.is_motor), level, role)
                    continue
                out[line] = (ref, False, level, role)
        return out

    @staticmethod
    def _carry_block(ob: Block, blk: Block) -> None:
        for name, v in blk.vars.items():
            ov = ob.vars.get(name)
            if ov is None:
                continue
            if blk.binds.get(name) is not None:
                if ob.binds.get(name) == blk.binds.get(name):
                    v.value, v.last_change = ov.value, ov.last_change
                continue
            if ob.binds.get(name) is None and _signature(ov.d) == _signature(v.d):
                v.value, v.last_change = ov.value, ov.last_change
        for n in blk.baselines:
            ov = ob.vars.get(n)
            if ov is not None and _signature(ov.d) == _signature(blk.vars[n].d):
                blk.baselines[n] = ob.baselines.get(n)
                if n in ob._learn:
                    blk._learn[n] = dict(ob._learn[n])
        old_rules = {r.id: r for r in ob.rules}
        for r in blk.rules:
            o = old_rules.get(r.id)
            if o is None or _signature(o.d) != _signature(r.d):
                continue  # a new or changed rule starts afresh; nothing else does
            for f in r.STATE:
                setattr(r, f, getattr(o, f))

    def _switch_runs(self, st: _State, layout: str, old_prog: Program, old_part: Optional[Block], new_prog: Program,
                     new_part: Optional[Block], now: float, wall: datetime) -> None:
        """The runs of `layout` across a switch of its part (README §7.5–7.6): a running schedule is carried by
        its id into the new part — it keeps its start, its end becomes start + its new duration, and its new
        start actions are applied at the switch pass; a schedule that is gone runs its old end actions at its
        planned end."""
        for key in [k for k, r in st.runs.items() if r.layout == layout]:
            run = st.runs[key]
            src = old_prog.block(run.part_key) or old_part
            s_old = src.schedules.get(run.sid) if src is not None else None
            s_new = new_part.schedules.get(run.sid) if new_part is not None else None
            if s_new is not None:
                run.part_key = new_part.key
                run.duration = float(s_new["durationMinutes"]) * 60.0
                run.window_s = float(s_new.get("startWindowMinutes") or 60) * 60.0
                run.planned_end = run.start_wall + timedelta(seconds=run.duration)
                if run.state == "running":
                    run.end_mono = now + max(0.0, (run.planned_end - wall).total_seconds())
                before = {P.stable_action(src, a) for a in (s_old.get("actions") or [])} if s_old is not None and src is not None else set()
                diff = [dict(a) for a in s_new.get("actions") or [] if P.stable_action(new_part, a) not in before]
                if diff:
                    run.pending = (run.pending or []) + diff
                run.on_keys |= P.start_set(new_part, s_new)
                # The carried run IS that schedule's run today: its new start does not start it again.
                st.sched.setdefault(key, {})["lastRunStart"] = _start_spec(s_new)
                continue
            st.runs.pop(key)
            if src is None or s_old is None:
                logger.info("run %s left the program: it is over", key)
                continue
            ends = {"layout": layout, "sid": run.sid, "partKey": run.part_key,
                    "actions": [P.stable_action(src, a) for a in s_old.get("endActions") or []],
                    "raw": [dict(a) for a in s_old.get("endActions") or []], "planned_end": _iso(run.planned_end)}
            if run.state == "running":
                st.orphans.append(dict(ends, end=run.end_mono))
                continue
            # Interrupted (a power cut, a restart) and gone: it can never resume, but it is still a run in progress —
            # what it opened stays open (a latching valve on its UPS) until its old end actions run. They run at
            # once; an orphaned end waits while the power does, so they run when the power is back, before the rules
            # start the pump again. A notice that its run was cut short.
            logger.info("run %s left the program while interrupted: its old end actions run when the power is back", key)
            st.orphans.append(dict(ends, end=now, quiet=True))
            gap = round(max(0.0, now - (run.interrupted_at if run.interrupted_at is not None else now)), 1)
            st.pending_events.append(("__emit__", {"type": "run_cut_short", "source": "program", "layout": layout,
                                                   "payload": {"scheduleId": run.sid, "gapSeconds": gap}}))

    def _switch_record(self, layout: str, old_part: Optional[Block], new_part: Optional[Block], cause: str) -> dict:
        ck, _, cid = layout.partition(":")
        return {"layout": layout, "containerKind": ck, "containerId": cid,
                "fromPartId": old_part.part_id if old_part else None, "toPartId": new_part.part_id if new_part else None,
                "validity": new_part.validity if new_part else None, "cause": cause, "released": []}

    def _plan_releases(self, st: _State, old: Program, new: Program, today: str, now: float, wall: datetime) -> None:
        old_w, new_w, all_new = old.written_on(today), new.written_on(today), new.all_written()
        st.releases = [r for r in st.releases if r["key"] not in new_w and r["key"] in new.outputs]
        st.now_releases = [r for r in st.now_releases if r["key"] not in new_w and r["key"] in new.outputs]
        pending = {r["key"] for r in st.releases} | {r["key"] for r in st.now_releases}
        for key, out in new.outputs.items():
            if key in new_w or key in pending:
                continue
            ow = old.writer_on(key, today)
            if ow is None and not (out.last_writer and old.block(out.last_writer) is not None and key in old.outputs):
                continue  # never written by a block (switched by hand): not a release
            if not is_on(out.level):
                if key not in all_new and key in old_w:
                    st.release_events.append(key)
                continue
            # The run that set it: the run whose start actions set it, else the runs in progress on the layout of
            # the block that last wrote it (for a rule, the latest planned end among its layout's runs).
            runs = {k for k, r in st.runs.items() if key in r.on_keys}
            if not runs:
                lb = (old.block(out.last_writer) if out.last_writer else None) or ow
                if lb is not None:
                    runs = {k for k, r in st.runs.items() if r.layout == lb.layout}
            wb = ow or (old.block(out.last_writer) if out.last_writer else None)
            if new.handover.get(key) == "now" or not runs:
                st.now_releases.append({"key": key, "layout": wb.layout if wb is not None else None})
            else:
                until = max((st.runs[k].planned_end for k in runs), default=None)
                st.releases.append({"key": key, "runs": runs, "until": _iso(until), "layout": st.runs[sorted(runs)[0]].layout})
        for sw in st.switches:
            for r in st.releases + st.now_releases:
                if r.get("layout") == sw["layout"]:
                    item = {"assetId": r["key"][0], "sourceKey": r["key"][1], "until": r.get("until") or _iso(wall)}
                    if item not in sw["released"]:
                        sw["released"].append(item)

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stopped = False
            self._report_all = True  # every start reports every card's outputs at its first pass (0.4.0 review)
        self._thread = threading.Thread(target=self._loop, name="blocks-v3-runtime", daemon=True)
        self._thread.start()
        if not isinstance(self.io, SimulatedIO):
            self._sampler = threading.Thread(target=self._sample_loop, name="blocks-v3-sampler", daemon=True)
            self._sampler.start()

    def stop(self, safe_state: bool = True, wait_record: bool = True) -> None:
        """``wait_record=False`` (the host's switch of language only): the clean-stop record is handed to the state
        file's writer without waiting for it — it is read only at a later v3 load, which waits for every record
        handed over before it (``_read_state``)."""
        # Contracts v1.15.2: only the threads this runtime started BEFORE this stop are joined — read with the flag,
        # under the lock — never one a start made meanwhile (a stop that joined that one waited out its timeout and
        # left two runtimes running).
        with self._lock:
            threads = (self._thread, self._sampler)
            self._running = False
            self._stopped = True  # from here no command is accepted (apply_command): no pass would carry it out
        self._wake.set()
        for t in threads:
            if t is not None and t is not threading.current_thread():
                t.join(timeout=3.0)
        with self._lock:
            if safe_state:
                # Agent 0.4.4 (README §7.9): the safe state, then every single-pulse valve's close pulse — and only then
                # the clean stop is recorded, so the next start finds each one closed (or in FAULT).
                self.apply_safe_state()
            self._persist(clean=True, wait=wait_record)
            self._cold = True
            self._booted = False
            # A reset accepted but not carried out — no pass ran it — is answered, never left in flight (and never
            # pulsed by a later start of this runtime; one that arrives after this stop is refused on arrival).
            for c in [c for c in self._pending_commands if c.get("type") == "resetValve"]:
                self._answer(c, "failed", RESET_STOPPED)
            self._pending_commands = [c for c in self._pending_commands if c.get("type") != "resetValve"]
            # What the stop raised (valve_closed, a valve_fault) goes out like a pass's events: no pass runs after it.
            events, self._pass_events = self._pass_events, []
        for t, pl, src in events:
            try:
                self.sink.event(t, pl, source=src)
            except Exception as e:  # noqa: BLE001 — telling the cloud never stops a stop
                logger.error("the stop's %s event could not be handed over: %s", t, e)
        self._send_acks()

    def apply_safe_state(self) -> None:
        """A clean stop: every motor and every other output whose safeState is off is switched off, each
        continuous valve closes; then (agent 0.4.4, contracts v1.26.0, README §7.9 — the founder's rule of 2026-10-14)
        every single-pulse valve that is not in FAULT gets ONE close pulse, in assets[] order, whatever position it
        recorded: recorded as any pulse is, with ``valve_closed`` (cause shutdown). Its supply is read first: not
        present (or not readable) puts it in FAULT (``supply_lost``) and no pulse is sent; absent after the pulse is
        ``pulse_cut_short``. The caller records the clean stop after this."""
        p = self.prog
        if p is None:
            return
        for key in sorted(p.output_order, key=lambda k: (0 if p.outputs[k].is_motor else 1, p.outputs[k].index)):
            out = p.outputs[key]
            try:
                if out.kind == "valve":
                    drv = out.valve
                    if drv is not None and not drv.latching:
                        coil = drv.coil_level(False)
                        self.io.write_output(drv.open_port, coil)
                        drv.coil_written = coil
                    elif drv is not None:
                        for k, port in drv.coils():  # a coil that is not known off is written off; the valve stays
                            if drv.coil_state.get(k, False) is not False:
                                self.io.write_output(port, False)
                                drv.coil_state[k] = False
                    continue
                if out.card.safe_state == "last_state":
                    continue
                self.io.write_output(out.port, off_value(out.type))
                out.written = off_value(out.type)
            except Exception as e:  # noqa: BLE001
                logger.error("safe-state write %s.%s failed: %s", out.asset_id, out.source_key, e)
        self._close_before_stop()

    def _close_before_stop(self) -> None:
        """README §7.9: before a clean shutdown, one close pulse to every single-pulse valve not in FAULT."""
        p = self.prog
        now, wall = self.clock.monotonic(), self.clock.now(p.tz)
        for aid in p.card_order:
            drv = p.valves.get(aid)
            if drv is None or not drv.latching:
                continue
            drv.start_close = False
            try:
                if drv.fault:
                    logger.warning("valve %s is in FAULT (%s): no close pulse before the stop — it stays in FAULT", drv.name, drv.fault)
                    continue
                supply = self._read_supply(drv)
                if supply is None or not bool(supply):
                    detail = (f"{self._supply_phrase(drv)} {'cannot be read' if supply is None else 'is not present'} as the "
                              "node stops: no close pulse sent")
                    self._fault(drv, "supply_lost", detail, now, wall)
                    continue
                if self._pulse(drv, False, now, wall):
                    self._closed_built_in(drv, "shutdown")
            except Exception as e:  # noqa: BLE001 — one valve never keeps the others open
                logger.error("valve %s: the close pulse before the stop failed: %s", drv.name, e)

    def _closed_built_in(self, drv, cause: str) -> None:
        """A single-pulse valve its driver closed, built in (README §7.9): ``valve_closed`` with its cause."""
        self._emit("valve_closed", {"valve": {"assetId": drv.asset_id, "nodeUid": self.node_uid, "cause": cause},
                                    "assetId": drv.asset_id}, NODE)
        self._persist_due = True
        logger.warning("valve %s: one close pulse %s — recorded closed", drv.name,
                       "as the node starts" if cause == "start_up" else "before the node stops")

    def _loop(self) -> None:
        while self._running:
            try:
                self.step()
            except Exception as e:  # noqa: BLE001
                logger.error("runtime pass failed: %s", e, exc_info=True)
            hook = self.after_pass
            if hook is not None and self._running:
                try:
                    hook()
                except Exception as e:  # noqa: BLE001
                    logger.error("after the pass: %s", e, exc_info=True)
            if not self._running:
                break
            self._wake.wait(self.tick_seconds)
            self._wake.clear()

    def _sample_loop(self) -> None:
        """Digital inputs every 100 ms — an E-stop or a flow switch does not wait for the tick."""
        while self._running:
            changed = False
            try:
                with self._lock:
                    if self.prog is not None:
                        changed = self._sample(self.clock.monotonic(), digital_only=True)
            except Exception as e:  # noqa: BLE001
                logger.debug("sampler: %s", e)
            if changed:
                self._wake.set()
            time.sleep(0.1)

    def _on_input_change(self) -> None:
        self._wake.set()

    # --------------------------------------------------------------- the pass

    def step(self) -> None:
        """One pass, then the answers of the commands it carried out (a resetValve's) — handed over even when the
        pass fails half-way, so an accepted reset is never left without one."""
        try:
            self._step()
        finally:
            self._send_acks()

    def _answer(self, c: dict, status: str, error: Optional[str] = None) -> None:
        """Record a command's final answer for the end of the pass (a command with no commandId has no document)."""
        cid = c.get("commandId")
        if cid:
            self._pass_acks.append((str(cid), status, None if error is None else str(error)[:500]))

    def _send_acks(self) -> None:
        """Hand the pass's command answers to the sink — after its events (the valve_reset of the same pass) — whose
        ack queue posts them (blocks/cloud.py CloudSink.command_ack; command_acks.py). Nothing here waits on the
        network (README §4.8); outside the runtime lock."""
        with self._lock:
            acks, self._pass_acks = self._pass_acks, []
        for cid, status, error in acks:
            try:
                self.sink.command_ack(cid, status, error)
            except Exception as e:  # noqa: BLE001 — an answer that cannot be handed over never fails the pass
                logger.error("command %s: its %s ack could not be handed over: %s", cid, status, e)

    def _step(self) -> None:
        with self._lock:
            p, st = self.prog, self.st
            if p is None:
                return
            now, wall = self.clock.monotonic(), self.clock.now(p.tz)
            self._pass_events = []
            self._noted_runs: set[str] = set()
            events_in = [e for e in st.pending_events if not e[0].startswith("__")]
            st.pending_events = [e for e in st.pending_events if e[0].startswith("__")]
            self._events_now = events_in
            self._sample(now, digital_only=False)
            boot = not self._booted
            if self._applied_pending:
                self._applied_pending = False
                events_in.append(("program_applied", {"version": p.version}))
                self.sink.event("program_applied", {"version": p.version})
                self.sink.program_ack(p.version, "applied")
            if boot:
                self._booted = True
                events_in.append(("boot", {}))
                if st.power_wait == "restart":
                    st.resume_at = now + P.RESUME_AFTER_POWER_SECONDS
            for t, pl in [e for e in st.pending_events if e[0] == "__valve_fault__"]:
                self._announce_fault(pl["assetId"], pl["reason"], now, wall, pl.get("detail", ""))
            for t, pl in [e for e in st.pending_events if e[0] == "__emit__"]:
                payload = dict(pl["payload"])
                if pl.get("layout"):
                    payload["program"] = {"version": p.version, "block": "instruction", "containerId": pl["layout"].partition(":")[2],
                                          "slot": "instruction", "nodeUid": self.node_uid}
                self._emit(pl["type"], payload, pl.get("source") or "program")
            st.pending_events = [e for e in st.pending_events if e[0] not in ("__valve_fault__", "__emit__")]
            before = {k: is_on(o.level) for k, o in p.outputs.items()}
            self._midnight(now, wall)
            # 2. the built-ins
            self._power(now, wall)
            self._estop(now, wall)
            self._valve_supplies(now, wall)
            self._guards(now, wall)
            self._apply_commands(now, wall)
            self._automation_timers(now, wall)
            self._holds(now, wall)
            self._now_releases(now)
            # 3. the instruction parts
            today = self._today(wall)
            if st.resuming is not None:
                for blk in p.blocks():
                    if not blk.interlock:
                        self._reset_rules(blk)
            parts_wait = st.paused or st.power_wait is not None
            for lk in p.layout_order:
                part = p.active_part(lk, today)
                if part is None or part.key in st.held or parts_wait:
                    continue
                self._eval_part(part, now, wall, events_in)
            self._orphan_ends(now, wall)
            # 4. the rule slots
            for blk in p.rules:
                if blk.key in st.held or (st.paused and not blk.interlock):
                    continue
                self._eval_block(blk, now, wall, events_in)
            if st.resuming is not None:
                self._resume_manual_off(now, wall)
            self._due_releases(now, wall)
            self._vetoes(now)
            # 5. the outputs
            self._flush(now, wall)
            self._report_outputs()
            self._announce(before, now, wall)
            for t, pl, src in self._pass_events:
                self.sink.event(t, pl, source=src)
            self._pass_events = []
            st.resuming = None
            if self._persist_due or (st.runs and (self._last_persist is None or now - self._last_persist >= KEEPALIVE_PERSIST_SECONDS)):
                # README §4.8 (the second 0.4.0 review): on the runtime's own thread the record is handed to the state
                # file's writer and the pass goes on — no fsync under the lock, so the sampler, a command, the E-stop
                # latch and the motor guards never wait for the SD card. Nothing this pass does next depends on the
                # record being on disk; the acts that do (a latching pulse, a valve reset, a cold start, a clean stop)
                # wait for theirs. step() from a test or the simulator waits, as before.
                self._persist(wait=threading.current_thread() is not self._thread)

    # ----------------------------------------------------------------- inputs

    def _sample(self, now: float, digital_only: bool) -> bool:
        p, st = self.prog, self.st
        changed = False
        for ref in p.inputs:
            digital = P.is_digital_input(ref.transport)
            if digital_only and not digital:
                continue
            try:
                raw = self.io.read_input(ref)
            except Exception as e:  # noqa: BLE001
                logger.debug("read %s.%s failed: %s", ref.asset_id, ref.source_key, e)
                raw = None
            key = (ref.asset_id, ref.source_key)
            if raw is None:
                if st.inputs.get(key) is not None:
                    st.inputs[key] = None  # cannot be read: never acted on as a value
                continue
            if digital:
                raw = bool(raw)
                d = st.debounce.get(key)
                if d is None:
                    # The first reading of an input is its value: a debounce filters changes, not the first value.
                    # (Withheld, an E-stop contact would read as pressed and a sensed supply as lost at every boot.)
                    st.debounce[key] = {"raw": raw, "since": now, "last": now}
                    val = raw
                else:
                    interval = (now - d["last"]) if d["last"] is not None else None
                    d["last"] = now
                    if d["raw"] != raw:
                        d["raw"], d["since"] = raw, now
                    deb = float(ref.transport.get("debounceMs") or 0) / 1000.0
                    if not ((now - d["since"]) >= deb or (interval is not None and interval >= deb)):
                        continue
                    val = raw
            else:
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    continue
            if st.inputs.get(key) != val or key not in st.inputs:
                st.inputs[key] = val
                st.input_changed[key] = now
                changed = True
        return changed

    def _input(self, key: Optional[tuple[str, str]]):
        return self.st.inputs.get(key) if key is not None else None

    # ---------------------------------------------------------------- events

    def _emit(self, type_: str, payload: dict, source: str = "program") -> None:
        self._pass_events.append((type_, payload, source))

    def _queue(self, type_: str, payload: Optional[dict] = None) -> None:
        """An event the on-rules see in the next pass."""
        self.st.pending_events.append((type_, payload or {}))

    # -------------------------------------------------------------- midnight

    def _today(self, wall: datetime) -> str:
        """The node's local date: the wall clock's while it can be trusted, else the last date a trusted clock
        gave (README §7.6 — the node switches part at ITS midnight, on its own clock)."""
        if self.st.date is not None and not self.clock.clock_ok():
            return self.st.date
        return wall.date().isoformat()

    @staticmethod
    def _pause_end(until: Optional[datetime], now: float, wall: datetime) -> Optional[float]:
        """A timed pause's end, monotonic: never in the past, never more than 7 days ahead (pauseAutomation)."""
        if until is None:
            return None
        if until.tzinfo is None and wall.tzinfo is not None:
            until = until.replace(tzinfo=wall.tzinfo)
        return now + min(float(PAUSE_MAX_SECONDS), max(0.0, (until - wall).total_seconds()))

    def _midnight(self, now: float, wall: datetime) -> None:
        st, p = self.st, self.prog
        if not self.clock.clock_ok():
            return  # a date from a clock that cannot be trusted switches nothing
        today = wall.date().isoformat()
        if st.date is None:
            st.date, st.date_trusted = today, True
            return
        if today == st.date:
            st.date_trusted = True
            return
        # The date moved on since the last trusted date: at least one local midnight has passed. When the node's
        # date itself was a guess (a start on a clock not trusted, with no trusted date kept), the part changes
        # with no instruction_switched — no midnight is known to have passed, and the event has no other cause.
        announce = st.date_trusted
        yesterday, st.date, st.date_trusted = st.date, today, True
        for lk in p.layout_order:
            op, np_ = p.active_part(lk, yesterday), p.active_part(lk, today)
            if op is np_:
                continue
            self._switch_runs(st, lk, p, op, p, np_, now, wall)
            rec = self._switch_record(lk, op, np_, "midnight")
            # An output only yesterday's part wrote, still on: kept to the end of the run that set it.
            for key in sorted((op.writes if op else set()) - p.written_on(today)):
                out = p.outputs.get(key)
                if out is None or not is_on(out.level):
                    continue
                runs = {k for k, r in st.runs.items() if key in r.on_keys or r.layout == lk}
                if runs:
                    until = max(st.runs[k].planned_end for k in runs)
                    st.releases.append({"key": key, "runs": runs, "until": _iso(until), "layout": lk})
                    rec["released"].append({"assetId": key[0], "sourceKey": key[1], "until": _iso(until)})
                else:
                    st.now_releases.append({"key": key, "layout": lk})
                    rec["released"].append({"assetId": key[0], "sourceKey": key[1], "until": _iso(wall)})
            if announce:
                st.switches.append(rec)
            logger.info("%s: layout %s switches from %s to %s", "midnight" if announce else "the clock is trusted now", lk,
                        op.part_id if op else None, np_.part_id if np_ else None)

    # ----------------------------------------------------------- power sense

    def _power(self, now: float, wall: datetime) -> None:
        p, st = self.prog, self.st
        ps = p.power
        if ps is not None:
            for supply, what in ps.update(now, wall, self._input(ps.mains), self._input(ps.ups)):
                key = ps.mains if supply == "mains" else ps.ups
                if what == "cut":
                    running = [r for r in st.runs.values() if r.state == "running"]
                    payload = {"nodeUid": self.node_uid, "supply": supply}
                    if supply == "mains":
                        payload["duringWatering"] = bool(running)
                        payload["runningBlocks"] = self._run_refs(running)
                        self._interrupt_runs(now, wall)
                        st.power_wait, st.resume_at = "cut", None
                    self._emit("power_cut", {"power": payload, "assetId": key[0]}, NODE)
                else:
                    payload = {"nodeUid": self.node_uid, "supply": supply}
                    if supply == "mains":
                        payload["outageSeconds"] = round(max(0.0, now - (ps.cut_at if ps.cut_at is not None else now)), 1)
                        payload["resumeAfterSeconds"] = P.RESUME_AFTER_POWER_SECONDS
                        st.power_wait, st.resume_at = "restoring", now + P.RESUME_AFTER_POWER_SECONDS
                        if st.resume_cause != "restart":
                            st.resume_cause = "power"
                        ps.cut_at = None
                        self._queue("power_restored")
                    self._emit("power_restored", {"power": payload, "assetId": key[0]}, NODE)
        if st.power_wait in ("restoring", "restart") and st.resume_at is not None and now >= st.resume_at:
            if ps is not None and ps.mains_present is False:
                st.power_wait, st.resume_at = "cut", None  # restarted on the UPS: wait for mains
                return
            cause = st.resume_cause
            st.power_wait, st.resume_at, st.resume_cause = None, None, "power"
            if self._clock_decides(now):
                self._resume_now(now, wall, cause)
            else:
                # Where a run is in its window cannot be known on a clock that cannot be trusted (no RTC, no NTP
                # yet): the interrupted runs wait — their outputs off — until the clock is trusted, or until their
                # window has certainly passed by the monotonic clock (README §7.8: a run runs only inside its
                # window). The node is not held meanwhile: the rules run, and a person may switch a motor.
                st.await_clock = cause
                logger.warning("the clock cannot be trusted: %s wait to resume until it can",
                               ", ".join(sorted(k for k, r in st.runs.items() if r.state == "interrupted")))
        elif st.await_clock is not None and st.power_wait is None and self._clock_decides(now):
            cause, st.await_clock = st.await_clock, None
            self._resume_now(now, wall, cause)

    def _clock_decides(self, now: float) -> bool:
        """Whether the interrupted runs can be resumed or cut short now: on a trusted clock, or when every one's
        window has certainly passed by the monotonic clock."""
        if self.clock.clock_ok():
            return True
        return all(r.window_mono_end is not None and now >= r.window_mono_end
                   for r in self.st.runs.values() if r.state == "interrupted")

    def _resume_now(self, now: float, wall: datetime, cause: str) -> None:
        self.st.await_clock = None
        self._resume_runs(now, wall, cause, self.clock.clock_ok())
        # "…and the rules start the pump as usual" (README §7.8): the ordinary rule slots restart as after a
        # program load, so a rule whose condition held through the cut (a latching valve stays open on its
        # UPS) starts its motor again. Interlocks keep their state: they ran through the cut.
        for blk in self.prog.blocks():
            if not blk.interlock:
                self._reset_rules(blk)

    def _interrupt_runs(self, now: float, wall: datetime) -> None:
        """Mains dropped: each running run is interrupted; motors are switched off, then the continuous
        valves. A latching valve stays where it is, on its sensed supply."""
        p, st = self.prog, self.st
        for run in st.runs.values():
            if run.state == "running":
                run.state = "interrupted"
                run.remaining = max(0.0, (run.end_mono or now) - now)
                run.end_mono = None
                run.interrupted_at = now
                run.window_mono_end = now + max(0.0, (run.window_end - wall).total_seconds())
        for out in p.outputs.values():
            if out.is_motor or (out.kind == "valve" and out.valve is not None and not out.valve.latching):
                self._set_level(out, off_value(out.type), None, now)

    def _resume_runs(self, now: float, wall: datetime, cause: str, clock_ok: bool = True) -> None:
        """30 s after the power is back (or after the boot pass): each interrupted run resumes for the time it
        had left, inside its window — planned start to planned end + its startWindowMinutes (README §7.8). On a
        clock that cannot be trusted only runs whose window has certainly passed get here: they are cut short."""
        st = self.st
        for key in list(st.runs):
            run = st.runs[key]
            if run.state != "interrupted":
                continue
            lost = now - (run.interrupted_at if run.interrupted_at is not None else now)
            run.lost += max(0.0, lost)
            left = run.remaining or 0.0
            if clock_ok:
                part = self.prog.active_part(run.layout, self._today(wall))
                if part is None or run.sid not in part.schedules:
                    # Kept through a start on a clock not trusted; the part that runs today has no such schedule.
                    st.runs.pop(key)
                    logger.info("run %s: its schedule is not in the part that runs today: it is over", key)
                    continue
                run.part_key = part.key
                room = (run.window_end - wall).total_seconds()
            else:
                room = (run.window_mono_end - now) if run.window_mono_end is not None else 0.0
            if room <= 0 or left <= 0:
                # Its window passed: a notice, never a ring (README §7.8).
                st.runs.pop(key)
                self._emit("run_cut_short", {"scheduleId": run.sid, "gapSeconds": round(lost, 1), "program": self._prog_ref(run)})
                self._end_actions_later(run, wall)
                continue
            run.state = "running"
            run.end_mono = now + min(left, room)
            run.cut_short_at_end = left > room
            run.pending = "__start__"  # its part re-applies its start actions at its next evaluation
            if run.part_key in st.held:
                # Its part holds (a latching valve in FAULT after an unclean boot): its clock runs from now, and
                # the water resumes — and is announced — when the hold clears (README §7.9).
                continue
            self._emit("watering_resumed", {"watering": dict(self._watering(run), cause=cause, remainingSeconds=round(left, 1),
                                                             lostSeconds=round(lost, 1))}, NODE)

    def _end_actions_later(self, run: Run, wall: datetime) -> None:
        """A run that is over without running its end in its part: its end actions as an orphaned end, now."""
        part = self.prog.block(run.part_key)
        sd = part.schedules.get(run.sid) if part is not None else None
        if part is None or sd is None:
            return
        self.st.orphans.append({"end": self.clock.monotonic(), "layout": run.layout, "sid": run.sid, "partKey": run.part_key,
                                "actions": [P.stable_action(part, a) for a in sd.get("endActions") or []],
                                "raw": [dict(a) for a in sd.get("endActions") or []], "planned_end": _iso(run.planned_end),
                                "quiet": True})

    # ---------------------------------------------------------------- E-stop

    def _estop(self, now: float, wall: datetime) -> None:
        p, st = self.prog, self.st
        es = p.estop
        if es is None:
            return
        what = es.update(now, wall, self._input(es.input), self._input(es.reset))
        if what == "pressed":
            motors = [o.asset_id for o in p.outputs.values() if o.is_motor and is_on(o.level)]
            held = [b.ref() for b in p.blocks() if b.drives_motor]
            for o in p.outputs.values():
                if o.is_motor:
                    self._set_level(o, off_value(o.type), None, now)
            self._emit("estop_pressed", {"estop": {"nodeUid": self.node_uid, "motorsStopped": motors, "heldBlocks": held},
                                         "assetId": es.input[0]}, NODE)
            self._persist_due = True
            logger.error("E-STOP pressed: every motor of the node is latched off; it clears only at the panel")
        elif what == "cleared":
            secs = int(max(0.0, now - (es.latched_mono if es.latched_mono is not None else now)))
            self._emit("estop_cleared", {"estop": {"nodeUid": self.node_uid, "resetKind": es.reset_kind, "latchedSeconds": secs},
                                         "assetId": es.input[0]}, NODE)
            self._persist_due = True
            logger.warning("E-stop cleared at the panel after %d s", secs)

    # ---------------------------------------------------------------- valves

    def _valve_supplies(self, now: float, wall: datetime) -> None:
        for aid, drv in self.prog.valves.items():
            if not drv.latching or drv.fault:
                continue
            v = self._input(drv.supply)
            if v is None or not bool(v):
                # Contracts v1.26.0: the port id with the sense card's name ("the supply on DI3 (UPS supply) dropped").
                where = (f"the supply on {self._supply_words(drv)}" if drv.supply_port_id
                         else f"the supply on {drv.supply[1]} of card {drv.supply[0]}")
                detail = f"{where} " + ("cannot be read" if v is None else "dropped")
                if drv.set_fault("supply_lost", detail, wall):
                    self._announce_fault(aid, "supply_lost", now, wall, detail)

    def _announce_fault(self, aid: str, reason: str, now: float, wall: datetime, detail: str = "") -> None:
        held = [b.ref() for b in self.prog.blocks() if aid in b.uses_valves]
        self._emit("valve_fault", {"valve": {"assetId": aid, "nodeUid": self.node_uid, "reason": reason,
                                             "detail": (detail or FAULT_WORDS.get(reason, ""))[:200], "heldBlocks": held},
                                   "assetId": aid}, NODE)
        self._persist_due = True
        logger.error("valve %s in FAULT (%s): the blocks that use it hold", aid, reason)

    # ---------------------------------------------------------------- guards

    def _guards(self, now: float, wall: datetime) -> None:
        p, st = self.prog, self.st
        for aid, g in p.guards.items():
            was = g.tripped
            if g.expire(now) and was:
                self._persist_due = True
            trip, learned = g.evaluate(now, wall, lambda k, a=aid: st.inputs.get((a, k)))
            if learned is not None:
                self._persist_due = True
                self._emit("baseline_learned", {"assetId": aid, "sourceKey": g.current[0] if g.current else g.output,
                                                "baseline": round(learned, 3)})
            if trip is None:
                continue
            out = p.outputs[(aid, g.output)]
            ended = self._end_runs_of_writer(out, "guard_tripped", now)
            self._set_level(out, off_value(out.type), None, now)
            payload = {"assetId": aid, "nodeUid": self.node_uid, "reason": trip.reason, "unit": trip.unit,
                       "lockoutSeconds": g.lockout_seconds, "endedBlocks": ended}
            if trip.measured is not None:
                payload["measured"] = round(trip.measured, 3)
            if trip.limit is not None:
                payload["limit"] = round(trip.limit, 3)
            if g.baseline is not None:
                payload["baselineA"] = round(g.baseline, 3)
            self._emit("guard_tripped", {"guard": payload, "assetId": aid}, NODE)
            self._persist_due = True

    def _end_runs_of_writer(self, out: Output, reason: str, now: float) -> list[dict]:
        """A trip ends the run of the block that had the motor on: the runs in progress on that block's layout."""
        st, p = self.st, self.prog
        if out.last_writer == "manual":
            return []  # switched on by hand: no block had it on, no run of a block is ended
        wb = p.block(out.last_writer) if out.last_writer else None
        if wb is None:
            wb = p.writer_on(out.key, st.date or "")
        if wb is None:
            return []
        refs = []
        for run in st.runs.values():
            if run.layout == wb.layout and run.state == "running":
                run.end_mono, run.short_reason = now, reason
                part = p.block(run.part_key)
                if part is not None and part.ref() not in refs:
                    refs.append(part.ref())
        return refs

    # ------------------------------------------------------------------ holds

    def _holds(self, now: float, wall: datetime) -> None:
        p, st = self.prog, self.st
        faulted = {aid for aid, d in p.valves.items() if d.latching and d.fault}
        latched = p.estop is not None and p.estop.latched
        now_held: dict[str, set] = {}
        for blk in p.blocks():
            reasons = {("valve_fault", aid) for aid in blk.uses_valves & faulted}
            if latched and blk.drives_motor:
                reasons.add(("estop", None))
            if reasons:
                now_held[blk.key] = reasons
        for key, reasons in now_held.items():
            if key not in st.held:
                blk = p.block(key)
                # A held block is not evaluated; the outputs it writes are switched off (motors first, by the
                # end-of-pass order) — the faulted valve itself is not driven.
                for k in blk.writes:
                    out = p.outputs[k]
                    self._set_level(out, off_value(out.type), None, now)
                st.hold_runs[key] = {k: now for k, r in st.runs.items() if r.layout == blk.layout and r.state == "running"}
                logger.warning("%s holds: %s", key, ", ".join(sorted(r[0] for r in reasons)))
            st.held[key] = reasons
        for key in [k for k in st.held if k not in now_held]:
            cleared = st.held.pop(key)
            blk = p.block(key)
            cause = "valve_reset" if any(r[0] == "valve_fault" for r in cleared) else "estop_cleared"
            self._resume_held(blk, st.hold_runs.pop(key, {}), cause, now, wall)

    def _resume_held(self, blk: Block, runs: dict[str, float], cause: str, now: float, wall: datetime) -> None:
        """A held block resumes under its own logic, by its schedules' own clock (README §7.9, §7.10): a run
        still before its planned end carries on until that end; one whose planned end has passed is over and
        is logged. No time is added."""
        st = self.st
        self._reset_rules(blk)
        reason = "valve_fault" if cause == "valve_reset" else "estop"
        keys = set(runs) | ({k for k, r in st.runs.items() if r.part_key == blk.key} if blk.kind == "part" else set())
        for rk in sorted(keys):
            since = runs.get(rk, now)
            run = st.runs.get(rk)
            if run is not None and run.state == "running" and run.end_mono is not None and now < run.end_mono:
                if run.part_key == blk.key:
                    run.pending = "__start__"  # its part re-opens what it had open
                if run.resumed_at != now:
                    run.resumed_at = now
                    self._emit("watering_resumed", {"watering": dict(self._watering(run), cause=cause,
                                                                     remainingSeconds=round(run.end_mono - now, 1),
                                                                     lostSeconds=round(now - since, 1))}, NODE)
                continue
            if run is not None:
                # Its planned end passed while its part held: it is over (ended by its part in this pass).
                run.short_reason, run.end_mono, run.over_while_held = reason, now, True
                continue
            # It ended on its own clock while this block held (its part did not hold): the water was not delivered.
            if rk in self._noted_runs:
                continue
            self._noted_runs.add(rk)
            layout, _, sid = rk.partition("/")
            ck, _, cid = layout.partition(":")
            self._emit("run_cut_short", {"scheduleId": sid, "gapSeconds": round(now - since, 1)})
            self._emit("watering_not_finished", {"watering": {"containerKind": ck, "containerId": cid, "nodeUid": self.node_uid,
                                                              "scheduleId": sid, "reason": reason}}, NODE)

    @staticmethod
    def _reset_rules(blk: Block) -> None:
        """As after a program load: hold timers and fired state cleared, so the block re-asserts what it wants."""
        for r in blk.rules:
            r.true_since, r.fired, r.last_fire, r.ignore_until, r.prev_ignore_after = None, False, None, None, None
            r.limit_since, r.limit_fired = None, False

    # --------------------------------------------------------------- vetoes

    def _vetoes(self, now: float) -> None:
        """A tripped or locked-out guard, a latched E-stop — and, from a mains cut until the runs resume 30 s after
        the power is back (or 30 s after a restart that has runs to resume), the power: watering resumes 30 s
        after the power returns, so no motor starts before it, whatever a rule asks (README §7.8)."""
        p, st = self.prog, self.st
        latched = p.estop is not None and p.estop.latched
        for out in p.outputs.values():
            if not out.is_motor or not is_on(out.level):
                continue
            if latched or st.power_wait is not None or (out.guard is not None and out.guard.vetoes(now)):
                self._set_level(out, off_value(out.type), None, now)

    # ------------------------------------------------------------- releases

    def _now_releases(self, now: float) -> None:
        st, p = self.st, self.prog
        for r in st.now_releases:
            out = p.outputs.get(r["key"])
            if out is not None:
                self._set_level(out, off_value(out.type), None, now)
                out.last_writer = None
                st.release_events.append(r["key"])
        st.now_releases = []

    def _due_releases(self, now: float, wall: datetime) -> None:
        st, p = self.st, self.prog
        keep = []
        for r in st.releases:
            if any(k in st.runs for k in r["runs"]):
                keep.append(r)
                continue
            out = p.outputs.get(r["key"])
            if out is not None:
                self._set_level(out, off_value(out.type), None, now)
                out.last_writer = None
                st.release_events.append(r["key"])
        st.releases = keep

    # -------------------------------------------------------------- blocks

    def _env(self, blk: Block, now: float, wall: datetime) -> dict:
        return {"vars": {n: v.value for n, v in blk.vars.items()}, "settings": blk.settings, "baselines": blk.baselines,
                "since": {n: (now - v.last_change if v.last_change is not None else None) for n, v in blk.vars.items()},
                "clock": {"hour": wall.hour, "minute": wall.minute, "weekday": wall.isoweekday()}}

    def _builtin(self, aid: str, name: str, now: float):
        p = self.prog
        if name.startswith("guard_"):
            g = p.guards[aid]
            return {"guard_tripped": g.tripped, "guard_reason": float(g.reason_index()), "guard_locked_out": g.locked_out(now)}[name]
        if name == "valve_state":
            drv = p.valves[aid]
            return float(drv.state_index(p.outputs[(aid, P.VALVE_OPEN)].level))
        if name == "valve_fault":
            return bool(p.valves[aid].fault)
        if name == "estop_latched":
            return bool(p.estop.latched) if p.estop else False
        if name == "power_present":
            sk = P._sense_port(p, aid)
            v = self.st.inputs.get((aid, sk)) if sk else None
            return None if v is None else bool(v)
        return None

    def _sync_in(self, blk: Block, now: float) -> None:
        p, st = self.prog, self.st
        entry = {}
        for name, bind in blk.binds.items():
            v = blk.vars[name]
            kind = bind[0]
            if kind == "out":
                out = p.outputs[bind[1]]
                val, ts = out.level, out.changed_at
            elif kind == "in":
                val, ts = st.inputs.get(bind[1]), st.input_changed.get(bind[1])
            else:
                val = self._builtin(bind[1], bind[2], now)
                seen = st.builtin_seen.get((bind[1], bind[2]))
                if seen is None or seen[0] != val:
                    st.builtin_seen[(bind[1], bind[2])] = (val, now)
                    seen = (val, now)
                ts = seen[1]
            if val is None:
                continue
            val = _coerce(v.type, val)
            if v.value != val or v.last_change is None:
                v.value, v.last_change = val, ts if ts is not None else now
            if kind == "out":
                entry[name] = v.value
        blk.entry = entry

    def _sync_out(self, blk: Block, now: float) -> None:
        p = self.prog
        for name, before in blk.entry.items():
            v = blk.vars[name]
            if v.value == before:
                continue
            _, key, access = blk.binds[name]
            out = p.outputs[key]
            if access == "write":
                self._set_level(out, _coerce(out.type, v.value), blk.key, now)
            elif access == "stop":
                if not is_on(v.value):
                    self._set_level(out, off_value(out.type), None, now)
                else:
                    logger.warning("%s: '%s' is a stop — it may only be set off; ignored", blk.key, name)
                    v.value = before

    def _set_level(self, out: Output, value, writer: Optional[str], now: float) -> None:
        if out.level != value:
            out.level = value
            out.changed_at = now
        if writer is not None:
            out.last_writer = writer

    def _eval_part(self, part: Block, now: float, wall: datetime, events: list) -> None:
        st = self.st
        self._sync_in(part, now)
        for run in [r for r in st.runs.values() if r.part_key == part.key and r.pending is not None]:
            actions = run.pending
            run.pending = None
            if actions == "__start__":
                sd = part.schedules.get(run.sid) or {}
                actions = sd.get("actions") or []
                run.on_keys |= P.start_set(part, sd)
            self._do_actions(part, actions, now, wall, run.sid)
        self._run_schedules(part, now, wall)
        self._eval_rules(part, now, wall, events)
        self._sync_out(part, now)

    def _eval_block(self, blk: Block, now: float, wall: datetime, events: list) -> None:
        self._sync_in(blk, now)
        self._eval_rules(blk, now, wall, events)
        self._sync_out(blk, now)

    def _eval_rules(self, blk: Block, now: float, wall: datetime, events: list) -> None:
        prev = {n: v.value for n, v in blk.vars.items()}
        for n in blk.order:
            v = blk.vars[n]
            if v.expr_ast is not None:
                val = E.evaluate(v.expr_ast, self._env(blk, now, wall))
                if val is not None:
                    self._set_var(v, _coerce(v.type, val), now)
        for n in list(blk.baselines):
            self._learn_baseline(blk, blk.vars[n], now)
        for r in blk.rules:
            if not r.enabled:
                continue
            try:
                if r.kind == "when":
                    self._rule_when(blk, r, now, wall)
                elif r.kind == "limit":
                    self._rule_limit(blk, r, now, wall)
                elif r.kind == "on":
                    self._rule_on(blk, r, now, wall, prev, events)
                elif r.kind == "sequence":
                    self._rule_sequence(blk, r, now, wall)
            except Exception as e:  # noqa: BLE001
                logger.error("%s rule %s failed: %s", blk.key, r.id, e, exc_info=True)

    @staticmethod
    def _set_var(v, value, now: float) -> bool:
        if value is None:
            return False
        if v.type in ("number", "enum"):
            value = float(value)
        if v.value == value and v.last_change is not None:
            return False
        v.value, v.last_change = value, now
        return True

    def _learn_baseline(self, blk: Block, v, now: float) -> None:
        cfg = v.d.get("baseline") or {}
        lw = blk.vars.get(cfg.get("learnWhen"))
        if lw is None:
            return
        st = blk._learn.setdefault(v.name, {"prev": None, "start": None, "samples": []})
        if st["prev"] is not None and not st["prev"] and lw.value:
            st["start"], st["samples"] = now + float(cfg.get("afterSeconds", 2)), []
        st["prev"] = bool(lw.value)
        if st["start"] is not None and lw.value:
            if now >= st["start"]:
                st["samples"].append(float(v.value))
                if now >= st["start"] + float(cfg.get("windowSeconds", 10)):
                    blk.baselines[v.name] = sum(st["samples"]) / len(st["samples"])
                    st["start"] = None
                    self._persist_due = True
        elif not lw.value:
            st["start"] = None

    def _rule_when(self, blk: Block, r, now: float, wall: datetime) -> None:
        w = r.d.get("when") or {}
        ia = w.get("ignoreAfter")
        if ia:
            cur = bool(blk.vars[ia].value)
            if r.prev_ignore_after is not None and not r.prev_ignore_after and cur:
                r.ignore_until = now + float(w.get("ignoreFirstSeconds") or 0)
            r.prev_ignore_after = cur
            if r.ignore_until is not None and now < r.ignore_until:
                return
        cond = E.evaluate(r.cond_ast, self._env(blk, now, wall))
        if cond is True:
            if r.true_since is None:
                r.true_since = now
            if now - r.true_since >= float(w.get("holdSeconds") or 0):
                retrig = float(w.get("retriggerSeconds") or 0)
                if not r.fired or (retrig > 0 and r.last_fire is not None and now - r.last_fire >= retrig):
                    r.fired, r.last_fire = True, now
                    self._do_actions(blk, r.d.get("actions") or [], now, wall, r.id)
        else:
            r.true_since = None
            if r.fired:
                r.fired = False
                self._do_actions(blk, r.d.get("elseActions") or [], now, wall, r.id)

    def _rule_limit(self, blk: Block, r, now: float, wall: datetime) -> None:
        lim = r.d.get("limit") or {}
        v = blk.vars[lim["variable"]]
        if v.value:
            if r.limit_since is None:
                r.limit_since = v.last_change if v.last_change is not None else now
            if not r.limit_fired and now - r.limit_since >= float(lim.get("maxSeconds")):
                r.limit_fired = True
                self._do_actions(blk, r.d.get("actions") or [], now, wall, r.id)
        else:
            r.limit_since, r.limit_fired = None, False

    def _rule_on(self, blk: Block, r, now: float, wall: datetime, prev: dict, events: list) -> None:
        o = r.d.get("on") or {}
        ev = o.get("event")
        fire = False
        if ev == "variable_changed":
            v = blk.vars[o["variable"]]
            if r.prev_var_value is None:
                r.prev_var_value = (v.value,)
            elif r.prev_var_value[0] != v.value:
                fire = True
            r.prev_var_value = (v.value,)
        else:
            fire = any(e[0] == ev for e in events)
        if not fire:
            return
        if r.filter_ast is not None and E.evaluate(r.filter_ast, self._env(blk, now, wall)) is not True:
            return
        self._do_actions(blk, r.d.get("actions") or [], now, wall, r.id)

    def _rule_sequence(self, blk: Block, r, now: float, wall: datetime) -> None:
        if not r.seq_active:
            return
        s = r.d.get("sequence") or {}
        if r.abort_ast is not None and E.evaluate(r.abort_ast, self._env(blk, now, wall)) is True:
            r.seq_active = False
            self._do_actions(blk, s.get("abortActions") or [], now, wall, r.id)
            return
        steps = s.get("steps") or []
        while r.seq_active and r.seq_step < len(steps):
            stp = steps[r.seq_step]
            if r.seq_next_at is None:
                r.seq_next_at = now + float(stp.get("delaySeconds") or 0)
            if now < r.seq_next_at:
                return
            self._do_actions(blk, stp.get("actions") or [], now, wall, r.id)
            r.seq_step += 1
            r.seq_next_at = None
        r.seq_active = False

    def _do_actions(self, blk: Block, actions: list, now: float, wall: datetime, rule_id: Optional[str]) -> None:
        for a in actions:
            t = a.get("type")
            if t == "set":
                v = blk.vars[a["variable"]]
                if "_ast" in a:
                    val = E.evaluate(a["_ast"], self._env(blk, now, wall))
                elif a.get("expression"):
                    val = self._evaluate_src(blk, a["expression"], now, wall)
                else:
                    val = a.get("value")
                if val is None:
                    continue
                if v.kind == "alert":
                    self._alert(blk, str(val), a.get("severity") or (v.d.get("alert") or {}).get("defaultSeverity") or "warning", rule_id, now)
                else:
                    self._set_var(v, _coerce(v.type, val), now)
            elif t == "alert":
                self._alert(blk, self._fmt(blk, a.get("text", "")), a.get("severity") or "warning", rule_id, now)
            elif t == "log":
                self.sink.log(self._fmt(blk, a.get("text", "")), None)
            elif t == "start_sequence":
                for r in blk.rules:
                    if r.id == a.get("sequenceId"):
                        r.seq_active, r.seq_step, r.seq_next_at = True, 0, None

    def _evaluate_src(self, blk: Block, src: str, now: float, wall: datetime):
        try:
            ast = E.parse(src)
        except E.ExprError:
            return None
        _, errs = E.validate(ast, blk.scope())
        if errs:
            return None
        return E.evaluate(ast, self._env(blk, now, wall))

    def _fmt(self, blk: Block, text: str) -> str:
        out = text
        for n, v in blk.vars.items():
            token = "{" + n + "}"
            if token in out:
                val = v.value
                if isinstance(val, float):
                    val = f"{val:.2f}".rstrip("0").rstrip(".")
                out = out.replace(token, str(val))
        return out

    def _alert(self, blk: Block, text: str, severity: str, rule_id: Optional[str], now: float) -> None:
        cooldown = float(((blk.alert_var.d.get("alert") if blk.alert_var else {}) or {}).get("cooldownSeconds", 300))
        last = self._alert_last.get((blk.key, text))
        if last is not None and now - last < cooldown:
            return
        self._alert_last[(blk.key, text)] = now
        if blk.alert_var is not None:
            blk.alert_var.value, blk.alert_var.last_change = text, now
        self.sink.alert(text, severity, rule_id, None)

    # ------------------------------------------------------------ schedules

    def _schedule_start(self, sd: dict, day: datetime) -> Optional[datetime]:
        if sd.get("start"):
            hh, mm = sd["start"].split(":")
            return day.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        loc = self.prog.location
        if sd.get("sunEvent") and loc:
            base = sun_event_local(day.date(), float(loc["latitude"]), float(loc["longitude"]), sd["sunEvent"], self.prog.tz)
            if base is None:
                return None
            return base + timedelta(minutes=int(sd.get("offsetMinutes") or 0))
        return None

    def _run_schedules(self, part: Block, now: float, wall: datetime) -> None:
        st = self.st
        today = wall.date().isoformat()
        clock_ok = self.clock.clock_ok()
        for sid, sd in part.schedules.items():
            key = f"{part.layout}/{sid}"
            run = st.runs.get(key)
            if run is not None:
                if run.state == "running" and run.end_mono is not None and now >= run.end_mono:
                    self._end_run(part, run, sd, now, wall)
                continue
            if not sd.get("enabled", True) or not clock_ok:
                continue
            if P.DAYS[wall.weekday()] not in (sd.get("days") or []):
                continue
            start = self._schedule_start(sd, wall)
            if start is None:
                continue
            window = timedelta(minutes=int(sd.get("startWindowMinutes") or 60))
            ss = st.sched.setdefault(key, {})
            spec = _start_spec(sd)
            # The runs are kept by (layout, schedule id) across the layout's parts: what ran today is that id at
            # its start. A today-only part that keeps the id at a new start is a new run; the same start, or a run
            # carried into the part (which records the part's start), is not run twice.
            ran_today = ss.get("lastRunDate") == today
            if ran_today and ss.get("lastRunStart", spec) == spec:
                continue
            if start <= wall < start + window:
                ss["lastRunDate"], ss["lastRunStart"] = today, spec
                run = Run(part.layout, sid, part.key, wall, float(sd["durationMinutes"]) * 60.0, window.total_seconds(), now)
                run.on_keys = P.start_set(part, sd)
                st.runs[key] = run
                self._persist_due = True
                self._emit("schedule_started", {"scheduleId": sid, "program": self._prog_ref(run)})
                self._queue("schedule_started", {"scheduleId": sid})
                self._do_actions(part, sd.get("actions") or [], now, wall, sid)
            elif wall >= start + window and not ran_today and ss.get("missedDate") != today:
                ss["missedDate"] = today
                self._persist_due = True
                self._emit("run_missed", {"scheduleId": sid, "reason": "start window elapsed", "program": self._prog_ref_of(part)})
                self._queue("run_missed", {"scheduleId": sid})

    def _end_run(self, part: Block, run: Run, sd: dict, now: float, wall: datetime) -> None:
        st = self.st
        st.runs.pop(run.key, None)
        self._persist_due = True
        self._emit("schedule_ended", {"scheduleId": run.sid, "program": self._prog_ref(run)})
        self._queue("schedule_ended", {"scheduleId": run.sid})
        self._do_actions(part, sd.get("endActions") or [], now, wall, run.sid)
        delivered = max(0.0, (wall - run.start_wall).total_seconds() - run.lost)
        if run.short_reason:
            if run.over_while_held:
                self._emit("run_cut_short", {"scheduleId": run.sid, "program": self._prog_ref(run)})
            self._emit("watering_not_finished", {"watering": dict(self._watering(run), reason=run.short_reason,
                                                                  plannedSeconds=round(run.duration, 1),
                                                                  deliveredSeconds=round(min(delivered, run.duration), 1))}, NODE)
        elif run.cut_short_at_end:
            self._emit("run_cut_short", {"scheduleId": run.sid, "program": self._prog_ref(run)})

    def _orphan_ends(self, now: float, wall: datetime) -> None:
        """The end of a running schedule a swap took out: its old end actions, each on what it named then."""
        st, p = self.st, self.prog
        if st.paused or st.power_wait is not None:
            return
        for o in [o for o in st.orphans if o["end"] is not None and now >= o["end"]]:
            st.orphans.remove(o)
            if not o.get("quiet"):
                self._emit("schedule_ended", {"scheduleId": o["sid"]})
            for st_a, raw in zip(o["actions"], o["raw"]):
                if st_a is None:
                    continue
                kind = st_a[0]
                try:
                    if kind == "set":
                        target, how = st_a[1], st_a[2]
                        val = raw.get("value") if isinstance(how, tuple) else None
                        if target[0] == "out":
                            out = p.outputs.get((target[1], target[2]))
                            if out is not None and val is not None:
                                self._set_level(out, _coerce(out.type, val), None, now)
                        elif target[0] == "local":
                            blk = p.block(target[1])
                            v = blk.vars.get(target[2]) if blk else None
                            if v is not None and v.kind == "local" and v.expr_ast is None and val is not None:
                                self._set_var(v, _coerce(v.type, val), now)
                    elif kind == "log":
                        self.sink.log(str(raw.get("text", "")), None)
                except Exception as e:  # noqa: BLE001
                    logger.error("orphaned end of %s/%s: %s", o["layout"], o["sid"], e)
            self._persist_due = True

    # ------------------------------------------------------------- the flush

    def _flush(self, now: float, wall: datetime) -> None:
        p, st = self.prog, self.st
        writes: list[tuple] = []
        for port, is_motor in st.line_releases:
            writes.append((0, 0 if is_motor else 1, -1, ("release", port)))
        st.line_releases = []
        for drv in p.valves.values():
            # A latching coil not known off (a line the old program drove as a level, an off-write that failed, a
            # pulse cut short): written off with the switch-offs, before any pulse — in FAULT too.
            for k, port in drv.coils():
                if drv.coil_state.get(k, False) is not False:
                    writes.append((0, 1, -1, ("coiloff", drv, k, port)))
        for key in p.output_order:
            out = p.outputs[key]
            on = is_on(out.level)
            cls = (0 if out.is_motor else 1) if not on else (1 if out.is_motor else 0)
            if out.kind == "valve":
                drv = out.valve
                if drv.latching:
                    if drv.start_close:
                        # Agent 0.4.4 (README §7.9): a clean start's close pulse — whatever position it recorded, with the
                        # switch-offs, before any open pulse; what a block set for it this pass waits for the next pass. In
                        # FAULT by now (its supply not sensed at the boot pass): no pulse.
                        if drv.fault:
                            drv.start_close = False
                        else:
                            writes.append((0, 1, out.index, ("startclose", out)))
                        continue
                    if drv.fault or drv.position == ("open" if on else "closed"):
                        continue
                    writes.append((1 if on else 0, cls, out.index, ("pulse", out, on)))
                else:
                    coil = drv.coil_level(on)
                    if drv.coil_written is not None and drv.coil_written == coil:
                        continue
                    writes.append((1 if on else 0, cls, out.index, ("coil", out, coil)))
                continue
            if out.written is not None and out.written == out.level:
                continue
            writes.append((1 if on else 0, cls, out.index, ("write", out, out.level)))
        for _, _, _, w in sorted(writes, key=lambda w: (w[0], w[1], w[2])):
            kind = w[0]
            try:
                if kind == "release":
                    # 0.4.1 (contracts v1.15.3): switched off and KEPT DRIVEN at off — never let go of to its pull.
                    self.io.write_output(w[1], False)
                elif kind == "write":
                    out = w[1]
                    self.io.write_output(out.port, w[2])
                    out.written = w[2]
                    if out.guard is not None:
                        out.guard.started(now) if is_on(w[2]) else out.guard.stopped(now)
                elif kind == "coil":
                    out = w[1]
                    self.io.write_output(out.valve.open_port, w[2])
                    out.valve.coil_written = w[2]
                elif kind == "coiloff":
                    try:
                        self.io.write_output(w[3], False)
                        w[1].coil_state[w[2]] = False
                    except Exception as e:  # noqa: BLE001 — retried every pass; said once
                        if w[1].coil_state.get(w[2]) is not None:
                            logger.error("valve %s: the %s coil cannot be written off: %s", w[1].asset_id, w[2], e)
                        w[1].coil_state[w[2]] = None
                elif kind == "pulse":
                    self._pulse(w[1].valve, w[2], now, wall)
                elif kind == "startclose":
                    drv = w[1].valve
                    drv.start_close = False
                    if self._pulse(drv, False, now, wall):
                        self._closed_built_in(drv, "start_up")
            except Exception as e:  # noqa: BLE001
                logger.error("write %s failed: %s", w, e)

    def _pulse(self, drv, want_open: bool, now: float, wall: datetime) -> bool:
        """One pulse of a latching valve: the pulse-in-progress mark is persisted first, the position after.
        Never both coils at once: the other coil is written off first unless it is known off, and no pulse is
        sent when it cannot be. No pulse is sent when the mark cannot be recorded either — a restart would then
        vouch for a position the node no longer has."""
        key = drv.open_key if want_open else drv.close_key
        if not self._other_coil_off(drv, key, wall, now):
            return False
        drv.pulse_mark = "open" if want_open else "close"
        if not self._persist():
            drv.pulse_mark = None
            self._fault(drv, "position_unknown", "the pulse could not be recorded in the state directory: no pulse sent", now, wall)
            return False
        ok = self._send_pulse(drv, key)
        supply = None
        if drv.supply and drv.supply[0] in self.prog.cards:
            try:
                supply = self.io.read_input(self.prog.cards[drv.supply[0]].ports[drv.supply[1]])
            except Exception:  # noqa: BLE001
                supply = None
        drv.pulse_mark = None
        if not ok or supply is None or not bool(supply):
            pid = drv.coil_port_id(key)
            which = "open" if want_open else "close"
            if not ok:
                detail = f"the {which} pulse on {pid} could not be sent" if pid else "the pulse could not be sent"
            else:
                detail = (f"the supply on {self._supply_words(drv)} was not present after the {which} pulse" if drv.supply_port_id
                          else "the supply was not present after the pulse")
            self._fault(drv, "pulse_cut_short", detail, now, wall)
            self._persist()
            return False
        drv.position = "open" if want_open else "closed"
        self._persist()
        return True

    def _other_coil_off(self, drv, key: str, wall: datetime, now: float) -> bool:
        """Before a pulse of coil `key`: the other coil is off — known off, or written off now. False (the valve in
        FAULT, no pulse) when it cannot be written."""
        for k, port in drv.coils():
            if k == key or drv.coil_state.get(k, False) is False:
                continue
            try:
                self.io.write_output(port, False)
                drv.coil_state[k] = False
            except Exception as e:  # noqa: BLE001
                logger.error("valve %s: the %s coil could not be written off (%s): no pulse sent", drv.asset_id, k, e)
                pid = drv.coil_port_id(k)
                which = "open" if k == drv.open_key else "close"
                self._fault(drv, "position_unknown", f"the {which} coil on {pid} could not be switched off: no pulse sent" if pid
                            else "the other coil could not be switched off: no pulse sent", now, wall)
                return False
        return True

    def _send_pulse(self, drv, key: str) -> bool:
        """The pulse itself. When it fails, the coil is written off once more: a coil that cannot be is left
        unknown, written off again with the next pass's switch-offs, and the valve stays in FAULT."""
        port = dict(drv.coils())[key]
        drv.coil_state[key] = None  # on while it runs
        try:
            self.io.pulse(port, drv.pulse_ms)
            drv.coil_state[key] = False
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("pulse %s of valve %s failed: %s", key, drv.asset_id, e)
        try:
            self.io.write_output(port, False)
            drv.coil_state[key] = False
        except Exception as e:  # noqa: BLE001
            logger.error("valve %s: the %s coil could not be written off after the pulse (%s) — it may still be on",
                         drv.asset_id, key, e)
        return False

    def _fault(self, drv, reason: str, detail: str, now: float, wall: datetime) -> None:
        if drv.set_fault(reason, detail, wall):
            self._announce_fault(drv.asset_id, reason, now, wall, detail)

    # ----------------------------------------------------------- announcing

    def _announce(self, before: dict, now: float, wall: datetime) -> None:
        p, st = self.prog, self.st
        if st.switches:
            after = {k: is_on(o.level) for k, o in p.outputs.items()}
            opened = sorted({k[0] for k, on in after.items() if on and not before.get(k)})
            kept = sorted({k[0] for k, on in after.items() if on and before.get(k)})
            for sw in st.switches:
                payload = {"containerKind": sw["containerKind"], "containerId": sw["containerId"], "nodeUid": self.node_uid,
                           "fromPartId": sw["fromPartId"], "toPartId": sw["toPartId"], "cause": sw["cause"],
                           "opened": opened, "kept": kept, "released": sw["released"]}
                if sw["validity"]:
                    payload["validity"] = sw["validity"]
                self._emit("instruction_switched", {"instruction": payload}, NODE)
            st.switches = []
        for key in st.release_events:
            self._emit("output_released", {"assetId": key[0], "sourceKey": key[1], "level": "off"})
        if st.release_events:
            self._persist_due = True
        st.release_events = []

    def _watering(self, run: Run) -> dict:
        ck, _, cid = run.layout.partition(":")
        part = self.prog.block(run.part_key) if self.prog else None
        w = {"containerKind": ck, "containerId": cid, "nodeUid": self.node_uid, "scheduleId": run.sid}
        if part is not None and part.part_id:
            w["partId"] = part.part_id
        return w

    def _prog_ref(self, run: Run) -> dict:
        ck, _, cid = run.layout.partition(":")
        return {"version": self.version, "block": "instruction", "containerId": cid, "slot": "instruction", "nodeUid": self.node_uid}

    def _prog_ref_of(self, part: Block) -> dict:
        return {"version": self.version, "block": "instruction", "containerId": part.container_id, "slot": "instruction",
                "nodeUid": self.node_uid}

    def _run_ref(self, run: Run) -> Optional[dict]:
        part = self.prog.block(run.part_key)
        return part.ref() if part is not None else None

    def _run_refs(self, runs: list[Run]) -> list[dict]:
        out = []
        for r in runs:
            ref = self._run_ref(r)
            if ref is not None and ref not in out:
                out.append(ref)
        return out

    # ------------------------------------------------------------ commands

    def _main_output(self, asset_id: str, source_key: Optional[str]) -> Optional[Output]:
        p = self.prog
        if source_key:
            return p.outputs.get((asset_id, source_key))
        if asset_id in p.valves:
            return p.outputs.get((asset_id, P.VALVE_OPEN))
        if asset_id in p.guards:
            return p.outputs.get((asset_id, p.guards[asset_id].output))
        outs = [o for o in p.outputs.values() if o.asset_id == asset_id]
        return outs[0] if len(outs) == 1 else p.outputs.get((asset_id, "device.power"))

    def _refuse_switch(self, out: Optional[Output], value, asset_id: str, source_key: str, now: float, paused: bool) -> Optional[str]:
        p = self.prog
        if out is None:
            card = p.cards.get(asset_id)
            if card is not None and asset_id in p.valves and source_key in p.valves[asset_id].cfg.values():
                pid = p.valves[asset_id].coil_port_id(source_key)
                if pid:
                    return f"refused: {pid} of {card.name} is driven by its valve driver — switch valve.open"
                return f"refused: {source_key} of card {asset_id} is driven by its valve driver — switch valve.open"
            if card is not None and source_key in card.ports:
                return f"refused: {source_key} of card {asset_id} is an input on this node"
            return f"refused: {source_key} of card {asset_id} is not an output of this node"
        on = is_on(value)
        if out.is_motor and on:
            if p.estop is not None and p.estop.latched:
                return "refused: the E-stop is latched at the panel"
            if out.guard is not None and out.guard.locked_out(now):
                until = out.guard.lockout_until_wall
                return (f"refused: {out.card.name} is locked out after a guard trip ({GUARD_WORDS.get(out.guard.reason, out.guard.reason)})"
                        + (f" until {until.strftime('%H:%M:%S')}" if until else ""))
        if out.kind == "valve" and out.valve is not None and out.valve.fault:
            return f"refused: {out.card.name} is in fault ({out.valve.fault}) — reset it first"
        if not paused and out.key in p.all_written():
            return "automation_running"
        return None

    def apply_command(self, command: dict) -> dict:
        """Check a command now (the immediate answer); it is applied at the start of the next pass."""
        t = command.get("type")
        with self._lock:
            p, st = self.prog, self.st
            if p is None:
                return {"accepted": False, "reason": "no program loaded"}
            if self._stopped:
                # Contracts v1.15.2 item 16: the runtime has stopped — the agent shutting down, its MQTT still passing
                # commands on for a few seconds — and no pass will carry a command out. Accepted, a reset would get no
                # answer at all (the document `sent` until the cloud's timeout) and a setPower would be acked completed
                # and never applied; refused, each is answered failed now, in words.
                return {"accepted": False, "reason": RESET_STOPPING if t == "resetValve" else STOPPING}
            now = self.clock.monotonic()
            if t in ("setPower", "setPortValue"):
                aid = command.get("assetId")
                sk = command.get("sourceKey") or None
                out = self._main_output(aid, sk) if aid else None
                refusal = self._refuse_switch(out, command.get("value"), aid, sk or "device.power", now, st.paused)
                if refusal:
                    return {"accepted": False, "reason": refusal}
            elif t == "resetValve":
                refusal = self._refuse_reset(command.get("assetId"))
                if refusal:
                    return {"accepted": False, "reason": refusal}
            elif t == "pauseAutomation":
                if st.paused:
                    return {"accepted": False, "reason": "refused: automation is already paused"}
                then = command.get("then") or None
                if then:
                    out = self._main_output(then.get("assetId"), None)
                    refusal = self._refuse_switch(out, then.get("power") == "on", then.get("assetId"), "device.power", now, True)
                    if refusal:
                        return {"accepted": False, "reason": refusal}
            elif t == "resumeAutomation":
                if not st.paused:
                    return {"accepted": False, "reason": "refused: automation is running"}
            elif t in ("runPlot", "stopPlot"):
                return {"accepted": False, "reason": "refused: plot runs ended with the v1 program — this node runs bundle v3"}
            elif t == "setVariable":
                return {"accepted": False, "reason": "refused: a v3 program has no card variables to set"}
            else:
                return {"accepted": False, "reason": f"unknown command {t}"}
            self._pending_commands.append(dict(command))
            self._wake.set()
            return {"accepted": True}

    def _refuse_reset(self, aid) -> Optional[str]:
        """Why a reset of `aid` is refused now (when it arrives, and again at the pass that would carry it out), or
        None. A latching valve that is not in FAULT is not refused: its reset is answered completed with no pulse at
        the pass — a reset is idempotent (contracts v1.15.2 item 15). A latched E-stop is no refusal (README §7.10)."""
        p = self.prog
        drv = p.valves.get(aid) if aid else None
        if drv is None or not drv.latching:
            return f"refused: {aid} is not a latching valve of this node"
        if not drv.fault:
            return None
        v = self._input(drv.supply)
        if v is None or not bool(v):
            return f"refused: {self._supply_phrase(drv)} is not present"
        return None

    def _supply_port(self, drv) -> Optional[PortRef]:
        card = self.prog.cards.get(drv.supply[0]) if drv.supply else None
        return card.ports.get(drv.supply[1]) if card is not None else None

    def _supply_words(self, drv) -> str:
        """A latching valve's supply sense named by its PORT. Agent 0.4.4 (contracts v1.26.0 — the founder's answer 7,
        SF-5): its node port id with the sense card's name — ``DI3 (UPS supply)`` — when the bundle names the port; else,
        as before, its chip line (``line 13 of pinctrl-bcm2835``; a bus input by its bus address: io.describe_port).
        Never a card id: a commandAck has no structured field for the card (contracts v1.15.2 item 15)."""
        if drv.supply_port_id:
            card = self.prog.cards.get(drv.supply[0]) if drv.supply else None
            return f"{drv.supply_port_id} ({card.name})" if card is not None and card.name else drv.supply_port_id
        port = self._supply_port(drv)
        return describe_port(port.transport) if port is not None else "its sense input"

    def _supply_phrase(self, drv) -> str:
        """``Valve 2's supply on DI3 (UPS supply)`` (a bundle that names the port: the valve's name too); else, as
        before, ``the valve's supply on line 13 of pinctrl-bcm2835``."""
        if drv.supply_port_id:
            return f"{drv.name}'s supply on {self._supply_words(drv)}"
        return f"the valve's supply on {self._supply_words(drv)}"

    def _read_supply(self, drv):
        """The supply sense read now (after a pulse), as the pulse path reads it; None when it cannot be read."""
        port = self._supply_port(drv)
        if port is None:
            return None
        try:
            return self.io.read_input(port)
        except Exception:  # noqa: BLE001
            return None

    def _apply_commands(self, now: float, wall: datetime) -> None:
        cmds, self._pending_commands = self._pending_commands, []
        # One reset per valve per pass (contracts v1.15.2 item 16): a second reset of the same valve in the same pass
        # — two people pressing Reset, the app and Agal Assist — takes the first one's answer and sends no pulse of its
        # own. Checked again, it would read the supply as sampled at the start of the pass, not as read after the
        # first one's close pulse, and could pulse again with the supply just read absent (or retry a pulse that
        # could not be sent).
        resets: dict[str, tuple[str, Optional[str]]] = {}
        for c in cmds:
            t = c.get("type")
            try:
                if t in ("setPower", "setPortValue"):
                    self._cmd_switch(c, now)
                elif t == "resetValve":
                    aid = c.get("assetId")
                    if aid in resets:
                        logger.info("resetValve %s: a second reset of the valve in this pass — answered as the first "
                                    "(%s), no pulse of its own", aid, resets[aid][0])
                    else:
                        resets[aid] = self._cmd_reset(c, now, wall)
                    self._answer(c, *resets[aid])
                elif t == "pauseAutomation":
                    self._cmd_pause(c, now, wall)
                elif t == "resumeAutomation":
                    self._resume(c.get("actor"), "person", now, wall)
                if t in ("setPower", "setPortValue"):
                    self.sink.event("command_applied", {"type": t, "commandId": c.get("commandId")})
            except _Refused as e:
                # Accepted when it arrived, refused at the pass: the state changed in between (a trip, the E-stop).
                if t in ("setPower", "setPortValue"):
                    self.sink.event("command_failed", {"type": t, "commandId": c.get("commandId"), "error": str(e)})
                logger.warning("command %s refused at the pass: %s", t, e)
            except Exception as e:  # noqa: BLE001
                logger.error("command %s failed: %s", t, e, exc_info=True)
                if t == "resetValve":
                    resets[c.get("assetId")] = ("failed", f"the reset could not be carried out: {e}")
                    self._answer(c, *resets[c.get("assetId")])

    def _cmd_switch(self, c: dict, now: float) -> None:
        st = self.st
        aid, sk = c.get("assetId"), c.get("sourceKey") or None
        out = self._main_output(aid, sk)
        refusal = self._refuse_switch(out, c.get("value"), aid, sk or "device.power", now, st.paused)
        if refusal:
            raise _Refused(refusal)
        self._manual_set(out, c.get("value"), c.get("minutes"), now)

    def _manual_set(self, out: Output, value, minutes, now: float) -> None:
        st = self.st
        val = _coerce(out.type, value) if out.type == "number" else is_on(value)
        self._set_level(out, val, "manual", now)
        if st.paused:
            if is_on(val) and out.asset_id not in st.manual_on:
                st.manual_on.append(out.asset_id)
            elif not is_on(val) and out.asset_id in st.manual_on:
                st.manual_on.remove(out.asset_id)
            st.automation["manualOn"] = list(st.manual_on)
        if minutes and is_on(val):
            st.manual_timers[out.key] = now + float(minutes) * 60.0
        else:
            st.manual_timers.pop(out.key, None)
        self._persist_due = True

    def _cmd_reset(self, c: dict, now: float, wall: datetime) -> tuple[str, Optional[str]]:
        """The reset, at the pass (README §7.9), and its answer (contracts v1.15.2 item 15; functions.yaml resetValve
        1.11.3): ("completed", None) once the close pulse has run, the valve is recorded closed and its FAULT is
        cleared — or, for a latching valve no longer in FAULT, with no pulse; ("failed", the node's words) when the
        reset is refused at this pass or cannot be carried out. The ack goes out with the pass's reports."""
        aid = c.get("assetId")
        refusal = self._refuse_reset(aid)
        if refusal:
            # Accepted when it arrived, refused now: the state changed in between (the supply dropped, a reload).
            logger.warning("resetValve %s refused at the pass: %s", aid, refusal)
            return "failed", refusal
        p = self.prog
        drv = p.valves[aid]
        if not drv.fault:
            # Idempotent: an earlier reset cleared it (the cloud's mirror had not caught up, or two people pressed
            # Reset). No pulse — a close pulse on a valve out of FAULT could close it under a running block.
            logger.info("resetValve %s: %s is not in fault (already reset) — answered completed, no pulse sent", aid, drv.name)
            return "completed", None
        out = p.outputs[(aid, P.VALVE_OPEN)]
        resumed = [b.ref() for b in p.blocks() if aid in b.uses_valves and b.key in self.st.held
                   and all(r == ("valve_fault", aid) for r in self.st.held[b.key])]
        # Never both coils at once: the open coil is off before the close pulse (it stays in FAULT if it cannot be).
        if not self._other_coil_off(drv, drv.close_key, wall, now):
            drv.fault_detail = "the open coil could not be switched off: no reset pulse sent"
            self._persist()
            logger.warning("resetValve %s at the pass: the open coil could not be switched off — no pulse sent", aid)
            coil = f"the open coil of {drv.name} on {drv.open_port_id}" if drv.open_port_id else "the open coil"
            return "failed", f"{coil} could not be switched off: no close pulse sent — {drv.name} stays in fault"
        drv.pulse_mark = "close"
        if not self._persist():
            drv.pulse_mark = None
            drv.fault_detail = "the reset could not be recorded in the state directory: no pulse sent"
            logger.warning("resetValve %s at the pass: the pulse cannot be recorded — no pulse sent, the valve stays in FAULT", aid)
            return "failed", (f"the reset could not be recorded in the state directory: no close pulse sent — "
                              f"{drv.name} stays in fault")
        sent = self._send_pulse(drv, drv.close_key)
        # As after any pulse (_pulse): the supply must still be there, or the pulse cannot be vouched for.
        supplied = sent and bool(self._read_supply(drv))
        if not supplied:
            drv.pulse_mark = None
            drv.fault = "pulse_cut_short"
            drv.fault_detail = ("the reset's close pulse could not be sent" if not sent
                                else "the supply was not present after the reset's close pulse")
            self._persist()
            logger.error("resetValve %s at the pass: %s — the valve stays in FAULT", aid, drv.fault_detail)
            pulse = f"the close pulse on {drv.close_port_id}" if drv.close_port_id else "the close pulse"
            return "failed", (f"{pulse} could not be sent — {drv.name} stays in fault" if not sent else
                              f"{self._supply_phrase(drv)} was not present after the close pulse — "
                              f"{drv.name} stays in fault")
        drv.pulse_mark = None
        drv.cleared()
        self._set_level(out, False, None, now)
        self._persist()
        by = _author(c.get("actor"), wall)
        valve = {"assetId": aid, "nodeUid": self.node_uid, "resumedBlocks": resumed}
        if by:
            valve["by"] = by
        self._emit("valve_reset", {"valve": valve, "assetId": aid}, NODE)
        logger.warning("valve %s reset: one close pulse sent, recorded closed", aid)
        return "completed", None

    def _cmd_pause(self, c: dict, now: float, wall: datetime) -> None:
        st, p = self.st, self.prog
        st.paused = True
        st.paused_mono = now
        until = _parse(c.get("until"))
        st.pause_until, st.pause_rederive = self._pause_end(until, now, wall), False
        st.manual_on = []
        st.automation = {"state": "paused", "pausedAt": wall.isoformat(), "manualOn": []}
        by = _author(c.get("actor"), wall)
        if by:
            st.automation["pausedBy"] = by
        if until:
            st.automation["resumeAt"] = until.isoformat()
        for blk in p.blocks():
            if not blk.interlock:
                for r in blk.rules:
                    r.seq_active, r.seq_step, r.seq_next_at = False, 0, None  # abandoned, without abortActions
        if c.get("stopAll"):
            for out in p.outputs.values():
                self._set_level(out, off_value(out.type), "manual", now)
        then = c.get("then") or None
        if then:
            out = self._main_output(then.get("assetId"), None)
            if out is not None:
                self._manual_set(out, then.get("power") == "on", then.get("minutes"), now)
        payload: dict = {"nodeUid": self.node_uid, "cause": "person", "stopAll": bool(c.get("stopAll"))}
        if by:
            payload["by"] = by
        if until:
            payload["until"] = until.isoformat()
        if then:
            payload["then"] = {k: then[k] for k in ("assetId", "power", "minutes") if k in then}
        self._emit("automation_paused", {"automation": payload})
        self._events_now.append(("automation_paused", {}))  # the pause pass delivers it to every rule
        self._persist_due = True

    def _resume(self, actor, cause: str, now: float, wall: datetime) -> None:
        """README §4a.7 applied to the active parts: waiting end actions run, running schedules re-apply their
        start actions, what was switched on by hand and no schedule has on is switched off, due schedules
        start, ordinary rules restart as after a program load."""
        st = self.st
        if not st.paused:
            return
        st.paused = False
        paused_s = int(max(0.0, now - (st.paused_mono or now)))
        for run in st.runs.values():
            if run.state == "running" and run.end_mono is not None and now < run.end_mono:
                run.pending = "__start__"
        by = _author(actor, wall)
        st.resuming = {"by": by, "cause": cause, "pausedSeconds": paused_s, "manualOn": list(st.manual_on)}
        st.automation = {"state": "running", "resumedAt": wall.isoformat()}
        if by:
            st.automation["resumedBy"] = by
        st.pause_until = None
        self._events_now.append(("automation_resumed", {}))  # the first pass delivers it
        self._persist_due = True

    def _resume_manual_off(self, now: float, wall: datetime) -> None:
        st, p = self.st, self.prog
        res = st.resuming or {}
        scheduled = set()
        for run in st.runs.values():
            scheduled |= {k[0] for k in run.on_keys}
        off = []
        for aid in res.get("manualOn") or []:
            if aid in scheduled:
                continue
            for out in p.outputs.values():
                if out.asset_id == aid and is_on(out.level):
                    self._set_level(out, off_value(out.type), None, now)
                    if aid not in off:
                        off.append(aid)
        st.manual_on = []
        payload = {"nodeUid": self.node_uid, "cause": res.get("cause") or "person", "pausedSeconds": res.get("pausedSeconds", 0),
                   "switchedOff": off}
        if res.get("by"):
            payload["by"] = res["by"]
        self._emit("automation_resumed", {"automation": payload})

    def _automation_timers(self, now: float, wall: datetime) -> None:
        st, p = self.st, self.prog
        for key, end in list(st.manual_timers.items()):
            if now >= end:
                st.manual_timers.pop(key)
                out = p.outputs.get(key)
                if out is not None:
                    self._manual_set(out, False, None, now)
        if st.paused and st.pause_rederive and self.clock.clock_ok():
            # A timed pause restored on a clock that could not be trusted: what it has left is known now.
            st.pause_rederive = False
            st.pause_until = self._pause_end(_parse(st.automation.get("resumeAt")), now, wall)
        if st.paused and st.pause_until is not None and now >= st.pause_until:
            self._resume(None, "timer", now, wall)

    # --------------------------------------------------------------- links

    def owns_port(self, asset_id: Optional[str], source_key: str) -> bool:
        with self._lock:
            p = self.prog
            if p is None or not asset_id:
                return False
            card = p.cards.get(asset_id)
            return (asset_id, source_key) in p.outputs or (card is not None and source_key in card.ports)

    def port_links(self) -> list[PortRef]:
        """Every port of every card the bundle carries, and each valve's logical valve.open (an output)."""
        with self._lock:
            p = self.prog
            if p is None:
                return []
            out = [ref for aid in p.card_order for ref in p.cards[aid].ports.values()]
            out += [PortRef(aid, P.VALVE_OPEN, {"kind": "logical", "of": "valve", "direction": "out"}) for aid in p.valves]
            return out

    def line_in_use(self, label, line) -> bool:
        with self._lock:
            p = self.prog
            if p is None or label is None or line is None:
                return False
            for c in p.cards.values():
                for ref in c.ports.values():
                    t = ref.transport
                    if t.get("chip") == label and t.get("line") is not None and int(t["line"]) == int(line):
                        return True
            return False

    # ---------------------------------------------------------------- status

    def _on_now(self) -> list[tuple[Output, str]]:
        """Under the lock: each output the program has ON now, with how it is on in words — what an update waits for."""
        p = self.prog
        out: list[tuple[Output, str]] = []
        for key in p.output_order:
            o = p.outputs[key]
            if o.kind == "valve" and o.valve is not None:
                drv = o.valve
                if drv.latching:
                    if drv.position == "open":
                        out.append((o, "valve open"))
                    elif drv.pulse_mark or not drv.coils_known_off():
                        out.append((o, "a valve coil not known off"))
                else:
                    coil_open = drv.coil_written is not None and drv.coil_written != drv.coil_level(False)
                    if is_on(o.level) or coil_open:
                        out.append((o, "valve open"))
                continue
            if is_on(o.level) or (o.written is not None and is_on(o.written)):
                out.append((o, o.source_key))
        return out

    def outputs_on(self) -> list[str]:
        """What the program has ON now, named for a person — an OTA update waits while this is not empty
        (agent 0.3.4's busy-wait, ota_updater): a restart switches every output off and closes every valve (agent
        0.4.4: a single-pulse valve gets its close pulse before the stop and again at the boot pass, README §7.9), and
        interrupts every run. So: every motor or plain output at an on level (wanted or last written), every valve
        that is open — a continuous one whose ``valve.open`` is on, a latching one recorded open — or has a pulse in
        progress or a coil not known off, and every schedule run that is running (a run interrupted by a power cut has
        its outputs off and resumes from the state directory after a restart)."""
        with self._lock:
            p, st = self.prog, self.st
            if p is None:
                return []
            out = [f"{o.card.name or o.asset_id} ({how})" for o, how in self._on_now()]
            out.extend(f"the run of schedule {r.sid} on {r.layout}" for r in st.runs.values() if r.state == "running")
            return out

    def outputs_on_items(self) -> dict:
        """The same as ``outputs_on``, for the update's report (agent 0.4.4, contracts v1.26.0, nodes/README.md §13):
        ``{"on": [{"name", "assetId"}], "runs": n}`` — each card that is on once, by its name in the bundle, in the node's
        order; ``runs`` the schedule runs in progress."""
        with self._lock:
            p, st = self.prog, self.st
            if p is None:
                return {"on": [], "runs": 0}
            on: list[dict] = []
            seen: set[str] = set()
            for o, _how in self._on_now():
                if o.asset_id in seen:
                    continue
                seen.add(o.asset_id)
                on.append({"name": o.card.name or o.asset_id, "assetId": o.asset_id})
            return {"on": on, "runs": sum(1 for r in st.runs.values() if r.state == "running")}

    # ------------------------------------------- what the cloud is told of the cards (v1.15.1)

    def card_outputs(self) -> dict[str, dict]:
        """Each card's outputs as the node drives them (functions.yaml 1.11.2 `variables.outputs`): a motor's guarded
        output and every plain output by sourceKey, at the level last written; a valve by its logical ``valve.open``
        (a continuous one from its coil, a latching one from its recorded position). An output never written, and a
        valve whose position is not known, is left out."""
        p = self.prog
        out: dict[str, dict] = {}
        if p is None:
            return out
        for key in p.output_order:
            o = p.outputs[key]
            if o.kind == "valve":
                drv = o.valve
                if drv is None:
                    continue
                if drv.latching:
                    if drv.fault or drv.position not in ("open", "closed"):
                        continue
                    val = drv.position == "open"
                else:
                    if drv.coil_written is None:
                        continue
                    val = drv.coil_written == drv.coil_level(True)
                out.setdefault(o.asset_id, {})[P.VALVE_OPEN] = bool(val)
                continue
            if o.written is None:
                continue
            out.setdefault(o.asset_id, {})[o.source_key] = o.written
        return out

    def _report_outputs(self) -> None:
        """After the pass's writes: every card whose outputs changed (every card after a load) is reported — a v3
        program has no variables, and the apps read a card's state from this (state.power)."""
        report = getattr(self.sink, "outputs", None)
        if report is None:
            return
        for aid, levels in self.card_outputs().items():
            if not self._report_all and self._outputs_reported.get(aid) == levels:
                continue
            self._outputs_reported[aid] = dict(levels)
            try:
                report(aid, dict(levels))
            except Exception as e:  # noqa: BLE001 — telling the cloud never stops a pass
                logger.debug("outputs report %s: %s", aid, e)
        self._report_all = False

    # ---------------------------------------- changing language (contracts v1.15.1)

    def driven_levels(self) -> dict[tuple[str, str], dict]:
        """(assetId, sourceKey) → {"value", "port"} of every output line this program has written — a continuous
        valve by its coil — what a program of the other language takes over at that level (programs/README.md
        §7.5a). A latching valve's coils are not: the language-1 program drives no latching valve."""
        with self._lock:
            p = self.prog
            out: dict[tuple[str, str], dict] = {}
            if p is None:
                return out
            for key in p.output_order:
                o = p.outputs[key]
                if o.kind == "valve":
                    drv = o.valve
                    if drv is not None and not drv.latching and drv.coil_written is not None:
                        out[(o.asset_id, drv.open_key)] = {"value": bool(drv.coil_written), "port": drv.open_port}
                    continue
                if o.port is not None and o.written is not None:
                    out[key] = {"value": o.written, "port": o.port}
            return out

    def output_lines(self) -> set:
        """The physical lines this program drives as outputs (its cards' output ports, valve coils included)."""
        with self._lock:
            p = self.prog
            if p is None:
                return set()
            return {physical_port(ref.transport) for aid in p.card_order for ref in p.cards[aid].ports.values()
                    if P.is_output_port(ref.transport)}

    def schedule_days(self) -> dict[str, str]:
        """schedule id → "ran" | "missed" on the node's date today (by schedule id, whatever the layout)."""
        with self._lock:
            p = self.prog
            if p is None:
                return {}
            today = self.st.date or self.clock.now(p.tz).date().isoformat()
            out: dict[str, str] = {}
            for key, ss in self.st.sched.items():
                sid = key.rpartition("/")[2]
                if ss.get("lastRunDate") == today:
                    out[sid] = "ran"
                elif ss.get("missedDate") == today:
                    out.setdefault(sid, "missed")
            return out

    def busy_reason(self) -> Optional[str]:
        """Why this program cannot hand over to the other language at the end of this pass, or None — a node changes
        language only between runs (programs/README.md §7.5a)."""
        with self._lock:
            st = self.st
            if self.prog is None:
                return None
            if st.runs:
                r = sorted(st.runs.values(), key=lambda x: x.key)[0]
                return f"the run of schedule {r.sid} on {r.layout} is {'in progress' if r.state == 'running' else 'interrupted'}"
            if st.orphans:
                return "the end of a run is still due"
            if st.releases or st.now_releases:
                return "an output is kept to the end of the run that set it"
            if st.paused:
                return "automation is paused"
            if st.power_wait is not None:
                return "it waits for the power to come back"
            if self._pending_commands:
                return "a command waits for the next pass"
            return None

    def plain_output_on_line(self, label, line) -> Optional[tuple[str, str]]:
        """(assetId, sourceKey) of the plain output on this chip line — a port test pulses it through the program,
        as on language 1. A motor's line (its guard supervises it) and a valve's coils are never handed out."""
        if label is None or line is None:
            return None
        with self._lock:
            p = self.prog
            if p is None:
                return None
            for key in p.output_order:
                o = p.outputs[key]
                if o.kind != "plain" or o.port is None:
                    continue
                t = o.port.transport
                if t.get("chip") == label and t.get("line") is not None and int(t["line"]) == int(line):
                    return key
            return None

    def take_over_language(self, bundle: dict, carried: dict[tuple[str, str], dict], days: dict[str, str]) -> None:
        """Load a language-3 bundle in place of the language-1 program that ran (the migration; programs/README.md
        §7.5a): cold — the built-ins from the node's persisted v3 state, the start recorded as not clean — but NOT a
        boot, with no run carried (the switch happens between runs), and every output the two programs share — the
        same card and port on the same line driven the same way; a continuous valve's ``valve.open`` from its v1
        coil — at the level the old program left: nothing is written for it at the first pass. A schedule id that ran
        (or was missed) today on the old program has done so here."""
        self.compile(bundle)
        with self._lock:
            p, st = self.prog, self.st
            now, wall = self.clock.monotonic(), self.clock.now(p.tz)
            # Not a start (README §7.9): the switch of language sends no single-pulse valve a close pulse.
            for drv in p.valves.values():
                drv.start_close = False
            st.runs.clear()
            st.orphans, st.releases, st.now_releases = [], [], []
            st.power_wait, st.resume_at, st.resuming = None, None, None
            kept = 0
            by_line = {physical_port(c["port"].transport): c for c in carried.values()}
            for key in p.output_order:
                o = p.outputs[key]
                if o.kind == "valve":
                    drv = o.valve
                    if drv is not None and drv.latching:
                        # Never both coils at once: a coil on a line the old program left driven is not known off — it is
                        # written off with the first pass's switch-offs, before any pulse (as in a swap, §7.5).
                        for k, port in drv.coils():
                            c = by_line.get(physical_port(port.transport))
                            if c is not None:
                                drv.coil_state[k] = False if c["value"] is False or c["value"] == 0 else True
                        continue
                    if drv is None:
                        continue
                    c = carried.get((o.asset_id, drv.open_key))
                    if c is None or not same_output_line(c["port"].transport, drv.open_port.transport):
                        continue
                    coil = bool(c["value"])
                    drv.coil_written = coil
                    o.level, o.changed_at = coil == drv.coil_level(True), now
                    kept += 1
                    continue
                if o.port is None:
                    continue
                c = carried.get(key)
                if c is None or not same_output_line(c["port"].transport, o.port.transport):
                    continue
                v = _coerce(o.type, c["value"])
                o.level, o.written, o.changed_at = v, v, now
                if o.guard is not None and is_on(v):
                    o.guard.started(now)  # a motor carried on: its inrush window starts at the switch (§7.11)
                kept += 1
            today = st.date or wall.date().isoformat()
            for part in p.parts:
                for sid, sd in part.schedules.items():
                    d = days.get(sid)
                    if d == "ran":
                        st.sched[f"{part.layout}/{sid}"] = {"lastRunDate": today, "lastRunStart": _start_spec(sd)}
                    elif d == "missed":
                        st.sched.setdefault(f"{part.layout}/{sid}", {})["missedDate"] = today
            self._booted = True  # not a boot: no `boot` event, no restart resume
            self._persist_due = True
            self._report_all = True
        logger.info("program v%d (language 3) took over from the language-1 program: %d outputs kept at their level",
                    p.version, kept)

    def level(self, asset_id: str, key: str):
        with self._lock:
            out = self.prog.outputs.get((asset_id, key)) if self.prog else None
            if out is None:
                return None
            if out.kind == "valve" and out.valve is not None and out.valve.latching:
                return out.valve.position == "open"
            return out.level

    def status_extra(self) -> dict:
        """The heartbeat's v3 status (functions.yaml 1.11.0 telemetryIngress)."""
        with self._lock:
            p, st = self.prog, self.st
            if p is None:
                return {}
            now = self.clock.monotonic()
            today = st.date or self.clock.now(p.tz).date().isoformat()
            safety: dict = {}
            if p.estop is not None:
                safety["estop"] = {"latched": p.estop.latched}
                if p.estop.pressed_at and p.estop.latched:
                    safety["estop"]["pressedAt"] = p.estop.pressed_at.isoformat()
            if p.power is not None:
                safety["power"] = p.power.status()
            valves = []
            for aid, drv in p.valves.items():
                v = {"assetId": aid, "state": drv.state(p.outputs[(aid, P.VALVE_OPEN)].level)}
                if drv.fault:
                    v["faultReason"] = drv.fault
                valves.append(v)
            guards = []
            for aid, g in p.guards.items():
                gs: dict = {"assetId": aid, "tripped": g.tripped}
                if g.tripped and g.reason:
                    gs["reason"] = g.reason
                if g.locked_out(now) and g.lockout_until_wall:
                    gs["lockoutUntil"] = g.lockout_until_wall.isoformat()
                if g.baseline is not None:
                    gs["baselineA"] = round(g.baseline, 3)
                guards.append(gs)
            held = []
            for key, reasons in st.held.items():
                for r in sorted(reasons, key=lambda x: (x[0], x[1] or "")):
                    h = {"key": key, "reason": r[0]}
                    if r[1]:
                        h["assetId"] = r[1]
                    held.append(h)
            instruction = []
            for lk in p.layout_order:
                part = p.active_part(lk, today)
                if part is not None:
                    instruction.append({"containerKind": part.container_kind, "containerId": part.container_id,
                                        "partId": part.part_id, "validity": part.validity, "version": part.version})
            auto = dict(st.automation)
            if st.paused:
                auto["manualOn"] = list(st.manual_on)
            return {"safety": safety, "valves": valves, "guards": guards, "held": held, "automation": auto,
                    "instruction": instruction}

    def snapshot(self) -> dict:
        with self._lock:
            p = self.prog
            if p is None:
                return {"version": 0}
            return {"version": p.version, "language": 3,
                    "outputs": {f"{k[0]} {k[1]}": o.level for k, o in p.outputs.items()},
                    "runs": {k: {"state": r.state, "plannedEnd": _iso(r.planned_end)} for k, r in self.st.runs.items()},
                    **self.status_extra()}

    # ----------------------------------------------------------- persistence

    def _state_path(self) -> Optional[str]:
        return os.path.join(self.state_dir, STATE_FILE) if self.state_dir else None

    def _read_state(self) -> Optional[dict]:
        """The persisted state; None when there is none (a first start); ``{UNREADABLE: True}`` when it cannot
        be read — the node then starts as after an unclean stop, with the E-stop taken as latched."""
        p = self._state_path()
        if p:
            _StateWriter.flush_path(p)  # every record handed over before this read is on disk first
        if not p or not os.path.exists(p):
            return None
        try:
            with open(p) as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
            raise ValueError("not an object")
        except Exception as e:  # noqa: BLE001
            logger.error("%s unreadable (%s): the node starts as after an unclean stop, the E-stop latched", p, e)
            return {UNREADABLE: True}

    def _persist(self, clean: bool = False, prog: Optional[Program] = None, st: Optional[_State] = None,
                 wait: bool = True) -> bool:
        """Record the state (atomically: a temporary file, then a rename), through the state file's writer (README
        §4.8): the record is built here, under the lock; the write, its fsync and the rename happen on the writer's
        thread. ``wait=True`` (the default): returns once it is on disk — True when it is; the callers that must not
        act on what they cannot record (a latching pulse, a valve reset, the start) check it. ``wait=False`` (the end
        of a pass on the runtime's own thread, a switch's clean stop): hands it over and returns True; a failure is
        logged by the writer, and the next record that must be on disk says so to its caller."""
        self._persist_due = False
        path = self._state_path()
        p, st = prog or self.prog, st or self.st
        if p is None:
            return False
        if not path:
            return True  # no state directory: nothing is vouched for after a restart (every start is a first start)
        self._last_persist = self.clock.monotonic()
        doc = {"language": 3, "version": p.version, "savedAt": self.clock.now(p.tz).isoformat(), "cleanStop": bool(clean),
               "date": st.date, "dateTrusted": bool(st.date_trusted),
               "valves": {aid: d.to_state() for aid, d in p.valves.items()},
               "guards": {aid: g.to_state() for aid, g in p.guards.items()},
               "estop": p.estop.to_state() if p.estop else None,
               "schedules": st.sched,
               "runs": {k: r.to_state() for k, r in st.runs.items()},
               "automation": dict(st.automation, manualOn=list(st.manual_on)) if st.paused else {"state": "running"}}
        try:
            text = json.dumps(doc, default=str)
        except Exception as e:  # noqa: BLE001
            self._persisted(e)
            return False
        return _StateWriter.for_path(path).submit(text, self._persisted, wait)

    def _persisted(self, error: Optional[Exception]) -> None:
        """What became of a record, said once per change (on the writer's thread — never takes the runtime lock: a
        caller holding it may be waiting for this very record)."""
        if error is not None:
            if not self._persist_failing:
                logger.error("v3 state persist failed: %s — no latching valve is pulsed until it can be written", error)
            self._persist_failing = True
            return
        if self._persist_failing:
            logger.warning("v3 state written again")
        self._persist_failing = False


class _StateWriter:
    """The one writer of a v3 state file (README §4.8, the second agent 0.4.0 review: a pass never waits on the disk).

    One per file path, for the whole process — a runtime made again on the same state directory (a restart in the
    tests, the host's two loads) shares it, so a record never lands after a newer one. A record handed over replaces
    one not yet picked up (the newer holds its facts too); one thread writes them in turn — temporary file, fsync,
    rename — and ends when there is nothing left. ``submit(wait=True)`` returns once a record at least as new is on
    disk (True when one was written); :meth:`flush` waits for every record handed over before it (a read of the file).
    """

    _by_path: dict[str, "_StateWriter"] = {}
    _registry = threading.Lock()

    @classmethod
    def for_path(cls, path: str) -> "_StateWriter":
        with cls._registry:
            w = cls._by_path.get(path)
            if w is None:
                w = cls._by_path[path] = cls(path)
            return w

    @classmethod
    def flush_path(cls, path: str) -> None:
        with cls._registry:
            w = cls._by_path.get(path)
        if w is not None:
            w.flush()

    def __init__(self, path: str):
        self.path = path
        self._cond = threading.Condition()
        self._next: Optional[tuple[int, str, Callable[[Optional[Exception]], None]]] = None
        self._made = 0     # the newest record handed over
        self._landed = 0   # the newest record written or failed
        self._good = 0     # the newest record written
        self._busy = False

    def submit(self, text: str, on_done: Callable[[Optional[Exception]], None], wait: bool) -> bool:
        with self._cond:
            self._made += 1
            seq = self._made
            self._next = (seq, text, on_done)
            start, self._busy = not self._busy, True
        if start:
            try:
                threading.Thread(target=self._run, name=STATE_WRITER_THREAD, daemon=True).start()
            except RuntimeError:  # no thread can be had: written here, as before
                self._run()
        if not wait:
            return True
        with self._cond:
            self._cond.wait_for(lambda: self._landed >= seq)
            return self._good >= seq

    def flush(self) -> None:
        with self._cond:
            seq = self._made
            self._cond.wait_for(lambda: self._landed >= seq)

    def _run(self) -> None:
        while True:
            with self._cond:
                item, self._next = self._next, None
                if item is None:
                    self._busy = False
                    return
            seq, text, on_done = item
            error: Optional[Exception] = None
            try:
                _write_atomically(self.path, text)
            except Exception as e:  # noqa: BLE001
                error = e
            try:
                on_done(error)
            except Exception:  # noqa: BLE001 — never stops the writer
                pass
            with self._cond:
                self._landed = seq
                if error is None:
                    self._good = seq
                self._cond.notify_all()


def _write_atomically(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


class _Refused(Exception):
    pass
