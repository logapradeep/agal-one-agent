"""Bundle v3 — the compiled program of ONE node (contracts v1.15.0, ADR-029; programs/README.md §7).

``build(bundle, io)`` checks a ``CompiledBundleV3`` (automation-block.schema.json) and turns it into a
:class:`Program`: the cards with their ports, the outputs blocks may name (a motor through the output its
guard supervises, a valve through its logical ``valve.open``, any other output port as it is), the built-ins
(motor guards, valve drivers, E-stop, power sense) and the blocks — every layout's rule slots for this node
and its instruction parts. Nothing here runs: :mod:`.runtime` does.

What the node refuses (the bundle is rejected and the previous program keeps running) — the server's check
(README §7.4) is the first line; these are the ones a node can see for itself:

* a malformed bundle, a port this node does not have, a name that resolves to nothing;
* a motor card without its guard (a motor is never compiled without one);
* one physical line claimed by two cards, or two ports of one card, one of which drives it (RBAC-01);
* a block that writes around a built-in: a valve's coil port, or a motor port other than the one its
  guard supervises; an AAB ``variable`` (there is no AAB in v3); a ``stop`` set to anything but off;
* two writers of one output (a layout's today-only part runs instead of its every-day part, so the two
  are never counted as two writers — the reference's ``writerConflicts``);
* the v1 actions and events (``run_plot`` / ``stop_plot``, ``interruption``), a schedule in a rule slot,
  a ``plotId``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .. import expr as E
from ..io import IOAdapter, PortRef, describe_port, physical_port
from ..runtime import CompileError, _Rule, _Var, _signature
from .guard import MotorGuard
from .safety import EStop, PowerSense
from .valve import ValveDriver

logger = logging.getLogger(__name__)

LANGUAGE = 3
RESUME_AFTER_POWER_SECONDS = 30  # fixed (founder 2026-10-14); settings.resumeAfterPowerSeconds is const 30
MOTOR_TYPES = ("motor_controller",)
VALVE_TYPES = ("valve_controller", "fertigation_valve")
VALVE_OPEN = "valve.open"
# The settings a v3 block may read without declaring them — the reference validator's
# BLOCK_RUNTIME_SETTINGS (validate.mjs semantic(…, "block")).
BLOCK_RUNTIME_SETTINGS: dict[str, Any] = {"max_run_seconds": 7200.0, "valve_energise_seconds": 3.0, "resume_within_seconds": 900.0}
BUILTIN_TYPES = {"guard_tripped": "bool", "guard_reason": "enum", "guard_locked_out": "bool",
                 "valve_state": "enum", "valve_fault": "bool", "estop_latched": "bool", "power_present": "bool"}
V3_EVENTS = ("variable_changed", "boot", "power_restored", "program_applied", "schedule_started", "schedule_ended",
             "run_missed", "automation_paused", "automation_resumed")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def is_on(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return bool(v)


def off_value(t: str):
    return False if t == "bool" else 0.0


class Card:
    def __init__(self, d: dict, index: int):
        self.asset_id: str = d["assetId"]
        self.index = index
        self.asset_type: str = str(d.get("assetType") or "")
        self.name: str = str(d.get("name") or self.asset_id)
        self.safe_state: str = str(d.get("safeState") or "off")
        self.ports: dict[str, PortRef] = {}
        self.d = d

    def outputs(self) -> list[PortRef]:
        return [p for p in self.ports.values() if is_output_port(p.transport)]

    def inputs(self) -> list[PortRef]:
        return [p for p in self.ports.values() if not is_output_port(p.transport)]


def is_output_port(t: dict) -> bool:
    return (t or {}).get("direction") == "out" or (t or {}).get("kind") == "pwm"


def is_digital_input(t: dict) -> bool:
    return str((t or {}).get("kind", "gpio")) == "gpio" and not ((t or {}).get("measure") or (t or {}).get("transform"))


class Output:
    """A logical output of the node: ``(assetId, key)`` — a motor's guarded output, a valve's
    ``valve.open``, or any other output port. ``level`` is what the program wants after this pass;
    the physical write follows at the end of the pass (with the built-ins' veto)."""

    __slots__ = ("key", "asset_id", "source_key", "kind", "type", "port", "valve", "guard", "is_motor", "index",
                 "level", "changed_at", "last_writer", "written", "card")

    def __init__(self, card: Card, source_key: str, kind: str, port: Optional[PortRef], index: int):
        self.key = (card.asset_id, source_key)
        self.asset_id, self.source_key = card.asset_id, source_key
        self.card = card
        self.kind = kind  # "motor" | "valve" | "plain"
        self.port = port
        self.type = "number" if port is not None and str(port.transport.get("kind")) == "pwm" else "bool"
        self.valve: Optional[ValveDriver] = None
        self.guard: Optional[MotorGuard] = None
        self.is_motor = kind == "motor"
        self.index = index
        self.level: Any = off_value(self.type)
        self.changed_at: Optional[float] = None
        self.last_writer: Optional[str] = None  # a block key, "manual", or None
        self.written: Any = None                # last physical level written (motor / plain); None = never


class Block:
    """A rule slot (``kind`` rule) or an instruction part (``kind`` part) of one layout."""

    def __init__(self, kind: str, d: dict):
        self.kind = kind
        self.d = d
        self.key: str = str(d["key"])
        self.container_kind: str = str(d["containerKind"])
        self.container_id: str = str(d["containerId"])
        self.layout = f"{self.container_kind}:{self.container_id}"
        self.version = int(d.get("version") or 0)
        self.slot = d.get("slot")
        self.part_id: Optional[str] = d.get("partId")
        self.validity: Optional[str] = d.get("validity")
        self.valid_on: Optional[str] = d.get("validOn")
        self.program: dict = d.get("program") or {}
        self.signature = _signature(self.program)
        self.vars: dict[str, _Var] = {}
        self.order: list[str] = []
        self.rules: list[_Rule] = []
        self.schedules: dict[str, dict] = {}
        self.settings: dict[str, Any] = dict(BLOCK_RUNTIME_SETTINGS)
        self.settings.update(self.program.get("settings") or {})
        self.baselines: dict[str, Optional[float]] = {}
        self._learn: dict[str, dict] = {}
        self.alert_var: Optional[_Var] = None
        self.binds: dict[str, tuple] = {}     # var name → ("out", key, access) | ("in", key) | ("builtin", assetId, name)
        self.writes: set[tuple[str, str]] = set()
        self.stops: set[tuple[str, str]] = set()
        self.uses_valves: set[str] = set()
        self.drives_motor = False
        self.interlock = False
        self.entry: dict[str, Any] = {}

    def ref(self) -> dict:
        r: dict = {"containerKind": self.container_kind, "containerId": self.container_id,
                   "slot": self.slot if self.kind == "rule" else "instruction"}
        if self.kind == "part":
            r["partId"] = self.part_id
        return r

    def scope(self) -> dict:
        def _t(v):
            return "bool" if isinstance(v, bool) else "number" if isinstance(v, (int, float)) else "string"
        return {"variables": {n: v.type for n, v in self.vars.items()}, "settings": {k: _t(v) for k, v in self.settings.items()}}


class Program:
    def __init__(self, bundle: dict):
        self.bundle = bundle
        self.version: int = int(bundle["version"])
        self.tz: str = str(bundle.get("timezone") or "Asia/Kolkata")
        self.location: Optional[dict] = bundle.get("location")
        self.cards: dict[str, Card] = {}
        self.card_order: list[str] = []
        self.outputs: dict[tuple[str, str], Output] = {}
        self.output_order: list[tuple[str, str]] = []
        self.guards: dict[str, MotorGuard] = {}
        self.valves: dict[str, ValveDriver] = {}
        self.estop: Optional[EStop] = None
        self.estop_card: Optional[str] = None
        self.power: Optional[PowerSense] = None
        self.rules: list[Block] = []
        self.parts: list[Block] = []
        self.layouts: dict[str, dict[str, Block]] = {}  # layout → {"every_day": blk, "today_only": blk}
        self.layout_order: list[str] = []
        self.handover: dict[tuple[str, str], str] = {}
        self.inputs: list[PortRef] = []

    # ------------------------------------------------------------ lookups

    def blocks(self) -> list[Block]:
        return self.parts + self.rules

    def block(self, key: str) -> Optional[Block]:
        for b in self.blocks():
            if b.key == key:
                return b
        return None

    def active_part(self, layout: str, date: str) -> Optional[Block]:
        """README §7.6: the today-only part whose validOn is the node's local date, else the every-day part."""
        parts = self.layouts.get(layout) or {}
        t = parts.get("today_only")
        if t is not None and t.valid_on == date:
            return t
        return parts.get("every_day")

    def written_on(self, date: str) -> set[tuple[str, str]]:
        out: set = set()
        for r in self.rules:
            out |= r.writes
        for lk in self.layout_order:
            p = self.active_part(lk, date)
            if p is not None:
                out |= p.writes
        return out

    def all_written(self) -> set[tuple[str, str]]:
        out: set = set()
        for b in self.blocks():
            out |= b.writes
        return out

    def writer_on(self, key: tuple[str, str], date: str) -> Optional[Block]:
        for r in self.rules:
            if key in r.writes:
                return r
        for lk in self.layout_order:
            p = self.active_part(lk, date)
            if p is not None and key in p.writes:
                return p
        return None

    def physical_in_use(self) -> set:
        return {physical_port(p.transport) for c in self.cards.values() for p in c.ports.values()}


# ===================================================================== build

def build(bundle: dict, io: IOAdapter) -> Program:
    try:
        return _build(bundle, io)
    except CompileError:
        raise
    except Exception as e:  # noqa: BLE001
        raise CompileError(f"malformed bundle: {e}") from e


def _build(bundle: dict, io: IOAdapter) -> Program:
    if not isinstance(bundle, dict) or bundle.get("language") != LANGUAGE:
        raise CompileError("not a language-3 bundle")
    version = bundle.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise CompileError("bundle.version missing")
    settings = bundle.get("settings") or {}
    if settings.get("resumeAfterPowerSeconds", RESUME_AFTER_POWER_SECONDS) != RESUME_AFTER_POWER_SECONDS:
        logger.warning("bundle v%s says resumeAfterPowerSeconds %s: it is fixed at %d s", version,
                       settings.get("resumeAfterPowerSeconds"), RESUME_AFTER_POWER_SECONDS)
    if bundle.get("dataCards"):
        logger.warning("bundle v%s carries %d data cards: data card capture (README §4b) is not built in this agent; "
                       "they record nothing", version, len(bundle["dataCards"]))
    p = Program(bundle)
    for i, a in enumerate(bundle.get("assets") or []):
        aid = a.get("assetId")
        if not aid or aid in p.cards:
            raise CompileError(f"asset id missing or duplicated: {aid}")
        card = Card(a, i)
        for sk, t in (a.get("ports") or {}).items():
            if not isinstance(t, dict):
                raise CompileError(f"{aid}.{sk}: the port has no transport")
            ref = PortRef(aid, sk, dict(t))
            if not io.has_port(ref):
                raise CompileError(f"{aid}: this node has no port for '{sk}' ({describe_port(t)})")
            card.ports[sk] = ref
        p.cards[aid] = card
        p.card_order.append(aid)
    _one_card_per_port(p)
    for aid in p.card_order:
        _card_builtins_and_outputs(p, p.cards[aid])
    p.inputs = [ref for aid in p.card_order for ref in p.cards[aid].inputs()]
    _safety(p, bundle.get("safety") or {})
    seen: set[str] = set()
    for d in bundle.get("rules") or []:
        blk = Block("rule", d)
        if blk.key in seen:
            raise CompileError(f"block {blk.key} is in the bundle twice")
        seen.add(blk.key)
        if blk.program.get("schedules"):
            raise CompileError(f"rule {blk.key}: a rule has no schedules — the instruction says what runs")
        _finish_block(p, blk)
        p.rules.append(blk)
    for d in bundle.get("instruction") or []:
        blk = Block("part", d)
        if blk.key in seen:
            raise CompileError(f"block {blk.key} is in the bundle twice")
        seen.add(blk.key)
        if blk.validity not in ("every_day", "today_only"):
            raise CompileError(f"instruction part {blk.key}: validity {blk.validity!r}")
        if blk.validity == "today_only" and not blk.valid_on:
            raise CompileError(f"instruction part {blk.key}: a today-only part needs validOn")
        slots = p.layouts.setdefault(blk.layout, {})
        if blk.validity in slots:
            raise CompileError(f"layout {blk.layout} has two {blk.validity} parts on this node")
        slots[blk.validity] = blk
        if blk.layout not in p.layout_order:
            p.layout_order.append(blk.layout)
        _finish_block(p, blk)
        p.parts.append(blk)
    _one_writer(p)
    for r in (bundle.get("handover") or {}).get("releases") or []:
        p.handover[(str(r.get("assetId")), str(r.get("sourceKey")))] = str(r.get("when"))
    return p


def _one_card_per_port(p: Program) -> None:
    claims: dict[tuple, list[tuple[str, str, bool, dict]]] = {}
    for aid in p.card_order:
        for ref in p.cards[aid].ports.values():
            claims.setdefault(physical_port(ref.transport), []).append((aid, ref.source_key, is_output_port(ref.transport), ref.transport))
    for who in claims.values():
        cards = sorted({(aid, key) for aid, key, _, _ in who})
        if len(cards) > 1 and any(out for _, _, out, _ in who):
            named = " and ".join(f"{key} of card {aid}" for aid, key in cards)
            raise CompileError(f"{describe_port(who[0][3])} is claimed by {named}: a port is linked to one card")


def _card_builtins_and_outputs(p: Program, card: Card) -> None:
    a, aid = card.d, card.asset_id
    g, v = a.get("guard"), a.get("valve")
    if card.asset_type in MOTOR_TYPES and not g:
        raise CompileError(f"motor {aid} has no guard: a motor is never compiled without one")
    owned: set[str] = set()  # ports a built-in owns
    if g:
        out_key = g.get("output")
        ref = card.ports.get(out_key)
        if ref is None or not is_output_port(ref.transport):
            raise CompileError(f"motor {aid}: the guard's output '{out_key}' is not an output port of the card")
        for k in list(g.get("current") or []) + list(g.get("phaseSense") or []) + ([g["voltage"]] if g.get("voltage") else []):
            r = card.ports.get(k)
            if r is None or is_output_port(r.transport):
                raise CompileError(f"motor {aid}: the guard's input '{k}' is not an input port of the card")
        guard = MotorGuard(aid, g, card.name)
        p.guards[aid] = guard
        out = _add_output(p, card, out_key, "motor", ref)
        out.guard = guard
        owned.add(out_key)
    if v:
        drv_ports = {}
        for k in (v.get("open"), v.get("close")):
            if k is None:
                continue
            r = card.ports.get(k)
            if r is None or not is_output_port(r.transport):
                raise CompileError(f"valve {aid}: coil '{k}' is not an output port of the card")
            drv_ports[k] = r
        if v.get("open") is None:
            raise CompileError(f"valve {aid}: the driver names no open output")
        if v.get("actuation") == "latching_dc":
            if not v.get("close") or not v.get("supply"):
                raise CompileError(f"valve {aid}: a latching valve needs its close output and its sensed supply")
            if v["close"] == v["open"]:
                raise CompileError(f"valve {aid}: the open and close pulses need two outputs")
        elif v.get("actuation") not in ("continuous_ac", "continuous_dc"):
            raise CompileError(f"valve {aid}: actuation {v.get('actuation')!r}")
        drv = ValveDriver(aid, v, drv_ports, card.name)
        p.valves[aid] = drv
        out = _add_output(p, card, VALVE_OPEN, "valve", None)
        out.valve = drv
        owned.update(drv_ports)
    for ref in card.outputs():
        if ref.source_key not in owned:
            _add_output(p, card, ref.source_key, "plain", ref)


def _add_output(p: Program, card: Card, key: str, kind: str, ref: Optional[PortRef]) -> Output:
    out = Output(card, key, kind, ref, len(p.output_order))
    p.outputs[out.key] = out
    p.output_order.append(out.key)
    return out


def _safety(p: Program, safety: dict) -> None:
    def input_of(pk: dict, what: str) -> tuple[str, str]:
        aid, sk = pk.get("assetId"), pk.get("sourceKey")
        card = p.cards.get(aid)
        ref = card.ports.get(sk) if card else None
        if ref is None or is_output_port(ref.transport):
            raise CompileError(f"{what}: {sk} of card {aid} is not an input of this node")
        return aid, sk
    e = safety.get("emergencyStop")
    if e:
        input_of(e.get("input") or {}, "the E-stop's contact")
        input_of(e.get("reset") or {}, "the E-stop's reset")
        if e.get("contact", "nc") not in ("nc", "no") or e.get("resetKind") not in ("reset_input", "panel_start"):
            raise CompileError("the E-stop's contact or reset kind is not one the node knows")
        p.estop = EStop(e)
        p.estop_card = e["input"]["assetId"]
    ps = safety.get("powerSense")
    if ps:
        input_of(ps.get("mains") or {}, "the mains sense")
        if ps.get("ups"):
            input_of(ps["ups"], "the UPS sense")
        p.power = PowerSense(ps)
    for aid, drv in p.valves.items():
        if drv.latching:
            input_of({"assetId": drv.supply[0], "sourceKey": drv.supply[1]} if drv.supply else {}, f"valve {aid}'s supply")


# ------------------------------------------------------------------ blocks

def _check_var(vd: dict, blk: Block) -> None:
    name = vd.get("name")
    if not E.is_identifier(name or ""):
        raise CompileError(f"{blk.key}: bad variable name {name!r}")
    if name in blk.vars:
        raise CompileError(f"{blk.key}: duplicate variable '{name}'")
    if vd.get("kind") not in ("asset", "local", "alert"):
        raise CompileError(f"{blk.key}: variable '{name}': kind {vd.get('kind')} is not a v3 block kind")
    if vd.get("type") not in ("bool", "number", "string", "enum"):
        raise CompileError(f"{blk.key}: variable '{name}': bad type")
    if vd.get("kind") == "alert" and vd.get("type") != "string":
        raise CompileError(f"{blk.key}: alert variable '{name}' must be a string")


def _bind_asset_var(p: Program, blk: Block, v: _Var, vd: dict) -> None:
    ref = vd.get("asset") or {}
    aid, access = ref.get("assetId"), ref.get("access")
    ctx = f"{blk.key}: variable '{v.name}'"
    forms = [k for k in ("variable", "sourceKey", "builtin") if ref.get(k) is not None]
    if len(forms) != 1:
        raise CompileError(f"{ctx}: an asset reference names exactly one of sourceKey and builtin")
    if forms[0] == "variable":
        raise CompileError(f"{ctx}: v3 has no AAB — name a port (sourceKey) or a builtin")
    card = p.cards.get(aid)
    if card is None:
        raise CompileError(f"{ctx}: card '{aid}' is not on this node")
    if access not in ("read", "write", "stop"):
        raise CompileError(f"{ctx}: access {access!r}")
    if forms[0] == "builtin":
        b = ref["builtin"]
        if b not in BUILTIN_TYPES:
            raise CompileError(f"{ctx}: unknown builtin {b!r}")
        if access != "read":
            raise CompileError(f"{ctx}: a builtin is read only")
        ok = ((b.startswith("guard_") and aid in p.guards) or (b.startswith("valve_") and aid in p.valves)
              or (b == "estop_latched" and p.estop is not None and aid == p.estop_card)
              or (b == "power_present" and (card.asset_type == "power_sense" or _sense_port(p, aid) is not None)))
        if not ok:
            raise CompileError(f"{ctx}: {b} is not a built-in of card {aid}")
        want = BUILTIN_TYPES[b]
        if not (v.type == want or (want == "enum" and v.type == "number")):
            raise CompileError(f"{ctx}: {b} is {want}, declared {v.type}")
        blk.binds[v.name] = ("builtin", aid, b)
        return
    sk = ref["sourceKey"]
    if aid in p.valves and sk != VALVE_OPEN:
        raise CompileError(f"{ctx}: names valve {aid}'s {sk} — a block drives a valve through valve.open (its coils are the driver's)")
    if aid in p.guards and access != "read" and sk != p.guards[aid].output:
        raise CompileError(f"{ctx}: drives {sk} of motor {aid}, not the output its guard supervises")
    out = p.outputs.get((aid, sk))
    if out is not None:
        if access != "read" and v.type not in ("bool", "number"):
            raise CompileError(f"{ctx}: an output is bool or number")
        if access in ("write", "stop") and not v.settable:
            raise CompileError(f"{ctx}: a written or stopped asset variable must be settable")
        blk.binds[v.name] = ("out", out.key, access)
        if access == "write":
            blk.writes.add(out.key)
            blk.drives_motor = blk.drives_motor or out.is_motor
        elif access == "stop":
            blk.stops.add(out.key)
        if out.kind == "valve" and out.valve is not None and out.valve.latching:
            blk.uses_valves.add(aid)  # reference usesValve: write, stop or read of valve.open
        return
    pref = card.ports.get(sk)
    if pref is None:
        raise CompileError(f"{ctx}: card {aid} has no port '{sk}' on this node")
    if access != "read":
        raise CompileError(f"{ctx}: {sk} of card {aid} is an input — it is read, never {access}")
    v.settable = False
    blk.binds[v.name] = ("in", (aid, sk))


def _sense_port(p: Program, aid: str) -> Optional[str]:
    if p.power is not None:
        for k in (p.power.mains, p.power.ups):
            if k is not None and k[0] == aid:
                return k[1]
    card = p.cards.get(aid)
    if card is not None and card.asset_type == "power_sense":
        ins = card.inputs()
        return ins[0].source_key if ins else None
    return None


def _finish_block(p: Program, blk: Block) -> None:
    prog = blk.program
    for vd in prog.get("variables") or []:
        _check_var(vd, blk)
        v = _Var(vd)
        if v.kind == "asset":
            _bind_asset_var(p, blk, v, vd)
            v.settable = blk.binds[v.name][0] == "out" and blk.binds[v.name][2] in ("write", "stop")
        if v.kind == "alert":
            blk.alert_var = v
        blk.vars[v.name] = v
        blk.order.append(v.name)
    outs = blk.writes | blk.stops
    blk.interlock = bool(outs) and not blk.writes
    if blk.kind == "rule" and "interlock" in blk.d and bool(blk.d["interlock"]) != blk.interlock:
        logger.warning("%s: the bundle says interlock=%s, its outputs say %s — the node goes by its outputs",
                       blk.key, blk.d["interlock"], blk.interlock)
    scope = blk.scope()

    def parse(src: str, ctx: str, want_bool: bool = True):
        try:
            ast = E.parse(src)
        except E.ExprError as e:
            raise CompileError(f"{blk.key} {ctx}: {e}") from e
        t, errs = E.validate(ast, scope)
        if errs:
            raise CompileError(f"{blk.key} {ctx}: {errs[0]}")
        if want_bool and t != "bool":
            raise CompileError(f"{blk.key} {ctx}: must be a bool expression (is {t})")
        return ast, t

    for v in blk.vars.values():
        d = v.d
        if d.get("expression"):
            if v.kind != "local":
                raise CompileError(f"{blk.key}: {v.name}: only local variables may be computed")
            v.expr_ast, t = parse(d["expression"], f"variable '{v.name}'", want_bool=False)
            v.settable = False
            if t != v.type and not (v.type == "enum" and t == "number"):
                raise CompileError(f"{blk.key}: variable '{v.name}': expression is {t}, declared {v.type}")
        if d.get("baseline"):
            lw = d["baseline"].get("learnWhen")
            if lw not in blk.vars or blk.vars[lw].type != "bool":
                raise CompileError(f"{blk.key}: {v.name}: baseline.learnWhen must be a bool variable")
            if v.type != "number":
                raise CompileError(f"{blk.key}: {v.name}: only numeric variables learn a baseline")
            blk.baselines[v.name] = None
    seq_ids = {r.get("id") for r in prog.get("rules") or [] if r.get("kind") == "sequence"}
    seen: set = set()
    for rd in prog.get("rules") or []:
        if rd.get("id") in seen:
            raise CompileError(f"{blk.key}: duplicate rule id '{rd.get('id')}'")
        seen.add(rd.get("id"))
        if rd.get("protection"):
            logger.info("%s rule %s: the protection flag is not used in v3 (a motor's protection is its guard)", blk.key, rd.get("id"))
        r = _Rule(rd)
        ctx = f"rule '{r.id}'"
        if r.kind == "when":
            w = rd.get("when") or {}
            r.cond_ast, _ = parse(w.get("condition", ""), ctx)
            ia = w.get("ignoreAfter")
            if ia and (ia not in blk.vars or blk.vars[ia].type != "bool"):
                raise CompileError(f"{blk.key} {ctx}: ignoreAfter must be a bool variable")
        elif r.kind == "limit":
            lv = (rd.get("limit") or {}).get("variable")
            if lv not in blk.vars or blk.vars[lv].type != "bool":
                raise CompileError(f"{blk.key} {ctx}: limit.variable must be a bool variable")
        elif r.kind == "on":
            o = rd.get("on") or {}
            if o.get("event") not in V3_EVENTS:
                raise CompileError(f"{blk.key} {ctx}: event {o.get('event')!r} is not a v3 event")
            if o.get("event") == "variable_changed" and o.get("variable") not in blk.vars:
                raise CompileError(f"{blk.key} {ctx}: unknown variable '{o.get('variable')}'")
            if o.get("filter"):
                r.filter_ast, _ = parse(o["filter"], f"{ctx} filter")
        elif r.kind == "sequence":
            s = rd.get("sequence") or {}
            if not s.get("steps"):
                raise CompileError(f"{blk.key} {ctx}: a sequence needs steps")
            if s.get("abortWhen"):
                r.abort_ast, _ = parse(s["abortWhen"], f"{ctx} abortWhen")
            for st in s["steps"]:
                _check_actions(blk, st.get("actions") or [], seq_ids, ctx, scope)
            _check_actions(blk, s.get("abortActions") or [], seq_ids, ctx, scope)
        else:
            raise CompileError(f"{blk.key} {ctx}: unknown kind {r.kind}")
        _check_actions(blk, rd.get("actions") or [], seq_ids, ctx, scope)
        _check_actions(blk, rd.get("elseActions") or [], seq_ids, ctx, scope)
        blk.rules.append(r)
    for sd in prog.get("schedules") or []:
        sid = sd.get("id")
        if not sid or sid in blk.schedules:
            raise CompileError(f"{blk.key}: schedule id missing or duplicated: {sid!r}")
        if sd.get("plotId"):
            raise CompileError(f"{blk.key} schedule '{sid}': plotId is v1 — a part's schedule sets its valves")
        if not sd.get("start") and not sd.get("sunEvent"):
            raise CompileError(f"{blk.key} schedule '{sid}': needs start or sunEvent")
        dur = sd.get("durationMinutes")
        if not isinstance(dur, (int, float)) or isinstance(dur, bool) or dur <= 0:
            raise CompileError(f"{blk.key} schedule '{sid}': durationMinutes")
        _check_actions(blk, sd.get("actions") or [], seq_ids, f"schedule '{sid}'", scope)
        _check_actions(blk, sd.get("endActions") or [], seq_ids, f"schedule '{sid}'", scope)
        blk.schedules[sid] = sd


def _check_actions(blk: Block, actions: list, seq_ids: set, ctx: str, scope: dict) -> None:
    for a in actions:
        t = a.get("type")
        if t == "set":
            vn = a.get("variable")
            v = blk.vars.get(vn)
            if v is None:
                raise CompileError(f"{blk.key} {ctx}: unknown variable '{vn}'")
            bind = blk.binds.get(vn)
            if bind is not None and not (bind[0] == "out" and bind[2] in ("write", "stop")):
                raise CompileError(f"{blk.key} {ctx}: '{vn}' is read only")
            writable = bind is not None or (v.kind == "local" and v.expr_ast is None and not v.d.get("expression")) or v.kind == "alert"
            if not writable:
                raise CompileError(f"{blk.key} {ctx}: '{vn}' is not settable")
            if a.get("expression"):
                try:
                    ast = E.parse(a["expression"])
                except E.ExprError as e:
                    raise CompileError(f"{blk.key} {ctx}: {e}") from e
                _, errs = E.validate(ast, scope)
                if errs:
                    raise CompileError(f"{blk.key} {ctx}: {errs[0]}")
                a["_ast"] = ast
            elif "value" not in a:
                raise CompileError(f"{blk.key} {ctx}: set needs value or expression")
            if bind is not None and bind[2] == "stop" and "_ast" not in a and is_on(a.get("value")):
                raise CompileError(f"{blk.key} {ctx}: '{vn}' is a stop — it may only be set off")
        elif t == "alert":
            if not a.get("text"):
                raise CompileError(f"{blk.key} {ctx}: alert needs text")
        elif t == "start_sequence":
            if a.get("sequenceId") not in seq_ids:
                raise CompileError(f"{blk.key} {ctx}: unknown sequence '{a.get('sequenceId')}'")
        elif t == "log":
            if not a.get("text"):
                raise CompileError(f"{blk.key} {ctx}: log needs text")
        elif t in ("run_plot", "stop_plot"):
            raise CompileError(f"{blk.key} {ctx}: {t} is v1 — a block sets its valves")
        else:
            raise CompileError(f"{blk.key} {ctx}: unknown action type {t}")


def _one_writer(p: Program) -> None:
    """The reference's writerConflicts: rules + the every-day parts, and again with each today-only part
    in its layout's place. `stop` is not a writer."""
    every = [b for b in p.parts if b.validity == "every_day"]
    configs = [every] + [[e for e in every if e.layout != t.layout] + [t] for t in p.parts if t.validity == "today_only"]
    for cfg in configs:
        writers: dict[tuple[str, str], list[str]] = {}
        for b in p.rules + cfg:
            for k in b.writes:
                writers.setdefault(k, []).append(b.key)
        for k, bs in writers.items():
            if len(bs) > 1:
                raise CompileError(f"{k[1]} of card {k[0]} has two writers on this node ({' and '.join(bs)}): one writer per output")


# ------------------------------------------------------------- stable actions

def stable_action(blk: Block, a: dict) -> Optional[tuple]:
    """An action in terms that survive a swap: what it names, not the name the block gives it."""
    t = a.get("type")
    if t == "set":
        bind = blk.binds.get(a.get("variable"))
        if bind is not None and bind[0] == "out":
            target: tuple = ("out", bind[1][0], bind[1][1])
        elif blk.vars.get(a.get("variable")) is not None and blk.vars[a["variable"]].kind == "alert":
            target = ("alert",)
        else:
            target = ("local", blk.key, a.get("variable"))
        return ("set", target, a.get("expression") if a.get("expression") else ("value", _freeze(a.get("value"))))
    if t in ("log", "alert"):
        return (t, a.get("text"), a.get("severity"))
    if t == "start_sequence":
        return (t, blk.key, a.get("sequenceId"))
    return None


def _freeze(v):
    return v if not isinstance(v, (dict, list)) else repr(v)


def start_set(blk: Block, sd: dict) -> set[tuple[str, str]]:
    """The outputs a schedule's start actions switch on (the reference's startSets)."""
    out: set = set()
    for a in sd.get("actions") or []:
        if a.get("type") != "set" or "_ast" in a:
            continue
        bind = blk.binds.get(a.get("variable"))
        if bind is not None and bind[0] == "out" and is_on(a.get("value")):
            out.add(bind[1])
    return out


def copy_actions(actions: list) -> list:
    return [dict(a) for a in actions or []]


__all__ = ["build", "Program", "Block", "Output", "Card", "LANGUAGE", "RESUME_AFTER_POWER_SECONDS", "VALVE_OPEN",
           "is_on", "off_value", "is_digital_input", "is_output_port", "stable_action", "start_set", "DAYS"]
