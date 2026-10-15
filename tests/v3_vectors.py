"""Replays the bundle-v3 golden vectors (Agal/contracts/programs/vectors, contracts v1.15.0) through RuntimeV3.

The vectors' RUNNER (instruction-switch.json `description`): start from `initial`; between steps the clock
advances in 1 s ticks and every tick is a pass; a step's `load` (and its `inputs`) land before that second's
pass; `writes` lists, in order, every physical output write made since the previous step up to and
including the step's pass — `value` is the logical level (the port's activeLow is the I/O layer's),
`pulseMs` a latching valve's coil pulse; `noWrites` names outputs that must not be written in that pass;
`levels` are the logical outputs after the pass (valve.open is a valve's position); `events` must each be
among what the node sent in that window, the payload compared for the keys given.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import _contracts

from agal_one_agent.blocks.clock import SimClock
from agal_one_agent.blocks.io import SimulatedIO
from agal_one_agent.blocks.runtime import RecordingSink
from agal_one_agent.blocks.v3.runtime import STATE_FILE, RuntimeV3

VECTORS = os.path.join(_contracts.CONTRACTS, "programs", "vectors")


def load(name: str) -> dict:
    with open(os.path.join(VECTORS, name)) as f:
        return json.load(f)


def pointer(ref: str) -> Any:
    """`file.json#/a/b/c` → that value of the vector file."""
    path, _, frag = ref.partition("#")
    doc = load(path)
    for part in [p for p in frag.split("/") if p]:
        doc = doc[int(part)] if isinstance(doc, list) else doc[part.replace("~1", "/").replace("~0", "~")]
    return copy.deepcopy(doc)


def bundle(vec: dict, name: str) -> dict:
    spec = vec["bundles"][name]
    src = spec["from"]
    base = pointer(src) if "#" in src else bundle(vec, src)
    b = copy.deepcopy(base)
    for k, v in spec.items():
        if k != "from":
            b[k] = copy.deepcopy(v)
    b.setdefault("compiledAt", "2026-10-15T00:30:00Z")
    return b


def subset(want: Any, got: Any) -> bool:
    """`want` ⊆ `got`: dict keys given are compared (recursively); lists and scalars exactly."""
    if isinstance(want, dict):
        return isinstance(got, dict) and all(k in got and subset(v, got[k]) for k, v in want.items())
    return want == got


class VectorRun:
    def __init__(self, vec: dict, state_dir: str, io: Optional[SimulatedIO] = None):
        self.vec = vec
        self.tz = ZoneInfo(vec.get("timezone") or "Asia/Kolkata")
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, STATE_FILE), "w") as f:
            json.dump(vec["initial"]["persisted"], f)
        self.io = io or SimulatedIO()
        for k, v in (vec["initial"].get("inputs") or {}).items():
            aid, sk = k.split(" ", 1)
            self.io.set_input(aid, sk, v)
        self.clock = SimClock(self.at(vec["steps"][0]["at"]), tz=vec.get("timezone") or "Asia/Kolkata")
        self.sink = RecordingSink()
        self.rt = RuntimeV3(self.io, self.sink, clock=self.clock, state_dir=state_dir, node_uid=vec["node"])
        self.failures: list[str] = []
        self.passes = 0

    def at(self, s: str) -> datetime:
        return datetime.fromisoformat(s).replace(tzinfo=self.tz)

    def tick(self) -> None:
        self.rt.step()
        self.passes += 1

    def run(self, on_step=None) -> list[str]:
        cur: Optional[datetime] = None
        log_mark = ev_mark = ack_mark = 0
        for step in self.vec["steps"]:
            at = self.at(step["at"])
            if cur is not None:
                while cur + timedelta(seconds=1) < at:
                    self.clock.advance(1.0)
                    cur += timedelta(seconds=1)
                    self.tick()
                self.clock.advance((at - cur).total_seconds())
            cur = at
            for k, v in (step.get("inputs") or {}).items():
                aid, sk = k.split(" ", 1)
                self.io.set_input(aid, sk, v)
            if step.get("load"):
                self.rt.compile(bundle(self.vec, step["load"]))
            pass_mark = len(self.io.log)
            self.tick()
            self.check(step, self.io.log[log_mark:], self.io.log[pass_mark:], self.sink.events[ev_mark:], self.sink.acks[ack_mark:])
            if on_step is not None:
                on_step(step, self)
            log_mark, ev_mark, ack_mark = len(self.io.log), len(self.sink.events), len(self.sink.acks)
        return self.failures

    def check(self, step: dict, window: list, this_pass: list, events: list, acks: list) -> None:
        exp, at = step.get("expect") or {}, step["at"]
        if "ack" in exp and not any(subset(exp["ack"], a) for a in acks):
            self.failures.append(f"{at}: ack {exp['ack']} not among {acks}")
        if "writes" in exp and window != exp["writes"]:
            self.failures.append(f"{at}: writes {window} != {exp['writes']}")
        for nw in exp.get("noWrites") or []:
            hit = [w for w in this_pass if w["assetId"] == nw["assetId"] and w["sourceKey"] == nw["sourceKey"]]
            if hit:
                self.failures.append(f"{at}: {nw} was written: {hit}")
        for k, want in (exp.get("levels") or {}).items():
            aid, key = k.split(" ", 1)
            got = self.rt.level(aid, key)
            if got != want:
                self.failures.append(f"{at}: level {k} = {got}, expected {want}")
        for e in exp.get("events") or []:
            if not any(ev.get("type") == e["type"] and subset(e.get("payload") or {}, ev) for ev in events):
                self.failures.append(f"{at}: event {e} not among {[ev for ev in events if ev.get('type') == e['type']] or [ev.get('type') for ev in events]}")
