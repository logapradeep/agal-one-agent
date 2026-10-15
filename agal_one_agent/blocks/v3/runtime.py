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

Deterministic under a SimClock and a SimulatedIO — the golden vectors of programs/vectors run through it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from .. import expr as E
from ..clock import Clock, SystemClock
from ..io import IOAdapter, PortRef, SimulatedIO, physical_port
from ..runtime import CompileError, EventSink, _coerce, _signature
from ..sun import sun_event_local
from . import program as P
from .guard import GUARD_WORDS
from .program import Block, Output, Program, is_on, off_value
from .valve import FAULT_WORDS

logger = logging.getLogger(__name__)

STATE_FILE = "blocks_v3_state.json"
KEEPALIVE_PERSIST_SECONDS = 60.0  # while a run is in progress: what a restart needs to know how long it had left
NODE = "node"                      # event source of the built-ins (functions.yaml 1.11.0: "source node")


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

    def copy(self) -> "_State":
        s = _State.__new__(_State)
        s.__dict__.update(self.__dict__)
        s.runs = {k: r.copy() for k, r in self.runs.items()}
        s.sched = {k: dict(v) for k, v in self.sched.items()}
        for f in ("orphans", "releases", "now_releases", "line_releases", "release_events", "switches",
                  "pending_events", "manual_on"):
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
        self._wake = threading.Event()
        self._cold = True
        self._booted = False
        self._applied_pending = False
        self._pending_commands: list[dict] = []
        self._persist_due = False
        self._last_persist: Optional[float] = None
        self._clean_stop_persisted = False
        self._pass_events: list[tuple[str, dict, str]] = []
        self._noted_runs: set[str] = set()
        self._events_now: list[tuple[str, dict]] = []
        self._alert_last: dict[tuple[str, str], float] = {}
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
            try:
                st = self._take_over(prog) if warm else self._cold_load(prog)
            except CompileError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error("program v%s could not take over from the running program: %s", prog.version, e, exc_info=True)
                raise CompileError(f"could not take over from the running program: {e}") from e
            self.prog, self.st = prog, st
            self._cold = False
            self._applied_pending = True
            self._persist_due = True
        logger.info("program v%d (language 3) compiled%s: %d cards, %d rule slots, %d instruction parts",
                    prog.version, " (took over from the running program)" if warm else "", len(prog.cards),
                    len(prog.rules), len(prog.parts))

    # --------------------------------------------------------- the cold load

    def _cold_load(self, prog: Program) -> _State:
        """The first program, or the first after a stop: built from the persisted state, as after a restart."""
        now, wall = self.clock.monotonic(), self.clock.now(prog.tz)
        st = _State()
        st.date = wall.date().isoformat()
        saved = self._read_state()
        first_start = saved is None
        saved = saved or {}
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
            elif ent.get("config") and ent["config"] != drv.signature:
                reason = "position_unknown"
            elif ent.get("pulse"):
                reason = "pulse_cut_short"
            elif ent.get("position") == "fault":
                drv.fault, drv.fault_detail = ent.get("faultReason") or "position_unknown", "in fault before the restart"
                continue
            elif not clean:
                reason = "unclean_boot"
            elif ent.get("position") in ("open", "closed"):
                # Vouched for. Every output starts off after a restart, so one known open gets one close pulse at
                # the boot pass; an interrupted run re-opens it when it resumes.
                drv.position = ent["position"]
            else:
                reason = "position_unknown"
            if reason:
                drv.set_fault(reason, FAULT_WORDS[reason], wall)
                st.pending_events.append(("__valve_fault__", {"assetId": aid, "reason": reason}))
        for aid, g in prog.guards.items():
            ent = (saved.get("guards") or {}).get(aid)
            if isinstance(ent, dict):
                g.load_state(ent, now, wall)
        es = saved.get("estop") or {}
        if prog.estop is not None and es.get("latched"):
            prog.estop.latched = True
            prog.estop.pressed_at = _parse(es.get("pressedAt"))
            prog.estop.latched_mono = now
        auto = saved.get("automation") or {}
        if auto.get("state") == "paused":
            st.paused = True
            st.automation = {k: v for k, v in auto.items() if k in ("state", "pausedAt", "pausedBy", "resumeAt", "manualOn")}
            st.manual_on = list(auto.get("manualOn") or [])
            st.paused_mono = now
            until = _parse(auto.get("resumeAt"))
            st.pause_until = now + max(0.0, (until - wall).total_seconds()) if until else None
        for k, v in (saved.get("schedules") or {}).items():
            if isinstance(v, dict):
                st.sched[k] = {"lastRunDate": v.get("lastRunDate"), "missedDate": v.get("missedDate")}
        # Runs in progress when the node stopped were interrupted by the restart: they resume 30 s after
        # the boot pass, inside their window (README §7.8). Their outputs start off, as every output does.
        saved_at = _parse(saved.get("savedAt"))
        for k, r in (saved.get("runs") or {}).items():
            run = self._run_from_state(r, prog, now, saved_at)
            if run is None:
                continue
            part = prog.active_part(run.layout, st.date)
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
            if r.get("state") == "interrupted" and r.get("remaining") is not None:
                run.remaining = float(r["remaining"])
            else:
                ref = saved_at or start
                run.remaining = max(0.0, (planned_end - ref).total_seconds())
            run.end_mono = None
            run.interrupted_at = now
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
            if od.signature == drv.signature or (not drv.latching and not od.latching):
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
            logger.error("the E-stop left the bundle while latched: the latch is kept off the program — nothing restarts by it")
        if new.power is not None and old.power is not None:
            new.power.carry_from(old.power)
        # Outputs: carried by their stable key.
        in_use = new.physical_in_use()
        for key, out in new.outputs.items():
            o = old.outputs.get(key)
            if o is None:
                continue
            out.level, out.changed_at, out.last_writer = o.level, o.changed_at, o.last_writer
            if out.port is not None and o.port is not None:
                if out.port.transport == o.port.transport:
                    out.written = o.written
                elif physical_port(o.port.transport) not in in_use:
                    st.line_releases.append((o.port, o.is_motor))
        for key, o in old.outputs.items():
            if key in new.outputs:
                continue
            ports = [o.port] if o.port is not None else (o.valve.coil_ports() if o.valve is not None else [])
            for port in ports:
                if physical_port(port.transport) not in in_use:
                    st.line_releases.append((port, o.is_motor))
            st.release_events.append(key)
        for aid, od in old.valves.items():  # a valve's coils moved to other lines
            nd = new.valves.get(aid)
            if nd is None:
                continue
            for op, np_ in ((od.open_port, nd.open_port), (od.close_port, nd.close_port)):
                if op is not None and (np_ is None or np_.transport != op.transport) and physical_port(op.transport) not in in_use:
                    st.line_releases.append((op, False))
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
                continue
            st.runs.pop(key)
            if run.state != "running" or src is None or s_old is None:
                logger.info("run %s left the program while interrupted: it is over", key)
                continue
            st.orphans.append({"end": run.end_mono, "layout": layout, "sid": run.sid, "partKey": run.part_key,
                               "actions": [P.stable_action(src, a) for a in s_old.get("endActions") or []],
                               "raw": [dict(a) for a in s_old.get("endActions") or []], "planned_end": _iso(run.planned_end)})

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
        self._thread = threading.Thread(target=self._loop, name="blocks-v3-runtime", daemon=True)
        self._thread.start()
        if not isinstance(self.io, SimulatedIO):
            self._sampler = threading.Thread(target=self._sample_loop, name="blocks-v3-sampler", daemon=True)
            self._sampler.start()

    def stop(self, safe_state: bool = True) -> None:
        self._running = False
        self._wake.set()
        for t in (self._thread, self._sampler):
            if t is not None and t is not threading.current_thread():
                t.join(timeout=3.0)
        with self._lock:
            if safe_state:
                self.apply_safe_state()
            self._persist(clean=True)
            self._cold = True
            self._booted = False

    def apply_safe_state(self) -> None:
        """A clean stop: every motor and every other output whose safeState is off is switched off, each
        continuous valve closes. A latching valve stays where it is (its position is persisted)."""
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
                    continue
                if out.card.safe_state == "last_state":
                    continue
                self.io.write_output(out.port, off_value(out.type))
                out.written = off_value(out.type)
            except Exception as e:  # noqa: BLE001
                logger.error("safe-state write %s.%s failed: %s", out.asset_id, out.source_key, e)

    def _loop(self) -> None:
        while self._running:
            try:
                self.step()
            except Exception as e:  # noqa: BLE001
                logger.error("runtime pass failed: %s", e, exc_info=True)
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
                self._announce_fault(pl["assetId"], pl["reason"], now, wall)
            st.pending_events = [e for e in st.pending_events if e[0] != "__valve_fault__"]
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
            today = wall.date().isoformat()
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
            self._announce(before, now, wall)
            for t, pl, src in self._pass_events:
                self.sink.event(t, pl, source=src)
            self._pass_events = []
            st.resuming = None
            if self._persist_due or (st.runs and (self._last_persist is None or now - self._last_persist >= KEEPALIVE_PERSIST_SECONDS)):
                self._persist()

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
                d = st.debounce.setdefault(key, {"raw": raw, "since": now, "last": None})
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

    def _midnight(self, now: float, wall: datetime) -> None:
        st, p = self.st, self.prog
        today = wall.date().isoformat()
        if st.date is None:
            st.date = today
            return
        if today == st.date:
            return
        yesterday, st.date = st.date, today
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
            st.switches.append(rec)
            logger.info("midnight: layout %s switches from %s to %s", lk, op.part_id if op else None, np_.part_id if np_ else None)

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
            self._resume_runs(now, wall, st.resume_cause)
            # "…and the rules start the pump as usual" (README §7.8): the ordinary rule slots restart as after a
            # program load, so a rule whose condition held through the cut (a latching valve stays open on its
            # UPS) starts its motor again. Interlocks keep their state: they ran through the cut.
            for blk in p.blocks():
                if not blk.interlock:
                    self._reset_rules(blk)
            st.power_wait, st.resume_at, st.resume_cause = None, None, "power"

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
        for out in p.outputs.values():
            if out.is_motor or (out.kind == "valve" and out.valve is not None and not out.valve.latching):
                self._set_level(out, off_value(out.type), None, now)

    def _resume_runs(self, now: float, wall: datetime, cause: str) -> None:
        """30 s after the power is back (or after the boot pass): each interrupted run resumes for the time it
        had left, inside its window — planned start to planned end + its startWindowMinutes (README §7.8)."""
        st = self.st
        for key in list(st.runs):
            run = st.runs[key]
            if run.state != "interrupted":
                continue
            lost = now - (run.interrupted_at if run.interrupted_at is not None else now)
            run.lost += max(0.0, lost)
            left = run.remaining or 0.0
            room = (run.window_end - wall).total_seconds()
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
                detail = f"the supply on {drv.supply[1]} of card {drv.supply[0]} " + ("cannot be read" if v is None else "dropped")
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
            if ss.get("lastRunDate") == today:
                continue
            if start <= wall < start + window:
                ss["lastRunDate"] = today
                run = Run(part.layout, sid, part.key, wall, float(sd["durationMinutes"]) * 60.0, window.total_seconds(), now)
                run.on_keys = P.start_set(part, sd)
                st.runs[key] = run
                self._persist_due = True
                self._emit("schedule_started", {"scheduleId": sid, "program": self._prog_ref(run)})
                self._queue("schedule_started", {"scheduleId": sid})
                self._do_actions(part, sd.get("actions") or [], now, wall, sid)
            elif wall >= start + window and ss.get("missedDate") != today:
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
        for key in p.output_order:
            out = p.outputs[key]
            on = is_on(out.level)
            cls = (0 if out.is_motor else 1) if not on else (1 if out.is_motor else 0)
            if out.kind == "valve":
                drv = out.valve
                if drv.latching:
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
                    self.io.write_output(w[1], False)
                    self.io.release_output(w[1])
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
                elif kind == "pulse":
                    self._pulse(w[1].valve, w[2], now, wall)
            except Exception as e:  # noqa: BLE001
                logger.error("write %s failed: %s", w, e)

    def _pulse(self, drv, want_open: bool, now: float, wall: datetime) -> bool:
        """One pulse of a latching valve: the pulse-in-progress mark is persisted first, the position after.
        Never both coils at once: only one is ever driven, and only for pulseMs."""
        port = drv.open_port if want_open else drv.close_port
        drv.pulse_mark = "open" if want_open else "close"
        self._persist()
        ok = True
        try:
            self.io.pulse(port, drv.pulse_ms)
        except Exception as e:  # noqa: BLE001
            logger.error("pulse %s of valve %s failed: %s", drv.pulse_mark, drv.asset_id, e)
            ok = False
        supply = None
        if drv.supply and drv.supply[0] in self.prog.cards:
            try:
                supply = self.io.read_input(self.prog.cards[drv.supply[0]].ports[drv.supply[1]])
            except Exception:  # noqa: BLE001
                supply = None
        drv.pulse_mark = None
        if not ok or supply is None or not bool(supply):
            detail = "the pulse could not be sent" if not ok else "the supply was not present after the pulse"
            if drv.set_fault("pulse_cut_short", detail, wall):
                self._announce_fault(drv.asset_id, "pulse_cut_short", now, wall, detail)
            self._persist()
            return False
        drv.position = "open" if want_open else "closed"
        self._persist()
        return True

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
        p = self.prog
        drv = p.valves.get(aid) if aid else None
        if drv is None or not drv.latching:
            return f"refused: {aid} is not a latching valve of this node"
        if not drv.fault:
            # Only a valve in FAULT is reset (the cloud refuses not_in_fault too): a close pulse on a healthy open
            # valve would close it under a running block.
            return f"refused: {drv.name} is not in fault"
        v = self._input(drv.supply)
        if v is None or not bool(v):
            return f"refused: the valve's supply ({drv.supply[1]} of card {drv.supply[0]}) is not present"
        return None

    def _apply_commands(self, now: float, wall: datetime) -> None:
        cmds, self._pending_commands = self._pending_commands, []
        for c in cmds:
            t = c.get("type")
            try:
                if t in ("setPower", "setPortValue"):
                    self._cmd_switch(c, now)
                elif t == "resetValve":
                    self._cmd_reset(c, now, wall)
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

    def _cmd_reset(self, c: dict, now: float, wall: datetime) -> None:
        aid = c.get("assetId")
        refusal = self._refuse_reset(aid)
        if refusal:
            logger.warning("resetValve %s refused at the pass: %s", aid, refusal)
            return
        p = self.prog
        drv = p.valves[aid]
        out = p.outputs[(aid, P.VALVE_OPEN)]
        resumed = [b.ref() for b in p.blocks() if aid in b.uses_valves and b.key in self.st.held
                   and all(r == ("valve_fault", aid) for r in self.st.held[b.key])]
        port = drv.close_port
        drv.pulse_mark = "close"
        self._persist()
        try:
            self.io.pulse(port, drv.pulse_ms)
        except Exception as e:  # noqa: BLE001
            drv.pulse_mark = None
            drv.fault, drv.fault_detail = "pulse_cut_short", f"the reset's close pulse could not be sent: {e}"[:200]
            self._persist()
            raise
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

    def _cmd_pause(self, c: dict, now: float, wall: datetime) -> None:
        st, p = self.st, self.prog
        st.paused = True
        st.paused_mono = now
        until = _parse(c.get("until"))
        st.pause_until = now + max(0.0, (until - wall).total_seconds()) if until else None
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
        p = self._state_path()
        if not p or not os.path.exists(p):
            return None
        try:
            with open(p) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception as e:  # noqa: BLE001
            logger.warning("%s unreadable (%s): the node starts as after an unclean stop", p, e)
            return {}

    def _persist(self, clean: bool = False) -> None:
        self._persist_due = False
        path = self._state_path()
        p, st = self.prog, self.st
        if not path or p is None:
            return
        self._last_persist = self.clock.monotonic()
        doc = {"language": 3, "version": p.version, "savedAt": self.clock.now(p.tz).isoformat(), "cleanStop": bool(clean),
               "valves": {aid: d.to_state() for aid, d in p.valves.items()},
               "guards": {aid: g.to_state() for aid, g in p.guards.items()},
               "estop": p.estop.to_state() if p.estop else None,
               "schedules": st.sched,
               "runs": {k: r.to_state() for k, r in st.runs.items()},
               "automation": dict(st.automation, manualOn=list(st.manual_on)) if st.paused else {"state": "running"}}
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(doc, f, default=str)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            logger.warning("v3 state persist failed: %s", e)


class _Refused(Exception):
    pass
