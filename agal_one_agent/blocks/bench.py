"""Bench physics for a cloud-connected simulated node (rebuild P4 exit test).

With ``blocks.simulated_io: true`` the agent runs on a laptop with in-memory
ports and this thread answers the outputs the way the reference build's
plumbing would: a valve that has been open for ``flow_delay`` seconds while
the pump relay is on makes its plot's flow switch read *flowing*; a pump whose
relay has been on for ``current_delay`` seconds draws its rated current (or a
nominal 4.5 A when the rating is still 0 = "learn on first run"); three-phase
pumps read all three currents and all mains-sense inputs present.

Faults (the bench page, ``bench_ui``): a sticky override the operator sets to
provoke what the physics never does by itself — a dry run (current collapses),
a lost phase, flow on a closed plot, no flow on an open plot. Nothing here
touches the cloud — the runtime publishes variables and alerts as it would on
real hardware, so the phone sees R-7 / R-8 / R-9 / R-11 / R-13 / R-28 behaviour
end to end.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from .io import SimulatedIO

logger = logging.getLogger(__name__)

NOMINAL_CURRENT_A = 4.5
DRY_RUN_FRACTION = 0.3  # a dry pump still draws 30-50 % of its running current

FAULT_KINDS = ("dry_run", "phase_loss", "no_flow", "flow_on_closed")


class BenchPhysics:
    def __init__(self, runtime, io: SimulatedIO, flow_delay: float = 5.0, current_delay: float = 2.0, tick: float = 0.5):
        self.runtime = runtime
        self.io = io
        self.flow_delay = flow_delay
        self.current_delay = current_delay
        self.tick = tick
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._valve_on_since: dict[str, float] = {}
        self._pump_on_since: dict[str, float] = {}
        # Sticky operator faults: key → {kind, assetId?, plotId?, phase?, since}
        self.faults: dict[str, dict] = {}
        self.journal: deque = deque(maxlen=80)
        self._last_flow: dict[str, bool] = {}
        self._last_running: dict[str, bool] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="bench-physics", daemon=True)
        self._thread.start()
        logger.warning("BENCH PHYSICS: simulated ports answer the program (valves → flow after %.0fs, pump → current after %.0fs)",
                       self.flow_delay, self.current_delay)
        self.note("bench physics started")

    def stop(self) -> None:
        self._stop.set()

    def note(self, text: str) -> None:
        self.journal.append({"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "text": text})
        logger.info("bench: %s", text)

    def set_fault(self, kind: str, asset_id: Optional[str] = None, plot_id: Optional[str] = None,
                  phase: Optional[str] = None, on: bool = True) -> str:
        if kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault '{kind}'")
        if kind in ("dry_run", "phase_loss") and not asset_id:
            raise ValueError(f"{kind} needs assetId")
        if kind in ("no_flow", "flow_on_closed") and not plot_id:
            raise ValueError(f"{kind} needs plotId")
        if kind == "phase_loss" and phase not in ("r", "y", "b"):
            raise ValueError("phase_loss needs phase r, y or b")
        key = ":".join(x for x in (kind, asset_id or "", plot_id or "", phase or "") if x)
        with self._lock:
            if on:
                self.faults[key] = {"kind": kind, "assetId": asset_id, "plotId": plot_id, "phase": phase,
                                    "since": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                self.note(f"fault ON: {key}")
            else:
                if self.faults.pop(key, None) is not None:
                    self.note(f"fault off: {key}")
        return key

    def clear_faults(self) -> None:
        with self._lock:
            if self.faults:
                self.faults.clear()
                self.note("all faults cleared")

    def describe(self) -> dict:
        """The bench state the UI shows (assets, ports, plots, timers, faults)."""
        bundle = getattr(self.runtime, "bundle", None) or {}
        assets = [a for a in bundle.get("assets", []) if isinstance(a, dict)]
        nab = bundle.get("nab") or {}
        var_asset = self._var_assets(nab)
        names = {a.get("assetId"): a.get("name") or a.get("assetId") for a in assets}
        out_assets = []
        for a in assets:
            aid = a.get("assetId")
            ports = []
            for key, transport in (a.get("ports") or {}).items():
                direction = (transport or {}).get("direction") or ("out" if key.startswith("device.") else "in")
                if direction == "out":
                    value = self.io.output(aid, key, None)
                else:
                    value = self.io.read_input(_Port(aid, key))
                ports.append({"key": key, "direction": direction, "value": value})
            out_assets.append({"assetId": aid, "name": names.get(aid), "type": a.get("assetType"),
                               "ports": sorted(ports, key=lambda p: (p["direction"] != "out", p["key"]))})
        plots = []
        for plot in nab.get("plots", []) or []:
            if not isinstance(plot, dict):
                continue
            valves = []
            for vn in plot.get("valveVariables", []) or []:
                va = var_asset.get(vn)
                if va:
                    valves.append({"assetId": va, "name": names.get(va, va), "on": bool(self.io.output(va, "device.power", False))})
            flow = None
            fv = plot.get("flowVariable")
            fa = var_asset.get(fv) if fv else None
            if fa:
                flow_ports = [k for k in ((next((x for x in assets if x.get("assetId") == fa), {}) or {}).get("ports") or {}) if "flow" in k]
                flowing = bool(self.io.read_input(_Port(fa, flow_ports[0]))) if flow_ports else None
                flow = {"assetId": fa, "name": names.get(fa, fa), "flowing": flowing}
            plots.append({"plotId": plot.get("plotId"), "name": plot.get("name") or plot.get("plotId"),
                          "manual": bool(plot.get("manual")), "valves": valves, "flow": flow})
        now = time.monotonic()
        with self._lock:
            faults = [{"key": k, **v} for k, v in self.faults.items()]
        return {
            "assets": out_assets,
            "plots": plots,
            "physics": {
                "flowDelay": self.flow_delay, "currentDelay": self.current_delay,
                "valveOnFor": {k: round(now - v, 1) for k, v in self._valve_on_since.items()},
                "pumpOnFor": {k: round(now - v, 1) for k, v in self._pump_on_since.items()},
            },
            "faults": faults,
            "journal": list(self.journal)[-40:],
        }

    def step(self, now: Optional[float] = None) -> None:
        """One physics pass (also used by the tests without the thread)."""
        now = time.monotonic() if now is None else now
        bundle = getattr(self.runtime, "bundle", None)
        if not bundle:
            return
        assets = {a.get("assetId"): a for a in bundle.get("assets", []) if isinstance(a, dict)}
        nab = bundle.get("nab") or {}
        var_asset = self._var_assets(nab)
        with self._lock:
            faults = list(self.faults.values())

        def fault(kind, **match):
            return [f for f in faults if f["kind"] == kind and all(f.get(k) == v for k, v in match.items())]

        # Pumps: relay → current (and mains present).
        pump_relay_on = False
        for aid, asset in assets.items():
            if asset.get("assetType") not in ("motor_controller", "dosing_pump"):
                continue
            ports = asset.get("ports") or {}
            relay_on = bool(self.io.output(aid, "device.power", False))
            pump_relay_on = pump_relay_on or relay_on
            if relay_on:
                since = self._pump_on_since.setdefault(aid, now)
                running = (now - since) >= self.current_delay
            else:
                self._pump_on_since.pop(aid, None)
                running = False
            rated = 0.0
            settings = (asset.get("aab") or {}).get("settings") or {}
            try:
                rated = float(settings.get("rated_current") or 0)
            except (TypeError, ValueError):
                rated = 0.0
            amps = (rated if rated > 0 else NOMINAL_CURRENT_A) if running else 0.0
            if amps and fault("dry_run", assetId=aid):
                amps *= DRY_RUN_FRACTION
            lost = {f["phase"] for f in fault("phase_loss", assetId=aid)}
            for key in ports:
                if key.startswith("sensor.current"):
                    phase = key.rsplit(".", 1)[-1] if key.count(".") >= 2 else None
                    value = 0.0 if phase in lost else (round(amps * random.uniform(0.97, 1.03), 2) if amps else 0.0)
                    self.io.set_input(aid, key, value)
                elif key.startswith("sensor.mains") or key.startswith("phase."):
                    phase = key.split(".")[1] if key.startswith("phase.") else key.rsplit(".", 1)[-1]
                    self.io.set_input(aid, key, phase not in lost)
            if self._last_running.get(aid) != running:
                self._last_running[aid] = running
                self.note(f"pump {asset.get('name') or aid}: {'current flowing' if running else 'stopped, 0 A'}")

        # Plots: valve open (+ pump on) → the plot's flow switch reads flowing.
        for plot in nab.get("plots", []) or []:
            if not isinstance(plot, dict):
                continue
            plot_id = plot.get("plotId")
            flow_var = plot.get("flowVariable")
            flow_asset = var_asset.get(flow_var) if flow_var else None
            if not flow_asset or flow_asset not in assets:
                continue
            open_since: Optional[float] = None
            for valve_var in plot.get("valveVariables", []) or []:
                valve_asset = var_asset.get(valve_var)
                if not valve_asset:
                    continue
                if bool(self.io.output(valve_asset, "device.power", False)):
                    since = self._valve_on_since.setdefault(valve_asset, now)
                    open_since = since if open_since is None else min(open_since, since)
                else:
                    self._valve_on_since.pop(valve_asset, None)
            flowing = open_since is not None and pump_relay_on and (now - open_since) >= self.flow_delay
            if fault("no_flow", plotId=plot_id):
                flowing = False
            if fault("flow_on_closed", plotId=plot_id):
                flowing = True
            flow_ports = [k for k in (assets[flow_asset].get("ports") or {}) if "flow" in k]
            for key in flow_ports:
                self.io.set_input(flow_asset, key, flowing)
            if self._last_flow.get(plot_id) != flowing:
                self._last_flow[plot_id] = flowing
                self.note(f"{plot.get('name') or plot_id}: {'water flowing' if flowing else 'no flow'}")

    # ----------------------------------------------------------------- private
    @staticmethod
    def _var_assets(nab: dict) -> dict:
        out = {}
        for v in nab.get("variables", []) or []:
            a = v.get("asset") if isinstance(v, dict) else None
            if isinstance(a, dict) and a.get("assetId"):
                out[v.get("name")] = a.get("assetId")
        return out

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:  # noqa: BLE001
                logger.debug("bench physics: %s", e)
            self._stop.wait(self.tick)


class _Port:
    """The two fields SimulatedIO.read_input needs."""

    def __init__(self, asset_id: str, source_key: str):
        self.asset_id = asset_id
        self.source_key = source_key
