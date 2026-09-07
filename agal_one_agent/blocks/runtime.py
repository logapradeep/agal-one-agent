"""BlockRuntime — executes a compiled bundle on the node (ADR-017).

Evaluation model (Agal/contracts/programs/README.md §4):
  * a pass = sample inputs → every AAB in asset order → the NAB → flush outputs
    → report changed variables. One pass per tick (1 s) plus an immediate pass
    whenever an input changes (the sampler thread) or a command arrives.
  * exactly one writer per output (this runtime); no second protection path.
  * rules see variable values as they are when they run; ``set`` takes effect
    for the following rules; outputs are written at the end of the pass, pumps
    before valves when switching off and after valves when switching on (R-7).
  * expressions are pure over the snapshot plus baseline(x), since(x) and the
    local clock; stateful behaviour (holds, ignore windows, limits, sequences,
    schedules) lives in this module.

The runtime is deterministic under a :class:`~.clock.SimClock` and a
:class:`~.io.SimulatedIO` — that is the simulated bench (_audit/99 P1).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from . import expr as E
from .clock import Clock, SystemClock
from .io import IOAdapter, PortRef, SimulatedIO
from .sun import sun_event_local

logger = logging.getLogger(__name__)

RUNTIME_SETTINGS: dict[str, Any] = {
    "app_toggle_holds": "next_event",
    "manual_plot_auto_start": False,
    "wall_switch_wins_locally": True,
    "max_run_seconds": 7200.0,
    "valve_energise_seconds": 3.0,
    "resume_within_seconds": 900.0,
}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class CompileError(ValueError):
    pass


class EventSink:
    """What the runtime tells the outside world. Override what you need."""

    def variables(self, asset_id: str, values: dict[str, Any]) -> None: ...
    def alert(self, text: str, severity: str, rule_id: Optional[str], asset_id: Optional[str] = None) -> None: ...
    def event(self, type_: str, payload: dict) -> None: ...
    def log(self, text: str, asset_id: Optional[str] = None) -> None: ...
    def reading(self, asset_id: str, source_key: str, value: Any, kind_uri: Optional[str], unit: Optional[str]) -> None: ...
    def program_ack(self, version: int, status: str, reason: Optional[str] = None) -> None: ...


class RecordingSink(EventSink):
    """Collects everything (tests + simulator)."""

    def __init__(self):
        self.variables_log: list[tuple[str, dict]] = []
        self.alerts: list[dict] = []
        self.events: list[dict] = []
        self.logs: list[dict] = []
        self.readings: list[dict] = []
        self.acks: list[dict] = []

    def variables(self, asset_id, values):
        self.variables_log.append((asset_id, dict(values)))

    def alert(self, text, severity, rule_id, asset_id=None):
        self.alerts.append({"text": text, "severity": severity, "ruleId": rule_id, "assetId": asset_id})

    def event(self, type_, payload):
        self.events.append({"type": type_, **payload})

    def log(self, text, asset_id=None):
        self.logs.append({"text": text, "assetId": asset_id})

    def reading(self, asset_id, source_key, value, kind_uri, unit):
        self.readings.append({"assetId": asset_id, "sourceKey": source_key, "value": value, "kindUri": kind_uri, "unit": unit})

    def program_ack(self, version, status, reason=None):
        self.acks.append({"version": version, "status": status, "reason": reason})


# ============================================================ compiled model

def _scalar_type(v) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    return "string"


def _coerce(t: str, v):
    if v is None:
        return None
    if t == "bool":
        return bool(v)
    if t in ("number", "enum"):
        return float(v) if not isinstance(v, bool) else (1.0 if v else 0.0)
    return str(v)


class _Var:
    __slots__ = ("name", "kind", "type", "d", "value", "last_change", "expr_ast", "settable", "exposed", "port")

    def __init__(self, d: dict):
        self.name = d["name"]
        self.kind = d["kind"]
        self.type = d["type"]
        self.d = d
        self.value = _coerce(self.type, d.get("default")) if d.get("default") is not None else (False if self.type == "bool" else (0.0 if self.type in ("number", "enum") else ""))
        self.last_change: Optional[float] = None
        self.expr_ast = None
        self.settable = bool(d.get("settable")) and not d.get("expression")
        self.exposed = bool(d.get("exposed"))
        self.port: Optional[PortRef] = None


class _Rule:
    def __init__(self, d: dict):
        self.d = d
        self.id = d["id"]
        self.kind = d["kind"]
        self.enabled = d.get("enabled", True)
        self.protection = bool(d.get("protection"))
        self.cond_ast = None
        self.filter_ast = None
        self.abort_ast = None
        # state
        self.true_since: Optional[float] = None
        self.fired = False
        self.last_fire: Optional[float] = None
        self.ignore_until: Optional[float] = None
        self.prev_ignore_after: Optional[bool] = None
        self.prev_var_value: Any = None
        self.limit_since: Optional[float] = None
        self.limit_fired = False
        # sequence
        self.seq_active = False
        self.seq_step = 0
        self.seq_next_at: Optional[float] = None


class _Schedule:
    def __init__(self, d: dict):
        self.d = d
        self.id = d["id"]
        self.last_run_date: Optional[str] = None
        self.missed_date: Optional[str] = None
        self.active_end_mono: Optional[float] = None
        self.active_end_wall: Optional[str] = None  # ISO, for restart resume
        self.active_started_at: Optional[str] = None


class _Block:
    """One AAB (asset_id set) or the NAB (asset_id None)."""

    def __init__(self, asset_id: Optional[str], d: dict, asset_type: str = ""):
        self.asset_id = asset_id
        self.asset_type = asset_type
        self.d = d
        self.vars: dict[str, _Var] = {}
        self.order: list[str] = []
        self.rules: list[_Rule] = []
        self.schedules: list[_Schedule] = []
        self.settings: dict[str, Any] = dict(RUNTIME_SETTINGS)
        self.settings.update(d.get("settings") or {})
        self.baselines: dict[str, Optional[float]] = {}
        self._learn: dict[str, dict] = {}   # var → {started, samples}
        self.pending_writes: dict[str, Any] = {}
        self.plots: dict[str, dict] = {}
        self.pump_variable: Optional[str] = None
        self.alert_var: Optional[_Var] = None
        self.dirty: set[str] = set()

    def scope(self) -> dict:
        return {
            "variables": {n: v.type for n, v in self.vars.items()},
            "settings": {k: _scalar_type(v) for k, v in self.settings.items()},
        }


# ================================================================== runtime

class BlockRuntime:
    def __init__(self, io: IOAdapter, sink: EventSink, clock: Optional[Clock] = None,
                 state_dir: Optional[str] = None, tick_seconds: float = 1.0):
        self.io = io
        self.sink = sink
        self.clock = clock or SystemClock()
        self.state_dir = state_dir
        self.tick_seconds = tick_seconds
        self.bundle: Optional[dict] = None
        self.version: int = 0
        self.tz = "Asia/Kolkata"
        self.location: Optional[dict] = None
        self.aabs: dict[str, _Block] = {}
        self.aab_order: list[str] = []
        self.nab: Optional[_Block] = None
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._sampler: Optional[threading.Thread] = None
        self._running = False
        self._wake = threading.Event()
        self._pending_commands: list[dict] = []
        self._pending_events: list[tuple[str, dict]] = []
        self._booted = False
        self._applied_pending = False
        self._last_written: dict[tuple[str, str], Any] = {}
        self._debounce: dict[tuple[str, str], dict] = {}
        self._alert_last: dict[str, float] = {}
        self._datalog_next: dict[tuple[str, str], float] = {}
        self._var_report_next: dict[str, float] = {}
        self._active_runs: dict[str, dict] = {}  # plotId → {end, started}
        self._clock_was_ok = True
        self._last_pass_mono: Optional[float] = None
        self._persist_due = False
        self.io.set_change_listener(self._on_input_change)

    # ------------------------------------------------------------ compile

    def compile(self, bundle: dict) -> None:
        """Validate a compiled bundle and load it (atomic: on error the previous program stays)."""
        try:
            aabs, order, nab, tz, loc = self._build(bundle)
        except CompileError:
            raise
        except Exception as e:  # noqa: BLE001
            raise CompileError(f"malformed bundle: {e}") from e
        with self._lock:
            old_state = self._snapshot_state()
            self.bundle = bundle
            self.version = int(bundle.get("version", 0))
            self.aabs, self.aab_order, self.nab = aabs, order, nab
            self.tz = tz
            self.location = loc
            self._restore_state(old_state)  # carry baselines / schedule state across versions
            self._load_persisted()
            self._applied_pending = True
            self._active_runs = {p: r for p, r in self._active_runs.items() if p in (nab.plots if nab else {})}
        logger.info("program v%d compiled: %d assets, NAB %d rules / %d schedules", self.version, len(order), len(nab.rules) if nab else 0, len(nab.schedules) if nab else 0)

    def _build(self, bundle: dict):
        version = bundle.get("version")
        if not isinstance(version, int) or version < 1:
            raise CompileError("bundle.version missing")
        tz = str(bundle.get("timezone") or "Asia/Kolkata")
        loc = bundle.get("location")
        aabs: dict[str, _Block] = {}
        order: list[str] = []
        for a in bundle.get("assets") or []:
            aid = a.get("assetId")
            if not aid or aid in aabs:
                raise CompileError(f"asset id missing or duplicated: {aid}")
            blk = _Block(aid, a.get("aab") or {}, str(a.get("assetType") or ""))
            ports = a.get("ports") or {}
            for vd in blk.d.get("variables") or []:
                self._check_var(vd, blk, "aab")
                v = _Var(vd)
                if v.kind == "node":
                    node = vd.get("node") or {}
                    sk = node.get("sourceKey")
                    if sk not in ports:
                        raise CompileError(f"{aid}.{v.name}: port '{sk}' is not resolved for this node")
                    v.port = PortRef(aid, sk, dict(ports[sk], direction=node.get("direction"), activeLow=node.get("activeLow", ports[sk].get("activeLow", False))))
                    if not self.io.has_port(v.port):
                        raise CompileError(f"{aid}.{v.name}: this node has no port for '{sk}'")
                blk.vars[v.name] = v
                blk.order.append(v.name)
            self._finish_block(blk, is_nab=False)
            aabs[aid] = blk
            order.append(aid)
        nabd = bundle.get("nab")
        if not isinstance(nabd, dict):
            raise CompileError("bundle.nab missing")
        nab = _Block(None, nabd)
        for vd in nabd.get("variables") or []:
            self._check_var(vd, nab, "nab")
            v = _Var(vd)
            if v.kind == "asset":
                ref = vd.get("asset") or {}
                target = aabs.get(ref.get("assetId"))
                if target is None:
                    raise CompileError(f"NAB.{v.name}: asset '{ref.get('assetId')}' is not on this node")
                tv = target.vars.get(ref.get("variable"))
                if tv is None or not tv.exposed:
                    raise CompileError(f"NAB.{v.name}: '{ref.get('assetId')}.{ref.get('variable')}' is not an exposed variable")
                if ref.get("access") == "write" and not tv.settable:
                    raise CompileError(f"NAB.{v.name}: '{ref.get('variable')}' is not settable")
                if tv.type != v.type:
                    raise CompileError(f"NAB.{v.name}: type {v.type} does not match {tv.type}")
                v.settable = ref.get("access") == "write"
            if v.kind == "alert":
                nab.alert_var = v
            nab.vars[v.name] = v
            nab.order.append(v.name)
        for p in nabd.get("plots") or []:
            for vn in p.get("valveVariables") or []:
                if vn not in nab.vars or not nab.vars[vn].settable:
                    raise CompileError(f"plot '{p.get('plotId')}': valve variable '{vn}' is not a writable NAB variable")
            fv = p.get("flowVariable")
            if fv and fv not in nab.vars:
                raise CompileError(f"plot '{p.get('plotId')}': flow variable '{fv}' unknown")
            nab.plots[p["plotId"]] = p
        pv = nabd.get("pumpVariable")
        if pv:
            if pv not in nab.vars or not nab.vars[pv].settable:
                raise CompileError(f"pumpVariable '{pv}' is not a writable NAB variable")
            nab.pump_variable = pv
        for sd in nabd.get("schedules") or []:
            if not sd.get("start") and not sd.get("sunEvent"):
                raise CompileError(f"schedule '{sd.get('id')}': needs start or sunEvent")
            if sd.get("plotId") and sd["plotId"] not in nab.plots:
                raise CompileError(f"schedule '{sd.get('id')}': unknown plot '{sd['plotId']}'")
            nab.schedules.append(_Schedule(sd))
        self._finish_block(nab, is_nab=True)
        return aabs, order, nab, tz, loc

    @staticmethod
    def _check_var(vd: dict, blk: _Block, which: str) -> None:
        name = vd.get("name")
        if not E.is_identifier(name or ""):
            raise CompileError(f"bad variable name {name!r}")
        if name in blk.vars:
            raise CompileError(f"duplicate variable '{name}'")
        kinds = ("node", "datalog", "ui", "local") if which == "aab" else ("asset", "local", "alert")
        if vd.get("kind") not in kinds:
            raise CompileError(f"variable '{name}': kind {vd.get('kind')} not allowed in {which}")
        if vd.get("type") not in ("bool", "number", "string", "enum"):
            raise CompileError(f"variable '{name}': bad type")
        if vd.get("kind") == "alert" and vd.get("type") != "string":
            raise CompileError(f"alert variable '{name}' must be a string")

    def _finish_block(self, blk: _Block, is_nab: bool) -> None:
        scope = blk.scope()

        def parse_bool(src: str, ctx: str, want_bool: bool = True):
            try:
                ast = E.parse(src)
            except E.ExprError as e:
                raise CompileError(f"{ctx}: {e}") from e
            t, errs = E.validate(ast, scope)
            if errs:
                raise CompileError(f"{ctx}: {errs[0]}")
            if want_bool and t != "bool":
                raise CompileError(f"{ctx}: must be a bool expression (is {t})")
            return ast, t

        for v in blk.vars.values():
            d = v.d
            if d.get("expression"):
                if v.kind != "local":
                    raise CompileError(f"{v.name}: only local variables may be computed")
                v.expr_ast, t = parse_bool(d["expression"], f"variable '{v.name}'", want_bool=False)
                if t != v.type and not (v.type == "enum" and t == "number"):
                    raise CompileError(f"variable '{v.name}': expression is {t}, declared {v.type}")
            if d.get("datalog") and d["datalog"].get("source") not in blk.vars:
                raise CompileError(f"{v.name}: datalog source unknown")
            if d.get("ui") and d["ui"].get("source") and d["ui"]["source"] not in blk.vars:
                raise CompileError(f"{v.name}: ui source unknown")
            if d.get("baseline"):
                lw = d["baseline"].get("learnWhen")
                if lw not in blk.vars or blk.vars[lw].type != "bool":
                    raise CompileError(f"{v.name}: baseline.learnWhen must be a bool variable")
                if v.type != "number":
                    raise CompileError(f"{v.name}: only numeric variables learn a baseline")
                blk.baselines[v.name] = None
        seq_ids = {r.get("id") for r in blk.d.get("rules") or [] if r.get("kind") == "sequence"}
        seen = set()
        for rd in blk.d.get("rules") or []:
            if rd.get("id") in seen:
                raise CompileError(f"duplicate rule id '{rd.get('id')}'")
            seen.add(rd.get("id"))
            r = _Rule(rd)
            ctx = f"rule '{r.id}'"
            if r.kind == "when":
                w = rd.get("when") or {}
                r.cond_ast, _ = parse_bool(w.get("condition", ""), ctx)
                ia = w.get("ignoreAfter")
                if ia and (ia not in blk.vars or blk.vars[ia].type != "bool"):
                    raise CompileError(f"{ctx}: ignoreAfter must be a bool variable")
            elif r.kind == "limit":
                lv = (rd.get("limit") or {}).get("variable")
                if lv not in blk.vars or blk.vars[lv].type != "bool":
                    raise CompileError(f"{ctx}: limit.variable must be a bool variable")
            elif r.kind == "on":
                o = rd.get("on") or {}
                if o.get("event") == "variable_changed" and o.get("variable") not in blk.vars:
                    raise CompileError(f"{ctx}: unknown variable '{o.get('variable')}'")
                if o.get("filter"):
                    r.filter_ast, _ = parse_bool(o["filter"], f"{ctx} filter")
            elif r.kind == "sequence":
                s = rd.get("sequence") or {}
                if not s.get("steps"):
                    raise CompileError(f"{ctx}: sequence needs steps")
                if s.get("abortWhen"):
                    r.abort_ast, _ = parse_bool(s["abortWhen"], f"{ctx} abortWhen")
                for st in s["steps"]:
                    self._check_actions(st.get("actions") or [], blk, is_nab, seq_ids, ctx, scope)
                self._check_actions(s.get("abortActions") or [], blk, is_nab, seq_ids, ctx, scope)
            else:
                raise CompileError(f"{ctx}: unknown kind {r.kind}")
            self._check_actions(rd.get("actions") or [], blk, is_nab, seq_ids, ctx, scope)
            self._check_actions(rd.get("elseActions") or [], blk, is_nab, seq_ids, ctx, scope)
            blk.rules.append(r)
        for s in blk.schedules:
            self._check_actions(s.d.get("actions") or [], blk, is_nab, seq_ids, f"schedule '{s.id}'", scope)
            self._check_actions(s.d.get("endActions") or [], blk, is_nab, seq_ids, f"schedule '{s.id}'", scope)

    def _check_actions(self, actions: list, blk: _Block, is_nab: bool, seq_ids: set, ctx: str, scope: dict) -> None:
        for a in actions:
            t = a.get("type")
            if t == "set":
                vn = a.get("variable")
                v = blk.vars.get(vn)
                if v is None:
                    raise CompileError(f"{ctx}: unknown variable '{vn}'")
                writable = v.settable or (v.kind == "local" and not v.expr_ast and not v.d.get("expression")) or v.kind == "alert"
                if not writable:
                    raise CompileError(f"{ctx}: '{vn}' is not settable")
                if a.get("expression"):
                    try:
                        ast = E.parse(a["expression"])
                    except E.ExprError as e:
                        raise CompileError(f"{ctx}: {e}") from e
                    ty, errs = E.validate(ast, scope)
                    if errs:
                        raise CompileError(f"{ctx}: {errs[0]}")
                    a["_ast"] = ast
                elif "value" not in a:
                    raise CompileError(f"{ctx}: set needs value or expression")
            elif t == "alert":
                if not is_nab or blk.alert_var is None:
                    raise CompileError(f"{ctx}: alert actions need a NAB alert variable")
                if not a.get("text"):
                    raise CompileError(f"{ctx}: alert needs text")
            elif t in ("run_plot", "stop_plot"):
                if not is_nab:
                    raise CompileError(f"{ctx}: {t} lives in the NAB")
                if a.get("plotId") and a["plotId"] not in blk.plots:
                    raise CompileError(f"{ctx}: unknown plot '{a.get('plotId')}'")
            elif t == "start_sequence":
                if a.get("sequenceId") not in seq_ids:
                    raise CompileError(f"{ctx}: unknown sequence '{a.get('sequenceId')}'")
            elif t == "log":
                if not a.get("text"):
                    raise CompileError(f"{ctx}: log needs text")
            else:
                raise CompileError(f"{ctx}: unknown action type {t}")

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._loop, name="blocks-runtime", daemon=True)
        self._thread.start()
        if self._needs_sampler():
            self._sampler = threading.Thread(target=self._sample_loop, name="blocks-sampler", daemon=True)
            self._sampler.start()

    def stop(self, safe_state: bool = True) -> None:
        self._running = False
        self._wake.set()
        for t in (self._thread, self._sampler):
            if t is not None and t is not threading.current_thread():
                t.join(timeout=3.0)
        if safe_state:
            self.apply_safe_state()
        self._persist()

    def apply_safe_state(self) -> None:
        """On a clean stop: pumps with safeState=off are de-energised (R-14)."""
        with self._lock:
            for aid in self.aab_order:
                a = self._asset_meta(aid)
                if a.get("safeState", "off") != "off":
                    continue
                blk = self.aabs[aid]
                for v in blk.vars.values():
                    if v.port is not None and v.port.transport.get("direction") == "out" and v.type == "bool":
                        try:
                            self.io.write_output(v.port, False)
                            self._last_written[(aid, v.port.source_key)] = False
                        except Exception as e:  # noqa: BLE001
                            logger.error("safe-state write %s failed: %s", v.port.source_key, e)

    def _asset_meta(self, aid: str) -> dict:
        for a in (self.bundle or {}).get("assets") or []:
            if a.get("assetId") == aid:
                return a
        return {}

    def _needs_sampler(self) -> bool:
        return not isinstance(self.io, SimulatedIO)

    def _loop(self) -> None:
        while self._running:
            try:
                self.step()
            except Exception as e:  # noqa: BLE001
                logger.error("runtime pass failed: %s", e, exc_info=True)
            self._wake.wait(self.tick_seconds)
            self._wake.clear()

    def _sample_loop(self) -> None:
        """Fast digital-input sampler (100 ms) — triggers a pass on a debounced change."""
        while self._running:
            changed = False
            try:
                with self._lock:
                    changed = self._sample_inputs(self.clock.monotonic(), digital_only=True)
            except Exception as e:  # noqa: BLE001
                logger.debug("sampler: %s", e)
            if changed:
                self._wake.set()
            time.sleep(0.1)

    def _on_input_change(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------- commands

    def apply_command(self, command: dict) -> dict:
        """Queue a cloud/app command; applied at the start of the next pass.
        Returns {accepted: bool, reason?} immediately (validation only)."""
        t = command.get("type")
        with self._lock:
            if self.nab is None:
                return {"accepted": False, "reason": "no program loaded"}
            if t in ("runPlot", "stopPlot"):
                if command.get("plotId") not in self.nab.plots:
                    return {"accepted": False, "reason": f"unknown plot {command.get('plotId')}"}
            elif t == "setVariable":
                blk = self.aabs.get(command.get("assetId"))
                v = blk.vars.get(command.get("variable")) if blk else None
                if v is None or not (v.settable or (v.kind == "ui" and v.d.get("ui", {}).get("source"))):
                    return {"accepted": False, "reason": "not a settable variable"}
            elif t in ("setPower", "setPortValue"):
                if self._port_var(command.get("assetId"), command.get("sourceKey") or "device.power") is None:
                    return {"accepted": False, "reason": "port not bound to a program variable"}
            else:
                return {"accepted": False, "reason": f"unknown command {t}"}
            self._pending_commands.append(command)
            self._wake.set()
            return {"accepted": True}

    def owns_port(self, asset_id: Optional[str], source_key: str) -> bool:
        with self._lock:
            return self._port_var(asset_id, source_key) is not None

    def _port_var(self, asset_id: Optional[str], source_key: str) -> Optional[tuple[_Block, _Var]]:
        candidates = [self.aabs[asset_id]] if asset_id in self.aabs else list(self.aabs.values())
        for blk in candidates:
            for v in blk.vars.values():
                if v.port is not None and v.port.source_key == source_key and v.port.transport.get("direction") == "out":
                    return blk, v
        return None

    # ----------------------------------------------------------------- pass

    def step(self) -> None:
        """One evaluation pass at the clock's current time."""
        with self._lock:
            if self.nab is None:
                return
            now = self.clock.monotonic()
            wall = self.clock.now(self.tz)
            self._sample_inputs(now, digital_only=False)
            if self._applied_pending:
                self._applied_pending = False
                self._pending_events.append(("program_applied", {"version": self.version}))
                self.sink.event("program_applied", {"version": self.version})
                self.sink.program_ack(self.version, "applied")
            ok = self.clock.clock_ok()
            if ok and not self._clock_was_ok:
                self._pending_events.append(("power_restored", {}))
            self._clock_was_ok = ok
            if not self._booted:
                # Boot is its own pass: `on boot` rules (e.g. the default NAB's "safe
                # outputs after a restart") run before any schedule, resume or command
                # touches an output, so a resumed run re-opens its valves *after* them.
                self._booted = True
                events = self._pending_events + [("boot", {})]
                self._pending_events = []
                for aid in self.aab_order:
                    self._eval_block(self.aabs[aid], now, wall, events)
                self._sync_nab_reads()
                self._eval_block(self.nab, now, wall, events)
                self._sync_nab_writes(now)
                self._flush_outputs()
                self._report_variables(now)
                self._last_pass_mono = now
                return
            self._apply_pending_commands(now)
            self._apply_pending_resumes(now)
            events = self._pending_events
            self._pending_events = []
            for aid in self.aab_order:
                self._eval_block(self.aabs[aid], now, wall, events)
            self._sync_nab_reads()
            self._run_schedules(now, wall, ok)
            self._eval_block(self.nab, now, wall, events)
            self._expire_runs(now)
            self._sync_nab_writes(now)
            self._flush_outputs()
            self._report_variables(now)
            self._datalog(now)
            self._last_pass_mono = now
            if self._persist_due:
                self._persist()

    def _apply_pending_resumes(self, now: float) -> None:
        resumes, self._pending_resumes = getattr(self, "_pending_resumes", []), []
        for plot_id, end_mono in resumes:
            if plot_id not in self.nab.plots:
                continue
            for vn in (self.nab.plots[plot_id].get("valveVariables") or []):
                self._set_var(self.nab, self.nab.vars[vn], True, now)
            self._active_runs[plot_id] = {"started": now, "end": end_mono, "source": "resume"}
            self.sink.event("plot_run_started", {"plotId": plot_id, "source": "resume"})

    def _sample_inputs(self, now: float, digital_only: bool) -> bool:
        changed = False
        for aid in self.aab_order:
            blk = self.aabs[aid]
            for v in blk.vars.values():
                if v.port is None or v.port.transport.get("direction") != "in":
                    continue
                node = v.d.get("node") or {}
                is_digital = v.type == "bool"
                if digital_only and not is_digital:
                    continue
                raw = self.io.read_input(v.port)
                if raw is None:
                    continue
                if is_digital:
                    raw = bool(raw)
                    key = (aid, v.name)
                    st = self._debounce.setdefault(key, {"raw": raw, "since": now, "last": None})
                    interval = (now - st["last"]) if st["last"] is not None else None
                    st["last"] = now
                    if st["raw"] != raw:
                        st["raw"], st["since"] = raw, now
                    debounce_s = float(node.get("debounceMs") or 0) / 1000.0
                    # Accept once the value has held for the debounce window, or when
                    # this sampler runs slower than the window (1 s ticks on the bench):
                    # then the sample interval itself is the debounce.
                    stable = (now - st["since"]) >= debounce_s
                    slow_sampler = interval is not None and interval >= debounce_s
                    if not (stable or slow_sampler):
                        continue
                    val = raw
                else:
                    val = float(raw)
                if self._set_var(blk, v, val, now):
                    changed = True
        return changed

    def _apply_pending_commands(self, now: float) -> None:
        cmds, self._pending_commands = self._pending_commands, []
        for c in cmds:
            t = c.get("type")
            try:
                if t == "runPlot":
                    self._run_plot(c["plotId"], c.get("durationMinutes"), now, source="app")
                elif t == "stopPlot":
                    self._stop_plot(c["plotId"], now, source="app")
                elif t == "setVariable":
                    blk = self.aabs[c["assetId"]]
                    self._write_ui(blk, blk.vars[c["variable"]], c.get("value"), now)
                    self._interrupt({"assetId": c["assetId"], "variable": c["variable"], "value": c.get("value"), "source": "app", "commandId": c.get("commandId")})
                elif t in ("setPower", "setPortValue"):
                    blk, v = self._port_var(c.get("assetId"), c.get("sourceKey") or "device.power")
                    val = bool(c.get("value")) if v.type == "bool" else _coerce(v.type, c.get("value"))
                    self._set_var(blk, v, val, now)
                    self._interrupt({"assetId": blk.asset_id, "variable": v.name, "value": val, "source": "app", "commandId": c.get("commandId")})
                self.sink.event("command_applied", {"type": t, "commandId": c.get("commandId")})
            except Exception as e:  # noqa: BLE001
                logger.error("command %s failed: %s", t, e)
                self.sink.event("command_failed", {"type": t, "commandId": c.get("commandId"), "error": str(e)})

    def _interrupt(self, payload: dict) -> None:
        """A person's action outside the program: queued for `on interruption` rules and
        reported to the cloud (R-23) so Agal Assist can state the consequence."""
        self._pending_events.append(("interruption", payload))
        self.sink.event("interruption", payload)

    def _write_ui(self, blk: _Block, v: _Var, value, now: float) -> None:
        src = (v.d.get("ui") or {}).get("source")
        target = blk.vars[src] if src else v
        self._set_var(blk, target, _coerce(target.type, value), now)
        if target is not v:
            self._set_var(blk, v, _coerce(v.type, value), now)

    def _set_var(self, blk: _Block, v: _Var, value, now: float) -> bool:
        if value is None:
            return False
        if v.type in ("number", "enum"):
            value = float(value)
        if v.value == value and v.last_change is not None:
            return False
        v.value = value
        v.last_change = now
        blk.dirty.add(v.name)
        return True

    # --------------------------------------------------------------- blocks

    def _env(self, blk: _Block, now: float, wall: datetime) -> dict:
        return {
            "vars": {n: v.value for n, v in blk.vars.items()},
            "settings": blk.settings,
            "baselines": blk.baselines,
            "since": {n: (now - v.last_change if v.last_change is not None else None) for n, v in blk.vars.items()},
            "clock": {"hour": wall.hour, "minute": wall.minute, "weekday": wall.isoweekday()},
        }

    def _eval_block(self, blk: _Block, now: float, wall: datetime, events: list[tuple[str, dict]]) -> None:
        prev = {n: v.value for n, v in blk.vars.items()}
        # computed locals + baselines
        for n in blk.order:
            v = blk.vars[n]
            if v.expr_ast is not None:
                val = E.evaluate(v.expr_ast, self._env(blk, now, wall))
                if val is not None:
                    self._set_var(blk, v, _coerce(v.type, val), now)
        for n in list(blk.baselines.keys()):
            self._learn_baseline(blk, blk.vars[n], now)
        # rules in order
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
                logger.error("rule %s failed: %s", r.id, e, exc_info=True)

    def _learn_baseline(self, blk: _Block, v: _Var, now: float) -> None:
        cfg = v.d.get("baseline") or {}
        lw = blk.vars.get(cfg.get("learnWhen"))
        if lw is None:
            return
        st = blk._learn.setdefault(v.name, {"prev": None, "start": None, "samples": []})
        if st["prev"] is not None and not st["prev"] and lw.value:
            st["start"] = now + float(cfg.get("afterSeconds", 2))
            st["samples"] = []
        st["prev"] = bool(lw.value)
        if st["start"] is not None and lw.value:
            if now >= st["start"]:
                st["samples"].append(float(v.value))
                if now >= st["start"] + float(cfg.get("windowSeconds", 10)):
                    avg = sum(st["samples"]) / len(st["samples"])
                    blk.baselines[v.name] = avg
                    st["start"] = None
                    self._persist_due = True
                    self.sink.event("baseline_learned", {"assetId": blk.asset_id, "variable": v.name, "baseline": avg, "samples": len(st["samples"])})
        elif not lw.value:
            st["start"] = None

    def _rule_when(self, blk: _Block, r: _Rule, now: float, wall: datetime) -> None:
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
            held = now - r.true_since
            if held >= float(w.get("holdSeconds") or 0):
                retrig = float(w.get("retriggerSeconds") or 0)
                if not r.fired or (retrig > 0 and r.last_fire is not None and now - r.last_fire >= retrig):
                    r.fired = True
                    r.last_fire = now
                    self._do_actions(blk, r.d.get("actions") or [], now, wall, r.id)
        else:
            r.true_since = None
            if r.fired:
                r.fired = False
                self._do_actions(blk, r.d.get("elseActions") or [], now, wall, r.id)

    def _rule_limit(self, blk: _Block, r: _Rule, now: float, wall: datetime) -> None:
        lim = r.d.get("limit") or {}
        v = blk.vars[lim["variable"]]
        if v.value:
            if r.limit_since is None:
                r.limit_since = v.last_change if v.last_change is not None else now
            if not r.limit_fired and now - r.limit_since >= float(lim.get("maxSeconds")):
                r.limit_fired = True
                self._do_actions(blk, r.d.get("actions") or [], now, wall, r.id)
        else:
            r.limit_since = None
            r.limit_fired = False

    def _rule_on(self, blk: _Block, r: _Rule, now: float, wall: datetime, prev: dict, events: list) -> None:
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

    def _rule_sequence(self, blk: _Block, r: _Rule, now: float, wall: datetime) -> None:
        if not r.seq_active:
            return
        s = r.d.get("sequence") or {}
        if r.abort_ast is not None and E.evaluate(r.abort_ast, self._env(blk, now, wall)) is True:
            r.seq_active = False
            self._do_actions(blk, s.get("abortActions") or [], now, wall, r.id)
            return
        steps = s.get("steps") or []
        while r.seq_active and r.seq_step < len(steps):
            st = steps[r.seq_step]
            if r.seq_next_at is None:
                r.seq_next_at = now + float(st.get("delaySeconds") or 0)
            if now < r.seq_next_at:
                return
            self._do_actions(blk, st.get("actions") or [], now, wall, r.id)
            r.seq_step += 1
            r.seq_next_at = None
        r.seq_active = False

    # -------------------------------------------------------------- actions

    def _do_actions(self, blk: _Block, actions: list, now: float, wall: datetime, rule_id: Optional[str]) -> None:
        for a in actions:
            t = a.get("type")
            if t == "set":
                v = blk.vars[a["variable"]]
                if "_ast" in a:
                    val = E.evaluate(a["_ast"], self._env(blk, now, wall))
                else:
                    val = a.get("value")
                if val is None:
                    continue
                if v.kind == "alert":
                    self._raise_alert(blk, str(val), (a.get("severity") or (v.d.get("alert") or {}).get("defaultSeverity") or "warning"), rule_id, now)
                else:
                    self._set_var(blk, v, _coerce(v.type, val), now)
            elif t == "alert":
                self._raise_alert(blk, self._fmt(blk, a.get("text", "")), a.get("severity") or "warning", rule_id, now)
            elif t == "log":
                self.sink.log(self._fmt(blk, a.get("text", "")), blk.asset_id)
            elif t == "run_plot":
                self._run_plot(a.get("plotId"), a.get("durationMinutes"), now, source=rule_id or "rule")
            elif t == "stop_plot":
                self._stop_plot(a.get("plotId"), now, source=rule_id or "rule")
            elif t == "start_sequence":
                for r in blk.rules:
                    if r.id == a.get("sequenceId"):
                        r.seq_active, r.seq_step, r.seq_next_at = True, 0, None

    def _fmt(self, blk: _Block, text: str) -> str:
        out = text
        for n, v in blk.vars.items():
            token = "{" + n + "}"
            if token in out:
                val = v.value
                if isinstance(val, float):
                    val = f"{val:.2f}".rstrip("0").rstrip(".")
                out = out.replace(token, str(val))
        return out

    def _raise_alert(self, blk: _Block, text: str, severity: str, rule_id: Optional[str], now: float) -> None:
        cooldown = float(((blk.alert_var.d.get("alert") if blk.alert_var else {}) or {}).get("cooldownSeconds", 300))
        last = self._alert_last.get(text)
        if last is not None and now - last < cooldown:
            return
        self._alert_last[text] = now
        if blk.alert_var is not None:
            blk.alert_var.value = text
            blk.alert_var.last_change = now
        self.sink.alert(text, severity, rule_id, blk.asset_id)

    # ---------------------------------------------------------------- plots

    def _run_plot(self, plot_id: Optional[str], duration_min: Optional[int], now: float, source: str) -> None:
        nab = self.nab
        plot = nab.plots.get(plot_id or "")
        if plot is None:
            raise ValueError(f"unknown plot {plot_id}")
        for vn in plot.get("valveVariables") or []:
            self._set_var(nab, nab.vars[vn], True, now)
        run = {"started": now, "end": (now + float(duration_min) * 60.0) if duration_min else None, "source": source}
        self._active_runs[plot_id] = run
        self._persist_due = True
        self.sink.event("plot_run_started", {"plotId": plot_id, "durationMinutes": duration_min, "source": source})

    def _stop_plot(self, plot_id: Optional[str], now: float, source: str) -> None:
        nab = self.nab
        plot = nab.plots.get(plot_id or "")
        if plot is None:
            raise ValueError(f"unknown plot {plot_id}")
        others_open = any(
            bool(nab.vars[vn].value)
            for pid, p in nab.plots.items() if pid != plot_id
            for vn in (p.get("valveVariables") or [])
        )
        if nab.pump_variable and not others_open:
            self._set_var(nab, nab.vars[nab.pump_variable], False, now)
        for vn in plot.get("valveVariables") or []:
            self._set_var(nab, nab.vars[vn], False, now)
        self._active_runs.pop(plot_id, None)
        self._persist_due = True
        self.sink.event("plot_run_ended", {"plotId": plot_id, "source": source})

    def _expire_runs(self, now: float) -> None:
        for pid, run in list(self._active_runs.items()):
            if run.get("end") is not None and now >= run["end"]:
                self._stop_plot(pid, now, source="timer")

    # ------------------------------------------------------------ schedules

    def _schedule_start(self, s: _Schedule, day: datetime) -> Optional[datetime]:
        d = s.d
        if d.get("start"):
            hh, mm = d["start"].split(":")
            return day.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if d.get("sunEvent") and self.location:
            base = sun_event_local(day.date(), float(self.location["latitude"]), float(self.location["longitude"]), d["sunEvent"], self.tz)
            if base is None:
                return None
            return base + timedelta(minutes=int(d.get("offsetMinutes") or 0))
        return None

    def _run_schedules(self, now: float, wall: datetime, clock_ok: bool) -> None:
        nab = self.nab
        today = wall.date().isoformat()
        for s in nab.schedules:
            d = s.d
            if s.active_end_mono is not None and now >= s.active_end_mono:
                s.active_end_mono = None
                s.active_end_wall = None
                self._schedule_end(s, now, wall)
                continue
            if not d.get("enabled", True) or not clock_ok:
                continue
            if DAYS[wall.weekday()] not in (d.get("days") or []):
                continue
            start = self._schedule_start(s, wall)
            if start is None:
                continue
            window = timedelta(minutes=int(d.get("startWindowMinutes") or 60))
            if s.last_run_date != today:
                if start <= wall < start + window:
                    s.last_run_date = today
                    s.active_end_mono = now + int(d["durationMinutes"]) * 60.0
                    s.active_end_wall = (wall + timedelta(minutes=int(d["durationMinutes"]))).isoformat()
                    s.active_started_at = wall.isoformat()
                    self._persist_due = True
                    self.sink.event("schedule_started", {"scheduleId": s.id, "plotId": d.get("plotId")})
                    self._pending_events.append(("schedule_started", {"scheduleId": s.id}))
                    self._do_actions(nab, self._with_plot(d.get("actions") or [], d.get("plotId")), now, wall, s.id)
                elif wall >= start + window and s.missed_date != today:
                    s.missed_date = today
                    self._persist_due = True
                    self.sink.event("run_missed", {"scheduleId": s.id, "plotId": d.get("plotId"), "reason": "start window elapsed"})
                    self._pending_events.append(("run_missed", {"scheduleId": s.id}))

    def _schedule_end(self, s: _Schedule, now: float, wall: datetime) -> None:
        d = s.d
        self.sink.event("schedule_ended", {"scheduleId": s.id, "plotId": d.get("plotId")})
        self._pending_events.append(("schedule_ended", {"scheduleId": s.id}))
        end_actions = d.get("endActions")
        if not end_actions and d.get("plotId"):
            end_actions = [{"type": "stop_plot", "plotId": d["plotId"]}]
        self._do_actions(self.nab, self._with_plot(end_actions or [], d.get("plotId")), now, wall, s.id)
        self._persist_due = True

    @staticmethod
    def _with_plot(actions: list, plot_id: Optional[str]) -> list:
        out = []
        for a in actions:
            if a.get("type") in ("run_plot", "stop_plot") and not a.get("plotId") and plot_id:
                a = dict(a, plotId=plot_id)
            out.append(a)
        return out

    # ---------------------------------------------------- NAB ⇄ AAB syncing

    def _sync_nab_reads(self) -> None:
        nab = self.nab
        for v in nab.vars.values():
            if v.kind != "asset" or v.name in nab.dirty:
                continue  # a write made earlier in this pass must not be clobbered
            ref = v.d["asset"]
            src = self.aabs[ref["assetId"]].vars[ref["variable"]]
            if v.value != src.value or v.last_change is None:
                v.value = src.value
                v.last_change = src.last_change if src.last_change is not None else self.clock.monotonic()

    def _sync_nab_writes(self, now: float) -> None:
        nab = self.nab
        for n in nab.dirty:
            v = nab.vars[n]
            if v.kind != "asset" or not v.settable:
                continue
            ref = v.d["asset"]
            blk = self.aabs[ref["assetId"]]
            tv = blk.vars[ref["variable"]]
            if tv.value != v.value:
                self._set_var(blk, tv, v.value, now)
                # mirror ui vars that source this variable
                for uv in blk.vars.values():
                    if uv.kind == "ui" and (uv.d.get("ui") or {}).get("source") == tv.name:
                        self._set_var(blk, uv, _coerce(uv.type, v.value), now)
        nab.dirty.clear()

    # -------------------------------------------------------------- outputs

    def _flush_outputs(self) -> None:
        writes: list[tuple[int, int, _Block, _Var]] = []
        for aid in self.aab_order:
            blk = self.aabs[aid]
            # ui vars sourcing an output var mirror it
            for uv in blk.vars.values():
                if uv.kind == "ui" and (uv.d.get("ui") or {}).get("source"):
                    src = blk.vars[uv.d["ui"]["source"]]
                    if uv.value != src.value:
                        uv.value = src.value
                        uv.last_change = src.last_change
                        blk.dirty.add(uv.name)
            for v in blk.vars.values():
                if v.port is None or v.port.transport.get("direction") != "out":
                    continue
                key = (aid, v.port.source_key)
                if self._last_written.get(key) == v.value and key in self._last_written:
                    continue
                is_motor = blk.asset_type in ("motor_controller",) or bool(self._asset_meta(aid).get("ratedCurrentA"))
                turning_on = bool(v.value)
                # off: motors first (0), valves after (1); on: valves first (0), motors after (1)
                order = (0 if is_motor else 1) if not turning_on else (1 if is_motor else 0)
                writes.append((1 if turning_on else 0, order, blk, v))
        for _, _, blk, v in sorted(writes, key=lambda w: (w[0], w[1])):
            try:
                self.io.write_output(v.port, v.value)
                self._last_written[(blk.asset_id, v.port.source_key)] = v.value
            except Exception as e:  # noqa: BLE001
                logger.error("write %s.%s failed: %s", blk.asset_id, v.port.source_key, e)

    def _report_variables(self, now: float) -> None:
        for aid in self.aab_order:
            blk = self.aabs[aid]
            if not blk.dirty:
                continue
            due = self._var_report_next.get(aid, 0.0)
            if now < due:
                continue
            values = {n: blk.vars[n].value for n in sorted(blk.dirty) if blk.vars[n].kind in ("ui", "local", "node")}
            blk.dirty.clear()
            if values:
                self.sink.variables(aid, values)
            self._var_report_next[aid] = now + 1.0

    def _datalog(self, now: float) -> None:
        for aid in self.aab_order:
            blk = self.aabs[aid]
            for v in blk.vars.values():
                if v.kind != "datalog":
                    continue
                dl = v.d.get("datalog") or {}
                key = (aid, v.name)
                if now < self._datalog_next.get(key, 0.0):
                    continue
                self._datalog_next[key] = now + float(dl.get("intervalSeconds", 60))
                src = blk.vars[dl["source"]]
                source_key = src.port.source_key if src.port is not None else f"var.{src.name}"
                val = src.value
                if isinstance(val, bool):
                    val = 1.0 if val else 0.0
                self.sink.reading(aid, source_key, val, dl.get("kindUri") or (src.d.get("node") or {}).get("kindUri"), v.d.get("unit") or src.d.get("unit"))

    # ----------------------------------------------------------- persistence

    def _snapshot_state(self) -> dict:
        return {
            "baselines": {aid: dict(b.baselines) for aid, b in self.aabs.items()},
            "schedules": {s.id: {"last_run_date": s.last_run_date, "missed_date": s.missed_date, "active_end_wall": s.active_end_wall, "active_started_at": s.active_started_at} for s in (self.nab.schedules if self.nab else [])},
            "runs": {pid: {"end_wall": (self.clock.now(self.tz) + timedelta(seconds=r["end"] - self.clock.monotonic())).isoformat() if r.get("end") else None, "source": r.get("source")} for pid, r in self._active_runs.items()},
        }

    def _restore_state(self, st: dict) -> None:
        for aid, b in st.get("baselines", {}).items():
            if aid in self.aabs:
                for n, val in b.items():
                    if n in self.aabs[aid].baselines:
                        self.aabs[aid].baselines[n] = val
        if self.nab:
            for s in self.nab.schedules:
                ss = st.get("schedules", {}).get(s.id)
                if ss:
                    s.last_run_date, s.missed_date = ss.get("last_run_date"), ss.get("missed_date")
                    s.active_end_wall, s.active_started_at = ss.get("active_end_wall"), ss.get("active_started_at")

    def _state_path(self) -> Optional[str]:
        return os.path.join(self.state_dir, "blocks_state.json") if self.state_dir else None

    def _persist(self) -> None:
        self._persist_due = False
        p = self._state_path()
        if not p:
            return
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"version": self.version, "savedAt": self.clock.now(self.tz).isoformat(), **self._snapshot_state()}, f)
            os.replace(tmp, p)
        except Exception as e:  # noqa: BLE001
            logger.warning("persist failed: %s", e)

    def _load_persisted(self) -> None:
        p = self._state_path()
        if not p or not os.path.exists(p):
            return
        try:
            with open(p) as f:
                st = json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.warning("state load failed: %s", e)
            return
        self._restore_state(st)
        # resume or cut short interrupted runs (R-21)
        now_wall = self.clock.now(self.tz)
        resume_within = float((self.nab.settings if self.nab else {}).get("resume_within_seconds", 900))
        saved_at = st.get("savedAt")
        try:
            saved_dt = datetime.fromisoformat(saved_at) if saved_at else None
        except Exception:  # noqa: BLE001
            saved_dt = None
        gap = (now_wall - saved_dt).total_seconds() if saved_dt else None
        for s in (self.nab.schedules if self.nab else []):
            if s.active_end_wall:
                try:
                    end = datetime.fromisoformat(s.active_end_wall)
                except Exception:  # noqa: BLE001
                    end = None
                if end is not None and now_wall < end and (gap is None or gap <= resume_within):
                    s.active_end_mono = self.clock.monotonic() + (end - now_wall).total_seconds()
                    self._pending_events.append(("schedule_resumed", {"scheduleId": s.id}))
                    self.sink.event("run_resumed", {"scheduleId": s.id, "plotId": s.d.get("plotId")})
                    if s.d.get("plotId"):
                        # applied after the boot pass (see step) so `on boot` rules cannot undo it
                        pending = getattr(self, "_pending_resumes", [])
                        pending.append((s.d["plotId"], s.active_end_mono))
                        self._pending_resumes = pending
                else:
                    self.sink.event("run_cut_short", {"scheduleId": s.id, "plotId": s.d.get("plotId"), "gapSeconds": gap})
                    s.active_end_wall = None
                    s.active_started_at = None
        self._persist_due = True

    # ------------------------------------------------------------ inspection

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": self.version,
                "assets": {aid: {n: v.value for n, v in b.vars.items()} for aid, b in self.aabs.items()},
                "nab": {n: v.value for n, v in self.nab.vars.items()} if self.nab else {},
                "baselines": {aid: dict(b.baselines) for aid, b in self.aabs.items()},
                "activeRuns": {pid: {"source": r.get("source"), "remaining": (r["end"] - self.clock.monotonic()) if r.get("end") else None} for pid, r in self._active_runs.items()},
            }
