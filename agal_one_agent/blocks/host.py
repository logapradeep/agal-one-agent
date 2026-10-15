"""ProgramHost — the node runs the bundle the cloud serves, whatever its language (contracts v1.15.0, ADR-029
decision 12; programs/README.md §7.2 "Coexistence").

The agent reports ``capabilities.blockLanguage = 3``. The cloud serves a v3 bundle only to a node that reports
3 AND whose v1 NAB is migrated (``/nodePrograms/{nodeUid}.migratedAt``, or no NAB); every other node keeps its
language-1 NAB bundle — including this one until its migration. So the host holds both runtimes and runs
the bundle it is given:

* a language-1 bundle (``language`` absent or 1) runs on :class:`~.runtime.BlockRuntime`, exactly as agent
  0.3.3 runs it;
* a language-3 bundle runs on :class:`~.v3.runtime.RuntimeV3`;
* language 2 was superseded before it was built: it is rejected.

A bundle of the language that runs takes over from the running program (each runtime's own bumpless swap).
A bundle of the other language — the migration, done once per node with the founder's yes — is checked
first; then the running runtime stops as on a clean stop (its safe state) and the new one loads cold, as
after a restart. The contract does not define a bumpless take-over across languages.

Both runtimes share one lock, so a switch command is checked and dispatched against whichever program runs.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from .clock import Clock, SystemClock
from .io import IOAdapter, PortRef
from .runtime import BlockRuntime, CompileError, EventSink
from .v3.runtime import RuntimeV3

logger = logging.getLogger(__name__)

BLOCK_LANGUAGE = 3  # what this agent reports as capabilities.blockLanguage (enums.yaml BlockLanguage)


def bundle_language(bundle: dict) -> int:
    lang = bundle.get("language", 1) if isinstance(bundle, dict) else 1
    return int(lang) if isinstance(lang, int) and not isinstance(lang, bool) else -1


class ProgramHost:
    def __init__(self, io: IOAdapter, sink: EventSink, clock: Optional[Clock] = None, state_dir: Optional[str] = None,
                 tick_seconds: float = 1.0, node_uid: str = ""):
        self.io = io
        self.sink = sink
        self.clock = clock or SystemClock()
        self._lock = threading.RLock()
        self.v1 = BlockRuntime(io, sink, clock=self.clock, state_dir=state_dir, tick_seconds=tick_seconds,
                               lock=self._lock, listen=False)
        self.v3 = RuntimeV3(io, sink, clock=self.clock, state_dir=state_dir, tick_seconds=tick_seconds,
                            node_uid=node_uid, lock=self._lock, listen=False)
        self.active = None
        self._started = False
        io.set_change_listener(self._on_input_change)

    # ------------------------------------------------------------- properties

    @property
    def runtime(self):
        """The runtime that runs the program (v1 until the node is migrated)."""
        return self.active or self.v1

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def loaded(self) -> bool:
        return self.active is not None and self.active.loaded

    @property
    def language(self) -> int:
        return 3 if self.active is self.v3 else 1

    @property
    def version(self) -> int:
        return self.runtime.version

    @property
    def bundle(self) -> Optional[dict]:
        return self.runtime.bundle

    @property
    def _running(self) -> bool:  # main.py checks whether the runtime thread runs
        return self.active is not None and self.active._running

    # ------------------------------------------------------------- compile

    def compile(self, bundle: dict) -> None:
        lang = bundle_language(bundle)
        if lang == 3:
            target = self.v3
        elif lang == 1:
            target = self.v1
        elif lang == 2:
            raise CompileError("block language 2 was superseded before it was built: this agent runs 1 and 3")
        else:
            raise CompileError(f"unknown block language {bundle.get('language')!r}")
        if self.active is None or target is self.active:
            target.compile(bundle)
            self.active = target
            return
        # The migration (v1 → v3), or back: check the new bundle first — a bundle that does not compile never
        # stops the running program. Then the running runtime stops as on a clean stop and the new one loads
        # cold. (Compiling cold leaves the outputs alone until its first pass; the old runtime is stopped
        # before that pass can run.)
        old = self.active
        was_running = old._running or self._started
        if old is self.v3 and self.v3.prog is not None and self.v3.prog.estop is not None and self.v3.prog.estop.latched:
            # README §7.10: nothing takes the latch off — a language without the E-stop would drop it, and the
            # motors would restart with no reset at the panel.
            raise CompileError("refused: the E-stop is latched at the panel — the node does not leave block language 3 "
                               "while it is latched; it clears only at the panel")
        prog3 = None
        if target is self.v3:
            from .v3 import program as P3
            prog3 = P3.build(bundle, self.io)  # raises CompileError: the running v1 program stays
            refusal = self.v3.latch_refusal(prog3)  # a latch the v3 runtime persisted before (a switch back and forth)
            if refusal:
                raise CompileError(refusal)
        else:
            target._build(bundle)
        logger.warning("program v%s is block language %d: stopping the language-%d runtime (its clean stop) and "
                       "starting the language-%d one cold", bundle.get("version"), lang, self.language, lang)
        old.stop(safe_state=True)
        if prog3 is not None:
            # The v1 program may have left a line on that the v3 program pulses as a latching coil (an output kept at
            # its last state by its safe state): every latching coil is written off before the v3 program starts.
            for drv in prog3.valves.values():
                for _, port in (drv.coils() if drv.latching else []):
                    try:
                        self.io.write_output(port, False)
                    except Exception as e:  # noqa: BLE001
                        logger.error("migration: writing the %s coil of valve %s off failed: %s", port.source_key, drv.asset_id, e)
        target.compile(bundle)
        self.active = target
        if was_running:
            target.start()

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._started = True
        if self.active is not None:
            self.active.start()

    def stop(self, safe_state: bool = True) -> None:
        self._started = False
        if self.active is not None:
            self.active.stop(safe_state=safe_state)

    def apply_safe_state(self) -> None:
        if self.active is not None:
            self.active.apply_safe_state()

    def step(self) -> None:
        if self.active is not None:
            self.active.step()

    def _on_input_change(self) -> None:
        if self.active is not None:
            self.active._on_input_change()

    # ------------------------------------------------------------- commands

    def apply_command(self, command: dict) -> dict:
        if self.active is None:
            return {"accepted": False, "reason": "no program loaded"}
        return self.active.apply_command(command)

    def owns_port(self, asset_id: Optional[str], source_key: str) -> bool:
        return self.active is not None and self.active.owns_port(asset_id, source_key)

    def port_links(self) -> list[PortRef]:
        return self.active.port_links() if self.active is not None else []

    def line_in_use(self, label, line) -> bool:
        """A chip line the running program reads or drives (a port test there is refused as busy)."""
        if self.active is self.v3:
            return self.v3.line_in_use(label, line)
        return self.port_for_line(label, line) is not None

    def port_for_line(self, label, line) -> Optional[tuple[str, str]]:
        """(assetId, sourceKey) of the v1 program variable on this chip line — a port test pulses it through
        the runtime. A v3 program never hands a line to a port test: its outputs are the built-ins' (a guard
        supervises a motor, a valve driver owns its coils), so a test there is refused as busy."""
        if self.active is not self.v1 or not self.v1.loaded or label is None or line is None:
            return None
        with self._lock:
            for aid, blk in self.v1.aabs.items():
                for v in blk.vars.values():
                    port = getattr(v, "port", None)
                    t = getattr(port, "transport", None) or {}
                    if t.get("chip") == label and t.get("line") is not None and int(t["line"]) == int(line):
                        return (aid, port.source_key)
        return None

    # ---------------------------------------------------------------- status

    def status_extra(self) -> dict:
        return self.v3.status_extra() if self.active is self.v3 else {}

    def snapshot(self) -> dict:
        return self.runtime.snapshot()
