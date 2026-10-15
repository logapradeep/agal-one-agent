"""A simulated pump house for the bundle-v3 unit tests: the compiled bundle of the shared site of the golden
vectors (two-node-layout.json, node_pumphouse), a SimClock, a SimulatedIO and a RecordingSink. No hardware."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import pytest

import v3_vectors as V
from agal_one_agent.blocks.clock import SimClock
from agal_one_agent.blocks.io import SimulatedIO
from agal_one_agent.blocks.runtime import RecordingSink
from agal_one_agent.blocks.v3.runtime import STATE_FILE, RuntimeV3

TZ = ZoneInfo("Asia/Kolkata")
PUMP, V1, V2, ESTOP, MAINS, UPS = "asset_pump0001", "asset_valve001", "asset_valve002", "asset_estop001", "asset_mains001", "asset_ups00001"
CLEAN = {"cleanStop": True, "valves": {"asset_valve002": "closed"}}  # the vectors' `initial.persisted`
HEALTHY = {(ESTOP, "sensor.contact"): True, (ESTOP, "sensor.reset"): False, (MAINS, "sensor.power"): True,
           (UPS, "sensor.power"): True, (PUMP, "sensor.current"): 0.0}


def pumphouse(version: int = 1) -> dict:
    if not os.path.isdir(V.VECTORS):
        pytest.skip(f"vectors not found at {V.VECTORS}")
    b = V.pointer("two-node-layout.json#/expect/bundles/node_pumphouse")
    b["version"], b["compiledAt"] = version, "2026-10-15T00:30:00Z"
    return b


def fieldbox(version: int = 1) -> dict:
    b = V.pointer("two-node-layout.json#/expect/bundles/node_fieldbox")
    b["version"], b["compiledAt"] = version, "2026-10-15T00:30:00Z"
    return b


def nxt(bundle: dict, change=None) -> dict:
    b = copy.deepcopy(bundle)
    b["version"] = bundle["version"] + 1
    b.pop("handover", None)
    if change:
        change(b)
    return b


def card(bundle: dict, aid: str) -> dict:
    return next(a for a in bundle["assets"] if a["assetId"] == aid)


def rule(bundle: dict, slot: int) -> dict:
    return next(r for r in bundle["rules"] if r["slot"] == slot)


def part(bundle: dict, part_id: str) -> dict:
    return next(p for p in bundle["instruction"] if p["partId"] == part_id)


def asset_var(name: str, aid: str, access: str, source_key: Optional[str] = None, builtin: Optional[str] = None,
              type_: str = "bool") -> dict:
    ref = {"assetId": aid, "access": access}
    if builtin:
        ref["builtin"] = builtin
    else:
        ref["sourceKey"] = source_key
    v = {"name": name, "kind": "asset", "type": type_, "asset": ref}
    if access in ("write", "stop"):
        v["settable"] = True
    return v


def rule_block(slot: int, program: dict, container: str = "field_north01", version: int = 1) -> dict:
    return {"key": f"field:{container}/rule/{slot}", "containerKind": "field", "containerId": container,
            "slot": slot, "version": version, "program": program}


class Bench:
    def __init__(self, bundle: Optional[dict] = None, start: Optional[datetime] = None, state_dir: Optional[str] = None,
                 persisted: Optional[dict] = None, inputs: Optional[dict] = None, node_uid: str = "node_pumphouse",
                 load: bool = True):
        self.clock = SimClock(start or datetime(2026, 10, 15, 5, 59, 58, tzinfo=TZ))
        self.io = SimulatedIO()
        self.sink = RecordingSink()
        # A state directory always: without one a latching valve has never been vouched for (a first start).
        # persisted=None → the vectors' clean stop with Valve 2 closed; persisted=False → no state file.
        if state_dir is None:
            state_dir = tempfile.mkdtemp(prefix="v3bench-")
        self.state_dir = str(state_dir)
        if persisted is None:
            persisted = CLEAN
        os.makedirs(self.state_dir, exist_ok=True)
        if persisted:
            with open(os.path.join(self.state_dir, STATE_FILE), "w") as f:
                json.dump(persisted, f)
        for (aid, sk), v in (HEALTHY if inputs is None else inputs).items():
            self.io.set_input(aid, sk, v)
        self.rt = RuntimeV3(self.io, self.sink, clock=self.clock, state_dir=self.state_dir, node_uid=node_uid)
        self.bundle = bundle
        if bundle is not None and load:
            self.rt.compile(bundle)

    def new_runtime(self) -> RuntimeV3:
        """A restart: a new runtime on the same state directory (and the same ports)."""
        self.rt = RuntimeV3(self.io, self.sink, clock=self.clock, state_dir=self.state_dir, node_uid=self.rt.node_uid)
        return self.rt

    def step(self) -> None:
        self.rt.step()

    def run(self, seconds: int) -> None:
        """`seconds` passes, one per simulated second (the clock advances before each)."""
        for _ in range(int(seconds)):
            self.clock.advance(1.0)
            self.rt.step()

    def until(self, hh: int, mm: int, ss: int = 0, day: int = 15) -> None:
        target = datetime(2026, 10, day, hh, mm, ss, tzinfo=TZ)
        while self.clock.now("Asia/Kolkata") < target:
            self.clock.advance(1.0)
            self.rt.step()

    def set(self, aid: str, sk: str, v) -> None:
        self.io.set_input(aid, sk, v)

    def level(self, aid: str, key: str):
        return self.rt.level(aid, key)

    def pump_on(self) -> bool:
        return bool(self.rt.level(PUMP, "device.power"))

    def mark(self) -> tuple[int, int]:
        return len(self.io.log), len(self.sink.events)

    def writes_since(self, mark: tuple[int, int]) -> list[dict]:
        return self.io.log[mark[0]:]

    def events_since(self, mark: tuple[int, int], type_: Optional[str] = None) -> list[dict]:
        evs = self.sink.events[mark[1]:]
        return [e for e in evs if type_ is None or e["type"] == type_]

    def events(self, type_: str) -> list[dict]:
        return [e for e in self.sink.events if e["type"] == type_]

    def source_of(self, event: dict) -> str:
        return self.sink.sources[self.sink.events.index(event)]

    def physical(self, aid: str, sk: str):
        return self.io.output(aid, sk)
