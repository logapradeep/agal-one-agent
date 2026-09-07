"""Bench physics for a cloud-connected simulated node (rebuild P4 exit test).

With ``blocks.simulated_io: true`` the agent runs on a laptop with in-memory
ports and this thread answers the outputs the way the reference build's
plumbing would: a valve that has been open for ``flow_delay`` seconds while
the pump relay is on makes its plot's flow switch read *flowing*; a pump whose
relay has been on for ``current_delay`` seconds draws its rated current (or a
nominal 4.5 A when the rating is still 0 = "learn on first run"); three-phase
pumps read all three currents and all mains-sense inputs present. Nothing
here touches the cloud — the runtime publishes variables and alerts as it
would on real hardware, so the phone sees R-7 / R-8 / R-9 behaviour end to end.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Optional

from .io import SimulatedIO

logger = logging.getLogger(__name__)

NOMINAL_CURRENT_A = 4.5


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

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="bench-physics", daemon=True)
        self._thread.start()
        logger.warning("BENCH PHYSICS: simulated ports answer the program (valves → flow after %.0fs, pump → current after %.0fs)",
                       self.flow_delay, self.current_delay)

    def stop(self) -> None:
        self._stop.set()

    def step(self, now: Optional[float] = None) -> None:
        """One physics pass (also used by the tests without the thread)."""
        now = time.monotonic() if now is None else now
        bundle = getattr(self.runtime, "bundle", None)
        if not bundle:
            return
        assets = {a.get("assetId"): a for a in bundle.get("assets", []) if isinstance(a, dict)}
        nab = bundle.get("nab") or {}
        var_asset = {}
        for v in nab.get("variables", []) or []:
            a = v.get("asset") if isinstance(v, dict) else None
            if isinstance(a, dict) and a.get("assetId"):
                var_asset[v.get("name")] = a.get("assetId")

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
            for key in ports:
                if key.startswith("sensor.current"):
                    self.io.set_input(aid, key, round(amps * random.uniform(0.97, 1.03), 2) if amps else 0.0)
                elif key.startswith("sensor.mains") or key.startswith("phase."):
                    self.io.set_input(aid, key, True)

        # Plots: valve open (+ pump on) → the plot's flow switch reads flowing.
        for plot in nab.get("plots", []) or []:
            if not isinstance(plot, dict):
                continue
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
            flow_ports = [k for k in (assets[flow_asset].get("ports") or {}) if "flow" in k]
            for key in flow_ports:
                self.io.set_input(flow_asset, key, flowing)

    # ----------------------------------------------------------------- private
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:  # noqa: BLE001
                logger.debug("bench physics: %s", e)
            self._stop.wait(self.tick)
